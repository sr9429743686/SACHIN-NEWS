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

# ફક્ત gemini-3.5-flash-lite (એક પણ 404 વોર્નિંગ નહીં આવે)
SCREEN_MODELS = ["gemini-3.5-flash-lite"]
WRITE_MODELS = ["gemini-3.5-flash-lite"]

REQUIRED_TOTAL_SCORE = 200   # 100 (Hard Python Gate) + 100 (Editorial Audit) = 200/200 ફરજિયાત
MAX_FIX_ROUNDS = 3
MIN_MARKET_CAP_CR = 500.0    # ₹500 Cr થી નીચેના Micro-Cap / SME સ્ટોક્સ બ્લોક
MIN_ENGLISH_WORDS = 500      # Google News & AdSense માટે પૂરતી લંબાઈ
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "10"))

BLOG_BASE_URL = "https://marketmunafa.blogspot.com/"
SITE_LOGO_URL = "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgXG-GojgyTIZWy4ftRRsgpLsDMxrAEKCRb_BnZsfVTKT0_T9IELdKQYKgACOQkhus69xfx_xg-iVyLvP2-RAki8voVBwja88Dfsg_tQRQR7JvgegMGFfEQfm73oO00WeTo1vPUHghTXmXd_by6k0C-m3Y_5JhoYEjUF8Y8V3KKTbGnUn53dUY-6AN32_A/s1254/1001173067.png"

# 100% સત્તાવાર (Official) NSE, BSE અને SEBI RSS ફીડ્સ (કુલ 16 ફીડ્સ)
FEEDS = [
    # --- NSE Official Feeds ---
    {"name": "NSE Announcement", "urls": ["https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"]},
    {"name": "NSE Corporate Action", "urls": ["https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"]},
    {"name": "NSE Daily Buyback", "urls": ["https://nsearchives.nseindia.com/content/RSS/Daily_Buyback.xml"]},
    {"name": "NSE Board Meetings", "urls": ["https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml"]},
    {"name": "NSE Shareholding Pattern", "urls": ["https://nsearchives.nseindia.com/content/RSS/Shareholding_Pattern.xml"]},
    {"name": "NSE Annual Reports", "urls": ["https://nsearchives.nseindia.com/content/RSS/Annual_Reports.xml"]},
    {"name": "NSE BRSR", "urls": ["https://nsearchives.nseindia.com/content/RSS/brsr.xml"]},
    {"name": "NSE Circular", "urls": ["https://nsearchives.nseindia.com/content/RSS/Circulars.xml"]},

    # --- BSE Official Feeds ---
    {"name": "BSE Announcement", "urls": ["https://www.bseindia.com/data/xml/announcements.aspx"]},
    {"name": "BSE Corporate Action", "urls": ["https://www.bseindia.com/data/XML/CorpActionFeed.aspx"]},
    {"name": "BSE Insider Trading", "urls": ["https://www.bseindia.com/Data/XML/InsiderTradingFeed.aspx"]},
    {"name": "BSE Board Meetings", "urls": ["https://beta.bseindia.com/Data/XML/BoardMeetingsFeed.xml"]},
    {"name": "BSE Annual Reports", "urls": ["https://www.bseindia.com/Data/XML/AnnualReportFeed.aspx"]},
    {"name": "BSE Notice", "urls": ["https://www.bseindia.com/data/xml/notices.xml"]},
    {"name": "BSE Sensex Update", "urls": ["https://www.bseindia.com/data/xml/sensexrss.xml"]},

    # --- SEBI Official Feed ---
    {"name": "SEBI Update", "urls": ["https://www.sebi.gov.in/sebirss.xml"]},
]

# માર્કેટ-વાઇડ ફીડ્સ (આના માટે સિંગલ કંપનીનું Screener માર્કેટ કેપ ચેક નહીં થાય)
MARKET_WIDE_SOURCES = {"SEBI Update", "NSE Circular", "BSE Sensex Update"}

# ખાસ ડેડિકેટેડ ફીડ્સ (જેમના ફાઈલિંગ સીધા મટિરિયાલિટી ગેટ પર જશે)
DEDICATED_PASS_SOURCES = {
    "NSE Daily Buyback",
    "BSE Insider Trading",
    "NSE Corporate Action",
    "BSE Corporate Action",
    "NSE Circular",
    "BSE Sensex Update",
    "SEBI Update",
    "NSE Shareholding Pattern",
    "NSE Annual Reports",
    "BSE Annual Reports",
    "NSE BRSR",
}

IMPORTANT_KEYWORDS = [
    "financial result", "quarterly result", "audited result", "unaudited result",
    "q1", "q2", "q3", "q4", "net profit", "revenue", "ebitda",
    "bagged", "work order", "purchase order", "contract", "letter of award", "loa", "l1 bidder", "tender", "order",
    "dividend", "bonus", "stock split", "sub-division", "buyback",
    "merger", "amalgamation", "acquisition", "acquire", "demerger", "takeover",
    "rights issue", "qip", "fund raising", "preferential issue", "capex", "expansion", "commissioning",
    "joint venture", "strategic partnership", "usfda", "fda approval", "patent",
    "delisting", "open offer", "sebi", "circular", "regulation", "guidelines",
    "credit rating upgrade", "downgrade", "insider", "promoter", "stake", "disposal",
    "sensex", "annual report", "shareholding", "brsr", "sustainability", "esg"
]

IGNORE_KEYWORDS = [
    "loss of share", "duplicate share", "trading window", "newspaper publication",
    "certificate under regulation 74", "regulation 74(5)", "regulation 74 (5)",
    "reconciliation of share capital", "intimation of schedule", "analyst/institutional",
    "investor meet", "investors meet", "conference call", "earnings call", "transcript",
    "audio recording", "video recording", "scrutinizer", "voting results", "postal ballot",
    "notice of agm", "notice of egm", "annual general meeting", "extraordinary general meeting",
    "esop", "esos", "employee stock option", "rta", "registrar and transfer",
    "registered office", "clarification on price", "spurt in volume", "movement in price",
    "compliance certificate", "statement of investor complaints", "grievance redressal",
    "corporate governance report", "book closure",
    "record date for interest", "interest payment", "redemption of ncd", "debenture",
    "non-convertible", "commercial paper", "sme", "emerge", "strike off", "closure of subsidiary",
    "resignation of company secretary", "appointment of company secretary",
    "change in directorate", "cessation of independent director", "re-appointment of independent",
    "independent director", "composition of board", "composition of the board",
    "regulation 17", "regulation 18", "regulation 19", "regulation 20", "regulation 21",
    "non-compliance with the composition", "shortfall of independent", "woman director",
    "corrigendum to notice"
]

BANNED_AI_PHRASES = [
    "delve into", "tapestry", "in conclusion", "game-changer", "testament to",
    "ever-evolving", "rollercoaster", "pivotal moment", "underscores the importance",
    "double-edged sword", "landscape of", "in today's fast-paced", "it is worth noting",
    "all eyes are on", "only time will tell", "विकास ऑर्डर", "यह ध्यान देने योग्य है",
    "निष्कर्ष के तौर पर", "અંતમાં કહીએ તો", "એ નોંધવું મહત્વપૂર્ણ છે"
]

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

# ================= CORE UTILITIES =================
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

def strip_html(html_str):
    txt = re.sub(r"<[^>]+>", " ", str(html_str or ""))
    return " ".join(txt.split())

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
    part = re.split(r"\s[-–—:|]\s", title, maxsplit=1)[0].strip()
    part = re.sub(r"\(.*?\)", "", part).strip()
    return part[:60]

def normalize_company_day_key(company_text, event_type="GEN"):
    clean = re.sub(r"[^a-z0-9\s]", " ", company_text.lower())
    stop = {"limited", "ltd", "india", "ind", "private", "pvt", "the", "of", "and", "co", "corp", "corporation", "industries", "enterprises"}
    words = [w for w in clean.split() if w not in stop and len(w) > 1]
    today_str = get_ist_now().strftime("%Y-%m-%d")
    return f"{today_str}|{'_'.join(words[:3])}|{event_type}"

def is_today_ist(entry):
    pub_str = entry.get("published") or entry.get("updated") or ""
    if not pub_str:
        return False
    try:
        clean_pub = re.sub(r"\bIST\b", "+0530", pub_str.strip())
        dt = date_parser.parse(clean_pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_TZ)
        return dt.astimezone(IST_TZ).date() == get_ist_now().date()
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

def detect_event_type(title, summary, pdf_text="", source=""):
    src_low = source.lower()
    text = f"{title} {summary} {pdf_text[:1500]}".lower()
    if "insider" in src_low or "insider trading" in text or "sast" in text:
        return "INSIDER_TRADING"
    if "buyback" in src_low or "buyback" in text:
        return "BUYBACK"
    if "board meeting" in src_low:
        return "BOARD_MEETING"
    if "shareholding" in src_low:
        return "SHAREHOLDING"
    if "annual report" in src_low:
        return "ANNUAL_REPORT"
    if "brsr" in src_low:
        return "BRSR_ESG"
    if "sensex" in src_low:
        return "MARKET_INDEX"
    if any(k in text for k in ["financial result", "quarterly result", "audited financial", "unaudited financial", "statement of financial results", "q1", "q2", "q3", "q4"]):
        return "RESULT"
    if any(k in text for k in ["work order", "purchase order", "letter of award", "loa", "bagged order", "received order", "contract worth", "order worth", "l1 bidder", "award of contract", "order"]):
        return "ORDER"
    if any(k in text for k in ["dividend", "bonus", "stock split", "sub-division"]):
        return "CORPORATE_ACTION"
    if any(k in text for k in ["merger", "acquisition", "amalgamation", "demerger", "takeover", "joint venture"]):
        return "M_AND_A"
    if "circular" in src_low or "sebi" in src_low or any(k in text for k in ["sebi", "circular", "usfda", "fda", "regulation", "guidelines"]):
        return "REGULATORY"
    return "IMPORTANT_UPDATE"

def is_important_news(title, summary, source=""):
    text = f"{title} {summary}".lower()
    if any(bad in text for bad in IGNORE_KEYWORDS):
        return False

    # બોર્ડ મીટિંગ ફીડમાં ફક્ત મોટા એજન્ડા (રિઝલ્ટ, ડિવિડન્ડ, બોનસ, સ્પ્લિટ, બાયબેક, ફંડ રેઈઝ, મર્જર) જ લો
    if source in ("NSE Board Meetings", "BSE Board Meetings"):
        high_agenda = [
            "result", "dividend", "bonus", "split", "sub-division", "buyback",
            "fund raising", "rights issue", "qip", "preferential", "merger", "demerger", "acquisition"
        ]
        return any(k in text for k in high_agenda)

    # ડેડિકેટેડ સત્તાવાર ફીડ્સને સીધા મટિરિયાલિટી ગેટ પર જવા દો
    if source in DEDICATED_PASS_SOURCES:
        return True

    return any(good in text for good in IMPORTANT_KEYWORDS)

def is_duplicate_post(company_name, seo_title, recent_posts, history_titles, is_market_wide=False):
    comp_clean = re.sub(r"[^a-z0-9\s]", " ", company_name.lower()).strip()
    stop = {"limited", "ltd", "india", "ind", "private", "pvt", "the", "of", "and", "corporation"}
    comp_words = [w for w in comp_clean.split() if w not in stop and len(w) > 2][:2]
    all_titles = [p["title"] for p in recent_posts] + history_titles

    new_norm = re.sub(r"[^a-z0-9\s]", "", seo_title.lower()).strip()
    today_tag = get_ist_now().strftime("%Y-%m-%d")

    for old_t in all_titles:
        old_norm = re.sub(r"[^a-z0-9\s]", "", str(old_t).lower()).strip()
        if old_norm and difflib.SequenceMatcher(None, new_norm, old_norm).ratio() >= 0.56:
            return True

    if not is_market_wide:
        for p in recent_posts:
            p_date = str(p.get("published", ""))[:10]
            p_title_low = p["title"].lower()
            if p_date == today_tag and comp_words and all(w in p_title_low for w in comp_words):
                return True
    return False

# ================= OFFICIAL DIRECT FETCHER (DoH DNS FIX) =================
def get_official_referer(url):
    low = url.lower()
    if "nseindia.com" in low:
        return "https://www.nseindia.com/"
    if "bseindia.com" in low:
        return "https://www.bseindia.com/"
    if "sebi.gov.in" in low:
        return "https://www.sebi.gov.in/"
    return None

def resolve_official_ip_doh(hostname):
    """ફક્ત સત્તાવાર ડોમેનનું IP શોધવા માટે Google/Cloudflare DNS-over-HTTPS વાપરે છે."""
    doh_endpoints = [
        f"https://dns.google/resolve?name={hostname}&type=A",
        f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A",
    ]
    for ep in doh_endpoints:
        try:
            r = requests.get(ep, headers={"Accept": "application/dns-json"}, timeout=8)
            if r.status_code == 200:
                for ans in r.json().get("Answer", []):
                    if ans.get("type") == 1 and ans.get("data"):
                        return ans["data"]
        except Exception:
            continue
    return None

def fetch_official_url_bytes(url, referer=None):
    """માત્ર સત્તાવાર URL પરથી જ ડેટા લાવે છે; DNS ફેઈલ થાય તો સત્તાવાર IP પર સીધું HTTPS કનેક્શન કરે છે."""
    req_headers = dict(HEADERS)
    ref = referer or get_official_referer(url)
    if ref:
        req_headers["Referer"] = ref
    try:
        resp = requests.get(url, headers=req_headers, timeout=18)
        if resp.status_code == 200 and resp.content:
            return resp.content, resp.headers.get("Content-Type", "")
    except Exception:
        pass

    parsed_u = urllib.parse.urlparse(url)
    host = parsed_u.hostname
    if not host:
        return None, ""
    port = parsed_u.port or (443 if parsed_u.scheme == "https" else 80)
    ip = resolve_official_ip_doh(host)
    if not ip:
        return None, ""
    try:
        cmd = [
            "curl", "-sL", "--max-time", "20",
            "--resolve", f"{host}:{port}:{ip}",
            "-A", HEADERS["User-Agent"],
        ]
        if ref:
            cmd.extend(["-e", ref])
        cmd.append(url)
        res = subprocess.run(cmd, capture_output=True, timeout=25)
        if res.returncode == 0 and res.stdout:
            return res.stdout, ""
    except Exception:
        pass
    return None, ""

def fetch_official_feed(feed_info):
    name = feed_info["name"]
    for url in feed_info["urls"]:
        content, _ = fetch_official_url_bytes(url)
        if content:
            parsed = feedparser.parse(content)
            if parsed.entries:
                return parsed
    print(f"ℹ️ {name} સત્તાવાર ફીડ હાલ ખાલી/અનુપલબ્ધ છે — સ્કિપ કર્યું.")
    return None

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
                    comp_item = arr[0]
                    break

        if comp_item and comp_item.get("url"):
            page_url = f"https://www.screener.in{comp_item['url']}"
            pr = requests.get(page_url, headers=HEADERS, timeout=12)
            if pr.status_code == 200:
                m = re.search(
                    r"Market\s*Cap.*?<span\s+class=[\"']number[\"'][^>]*>\s*([0-9,\.]+)\s*</span>",
                    pr.text,
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

# ================= BLOGGER & OFFICIAL PDF HELPERS =================
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

def extract_pdf_text(link, depth=0):
    """સીધા સત્તાવાર NSE/BSE/SEBI સર્વર પરથી જ PDF અથવા સત્તાવાર ડોક્યુમેન્ટ વાંચે છે."""
    if not link:
        return ""
    referer = get_official_referer(link)
    content, content_type = fetch_official_url_bytes(link, referer=referer)
    if not content:
        return ""

    ct = (content_type or "").lower()
    try:
        if "pdf" in ct or link.lower().endswith(".pdf") or content[:4] == b"%PDF":
            reader = PdfReader(io.BytesIO(content))
            extracted = [p.extract_text().strip() for p in reader.pages[:8] if p.extract_text()]
            return "\n".join(extracted)[:14000]

        if "sebi.gov.in" in link.lower() and depth < 1:
            html_text = content.decode("utf-8", errors="ignore")
            pdf_match = re.search(r'href=["\'](https?://[^"\']+\.pdf|/sebi_data/[^"\']+\.pdf)["\']', html_text, flags=re.I)
            if pdf_match:
                inner_pdf = pdf_match.group(1)
                if inner_pdf.startswith("/"):
                    inner_pdf = "https://www.sebi.gov.in" + inner_pdf
                return extract_pdf_text(inner_pdf, depth=depth + 1)
            clean_txt = strip_html(html_text)
            if len(clean_txt) > 500:
                return clean_txt[:9000]
    except Exception:
        pass
    return ""

# ================= GEMINI CALLER =================
def clean_json_response(raw_text):
    txt = (raw_text or "").strip()
    txt = re.sub(r"^```json\s*|\s*```$", "", txt, flags=re.I).strip()
    return txt

def call_gemini_json(prompt, temperature=0.25, models=None):
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

    model_list = models or WRITE_MODELS
    last_err = None
    for model_name in model_list:
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
                print(f"⚠️ Gemini error ({model_name}), trying next key: {e}")
                last_err = e
                time.sleep(1.5)
    raise RuntimeError(f"All Gemini keys failed: {last_err}")

# ================= HARD PYTHON VALIDATORS (ZERO AI TRUST) =================
def count_english_words(html_str):
    return len(re.findall(r"[A-Za-z0-9]+", strip_html(html_str)))

def script_stats(text, unicode_range):
    plain = strip_html(text)
    target_chars = len(re.findall(f"[{unicode_range}]", plain))
    latin_chars = len(re.findall(r"[A-Za-z]", plain))
    total_alpha = target_chars + latin_chars
    ratio = (target_chars / total_alpha) if total_alpha > 0 else 0.0
    return target_chars, ratio

def find_copyright_overlaps(source_text, article_html, ngram_size=8):
    if not source_text or len(source_text) < 200:
        return []
    src_words = re.findall(r"[a-z0-9]+", source_text.lower())
    art_words = re.findall(r"[a-z0-9]+", strip_html(article_html).lower())
    if len(src_words) < ngram_size or len(art_words) < ngram_size:
        return []
    src_grams = {" ".join(src_words[i:i + ngram_size]) for i in range(len(src_words) - ngram_size + 1)}
    matches = []
    for i in range(len(art_words) - ngram_size + 1):
        gram = " ".join(art_words[i:i + ngram_size])
        if gram in src_grams and gram not in matches:
            if any(legal in gram for legal in ["regulation 30", "sebi listing obligations", "disclosure requirements"]):
                continue
            matches.append(gram)
            if len(matches) >= 8:
                break
    return matches

def run_hard_python_gate(bundle, source_text):
    failures = []

    gu_imp_c, gu_imp_r = script_stats(bundle.get("impact_gu", ""), r"\u0A80-\u0AFF")
    if gu_imp_c < 18 or gu_imp_r < 0.30:
        failures.append(f"impact_gu is NOT in Gujarati script (gu_chars={gu_imp_c}, ratio={gu_imp_r:.2f}). Write strictly in natural Gujarati.")

    qs_gu = bundle.get("quick_story_gu", [])
    if len(qs_gu) < 4:
        failures.append("quick_story_gu must have 4-5 Gujarati bullet points.")
    for idx, pt in enumerate(qs_gu, 1):
        c, r = script_stats(pt, r"\u0A80-\u0AFF")
        if c < 14 or r < 0.25:
            failures.append(f"quick_story_gu bullet #{idx} leaked into English. Rewrite strictly in natural Gujarati (only company name & 0-9 digits in English).")

    gu_body_c, gu_body_r = script_stats(bundle.get("gujarati_html", ""), r"\u0A80-\u0AFF")
    if gu_body_c < 1100 or gu_body_r < 0.48:
        failures.append(f"gujarati_html too short or too much English (gu_chars={gu_body_c}, ratio={gu_body_r:.2f}).")

    hi_imp_c, hi_imp_r = script_stats(bundle.get("impact_hi", ""), r"\u0900-\u097F")
    if hi_imp_c < 18 or hi_imp_r < 0.30:
        failures.append(f"impact_hi is NOT in Hindi Devanagari script (hi_chars={hi_imp_c}, ratio={hi_imp_r:.2f}).")

    qs_hi = bundle.get("quick_story_hi", [])
    if len(qs_hi) < 4:
        failures.append("quick_story_hi must have 4-5 Hindi bullet points.")
    for idx, pt in enumerate(qs_hi, 1):
        c, r = script_stats(pt, r"\u0900-\u097F")
        if c < 14 or r < 0.25:
            failures.append(f"quick_story_hi bullet #{idx} leaked into English. Rewrite strictly in natural Hindi.")

    hi_body_c, hi_body_r = script_stats(bundle.get("hindi_html", ""), r"\u0900-\u097F")
    if hi_body_c < 1100 or hi_body_r < 0.48:
        failures.append(f"hindi_html too short or too much English (hi_chars={hi_body_c}, ratio={hi_body_r:.2f}).")

    en_html = bundle.get("english_html", "")
    en_words = count_english_words(en_html)
    if en_words < MIN_ENGLISH_WORDS:
        failures.append(f"english_html too short ({en_words} words; minimum {MIN_ENGLISH_WORDS} required).")
    if re.search(r"[\u0900-\u0D7F]", strip_html(en_html)):
        failures.append("english_html contains non-English Indic characters.")

    for field in ["gujarati_html", "hindi_html", "english_html"]:
        val = str(bundle.get(field, ""))
        if "**" in val or "##" in val or "|---" in val:
            failures.append(f"{field} contains raw markdown (** or ##). Use clean HTML tags only.")
        if "<table" not in val.lower():
            failures.append(f"{field} is missing the structured HTML <table> of verified details.")

    combined_low = " ".join([
        strip_html(en_html), strip_html(bundle.get("gujarati_html", "")), strip_html(bundle.get("hindi_html", ""))
    ]).lower()
    found_cliches = [p for p in BANNED_AI_PHRASES if p in combined_low]
    if found_cliches:
        failures.append(f"Robotic AI phrases detected: {found_cliches}. Rewrite naturally like a human business journalist.")

    overlaps = find_copyright_overlaps(source_text, en_html, ngram_size=8)
    if overlaps:
        failures.append(f"Copyright overlap (8+ words matched source): {overlaps[:4]}. Rewrite those sentences completely in fresh words.")

    short_comp = str(bundle.get("short_company_name", "")).strip()
    if not short_comp or len(short_comp) > 26:
        failures.append(f"short_company_name ('{short_comp}') must be <= 24 chars.")

    seo_title = str(bundle.get("seo_title_en", "")).strip()
    if len(seo_title) < 35 or len(seo_title) > 85:
        failures.append(f"seo_title_en length ({len(seo_title)}) must be between 45 and 78 characters.")

    python_score = 100 if not failures else max(0, 100 - (len(failures) * 15))
    return python_score, failures

# ================= STRICT PRE-SCREENING GATE =================
def pre_screen_filing(source, title, summary, pdf_text, screener_info):
    prompt = f"""
You are the Managing Editor of a respected Indian financial newspaper.
Decide STRICTLY whether this official NSE/BSE/SEBI filing is genuine, material, high-impact stock market news worthy of a full research report.

SOURCE: {source}
TITLE: {title}
SUMMARY: {summary}
SCREENER MARKET CAP DATA: {json.dumps(screener_info)}
PDF EXCERPT:
{pdf_text[:3200] if pdf_text else "No PDF extracted"}

STRICT REJECTION RULES (REJECT WITHOUT HESITATION):
1. REJECT ROUTINE / MINOR FINES: Reject NSE/BSE fines for independent director vacancy, board composition (LODR Reg 17/18/19/20/21), or any minor compliance penalty below ₹5 Crore (or < 0.05% of market cap).
2. REJECT TRIVIAL / PERCHURAN FILINGS: Reject routine/tiny insider trades (below ₹1 Crore value or routine ESOP exercise), generic boilerplate annual report/BRSR links with no concrete financial highlights extracted, routine board meetings without dividend/result/bonus/split/buyback/fund-raise agenda, routine compliance notices, or filings lacking real material numbers.
3. LARGE CAP, MID CAP, OR SMALL CAP ONLY:
   - If Market Cap < ₹500 Crore -> REJECT (Micro Cap / SME).
   - If Source is SEBI Update, NSE Circular, or BSE Sensex Update -> approve ONLY if it is a major market-moving circular, rule, or index development affecting Indian retail/equity investors (`cap_category = "Market Wide"`).
   - For all individual stock filings: ONLY approve established Large Cap, Mid Cap, or Small Cap (>=₹500 Cr) companies.
4. APPROVE ONLY HIGH-IMPACT NEWS:
   - Quarterly/Annual Financial Results (Q1/Q2/Q3/Q4) with real numbers
   - Material Order Wins / Contracts / LOA with meaningful value
   - Dividend, Bonus, Stock Split, Daily Buyback progress/announcement
   - Significant Insider Trading / Promoter Buying or Selling (>= ₹1 Crore value)
   - High-impact Board Meetings (for results, dividend, bonus, split, buyback, fund raise)
   - Notable Shareholding Pattern shifts (FII/DII/Promoter stake change) or Annual Report/BRSR with clear concrete numbers
   - Merger, Acquisition, Demerger, Major Capex/Expansion, USFDA Approval, QIP/Rights Issue, Major Official SEBI/NSE Circular or Sensex update.

Return ONLY valid JSON:
{{
  "is_eligible": true or false,
  "reject_reason": "Clear short reason if false, else empty",
  "company_name": "Official English Company Name (or 'SEBI' / 'NSE' / 'BSE Sensex' for market-wide updates)",
  "short_company_name": "Clean short display name for thumbnail, max 20 chars (e.g., 'Tata Motors', 'SEBI Update', 'BSE Sensex')",
  "cap_category": "Large Cap or Mid Cap or Small Cap or Market Wide or Micro Cap",
  "event_type": "RESULT or ORDER or BUYBACK or INSIDER_TRADING or CORPORATE_ACTION or BOARD_MEETING or SHAREHOLDING or ANNUAL_REPORT or BRSR_ESG or M_AND_A or REGULATORY or MARKET_INDEX or IMPORTANT_UPDATE",
  "material_sentiment": "POSITIVE or NEGATIVE or NEUTRAL"
}}
"""
    raw = call_gemini_json(prompt, temperature=0.1, models=SCREEN_MODELS)
    return json.loads(raw)

# ================= CLEAN EDITORIAL THUMBNAIL ENGINE (ZERO TEXT OVERFLOW) =================
def remove_white_bg(img):
    img = img.convert("RGBA")
    data = np.array(img)
    r, g, b = data[:, :, 0], data[:, :, 1], data[:, :, 2]
    data[(r > 235) & (g > 235) & (b > 235), 3] = 0
    return Image.fromarray(data)

def add_sticker_outline(img, border_size=6, border_color=(255, 255, 255, 255)):
    alpha = img.split()[3]
    dilated = alpha.filter(ImageFilter.MaxFilter(border_size * 2 + 1))
    outline = Image.new("RGBA", img.size, border_color)
    outline.putalpha(dilated)
    return Image.alpha_composite(outline, img)

def load_font(size):
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "arialbd.ttf",
    ]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()

def wrap_lines_to_pixel_width(draw, text, font, max_width):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines

def fit_single_or_two_line_title(draw, text, max_width, start_size=50, min_size=30, max_lines=2):
    for sz in range(start_size, min_size - 1, -2):
        fnt = load_font(sz)
        lines = wrap_lines_to_pixel_width(draw, text, fnt, max_width)
        if len(lines) <= max_lines and all((draw.textbbox((0, 0), l, font=fnt)[2] - draw.textbbox((0, 0), l, font=fnt)[0]) <= max_width for l in lines):
            return fnt, lines
    fnt = load_font(min_size)
    return fnt, wrap_lines_to_pixel_width(draw, text, fnt, max_width)[:max_lines]

def create_and_push_thumbnail(short_company_name, headline_short, sentiment, cap_category, source_name, file_hash):
    W, H = 1280, 720
    base = Image.new("RGBA", (W, H), (245, 247, 250, 255))
    draw = ImageDraw.Draw(base)

    if sentiment == "POSITIVE":
        accent, glow, badge_text = (15, 118, 110, 255), (20, 184, 166, 60), "MARKET POSITIVE"
    elif sentiment == "NEGATIVE":
        accent, glow, badge_text = (185, 28, 28, 255), (239, 68, 68, 55), "KEY ALERT"
    else:
        accent, glow, badge_text = (29, 78, 216, 255), (59, 130, 246, 55), "STOCK UPDATE"

    draw.ellipse([700, 60, 1250, 610], fill=glow)

    CARD_L, CARD_T, CARD_R, CARD_B = 42, 42, 682, 678
    TEXT_X = 78
    MAX_TEXT_W = (CARD_R - 36) - TEXT_X

    draw.rounded_rectangle([CARD_L, CARD_T, CARD_R, CARD_B], radius=28, fill=(255, 255, 255, 252), outline=accent, width=5)

    font_pill = load_font(24)
    top_tag = f"{cap_category.upper()}  •  {source_name.split()[0].upper()}"
    tb1 = draw.textbbox((0, 0), top_tag, font=font_pill)
    pill_w = min(MAX_TEXT_W, (tb1[2] - tb1[0]) + 44)
    draw.rounded_rectangle([TEXT_X, 78, TEXT_X + pill_w, 126], radius=12, fill=(15, 23, 42, 255))
    draw.text((TEXT_X + 22, 89), top_tag, fill=(255, 255, 255, 255), font=font_pill)

    font_badge = load_font(26)
    tb2 = draw.textbbox((0, 0), badge_text, font=font_badge)
    badge_w = min(MAX_TEXT_W, (tb2[2] - tb2[0]) + 48)
    draw.rounded_rectangle([TEXT_X, 146, TEXT_X + badge_w, 200], radius=12, fill=accent)
    draw.text((TEXT_X + 24, 159), badge_text, fill=(255, 255, 255, 255), font=font_badge)

    clean_comp = " ".join(re.sub(r"[^\x20-\x7E&]", "", short_company_name).split()).upper()
    font_comp, comp_lines = fit_single_or_two_line_title(draw, clean_comp, MAX_TEXT_W, start_size=52, min_size=32, max_lines=2)
    y_cur = 232
    for cl in comp_lines:
        draw.text((TEXT_X, y_cur), cl, fill=(15, 23, 42, 255), font=font_comp)
        cb = draw.textbbox((0, 0), cl, font=font_comp)
        y_cur += (cb[3] - cb[1]) + 14

    y_cur += 10
    draw.rectangle([TEXT_X, y_cur, TEXT_X + 120, y_cur + 6], fill=accent)
    y_cur += 26

    clean_hl = " ".join(re.sub(r"[^\x20-\x7E₹]", "", headline_short).split())
    font_hl, hl_lines = fit_single_or_two_line_title(draw, clean_hl, MAX_TEXT_W, start_size=38, min_size=26, max_lines=3)
    for hl in hl_lines:
        draw.text((TEXT_X, y_cur), hl, fill=(51, 65, 85, 255), font=font_hl)
        hb = draw.textbbox((0, 0), hl, font=font_hl)
        y_cur += (hb[3] - hb[1]) + 16

    font_meta = load_font(22)
    date_str = get_ist_now().strftime("%d %b %Y").upper()
    draw.text((TEXT_X, CARD_B - 62), f"EQUITY RESEARCH DESK   •   {date_str}", fill=(100, 116, 139, 255), font=font_meta)

    draw.rounded_rectangle([715, 610, 1238, 678], radius=20, fill=(15, 23, 42, 255))
    font_brand = load_font(30)
    bb = draw.textbbox((0, 0), "MARKET MUNAFA", font=font_brand)
    draw.text((715 + (523 - (bb[2] - bb[0])) // 2, 628), "MARKET MUNAFA", fill=(255, 255, 255, 255), font=font_brand)

    if os.path.exists(AUTHOR_IMG_PATH):
        author = Image.open(AUTHOR_IMG_PATH)
        author = remove_white_bg(author)
        bbox = author.getbbox()
        if bbox:
            author = author.crop(bbox)
        author.thumbnail((510, 520), Image.Resampling.LANCZOS)
        author = add_sticker_outline(author, border_size=6)
        shadow = Image.new("RGBA", author.size, (0, 0, 0, 80))
        shadow.putalpha(author.split()[3])
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        pos_x = 715 + (523 - author.width) // 2
        pos_y = 612 - author.height
        base.paste(shadow, (pos_x + 8, pos_y + 8), shadow)
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

# ================= SEPARATE ORGANIC MULTILINGUAL PIPELINE =================
def sanitize_non_default_headings(html_str):
    return re.sub(
        r"<h2[^>]*>(.*?)</h2>",
        r'<div style="font-size:20px; font-weight:800; color:#0f172a; margin:22px 0 10px; border-left:4px solid #1d4ed8; padding-left:10px;">\1</div>',
        html_str,
        flags=re.IGNORECASE | re.DOTALL,
    )

def get_event_specific_instructions(event_type):
    if event_type == "RESULT":
        return """
MANDATORY RESULT DETAILS:
- Include a clean HTML <table> with exact verified numbers from the PDF: Revenue from Operations (₹ Cr/Lakh) + YoY% & QoQ%, Net Profit/PAT + YoY% & QoQ%, EBITDA & Margin% (if disclosed), EPS (₹), Dividend (if any), and Consolidated/Standalone basis.
- Explain real operational drivers from the filing. If any metric is missing in the PDF, write 'Not Disclosed' — never guess.
"""
    if event_type == "ORDER":
        return """
MANDATORY ORDER / CONTRACT DETAILS:
- Include a clean HTML <table> with exact Regulation 30 points from the PDF: Exact Order Value (₹ Cr/Lakh/USD), Client / Awarding Authority, Domestic or International, Scope of Work, Execution Timeline, and Related-Party Disclosure (Yes/No).
"""
    if event_type == "INSIDER_TRADING":
        return """
MANDATORY INSIDER TRADING DETAILS:
- Include a clean HTML <table> with exact verified filing details: Name & Category of Person (Promoter / Director / KMP), Transaction Type (Market Purchase / Disposal), Number of Shares, Transaction Value (₹ Cr/Lakh), Transaction Mode, and Pre vs Post-Transaction Shareholding %.
"""
    if event_type == "BUYBACK":
        return """
MANDATORY BUYBACK DETAILS:
- Include a clean HTML <table> summarizing: Buyback Route (Tender Offer / Open Market), Shares Bought Back (Daily & Cumulative), Buyback Price / Average Price (₹), Total Outlay / Utilization (₹ Cr), and Percentage of Total Buyback Completed (only verified numbers from filing).
"""
    return """
MANDATORY FILING DETAILS:
- Include a clean HTML <table> summarizing key parameters (Entity/Company, Category, Key Financial/Corporate Action/Regulatory Metric, Effective/Record/Meeting Date, Key Terms) strictly from the official filing/document.
"""

def step1_write_organic_english(filing_context, company_name, short_company_name, cap_category, event_type, sentiment):
    event_rules = get_event_specific_instructions(event_type)
    prompt = f"""
You are a senior Mumbai equity markets journalist (writing in the crisp, calm, authoritative style of Mint or Business Standard) for 'Market Munafa'.
Write a 100% ORIGINAL, copyright-free, completely natural human-sounding financial news article in Indian English based ONLY on the verified official filing below.

{filing_context}
{event_rules}

ORGANIC JOURNALISM & GOOGLE #1 SEO RULES:
1. SOUND 100% HUMAN & ORGANIC: Write like a real business reporter—direct sentences, specific facts, clear context, zero hype, zero fluff, zero dramatic exclamation marks.
2. STRICTLY BANNED AI PHRASES: Never use 'delve', 'tapestry', 'in conclusion', 'game-changer', 'testament', 'ever-evolving', 'rollercoaster', 'pivotal moment', 'underscores', 'landscape', 'it is worth noting', 'all eyes are on', 'only time will tell'.
3. WORD COUNT FOR GOOGLE NEWS & ADSENSE: Write 600 to 780 words of genuine substance in `english_html`.
4. ZERO COPYRIGHT OVERLAP: Never copy sentences verbatim from the PDF or RSS summary. Express facts in completely fresh reporting prose.
5. STRUCTURE OF `english_html`:
   - 2 tight opening lead paragraphs (who, what, exact rupee/percentage numbers, why it matters).
   - Clean HTML `<table>` of verified filing details.
   - 3 clear `<h2>` sections: (1) Breakdown of the Announcement, (2) Business & Sector Context, (3) Key Points for Shareholders/Investors (balanced, neutral — NO buy/sell advice).
   - 3 natural reader FAQs inside the article + matching `faqs_schema` array.
6. CLEAN HTML ONLY (`<h2>`, `<h3>`, `<p>`, `<table>`, `<thead>`, `<tbody>`, `<tr>`, `<th>`, `<td>`, `<ul>`, `<li>`, `<strong>`). Zero markdown (`**` or `##`).

Return ONLY valid JSON:
{{
  "seo_title_en": "Natural keyword-first Google News headline with key number (56-74 chars)",
  "meta_description": "Crisp 145-155 char English meta summary",
  "company_name": "{company_name}",
  "short_company_name": "{short_company_name}",
  "thumb_headline": "Short crisp 4-6 word English headline for thumbnail",
  "sentiment": "{sentiment}",
  "tags": ["Stock Market", "{cap_category}", "{short_company_name}", "NSE", "BSE"],
  "impact_en": "1 calm, concrete sentence explaining the real investor takeaway.",
  "quick_story_en": ["Fact-rich crisp bullet 1", "Bullet 2", "Bullet 3", "Bullet 4"],
  "faqs_schema": [
    {{"q": "Natural search question 1?", "a": "Direct 2-sentence factual answer."}},
    {{"q": "Natural search question 2?", "a": "Direct 2-sentence factual answer."}},
    {{"q": "Natural search question 3?", "a": "Direct 2-sentence factual answer."}}
  ],
  "english_html": "Complete 600-780 word organic Indian English HTML article."
}}
"""
    raw = call_gemini_json(prompt, temperature=0.35, models=WRITE_MODELS)
    return json.loads(raw)

def step2_write_native_language(en_data, target_lang_name, lang_code, company_name, short_company_name):
    style_ref = (
        "Divya Bhaskar Business / Gujarat Samachar Business (natural everyday Gujarati used by Gujarati investors today, never textbook or Sanskritized)"
        if lang_code == "gu"
        else "Mint Hindi / Economic Times Hindi (natural fluent Hindi used by Indian market readers today, never awkward translated phrases like 'विकास ऑर्डर')"
    )
    prompt = f"""
You are a veteran native {target_lang_name} stock market journalist writing in the warm, crisp, natural newspaper style of {style_ref}.
Rewrite the verified English stock report below into 100% authentic, fluent, human-sounding {target_lang_name}.

CRITICAL SCRIPT & ACCURACY RULES (NON-NEGOTIABLE):
1. WRITE EVERY SINGLE SENTENCE IN {target_lang_name} SCRIPT: `impact_{lang_code}`, EVERY bullet in `quick_story_{lang_code}`, and ALL paragraphs, headings, table labels, and FAQs in `{target_lang_name.lower()}_html` MUST be written in natural {target_lang_name} script — NEVER leave full sentences in English!
2. PRESERVE ENGLISH DIGITS & COMPANY/SHARE NAMES ONLY:
   - Keep ALL numbers strictly in English Arabic digits (`0-9`, e.g., `₹450 Crore`, `14.5%`, `Q2 FY27`).
   - Keep ALL company names, stock names, and standard market acronyms strictly in English Latin letters (`{company_name}`, `{short_company_name}`, `NSE`, `BSE`, `SEBI`, `PAT`, `EBITDA`, `EPS`, `IPO`, `LOA`, `BRSR`).
3. KEEP 100% OF FACTS, NUMBERS & HTML TABLE: Include the complete verified HTML `<table>` and all 3 `<h2>` sections + 3 FAQs (600-780 words). Do not shorten, summarize, or invent any number.
4. CLEAN HTML ONLY: Use `<h2>`, `<h3>`, `<p>`, `<table>`, `<thead>`, `<tbody>`, `<tr>`, `<th>`, `<td>`, `<ul>`, `<li>`, `<strong>`. Zero markdown (`**` or `##`).

VERIFIED ENGLISH REPORT TO ADAPT NATIVELY:
{json.dumps({
    "impact_en": en_data["impact_en"],
    "quick_story_en": en_data["quick_story_en"],
    "english_html": en_data["english_html"]
}, ensure_ascii=False)}

Return ONLY valid JSON with these exact 3 keys:
{{
  "impact_{lang_code}": "1 crisp natural {target_lang_name} sentence on investor impact (with English stock name & 0-9 digits)",
  "quick_story_{lang_code}": ["Natural {target_lang_name} bullet 1", "Bullet 2", "Bullet 3", "Bullet 4"],
  "{target_lang_name.lower()}_html": "Complete 600-780 word organic {target_lang_name} HTML article with full data table, H2 sections, and 3 FAQs."
}}
"""
    raw = call_gemini_json(prompt, temperature=0.3, models=WRITE_MODELS)
    return enforce_english_digits(json.loads(raw))

def step3_editorial_audit_and_fix(bundle, filing_context, python_failures):
    failure_block = (
        "CRITICAL HARD ERRORS DETECTED BY AUTOMATED CODE GATE (MUST FIX ALL 100%):\n- " + "\n- ".join(python_failures)
        if python_failures else "Automated Python Gate passed 100/100! Now do a final strict factual, grammar, and human-readability audit."
    )

    prompt = f"""
You are the Strict Chief Editor & Fact-Checker of 'Market Munafa'.
Your job is to inspect this complete trilingual article JSON against the Official Exchange Filing and FIX EVERY SINGLE ERROR.

{filing_context}

{failure_block}

STRICT 100-POINT EDITORIAL AUDIT CHECKLIST:
1. FACTUAL PERFECTION: Cross-check every single ₹ figure, percentage, YoY/QoQ growth, client name, execution period, and Standalone/Consolidated basis against the official PDF. Zero hallucination, zero wrong math.
2. LANGUAGE PURITY & ORGANIC HUMAN FLOW:
   - `impact_gu`, ALL 4 bullets of `quick_story_gu`, and `gujarati_html` MUST be 100% fluent contemporary Gujarati script (only company/share names and 0-9 digits in English).
   - `impact_hi`, ALL 4 bullets of `quick_story_hi`, and `hindi_html` MUST be 100% fluent contemporary Hindi Devanagari script (only company/share names and 0-9 digits in English).
   - `english_html` must read like a calm, natural Mumbai business journalist (zero AI clichés, zero hype).
3. ZERO COPYRIGHT OVERLAP: Rewrite any sentence echoing the PDF/summary verbatim.
4. GOOGLE NEWS & ADSENSE COMPLIANCE: Neutral E-E-A-T analysis, zero buy/sell tips, clean HTML tables in all 3 languages, zero markdown (`**`, `##`).
5. HONEST SCORING: Only give `"editor_score": 100` if EVERY single check above is genuinely flawless AFTER your fixes. Otherwise give the honest lower score and list remaining `"issues_found"`.

Return ONLY valid JSON containing ALL keys of the input bundle PLUS `"editor_score"` (int 0-100) and `"issues_found"` (list).

CURRENT ARTICLE BUNDLE:
{json.dumps(bundle, ensure_ascii=False)}
"""
    raw = call_gemini_json(prompt, temperature=0.1, models=WRITE_MODELS)
    return enforce_english_digits(json.loads(raw))

def generate_and_verify_200_percent(source, title, summary, pdf_text, company_name, short_company_name, cap_category, mcap_cr, event_type, sentiment):
    mcap_note = f"₹{mcap_cr:,.0f} Crore ({cap_category})" if mcap_cr else cap_category
    today_str = get_ist_now().strftime("%B %d, %Y")
    filing_context = f"""
Date: {today_str}
Source: {source}
Company: {company_name} (Short Name: {short_company_name})
Market Cap: {mcap_note}
Event Type: {event_type}
RSS Title: {title}
RSS Summary: {summary}
Official PDF/Document Full Text:
{pdf_text if pdf_text else "Document text unavailable; strictly rely on verified RSS facts and mark undisclosed figures clearly."}
"""

    print("   ✍️ Step 1/3: Writing organic English editorial report...")
    en_data = step1_write_organic_english(filing_context, company_name, short_company_name, cap_category, event_type, sentiment)
    time.sleep(1.5)

    print("   ✍️ Step 2/3: Writing native Gujarati editorial report...")
    gu_data = step2_write_native_language(en_data, "Gujarati", "gu", company_name, short_company_name)
    time.sleep(1.5)

    print("   ✍️ Step 3/3: Writing native Hindi editorial report...")
    hi_data = step2_write_native_language(en_data, "Hindi", "hi", company_name, short_company_name)
    time.sleep(1.5)

    bundle = {**en_data, **gu_data, **hi_data}
    bundle = enforce_english_digits(bundle)
    source_blob = f"{summary}\n{pdf_text}"

    for rnd in range(1, MAX_FIX_ROUNDS + 1):
        py_score, py_failures = run_hard_python_gate(bundle, source_blob)
        print(f"   🔎 Audit Round {rnd}/{MAX_FIX_ROUNDS} | Pre-Fix Python Gate: {py_score}/100 | Failures: {len(py_failures)}")

        audited = step3_editorial_audit_and_fix(bundle, filing_context, py_failures)
        editor_score = int(audited.get("editor_score", 0))
        issues_found = audited.get("issues_found", [])

        post_py_score, post_py_failures = run_hard_python_gate(audited, source_blob)
        total_score = post_py_score + editor_score
        print(f"   📊 Round {rnd} Result -> Python: {post_py_score}/100 + Editor: {editor_score}/100 = {total_score}/200")
        if post_py_failures:
            print(f"      ❌ Remaining Python Errors: {post_py_failures[:3]}")
        if issues_found:
            print(f"      📝 Editor Notes: {issues_found[:3]}")

        if total_score >= REQUIRED_TOTAL_SCORE and post_py_score == 100 and editor_score == 100 and not post_py_failures:
            print("   ✅ PASSED 200/200 ZERO-ERROR ORGANIC QUALITY GATE!")
            audited["hindi_html"] = sanitize_non_default_headings(audited["hindi_html"])
            audited["english_html"] = sanitize_non_default_headings(audited["english_html"])
            return audited

        bundle = {k: v for k, v in audited.items() if k not in ("editor_score", "issues_found")}
        time.sleep(2)

    raise ValueError(f"Article strictly REJECTED: Failed to achieve {REQUIRED_TOTAL_SCORE}/200 zero-defect score after {MAX_FIX_ROUNDS} rounds.")

# ================= CLEAN NEWSPAPER-STYLE BLOGGER HTML BUILDER =================
def render_quick_story_box(points, heading, accent_color):
    items_html = "".join(
        f"<li style='margin-bottom:9px; padding-left:4px;'>{pt}</li>"
        for pt in points
    )
    return f"""
    <div style="background:#f8fafc; border:1px solid #e2e8f0; border-left:4px solid {accent_color}; border-radius:8px; padding:16px 20px; margin-bottom:22px;">
      <div style="font-size:16.5px; font-weight:800; color:#0f172a; margin-bottom:10px; letter-spacing:0.2px;">{heading}</div>
      <ul style="margin:0; padding-left:20px; font-size:15px; color:#1e293b; line-height:1.75;">{items_html}</ul>
    </div>
    """

def render_impact_box(label, text, accent_color):
    return f"""
    <div style="background:#ffffff; border:1px solid #e2e8f0; border-left:4px solid {accent_color}; padding:13px 16px; border-radius:6px; margin-bottom:18px; font-size:15px; color:#1e293b;">
      <strong style="color:#0f172a;">{label}:</strong> {text}
    </div>
    """

def build_blogger_html(data, thumb_url, source_name, cap_category, mcap_cr, official_link, related_posts, uid_short):
    sentiment = data.get("sentiment", "NEUTRAL").upper()
    if sentiment == "POSITIVE":
        accent_color, sent_text = "#0f766e", "Positive"
    elif sentiment == "NEGATIVE":
        accent_color, sent_text = "#b91c1c", "Cautious / Negative"
    else:
        accent_color, sent_text = "#1d4ed8", "Neutral / Informational"

    now_ist = get_ist_now()
    now_iso = now_ist.isoformat()
    ist_display = now_ist.strftime("%B %d, %Y, %I:%M %p IST")
    mcap_text = f"{cap_category} (₹{mcap_cr:,.0f} Cr)" if mcap_cr else cap_category

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
        <div style="margin-top:26px; padding:16px 18px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px;">
          <div style="font-size:16.5px; font-weight:800; color:#0f172a; margin-bottom:10px;">Also Read on Market Munafa</div>
          <ul style="margin:0; padding-left:20px; line-height:1.7;">{links_li}</ul>
        </div>
        """

    return f"""
<script type="application/ld+json">{news_schema}</script>
{faq_schema_tag}

<article id="mm-post-{uid_short}" style="font-family: 'Noto Sans Gujarati', 'Noto Sans Devanagari', 'Segoe UI', Roboto, Arial, sans-serif; line-height: 1.85; color: #1e293b;">
  <style>
    #mm-post-{uid_short} table {{ width:100%; border-collapse:collapse; margin:18px 0 22px 0; font-size:14.5px; }}
    #mm-post-{uid_short} th {{ background:#0f172a; color:#ffffff; padding:10px 12px; border:1px solid #cbd5e1; text-align:left; font-weight:600; }}
    #mm-post-{uid_short} td {{ padding:10px 12px; border:1px solid #cbd5e1; background:#ffffff; }}
    #mm-post-{uid_short} tr:nth-child(even) td {{ background:#f8fafc; }}
    #mm-post-{uid_short} h2 {{ color:#0f172a; font-size:20px; margin:24px 0 10px 0; border-left:4px solid {accent_color}; padding-left:10px; line-height:1.4; }}
  </style>

  <figure style="text-align:center; margin:0 0 16px 0;">
    <img src="{thumb_url}" alt="{data['seo_title_en']}" title="{data['seo_title_en']}" width="1280" height="720" style="width:100%; max-width:820px; height:auto; border-radius:12px; box-shadow:0 4px 16px rgba(15,23,42,0.10);" />
  </figure>

  <div style="display:flex; flex-wrap:wrap; gap:8px; justify-content:center; align-items:center; font-size:13.5px; color:#475569; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px 14px; margin-bottom:14px;">
    <span><strong>Source:</strong> {source_name}</span>
    <span>•</span>
    <span><strong>Category:</strong> {mcap_text}</span>
    <span>•</span>
    <span><strong>Outlook:</strong> <span style="color:{accent_color}; font-weight:700;">{sent_text}</span></span>
  </div>

  <p style="font-size:13px; color:#64748b; text-align:center; margin:0 0 16px 0;">
    Updated: {ist_display} &nbsp;|&nbsp; Market Munafa Equity Research Desk
  </p>

  <div class="mm-langbar" style="display:flex; gap:10px; justify-content:center; margin-bottom:22px; flex-wrap:wrap;">
    <button type="button" class="mm-lbtn" onclick="switchLang_{uid_short}('gu')" id="btn-{uid_short}-gu" style="padding:9px 22px; border:1px solid #0f172a; border-radius:6px; background:#0f172a; color:#fff; font-weight:700; cursor:pointer;">ગુજરાતી</button>
    <button type="button" class="mm-lbtn" onclick="switchLang_{uid_short}('hi')" id="btn-{uid_short}-hi" style="padding:9px 22px; border:1px solid #cbd5e1; border-radius:6px; background:#ffffff; color:#0f172a; font-weight:700; cursor:pointer;">हिन्दी</button>
    <button type="button" class="mm-lbtn" onclick="switchLang_{uid_short}('en')" id="btn-{uid_short}-en" style="padding:9px 22px; border:1px solid #cbd5e1; border-radius:6px; background:#ffffff; color:#0f172a; font-weight:700; cursor:pointer;">English</button>
  </div>

  <div id="lang-{uid_short}-gu" class="mm-lang-pane" data-lang="gu" style="display:block;">
    {render_impact_box("રોકાણકારો માટે મુખ્ય અસર", data.get("impact_gu", ""), accent_color)}
    {render_quick_story_box(data.get("quick_story_gu", []), "એક નજરમાં મુખ્ય બાબતો", accent_color)}
    {data['gujarati_html']}
  </div>

  <div id="lang-{uid_short}-hi" class="mm-lang-pane" data-lang="hi" style="display:none;">
    {render_impact_box("निवेशकों के लिए मुख्य असर", data.get("impact_hi", ""), accent_color)}
    {render_quick_story_box(data.get("quick_story_hi", []), "एक नज़र में मुख्य बातें", accent_color)}
    {data['hindi_html']}
  </div>

  <div id="lang-{uid_short}-en" class="mm-lang-pane" data-lang="en" style="display:none;">
    {render_impact_box("Key Takeaway for Investors", data.get("impact_en", ""), accent_color)}
    {render_quick_story_box(data.get("quick_story_en", []), "Key Highlights at a Glance", accent_color)}
    {data['english_html']}
  </div>

  {related_html}

  <hr style="margin:24px 0; border:0; border-top:1px solid #e2e8f0;" />
  <div style="background:#f8fafc; padding:14px 16px; border-radius:8px; border:1px solid #e2e8f0; font-size:13px; color:#475569; line-height:1.65;">
    <strong>Official Reference:</strong> <a href="{official_link}" target="_blank" rel="nofollow noopener">{source_name} Document</a><br/>
    <strong>Disclaimer:</strong> આ અહેવાલ સત્તાવાર એક્સચેન્જ ફાઈલિંગ (NSE/BSE/SEBI) પર આધારિત માત્ર માહિતી અને શૈક્ષણિક હેતુ માટે છે. આ કોઈ રોકાણની સલાહ નથી; કોઈ પણ નિર્ણય લેતા પહેલા SEBI રજિસ્ટર્ડ નાણાકીય સલાહકારની સલાહ લો.
  </div>
</article>

<script>
function switchLang_{uid_short}(lang) {{
  ['gu', 'hi', 'en'].forEach(function(l) {{
    var pane = document.getElementById('lang-{uid_short}-' + l);
    var btn = document.getElementById('btn-{uid_short}-' + l);
    if (pane) pane.style.display = (l === lang) ? 'block' : 'none';
    if (btn) {{
      btn.style.background = (l === lang) ? '#0f172a' : '#ffffff';
      btn.style.color = (l === lang) ? '#ffffff' : '#0f172a';
      btn.style.borderColor = (l === lang) ? '#0f172a' : '#cbd5e1';
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
        print(f"📡 Scanning today's official filings from: {feed_info['name']}...")
        parsed = fetch_official_feed(feed_info)
        if not parsed:
            continue

        for entry in parsed.entries[:150]:
            if not is_today_ist(entry):
                continue
            title = strip_html(entry.get("title", "")).strip()
            link = entry.get("link", "").strip()
            summary = strip_html(entry.get("summary", "") or entry.get("description", ""))

            if not title or not link or not is_important_news(title, summary, feed_info["name"]):
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

    all_today_candidates.sort(key=lambda x: x["dt"], reverse=True)
    print(f"🔎 Found {len(all_today_candidates)} today's official candidate filings across all {len(FEEDS)} feeds.")

    for item in all_today_candidates:
        if published_count >= MAX_POSTS_PER_RUN:
            print(f"ℹ️ Reached per-run limit ({MAX_POSTS_PER_RUN}).")
            break

        title, link, summary, source, uid = item["title"], item["link"], item["summary"], item["source"], item["uid"]
        is_market_wide = source in MARKET_WIDE_SOURCES
        raw_company = source if is_market_wide else extract_company_prefix_from_title(title)
        prelim_event = detect_event_type(title, summary, source=source)
        prelim_key = normalize_company_day_key(title[:45] if is_market_wide else raw_company, prelim_event)

        if uid in history_keys or prelim_key in history_keys:
            continue

        print(f"\n🔥 Checking [{source} | {item['dt'].strftime('%I:%M %p IST')}]: {title}")
        try:
            screener_info = {"found": False, "mcap_cr": None, "category": "Market Wide", "screener_name": source}
            if not is_market_wide:
                screener_info = get_market_cap_info(raw_company)
                if screener_info["found"] and screener_info["mcap_cr"] is not None:
                    print(f"   💹 Market Cap: ₹{screener_info['mcap_cr']:,.0f} Cr ({screener_info['category']})")
                    if screener_info["mcap_cr"] < MIN_MARKET_CAP_CR:
                        print(f"   ⏭️ Skipped Micro-Cap/SME (< ₹{MIN_MARKET_CAP_CR:.0f} Cr).")
                        history_keys.add(uid)
                        history["keys"].append(uid)
                        save_history(history)
                        continue

            pdf_text = extract_pdf_text(link)
            print(f"   📄 Extracted {len(pdf_text)} chars from official PDF/Document.")

            if any(bad in pdf_text[:2500].lower() for bad in ["shortfall of independent", "composition of the board", "composition of board", "regulation 17(1)"]):
                print("   ⏭️ Skipped routine board composition / independent director compliance fine.")
                history_keys.add(uid)
                history["keys"].append(uid)
                save_history(history)
                continue

            screen = pre_screen_filing(source, title, summary, pdf_text, screener_info)
            if not screen.get("is_eligible"):
                print(f"   ⏭️ Skipped by Materiality Gate: {screen.get('reject_reason', 'Not high impact')}")
                history_keys.add(uid)
                history["keys"].append(uid)
                save_history(history)
                continue

            company_name = screen.get("company_name") or screener_info.get("screener_name") or raw_company
            short_company_name = (screen.get("short_company_name") or company_name).strip()[:22]
            cap_category = "Market Wide" if is_market_wide else (
                screener_info["category"] if screener_info["found"] else screen.get("cap_category", "Small Cap")
            )
            if not is_market_wide and cap_category.lower() in ("micro cap", "sme", "unknown"):
                print(f"   ⏭️ Skipped ineligible cap category: {cap_category}")
                history_keys.add(uid)
                history["keys"].append(uid)
                save_history(history)
                continue

            event_type = screen.get("event_type") or detect_event_type(title, summary, pdf_text, source=source)
            sentiment = screen.get("material_sentiment", "NEUTRAL").upper()
            final_comp_key = normalize_company_day_key(title[:45] if is_market_wide else company_name, event_type)
            if final_comp_key in history_keys:
                print(f"   ⏭️ Already posted today ({final_comp_key}).")
                history_keys.add(uid)
                history["keys"].append(uid)
                save_history(history)
                continue

            article_data = generate_and_verify_200_percent(
                source, title, summary, pdf_text,
                company_name, short_company_name, cap_category,
                screener_info.get("mcap_cr"), event_type, sentiment
            )

            if is_duplicate_post(article_data["company_name"], article_data["seo_title_en"], recent_posts, history_titles, is_market_wide=is_market_wide):
                print(f"   ⏭️ Skipped duplicate already on Blogger: {article_data['company_name']}")
                history_keys.update([uid, final_comp_key])
                history["keys"].extend([uid, final_comp_key])
                save_history(history)
                continue

            thumb_url = create_and_push_thumbnail(
                article_data.get("short_company_name", short_company_name),
                article_data["thumb_headline"],
                article_data["sentiment"],
                cap_category,
                source,
                uid[:10],
            )

            raw_labels = list(article_data.get("tags", [])) + [cap_category, "MARKET NEWS"]
            if event_type == "RESULT":
                raw_labels.append("STOCK RESULT")
            elif event_type == "ORDER":
                raw_labels.append("SHARE ORDER")
            elif event_type == "BUYBACK":
                raw_labels.append("BUYBACK")
            elif event_type == "INSIDER_TRADING":
                raw_labels.append("INSIDER TRADING")
            elif source == "SEBI Update":
                raw_labels.append("SEBI UPDATE")

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

            body = {
                "kind": "blogger#post",
                "title": article_data["seo_title_en"],
                "content": post_html,
                "labels": clean_labels[:10],
            }
            post = blogger_service.posts().insert(blogId=os.environ["BLOG_ID"], body=body, isDraft=False).execute()
            post_url = post.get("url", "")
            print(f"   ✅ Published (200/200 Verified): {post_url}")

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

    print(f"\n🎉 Done. Total 200/200 verified official posts published in this run: {published_count}")

if __name__ == "__main__":
    main()
