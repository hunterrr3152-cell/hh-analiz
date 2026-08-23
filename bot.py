import os
import re
import json
import asyncio
import itertools
import base64
import httpx
import time
from typing import Dict, Any, Tuple
import fitz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from google import genai
from google.genai import types

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RAW_KEYS = os.environ.get("GEMINI_API_KEYS", "")
GEMINI_KEYS = [k.strip() for k in RAW_KEYS.split(",") if k.strip()]
if not GEMINI_KEYS and os.environ.get("GEMINI_API_KEY"):
    GEMINI_KEYS = [os.environ.get("GEMINI_API_KEY")]

RAW_GROQ = os.environ.get("GROQ_API_KEYS", "")
GROQ_KEYS = [k.strip() for k in RAW_GROQ.split(",") if k.strip()]

API_POOL = []
for k in GEMINI_KEYS:
    API_POOL.append({"type": "gemini", "key": k, "client": genai.Client(api_key=k)})
for k in GROQ_KEYS:
    API_POOL.append({"type": "groq", "key": k})

api_cycle = itertools.cycle(API_POOL) if API_POOL else None
chat_statements: Dict[int, Dict[str, Any]] = {}

AUTH_USERS_RAW = os.environ.get("YETKILI_KISILER", "")
AUTH_USERS = [u.strip().lower() for u in AUTH_USERS_RAW.split(",") if u.strip()]

def is_auth(update: Update) -> bool:
    chat = update.effective_chat
    if chat and chat.type == "private":
        return False
    user = update.effective_user
    if not user:
        return False
    uname = f"@{user.username.lower()}" if user.username else ""
    uid = str(user.id)
    if not AUTH_USERS:
        return True
    return uname in AUTH_USERS or uid in AUTH_USERS

def pdf_to_jpeg_sync(file_bytes: bytes) -> bytes:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    return doc[0].get_pixmap(dpi=150).tobytes("jpeg")

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return "".join(page.get_text("text") + "\n" for page in doc)

def parse_statement_pdf(text: str) -> Dict[str, Any]:
    ibans = re.findall(r'TR\s*\d{2}\s*(?:\d{4}\s*){5}\d{2}', text, re.IGNORECASE)
    stmt_iban = re.sub(r'\s+', '', ibans[0]).upper() if ibans else "Bilinmiyor"
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 3]
    return {
        "iban": stmt_iban,
        "raw_text": text,
        "lines": lines,
        "unmatched_lines": {i: l for i, l in enumerate(lines)},
        "queued_dekonts": [],
        "processed_count": 0,
        "matched_count": 0,
        "failed_count": 0,
        "failed_list": [],
        "state": "IDLE",
        "lock": asyncio.Lock(),
        "panel_msg_id": None
    }

def get_menu_text(st: Dict[str, Any]) -> str:
    state = st["state"]
    iban = st.get("iban", "Bilinmiyor")
    lines = st.get("lines", [])
    unmatched = st.get("unmatched_lines", {})
    queued = len(st.get("queued_dekonts", []))
    
    text = "🤖 <b>Dekont Analiz Kontrol Paneli</b>\n"
    text += "━━━━━━━━━━━━━━━━━━━\n"
    text += f"🏦 <b>Ekstre Durumu:</b> {'✅ Yüklü' if iban != 'Bilinmiyor' else '❌ Yüklü Değil'}\n"
    if iban != "Bilinmiyor":
        text += f"💳 <b>IBAN:</b> <code>{iban}</code>\n"
        text += f"📋 <b>Toplam İşlem:</b> <code>{len(lines)} adet</code>\n"
        text += f"⏳ <b>Eşleşme Bekleyen:</b> <code>{len(unmatched)} adet</code>\n"
    text += "━━━━━━━━━━━━━━━━━━━\n"
    text += f"📦 <b>Kuyruktaki Dekontlar:</b> <code>{queued} adet</code>\n"
    text += "━━━━━━━━━━━━━━━━━━━\n\n"
    
    if state == "WAIT_STATEMENT":
        text += "📂 <i>Lütfen PDF formatındaki hesap ekstresini gruba gönderin...</i>"
    elif state == "WAIT_DEKONT":
        text += "🧾 <i>Lütfen dekontları (Fotoğraf veya PDF) gruba gönderin...\nYükledikçe yukarıdaki 'Kuyruktaki Dekontlar' sayısı canlı olarak artacaktır.\nİşleminiz bitince Analize Başla butonuna tıklayın.</i>"
    elif state == "ANALYZING":
        total = queued + st["processed_count"]
        text += f"⏳ <b>Analiz Ediliyor...</b> ({st['processed_count']} / {total})\n"
        text += f"✅ Başarılı: {st['matched_count']} | ❌ Başarısız: {st['failed_count']}"
    else:
        text += "⚙️ <i>Lütfen yapmak istediğiniz işlemi aşağıdan seçin.</i>"
        
    return text

def get_menu_keyboard(state: str) -> InlineKeyboardMarkup:
    if state == "WAIT_STATEMENT":
        kb = [[InlineKeyboardButton("🔙 İptal", callback_data="cmd_cancel")]]
    elif state == "WAIT_DEKONT":
        kb = [
            [InlineKeyboardButton("▶️ Analize Başla", callback_data="cmd_analyze")],
            [InlineKeyboardButton("🔙 İptal", callback_data="cmd_cancel")]
        ]
    elif state == "ANALYZING":
        kb = [[InlineKeyboardButton("🛑 Analizi Durdur", callback_data="cmd_stop")]]
    else:
        kb = [
            [InlineKeyboardButton("📂 Hesap Hareketleri Yükle", callback_data="cmd_upload_stmt")],
            [InlineKeyboardButton("🧾 Dekont Yükle", callback_data="cmd_upload_dekont")],
            [InlineKeyboardButton("▶️ Analize Başla", callback_data="cmd_analyze")],
            [InlineKeyboardButton("🛑 Sıfırla", callback_data="cmd_reset")]
        ]
    return InlineKeyboardMarkup(kb)

async def cmd_analiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update):
        return
    chat_id = update.effective_chat.id
    if chat_id not in chat_statements:
        chat_statements[chat_id] = parse_statement_pdf("")
    st = chat_statements[chat_id]
    st["state"] = "IDLE"
    msg = await update.message.reply_text(get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
    st["panel_msg_id"] = msg.message_id

async def process_dekont_with_ai(image_bytes: bytes, chat_id: int) -> Tuple[bool, str]:
    st = chat_statements[chat_id]
    
    unmatched_copy = dict(st.get("unmatched_lines", {}))
    if not unmatched_copy:
        return False, "❌ [DEKONT] Ekstrede işlem kalmadı"
        
    lines_text = "\n".join([f"{k}: {v}" for k, v in unmatched_copy.items()])
    if len(lines_text) > 800000:
        lines_text = lines_text[:800000]
        
    prompt = f"""Görev: Ekteki dekont görüntüsünü analiz et ve aşağıdaki hesap ekstresi satırları arasında bu dekonta ait işlemi bul.
Ekstre IBAN'ı: {st.get('iban', '')}
Hesap Ekstresi Satırları (Format -> ID: Satır Metni):
{lines_text}
Kurallar:
1. Dekonttaki Alıcı IBAN ile Ekstre IBAN'ı uyuşmalıdır. Dekontta IBAN gizlenmişse (örn: TR12****34) veya Kolay Adres (Telefon/TC) ise, uyumlu olup olmadığına bak. Kesin uyumsuzsa reddet.
2. Dekont tarihi ile ekstre tarihi uyuşmalıdır (EFT/FAST valör farklarından dolayı 1-2 gün tolere edilebilir).
3. Tutar kuruşu kuruşuna uyuşmalıdır.
4. Sadece taslak/talep olan (gerçekleşmemiş) veya dekont olmayan görselleri reddet.
Lütfen JSON dön:
{{
  "sender_name": "Gönderen Adı",
  "amount": "Tutar",
  "date": "Tarih",
  "is_matched": true/false,
  "matched_line_id": eşleşen satırın ID'si (eşleşme yoksa -1),
  "reason": "Neden eşleşti veya eşleşmedi (kısa açıklama)"
}}"""
    schema = {
        "type": "OBJECT",
        "properties": {
            "sender_name": {"type": "STRING"},
            "amount": {"type": "STRING"},
            "date": {"type": "STRING"},
            "is_matched": {"type": "BOOLEAN"},
            "matched_line_id": {"type": "INTEGER"},
            "reason": {"type": "STRING"}
        },
        "required": ["sender_name", "amount", "date", "is_matched", "matched_line_id", "reason"]
    }
    
    dekont_info = {}
    if not API_POOL:
        return False, "API Yok"
        
    for _ in range(len(API_POOL)):
        api_node = next(api_cycle)
        try:
            if api_node["type"] == "gemini":
                response = await asyncio.to_thread(
                    api_node["client"].models.generate_content,
                    model='gemini-3.5-flash',
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                        prompt
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=0.0
                    )
                )
                dekont_info = json.loads(response.text)
                break
            elif api_node["type"] == "groq":
                b64_img = base64.b64encode(image_bytes).decode('utf-8')
                payload = {
                    "model": "qwen/qwen3.6-27b",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt + "\n\nLütfen sadece yukarıdaki JSON formatında çıktı ver, başka hiçbir açıklama ekleme."},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{b64_img}"
                                    }
                                }
                            ]
                        }
                    ],
                    "temperature": 0.0
                }
                headers = {
                    "Authorization": f"Bearer {api_node['key']}",
                    "Content-Type": "application/json"
                }
                async with httpx.AsyncClient() as http_client:
                    resp = await http_client.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=30.0)
                    if resp.status_code != 200:
                        raise Exception()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    if "```json" in content:
                        content = content.split("```json")[1].split("```")[0].strip()
                    elif "```" in content:
                        content = content.split("```")[1].split("```")[0].strip()
                    dekont_info = json.loads(content)
                    break
        except Exception:
            await asyncio.sleep(1.0)
            continue
            
    if not dekont_info:
        return False, "❌ Okunamadı (API Hatası)"
        
    sender = dekont_info.get("sender_name", "?")
    amt = dekont_info.get("amount", "0")
    reason = dekont_info.get("reason", "")
    summary_line = f"👤 {sender} | 💰 {amt} TL"
    
    async with st["lock"]:
        if dekont_info.get("is_matched") and dekont_info.get("matched_line_id") in st["unmatched_lines"]:
            st["unmatched_lines"].pop(dekont_info["matched_line_id"])
            return True, summary_line
        elif dekont_info.get("is_matched"):
            return False, f"❌ {summary_line}\n⚠️ <i>Satır başka bir dekont ile eşleşti (Çakışma)</i>"
        else:
            return False, f"❌ {summary_line}\n⚠️ <i>{reason}</i>"

async def analyze_worker(file_bytes: bytes, chat_id: int, sem: asyncio.Semaphore) -> Tuple[bool, str, bytes]:
    async with sem:
        is_matched, res_text = await process_dekont_with_ai(file_bytes, chat_id)
        return is_matched, res_text, file_bytes

async def run_analysis(chat_id: int, context: ContextTypes.DEFAULT_TYPE, msg_id: int):
    st = chat_statements.get(chat_id)
    if not st or st["state"] != "ANALYZING":
        return
        
    dekonts_to_process = list(st["queued_dekonts"])
    total = len(dekonts_to_process)
    if total == 0:
        return
        
    sem = asyncio.Semaphore(15)
    tasks = [asyncio.create_task(analyze_worker(f, chat_id, sem)) for f in dekonts_to_process]
    
    last_update = time.time()
    for coro in asyncio.as_completed(tasks):
        if st["state"] != "ANALYZING":
            break
        is_matched, res_text, f_bytes = await coro
        st["processed_count"] += 1
        if is_matched:
            st["matched_count"] += 1
        else:
            st["failed_count"] += 1
            st["failed_list"].append(res_text)
            
        try:
            st["queued_dekonts"].remove(f_bytes)
        except ValueError:
            pass
            
        if time.time() - last_update > 2 or st["processed_count"] == total:
            try:
                await context.bot.edit_message_text(get_menu_text(st), chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=get_menu_keyboard("ANALYZING"))
            except Exception:
                pass
            last_update = time.time()
            
    if st["state"] == "ANALYZING":
        st["state"] = "IDLE"
        final_text = get_menu_text(st) + "\n\n🎯 <b>Analiz Tamamlandı!</b>\n\n"
        if st["failed_list"]:
            final_text += "<b>⚠️ Başarısız İşlem Detayları:</b>\n" + "\n\n".join(st["failed_list"])
        if len(final_text) > 4000:
            final_text = final_text[:4000]
        try:
            await context.bot.edit_message_text(final_text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
        except Exception:
            await context.bot.send_message(chat_id, final_text, parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_auth(update):
        await query.answer("Yetkiniz yok!", show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat_id
    if chat_id not in chat_statements:
        chat_statements[chat_id] = parse_statement_pdf("")
    st = chat_statements[chat_id]
    d = query.data
    st["panel_msg_id"] = query.message.message_id

    if d == "cmd_upload_stmt":
        st["state"] = "WAIT_STATEMENT"
        await query.edit_message_text(get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard(st["state"]))
    elif d == "cmd_upload_dekont":
        if not st.get("iban") or st["iban"] == "Bilinmiyor":
            st["state"] = "IDLE"
            try:
                await query.edit_message_text("⚠️ <b>Önce Hesap Hareketleri Yüklemelisiniz!</b>\n\n" + get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
            except Exception:
                pass
            return
        st["state"] = "WAIT_DEKONT"
        await query.edit_message_text(get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard(st["state"]))
    elif d == "cmd_cancel":
        st["state"] = "IDLE"
        await query.edit_message_text(get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
    elif d == "cmd_reset":
        st["unmatched_lines"] = {i: l for i, l in enumerate(st.get("lines", []))}
        st["queued_dekonts"] = []
        st["processed_count"] = 0
        st["matched_count"] = 0
        st["failed_count"] = 0
        st["failed_list"] = []
        st["state"] = "IDLE"
        await query.edit_message_text(get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
    elif d == "cmd_stop":
        st["state"] = "IDLE"
        await query.edit_message_text(get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
    elif d == "cmd_analyze":
        if not st["queued_dekonts"]:
            st["state"] = "IDLE"
            try:
                await query.edit_message_text("⚠️ <b>Kuyrukta analiz edilecek dekont yok!</b>\n\n" + get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
            except Exception:
                pass
            return
        st["state"] = "ANALYZING"
        await query.edit_message_text(get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("ANALYZING"))
        asyncio.create_task(run_analysis(chat_id, context, query.message.message_id))

async def handle_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update):
        return
    chat_id = update.effective_chat.id
    if chat_id not in chat_statements:
        return
    st = chat_statements[chat_id]
    state = st.get("state", "IDLE")

    if state == "WAIT_STATEMENT":
        doc = update.message.document
        if not doc or not doc.file_name.lower().endswith(".pdf"):
            return
        file = await doc.get_file()
        file_bytes = bytes(await file.download_as_bytearray())
        text = extract_text_from_pdf(file_bytes)
        if any(k in text.upper() for k in ["HESAP HAREKET", "EKSTRE", "HESAP OZETI", "HESAP ÖZETİ", "GUNCEL BAKIYE", "GÜNCEL BAKİYE"]):
            n_st = parse_statement_pdf(text)
            n_st["state"] = "IDLE"
            n_st["panel_msg_id"] = st.get("panel_msg_id")
            chat_statements[chat_id] = n_st
            try:
                await update.message.delete()
            except Exception:
                pass
            if n_st["panel_msg_id"]:
                try:
                    await context.bot.edit_message_text(get_menu_text(n_st), chat_id=chat_id, message_id=n_st["panel_msg_id"], parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
                except Exception:
                    pass
    
    elif state == "WAIT_DEKONT":
        file_bytes = None
        if update.message.photo:
            photo = update.message.photo[-1]
            file = await photo.get_file()
            file_bytes = bytes(await file.download_as_bytearray())
        elif update.message.document:
            doc = update.message.document
            file = await doc.get_file()
            f_bytes = bytes(await file.download_as_bytearray())
            if doc.file_name.lower().endswith(".pdf"):
                file_bytes = await asyncio.to_thread(pdf_to_jpeg_sync, f_bytes)
            else:
                file_bytes = f_bytes
        if file_bytes:
            st["queued_dekonts"].append(file_bytes)
            try:
                await update.message.delete()
            except Exception:
                pass
            if st.get("panel_msg_id"):
                try:
                    await context.bot.edit_message_text(get_menu_text(st), chat_id=chat_id, message_id=st["panel_msg_id"], parse_mode="HTML", reply_markup=get_menu_keyboard(st["state"]))
                except Exception:
                    pass

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

if __name__ == "__main__":
    if not BOT_TOKEN:
        exit(1)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("analiz", cmd_analiz))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_files))
    app.add_handler(MessageHandler(filters.ALL, fallback))
    app.run_polling()
