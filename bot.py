import os
import re
import json
import asyncio
import itertools
from typing import Dict, Any, List, Tuple
import fitz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from google import genai
from google.genai import types

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RAW_KEYS = os.environ.get("GEMINI_API_KEYS", "")
API_KEYS = [k.strip() for k in RAW_KEYS.split(",") if k.strip()]

if not API_KEYS and os.environ.get("GEMINI_API_KEY"):
    API_KEYS = [os.environ.get("GEMINI_API_KEY")]

clients = [genai.Client(api_key=k) for k in API_KEYS]
client_cycle = itertools.cycle(clients) if clients else None

chat_statements: Dict[int, Dict[str, Any]] = {}
api_semaphore = asyncio.Semaphore(max(1, len(API_KEYS) * 2))

PROMPT_DEKONT = """
Bu bir banka dekontu veya para transfer ekran görüntüsüdür.
Aşağıdaki bilgileri JSON şemasına uygun olarak eksiksiz çıkar:
- is_demand_only: Transfer tamamlanmış kesin bir işlem ise false; taslak, FAST talebi, onay bekleyen veya iptal/bekleyen işlem ise true.
- sender_bank: Gönderen banka adı.
- receiver_bank: Alıcı banka adı.
- sender_name: Gönderen kişi veya kurum adı.
- receiver_name: Alıcı kişi veya kurum adı.
- receiver_iban: Alıcının TR ile başlayan boşluksuz IBAN numarası.
- amount: Sadece sayısal tutar (Örn: 1250.50).
- sorgu_no: FAST sorgu no, referans no, işlem no veya dekont takip numarası.
- date_str: İşlem tarihi ve saati.
- tckn: Varsa TCKN veya VKN.
"""

JSON_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_demand_only": {"type": "BOOLEAN"},
        "sender_bank": {"type": "STRING"},
        "receiver_bank": {"type": "STRING"},
        "sender_name": {"type": "STRING"},
        "receiver_name": {"type": "STRING"},
        "receiver_iban": {"type": "STRING"},
        "amount": {"type": "STRING"},
        "sorgu_no": {"type": "STRING"},
        "date_str": {"type": "STRING"},
        "tckn": {"type": "STRING"}
    },
    "required": ["is_demand_only", "sender_name", "receiver_name", "amount", "sorgu_no"]
}

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Ekstre Durumu"), KeyboardButton("🧹 Ekstreyi Temizle")],
        [KeyboardButton("❓ Nasıl Kullanılır?")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def normalize_text(text: str) -> str:
    if not text:
        return ""
    t = str(text).strip()
    tr_map = {
        "i": "I", "ı": "I", "İ": "I", "I": "I", "ş": "S", "Ş": "S",
        "ğ": "G", "Ğ": "G", "ü": "U", "Ü": "U", "ö": "O", "Ö": "O", "ç": "C", "Ç": "C"
    }
    for k, v in tr_map.items():
        t = t.replace(k, v)
    return re.sub(r"[^A-Z0-9\s]", " ", t.upper())

async def parse_image_with_gemini(image_bytes: bytes) -> Dict[str, Any]:
    if not clients:
        return {}
    async with api_semaphore:
        for _ in range(len(clients)):
            client = next(client_cycle)
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model='gemini-2.5-flash-lite',
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                        PROMPT_DEKONT
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=JSON_SCHEMA,
                        temperature=0.0
                    )
                )
                return json.loads(response.text)
            except Exception:
                await asyncio.sleep(0.3)
                continue
    return {}

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
        "processed_count": 0,
        "matched_count": 0,
        "failed_count": 0
    }

def parse_digital_pdf_dekont(text: str) -> Dict[str, Any]:
    t_up = text.upper()
    is_demand = any(k in t_up for k in ["GİDEN FAST TALEP", "GIDEN FAST TALEP", "FAST TALEP", "İŞLEM TASLAĞI", "BEKLEYEN İŞLEM"])
    
    ibans = re.findall(r'TR\s*\d{2}\s*(?:\d{4}\s*){5}\d{2}', text, re.IGNORECASE)
    rec_iban = re.sub(r'\s+', '', ibans[-1]).upper() if ibans else ""
    
    amt_match = re.search(r'(?:TUTAR|TUTARI|ÖDEME\s*TUTARI)\s*[:.]?\s*(\d+(?:[.,]\d+)*)', text, re.IGNORECASE)
    amount = amt_match.group(1) if amt_match else "0.0"
    
    sorgu_match = re.search(r'(?:Sorgu\s*No|Referans\s*No|İşlem\s*No|Dekont\s*No|Ref\s*No)\s*[:.]?\s*([0-9A-Za-z-]{6,35})', text, re.IGNORECASE)
    sorgu = sorgu_match.group(1) if sorgu_match else ""
    
    sender_match = re.search(r'(?:Gönderen|Ödeyen|Müşteri\s*Adı)\s*[:.]?\s*([A-ZÇĞİÖŞÜa-zçğıöşü\s.]{3,35})', text, re.IGNORECASE)
    sender = sender_match.group(1).strip() if sender_match else "Belirsiz"
    
    rec_match = re.search(r'(?:Alıcı|Alacaklı|Lehtar)\s*[:.]?\s*([A-ZÇĞİÖŞÜa-zçğıöşü\s.]{3,35})', text, re.IGNORECASE)
    receiver = rec_match.group(1).strip() if rec_match else "Belirsiz"

    return {
        "is_demand_only": is_demand,
        "sender_bank": "",
        "receiver_bank": "",
        "sender_name": sender,
        "receiver_name": receiver,
        "receiver_iban": rec_iban,
        "amount": amount,
        "sorgu_no": sorgu,
        "date_str": "",
        "tckn": ""
    }

def match_dekont(dekont: Dict[str, Any], stmt_info: Dict[str, Any]) -> Tuple[bool, str]:
    stmt_info["processed_count"] = stmt_info.get("processed_count", 0) + 1

    if dekont.get("is_demand_only"):
        stmt_info["failed_count"] = stmt_info.get("failed_count", 0) + 1
        msg = (
            "🚨 <b>HESABA GEÇMEDİ (TALEP / TASLAK)</b>\n"
            "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
            f"👤 <b>Gönderen:</b> <code>{dekont.get('sender_name', 'Belirsiz')}</code>\n"
            f"🏢 <b>Alıcı:</b> <code>{dekont.get('receiver_name', 'Belirsiz')}</code>\n"
            f"💰 <b>Tutar:</b> <code>{dekont.get('amount', '0.00')} TL</code>\n"
            f"🔢 <b>Ref:</b> <code>{dekont.get('sorgu_no', 'Belirsiz')}</code>\n"
            "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
            "⚠️ <i>Bu işlem sadece FAST talebidir. Para girişi olmamıştır.</i>"
        )
        return False, msg

    stmt_raw = stmt_info["raw_text"].upper()
    stmt_lines = stmt_info.get("lines", [])
    stmt_iban = stmt_info["iban"]
    
    rec_iban = dekont.get("receiver_iban", "")
    if rec_iban and stmt_iban != "Bilinmiyor" and stmt_iban not in rec_iban and rec_iban not in stmt_iban:
        stmt_info["failed_count"] = stmt_info.get("failed_count", 0) + 1
        msg = (
            "⚠️ <b>FARKLI HESABA GÖNDERİLMİŞ</b>\n"
            "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
            f"👤 <b>Gönderen:</b> <code>{dekont.get('sender_name', 'Belirsiz')}</code>\n"
            f"💰 <b>Tutar:</b> <code>{dekont.get('amount', '0.00')} TL</code>\n"
            f"💳 <b>Hedef IBAN:</b> <code>{rec_iban}</code>\n"
            f"🏢 <b>Ekstre IBAN:</b> <code>{stmt_iban}</code>\n"
            "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
            "📌 <i>Dekonttaki alıcı IBAN sizin ekstrenizle uyuşmuyor.</i>"
        )
        return False, msg

    found = False
    proof = ""
    reason = ""

    sorgu = str(dekont.get("sorgu_no", "")).strip().upper()
    if sorgu and len(sorgu) >= 5 and sorgu in stmt_raw:
        found = True
        reason = f"Ref/Sorgu No ({sorgu})"
        for l in stmt_lines:
            if sorgu in l.upper():
                proof = l
                break

    if not found and dekont.get("tckn") and len(dekont["tckn"]) == 11 and dekont["tckn"] in stmt_raw:
        found = True
        reason = f"TCKN ({dekont['tckn']})"
        for l in stmt_lines:
            if dekont["tckn"] in l:
                proof = l
                break

    if not found:
        sender_norm = normalize_text(dekont.get("sender_name", ""))
        amt_raw = str(dekont.get("amount", "")).replace(" ", "").replace(",", ".")
        try:
            amt_val = float(re.sub(r"[^\d.]", "", amt_raw))
        except Exception:
            amt_val = 0.0

        for l in stmt_lines:
            l_norm = normalize_text(l)
            if sender_norm and len(sender_norm) > 3 and sender_norm.split()[-1] in l_norm:
                if amt_val > 0 and (f"{amt_val:.2f}" in l.replace(",", ".") or f"{int(amt_val)}" in l.replace(" ", "")):
                    found = True
                    reason = "İsim + Tutar Eşleşti"
                    proof = l
                    break

        if not found and amt_val >= 10.0:
            for l in stmt_lines:
                l_up = l.upper()
                is_inflow = any(k in l_up for k in ["ALACAK", "GELEN", "FAST", "EFT", "HAVALE", "+"]) and "BORÇ" not in l_up and "BORC" not in l_up
                if is_inflow and (f"{amt_val:.2f}" in l.replace(",", ".") or f"{int(amt_val)}" in l.replace(" ", "")):
                    found = True
                    reason = f"Tutar Para Girişi ({dekont.get('amount')} TL)"
                    proof = l
                    break

    if found:
        stmt_info["matched_count"] = stmt_info.get("matched_count", 0) + 1
        msg = (
            "✅ <b>HESABA BAŞARIYLA GEÇTİ</b>\n"
            "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
            f"👤 <b>Gönderen:</b> <code>{dekont.get('sender_name', 'Belirsiz')}</code>\n"
            f"🏢 <b>Alıcı:</b> <code>{dekont.get('receiver_name', 'Belirsiz')}</code>\n"
            f"💰 <b>Tutar:</b> <code>{dekont.get('amount', '0.00')} TL</code>\n"
            f"📅 <b>Tarih:</b> <code>{dekont.get('date_str', 'Belirsiz')}</code>\n"
            f"🔢 <b>Ref:</b> <code>{dekont.get('sorgu_no', 'Belirsiz')}</code>\n"
            "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
            f"🎯 <b>Eşleşme:</b> <i>{reason}</i>\n"
        )
        if proof:
            msg += f"📌 <b>Kayıt:</b> <code>{proof[:100]}</code>"
        return True, msg

    stmt_info["failed_count"] = stmt_info.get("failed_count", 0) + 1
    msg = (
        "❌ <b>EKSTREDE BULUNAMADI</b>\n"
        "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
        f"👤 <b>Gönderen:</b> <code>{dekont.get('sender_name', 'Belirsiz')}</code>\n"
        f"🏢 <b>Alıcı:</b> <code>{dekont.get('receiver_name', 'Belirsiz')}</code>\n"
        f"💰 <b>Tutar:</b> <code>{dekont.get('amount', '0.00')} TL</code>\n"
        f"🔢 <b>Ref:</b> <code>{dekont.get('sorgu_no', 'Belirsiz')}</code>\n"
        "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
        "⚠️ <i>Bu işlem hesap hareketlerinizde mevcut değildir.</i>"
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
            await set_telegram_reaction(context, chat_id, msg_id, True)
            return

        if chat_id not in chat_statements:
            await update.message.reply_text(
                "⚠️ Lütfen önce karşılaştırma yapılacak <b>Hesap Ekstresi PDF</b> dosyasını yükleyin.",
                parse_mode="HTML",
                reply_to_message_id=msg_id
            )
            return

        if len(text.strip()) > 60:
            dekont_data = parse_digital_pdf_dekont(text)
        else:
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            pix = pdf_doc[0].get_pixmap(dpi=150)
            dekont_data = await parse_image_with_gemini(pix.tobytes("jpeg"))
    else:
        if chat_id not in chat_statements:
            await update.message.reply_text(
                "⚠️ Lütfen önce <b>Hesap Ekstresi PDF</b> dosyasını yükleyin.",
                parse_mode="HTML",
                reply_to_message_id=msg_id
            )
            return
        dekont_data = await parse_image_with_gemini(file_bytes)

    is_matched, res_text = match_dekont(dekont_data, chat_statements[chat_id])
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

    dekont_data = await parse_image_with_gemini(img_bytes)
    is_matched, res_text = match_dekont(dekont_data, chat_statements[chat_id])
    await update.message.reply_text(res_text, parse_mode="HTML", reply_to_message_id=msg_id)
    await set_telegram_reaction(context, chat_id, msg_id, is_matched)

if __name__ == "__main__":
    if not BOT_TOKEN:
        exit(1)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("durum", lambda u, c: send_status(u.effective_chat.id, c)))
    app.add_handler(CommandHandler("temizle", lambda u, c: clear_statement(u.effective_chat.id, c)))
    app.add_handler(CommandHandler("reset", lambda u, c: clear_statement(u.effective_chat.id, c)))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_menu))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()
