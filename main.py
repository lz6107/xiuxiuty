import os
import re
import json
import time
import html
import hashlib
import logging
from typing import Dict, List, Optional

import feedparser
import requests
from deep_translator import GoogleTranslator


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
FEEDS = [x.strip() for x in os.getenv("FEEDS", "").split(",") if x.strip()]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
SEND_DELAY = float(os.getenv("SEND_DELAY", "1.5"))
FIRST_RUN_SKIP_OLD = os.getenv("FIRST_RUN_SKIP_OLD", "true").lower() == "true"
TRANSLATE = os.getenv("TRANSLATE", "true").lower() == "true"
TRANSLATE_SUMMARY = os.getenv("TRANSLATE_SUMMARY", "false").lower() == "true"
TARGET_LANG = os.getenv("TARGET_LANG", "zh-CN").strip()
STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()
MAX_SUMMARY_LENGTH = int(os.getenv("MAX_SUMMARY_LENGTH", "180"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

if not BOT_TOKEN:
    raise ValueError("缺少环境变量 BOT_TOKEN")
if not CHAT_ID:
    raise ValueError("缺少环境变量 CHAT_ID")
if not FEEDS:
    raise ValueError("缺少环境变量 FEEDS，格式示例：https://a.com/rss,https://b.com/feed")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"sent": [], "initialized": False}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": [], "initialized": False}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def clean_html_text(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def has_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def translate_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if has_chinese(text):
        return text
    try:
        return GoogleTranslator(source="auto", target=TARGET_LANG).translate(text)
    except Exception as e:
        logging.warning(f"翻译失败: {e}")
        return text


def make_entry_id(entry) -> str:
    raw = (
        entry.get("id")
        or entry.get("link")
        or f"{entry.get('title', '')}-{entry.get('published', '')}"
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def send_telegram_message(text: str) -> bool:
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(TELEGRAM_API, data=payload, timeout=REQUEST_TIMEOUT)
        logging.info(f"发送结果: {resp.status_code} {resp.text[:300]}")
        return resp.ok
    except Exception as e:
        logging.error(f"发送失败: {e}")
        return False


def parse_feed(feed_url: str):
    try:
        parsed = feedparser.parse(feed_url)
        if parsed.bozo:
            logging.warning(f"RSS 解析警告: {feed_url}")
        return parsed
    except Exception as e:
        logging.error(f"RSS 读取失败 {feed_url}: {e}")
        return None


def format_message(
    source_title: str,
    title: str,
    translated_title: str,
    summary: str,
    translated_summary: str,
    link: str,
) -> str:
    parts: List[str] = []

    if TRANSLATE and translated_title and translated_title != title:
        parts.append("【体育翻译】")
        parts.append("")
        parts.append(translated_title)
        parts.append("")
        parts.append(f"原文：{title}")
    else:
        parts.append(title)

    if summary:
        parts.append("")
        if TRANSLATE_SUMMARY and translated_summary and translated_summary != summary:
            parts.append(f"摘要：{translated_summary}")
            parts.append(f"原摘要：{summary}")
        else:
            parts.append(summary)

    parts.append("")
    parts.append(f"来源：{source_title}")
    parts.append(link)

    return "\n".join(parts).strip()


def main():
    state = load_state()
    sent_ids = set(state.get("sent", []))
    initialized = state.get("initialized", False)

    logging.info("程序启动成功")
    logging.info(f"订阅数: {len(FEEDS)}")
    logging.info(f"检查间隔: {CHECK_INTERVAL}s")
    logging.info(f"首跑跳过旧新闻: {FIRST_RUN_SKIP_OLD}")
    logging.info(f"翻译开启: {TRANSLATE}")
    logging.info(f"翻译摘要: {TRANSLATE_SUMMARY}")

    while True:
        try:
            new_sent = 0

            for feed_url in FEEDS:
                logging.info(f"检查 RSS: {feed_url}")
                parsed = parse_feed(feed_url)
                if not parsed:
                    continue

                source_title = parsed.feed.get("title", feed_url)

                entries = parsed.entries or []
                if not entries:
                    continue

                # 老到新排序，避免频道顺序颠倒
                entries = list(entries)[::-1]

                for entry in entries:
                    eid = make_entry_id(entry)

                    if eid in sent_ids:
                        continue

                    if FIRST_RUN_SKIP_OLD and not initialized:
                        sent_ids.add(eid)
                        continue

                    title = clean_html_text(entry.get("title", "")).strip()
                    link = entry.get("link", "").strip()

                    summary = clean_html_text(
                        entry.get("summary", "") or entry.get("description", "")
                    )
                    summary = truncate_text(summary, MAX_SUMMARY_LENGTH)

                    translated_title = translate_text(title) if TRANSLATE else title
                    translated_summary = (
                        translate_text(summary)
                        if TRANSLATE and TRANSLATE_SUMMARY and summary
                        else summary
                    )

                    msg = format_message(
                        source_title=source_title,
                        title=title,
                        translated_title=translated_title,
                        summary=summary,
                        translated_summary=translated_summary,
                        link=link,
                    )

                    ok = send_telegram_message(msg)
                    if ok:
                        sent_ids.add(eid)
                        new_sent += 1
                        time.sleep(SEND_DELAY)

            if not initialized:
                initialized = True

            # 控制 state 大小
            sent_list = list(sent_ids)
            if len(sent_list) > 5000:
                sent_list = sent_list[-5000:]
                sent_ids = set(sent_list)

            save_state({
                "sent": sent_list,
                "initialized": initialized
            })

            logging.info(f"本轮完成，新增推送: {new_sent}")
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logging.exception(f"主循环异常: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
