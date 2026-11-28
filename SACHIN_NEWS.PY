import os
import re
import io
import json
import time
import difflib
import hashlib
import subprocess
import urllib.parse
import requests
import feedparser
import numpy as np
from pypdf import PdfReader
from datetime import datetime, timezone, timedelta
from dateutil import parser as date_parser
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ================= CONFIGURATION =================
IST_TZ = timezone(timedelta(hours=5, minutes=30))

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
FALLBACK_MODELS = list(dict.fromkeys([MODEL_NAME, "gemini-3.5-flash-lite", "gemini-3.0-flash"]))

MIN_QUALITY_SCORE = 95
MAX_AUDIT_ROUNDS = 3
MIN_MARKET_CAP_CR = 500.0  # ₹500 Cr થી નીચેના Micro-Cap / SME સ્ટોક્સ સ્કિપ થશે
MIN_ENGLISH_WORDS = 480    # AdSense & Google News માટે થિન કન્ટેન્ટ બ્લોક
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "12"))

BLOG_BASE_URL = "https://marketmunafa.blogspot.com/"
SITE_LOGO_URL = "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgXG-GojgyTIZWy4ftRRsgpLsDMxrAEKCRb_BnZsfVTKT0_T9IELdKQYKgACOQkhus69xfx_xg-iVyLvP2-RAki8voVBwja88Dfsg_tQRQR7JvgegMGFfEQfm73oO00WeTo1vPUHghTXmXd_by6k0C-m3Y_5JhoYEjUF8Y8V3KKTbGnUn53dUY-6AN32_A/s1254/1001173067.png"

FEEDS = [
    {"name": "NSE Announcement", "url": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"},
    {"name": "BSE Announcement", "url": "https://www.bseindia.com/data/xml/announcements.aspx"},
    {"name": "NSE Corporate Action", "url": "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"},
    {"name": "BSE Notice", "url": "https://www.bseindia.com/data/xml/notices.xml"},
    {"name": "SEBI Update", "url": "https://www.sebi.gov.in/sebirss.xml"},
]

# માત્ર હાઈ-ઈમ્પેક્ટ, માર્કેટ મૂવિંગ સ્ટોક ન્યૂઝના કીવર્ડ્સ
IMPORTANT_KEYWORDS = [
    "financial result", "quarterly result", "audited result", "unaudited result",
    "q1", "q2", "q3", "q4", "net profit", "revenue", "ebitda",
    "bagged", "order", "work order", "purchase order", "contract", "letter of award", "loa", "l1 bidder", "tender",
    "dividend", "bonus", "stock split", "sub-division", "buyback",
    "merger", "amalgamation", "acquisition", "acquire", "demerger", "takeover",
    "rights issue", "qip", "fund raising", "preferential issue", "capex", "expansion", "commissioning",
    "joint venture", "strategic partnership", "usfda", "fda approval", "patent", "launch",
    "delisting", "open offer", "sebi", "penalty", "order passed", "circular", "credit rating upgrade", "downgrade"
]

# પરચુરણ (રૂટિન/બિનજરૂરી) ન્યૂઝ સખત રીતે બ્લોક કરવા માટે
IGNORE_KEYWORDS = [
    "loss of share", "duplicate share", "trading window", "newspaper publication",
    "certificate under regulation 74", "regulation 74(5)", "regulation 74 (5)",
    "reconciliation of share capital", "intimation of schedule", "analyst/institutional",
    "investor meet", "investors meet", "conference call", "earnings call", "transcript",
    "audio recording", "video recording", "scrutinizer", "voting results", "postal ballot",
    "notice of agm", "notice of egm", "annual general meeting", "extraordinary general meeting",
    "annual report", "esop", "esos", "employee stock option", "rta", "registrar and transfer",
    "registered office", "clarification on price", "spurt in volume", "movement in price",
    "compliance certificate", "statement of investor complaints", "grievance redressal",
    "corporate governance report", "shareholding pattern", "book closure",
    "record date for interest", "interest payment", "redemption of ncd", "debenture",
    "non-convertible", "commercial paper", "sme", "emerge", "strike off", "closure of subsidiary",
    "resignation of company secretary", "appointment of company secretary",
    "change in directorate", "cessation of independent director", "re-appointment of independent",
    "media release - routine", "corrigendum to notice", "intimation of board meeting"
]

# ભારતીય આંકડાને ફરજિયાત અંગ્રેજી 0-9 માં ફેરવવા માટે
INDIC_DIGIT_TRANS = str.maketrans({
    chr(base + d): str(d)
    for base in [0x0966, 0x0AE6]
    for d in range(10)
})

HISTORY_FILE = "posted_history.json"
THUMBNAIL_DIR = "thumbnails"
AUTHOR_IMG_PATH = "author.png"
os.makedirs(THUMBNAIL_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/pdf,application/xml,text/xml,application/rss+xml,application/json,text/html,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

MARKET_CAP_CACHE = {}

# ================= HELPER FUNCTIONS =================
def get_ist_now():
    return datetime.now(IST_TZ)

def enforce_english_digits(obj):
    if isinstance(obj, str):
        return obj.translate(INDIC_DIGIT_TRANS)
    if isinstance(obj, list):
        return [enforce_english_digits(x) for x in obj]
    if isinstance(obj, dict):
        return {k: enforce_english_digits(v) for k, v in obj.items()}
    return obj

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {"keys": data, "titles": []}
                data.setdefault("keys", [])
                data.setdefault("titles", [])
                return data
        except Exception:
            pass
    return {"keys": [], "titles": []}

def save_history(history_dict):
    history_dict["keys"] = history_dict["keys"][-1500:]
    history_dict["titles"] = history_dict["titles"][-500:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_dict, f, ensure_ascii=False, indent=2)

def extract_company_prefix_from_title(title):
    # NSE/BSE RSS ટાઇટલ સામાન્ય રીતે "Company Name - Subject" ફોર્મેટમાં હોય છે
    part = re.split(r"\s[-–—:|]\s", title, maxsplit=1)[0].strip()
    part = re.sub(r"\(.*?\)", "", part).strip()
    return part[:60]

def normalize_company_day_key(company_text, event_type="GEN"):
    clean = re.sub(r"[^a-z0-9\s]", " ", company_text.lower())
    stop = {"limited", "ltd", "india", "ind", "private", "pvt", "the", "of", "and", "co", "corp", "industries", "enterprises"}
    words = [w for w in clean.split() if w not in stop and len(w) > 1]
    today_str = get_ist_now().strftime("%Y-%m-%d")
    return f"{today_str}|{'_'.join(words[:3])}|{event_type}"

def is_today_ist(entry):
    """જે તે દિવસના (આજની IST તારીખના) બધા સ્ટોક ન્યૂઝ પસંદ કરે છે."""
    pub_str = entry.get("published") or entry.get("updated") or ""
    if not pub_str:
        return False
    try:
        clean_pub = re.sub(r"\bIST\b", "+0530", pub_str.strip())
        dt = date_parser.parse(clean_pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_TZ)
        dt_ist = dt.astimezone(IST_TZ)
        now_ist = get_ist_now()
        return dt_ist.date() == now_ist.date()
    except Exception:
        return False

def get_entry_dt(entry):
    pub_str = entry.get("published") or entry.get("updated") or ""
    try:
        clean_pub = re.sub(r"\bIST\b", "+0530", pub_str.strip())
        dt = date_parser.parse(clean_pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_TZ)
        return dt.astimezone(IST_TZ)
    except Exception:
        return get_ist_now()

def detect_event_type(title, summary, pdf_text=""):
    text = f"{title} {summary} {pdf_text[:1200]}".lower()
    if any(k in text for k in ["financial result", "quarterly result", "audited financial", "unaudited financial", "statement of financial results", "q1", "q2", "q3", "q4"]):
        return "RESULT"
    if any(k in text for k in ["work order", "purchase order", "letter of award", "loa", "bagged order", "received order", "contract worth", "order worth", "l1 bidder", "award of contract", "order"]):
        return "ORDER"
    if any(k in text for k in ["dividend", "bonus", "stock split", "sub-division", "buyback"]):
        return "CORPORATE_ACTION"
    if any(k in text for k in ["merger", "acquisition", "amalgamation", "demerger", "takeover", "joint venture"]):
        return "M_AND_A"
    if any(k in text for k in ["sebi", "circular", "penalty", "usfda", "fda"]):
        return "REGULATORY"
    return "IMPORTANT_UPDATE"

def is_important_news(title, summary):
    text = f"{title} {summary}".lower()
    if any(bad in text for bad in IGNORE_KEYWORDS):
        return False
    return any(good in text for good in IMPORTANT_KEYWORDS)

def is_duplicate_post(company_name, seo_title, event_type, recent_posts, history_titles):
    comp_clean = re.sub(r"[^a-z0-9\s]", " ", company_name.lower()).strip()
    stop = {"limited", "ltd", "india", "ind", "private", "pvt", "the", "of", "and"}
    comp_words = [w for w in comp_clean.split() if w not in stop and len(w) > 2][:2]
    all_titles = [p["title"] for p in recent_posts] + history_titles

    new_norm = re.sub(r"[^a-z0-9\s]", "", seo_title.lower()).strip()
    today_tag = get_ist_now().strftime("%Y-%m-%d")

    for old_t in all_titles:
        old_norm = re.sub(r"[^a-z0-9\s]", "", str(old_t).lower()).strip()
        if not old_norm:
            continue
        if difflib.SequenceMatcher(None, new_norm, old_norm).ratio() >= 0.58:
            return True

    # આજની જ પબ્લિશ થયેલી પોસ્ટમાં એ જ કંપનીની એ જ ઇવેન્ટ રિપીટ ન થાય
    for p in recent_posts:
        p_date = str(p.get("published", ""))[:10]
        p_title_low = p["title"].lower()
        if p_date == today_tag and comp_words and all(w in p_title_low for w in comp_words):
            return True
    return False

# ================= MARKET CAP FILTER (LARGE / MID / SMALL CAP ONLY) =================
def classify_market_cap(mcap_cr):
    if mcap_cr is None:
        return "UNKNOWN"
    if mcap_cr >= 20000:
        return "Large Cap"
    if mcap_cr >= 5000:
        return "Mid Cap"
    if mcap_cr >= MIN_MARKET_CAP_CR:
        return "Small Cap"
    return "Micro Cap"

def get_market_cap_info(company_query):
    """Screener.in પરથી કંપનીનું લાઈવ માર્કેટ કેપ (₹ Crore) લાવીને Large/Mid/Small Cap ઓળખે છે."""
    clean_q = re.sub(r"\b(limited|ltd|india|private|pvt)\b", "", company_query, flags=re.I)
    clean_q = " ".join(re.sub(r"[^A-Za-z0-9\s&]", " ", clean_q).split()).strip()
    if len(clean_q) < 2:
        return {"found": False, "mcap_cr": None, "category": "UNKNOWN", "screener_name": ""}

    cache_key = clean_q.lower()
    if cache_key in MARKET_CAP_CACHE:
        return MARKET_CAP_CACHE[cache_key]

    result = {"found": False, "mcap_cr": None, "category": "UNKNOWN", "screener_name": ""}
    try:
        search_queries = [clean_q]
        words = clean_q.split()
        if len(words) > 2:
            search_queries.append(" ".join(words[:2]))

        comp_item = None
        for sq in search_queries:
            s_url = f"https://www.screener.in/api/company/search/?q={urllib.parse.quote(sq)}"
            r = requests.get(s_url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                arr = r.json()
                if isinstance(arr, list) and arr:
                    # સૌથી નજીકનું મેચ પસંદ કરો
                    comp_item = arr[0]
                    break

        if comp_item and comp_item.get("url"):
            page_url = f"https://www.screener.in{comp_item['url']}"
            pr = requests.get(page_url, headers=HEADERS, timeout=12)
            if pr.status_code == 200:
                html_txt = pr.text
                m = re.search(
                    r"Market\s*Cap.*?<span\s+class=[\"']number[\"'][^>]*>\s*([0-9,\.]+)\s*</span>",
                    html_txt,
                    flags=re.I | re.S,
                )
                if m:
                    mcap_val = float(m.group(1).replace(",", ""))
                    result = {
                        "found": True,
                        "mcap_cr": mcap_val,
                        "category": classify_market_cap(mcap_val),
                        "screener_name": comp_item.get("name", clean_q),
                    }
    except Exception as e:
        print(f"⚠️ Market cap lookup skipped for '{clean_q}': {e}")

    MARKET_CAP_CACHE[cache_key] = result
    return result

# ================= BLOGGER & PDF HELPERS =================
def get_blogger_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["CLIENT_ID"],
        client_secret=os.environ["CLIENT_SECRET"],
    )
    return build("blogger", "v3", credentials=creds)

def get_recent_blogger_posts(service, limit=50):
    try:
        res = service.posts().list(blogId=os.environ["BLOG_ID"], maxResults=limit, fetchBodies=False).execute()
        return [
            {
                "title": p.get("title", ""),
                "url": p.get("url", ""),
                "labels": p.get("labels", []),
                "published": p.get("published", ""),
            }
            for p in res.get("items", [])
            if p.get("title") and p.get("url")
        ]
    except Exception as e:
        print(f"⚠️ Could not fetch recent Blogger posts: {e}")
        return []

def pick_related_internal_links(recent_posts, current_tags, max_links=3):
    if not recent_posts:
        return []
    tag_set = {str(t).lower() for t in current_tags}
    scored = []
    for p in recent_posts[:30]:
        p_labels = {str(l).lower() for l in p.get("labels", [])}
        score = len(tag_set & p_labels)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:max_links]]

def extract_pdf_text(link):
    if not link:
        return ""
    try:
        req_headers = dict(HEADERS)
        if "nseindia.com" in link.lower():
            req_headers["Referer"] = "https://www.nseindia.com/"
        elif "bseindia.com" in link.lower():
            req_headers["Referer"] = "https://www.bseindia.com/"

        resp = requests.get(link, headers=req_headers, timeout=22)
        content_type = resp.headers.get("Content-Type", "").lower()
        if "pdf" in content_type or link.lower().endswith(".pdf") or resp.content[:4] == b"%PDF":
            reader = PdfReader(io.BytesIO(resp.content))
            extracted = []
            for page in reader.pages[:8]:
                txt = page.extract_text()
                if txt:
                    extracted.append(txt.strip())
            full_text = "\n".join(extracted)
            return full_text[:14000]
    except Exception as e:
        print(f"⚠️ PDF extraction skipped/failed for {link}: {e}")
    return ""

# ================= GEMINI & COPYRIGHT CHECK =================
def clean_json_response(raw_text):
    txt = (raw_text or "").strip()
    txt = re.sub(r"^```json\s*|\s*```$", "", txt, flags=re.I).strip()
    return txt

def call_gemini_json(prompt, temperature=0.2):
    raw_keys = [
        os.environ.get("GEMINI_API_KEY_1", ""),
        os.environ.get("GEMINI_API_KEY_2", ""),
        os.environ.get("GEMINI_API_KEY", ""),
        os.environ.get("GEMINI_API_KEYS", ""),
    ]
    keys = []
    for rk in raw_keys:
        for k in re.split(r"[\s,;]+", rk):
            if len(k.strip()) > 10 and k.strip() not in keys:
                keys.append(k.strip())
    if not keys:
        raise ValueError("No GEMINI_API_KEY found in environment secrets.")

    last_err = None
    for model_name in FALLBACK_MODELS:
        for key in keys:
            try:
                client = genai.Client(api_key=key)
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=temperature,
                    ),
                )
                return clean_json_response(response.text)
            except Exception as e:
                print(f"⚠️ Gemini error ({model_name}), trying next: {e}")
                last_err = e
                time.sleep(1.5)
    raise RuntimeError(f"All Gemini keys/models failed: {last_err}")

def strip_html(html_str):
    txt = re.sub(r"<[^>]+>", " ", str(html_str or ""))
    return " ".join(txt.split())

def count_words(html_str):
    return len(re.findall(r"[A-Za-z0-9]+", strip_html(html_str)))

def find_copyright_overlaps(source_text, article_html, ngram_size=8):
    """મૂળ PDF/ફાઈલિંગમાંથી 8+ શબ્દોની બેઠી લાઈન કોપી થઈ હોય તો પકડે છે."""
    if not source_text or len(source_text) < 200:
        return []
    src_words = re.findall(r"[a-z0-9]+", source_text.lower())
    art_words = re.findall(r"[a-z0-9]+", strip_html(article_html).lower())
    if len(src_words) < ngram_size or len(art_words) < ngram_size:
        return []
    src_grams = {" ".join(src_words[i:i + ngram_size]) for i in range(len(src_words) - ngram_size + 1)}
    matches = []
    for i in range(len(art_words) - ngram_size + 1):
        tokens = art_words[i:i + ngram_size]
        gram = " ".join(tokens)
        if gram in src_grams and gram not in matches:
            # સત્તાવાર સેબી/રેગ્યુલેશન શબ્દો કે શુદ્ધ આંકડાઓને અવગણો
            if any(legal in gram for legal in ["regulation 30", "sebi listing obligations", "disclosure requirements"]):
                continue
            matches.append(gram)
            if len(matches) >= 8:
                break
    return matches

# ================= PRE-SCREENING GATE (MATERIALITY + CAP VERIFICATION) =================
def pre_screen_filing(source, title, summary, pdf_text, screener_info):
    """પરચુરણ ન્યૂઝ અને Micro-cap/SME સ્ટોક્સને શરૂઆતમાં જ ફિલ્ટર કરી દે છે."""
    prompt = f"""
You are the Senior Stock Market Desk Editor for an Indian financial portal.
Evaluate the exchange filing below and decide STRICTLY whether it deserves a full news article.

SOURCE: {source}
TITLE: {title}
SUMMARY: {summary}
SCREENER MARKET CAP DATA: {json.dumps(screener_info)}
PDF EXCERPT:
{pdf_text[:2800] if pdf_text else "No PDF extracted"}

STRICT ELIGIBILITY RULES:
1. HIGH-IMPACT ONLY (NO TRIVIAL / PERCHURAN NEWS):
   - APPROVE ONLY truly market-moving news: Quarterly/Annual Financial Results (Q1/Q2/Q3/Q4), Significant Order Wins/Contracts/LOA, Dividend, Bonus, Stock Split, Buyback, Merger/Acquisition/Demerger, Major Capex/Expansion, USFDA/Major Approval, QIP/Rights/Major Fund Raise, Major SEBI Action/Circular affecting stock market investors.
   - REJECT routine/trivial filings: minor/immaterial orders, routine compliance, investor meet intimations, board meeting date intimations (without actual outcome), credit rating reaffirmation without change, routine subsidiary updates, or filings lacking any concrete financial details.
2. LARGE CAP, MID CAP, OR SMALL CAP ONLY:
   - If Screener Market Cap is provided and < ₹500 Crore -> REJECT (Micro Cap / SME).
   - If Screener Market Cap is not found: for stock filings, ONLY approve if it is a clearly recognized NSE/BSE Large Cap (>=₹20,000 Cr), Mid Cap (₹5,000-₹20,000 Cr), or Small Cap (₹500-₹5,000 Cr) company. Strictly REJECT micro-cap, penny, SME/Emerge, or obscure companies.
   - For SEBI Update circulars affecting the broader Indian stock market, set cap_category = "Market Wide" and is_eligible = true if major.

Return ONLY valid JSON:
{{
  "is_eligible": true or false,
  "reject_reason": "Short reason if false, else empty string",
  "company_name": "Clean Official English Company Name (e.g., Larsen & Toubro, Tata Power, or SEBI)",
  "cap_category": "Large Cap or Mid Cap or Small Cap or Market Wide or Micro Cap",
  "event_type": "RESULT or ORDER or CORPORATE_ACTION or M_AND_A or REGULATORY or IMPORTANT_UPDATE"
}}
"""
    raw = call_gemini_json(prompt, temperature=0.1)
    return json.loads(raw)

# ================= THUMBNAIL GENERATOR + INSTANT GIT PUSH =================
def remove_white_bg(img):
    img = img.convert("RGBA")
    data = np.array(img)
    r, g, b = data[:, :, 0], data[:, :, 1], data[:, :, 2]
    data[(r > 235) & (g > 235) & (b > 235), 3] = 0
    return Image.fromarray(data)

def add_sticker_outline(img, border_size=7, border_color=(255, 255, 255, 255)):
    alpha = img.split()[3]
    dilated = alpha.filter(ImageFilter.MaxFilter(border_size * 2 + 1))
    outline = Image.new("RGBA", img.size, border_color)
    outline.putalpha(dilated)
    return Image.alpha_composite(outline, img)

def create_and_push_thumbnail(company_name, headline_short, sentiment, cap_category, source_name, file_hash):
    W, H = 1280, 720
    base = Image.new("RGBA", (W, H), (244, 240, 234, 255))
    draw = ImageDraw.Draw(base)

    if sentiment == "POSITIVE":
        accent, glow, badge_text = (16, 150, 72, 255), (34, 197, 94, 70), "POSITIVE NEWS"
    elif sentiment == "NEGATIVE":
        accent, glow, badge_text = (210, 35, 42, 255), (239, 68, 68, 70), "NEGATIVE NEWS"
    else:
        accent, glow, badge_text = (29, 78, 216, 255), (59, 130, 246, 70), "IMPORTANT UPDATE"

    draw.ellipse([680, 70, 1230, 620], fill=glow)
    draw.rounded_rectangle([40, 40, 680, 680], radius=32, fill=(255, 255, 255, 244), outline=accent, width=5)
    draw.rounded_rectangle([710, 610, 1240, 680], radius=24, fill=(15, 23, 42, 255))

    try:
        font_badge = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
        font_comp = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
        font_head = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        font_brand = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
    except Exception:
        font_badge = font_comp = font_head = font_brand = ImageFont.load_default()

    top_tag = f"{cap_category.upper()} | {source_name.split()[0].upper()}"[:26]
    draw.rounded_rectangle([80, 78, 620, 134], radius=14, fill=(15, 23, 42, 255))
    draw.text((102, 91), top_tag, fill=(255, 255, 255, 255), font=font_brand)

    draw.rounded_rectangle([80, 158, 490, 224], radius=16, fill=accent)
    draw.text((108, 174), badge_text, fill=(255, 255, 255, 255), font=font_badge)

    draw.text((80, 255), company_name[:20].upper(), fill=(15, 23, 42, 255), font=font_comp)

    clean_hl = " ".join(re.sub(r"[^\x20-\x7E₹]", "", headline_short).split())
    words, lines, cur = clean_hl.split(), [], ""
    for w in words:
        if len(cur + " " + w) <= 24:
            cur = (cur + " " + w).strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    y_text = 345
    for line in lines[:4]:
        draw.text((80, y_text), line, fill=(51, 65, 85, 255), font=font_head)
        y_text += 54

    draw.text((820, 628), "MARKET MUNAFA", fill=(255, 255, 255, 255), font=font_brand)

    if os.path.exists(AUTHOR_IMG_PATH):
        author = Image.open(AUTHOR_IMG_PATH)
        author = remove_white_bg(author)
        author.thumbnail((520, 520), Image.Resampling.LANCZOS)
        author = add_sticker_outline(author, border_size=7)
        shadow = Image.new("RGBA", author.size, (0, 0, 0, 95))
        shadow.putalpha(author.split()[3])
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        pos_x, pos_y = 730, 615 - author.height
        base.paste(shadow, (pos_x + 10, pos_y + 10), shadow)
        base.paste(author, (pos_x, pos_y), author)

    out_path = os.path.join(THUMBNAIL_DIR, f"thumb_{file_hash}.jpg")
    base.convert("RGB").save(out_path, "JPEG", quality=92, optimize=True)

    try:
        subprocess.run(["git", "config", "--local", "user.email", "action@github.com"], check=False)
        subprocess.run(["git", "config", "--local", "user.name", "MarketMunafa Bot"], check=False)
        subprocess.run(["git", "add", out_path], check=True)
        subprocess.run(["git", "commit", "-m", f"Add thumbnail {file_hash}"], check=False)
        subprocess.run(["git", "push"], check=True)
        time.sleep(2)
    except Exception as e:
        print(f"⚠️ Git push warning for thumbnail: {e}")

    repo = (os.environ.get("GITHUB_REPO") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    branch = os.environ.get("GITHUB_BRANCH", "main").strip()
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{out_path}"

# ================= STRICT ITERATIVE QUALITY & SEO GATE =================
def sanitize_non_default_headings(html_str):
    return re.sub(
        r"<h2[^>]*>(.*?)</h2>",
        r'<div style="font-size:21px; font-weight:800; color:#0f172a; margin:20px 0 10px; border-left:4px solid #1d4ed8; padding-left:10px;">\1</div>',
        html_str,
        flags=re.IGNORECASE | re.DOTALL,
    )

def get_event_specific_instructions(event_type):
    if event_type == "RESULT":
        return """
MANDATORY FINANCIAL RESULTS FORMAT:
- You MUST include a clean HTML <table> (border-collapse:collapse; width:100%) titled Financial Performance Breakdown showing EVERY available metric from the PDF:
  1. Revenue from Operations (₹ Crore/Lakh) + YoY % & QoQ % Change
  2. Net Profit / PAT (₹ Crore/Lakh) + YoY % & QoQ % Change
  3. Operating Profit / EBITDA & Operating Margin (%) (if disclosed)
  4. Basic EPS (₹) & Dividend per share (if announced)
  5. Basis: Consolidated vs Standalone (state clearly)
- Also explain margin drivers, segment highlights, and year-on-year growth context ONLY from the PDF. If a specific figure is not in the PDF, write 'Not Disclosed' — NEVER guess or calculate wrong percentages.
"""
    if event_type == "ORDER":
        return """
MANDATORY ORDER / CONTRACT WIN FORMAT:
- You MUST include a clean HTML <table> (border-collapse:collapse; width:100%) titled Complete Order / Contract Details covering ALL Regulation 30 points from the PDF:
  1. Exact Order / Contract Value (₹ Crore / Lakh / USD — if not disclosed, clearly state 'Not Disclosed')
  2. Client / Awarding Entity Name
  3. Order Category: Domestic or International
  4. Nature & Scope of Work / Products Supplied
  5. Execution Timeline / Completion Period
  6. Promoter / Promoter Group Interest & Related-Party Transaction Disclosure (Yes/No as per filing)
- Explain how this order strengthens business visibility without giving buy/sell advice.
"""
    return """
MANDATORY STRUCTURED DETAILS FORMAT:
- Include a clean HTML <table> (border-collapse:collapse; width:100%) summarizing all key parameters (Company, Market Cap Category, Key Financial/Action Metric, Record/Effective Date, Board/SEBI Approval Details) directly verified from the PDF.
"""

def generate_and_strictly_verify(source, title, summary, pdf_text, link, company_name, cap_category, mcap_cr, event_type):
    print(f"📝 Generating Google #1 SEO Draft ({event_type} | {cap_category})...")
    mcap_note = f"₹{mcap_cr:,.0f} Crore ({cap_category})" if mcap_cr else cap_category
    today_str = get_ist_now().strftime("%B %d, %Y")
    event_rules = get_event_specific_instructions(event_type)

    filing_context = f"""
Date: {today_str}
Source: {source}
Company: {company_name}
Market Cap Category: {mcap_note}
Event Type: {event_type}
RSS Title: {title}
RSS Summary: {summary}
Official PDF Full Text:
{pdf_text if pdf_text else "PDF text not available; strictly stick to verified RSS details and mark undisclosed figures clearly."}
"""

    draft_prompt = f"""
You are a senior Indian equity research journalist writing for 'Market Munafa' (targeted for Google News Top Stories, Google #1 Search Ranking, fast Search Console indexing, and 100% AdSense E-E-A-T compliance).

Write a 100% ORIGINAL, deeply detailed, error-free, copyright-free financial news article from scratch based ONLY on the verified official filing below:

{filing_context}

{event_rules}

STRICT EDITORIAL, SEO & ADSENSE STANDARDS:
1. GOOGLE NEWS & ADSENSE WORD COUNT: Each language version (Gujarati, Hindi, AND English) MUST be comprehensive and complete — strictly **650 to 850 words** each! Zero thin content, zero half-written sections.
2. ZERO COPYRIGHT OVERLAP: Do NOT copy prose sentences from the PDF or RSS summary. Write completely fresh, natural journalistic analysis in your own words while keeping numbers and company facts 100% exact.
3. ENGLISH PRESERVATION RULE: Keep ALL numbers in English Arabic digits (`0-9`, e.g., `₹450 Crore`, `18.5%`, `Q2 FY27`), and keep ALL company names, stock tickers, and market terms (`NSE`, `BSE`, `SEBI`, `PAT`, `EBITDA`, `EPS`, `LOA`, `{company_name}`) strictly in English script even inside Gujarati and Hindi text!
4. NATURAL CONTEMPORARY LANGUAGE: Write warm, crisp, authoritative modern Gujarati, Hindi, and Indian English — zero robotic AI clichés ('delve', 'tapestry', 'game-changer', 'in conclusion', 'विकास ऑर्डर').
5. GOOGLE #1 SEO STRUCTURE (for each language HTML):
   - Direct inverted-pyramid opening paragraphs answering Who, What, Exact Value/Numbers, and Why it matters.
   - Clean styled HTML `<table>` with exact verified filing details (`border:1px solid #cbd5e1; padding:9px 12px;`).
   - 3–4 focused `<h2>` sections: (1) Complete Announcement / Filing Breakdown, (2) Key Business & Sector Context, (3) What Investors Should Track Next (balanced E-E-A-T view, strictly NO buy/sell tips or guaranteed returns), (4) Frequently Asked Questions (3 crisp FAQs).
6. CLEAN HTML ONLY: Use `<h2>`, `<h3>`, `<p>`, `<table>`, `<thead>`, `<tbody>`, `<tr>`, `<th>`, `<td>`, `<ul>`, `<li>`, `<strong>`. NEVER use markdown (`**`, `##`, `|---|`).

Return ONLY valid JSON with these exact keys:
{{
  "seo_title_en": "Keyword-first high-CTR English Google News Title with exact figure/event (58-76 chars)",
  "meta_description": "Compelling 145-155 char English SEO meta summary containing company name & key figure",
  "company_name": "{company_name}",
  "thumb_headline": "Short punchy 4-6 word English headline for thumbnail (include ₹ value / % if available)",
  "sentiment": "POSITIVE or NEGATIVE or NEUTRAL",
  "impact_gu": "1 crisp natural Gujarati sentence on investor impact (English digits & stock name)",
  "impact_hi": "1 crisp natural Hindi sentence on investor impact (English digits & stock name)",
  "impact_en": "1 crisp natural Indian English sentence on investor impact",
  "tags": ["Stock Market", "{cap_category}", "{company_name}", "NSE", "BSE", "Market News"],
  "faqs_schema": [
    {{"q": "Direct Google search question 1 in English?", "a": "Concise factual 2-sentence answer."}},
    {{"q": "Direct Google search question 2 in English?", "a": "Concise factual 2-sentence answer."}},
    {{"q": "Direct Google search question 3 in English?", "a": "Concise factual 2-sentence answer."}}
  ],
  "quick_story_gu": ["Point 1 with exact figures", "Point 2", "Point 3", "Point 4", "Point 5"],
  "quick_story_hi": ["Point 1 with exact figures", "Point 2", "Point 3", "Point 4", "Point 5"],
  "quick_story_en": ["Point 1 with exact figures", "Point 2", "Point 3", "Point 4", "Point 5"],
  "gujarati_html": "Complete 650-850 word Gujarati HTML article with exact data table, H2 sections, and 3 FAQs.",
  "hindi_html": "Complete 650-850 word Hindi HTML article with same rich structure and exact data table.",
  "english_html": "Complete 650-850 word Indian English HTML article with same rich structure and exact data table."
}}
"""
    current_json_str = call_gemini_json(draft_prompt, temperature=0.25)

    for attempt in range(1, MAX_AUDIT_ROUNDS + 1):
        print(f"🔍 Deep Quality, Fact & Copyright Audit Round {attempt}/{MAX_AUDIT_ROUNDS}...")
        try:
            draft_obj = json.loads(current_json_str)
        except Exception:
            draft_obj = {}

        en_words = count_words(draft_obj.get("english_html", ""))
        overlaps = find_copyright_overlaps(f"{summary}\n{pdf_text}", draft_obj.get("english_html", ""), ngram_size=8)

        auto_alerts = []
        if en_words < MIN_ENGLISH_WORDS:
            auto_alerts.append(f"WORD COUNT TOO SHORT ({en_words} words). Expand Gujarati, Hindi, and English articles to 650-850 rich, insightful words each (AdSense & Google News requirement).")
        if overlaps:
            auto_alerts.append(f"COPYRIGHT OVERLAP DETECTED: Completely rewrite sentences containing these 8-word source matches: {overlaps}")
        if "<table" not in str(draft_obj.get("gujarati_html", "")).lower():
            auto_alerts.append("MISSING HTML TABLE: Add a clean verified HTML <table> summarizing the Result/Order/Filing numbers in all 3 languages.")
        alert_str = "\n".join(auto_alerts) if auto_alerts else "Verify zero factual errors, zero typos, natural phrasing, and full Google News #1 readiness."

        audit_prompt = f"""
You are the Ruthless Chief Editor, Fact-Checker, and Google News SEO Auditor of 'Market Munafa'.
Deeply inspect and FIX EVERY SINGLE ERROR in this JSON article against the Official Filing & PDF Text.

{filing_context}

AUTOMATED CHECKER ALERTS TO FIX NOW:
{alert_str}

MANDATORY 7-POINT DEEP AUDIT:
1. 100% FACT & NUMBER ACCURACY: Verify every ₹ value, %, YoY/QoQ growth, client name, execution period, record date, and Standalone/Consolidated status directly against the PDF. Correct any mistake or missing PDF detail immediately.
2. ZERO HALLUCINATION: Delete any number, target price, or claim not backed by the official filing.
3. RESULT / ORDER COMPLETENESS: Confirm the HTML data `<table>` has complete verified details for this {event_type}.
4. ZERO COPYRIGHT OVERLAP: Rewrite any sentence closely echoing the PDF/summary into completely fresh journalism.
5. ZERO LANGUAGE/SPELLING ERRORS: Fix every typo, grammar slip, or unnatural phrase in Gujarati, Hindi, and English. Ensure English digits (0-9) and English company/stock names are preserved everywhere.
6. GOOGLE NEWS, ADSENSE & WORD COUNT: Ensure 650-850 words per language, neutral E-E-A-T tone, no buy/sell advice, keyword-focused English SEO title (58-76 chars), and 3 solid `faqs_schema` items.
7. CLEAN HTML: Zero markdown (`**`, `##`), properly closed tags.

FIX all issues directly in the JSON, then give an honest "quality_score" (0-100) and "issues_found" list.
Return ONLY valid JSON with all original keys PLUS "quality_score" (int) and "issues_found" (list).

ARTICLE JSON TO AUDIT & FIX:
{current_json_str}
"""
        audited_str = call_gemini_json(audit_prompt, temperature=0.1)
        data = enforce_english_digits(json.loads(audited_str))
        score = int(data.get("quality_score", 0))
        final_en_words = count_words(data.get("english_html", ""))
        final_overlaps = find_copyright_overlaps(f"{summary}\n{pdf_text}", data.get("english_html", ""), ngram_size=8)

        print(f"📊 Round {attempt} Score: {score}/100 | EN Words: {final_en_words} | Overlaps: {len(final_overlaps)} | Issues: {data.get('issues_found', [])}")

        has_markdown_bug = any(
            "**" in str(data.get(k, "")) or "##" in str(data.get(k, ""))
            for k in ["gujarati_html", "hindi_html", "english_html"]
        )
        all_langs_rich = (
            final_en_words >= MIN_ENGLISH_WORDS
            and len(strip_html(data.get("gujarati_html", ""))) >= 1400
            and len(strip_html(data.get("hindi_html", ""))) >= 1400
        )

        if score >= MIN_QUALITY_SCORE and not has_markdown_bug and all_langs_rich and len(final_overlaps) <= 1:
            print(f"✅ PASSED Strict Quality, Fact & Copyright Gate ({score}/100)!")
            data["hindi_html"] = sanitize_non_default_headings(data["hindi_html"])
            data["english_html"] = sanitize_non_default_headings(data["english_html"])
            return data

        current_json_str = json.dumps(data, ensure_ascii=False)
        time.sleep(2)

    raise ValueError(f"Article rejected: Did not meet strict {MIN_QUALITY_SCORE}/100 + word-count/copyright standards after {MAX_AUDIT_ROUNDS} rounds.")

# ================= BLOGGER HTML BUILDER (#1 SEO + DUAL SCHEMA + INTERNAL LINKS) =================
def render_quick_story_box(points, heading, accent_color):
    items_html = "".join(f"<li style='margin-bottom:8px;'>⚡ {pt}</li>" for pt in points)
    return f"""
    <div style="background:linear-gradient(135deg, #f8fafc 0%, #eef2ff 100%); border:1px solid #cbd5e1; border-left:6px solid {accent_color}; border-radius:12px; padding:16px 20px; margin-bottom:22px;">
      <div style="font-size:17px; font-weight:800; color:#0f172a; margin-bottom:10px;">🚀 {heading}</div>
      <ul style="margin:0; padding-left:14px; list-style:none; font-size:15px; color:#1e293b;">{items_html}</ul>
    </div>
    """

def render_impact_box(label, text, accent_color):
    return f"""
    <div style="background:#f8fafc; border-left:5px solid {accent_color}; padding:12px 16px; border-radius:6px; margin-bottom:18px; font-size:15px;">
      <strong>{label}:</strong> {text}
    </div>
    """

def build_blogger_html(data, thumb_url, source_name, cap_category, mcap_cr, official_link, related_posts, uid_short):
    sentiment = data.get("sentiment", "NEUTRAL").upper()
    if sentiment == "POSITIVE":
        badge_bg, badge_label = "#16a34a", "📈 POSITIVE / BULLISH NEWS"
    elif sentiment == "NEGATIVE":
        badge_bg, badge_label = "#dc2626", "📉 NEGATIVE / BEARISH NEWS"
    else:
        badge_bg, badge_label = "#2563eb", "ℹ️ IMPORTANT MARKET UPDATE"

    now_ist = get_ist_now()
    now_iso = now_ist.isoformat()
    ist_display = now_ist.strftime("%B %d, %Y | %I:%M %p IST")
    mcap_badge = f"{cap_category} (₹{mcap_cr:,.0f} Cr)" if mcap_cr else cap_category

    # Dual Schema: NewsArticle + FAQPage (Google News + Fast Search Console Indexing)
    news_schema = json.dumps({
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": data["seo_title_en"],
        "description": data.get("meta_description", data["seo_title_en"]),
        "keywords": ", ".join(data.get("tags", [])),
        "articleSection": "Indian Stock Market",
        "image": [thumb_url],
        "datePublished": now_iso,
        "dateModified": now_iso,
        "author": {"@type": "Organization", "name": "Market Munafa Editorial Desk", "url": BLOG_BASE_URL},
        "publisher": {
            "@type": "Organization",
            "name": "Market Munafa",
            "logo": {"@type": "ImageObject", "url": SITE_LOGO_URL}
        }
    }, ensure_ascii=False)

    faq_items = data.get("faqs_schema", [])
    faq_schema_tag = ""
    if faq_items:
        faq_schema = json.dumps({
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": f.get("q", ""),
                    "acceptedAnswer": {"@type": "Answer", "text": f.get("a", "")}
                }
                for f in faq_items if f.get("q") and f.get("a")
            ]
        }, ensure_ascii=False)
        faq_schema_tag = f'<script type="application/ld+json">{faq_schema}</script>'

    related_html = ""
    if related_posts:
        links_li = "".join(
            f"<li style='margin-bottom:8px;'><a href='{rp['url']}' style='color:#1d4ed8; font-weight:600; text-decoration:none;'>{rp['title']}</a></li>"
            for rp in related_posts
        )
        related_html = f"""
        <div style="margin-top:26px; padding:16px 18px; background:#f8fafc; border:1px solid #cbd5e1; border-radius:10px;">
          <div style="font-size:17px; font-weight:800; color:#0f172a; margin-bottom:10px;">🔗 Also Read on Market Munafa</div>
          <ul style="margin:0; padding-left:20px; line-height:1.7;">{links_li}</ul>
        </div>
        """

    return f"""
<script type="application/ld+json">{news_schema}</script>
{faq_schema_tag}

<article id="mm-post-{uid_short}" style="font-family: 'Noto Sans Gujarati', 'Noto Sans Devanagari', 'Segoe UI', Roboto, Arial, sans-serif; line-height: 1.85; color: #1e293b;">
  <style>
    #mm-post-{uid_short} table {{ width:100%; border-collapse:collapse; margin:16px 0 22px 0; font-size:14.5px; }}
    #mm-post-{uid_short} th {{ background:#0f172a; color:#ffffff; padding:10px 12px; border:1px solid #cbd5e1; text-align:left; }}
    #mm-post-{uid_short} td {{ padding:10px 12px; border:1px solid #cbd5e1; background:#ffffff; }}
    #mm-post-{uid_short} tr:nth-child(even) td {{ background:#f8fafc; }}
  </style>

  <figure style="text-align:center; margin:0 0 18px 0;">
    <img src="{thumb_url}" alt="{data['seo_title_en']}" title="{data['seo_title_en']}" width="1280" height="720" style="width:100%; max-width:820px; height:auto; border-radius:14px; box-shadow:0 8px 24px rgba(0,0,0,0.12);" />
  </figure>

  <div style="background:{badge_bg}; color:#ffffff; padding:12px 18px; border-radius:10px; font-weight:bold; font-size:15.5px; margin-bottom:14px; text-align:center;">
    {badge_label} &nbsp;|&nbsp; {mcap_badge} &nbsp;|&nbsp; Source: {source_name}
  </div>

  <p style="font-size:13.5px; color:#64748b; text-align:center; margin:0 0 16px 0;">
    📅 Published: {ist_display} &nbsp;•&nbsp; By <strong>Market Munafa Equity Research Desk</strong> &nbsp;•&nbsp; 4 min read
  </p>

  <div class="mm-langbar" style="display:flex; gap:10px; justify-content:center; margin-bottom:22px; flex-wrap:wrap;">
    <button type="button" class="mm-lbtn" onclick="switchLang_{uid_short}('gu')" id="btn-{uid_short}-gu" style="padding:10px 22px; border:none; border-radius:8px; background:#0f172a; color:#fff; font-weight:bold; cursor:pointer;">ગુજરાતી</button>
    <button type="button" class="mm-lbtn" onclick="switchLang_{uid_short}('hi')" id="btn-{uid_short}-hi" style="padding:10px 22px; border:none; border-radius:8px; background:#e2e8f0; color:#0f172a; font-weight:bold; cursor:pointer;">हिन्दी</button>
    <button type="button" class="mm-lbtn" onclick="switchLang_{uid_short}('en')" id="btn-{uid_short}-en" style="padding:10px 22px; border:none; border-radius:8px; background:#e2e8f0; color:#0f172a; font-weight:bold; cursor:pointer;">English</button>
  </div>

  <div id="lang-{uid_short}-gu" class="mm-lang-pane" data-lang="gu" style="display:block;">
    {render_impact_box("રોકાણકારો માટે અસર", data.get("impact_gu", ""), badge_bg)}
    {render_quick_story_box(data.get("quick_story_gu", []), "ક્વિક સ્ટોરી — મુખ્ય મુદ્દાઓ", badge_bg)}
    {data['gujarati_html']}
  </div>

  <div id="lang-{uid_short}-hi" class="mm-lang-pane" data-lang="hi" style="display:none;">
    {render_impact_box("निवेशकों पर असर", data.get("impact_hi", ""), badge_bg)}
    {render_quick_story_box(data.get("quick_story_hi", []), "क्विक स्टोरी — मुख्य बातें", badge_bg)}
    {data['hindi_html']}
  </div>

  <div id="lang-{uid_short}-en" class="mm-lang-pane" data-lang="en" style="display:none;">
    {render_impact_box("Impact on Investors", data.get("impact_en", ""), badge_bg)}
    {render_quick_story_box(data.get("quick_story_en", []), "Quick Story — Key Highlights", badge_bg)}
    {data['english_html']}
  </div>

  {related_html}

  <hr style="margin:24px 0; border:0; border-top:1px solid #cbd5e1;" />
  <div style="background:#f1f5f9; padding:14px 16px; border-radius:10px; font-size:13px; color:#475569; line-height:1.65;">
    <strong>Official Exchange Reference:</strong> <a href="{official_link}" target="_blank" rel="nofollow noopener">View Official {source_name} Filing</a><br/>
    <strong>Editorial & SEBI Disclaimer (Market Munafa):</strong> આ આર્ટિકલ સત્તાવાર એક્સચેન્જ ફાઈલિંગ (NSE/BSE/SEBI) ના આધારે માત્ર માહિતી અને શૈક્ષણિક હેતુ માટે તૈયાર કરવામાં આવ્યો છે. આ કોઈ શેર ખરીદવા કે વેચવાની સલાહ નથી; રોકાણ કરતા પહેલા તમારા SEBI રજિસ્ટર્ડ સલાહકારની સલાહ લો.
  </div>
</article>

<script>
function switchLang_{uid_short}(lang) {{
  ['gu', 'hi', 'en'].forEach(function(l) {{
    var pane = document.getElementById('lang-{uid_short}-' + l);
    var btn = document.getElementById('btn-{uid_short}-' + l);
    if (pane) pane.style.display = (l === lang) ? 'block' : 'none';
    if (btn) {{
      btn.style.background = (l === lang) ? '#0f172a' : '#e2e8f0';
      btn.style.color = (l === lang) ? '#ffffff' : '#0f172a';
    }}
  }});
}}
</script>
"""

# ================= MAIN EXECUTION =================
def main():
    history = load_history()
    history_keys = set(history["keys"])
    history_titles = list(history["titles"])

    blogger_service = get_blogger_service()
    recent_posts = get_recent_blogger_posts(blogger_service, limit=50)
    published_count = 0

    all_today_candidates = []

    for feed_info in FEEDS:
        print(f"📡 Scanning today's filings from: {feed_info['name']}...")
        try:
            resp = requests.get(feed_info["url"], headers=HEADERS, timeout=22)
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"⚠️ Failed to fetch {feed_info['name']}: {e}")
            continue

        for entry in parsed.entries[:150]:
            if not is_today_ist(entry):
                continue
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "") or entry.get("description", "")

            if not title or not link:
                continue
            if not is_important_news(title, summary):
                continue

            uid = hashlib.md5(f"{title}|{link}".encode()).hexdigest()
            if uid in history_keys:
                continue

            all_today_candidates.append({
                "source": feed_info["name"],
                "title": title,
                "link": link,
                "summary": summary,
                "uid": uid,
                "dt": get_entry_dt(entry),
            })

    # આજના સૌથી નવા મહત્વના ન્યૂઝ પહેલા પ્રોસેસ કરો
    all_today_candidates.sort(key=lambda x: x["dt"], reverse=True)
    print(f"🔎 Found {len(all_today_candidates)} today's important candidate filings across all exchanges.")

    for item in all_today_candidates:
        if published_count >= MAX_POSTS_PER_RUN:
            print(f"ℹ️ Reached per-run limit ({MAX_POSTS_PER_RUN}). Remaining today's filings will be covered in the next scheduled run.")
            break

        title = item["title"]
        link = item["link"]
        summary = item["summary"]
        source = item["source"]
        uid = item["uid"]

        raw_company = extract_company_prefix_from_title(title)
        prelim_event = detect_event_type(title, summary)
        prelim_key = normalize_company_day_key(raw_company, prelim_event)

        if uid in history_keys or prelim_key in history_keys:
            continue

        print(f"\n🔥 Checking [{source} | {item['dt'].strftime('%I:%M %p IST')}]: {title}")
        try:
            # 1) Screener.in પરથી Large / Mid / Small Cap તપાસ (SEBI Update સિવાય)
            screener_info = {"found": False, "mcap_cr": None, "category": "Market Wide", "screener_name": "SEBI"}
            if source != "SEBI Update":
                screener_info = get_market_cap_info(raw_company)
                if screener_info["found"] and screener_info["mcap_cr"] is not None:
                    print(f"   💹 Market Cap: ₹{screener_info['mcap_cr']:,.0f} Cr ({screener_info['category']})")
                    if screener_info["mcap_cr"] < MIN_MARKET_CAP_CR:
                        print(f"   ⏭️ Skipped Micro-Cap/SME stock (< ₹{MIN_MARKET_CAP_CR:.0f} Cr).")
                        history_keys.add(uid)
                        history["keys"].append(uid)
                        save_history(history)
                        continue

            # 2) ઓફિશિયલ PDF માંથી સંપૂર્ણ વિગત ખેંચો
            pdf_text = extract_pdf_text(link)
            print(f"   📄 Extracted {len(pdf_text)} chars from official PDF.")

            # 3) AI Pre-Screening: પરચુરણ ન્યૂઝ અને Micro-cap/SME સખત રીતે બહાર કાઢો
            screen = pre_screen_filing(source, title, summary, pdf_text, screener_info)
            if not screen.get("is_eligible"):
                print(f"   ⏭️ Skipped by Materiality/Cap Gate: {screen.get('reject_reason', 'Not high impact')}")
                history_keys.add(uid)
                history["keys"].append(uid)
                save_history(history)
                continue

            company_name = screen.get("company_name") or screener_info.get("screener_name") or raw_company
            cap_category = screener_info["category"] if screener_info["found"] else screen.get("cap_category", "Small Cap")
            if cap_category.lower() in ("micro cap", "sme", "unknown"):
                print(f"   ⏭️ Skipped ineligible market cap category: {cap_category}")
                history_keys.add(uid)
                history["keys"].append(uid)
                save_history(history)
                continue

            event_type = screen.get("event_type") or detect_event_type(title, summary, pdf_text)
            final_comp_key = normalize_company_day_key(company_name, event_type)
            if final_comp_key in history_keys:
                print(f"   ⏭️ Already posted today ({final_comp_key}).")
                continue

            # 4) સંપૂર્ણ વિગતવાર, 3-રાઉન્ડ ઓડિટેડ SEO આર્ટિકલ બનાવો
            article_data = generate_and_strictly_verify(
                source, title, summary, pdf_text, link,
                company_name, cap_category, screener_info.get("mcap_cr"), event_type
            )

            # 5) Blogger પર ડુપ્લીકેટ ચેક
            if is_duplicate_post(article_data["company_name"], article_data["seo_title_en"], event_type, recent_posts, history_titles):
                print(f"   ⏭️ Skipped duplicate already on Blogger: {article_data['company_name']}")
                history_keys.add(uid)
                history_keys.add(final_comp_key)
                history["keys"].extend([uid, final_comp_key])
                save_history(history)
                continue

            # 6) થમ્બનેલ બનાવી તરત GitHub પર લાઈવ કરો
            thumb_url = create_and_push_thumbnail(
                article_data["company_name"],
                article_data["thumb_headline"],
                article_data["sentiment"],
                cap_category,
                source,
                uid[:10],
            )

            # 7) સ્વચ્છ લેબલ્સ અને Internal Links તૈયાર કરો
            raw_labels = list(article_data.get("tags", [])) + [cap_category, "MARKET NEWS"]
            if event_type == "RESULT":
                raw_labels.append("STOCK RESULT")
            elif event_type == "ORDER":
                raw_labels.append("SHARE ORDER")

            clean_labels = []
            total_len = 0
            for lbl in raw_labels:
                c = re.sub(r"[^A-Za-z0-9\s\-&]", "", str(lbl)).strip()[:28]
                if c and c not in clean_labels and (total_len + len(c) + 1) <= 185:
                    clean_labels.append(c)
                    total_len += len(c) + 1

            related_posts = pick_related_internal_links(recent_posts, clean_labels, max_links=3)
            post_html = build_blogger_html(
                article_data, thumb_url, source, cap_category,
                screener_info.get("mcap_cr"), link, related_posts, uid[:8]
            )

            # 8) Blogger પર પબ્લિશ કરો
            body = {
                "kind": "blogger#post",
                "title": article_data["seo_title_en"],
                "content": post_html,
                "labels": clean_labels[:10],
            }
            post = blogger_service.posts().insert(blogId=os.environ["BLOG_ID"], body=body, isDraft=False).execute()
            post_url = post.get("url", "")
            print(f"   ✅ Published: {post_url}")

            recent_posts.insert(0, {
                "title": article_data["seo_title_en"],
                "url": post_url,
                "labels": clean_labels[:10],
                "published": get_ist_now().isoformat(),
            })
            history_keys.update([uid, prelim_key, final_comp_key])
            history_titles.append(article_data["seo_title_en"])
            history["keys"].extend([uid, prelim_key, final_comp_key])
            history["titles"].append(article_data["seo_title_en"])
            save_history(history)
            published_count += 1
            time.sleep(5)

        except Exception as e:
            print(f"   ❌ NOT PUBLISHED '{title}': {e}")

    print(f"\n🎉 Done. Total high-impact Large/Mid/Small Cap posts published in this run: {published_count}")

if __name__ == "__main__":
    main()
