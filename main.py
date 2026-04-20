import os
import re
import time
import html
import sqlite3
import random
import tempfile
from datetime import datetime
from urllib.parse import urlparse, urljoin

import feedparser
import requests
from deep_translator import GoogleTranslator


# =========================
# 体育 RSS 源（精简版）
# =========================

RSS_URLS = [
    "https://www.espn.com/espn/rss/news",
    "http://feeds.bbci.co.uk/sport/rss.xml?edition=uk",
    "https://www.skysports.com/rss/12040",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "900"))   # 默认 15 分钟
SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))
MAX_SUMMARY_LENGTH = int(os.getenv("MAX_SUMMARY_LENGTH", "600"))

FIRST_RUN_SKIP_OLD = True
MAX_FEED_ITEMS_PER_CHECK = 3
COVERS_DIR = "covers"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

SKIP_KEYWORDS = [
    "live",
    "watch",
    "video",
    "highlights",
    "podcast",
    "newsletter",
    "ranking",
    "power rankings",
    "fantasy",
    "rumour",
    "rumor",
    "minute-by-minute",
    "as it happened",
    "preview show",
]


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
# 文本处理
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
    """
    翻译成功返回中文
    翻译失败返回空字符串
    """
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


def should_skip_title(title_en: str) -> bool:
    title_lower = title_en.lower().strip()
    if not title_lower:
        return True

    if any(k in title_lower for k in SKIP_KEYWORDS):
        return True

    return False


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


def build_caption(title_cn: str, summary_cn: str, tags: list) -> str:
    """
    只发中文，不带链接，不带来源，不带英文原文
    """
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

    # Telegram caption 最长 1024，留点余量
    if len(caption) > 1000:
        caption = caption[:1000].rstrip() + "..."
    return caption


# =========================
# 图片处理
# =========================

def is_valid_http_url(url: str) -> bool:
    if not url:
        return False
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def normalize_image_url(img_url: str, base_url: str) -> str:
    if not img_url:
        return ""
    if img_url.startswith("//"):
        return "https:" + img_url
    if img_url.startswith("/"):
        return urljoin(base_url, img_url)
    return img_url


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


def get_image_url_from_page(article_url: str) -> str:
    if not is_valid_http_url(article_url):
        return ""

    try:
        resp = requests.get(article_url, headers=REQUEST_HEADERS, timeout=15)
        if resp.status_code != 200 or not resp.text:
            return ""

        html_text = resp.text

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

        imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_text, re.I)
        for img in imgs:
            img = normalize_image_url(img.strip(), article_url)
            if not is_valid_http_url(img):
                continue

            lower_img = img.lower()
            if any(x in lower_img for x in ["logo", "icon", "avatar", "sprite", ".svg"]):
                continue

            return img

    except Exception as e:
        print(f"网页抓图失败: {article_url} -> {e}")

    return ""


def get_local_cover_list():
    if not os.path.isdir(COVERS_DIR):
        return []

    files = []
    for name in os.listdir(COVERS_DIR):
        lower = name.lower()
        if lower.endswith(".jpg") or lower.endswith(".jpeg") or lower.endswith(".png"):
            files.append(os.path.join(COVERS_DIR, name))

    return sorted(files)


def get_random_local_cover():
    covers = get_local_cover_list()
    if not covers:
        return ""
    return random.choice(covers)


def get_best_remote_image_url(entry, article_url: str) -> str:
    rss_img = get_image_url_from_rss(entry)
    if is_valid_http_url(rss_img):
        return rss_img

    page_img = get_image_url_from_page(article_url)
    if is_valid_http_url(page_img):
        return page_img

    return ""


def guess_extension_from_response(resp, url: str) -> str:
    content_type = (resp.headers.get("Content-Type") or "").lower()

    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"

    parsed = urlparse(url)
    path = parsed.path.lower()
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        return ".jpg"
    if path.endswith(".png"):
        return ".png"
    if path.endswith(".webp"):
        return ".webp"

    return ".jpg"


def download_remote_image(url: str) -> str:
    if not is_valid_http_url(url):
        return ""

    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20, stream=True)
        if resp.status_code != 200:
            print(f"下载图片失败，状态码: {resp.status_code} -> {url}")
            return ""

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "image/" not in content_type and not any(x in content_type for x in ["jpeg", "jpg", "png", "webp"]):
            print(f"下载内容不是图片: {content_type} -> {url}")
            return ""

        ext = guess_extension_from_response(resp, url)
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    tmp.write(chunk)
            return tmp.name

    except Exception as e:
        print(f"下载远程图片异常: {url} -> {e}")
        return ""


# =========================
# Telegram 发送
# =========================

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


def send_telegram_photo_by_file(photo_path: str, caption: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as f:
        resp = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "caption": caption
            },
            files={"photo": f},
            timeout=30
        )
    print("sendPhoto(file) 结果:", resp.status_code, resp.text)
    return resp


# =========================
# 主流程
# =========================

def process_feed(feed_url: str):
    print(f"[{datetime.now()}] 检查 RSS: {feed_url}")
    feed = feedparser.parse(feed_url)

    if not feed.entries:
        print("没有抓到内容")
        return

    entries = list(feed.entries[:MAX_FEED_ITEMS_PER_CHECK])
    entries.reverse()

    first_run = not has_any_sent_items()

    for entry in entries:
        link = getattr(entry, "link", "").strip()
        title_en = clean_html(getattr(entry, "title", "").strip())

        if not link or not title_en:
            continue

        if should_skip_title(title_en):
            print("跳过低价值内容:", title_en)
            # 不标记 sent，这样以后如果标题规范变化仍有机会被过滤规则重新处理
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

        temp_remote_file = ""
        try:
            resp = None

            # 1) 先尝试远程图：下载后上传
            remote_img_url = get_best_remote_image_url(entry, link)
            if remote_img_url:
                temp_remote_file = download_remote_image(remote_img_url)

            if temp_remote_file and os.path.isfile(temp_remote_file):
                resp = send_telegram_photo_by_file(temp_remote_file, caption)
                if resp.status_code != 200:
                    print("远程图上传失败，尝试公图")

            # 2) 远程图失败，尝试公图
            if resp is None or resp.status_code != 200:
                local_cover = get_random_local_cover()
                if local_cover and os.path.isfile(local_cover):
                    resp = send_telegram_photo_by_file(local_cover, caption)
                    if resp.status_code != 200:
                        print("公图发送失败，改为纯文字")
                        resp = send_telegram_message(caption)
                else:
                    resp = send_telegram_message(caption)

            if resp.status_code == 200:
                mark_sent(link)
                print("已发送:", title_en)
            else:
                print("发送失败，未记录:", title_en)

        except Exception as e:
            print("处理失败:", title_en, "->", e)

        finally:
            if temp_remote_file and os.path.isfile(temp_remote_file):
                try:
                    os.remove(temp_remote_file)
                except Exception:
                    pass

        time.sleep(SEND_DELAY)


def main():
    if not BOT_TOKEN:
        raise ValueError("缺少环境变量 BOT_TOKEN")
    if not CHAT_ID:
        raise ValueError("缺少环境变量 CHAT_ID")

    init_db()

    print("体育频道机器人启动成功（精简版）")
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
