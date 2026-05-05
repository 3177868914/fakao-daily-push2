#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法考每日一题自动推送脚本 v3 - RSS版
- 通过RSSHub获取微博内容，解决GitHub Actions海外IP被封的问题
- 不需要微博Cookie！零维护！
- 每天早上推送题目，晚上推送答案
- 通过 Server酱 推送到微信
- 自动去重，避免重复推送
"""

import os
import json
import re
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============ 配置 ============
CONFIG_PATH = Path(__file__).parent / "config" / "teachers.json"
STATE_PATH = Path(__file__).parent / "state.json"

SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
MODE = os.environ.get("PUSH_MODE", "question")

BEIJING_TZ = timezone(timedelta(hours=8))

RSSHUB_INSTANCES = [
    "https://rsshub.rssforever.com",
    "https://rss.shab.fun",
    "https://rsshub.liumingye.cn",
    "https://rsshub.agbot.top",
    "https://hub.slarker.me",
    "https://rsshub.diygod.me",
    "https://rsshub.app",
]


# ============ 工具函数 ============

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"pushed_ids": {}, "last_push_date": ""}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def clean_html(html_text):
    if not html_text:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', html_text)
    text = re.sub(r'<a[^>]*>全文</a >', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*/>', r'[\1]', text)
    text = re.sub(r'<img[^>]*/>', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&').replace('&quot;', '"')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_rss_time(time_str):
    if not time_str:
        return datetime.now(BEIJING_TZ)
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(time_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(BEIJING_TZ)
        except ValueError:
            continue
    return datetime.now(BEIJING_TZ)


def is_recent(pub_time, hours=48):
    now = datetime.now(BEIJING_TZ)
    return (now - pub_time).total_seconds() < hours * 3600


# ============ RSS获取 ============

def fetch_rss_feed(uid, retry_instances=4):
    rss_path = f"/weibo/user/{uid}"
    for i, instance in enumerate(RSSHUB_INSTANCES[:retry_instances]):
        url = instance + rss_path
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/xml, application/rss+xml, text/xml, */*",
        }
        try:
            print(f"  🔄 尝试 {instance} ...")
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            posts = parse_rss_xml(resp.text, uid)
            if posts:
                print(f"  ✅ 从 {instance} 获取到 {len(posts)} 条微博")
                return posts
            else:
                print(f"  ⚠️  {instance} 返回空内容，尝试下一个")
                continue
        except requests.exceptions.RequestException as e:
            print(f"  ❌ {instance} 请求失败: {e}")
            continue
        except ET.ParseError as e:
            print(f"  ❌ {instance} XML解析失败: {e}")
            continue
    print(f"  🔄 RSSHub全部失败，尝试备选方案...")
    return fetch_rss_feed_backup(uid)


def parse_rss_xml(xml_text, uid):
    posts = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = root.findall('.//item')
    if not items:
        items = root.findall('.//{http://www.w3.org/2005/Atom}entry')
    for item in items:
        title = item.findtext('title', '') or item.findtext('{http://www.w3.org/2005/Atom}title', '')
        link = item.findtext('link', '') or ''
        if not link:
            link_elem = item.find('link')
            if link_elem is not None:
                link = link_elem.get('href', link_elem.text or '')
        description = item.findtext('description', '') or item.findtext('{http://www.w3.org/2005/Atom}content', '') or item.findtext('{http://www.w3.org/2005/Atom}summary', '')
        pub_date = item.findtext('pubDate', '') or item.findtext('{http://www.w3.org/2005/Atom}published', '') or item.findtext('{http://www.w3.org/2005/Atom}updated', '')
        mid = ""
        mid_match = re.search(r'/(\d+)$', link)
        if mid_match:
            mid = mid_match.group(1)
        post_id = str(hash(title + link)) if title and link else mid
        posts.append({
            "id": post_id,
            "mid": mid,
            "title": title,
            "text": description,
            "created_at": pub_date,
            "link": link,
            "parsed_time": parse_rss_time(pub_date),
        })
    return posts


def fetch_rss_feed_backup(uid):
    backup_urls = [
        f"https://rssfeed.today/weibo/rss/{uid}",
    ]
    for url in backup_urls:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/xml, text/xml, */*",
        }
        try:
            print(f"  🔄 尝试备选 {url} ...")
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            posts = parse_rss_xml(resp.text, uid)
            if posts:
                print(f"  ✅ 备选方案获取到 {len(posts)} 条微博")
                return posts
        except Exception as e:
            print(f"  ❌ 备选方案失败: {e}")
            continue
    print(f"  ❌ 所有RSS方案均失败")
    return []


# ============ 推送 ============

def push_to_serverchan(title, content, sendkey):
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = {"title": title, "desp": content}
    try:
        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 0:
            print(f"  ✅ 推送成功: {title}")
            return True
        else:
            print(f"  ❌ 推送失败: {result.get('message', 'unknown error')}")
            return False
    except Exception as e:
        print(f"  ❌ 推送异常: {e}")
        return False


def format_question_push(teacher, post):
    text = clean_html(post["text"])
    subject = teacher["subject"]
    name = teacher["name"]
    link = post.get("link", "")
    return f"""## 📝 {subject} · 每日一题

> **老师**: {name}
> **科目**: {subject}
> **发布时间**: {post['created_at']}

---

{text}

---

🔗 [查看原微博]({link})

---
💡 **做完题后，答案会在今晚20:00推送！**
📚 **想要知识库深度解析？来 ima.copilot 找我聊！**
"""


def format_answer_push(teacher, post):
    text = clean_html(post["text"])
    subject = teacher["subject"]
    name = teacher["name"]
    link = post.get("link", "")
    return f"""## ✅ {subject} · 每日一题答案

> **老师**: {name}
> **科目**: {subject}
> **发布时间**: {post['created_at']}

---

{text}

---

🔗 [查看原微博]({link})

---
📚 **想要知识库深度解析？来 ima.copilot 找我聊！**
"""


# ============ 识别逻辑 ============

def is_question_post(post, keywords):
    text = clean_html(post["text"]).lower()
    title = (post.get("title") or "").lower()
    combined = text + " " + title
    for kw in keywords:
        if kw.lower() in combined:
            return True
    return False


def is_answer_post(post, answer_keywords, question_keywords):
    text = clean_html(post["text"]).lower()
    title = (post.get("title") or "").lower()
    combined = text + " " + title
    has_answer_kw = any(kw.lower() in combined for kw in answer_keywords)
    has_question_kw = any(kw.lower() in combined for kw in question_keywords)
    return has_answer_kw and has_question_kw


# ============ 主逻辑 ============

def run():
    print(f"{'='*50}")
    print(f"🚀 法考每日一题推送 v3（RSS版）")
    print(f"📋 模式: {MODE}")
    print(f"⏰ 运行时间: {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    if not SENDKEY:
        print("❌ 错误: SERVERCHAN_SENDKEY 未设置！")
        return

    config = load_config()
    state = load_state()
    pushed_ids = state.get("pushed_ids", {})
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    total_pushed = 0
    total_new = 0

    settings = config.get("settings", {})
    recent_hours = settings.get("recent_hours", 48)
    max_state = settings.get("state_max_ids", 1000)

    for teacher in config["teachers"]:
        uid = teacher["uid"]
        name = teacher["name"]
        subject = teacher["subject"]
        weibo_name = teacher["weibo_name"]

        print(f"\n📋 正在检查: {subject} - {name} (@{weibo_name})")

        posts = fetch_rss_feed(uid)
        if not posts:
            print(f"  ⚠️  未获取到微博")
            continue

        print(f"  📊 获取到 {len(posts)} 条微博")

        matched_posts = []
        for post in posts:
            post_id = post["id"]
            if post_id in pushed_ids:
                continue
            if not is_recent(post.get("parsed_time", datetime.now(BEIJING_TZ)), hours=recent_hours):
                continue
            if MODE == "question":
                if is_question_post(post, teacher["question_keywords"]):
                    if not is_answer_post(post, teacher["answer_keywords"], teacher["question_keywords"]):
                        matched_posts.append(post)
            elif MODE == "answer":
                if is_answer_post(post, teacher["answer_keywords"], teacher["question_keywords"]):
                    matched_posts.append(post)

        matched_posts = matched_posts[:3]

        for post in matched_posts:
            post_id = post["id"]
            if MODE == "question":
                title = f"📝 {subject}每日一题 - {name}"
                content = format_question_push(teacher, post)
            else:
                title = f"✅ {subject}每日一题答案 - {name}"
                content = format_answer_push(teacher, post)

            success = push_to_serverchan(title, content, SENDKEY)
            if success:
                pushed_ids[post_id] = {
                    "teacher": name,
                    "subject": subject,
                    "mode": MODE,
                    "pushed_at": datetime.now(BEIJING_TZ).isoformat(),
                }
                total_pushed += 1
            total_new += 1

        time.sleep(1)

    if len(pushed_ids) > max_state:
        sorted_ids = sorted(
            pushed_ids.items(),
            key=lambda x: x[1].get("pushed_at", ""),
            reverse=True,
        )
        pushed_ids = dict(sorted_ids[:max_state])

    state["pushed_ids"] = pushed_ids
    state["last_push_date"] = today
    save_state(state)

    # 无论是否匹配到题目，都发一条汇总
    debug_title = f"📊 法考推送运行报告 - {MODE}"
    debug_content = f"""## 运行报告

> 时间: {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}
> 模式: {MODE}

- 匹配到新帖子: {total_new}
- 成功推送: {total_pushed}
- 状态记录数: {len(pushed_ids)}

💡 如果匹配为0，可能是老师最近没发"每日一题"，属正常现象
"""
    push_to_serverchan(debug_title, debug_content, SENDKEY)

    print(f"\n{'='*50}")
    print(f"📊 本次运行汇总:")
    print(f"  - 匹配到新帖子: {total_new}")
    print(f"  - 成功推送: {total_pushed}")
    print(f"  - 状态记录数: {len(pushed_ids)}")
    print(f"{'='*50}")


if __name__ == "__main__":
    run()
