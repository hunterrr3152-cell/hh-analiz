import os
import re
import json
import asyncio
import itertools
import base64
import httpx
from typing import Dict, Any, List, Tuple
import fitz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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
        "processed_count": 0,
        "matched_count": 0,
        "failed_count": 0,
        "matched_list": [],
        "failed_list": []
    }


def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Ekstre Durumu"), KeyboardButton("📝 Rapor Al")],
        [KeyboardButton("🧹 Ekstreyi Temizle"), KeyboardButton("❓ Nasıl Kullanılır?")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def process_dekont_with_ai(image_bytes: bytes, chat_id: int) -> Tuple[bool, str]:
    st = chat_statements[chat_id]
    
    async with st["lock"]:
        st["processed_count"] = st.get("processed_count", 0) + 1
        
        lines_text = "\n".join([f"{k}: {v}" for k, v in st["unmatched_lines"].items()])
        if len(lines_text) > 800000:
            lines_text = lines_text[:800000]
            
        prompt = f"""Görev: Ekteki dekont görüntüsünü analiz et ve aşağıdaki hesap ekstresi satırları arasında bu dekonta ait işlemi bul.

Ekstre IBAN'ı: {st['iban']}

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
            return False, "⚠️ <b>Hata:</b> Sisteme tanımlı hiçbir API anahtarı (Gemini veya Groq) bulunamadı."
        
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
                            raise Exception(f"Groq API Error: {resp.text}")
                        data = resp.json()
                        content = data["choices"][0]["message"]["content"]
                        if "```json" in content:
                            content = content.split("```json")[1].split("```")[0].strip()
                        elif "```" in content:
                            content = content.split("```")[1].split("```")[0].strip()
                        dekont_info = json.loads(content)
                        break
            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "exhausted" in err_msg or "rate" in err_msg:
                    await asyncio.sleep(2.0)
                else:
                    await asyncio.sleep(0.5)
                continue
                
        if not dekont_info:
            st["failed_count"] = st.get("failed_count", 0) + 1
            return False, "⚠️ <b>API Hatası:</b> Dekont okunamadı."

        sender = dekont_info.get("sender_name", "Belirsiz")
        amt = dekont_info.get("amount", "0.00")
        reason = dekont_info.get("reason", "")
        summary_line = f"{sender} | {amt} TL"
        
        if dekont_info.get("is_matched") and dekont_info.get("matched_line_id") in st["unmatched_lines"]:
            m_id = dekont_info["matched_line_id"]
            proof = st["unmatched_lines"].pop(m_id)
            st["matched_count"] = st.get("matched_count", 0) + 1
            st["matched_list"].append(f"✅ {summary_line}")
            
            msg = (
                "✅ <b>HESABA BAŞARIYLA GEÇTİ</b>\n"
                "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                f"👤 <b>Gönderen:</b> <code>{sender}</code>\n"
                f"💰 <b>Tutar:</b> <code>{amt} TL</code>\n"
                f"📅 <b>Tarih:</b> <code>{dekont_info.get('date', 'Belirsiz')}</code>\n"
                "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                f"🎯 <b>Yapay Zeka Notu:</b> <i>{reason}</i>\n"
                f"📌 <b>Kayıt:</b> <code>{proof[:100]}</code>"
            )
            return True, msg
        else:
            st["failed_count"] = st.get("failed_count", 0) + 1
            st["failed_list"].append(f"❌ {summary_line}")
            msg = (
                "❌ <b>EKSTREDE BULUNAMADI / REDDEDİLDİ</b>\n"
                "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                f"👤 <b>Gönderen:</b> <code>{sender}</code>\n"
                f"💰 <b>Tutar:</b> <code>{amt} TL</code>\n"
                f"📅 <b>Tarih:</b> <code>{dekont_info.get('date', 'Belirsiz')}</code>\n"
                "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                f"⚠️ <b>Yapay Zeka Notu:</b> <i>{reason}</i>"
            )
            return False, msg

async def set_telegram_reaction(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, is_success: bool):
    emoji = "👍" if is_success else "👎"
    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[{"type": "emoji", "emoji": emoji}]
        )
    except Exception:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 <b>Otomatik Dekont Doğrulama Botu</b>\n\n"
        "⚡ <b>Nasıl Kullanılır?</b>\n"
        "1️⃣ Bankanızdan indirdiğiniz <b>Hesap Ekstresi PDF</b> dosyasını buraya gönderin.\n"
        "2️⃣ Ardından kontrol edilecek tüm <b>Dekontları (Görsel veya PDF)</b> iletin.\n"
        "3️⃣ Bot her dekontu alıntılayarak kontrol eder ve mesajınıza doğrudan tepki bırakır."
    )
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=get_main_keyboard())

async def send_status(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    if chat_id not in chat_statements:
        inline_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❓ Yardım", callback_data="btn_help")]])
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ <b>Hafızada aktif bir hesap ekstresi bulunmuyor.</b>\nLütfen önce ekstrenizi PDF olarak gönderin.",
            parse_mode="HTML",
            reply_markup=inline_kb
        )
        return

    st = chat_statements[chat_id]
    msg = (
        "📊 <b>Aktif Oturum ve Ekstre Durumu</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>Ekstre IBAN:</b> <code>{st['iban']}</code>\n"
        f"📋 <b>Toplam Kayıt:</b> <code>{len(st['lines'])} satır</code>\n"
        f"🔄 <b>İncelenen Dekont:</b> <code>{st.get('processed_count', 0)} adet</code>\n"
        f"✅ <b>Doğrulanan (Geçen):</b> <code>{st.get('matched_count', 0)} adet</code>\n"
        f"❌ <b>Bulunamayan:</b> <code>{st.get('failed_count', 0)} adet</code>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    inline_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Ekstreyi Sıfırla", callback_data="btn_clear")]
    ])
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML", reply_markup=inline_kb)

async def send_report(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    if chat_id not in chat_statements:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ <b>Raporlanacak veri bulunamadı.</b>\nÖnce ekstre ve dekont yüklemelisiniz.",
            parse_mode="HTML"
        )
        return

    st = chat_statements[chat_id]
    if st.get('processed_count', 0) == 0:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Ekstre yüklendi ancak henüz hiç dekont taratmadınız.",
            parse_mode="HTML"
        )
        return

    msg = (
        "📝 <b>Genel İşlem Raporu</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Toplam İncelenen: {st.get('processed_count', 0)}\n\n"
        "✅ <b>ONAYLANAN İŞLEMLER</b>\n"
    )
    
    if st.get("matched_list"):
        msg += "\n".join(st["matched_list"]) + "\n"
    else:
        msg += "<i>Onaylanan işlem bulunamadı.</i>\n"

    msg += "\n❌ <b>REDDEDİLEN / BULUNAMAYANLAR</b>\n"
    
    if st.get("failed_list"):
        msg += "\n".join(st["failed_list"]) + "\n"
    else:
        msg += "<i>Reddedilen işlem bulunamadı.</i>\n"
        
    msg += "━━━━━━━━━━━━━━━━━━━━"

    if len(msg) > 4000:
        for x in range(0, len(msg), 4000):
            await context.bot.send_message(chat_id=chat_id, text=msg[x:x+4000], parse_mode="HTML")
    else:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")

async def clear_statement(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    if chat_id in chat_statements:
        del chat_statements[chat_id]
    await context.bot.send_message(
        chat_id=chat_id,
        text="🧹 <b>Hafızadaki ekstre başarıyla sıfırlandı.</b>\nYeni bir ekstre PDF dosyası yükleyebilirsiniz.",
        parse_mode="HTML"
    )

async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "📊 Ekstre Durumu":
        await send_status(chat_id, context)
    elif text == "📝 Rapor Al":
        await send_report(chat_id, context)
    elif text in ["🧹 Ekstreyi Temizle", "/temizle", "/reset"]:
        await clear_statement(chat_id, context)
    elif text == "❓ Nasıl Kullanılır?":
        await start(update, context)

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "btn_clear":
        await clear_statement(chat_id, context)
    elif query.data == "btn_help":
        await start(query, context)
    elif query.data == "btn_status":
        await send_status(chat_id, context)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    doc = update.message.document
    msg_id = update.message.message_id
    file = await doc.get_file()
    file_bytes = bytes(await file.download_as_bytearray())

    if doc.file_name.lower().endswith(".pdf"):
        text = extract_text_from_pdf(file_bytes)
        if any(k in text.upper() for k in ["HESAP HAREKET", "EKSTRE", "HESAP OZETI", "HESAP ÖZETİ", "GUNCEL BAKIYE", "GÜNCEL BAKİYE"]):
            stmt_info = parse_statement_pdf(text)
            stmt_info["lock"] = asyncio.Lock()
            chat_statements[chat_id] = stmt_info
            
            inline_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Durumu Görüntüle", callback_data="btn_status"), InlineKeyboardButton("🧹 Sıfırla", callback_data="btn_clear")]
            ])
            await update.message.reply_text(
                f"✅ <b>Hesap Ekstresi Başarıyla Yüklendi!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💳 <b>IBAN:</b> <code>{stmt_info['iban']}</code>\n"
                f"📋 <b>Satır Sayısı:</b> <code>{len(stmt_info['lines'])}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Şimdi kontrol edilecek dekontları toplu veya tek tek gönderebilirsiniz.",
                parse_mode="HTML",
                reply_to_message_id=msg_id,
                reply_markup=inline_kb
            )
            return

        if chat_id not in chat_statements:
            await update.message.reply_text(
                "⚠️ Lütfen önce karşılaştırma yapılacak <b>Hesap Ekstresi PDF</b> dosyasını yükleyin.",
                parse_mode="HTML",
                reply_to_message_id=msg_id
            )
            return

        jpeg_bytes = await asyncio.to_thread(pdf_to_jpeg_sync, file_bytes)
        is_matched, res_text = await process_dekont_with_ai(jpeg_bytes, chat_id)
    else:
        if chat_id not in chat_statements:
            await update.message.reply_text(
                "⚠️ Lütfen önce <b>Hesap Ekstresi PDF</b> dosyasını yükleyin.",
                parse_mode="HTML",
                reply_to_message_id=msg_id
            )
            return
        is_matched, res_text = await process_dekont_with_ai(file_bytes, chat_id)

    await update.message.reply_text(res_text, parse_mode="HTML", reply_to_message_id=msg_id)
    await set_telegram_reaction(context, chat_id, msg_id, is_matched)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    if chat_id not in chat_statements:
        await update.message.reply_text(
            "⚠️ Lütfen önce karşılaştırma yapılacak <b>Hesap Ekstresi PDF</b> dosyasını yükleyin.",
            parse_mode="HTML",
            reply_to_message_id=msg_id
        )
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_bytes = bytes(await file.download_as_bytearray())

    is_matched, res_text = await process_dekont_with_ai(img_bytes, chat_id)
    await update.message.reply_text(res_text, parse_mode="HTML", reply_to_message_id=msg_id)
    await set_telegram_reaction(context, chat_id, msg_id, is_matched)

if __name__ == "__main__":
    if not BOT_TOKEN:
        exit(1)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("durum", lambda u, c: send_status(u.effective_chat.id, c)))
    app.add_handler(CommandHandler("rapor", lambda u, c: send_report(u.effective_chat.id, c)))
    app.add_handler(CommandHandler("temizle", lambda u, c: clear_statement(u.effective_chat.id, c)))
    app.add_handler(CommandHandler("reset", lambda u, c: clear_statement(u.effective_chat.id, c)))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_menu))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()
