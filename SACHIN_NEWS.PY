import os
import re
import json
import time
import hashlib
import subprocess
import requests
import feedparser
import numpy as np
from datetime import datetime, timezone, timedelta
from dateutil import parser as date_parser
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ================= CONFIGURATION =================
MODEL_NAME = "gemini-3.5-flash-lite"
MIN_QUALITY_SCORE = 95       # 95/100 થી ઓછો સ્કોર હોય તો પોસ્ટ નહીં થાય
MAX_AUDIT_ROUNDS = 3         # જ્યાં સુધી પરફેક્ટ ન થાય ત્યાં સુધી 3 રાઉન્ડ ચેક
TIME_WINDOW_MINUTES = 15     # છેલ્લી 15 મિનિટના ન્યૂઝ

FEEDS = [
    {"name": "BSE Announcement", "url": "https://www.bseindia.com/data/xml/announcements.aspx"},
    {"name": "BSE Notice", "url": "https://www.bseindia.com/data/xml/notices.xml"},
    {"name": "NSE Announcement", "url": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"},
    {"name": "NSE Corporate Action", "url": "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"},
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

    # પોસ્ટ પબ્લિશ થાય તે પહેલાં જ GitHub પર ફોટો પુશ કરો જેથી URL તરત લાઈવ થઈ જાય
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
    # jsDelivr CDN લિંક Google News અને Blogger બંનેમાં ફાસ્ટ લોડ થાય છે
    return f"https://cdn.jsdelivr.net/gh/{repo}@main/{out_path}"

# ================= STRICT ITERATIVE QUALITY GATE =================
def sanitize_non_default_headings(html_str):
    # હિન્દી અને અંગ્રેજીમાં <h2> ને સ્ટાઈલ્ડ <div> માં ફેરવે છે જેથી થીમનું Quick Summary મિક્સ ન થાય
    html_str = re.sub(
        r"<h2[^>]*>(.*?)</h2>",
        r'<div style="font-size:21px; font-weight:800; color:#0f172a; margin:20px 0 10px;">\1</div>',
        html_str,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html_str

def generate_and_strictly_verify(source, title, summary, link):
    print("📝 Generating Initial SEO #1 Draft...")
    draft_prompt = f"""
You are a senior Indian stock market financial journalist for 'Market Munafa' (Google News & AdSense approved).
Write a 100% original, copyright-free, human-like E-E-A-T news article based ONLY on this official exchange filing:

Source: {source}
Title: {title}
Details: {summary}
Link: {link}

STRICT WRITING RULES:
- Use natural contemporary Indian financial terms (e.g. write 'ડેવલપમેન્ટ ઓર્ડર', 'ડિફેન્સ સેક્ટર', 'डेवलपमेंट ऑर्डर' — NEVER robotic literal translations like 'विकास ऑर्डर').
- Zero invented share prices, targets, or fake numbers.
- No markdown (** or ##) inside HTML. Use clean <h2>, <h3>, <p>, <ul>, <li>, <strong>.

Return ONLY valid JSON with these exact keys:
{{
  "seo_title_en": "High-CTR Google News SEO Title in English (60-78 chars)",
  "meta_description": "Crisp 150-char English SEO meta summary",
  "company_name": "Short Company Name (max 18 chars)",
  "thumb_headline": "Short punchy 4-6 word English headline for thumbnail",
  "sentiment": "POSITIVE or NEGATIVE or NEUTRAL",
  "impact_gu": "1 crisp natural Gujarati sentence on investor impact",
  "impact_hi": "1 crisp natural Hindi sentence on investor impact",
  "impact_en": "1 crisp natural Indian English sentence on investor impact",
  "tags": ["Stock Market", "NSE", "BSE", "Company Name", "Share News"],
  "quick_story_gu": ["Point 1", "Point 2", "Point 3", "Point 4"],
  "quick_story_hi": ["Point 1", "Point 2", "Point 3", "Point 4"],
  "quick_story_en": ["Point 1", "Point 2", "Point 3", "Point 4"],
  "gujarati_html": "Detailed, natural Gujarati HTML article (What Happened, Key Filing Details, Investor Impact, 2 FAQs)",
  "hindi_html": "Detailed, natural Hindi HTML article with same structure",
  "english_html": "Detailed, crisp Indian English HTML article with same structure"
}}
"""
    current_json_str = call_gemini_json(draft_prompt, temperature=0.25)

    for attempt in range(1, MAX_AUDIT_ROUNDS + 1):
        print(f"🔍 Quality Audit Round {attempt}/{MAX_AUDIT_ROUNDS}...")
        audit_prompt = f"""
You are the Ruthless Chief Editor of a top Google News & AdSense financial portal.
Audit this JSON article strictly against the original exchange filing.

Original Filing: {source} | {title} | {summary}

Check ALL 5 criteria:
1. 100% Factual Accuracy (zero hallucinated numbers/dates).
2. Zero Robotic Translation (no awkward literal Hindi/Gujarati words like 'विकास ऑर्डर'; must read like a native Indian market journalist).
3. Clean HTML (no markdown ** or ##, all tags properly closed).
4. Complete Quick Story (4 crisp points in all 3 languages) and Impact lines in all 3 languages.
5. High AdSense E-E-A-T & Google News #1 SEO quality.

Fix ANY issue you find, then assign an honest "quality_score" (0 to 100) and "issues_found" list.
Return ONLY valid JSON with all original keys PLUS "quality_score" (integer) and "issues_found" (list of strings).

ARTICLE JSON TO AUDIT:
{current_json_str}
"""
        audited_str = call_gemini_json(audit_prompt, temperature=0.1)
        data = json.loads(audited_str)
        score = int(data.get("quality_score", 0))
        issues = data.get("issues_found", [])
        print(f"📊 Round {attempt} Score: {score}/100 | Issues: {issues}")

        # Python-level strict checks
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

    raise ValueError(f"Article rejected: Failed to reach {MIN_QUALITY_SCORE}/100 after {MAX_AUDIT_ROUNDS} audit rounds.")

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

    html = f"""
<script type="application/ld+json">{schema_json}</script>

<div style="font-family: 'Segoe UI', Roboto, Arial, sans-serif; line-height: 1.8; color: #1e293b;">
  <div style="text-align:center; margin-bottom:18px;">
    <img src="{thumb_url}" alt="{data['seo_title_en']}" width="1280" height="720" style="width:100%; max-width:800px; height:auto; border-radius:14px; box-shadow:0 8px 24px rgba(0,0,0,0.12);" />
  </div>

  <div style="background:{badge_bg}; color:#ffffff; padding:12px 18px; border-radius:10px; font-weight:bold; font-size:16px; margin-bottom:18px; text-align:center;">
    {badge_label} &nbsp;|&nbsp; Source: {source_name}
  </div>

  <!-- Language Switcher (Gujarati Default) -->
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

            if uid in history or not is_within_time_window(entry) or not is_important_news(title, summary):
                continue

            print(f"🔥 Processing Important News: {title}")
            try:
                article_data = generate_and_strictly_verify(feed_info["name"], title, summary, link)
                thumb_url = create_and_push_thumbnail(
                    article_data["company_name"],
                    article_data["thumb_headline"],
                    article_data["sentiment"],
                    feed_info["name"],
                    uid[:10],
                )
                post_html = build_blogger_html(article_data, thumb_url, feed_info["name"], link)
                publish_to_blogger(article_data["seo_title_en"], post_html, article_data.get("tags", ["Stock Market"]))

                history.add(uid)
                save_history(history)
                published_count += 1
                time.sleep(5)
            except Exception as e:
                print(f"❌ NOT PUBLISHED '{title}': {e}")

    print(f"Done. Total published in this run: {published_count}")

if __name__ == "__main__":
    main()
