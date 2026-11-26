import os
import re
import json
import time
import hashlib
import requests
import feedparser
import numpy as np
from datetime import datetime, timezone, timedelta
from dateutil import parser as date_parser
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ================= CONFIGURATION =================
FEEDS = [
    {"name": "BSE Announcement", "url": "https://www.bseindia.com/data/xml/announcements.aspx"},
    {"name": "BSE Notice", "url": "https://www.bseindia.com/data/xml/notices.xml"},
    {"name": "NSE Announcement", "url": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"},
    {"name": "NSE Corporate Action", "url": "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"},
    {"name": "SEBI Update", "url": "https://www.sebi.gov.in/sebirss.xml"},
]

# માત્ર મહત્વપૂર્ણ ન્યૂઝ માટેના કીવર્ડ્સ
IMPORTANT_KEYWORDS = [
    "dividend", "bonus", "split", "buyback", "merger", "acquisition", "order",
    "contract", "result", "profit", "revenue", "rights issue", "fund raising",
    "qip", "ipo", "delisting", "sebi", "penalty", "order", "circular",
    "expansion", "joint venture", "agreement", "capex", "allotment", "demerger"
]

# નકામી/સામાન્ય નોટિસ દૂર કરવા માટે
IGNORE_KEYWORDS = [
    "loss of share", "duplicate share", "trading window", "newspaper publication",
    "certificate under regulation 74", "reconciliation of share capital", "intimation of schedule"
]

HISTORY_FILE = "posted_history.json"
THUMBNAIL_DIR = "thumbnails"
AUTHOR_IMG_PATH = "author.png"
TIME_WINDOW_MINUTES = 15  # છેલ્લી 15 મિનિટના ન્યૂઝ

os.makedirs(THUMBNAIL_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/xml,text/xml,application/rss+xml,*/*;q=0.8",
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

def get_gemini_client():
    keys = [os.environ.get("GEMINI_API_KEY_1"), os.environ.get("GEMINI_API_KEY_2")]
    keys = [k for k in keys if k]
    if not keys:
        raise ValueError("No GEMINI_API_KEY found in environment variables.")
    return keys

def call_gemini(prompt):
    keys = get_gemini_client()
    last_err = None
    for key in keys:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return response.text
        except Exception as e:
            print(f"Gemini key error, trying next key: {e}")
            last_err = e
    raise RuntimeError(f"All Gemini keys failed: {last_err}")

def is_within_time_window(entry):
    pub_str = entry.get("published") or entry.get("updated") or ""
    if not pub_str:
        return False
    try:
        dt = date_parser.parse(pub_str)
        if dt.tzinfo is None:
            # ભારતીય ફીડ્સ સામાન્ય રીતે IST (+05:30) માં હોય છે
            ist = timezone(timedelta(hours=5, minutes=30))
            dt = dt.replace(tzinfo=ist)
        dt_utc = dt.astimezone(timezone.utc)
        now_utc = datetime.now(timezone.utc)
        diff_minutes = (now_utc - dt_utc).total_seconds() / 60.0
        return -2 <= diff_minutes <= TIME_WINDOW_MINUTES
    except Exception:
        return False

def is_important_news(title, summary):
    text = f"{title} {summary}".lower()
    if any(bad in text for bad in IGNORE_KEYWORDS):
        return False
    return any(good in text for good in IMPORTANT_KEYWORDS)

# ================= THUMBNAIL GENERATOR (HD 3D STYLE) =================
def remove_white_bg(img):
    img = img.convert("RGBA")
    data = np.array(img)
    r, g, b, a = data[:, :, 0], data[:, :, 1], data[:, :, 2], data[:, :, 3]
    white_mask = (r > 235) & (g > 235) & (b > 235)
    data[white_mask, 3] = 0
    return Image.fromarray(data)

def add_sticker_outline(img, border_size=8, border_color=(255, 255, 255, 255)):
    alpha = img.split()[3]
    dilated = alpha.filter(ImageFilter.MaxFilter(border_size * 2 + 1))
    outline = Image.new("RGBA", img.size, border_color)
    outline.putalpha(dilated)
    combined = Image.alpha_composite(outline, img)
    return combined

def create_hd_thumbnail(company_name, headline_short, sentiment, source_name, file_hash):
    W, H = 1280, 720
    # 3D-feel Editorial Gradient Background
    base = Image.new("RGBA", (W, H), (244, 240, 234, 255))
    draw = ImageDraw.Draw(base)

    # Color theme based on sentiment
    if sentiment == "POSITIVE":
        accent = (16, 150, 72, 255)      # Deep Green
        glow = (34, 197, 94, 60)
        badge_text = "POSITIVE NEWS"
    elif sentiment == "NEGATIVE":
        accent = (210, 35, 42, 255)      # Deep Red
        glow = (239, 68, 68, 60)
        badge_text = "NEGATIVE NEWS"
    else:
        accent = (29, 78, 216, 255)      # Royal Blue
        glow = (59, 130, 246, 60)
        badge_text = "IMPORTANT UPDATE"

    # Subtle 3D geometric shapes & editorial arch behind portrait
    draw.ellipse([680, 70, 1230, 620], fill=glow)
    draw.rounded_rectangle([40, 40, 680, 680], radius=32, fill=(255, 255, 255, 235), outline=accent, width=4)

    # 3D Bottom podium bar
    draw.rounded_rectangle([710, 610, 1240, 680], radius=24, fill=(30, 41, 59, 255))

    # Load Fonts (fallback to default if DejaVu not found)
    try:
        font_badge = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
        font_comp = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 58)
        font_head = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
        font_brand = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    except Exception:
        font_badge = font_comp = font_head = font_brand = ImageFont.load_default()

    # Source Tag
    draw.rounded_rectangle([80, 80, 420, 140], radius=14, fill=(15, 23, 42, 255))
    draw.text((105, 92), source_name.upper()[:18], fill=(255, 255, 255, 255), font=font_brand)

    # Sentiment 3D Pill Badge
    draw.rounded_rectangle([80, 165, 480, 235], radius=18, fill=accent)
    draw.text((110, 180), badge_text, fill=(255, 255, 255, 255), font=font_badge)

    # Company Name
    draw.text((80, 270), company_name[:20].upper(), fill=(15, 23, 42, 255), font=font_comp)

    # Wrapped Short Headline
    words = headline_short.split()
    lines, cur = [], ""
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

    # Brand Name on Bottom Podium
    draw.text((810, 628), "MARKET MUNAFA", fill=(255, 255, 255, 255), font=font_brand)

    # Add Author Photo with White Sticker Border & Drop Shadow
    if os.path.exists(AUTHOR_IMG_PATH):
        author = Image.open(AUTHOR_IMG_PATH)
        author = remove_white_bg(author)
        author.thumbnail((520, 520), Image.Resampling.LANCZOS)
        author = add_sticker_outline(author, border_size=7)

        # Drop shadow
        shadow = Image.new("RGBA", author.size, (0, 0, 0, 90))
        shadow.putalpha(author.split()[3])
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))

        pos_x = 730
        pos_y = 615 - author.height
        base.paste(shadow, (pos_x + 10, pos_y + 10), shadow)
        base.paste(author, (pos_x, pos_y), author)

    out_path = os.path.join(THUMBNAIL_DIR, f"thumb_{file_hash}.jpg")
    base.convert("RGB").save(out_path, "JPEG", quality=95)
    return out_path

# ================= GEMINI 2-STEP ARTICLE GENERATION =================
def generate_and_verify_article(source, title, summary, link):
    # STEP 1: Draft Generation
    draft_prompt = f"""
You are a senior Indian stock market financial journalist writing for Google News and AdSense-approved portal 'Market Munafa'.
Write a 100% original, copyright-free, deeply informative, SEO #1 ranking news article based on this official exchange filing:

Source: {source}
Title: {title}
Details: {summary}
Official Link: {link}

Return ONLY valid JSON with these exact keys:
{{
  "seo_title_en": "Catchy Google News SEO Title in English (max 80 chars, include stock/entity name)",
  "company_name": "Short Company or Entity Name (max 18 chars)",
  "thumb_headline": "Short punchy 4-6 word English headline for thumbnail",
  "sentiment": "POSITIVE or NEGATIVE or NEUTRAL",
  "sentiment_reason_gu": "1 line in Gujarati explaining why it is positive/negative/neutral for investors",
  "tags": ["Stock Market", "NSE", "BSE", "Company Name", "Share News"],
  "gujarati_html": "Complete rich HTML article in natural Gujarati (keep numbers, stock names, and financial terms clear). Include: Key Highlights bullet box, Detailed Analysis, Impact on Investors, and 2 FAQs.",
  "hindi_html": "Complete rich HTML article in natural Hindi with same structure.",
  "english_html": "Complete rich HTML article in crisp Indian English with same structure."
}}
Do not wrap JSON in markdown backticks if possible, or ensure clean JSON.
"""
    raw_draft = call_gemini(draft_prompt)
    clean_draft = re.sub(r"^```json|```$", "", raw_draft.strip(), flags=re.MULTILINE).strip()

    # STEP 2: Double-Check & Proofreading Verification
    verify_prompt = f"""
You are the Chief Editor of a Google News approved Indian financial publication.
Double-check the following JSON article against the original exchange filing.
Original Filing: {source} | {title} | {summary}

Check and fix:
1. Zero factual errors or hallucinated numbers.
2. Natural, fluent Gujarati, Hindi, and English (no robotic translation).
3. Clean HTML tags (<h2>, <h3>, <p>, <ul>, <li>, <strong>) suitable for Blogger & AdSense.
4. Ensure sentiment (POSITIVE / NEGATIVE / NEUTRAL) is accurate.

Return ONLY the final corrected valid JSON with the exact same keys.

DRAFT JSON:
{clean_draft}
"""
    verified_raw = call_gemini(verify_prompt)
    verified_clean = re.sub(r"^```json|```$", "", verified_raw.strip(), flags=re.MULTILINE).strip()
    return json.loads(verified_clean)

# ================= BLOGGER HTML BUILDER =================
def build_blogger_html(data, thumb_url, source_name, official_link):
    sentiment = data["sentiment"].upper()
    if sentiment == "POSITIVE":
        badge_bg, badge_label = "#16a34a", "📈 POSITIVE / BULLISH NEWS"
    elif sentiment == "NEGATIVE":
        badge_bg, badge_label = "#dc2626", "📉 NEGATIVE / BEARISH NEWS"
    else:
        badge_bg, badge_label = "#2563eb", "ℹ️ IMPORTANT MARKET UPDATE"

    html = f"""
<div style="font-family: 'Segoe UI', Roboto, sans-serif; line-height: 1.75; color: #1e293b;">
  <div style="text-align:center; margin-bottom:18px;">
    <img src="{thumb_url}" alt="{data['seo_title_en']}" style="width:100%; max-width:800px; border-radius:14px; box-shadow:0 8px 24px rgba(0,0,0,0.12);" />
  </div>

  <div style="background:{badge_bg}; color:#ffffff; padding:12px 18px; border-radius:10px; font-weight:bold; font-size:16px; margin-bottom:16px; text-align:center;">
    {badge_label} &nbsp;|&nbsp; Source: {source_name}
  </div>

  <div style="background:#f8fafc; border-left:5px solid {badge_bg}; padding:12px 16px; border-radius:6px; margin-bottom:20px; font-size:15px;">
    <strong>રોકાણકારો માટે અસર:</strong> {data.get('sentiment_reason_gu', '')}
  </div>

  <!-- Language Switcher -->
  <div style="display:flex; gap:10px; justify-content:center; margin-bottom:22px; flex-wrap:wrap;">
    <button onclick="switchLang('gu')" id="btn-gu" style="padding:10px 20px; border:none; border-radius:8px; background:#0f172a; color:#fff; font-weight:bold; cursor:pointer;">ગુજરાતી</button>
    <button onclick="switchLang('hi')" id="btn-hi" style="padding:10px 20px; border:none; border-radius:8px; background:#e2e8f0; color:#0f172a; font-weight:bold; cursor:pointer;">हिन्दी</button>
    <button onclick="switchLang('en')" id="btn-en" style="padding:10px 20px; border:none; border-radius:8px; background:#e2e8f0; color:#0f172a; font-weight:bold; cursor:pointer;">English</button>
  </div>

  <div id="lang-gu" style="display:block;">
    {data['gujarati_html']}
  </div>
  <div id="lang-hi" style="display:none;">
    {data['hindi_html']}
  </div>
  <div id="lang-en" style="display:none;">
    {data['english_html']}
  </div>

  <hr style="margin:24px 0; border:0; border-top:1px solid #cbd5e1;" />
  <p style="font-size:13px; color:#64748b;">
    <strong>Official Exchange Reference:</strong> <a href="{official_link}" target="_blank" rel="nofollow noopener">View Official {source_name} Filing</a><br/>
    <em>Disclaimer: આ માહિતી માત્ર શૈક્ષણિક અને ન્યૂઝ હેતુ માટે છે. રોકાણ કરતા પહેલા તમારા SEBI રજિસ્ટર્ડ સલાહકારની સલાહ લો.</em>
  </p>
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
    return html

# ================= BLOGGER PUBLISHER =================
def publish_to_blogger(title, html_content, labels):
    creds = Credentials(
        token=None,
        refresh_token=os.environ["REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["CLIENT_ID"],
        client_secret=os.environ["CLIENT_SECRET"],
    )
    service = build("blogger", "v3", credentials=creds)
    body = {
        "kind": "blogger#post",
        "title": title,
        "content": html_content,
        "labels": labels[:8],
    }
    post = service.posts().insert(blogId=os.environ["BLOG_ID"], body=body, isDraft=False).execute()
    print(f"✅ Published: {post.get('url')}")

# ================= MAIN EXECUTION =================
def main():
    history = load_history()
    repo = os.environ.get("GITHUB_REPO", "")
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

            if uid in history:
                continue
            if not is_within_time_window(entry):
                continue
            if not is_important_news(title, summary):
                continue

            print(f"🔥 Found Important Recent News: {title}")
            try:
                article_data = generate_and_verify_article(feed_info["name"], title, summary, link)
                thumb_path = create_hd_thumbnail(
                    article_data["company_name"],
                    article_data["thumb_headline"],
                    article_data["sentiment"],
                    feed_info["name"],
                    uid[:10],
                )
                thumb_url = f"https://raw.githubusercontent.com/{repo}/main/{thumb_path}"
                post_html = build_blogger_html(article_data, thumb_url, feed_info["name"], link)
                publish_to_blogger(article_data["seo_title_en"], post_html, article_data.get("tags", ["Stock Market"]))

                history.add(uid)
                save_history(history)
                published_count += 1
                time.sleep(5)
            except Exception as e:
                print(f"❌ Error processing '{title}': {e}")

    print(f"Done. Total published in this run: {published_count}")

if __name__ == "__main__":
    main()
