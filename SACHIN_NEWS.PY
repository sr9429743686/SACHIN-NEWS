import os
import re
import io
import json
import time
import hashlib
import subprocess
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
MODEL_NAME = "gemini-3.5-flash-lite"
MIN_QUALITY_SCORE = 95
MAX_AUDIT_ROUNDS = 3
TIME_WINDOW_MINUTES = 15

FEEDS = [
    {"name": "NSE Announcement", "url": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"},
    {"name": "BSE Announcement", "url": "https://www.bseindia.com/data/xml/announcements.aspx"},
    {"name": "NSE Corporate Action", "url": "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"},
    {"name": "BSE Notice", "url": "https://www.bseindia.com/data/xml/notices.xml"},
    {"name": "SEBI Update", "url": "https://www.sebi.gov.in/sebirss.xml"},
]

IMPORTANT_KEYWORDS = [
    "dividend", "bonus", "split", "buyback", "merger", "acquisition", "order",
    "contract", "result", "profit", "revenue", "rights issue", "fund raising",
    "qip", "ipo", "delisting", "sebi", "penalty", "circular", "expansion",
    "joint venture", "agreement", "capex", "allotment", "demerger"
]

IGNORE_KEYWORDS = [
    "loss of share", "duplicate share", "trading window", "newspaper publication",
    "certificate under regulation 74", "reconciliation of share capital", "intimation of schedule"
]

HISTORY_FILE = "posted_history.json"
THUMBNAIL_DIR = "thumbnails"
AUTHOR_IMG_PATH = "author.png"

os.makedirs(THUMBNAIL_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/pdf,application/xml,text/xml,application/rss+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ================= HELPER FUNCTIONS =================
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_history(history_set):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(list(history_set)[-500:], f, indent=2)

def normalize_company_key(text):
    # NSE અને BSE બંને પરથી એક જ કંપનીના ડુપ્લિકેટ ન્યૂઝ રોકવા માટે
    clean = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    words = [w for w in clean.split() if w not in {"limited", "ltd", "india", "ind", "private", "pvt", "the", "of", "and"}]
    today_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
    return f"{today_str}|{'_'.join(words[:3])}"

def get_blogger_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["CLIENT_ID"],
        client_secret=os.environ["CLIENT_SECRET"],
    )
    return build("blogger", "v3", credentials=creds)

def get_recent_blogger_titles(service):
    try:
        res = service.posts().list(blogId=os.environ["BLOG_ID"], maxResults=25).execute()
        return [p.get("title", "").lower() for p in res.get("items", [])]
    except Exception as e:
        print(f"⚠️ Could not fetch recent Blogger posts: {e}")
        return []

def is_duplicate_on_blogger(company_name, recent_titles):
    comp_clean = re.sub(r"[^a-z0-9\s]", "", company_name.lower()).strip()
    comp_words = [w for w in comp_clean.split() if len(w) > 2][:2]
    if not comp_words:
        return False
    for t in recent_titles:
        if all(w in t for w in comp_words):
            return True
    return False

def extract_pdf_text(link):
    if not link:
        return ""
    try:
        resp = requests.get(link, headers=HEADERS, timeout=20)
        content_type = resp.headers.get("Content-Type", "").lower()
        if "pdf" in content_type or link.lower().endswith(".pdf"):
            reader = PdfReader(io.BytesIO(resp.content))
            extracted = []
            for page in reader.pages[:4]:  # પ્રથમ 4 પેજમાંથી પૂરી વિગત ખેંચશે
                txt = page.extract_text()
                if txt:
                    extracted.append(txt.strip())
            full_text = "\n".join(extracted)
            return full_text[:7000]  # Gemini માટે ક્લીન લિમિટ
    except Exception as e:
        print(f"⚠️ PDF extraction skipped/failed for {link}: {e}")
    return ""

def call_gemini_json(prompt, temperature=0.2):
    keys = [os.environ.get("GEMINI_API_KEY_1"), os.environ.get("GEMINI_API_KEY_2")]
    keys = [k for k in keys if k]
    if not keys:
        raise ValueError("No GEMINI_API_KEY found.")
    last_err = None
    for key in keys:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=temperature,
                ),
            )
            return response.text
        except Exception as e:
            print(f"Gemini key error, trying next: {e}")
            last_err = e
    raise RuntimeError(f"All Gemini keys failed: {last_err}")

def is_within_time_window(entry):
    pub_str = entry.get("published") or entry.get("updated") or ""
    if not pub_str:
        return False
    try:
        dt = date_parser.parse(pub_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
        diff_minutes = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60.0
        return -2 <= diff_minutes <= TIME_WINDOW_MINUTES
    except Exception:
        return False

def is_important_news(title, summary):
    text = f"{title} {summary}".lower()
    if any(bad in text for bad in IGNORE_KEYWORDS):
        return False
    return any(good in text for good in IMPORTANT_KEYWORDS)

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

def create_and_push_thumbnail(company_name, headline_short, sentiment, source_name, file_hash):
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
    draw.rounded_rectangle([40, 40, 680, 680], radius=32, fill=(255, 255, 255, 242), outline=accent, width=5)
    draw.rounded_rectangle([710, 610, 1240, 680], radius=24, fill=(15, 23, 42, 255))

    try:
        font_badge = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
        font_comp = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 56)
        font_head = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 38)
        font_brand = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    except Exception:
        font_badge = font_comp = font_head = font_brand = ImageFont.load_default()

    draw.rounded_rectangle([80, 80, 460, 140], radius=14, fill=(15, 23, 42, 255))
    draw.text((105, 92), source_name.upper()[:18], fill=(255, 255, 255, 255), font=font_brand)

    draw.rounded_rectangle([80, 165, 480, 235], radius=18, fill=accent)
    draw.text((110, 180), badge_text, fill=(255, 255, 255, 255), font=font_badge)

    draw.text((80, 270), company_name[:20].upper(), fill=(15, 23, 42, 255), font=font_comp)

    words, lines, cur = headline_short.split(), [], ""
    for w in words:
        if len(cur + " " + w) <= 24:
            cur = (cur + " " + w).strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    y_text = 365
    for line in lines[:4]:
        draw.text((80, y_text), line, fill=(51, 65, 85, 255), font=font_head)
        y_text += 55

    draw.text((815, 628), "MARKET MUNAFA", fill=(255, 255, 255, 255), font=font_brand)

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
        time.sleep(3)
    except Exception as e:
        print(f"⚠️ Git push warning for thumbnail: {e}")

    repo = os.environ.get("GITHUB_REPO", "")
    return f"https://cdn.jsdelivr.net/gh/{repo}@main/{out_path}"

# ================= STRICT ITERATIVE QUALITY GATE =================
def sanitize_non_default_headings(html_str):
    return re.sub(
        r"<h2[^>]*>(.*?)</h2>",
        r'<div style="font-size:21px; font-weight:800; color:#0f172a; margin:20px 0 10px;">\1</div>',
        html_str,
        flags=re.IGNORECASE | re.DOTALL,
    )

def generate_and_strictly_verify(source, title, summary, pdf_text, link):
    print("📝 Generating Detailed Draft from Official Filing + PDF...")
    filing_context = f"""
Source: {source}
Title: {title}
RSS Summary: {summary}
Official PDF Full Text:
{pdf_text if pdf_text else "Not available in PDF; use RSS details only."}
"""
    draft_prompt = f"""
You are a senior Indian stock market financial journalist for 'Market Munafa' (Google News & AdSense approved).
Write a 100% original, deeply detailed, copyright-free news article using ALL concrete numbers and facts from the official PDF/filing below:

{filing_context}

STRICT RULES:
1. EXTRACT CONCRETE DETAILS: Always mention exact order value (₹ Crore/Lakh), client/awarding entity name, execution period, dividend ₹/share, record date, or exact SEBI penalty/order details from the PDF text! If a number is genuinely not disclosed in the PDF, explicitly state that the company has not disclosed the exact value.
2. NATURAL LANGUAGE: Write fluent contemporary Gujarati, Hindi, and Indian English (NEVER robotic translations like 'विकास ऑर्डर').
3. CLEAN HTML: Use <h2>, <h3>, <p>, <ul>, <li>, <strong>. Never use markdown (** or ##).

Return ONLY valid JSON with these exact keys:
{{
  "seo_title_en": "High-CTR Google News SEO Title in English with key number/order value if available (60-78 chars)",
  "meta_description": "Crisp 150-char English SEO meta summary with key figures",
  "company_name": "Short Company Name (max 18 chars)",
  "thumb_headline": "Short punchy 4-6 word English headline for thumbnail (include ₹ value if available)",
  "sentiment": "POSITIVE or NEGATIVE or NEUTRAL",
  "impact_gu": "1 crisp natural Gujarati sentence on investor impact",
  "impact_hi": "1 crisp natural Hindi sentence on investor impact",
  "impact_en": "1 crisp natural Indian English sentence on investor impact",
  "tags": ["Stock Market", "NSE", "BSE", "Company Name", "Share News"],
  "quick_story_gu": ["Point 1 with exact figures", "Point 2", "Point 3", "Point 4"],
  "quick_story_hi": ["Point 1 with exact figures", "Point 2", "Point 3", "Point 4"],
  "quick_story_en": ["Point 1 with exact figures", "Point 2", "Point 3", "Point 4"],
  "gujarati_html": "Detailed Gujarati HTML article with exact PDF figures: What Happened, Full Filing/Order Details (table or bullets), Investor Impact, and 2 FAQs.",
  "hindi_html": "Detailed Hindi HTML article with same rich structure and exact figures.",
  "english_html": "Detailed Indian English HTML article with same rich structure and exact figures."
}}
"""
    current_json_str = call_gemini_json(draft_prompt, temperature=0.25)

    for attempt in range(1, MAX_AUDIT_ROUNDS + 1):
        print(f"🔍 Quality Audit Round {attempt}/{MAX_AUDIT_ROUNDS}...")
        audit_prompt = f"""
You are the Ruthless Chief Editor of 'Market Munafa'. Audit this JSON article strictly against the Official Filing & PDF Text.

{filing_context}

CHECK ALL 5 CRITERIA:
1. FULL DETAILS INCLUDED: Did the article include the exact order amount, client name, execution timeline, or financial numbers from the PDF? If missing from the draft but present in the PDF, ADD THEM NOW!
2. ZERO HALLUCINATION: Remove any number not present in the filing/PDF.
3. NATURAL LANGUAGE: Fix any robotic Hindi/Gujarati phrasing.
4. CLEAN HTML: No markdown (** or ##), all HTML tags properly closed.
5. COMPLETE STRUCTURE: 4 crisp Quick Story points and Impact lines in all 3 languages.

Fix ANY issue, then assign an honest "quality_score" (0 to 100) and "issues_found" list.
Return ONLY valid JSON with all original keys PLUS "quality_score" (int) and "issues_found" (list).

ARTICLE JSON TO AUDIT:
{current_json_str}
"""
        audited_str = call_gemini_json(audit_prompt, temperature=0.1)
        data = json.loads(audited_str)
        score = int(data.get("quality_score", 0))
        print(f"📊 Round {attempt} Score: {score}/100 | Issues: {data.get('issues_found', [])}")

        has_markdown_bug = any(
            "**" in str(data.get(k, "")) or "##" in str(data.get(k, ""))
            for k in ["gujarati_html", "hindi_html", "english_html"]
        )
        all_langs_ok = all(
            len(str(data.get(k, ""))) >= 300
            for k in ["gujarati_html", "hindi_html", "english_html"]
        )

        if score >= MIN_QUALITY_SCORE and not has_markdown_bug and all_langs_ok:
            print(f"✅ PASSED Quality Gate with {score}/100!")
            data["hindi_html"] = sanitize_non_default_headings(data["hindi_html"])
            data["english_html"] = sanitize_non_default_headings(data["english_html"])
            return data

        current_json_str = audited_str
        time.sleep(2)

    raise ValueError(f"Article rejected: Failed to reach {MIN_QUALITY_SCORE}/100 after {MAX_AUDIT_ROUNDS} rounds.")

# ================= BLOGGER HTML BUILDER =================
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

def build_blogger_html(data, thumb_url, source_name, official_link):
    sentiment = data.get("sentiment", "NEUTRAL").upper()
    if sentiment == "POSITIVE":
        badge_bg, badge_label = "#16a34a", "📈 POSITIVE / BULLISH NEWS"
    elif sentiment == "NEGATIVE":
        badge_bg, badge_label = "#dc2626", "📉 NEGATIVE / BEARISH NEWS"
    else:
        badge_bg, badge_label = "#2563eb", "ℹ️ IMPORTANT MARKET UPDATE"

    now_iso = datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()

    schema_json = json.dumps({
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": data["seo_title_en"],
        "description": data.get("meta_description", data["seo_title_en"]),
        "image": [thumb_url],
        "datePublished": now_iso,
        "dateModified": now_iso,
        "author": {"@type": "Person", "name": "Sachin Patel"},
        "publisher": {
            "@type": "Organization",
            "name": "Market Munafa",
            "logo": {"@type": "ImageObject", "url": thumb_url}
        }
    }, ensure_ascii=False)

    return f"""
<script type="application/ld+json">{schema_json}</script>

<div style="font-family: 'Segoe UI', Roboto, Arial, sans-serif; line-height: 1.8; color: #1e293b;">
  <div style="text-align:center; margin-bottom:18px;">
    <img src="{thumb_url}" alt="{data['seo_title_en']}" width="1280" height="720" style="width:100%; max-width:800px; height:auto; border-radius:14px; box-shadow:0 8px 24px rgba(0,0,0,0.12);" />
  </div>

  <div style="background:{badge_bg}; color:#ffffff; padding:12px 18px; border-radius:10px; font-weight:bold; font-size:16px; margin-bottom:18px; text-align:center;">
    {badge_label} &nbsp;|&nbsp; Source: {source_name}
  </div>

  <div style="display:flex; gap:10px; justify-content:center; margin-bottom:22px; flex-wrap:wrap;">
    <button onclick="switchLang('gu')" id="btn-gu" style="padding:10px 22px; border:none; border-radius:8px; background:#0f172a; color:#fff; font-weight:bold; cursor:pointer;">ગુજરાતી</button>
    <button onclick="switchLang('hi')" id="btn-hi" style="padding:10px 22px; border:none; border-radius:8px; background:#e2e8f0; color:#0f172a; font-weight:bold; cursor:pointer;">हिन्दी</button>
    <button onclick="switchLang('en')" id="btn-en" style="padding:10px 22px; border:none; border-radius:8px; background:#e2e8f0; color:#0f172a; font-weight:bold; cursor:pointer;">English</button>
  </div>

  <div id="lang-gu" style="display:block;">
    {render_impact_box("રોકાણકારો માટે અસર", data.get("impact_gu", ""), badge_bg)}
    {render_quick_story_box(data.get("quick_story_gu", []), "ક્વિક સ્ટોરી — મુખ્ય મુદ્દાઓ", badge_bg)}
    {data['gujarati_html']}
  </div>

  <div id="lang-hi" style="display:none;">
    {render_impact_box("निवेशकों पर असर", data.get("impact_hi", ""), badge_bg)}
    {render_quick_story_box(data.get("quick_story_hi", []), "क्विक स्टोरी — मुख्य बातें", badge_bg)}
    {data['hindi_html']}
  </div>

  <div id="lang-en" style="display:none;">
    {render_impact_box("Impact on Investors", data.get("impact_en", ""), badge_bg)}
    {render_quick_story_box(data.get("quick_story_en", []), "Quick Story — Key Highlights", badge_bg)}
    {data['english_html']}
  </div>

  <hr style="margin:24px 0; border:0; border-top:1px solid #cbd5e1;" />
  <div style="background:#f1f5f9; padding:14px 16px; border-radius:10px; font-size:13px; color:#475569;">
    <strong>Official Exchange Reference:</strong> <a href="{official_link}" target="_blank" rel="nofollow noopener">View Official {source_name} Filing</a><br/>
    <strong>Editorial Note (Market Munafa):</strong> આ આર્ટિકલ સત્તાવાર એક્સચેન્જ ફાઈલિંગના આધારે તૈયાર કરવામાં આવ્યો છે. આ કોઈ રોકાણની સલાહ નથી; રોકાણ કરતા પહેલા તમારા SEBI રજિસ્ટર્ડ સલાહકારની સલાહ લો.
  </div>
</div>

<script>
function switchLang(lang) {{
  ['gu', 'hi', 'en'].forEach(function(l) {{
    document.getElementById('lang-' + l).style.display = (l === lang) ? 'block' : 'none';
    var btn = document.getElementById('btn-' + l);
    btn.style.background = (l === lang) ? '#0f172a' : '#e2e8f0';
    btn.style.color = (l === lang) ? '#ffffff' : '#0f172a';
  }});
}}
</script>
"""

# ================= MAIN EXECUTION =================
def main():
    history = load_history()
    blogger_service = get_blogger_service()
    recent_titles = get_recent_blogger_titles(blogger_service)
    published_count = 0

    for feed_info in FEEDS:
        print(f"Checking feed: {feed_info['name']}...")
        try:
            resp = requests.get(feed_info["url"], headers=HEADERS, timeout=20)
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"Failed to fetch {feed_info['name']}: {e}")
            continue

        for entry in parsed.entries[:25]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "") or entry.get("description", "")

            uid = hashlib.md5(f"{title}|{link}".encode()).hexdigest()
            company_day_key = normalize_company_key(title)

            if uid in history or company_day_key in history:
                continue
            if not is_within_time_window(entry) or not is_important_news(title, summary):
                continue

            print(f"🔥 Processing Important News: {title}")
            try:
                pdf_text = extract_pdf_text(link)
                print(f"📄 Extracted {len(pdf_text)} chars from official PDF.")

                article_data = generate_and_strictly_verify(feed_info["name"], title, summary, pdf_text, link)

                # બ્લોગર પર પહેલાંથી આ કંપનીની પોસ્ટ છે કે નહીં તેનો ફાઈનલ ચેક
                if is_duplicate_on_blogger(article_data["company_name"], recent_titles):
                    print(f"⏭️ Skipped duplicate company already on Blogger: {article_data['company_name']}")
                    history.add(uid)
                    history.add(company_day_key)
                    save_history(history)
                    continue

                thumb_url = create_and_push_thumbnail(
                    article_data["company_name"],
                    article_data["thumb_headline"],
                    article_data["sentiment"],
                    feed_info["name"],
                    uid[:10],
                )
                post_html = build_blogger_html(article_data, thumb_url, feed_info["name"], link)

                body = {
                    "kind": "blogger#post",
                    "title": article_data["seo_title_en"],
                    "content": post_html,
                    "labels": article_data.get("tags", ["Stock Market"])[:8],
                }
                post = blogger_service.posts().insert(blogId=os.environ["BLOG_ID"], body=body, isDraft=False).execute()
                print(f"✅ Published: {post.get('url')}")

                recent_titles.append(article_data["seo_title_en"].lower())
                history.add(uid)
                history.add(company_day_key)
                save_history(history)
                published_count += 1
                time.sleep(5)
            except Exception as e:
                print(f"❌ NOT PUBLISHED '{title}': {e}")

    print(f"Done. Total published in this run: {published_count}")

if __name__ == "__main__":
    main()
