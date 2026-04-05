import os
import re
import time
import html
import sqlite3
from datetime import datetime
from urllib.parse import urlparse, urljoin

import feedparser
import requests
from deep_translator import GoogleTranslator


# =========================
# 体育 RSS 源
# =========================

RSS_URLS = [
    "https://www.espn.com/espn/rss/news",
    "http://feeds.bbci.co.uk/sport/rss.xml?edition=uk",
    "https://www.skysports.com/rss/12040",
    "https://sports.yahoo.com/rss/",
    "https://www.cbssports.com/rss/headlines/",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))
MAX_SUMMARY_LENGTH = int(os.getenv("MAX_SUMMARY_LENGTH", "900"))

FIRST_RUN_SKIP_OLD = True

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
}


# =========================
# 数据库
# =========================

def init_db():
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_items (
            link TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def has_sent(link: str) -> bool:
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_items WHERE link = ?", (link,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_sent(link: str):
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO sent_items(link, created_at) VALUES (?, ?)",
        (link, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def has_any_sent_items() -> bool:
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sent_items")
    count = cur.fetchone()[0]
    conn.close()
    return count > 0


# =========================
# 工具函数
# =========================

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<.*?>", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def shorten_text(text: str, max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def safe_translate(text: str) -> str:
    if not text:
        return ""

    text = text.strip()
    if not text:
        return ""

    if len(text) > 1200:
        text = text[:1200]

    for i in range(3):
        try:
            result = GoogleTranslator(source="auto", target="zh-CN").translate(text)
            if result and result.strip():
                return result.strip()
        except Exception as e:
            print(f"翻译失败，第{i + 1}次: {e}")
            time.sleep(1)

    return ""


def detect_tags(title_en: str, title_cn: str, summary_cn: str) -> list:
    text = f"{title_en}\n{title_cn}\n{summary_cn}".lower()
    tags = []

    keyword_map = {
        "#足球": ["football", "soccer", "premier league", "champions league", "fifa", "uefa", "man utd", "arsenal", "barcelona", "real madrid"],
        "#篮球": ["nba", "basketball", "lakers", "warriors", "celtics", "lebron", "curry"],
        "#网球": ["tennis", "atp", "wta", "grand slam", "djokovic", "nadal", "federer"],
        "#F1": ["formula 1", "f1", "verstappen", "hamilton", "ferrari", "red bull"],
        "#NFL": ["nfl", "super bowl", "chiefs", "eagles", "cowboys"],
        "#棒球": ["mlb", "baseball", "yankees", "dodgers"],
        "#高尔夫": ["golf", "pga", "masters", "rory mcilroy", "tiger woods"],
        "#综合": ["olympics", "sports", "athlete", "match", "tournament", "coach"],
    }

    for tag, keywords in keyword_map.items():
        if any(k in text for k in keywords):
            tags.append(tag)

    return tags[:3]


def is_valid_http_url(url: str) -> bool:
    if not url:
        return False
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def get_image_url_from_rss(entry) -> str:
    media_content = getattr(entry, "media_content", None)
    if media_content and isinstance(media_content, list):
        for item in media_content:
            url = item.get("url")
            if url:
                return url

    media_thumbnail = getattr(entry, "media_thumbnail", None)
    if media_thumbnail and isinstance(media_thumbnail, list):
        for item in media_thumbnail:
            url = item.get("url")
            if url:
                return url

    links = getattr(entry, "links", [])
    if links:
        for item in links:
            href = item.get("href", "")
            type_ = item.get("type", "")
            rel = item.get("rel", "")
            if href and (rel == "enclosure" or str(type_).startswith("image/")):
                return href

    raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
    if raw_summary:
        m = re.search(r'<img[^>]+src="([^"]+)"', raw_summary, re.I)
        if m:
            return m.group(1)

    return ""


def normalize_image_url(img_url: str, base_url: str) -> str:
    if not img_url:
        return ""
    if img_url.startswith("//"):
        return "https:" + img_url
    if img_url.startswith("/"):
        return urljoin(base_url, img_url)
    return img_url


def get_image_url_from_page(article_url: str) -> str:
    """
    RSS 没图时，进原文页面抓图。
    优先：
    1. og:image
    2. twitter:image
    3. 页面里第一张较大的图片
    """
    if not is_valid_http_url(article_url):
        return ""

    try:
        resp = requests.get(article_url, headers=REQUEST_HEADERS, timeout=15)
        if resp.status_code != 200 or not resp.text:
            return ""

        html_text = resp.text

        # og:image
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        ]

        for pattern in patterns:
            m = re.search(pattern, html_text, re.I)
            if m:
                img = normalize_image_url(m.group(1).strip(), article_url)
                if is_valid_http_url(img):
                    return img

        # 退而求其次：找 img 标签
        imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_text, re.I)
        for img in imgs:
            img = normalize_image_url(img.strip(), article_url)
            if not is_valid_http_url(img):
                continue

            lower_img = img.lower()
            # 尽量过滤 logo / icon / avatar
            if any(x in lower_img for x in ["logo", "icon", "avatar", "sprite", ".svg"]):
                continue

            return img

    except Exception as e:
        print(f"网页抓图失败: {article_url} -> {e}")

    return ""


def get_best_image_url(entry, article_url: str) -> str:
    # 先用 RSS 里的图
    rss_img = get_image_url_from_rss(entry)
    if is_valid_http_url(rss_img):
        return rss_img

    # 再去原文页面抓图
    page_img = get_image_url_from_page(article_url)
    if is_valid_http_url(page_img):
        return page_img

    return ""


def build_caption(title_cn: str, summary_cn: str, tags: list) -> str:
    header = "【体育快讯】"
    tag_line = " ".join(tags).strip()

    parts = [header]
    if tag_line:
        parts.append(tag_line)

    parts.append("")
    parts.append(title_cn.strip())

    if summary_cn.strip():
        parts.append("")
        parts.append(summary_cn.strip())

    caption = "\n".join(parts).strip()

    if len(caption) > 1000:
        caption = caption[:1000].rstrip() + "..."
    return caption


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True
        },
        timeout=30
    )
    print("sendMessage 结果:", resp.status_code, resp.text)
    return resp


def send_telegram_photo(photo_url: str, caption: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "photo": photo_url,
            "caption": caption
        },
        timeout=30
    )
    print("sendPhoto 结果:", resp.status_code, resp.text)
    return resp


def extract_summary(entry) -> str:
    raw_summary = (
        getattr(entry, "summary", "")
        or getattr(entry, "description", "")
    )

    content_list = getattr(entry, "content", None)
    if content_list and isinstance(content_list, list):
        for item in content_list:
            value = item.get("value", "")
            if value and len(value) > len(raw_summary):
                raw_summary = value

    summary_clean = clean_html(raw_summary)
    summary_clean = re.sub(r"\s+", " ", summary_clean).strip()

    if len(summary_clean) < 40:
        return ""

    return shorten_text(summary_clean, MAX_SUMMARY_LENGTH)


# =========================
# 核心逻辑
# =========================

def process_feed(feed_url: str):
    print(f"[{datetime.now()}] 检查 RSS: {feed_url}")
    feed = feedparser.parse(feed_url)

    if not feed.entries:
        print("没有抓到内容")
        return

    entries = list(feed.entries[:10])
    entries.reverse()

    first_run = not has_any_sent_items()

    for entry in entries:
        link = getattr(entry, "link", "").strip()
        title_en = clean_html(getattr(entry, "title", "").strip())

        if not link or not title_en:
            continue

        if has_sent(link):
            continue

        if first_run and FIRST_RUN_SKIP_OLD:
            print("首次运行，跳过旧新闻:", title_en)
            mark_sent(link)
            continue

        summary_clean = extract_summary(entry)

        title_cn = safe_translate(title_en)
        summary_cn = safe_translate(summary_clean) if summary_clean else ""

        if not title_cn:
            print("跳过：标题翻译失败 ->", title_en)
            mark_sent(link)
            continue

        if summary_clean and not summary_cn:
            print("摘要翻译失败，仅发送标题 ->", title_en)
            summary_cn = ""

        if summary_cn and len(summary_cn.strip()) < 15:
            summary_cn = ""

        tags = detect_tags(title_en, title_cn, summary_cn)
        caption = build_caption(title_cn, summary_cn, tags)

        image_url = get_best_image_url(entry, link)

        try:
            if is_valid_http_url(image_url):
                resp = send_telegram_photo(image_url, caption)
                if resp.status_code != 200:
                    print("图片发送失败，改为纯文字")
                    send_telegram_message(caption)
            else:
                send_telegram_message(caption)

            mark_sent(link)
            print("已发送:", title_en)

        except Exception as e:
            print("发送失败:", e)

        time.sleep(SEND_DELAY)


def main():
    if not BOT_TOKEN:
        raise ValueError("缺少环境变量 BOT_TOKEN")
    if not CHAT_ID:
        raise ValueError("缺少环境变量 CHAT_ID")

    init_db()

    print("体育机器人启动成功（抓原文图版）")
    print("频道:", CHAT_ID)

    while True:
        for rss in RSS_URLS:
            try:
                process_feed(rss)
            except Exception as e:
                print(f"处理 RSS 失败 {rss}: {e}")

        print(f"休眠 {CHECK_INTERVAL} 秒...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
