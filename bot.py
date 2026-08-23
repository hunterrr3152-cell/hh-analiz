import os
import re
import json
import asyncio
import itertools
from typing import Dict, Any, List
import fitz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
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
        "lines": lines
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

def match_dekont(dekont: Dict[str, Any], stmt_info: Dict[str, Any]) -> str:
    if dekont.get("is_demand_only"):
        return (
            "🚨 <b>HESABA GEÇMEDİ / ŞÜPHELİ İŞLEM</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Gönderen:</b> {dekont.get('sender_name')}\n"
            f"🏢 <b>Alıcı:</b> {dekont.get('receiver_name')}\n"
            f"💰 <b>Tutar:</b> <code>{dekont.get('amount')} TL</code>\n"
            f"🔢 <b>Ref No:</b> <code>{dekont.get('sorgu_no')}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>Uyarı:</b> Dekont bir <b>FAST Talebi / Taslaktır</b>. Hesaba para girişi olmamıştır."
        )

    stmt_raw = stmt_info["raw_text"].upper()
    stmt_lines = stmt_info.get("lines", [])
    stmt_iban = stmt_info["iban"]
    
    rec_iban = dekont.get("receiver_iban", "")
    if rec_iban and stmt_iban != "Bilinmiyor" and stmt_iban not in rec_iban and rec_iban not in stmt_iban:
        return (
            "⚠️ <b>FARKLI HESABA GÖNDERİLMİŞ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Gönderen:</b> {dekont.get('sender_name')}\n"
            f"💰 <b>Tutar:</b> <code>{dekont.get('amount')} TL</code>\n"
            f"💳 <b>Hedef IBAN:</b> <code>{rec_iban}</code>\n"
            f"🏢 <b>Ekstre IBAN:</b> <code>{stmt_iban}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📌 Dekonttaki IBAN ekstreniz ile eşleşmiyor."
        )

    found = False
    proof = ""
    reason = ""

    sorgu = str(dekont.get("sorgu_no", "")).strip().upper()
    if sorgu and len(sorgu) >= 5 and sorgu in stmt_raw:
        found = True
        reason = f"Referans/Sorgu No Doğrulandı ({sorgu})"
        for l in stmt_lines:
            if sorgu in l.upper():
                proof = l
                break

    if not found and dekont.get("tckn") and len(dekont["tckn"]) == 11 and dekont["tckn"] in stmt_raw:
        found = True
        reason = f"TCKN Doğrulandı ({dekont['tckn']})"
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
                    reason = "İsim ve Tutar Doğrulandı"
                    proof = l
                    break

        if not found and amt_val >= 10.0:
            for l in stmt_lines:
                l_up = l.upper()
                is_inflow = any(k in l_up for k in ["ALACAK", "GELEN", "FAST", "EFT", "HAVALE", "+"]) and "BORÇ" not in l_up and "BORC" not in l_up
                if is_inflow and (f"{amt_val:.2f}" in l.replace(",", ".") or f"{int(amt_val)}" in l.replace(" ", "")):
                    found = True
                    reason = f"Tutar Para Girişi ile Eşleşti ({dekont.get('amount')} TL)"
                    proof = l
                    break

    if found:
        res = (
            "✅ <b>HESABA BAŞARIYLA GEÇTİ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Gönderen:</b> {dekont.get('sender_name')}\n"
            f"🏢 <b>Alıcı:</b> {dekont.get('receiver_name')}\n"
            f"💰 <b>Tutar:</b> <code>{dekont.get('amount')} TL</code>\n"
            f"📅 <b>Tarih:</b> {dekont.get('date_str')}\n"
            f"🔢 <b>Ref No:</b> <code>{dekont.get('sorgu_no')}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Yöntem:</b> {reason}\n"
        )
        if proof:
            res += f"📌 <b>Kayıt:</b> <code>{proof[:120]}</code>"
        return res

    return (
        "🚨 <b>HESABA GELMEDİ / EKSTREDE BULUNAMADI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Gönderen:</b> {dekont.get('sender_name')}\n"
        f"🏢 <b>Alıcı:</b> {dekont.get('receiver_name')}\n"
        f"💰 <b>Tutar:</b> <code>{dekont.get('amount')} TL</code>\n"
        f"🔢 <b>Ref No:</b> <code>{dekont.get('sorgu_no')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Bu dekonta ait tutar veya sorgu numarası hesap ekstresinde yer almamaktadır."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Dekont Doğrulama Botu</b>\n\n"
        "1. Önce bankanızdan indirdiğiniz <b>Hesap Ekstresi PDF</b> dosyasını gönderin.\n"
        "2. Ardından kontrol etmek istediğiniz dekontları (Fotoğraf veya PDF) iletin.",
        parse_mode="HTML"
    )

async def temizle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in chat_statements:
        del chat_statements[chat_id]
    await update.message.reply_text("🧹 Hafızadaki ekstre temizlendi. Yeni ekstre PDF yükleyebilirsiniz.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    doc = update.message.document
    file = await doc.get_file()
    file_bytes = bytes(await file.download_as_bytearray())

    if doc.file_name.lower().endswith(".pdf"):
        text = extract_text_from_pdf(file_bytes)
        if any(k in text.upper() for k in ["HESAP HAREKET", "EKSTRE", "HESAP OZETI", "HESAP ÖZETİ", "GUNCEL BAKIYE", "GÜNCEL BAKİYE"]):
            stmt_info = parse_statement_pdf(text)
            chat_statements[chat_id] = stmt_info
            await update.message.reply_text(
                f"✅ <b>Hesap ekstresi hafızaya alındı.</b>\n"
                f"💳 <b>IBAN:</b> <code>{stmt_info['iban']}</code>\n"
                f"📋 <b>Satır Sayısı:</b> {len(stmt_info['lines'])}\n\n"
                f"Dekontları gönderebilirsiniz.",
                parse_mode="HTML"
            )
            return

        if chat_id not in chat_statements:
            await update.message.reply_text("⚠️ Lütfen önce karşılaştırma yapılacak <b>Hesap Ekstresi PDF</b> dosyasını yükleyin.")
            return

        if len(text.strip()) > 60:
            dekont_data = parse_digital_pdf_dekont(text)
        else:
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            pix = pdf_doc[0].get_pixmap(dpi=150)
            dekont_data = await parse_image_with_gemini(pix.tobytes("jpeg"))
    else:
        if chat_id not in chat_statements:
            await update.message.reply_text("⚠️ Lütfen önce <b>Hesap Ekstresi PDF</b> dosyasını yükleyin.")
            return
        dekont_data = await parse_image_with_gemini(file_bytes)

    res = match_dekont(dekont_data, chat_statements[chat_id])
    await update.message.reply_text(res, parse_mode="HTML")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in chat_statements:
        await update.message.reply_text("⚠️ Lütfen önce karşılaştırma yapılacak <b>Hesap Ekstresi PDF</b> dosyasını yükleyin.")
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_bytes = bytes(await file.download_as_bytearray())

    dekont_data = await parse_image_with_gemini(img_bytes)
    res = match_dekont(dekont_data, chat_statements[chat_id])
    await update.message.reply_text(res, parse_mode="HTML")

if __name__ == "__main__":
    if not BOT_TOKEN:
        exit(1)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("temizle", temizle))
    app.add_handler(CommandHandler("reset", temizle))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()