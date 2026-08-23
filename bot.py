import os
import re
import json
import asyncio
import itertools
import httpx
import time
import difflib
from datetime import datetime
from typing import Dict, Any, Tuple
import fitz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from google import genai
from google.genai import types

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

RAW_KEYS = os.environ.get("GEMINI_API_KEYS", "")
GEMINI_KEYS = list(set([k.strip() for k in RAW_KEYS.split(",") if k.strip()]))
if not GEMINI_KEYS and os.environ.get("GEMINI_API_KEY"):
    GEMINI_KEYS = [os.environ.get("GEMINI_API_KEY").strip()]

API_POOL = []
for k in GEMINI_KEYS:
    API_POOL.append({"type": "gemini", "key": k, "client": genai.Client(api_key=k), "rpm_limit": 25, "usage": []})

GROQ_RAW = os.environ.get("GROQ_API_KEYS", "")
GROQ_KEYS = list(set([k.strip() for k in GROQ_RAW.split(",") if k.strip()]))
if not GROQ_KEYS and os.environ.get("GROQ_API_KEY"):
    GROQ_KEYS = [os.environ.get("GROQ_API_KEY").strip()]

for k in GROQ_KEYS:
    API_POOL.append({"type": "groq", "key": k, "rpm_limit": 14, "usage": []})

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
    return doc[0].get_pixmap(dpi=72).tobytes("jpeg")

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return "".join(page.get_text("text") + "\n" for page in doc)

def parse_date_robust(date_str: str) -> datetime:
    ds = re.sub(r'[/_,-]', '.', date_str.strip())
    match = re.search(r'\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b', ds)
    if not match: return None
    day, month, year = match.groups()
    if not year: year = str(datetime.now().year)
    elif len(year) == 2: year = "20" + year
    try: return datetime(int(year), int(month), int(day))
    except: return None

def get_line_dates(line: str) -> list[datetime]:
    ds = re.sub(r'[/_,-]', '.', line)
    matches = re.findall(r'\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b', ds)
    res = []
    for day, month, year in matches:
        if not year: year = str(datetime.now().year)
        elif len(year) == 2: year = "20" + year
        try: res.append(datetime(int(year), int(month), int(day)))
        except: pass
    return res

async def fetch_image_from_link(url: str) -> bytes:
    u_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1"
    ]
    for attempt in range(8):
        headers = {
            "User-Agent": u_agents[attempt % len(u_agents)],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Upgrade-Insecure-Requests": "1",
            "Referer": "https://www.google.com/"
        }
        
        target_url = url
        if attempt >= 3 and attempt <= 4:
            target_url = f"https://api.allorigins.win/raw?url={url}"
        elif attempt >= 5:
            target_url = f"https://corsproxy.io/?{url}"
            
        try:
            async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
                if "prnt.sc" in url:
                    resp = await client.get(target_url, headers=headers, timeout=15.0)
                    if resp.status_code in [200, 304]:
                        match = re.search(r'<img[^>]+id="screenshot-image"[^>]+src="([^"]+)"', resp.text)
                        if not match:
                            match = re.search(r'<img[^>]+src="([^"]+)"', resp.text)
                        if match:
                            img_url = match.group(1)
                            if img_url.startswith("//"):
                                img_url = "https:" + img_url
                            if "st.prntscr.com" not in img_url:
                                img_resp = await client.get(img_url, headers=headers, timeout=15.0)
                                if img_resp.status_code == 200:
                                    return img_resp.content
                                    
                elif "imgur.com" in url:
                    if "i.imgur.com" not in url:
                        img_id = url.rstrip("/").split("/")[-1]
                        fetch_url = f"https://i.imgur.com/{img_id}.png"
                    else:
                        fetch_url = url
                        
                    if attempt >= 3:
                        fetch_url = f"https://api.allorigins.win/raw?url={fetch_url}"
                        
                    resp = await client.get(fetch_url, headers=headers, timeout=15.0)
                    if resp.status_code == 200:
                        return resp.content
        except Exception:
            pass
            
        await asyncio.sleep(1.0 + (attempt * 0.5))
    return None

def parse_turkish_amount(amt_str: str) -> float:
    amt_str = re.sub(r'[^\d.,]', '', amt_str)
    if not amt_str: return 0.0
    if '.' in amt_str and ',' in amt_str:
        if amt_str.rfind('.') > amt_str.rfind(','):
            amt_str = amt_str.replace(',', '')
        else:
            amt_str = amt_str.replace('.', '').replace(',', '.')
    elif ',' in amt_str:
        parts = amt_str.split(',')
        if len(parts[-1]) <= 2:
            amt_str = amt_str.replace(',', '.')
        else:
            amt_str = amt_str.replace(',', '')
    else:
        parts = amt_str.split('.')
        if len(parts) > 1 and len(parts[-1]) <= 2:
            pass
        else:
            amt_str = amt_str.replace('.', '')
    try:
        return float(amt_str)
    except:
        return 0.0

def extract_all_amounts(line: str) -> list[float]:
    words = line.split()
    amts = []
    for w in words:
        if re.search(r'\d', w):
            amts.append(parse_turkish_amount(w))
    return amts

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
        "panel_msg_id": None,
        "last_upload_time": 0,
        "debounce_task": None,
        "last_user_msg_id": 0
    }

def get_pbar(current: int, total: int, length: int = 12) -> str:
    if total == 0:
        return "░" * length
    filled = int((current / total) * length)
    return "█" * filled + "░" * (length - filled)

def get_menu_text(st: Dict[str, Any]) -> str:
    state = st["state"]
    iban = st.get("iban", "Bilinmiyor")
    lines = st.get("lines", [])
    unmatched = st.get("unmatched_lines", {})
    queued = len(st.get("queued_dekonts", []))
    
    text = "💠 <b>DİJİTAL DEKONT ANALİZ MERKEZİ</b> 💠\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"🏦 <b>Ekstre:</b> {'✅ Yüklendi' if iban != 'Bilinmiyor' else '❌ Yüklü Değil'}\n"
    if iban != "Bilinmiyor":
        text += f"💳 <b>IBAN:</b> <code>{iban}</code>\n"
        text += f"📋 <b>Sıradaki İşlem:</b> <code>{len(unmatched)} / {len(lines)} Bekliyor</code>\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"📦 <b>Kuyruktaki Dekont:</b> <code>{queued} Adet</code>\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    if state == "WAIT_STATEMENT":
        text += "📂 <i>Lütfen PDF formatındaki hesap ekstresini gruba gönderin...</i>"
    elif state == "WAIT_DEKONT":
        text += "🧾 <i>Lütfen dekontları (Fotoğraf/PDF) gruba gönderin...\nSiz yükledikçe sistem arka planda alacaktır.</i>"
    elif state == "WAIT_LINK":
        text += "🔗 <i>Lütfen prnt.sc veya imgur linklerini gönderin...\nTek mesajda toplu link gönderebilirsiniz.</i>"
    elif state == "ANALYZING":
        total = queued + st["processed_count"]
        pct = int((st['processed_count'] / total) * 100) if total > 0 else 0
        bar = get_pbar(st['processed_count'], total)
        text += f"⚡ <b>Yapay Zeka Analizi Sürüyor...</b>\n\n"
        text += f"📊 <b>İlerleme:</b> {bar} <b>%{pct}</b>\n"
        text += f"🔍 <b>Taranan:</b> {st['processed_count']} / {total}\n"
        text += f"✅ <b>Eşleşen:</b> {st['matched_count']}   |   ❌ <b>Hatalı:</b> {st['failed_count']}"
    else:
        text += "⚙️ <i>Sistem Hazır. Lütfen aşağıdaki menüden işlem seçiniz.</i>"
        
    return text

def get_menu_keyboard(state: str) -> InlineKeyboardMarkup:
    if state in ["WAIT_STATEMENT", "WAIT_DEKONT", "WAIT_LINK"]:
        kb = [
            [InlineKeyboardButton("▶️ Analize Başla", callback_data="cmd_analyze")],
            [InlineKeyboardButton("🔙 İptal", callback_data="cmd_cancel")]
        ]
    elif state == "ANALYZING":
        kb = [[InlineKeyboardButton("🛑 Analizi Durdur", callback_data="cmd_stop")]]
    else:
        kb = [
            [InlineKeyboardButton("📂 Ekstre Yükle", callback_data="cmd_upload_stmt"),
             InlineKeyboardButton("🔗 Link Yükle", callback_data="cmd_upload_link")],
            [InlineKeyboardButton("🧾 Foto/PDF Dekont Yükle", callback_data="cmd_upload_dekont")],
            [InlineKeyboardButton("▶️ Analize Başla", callback_data="cmd_analyze")],
            [InlineKeyboardButton("🛑 Sıfırlayıp Temizle", callback_data="cmd_reset")],
            [InlineKeyboardButton("❌ Paneli Kapat", callback_data="cmd_close")]
        ]
    return InlineKeyboardMarkup(kb)

async def smart_update_panel(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = chat_statements.get(chat_id)
    if not st: return
    if st.get("panel_msg_id") and st["panel_msg_id"] > st.get("last_user_msg_id", 0):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=st["panel_msg_id"],
                text=get_menu_text(st),
                parse_mode="HTML",
                reply_markup=get_menu_keyboard(st["state"])
            )
            return
        except Exception:
            pass
            
    if st.get("panel_msg_id"):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=st["panel_msg_id"])
        except Exception:
            pass
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=get_menu_text(st),
            parse_mode="HTML",
            reply_markup=get_menu_keyboard(st["state"])
        )
        st["panel_msg_id"] = msg.message_id
    except Exception:
        pass

async def smart_update_panel_debounced(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = chat_statements.get(chat_id)
    if not st: return
    while time.time() - st.get("last_upload_time", 0) < 1.0:
        await asyncio.sleep(0.5)
    await smart_update_panel(chat_id, context)

async def cmd_analiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update):
        return
    chat_id = update.effective_chat.id
    if chat_id not in chat_statements:
        chat_statements[chat_id] = parse_statement_pdf("")
    st = chat_statements[chat_id]
    st["state"] = "IDLE"
    st["last_user_msg_id"] = update.message.message_id
    msg = await update.message.reply_text(get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
    st["panel_msg_id"] = msg.message_id

async def get_available_api(attempted_keys: set) -> dict:
    while True:
        now = time.time()
        available_nodes = [n for n in API_POOL if n["key"] not in attempted_keys]
        if not available_nodes:
            return None
            
        for node in available_nodes:
            node["usage"] = [t for t in node["usage"] if now - t < 60]
            if len(node["usage"]) < node["rpm_limit"]:
                node["usage"].append(now)
                return node
        await asyncio.sleep(0.5)

async def process_dekont_with_ai(image_bytes: bytes, chat_id: int) -> Tuple[bool, str]:
    st = chat_statements[chat_id]
    if not st.get("unmatched_lines"):
        return False, "❌ Ekstrede eşleşecek işlem kalmadı"
        
    prompt = f"""Görev: Ekteki dekont görüntüsünü analiz et ve bilgileri çıkar.
Ekstre Sahibinin IBAN'ı: {st.get('iban', 'Bilinmiyor')}

Kurallar:
1. Görsel bir para transferi dekontu değilse (taslak, talep, hata sayfası vs.) is_dekont = false dön.
2. Alıcı IBAN Kontrolü: Dekonttaki alıcı IBAN (veya maskeli hali, örn: TR12****34) ile üstteki Ekstre Sahibinin IBAN'ı açıkça uyuşmuyorsa is_iban_matched = false dön. Dekontta IBAN yoksa, gizliyse ve çelişmiyorsa veya sadece kolay adres varsa true dön.
3. Tutar Kontrolü: Dekontta "İşlem Tutarı" ve "Masraf/Ücret" ayrı ayrı belirtilmişse (veya "Toplam Tutar" masrafla birlikte daha yüksekse), KESİNLİKLE sadece net transfer edilen "İşlem Tutarı"nı (Hesaba geçen/gönderilen saf parayı) baz al. Masraf eklenmiş "Toplam Tutar"ı çıkarma!

Sadece JSON dön:
{{
  "sender_name": "Gönderen Adı Soyadı",
  "amount": 1500.50,
  "date": "15.08.2023",
  "is_iban_matched": true,
  "is_dekont": true
}}"""
    schema = {
        "type": "OBJECT",
        "properties": {
            "sender_name": {"type": "STRING"},
            "amount": {"type": "NUMBER"},
            "date": {"type": "STRING"},
            "is_iban_matched": {"type": "BOOLEAN"},
            "is_dekont": {"type": "BOOLEAN"}
        },
        "required": ["sender_name", "amount", "date", "is_iban_matched", "is_dekont"]
    }
    
    dekont_info = {}
    if not API_POOL:
        return False, "API Bulunamadı"
        
    last_error = "Bilinmeyen Hata"
    attempted_keys = set()
    global_retry_count = 0
    max_global_retries = 4
    
    while True:
        api_node = await get_available_api(attempted_keys)
        if not api_node:
            if global_retry_count < max_global_retries:
                global_retry_count += 1
                attempted_keys.clear()
                await asyncio.sleep(5.0)
                continue
            break
            
        try:
            if api_node["type"] == "gemini":
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        api_node["client"].models.generate_content,
                        model='gemini-3.7-flash',
                        contents=[
                            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                            prompt
                        ],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=schema,
                            temperature=0.0
                        )
                    ),
                    timeout=30.0
                )
                try:
                    dekont_info = json.loads(response.text)
                except Exception:
                    raise Exception("Gemini JSON Çözümleme Hatası")
                break
            elif api_node["type"] == "groq":
                import base64
                b64_img = base64.b64encode(image_bytes).decode("utf-8")
                headers = {
                    "Authorization": f"Bearer {api_node['key']}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": "llama-4-scout-17bx16moe",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                            ]
                        }
                    ],
                    "temperature": 0.0
                }
                async with httpx.AsyncClient(verify=False) as client:
                    resp = await client.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=30.0)
                if resp.status_code == 200:
                    resp_json = resp.json()
                    content = resp_json["choices"][0]["message"]["content"]
                    match = re.search(r'\{.*\}', content, re.DOTALL)
                    if match:
                        dekont_info = json.loads(match.group(0))
                    else:
                        raise Exception("Groq JSON Formatı Bulunamadı")
                elif resp.status_code == 429:
                    raise Exception("Groq Rate Limit")
                else:
                    raise Exception(f"Groq Hatası: {resp.status_code}")
                break
        except Exception as e:
            last_error = str(e)
            attempted_keys.add(api_node["key"])
            continue
            
    if not dekont_info:
        return False, f"❌ Okunamadı (API Hatası: {last_error})"
        
    if not dekont_info.get("is_dekont"):
        return False, "❌ 👤 ? | 💰 0 TL\n⚠️ <i>Bu görsel geçerli bir dekont değil.</i>"

    try:
        ai_amt = float(dekont_info.get("amount", 0))
    except:
        ai_amt = 0.0
        
    ai_name = str(dekont_info.get("sender_name", "?")).upper()
    ai_name_clean = ai_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    summary_line = f"👤 {ai_name_clean} | 💰 {ai_amt} TL"
    
    if ai_amt <= 0:
        return False, f"❌ {summary_line}\n⚠️ <i>Tutar net okunamadı.</i>"

    if not dekont_info.get("is_iban_matched", True):
        return False, f"❌ {summary_line}\n⚠️ <i>Alıcı IBAN ekstre sahibi ile eşleşmedi! (Farklı hesaba gönderilmiş)</i>"

    ai_date_obj = parse_date_robust(dekont_info.get("date", ""))

    async with st["lock"]:
        candidates = []
        for line_id, line_text in st["unmatched_lines"].items():
            line_amts = extract_all_amounts(line_text)
            if any(abs(la - ai_amt) < 0.01 for la in line_amts):
                candidates.append((line_id, line_text))
        
        if not candidates:
            return False, f"❌ {summary_line}\n⚠️ <i>Ekstrede bu tutarda ({ai_amt} TL) işlem bulunamadı.</i>"
            
        best_score = -999.0
        best_id = candidates[0][0]
        
        for cid, ctext in candidates:
            name_score = difflib.SequenceMatcher(None, ai_name, ctext.upper()).ratio()
            date_score = 0.0
            if ai_date_obj:
                c_dates = get_line_dates(ctext)
                if c_dates:
                    min_diff = 9999
                    for cd in c_dates:
                        diff = abs((cd - ai_date_obj).days)
                        if cd.day == ai_date_obj.day and cd.month == ai_date_obj.month:
                            diff = min(diff, 0)
                        min_diff = min(min_diff, diff)
                        
                    if min_diff <= 2:
                        date_score = 1.0 - (min_diff * 0.2)
                    else:
                        date_score = -2.0
            
            total_score = name_score + date_score
            if total_score > best_score:
                best_score = total_score
                best_id = cid
                
        if best_score < -0.5:
            return False, f"❌ {summary_line}\n⚠️ <i>Tutar uyuyor ancak TARIH veya ALICI İSMİ tamamen hatalı! (Çakışma Önlendi)</i>"
                
        st["unmatched_lines"].pop(best_id)
        return True, summary_line

async def analyze_worker(item: dict, chat_id: int, sem: asyncio.Semaphore) -> Tuple[bool, str, dict]:
    async with sem:
        try:
            is_matched, res_text = await process_dekont_with_ai(item["bytes"], chat_id)
            return is_matched, res_text, item
        except Exception as e:
            return False, f"❌ Kritik Sistem Hatası: {str(e)}", item

async def run_analysis(chat_id: int, context: ContextTypes.DEFAULT_TYPE, msg_id: int):
    st = chat_statements.get(chat_id)
    if not st or st["state"] != "ANALYZING":
        return
        
    dekonts_to_process = list(st["queued_dekonts"])
    total = len(dekonts_to_process)
    if total == 0:
        return
        
    sem = asyncio.Semaphore(len(API_POOL) if API_POOL else 1)
    tasks = [asyncio.create_task(analyze_worker(f, chat_id, sem)) for f in dekonts_to_process]
    
    last_update = time.time()
    for coro in asyncio.as_completed(tasks):
        if st["state"] != "ANALYZING":
            break
        try:
            is_matched, res_text, item = await coro
        except Exception as e:
            is_matched = False
            res_text = f"❌ Arka Plan Görev Hatası: {str(e)}"
            item = None
            
        st["processed_count"] += 1
        if is_matched:
            st["matched_count"] += 1
        else:
            st["failed_count"] += 1
            st["failed_list"].append(res_text)
            if item and item.get("msg_id"):
                reply_txt = f"🔴 <b>İŞLEM BAŞARISIZ!</b>\n\n{res_text}"
                if item.get("url"):
                    reply_txt += f"\n\n🔗 <b>İlgili Link:</b> {item['url']}"
                try:
                    sent_fail = await context.bot.send_message(chat_id=chat_id, text=reply_txt, reply_to_message_id=item["msg_id"], parse_mode="HTML")
                    st["last_user_msg_id"] = max(st.get("last_user_msg_id", 0), sent_fail.message_id)
                except Exception:
                    pass
            
        if item:
            try:
                st["queued_dekonts"].remove(item)
            except ValueError:
                pass
            
        if time.time() - last_update > 2 or st["processed_count"] == total:
            await smart_update_panel(chat_id, context)
            last_update = time.time()
            
    if st["state"] == "ANALYZING":
        st["state"] = "IDLE"
        st["queued_dekonts"].clear()
        
        header_text = get_menu_text(st) + "\n\n🎯 <b>Analiz Tamamlandı!</b>\n\n"
        messages = []
        
        if not st["failed_list"]:
            messages.append(header_text)
        else:
            current_msg = header_text + "<b>⚠️ Başarısız İşlem Detayları:</b>"
            for fail_msg in st["failed_list"]:
                if len(current_msg) + len(fail_msg) > 3900:
                    messages.append(current_msg)
                    current_msg = "<b>⚠️ (Devamı) Başarısız İşlem Detayları:</b>"
                current_msg += "\n\n" + fail_msg
            messages.append(current_msg)
            
        if st.get("panel_msg_id"):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=st["panel_msg_id"])
            except Exception:
                pass
                
        for i, msg in enumerate(messages):
            kb = get_menu_keyboard("IDLE") if i == len(messages) - 1 else None
            try:
                sent_msg = await context.bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=kb)
                if i == len(messages) - 1:
                    st["panel_msg_id"] = sent_msg.message_id
            except Exception:
                pass

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
        await smart_update_panel(chat_id, context)
    elif d == "cmd_upload_dekont":
        if not st.get("iban") or st["iban"] == "Bilinmiyor":
            st["state"] = "IDLE"
            try:
                await query.edit_message_text("⚠️ <b>Önce Hesap Hareketleri Yüklemelisiniz!</b>\n\n" + get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
            except Exception:
                pass
            return
        st["state"] = "WAIT_DEKONT"
        await smart_update_panel(chat_id, context)
    elif d == "cmd_upload_link":
        if not st.get("iban") or st["iban"] == "Bilinmiyor":
            st["state"] = "IDLE"
            try:
                await query.edit_message_text("⚠️ <b>Önce Hesap Hareketleri Yüklemelisiniz!</b>\n\n" + get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
            except Exception:
                pass
            return
        st["state"] = "WAIT_LINK"
        await smart_update_panel(chat_id, context)
    elif d == "cmd_cancel":
        st["state"] = "IDLE"
        await smart_update_panel(chat_id, context)
    elif d == "cmd_reset":
        st["unmatched_lines"] = {i: l for i, l in enumerate(st.get("lines", []))}
        st["queued_dekonts"] = []
        st["processed_count"] = 0
        st["matched_count"] = 0
        st["failed_count"] = 0
        st["failed_list"] = []
        st["state"] = "IDLE"
        await smart_update_panel(chat_id, context)
    elif d == "cmd_stop":
        st["state"] = "IDLE"
        await smart_update_panel(chat_id, context)
    elif d == "cmd_close":
        if st.get("panel_msg_id"):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=st["panel_msg_id"])
            except Exception:
                pass
        st["state"] = "CLOSED"
    elif d == "cmd_analyze":
        if not st["queued_dekonts"]:
            st["state"] = "IDLE"
            try:
                await query.edit_message_text("⚠️ <b>Kuyrukta analiz edilecek dekont yok!</b>\n\n" + get_menu_text(st), parse_mode="HTML", reply_markup=get_menu_keyboard("IDLE"))
            except Exception:
                pass
            return
        st["state"] = "ANALYZING"
        await smart_update_panel(chat_id, context)
        asyncio.create_task(run_analysis(chat_id, context, query.message.message_id))

async def handle_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update):
        return
    chat_id = update.effective_chat.id
    if chat_id not in chat_statements:
        return
    st = chat_statements[chat_id]
    state = st.get("state", "IDLE")
    msg_id = update.message.message_id
    st["last_user_msg_id"] = max(st.get("last_user_msg_id", 0), msg_id)

    if state == "WAIT_STATEMENT" and update.message.document:
        doc = update.message.document
        if not doc.file_name.lower().endswith(".pdf"):
            return
        file = await doc.get_file()
        file_bytes = bytes(await file.download_as_bytearray())
        text = extract_text_from_pdf(file_bytes)
        if any(k in text.upper() for k in ["HESAP HAREKET", "EKSTRE", "HESAP OZETI", "HESAP ÖZETİ", "GUNCEL BAKIYE", "GÜNCEL BAKİYE"]):
            n_st = parse_statement_pdf(text)
            n_st["state"] = "IDLE"
            n_st["panel_msg_id"] = st.get("panel_msg_id")
            n_st["last_user_msg_id"] = st.get("last_user_msg_id", 0)
            chat_statements[chat_id] = n_st
            n_st["last_upload_time"] = time.time()
            if not n_st.get("debounce_task") or n_st["debounce_task"].done():
                n_st["debounce_task"] = asyncio.create_task(smart_update_panel_debounced(chat_id, context))
    
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
            st["queued_dekonts"].append({"bytes": file_bytes, "msg_id": msg_id, "url": None})
            st["last_upload_time"] = time.time()
            if not st.get("debounce_task") or st["debounce_task"].done():
                st["debounce_task"] = asyncio.create_task(smart_update_panel_debounced(chat_id, context))
                
    elif state == "WAIT_LINK" and update.message.text:
        text = update.message.text
        links = re.findall(r'(https?://(?:prnt\.sc|imgur\.com|\S*imgur\S*)[^\s]+)', text)
        if links:
            total = len(links)
            tmp_msg = await update.message.reply_text(f"⏳ 0 / {total} link indiriliyor...")
            st["last_user_msg_id"] = max(st.get("last_user_msg_id", 0), tmp_msg.message_id)
            
            sem_dl = asyncio.Semaphore(8)
            async def download_worker(l_url):
                async with sem_dl:
                    return l_url, await fetch_image_from_link(l_url)
                    
            dl_tasks = [asyncio.create_task(download_worker(link)) for link in links]
            success_count = 0
            last_edit_time = time.time()
            
            for i, coro in enumerate(asyncio.as_completed(dl_tasks)):
                l_url, img_bytes = await coro
                if img_bytes:
                    st["queued_dekonts"].append({"bytes": img_bytes, "msg_id": msg_id, "url": l_url})
                    success_count += 1
                
                if time.time() - last_edit_time > 2.0 or i == total - 1:
                    try:
                        await tmp_msg.edit_text(f"⏳ Linkler İndiriliyor...\nDurum: {i+1} / {total}\nBaşarılı: {success_count} | Başarısız: {(i+1)-success_count}")
                    except Exception:
                        pass
                    last_edit_time = time.time()
                        
            try:
                await tmp_msg.delete()
            except Exception:
                pass
            
            st["state"] = "IDLE"
            st["last_upload_time"] = time.time()
            if not st.get("debounce_task") or st["debounce_task"].done():
                st["debounce_task"] = asyncio.create_task(smart_update_panel_debounced(chat_id, context))

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

if __name__ == "__main__":
    if not BOT_TOKEN:
        exit(1)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("analiz", cmd_analiz))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL | filters.TEXT, handle_files))
    app.add_handler(MessageHandler(filters.ALL, fallback))
    app.run_polling()
