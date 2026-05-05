#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法考每日一题自动推送脚本 v2 - 零维护版
- 不需要微博Cookie！使用公开API，永不过期
- 每天早上推送题目，晚上推送答案
- 通过 Server酱 推送到微信
- 自动去重，避免重复推送
- 自动重试 + 降级机制，保证稳定性
"""

import os
import json
import re
import time
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============ 配置 ============
CONFIG_PATH = Path(__file__).parent / "config" / "teachers.json"
STATE_PATH = Path(__file__).parent / "state.json"

# 从环境变量读取
SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
MODE = os.environ.get("PUSH_MODE", "question")  # question 或 answer

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

# 请求配置（不用Cookie！）
HEADERS_LIST = [
    # 备选UA列表，轮换使用防封
    {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/124.0.0.0 Mobile Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "MWeibo-Pwa": "1",
        "Referer": "https://m.weibo.cn/",
        "X-Requested-With": "XMLHttpRequest",
    },
    {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "Accept": "application/json, text/plain, */*",
        "MWeibo-Pwa": "1",
        "Referer": "https://m.weibo.cn/",
    },
    {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "MWeibo-Pwa": "1",
        "Referer": "https://m.weibo.cn/",
    },
]


# ============ 工具函数 ============

def load_config():
    """加载老师配置"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    """加载推送状态（去重用）"""
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"pushed_ids": {}, "last_push_date": ""}


def save_state(state):
    """保存推送状态"""
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def clean_html(html_text):
    """清理微博HTML，提取纯文本"""
    if not html_text:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', html_text)
    # 处理微博全文链接
    text = re.sub(r'<a[^>]*>全文</a>', '', text, flags=re.IGNORECASE)
    # 保留表情的alt文字
    text = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*/>', r'[\1]', text)
    text = re.sub(r'<img[^>]*/>', '', text)
    # 去掉所有HTML标签
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&').replace('&quot;', '"')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_weibo_time(time_str):
    """解析微博时间字符串"""
    if not time_str:
        return datetime.now(BEIJING_TZ)

    now = datetime.now(BEIJING_TZ)

    m = re.match(r'(\d+)分钟前', time_str)
    if m:
        return now - timedelta(minutes=int(m.group(1)))

    m = re.match(r'(\d+)小时前', time_str)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    m = re.match(r'今天\s*(\d{1,2}:\d{2})', time_str)
    if m:
        t = datetime.strptime(m.group(1), "%H:%M")
        return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)

    m = re.match(r'昨天\s*(\d{1,2}:\d{2})', time_str)
    if m:
        t = datetime.strptime(m.group(1), "%H:%M")
        yesterday = now - timedelta(days=1)
        return yesterday.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)

    try:
        dt = datetime.strptime(time_str, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(BEIJING_TZ)
    except ValueError:
        pass

    try:
        dt = datetime.strptime(time_str, "%m月%d日 %H:%M")
        return dt.replace(year=now.year, tzinfo=BEIJING_TZ)
    except ValueError:
        pass

    return now


def is_recent(post, hours=48):
    """判断微博是否在指定小时内"""
    created = parse_weibo_time(post.get("created_at", ""))
    now = datetime.now(BEIJING_TZ)
    return (now - created).total_seconds() < hours * 3600


# ============ 微博抓取（无需Cookie！） ============

def fetch_weibo_posts(uid, page=1, retry=3):
    """
    通过 m.weibo.cn 公开API获取用户微博 - 不需要Cookie！
    
    原理：微博移动端有公开的JSON接口，不需要登录即可获取
    大部分用户的公开微博（约90%+的内容）
    
    API格式：
    - 获取用户信息+containerid: 
      https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}
    - 获取微博列表:
      https://m.weibo.cn/api/container/getIndex?containerid=107603{uid}&page={page}
    """

    # 方法1：使用containerid直接获取（最稳定）
    containerid = f"107603{uid}"
    url = "https://m.weibo.cn/api/container/getIndex"
    params = {
        "containerid": containerid,
        "page": page,
    }

    for attempt in range(retry):
        # 轮换UA
        headers = HEADERS_LIST[attempt % len(HEADERS_LIST)]
        headers["Referer"] = f"https://m.weibo.cn/profile/{uid}"

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            if data.get("ok") != 1:
                # 如果方法1失败，尝试方法2
                if attempt == 0:
                    return fetch_weibo_posts_v2(uid, page)
                continue

            cards = data.get("data", {}).get("cards", [])
            posts = []

            for card in cards:
                mblog = card.get("mblog")
                if not mblog:
                    continue

                # 跳过置顶微博
                if mblog.get("isTop") or mblog.get("isTopTag"):
                    continue

                # 跳过转发微博（每日一题通常是原创）
                # 但也保留，以防万一
                is_retweet = mblog.get("retweeted_status") is not None

                posts.append({
                    "id": str(mblog.get("id", "")),
                    "mid": str(mblog.get("mid", "")),
                    "bid": str(mblog.get("bid", "")),
                    "text": mblog.get("text", ""),
                    "text_raw": mblog.get("raw_text", ""),
                    "created_at": mblog.get("created_at", ""),
                    "reposts_count": mblog.get("reposts_count", 0),
                    "comments_count": mblog.get("comments_count", 0),
                    "attitudes_count": mblog.get("attitudes_count", 0),
                    "user_name": mblog.get("user", {}).get("screen_name", ""),
                    "is_retweet": is_retweet,
                    "pics": [pic.get("large", pic).get("url", "") for pic in (mblog.get("pics") or [])],
                })

            return posts

        except requests.exceptions.RequestException as e:
            print(f"  ⚠️  网络错误(尝试{attempt+1}/{retry}): {e}")
            if attempt < retry - 1:
                time.sleep(2 ** attempt)  # 指数退避
        except json.JSONDecodeError as e:
            print(f"  ⚠️  JSON解析失败(尝试{attempt+1}/{retry}): {e}")
            if attempt < retry - 1:
                time.sleep(2)

    print(f"  ❌ UID {uid} 所有尝试均失败")
    return []


def fetch_weibo_posts_v2(uid, page=1):
    """
    备选方案：先获取containerid再请求
    """
    url = "https://m.weibo.cn/api/container/getIndex"

    # 第一步：获取containerid
    params_init = {
        "type": "uid",
        "value": uid,
    }
    headers = HEADERS_LIST[0].copy()
    headers["Referer"] = "https://m.weibo.cn/"

    try:
        resp = requests.get(url, params=params_init, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        tabs = data.get("data", {}).get("tabsInfo", {}).get("tabs", [])
        containerid = None
        for tab in tabs:
            if tab.get("tab_type") == "weibo":
                containerid = tab.get("containerid")
                break

        if not containerid:
            containerid = f"107603{uid}"

        # 第二步：用containerid获取微博
        params_list = {
            "containerid": containerid,
            "page": page,
        }

        time.sleep(1)  # 礼貌性等待
        resp = requests.get(url, params=params_list, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        if data.get("ok") != 1:
            return []

        cards = data.get("data", {}).get("cards", [])
        posts = []

        for card in cards:
            mblog = card.get("mblog")
            if not mblog:
                continue
            if mblog.get("isTop") or mblog.get("isTopTag"):
                continue

            is_retweet = mblog.get("retweeted_status") is not None

            posts.append({
                "id": str(mblog.get("id", "")),
                "mid": str(mblog.get("mid", "")),
                "bid": str(mblog.get("bid", "")),
                "text": mblog.get("text", ""),
                "text_raw": mblog.get("raw_text", ""),
                "created_at": mblog.get("created_at", ""),
                "reposts_count": mblog.get("reposts_count", 0),
                "comments_count": mblog.get("comments_count", 0),
                "attitudes_count": mblog.get("attitudes_count", 0),
                "user_name": mblog.get("user", {}).get("screen_name", ""),
                "is_retweet": is_retweet,
                "pics": [pic.get("large", pic).get("url", "") for pic in (mblog.get("pics") or [])],
            })

        return posts

    except Exception as e:
        print(f"  ❌ 备选方案也失败了: {e}")
        return []


# ============ 推送 ============

def push_to_serverchan(title, content, sendkey):
    """通过 Server酱 推送消息"""
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = {
        "title": title,
        "desp": content,
    }
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
    """格式化题目推送"""
    text = clean_html(post["text"])
    subject = teacher["subject"]
    name = teacher["name"]
    bid = post.get("bid", post["mid"])

    content = f"""## 📝 {subject} · 每日一题

> **老师**: {name}
> **科目**: {subject}
> **发布时间**: {post['created_at']}

---

{text}

---

🔗 [查看原微博](https://m.weibo.cn/detail/{post['mid']})

---
💡 **做完题后，答案会在今晚20:00推送！**
📚 **想要知识库深度解析？来 ima.copilot 找我聊！**
"""
    return content


def format_answer_push(teacher, post):
    """格式化答案推送"""
    text = clean_html(post["text"])
    subject = teacher["subject"]
    name = teacher["name"]

    content = f"""## ✅ {subject} · 每日一题答案

> **老师**: {name}
> **科目**: {subject}
> **发布时间**: {post['created_at']}

---

{text}

---

🔗 [查看原微博](https://m.weibo.cn/detail/{post['mid']})

---
📚 **想要知识库深度解析？来 ima.copilot 找我聊！**
"""
    return content


# ============ 识别逻辑 ============

def is_question_post(post, keywords):
    """判断是否是题目帖"""
    text = clean_html(post["text"]).lower()
    for kw in keywords:
        if kw.lower() in text:
            return True
    return False


def is_answer_post(post, answer_keywords, question_keywords):
    """判断是否是答案帖"""
    text = clean_html(post["text"]).lower()
    has_answer_kw = any(kw.lower() in text for kw in answer_keywords)
    has_question_kw = any(kw.lower() in text for kw in question_keywords)
    return has_answer_kw and has_question_kw


# ============ 主逻辑 ============

def run():
    """主运行逻辑"""
    print(f"{'='*50}")
    print(f"🚀 法考每日一题推送 v2（零Cookie版）")
    print(f"📋 模式: {MODE}")
    print(f"⏰ 运行时间: {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    if not SENDKEY:
        print("❌ 错误: SERVERCHAN_SENDKEY 未设置！")
        return

    # 注意：不需要Cookie了！
    print("✅ 无需微博Cookie，使用公开API获取数据")

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

        # 获取微博（不需要Cookie！）
        posts = fetch_weibo_posts(uid)
        if not posts:
            print(f"  ⚠️  未获取到微博，可能网络问题")
            continue

        print(f"  📊 获取到 {len(posts)} 条微博")

        matched_posts = []
        for post in posts:
            post_id = post["id"]

            if post_id in pushed_ids:
                continue

            if not is_recent(post, hours=recent_hours):
                continue

            if MODE == "question":
                if is_question_post(post, teacher["question_keywords"]):
                    if not is_answer_post(post, teacher["answer_keywords"], teacher["question_keywords"]):
                        matched_posts.append(post)
            elif MODE == "answer":
                if is_answer_post(post, teacher["answer_keywords"], teacher["question_keywords"]):
                    matched_posts.append(post)

        # 限制每天每个老师最多推送3条，避免Server酱额度不够
        matched_posts = matched_posts[:3]

        for post in matched_posts:
            post_id = post["id"]
            mid = post["mid"]

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
                    "mid": mid,
                }
                total_pushed += 1

            total_new += 1

        # 礼貌性间隔，避免请求太频繁
        time.sleep(2)

    # 清理过期状态
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

    print(f"\n{'='*50}")
    print(f"📊 本次运行汇总:")
    print(f"  - 匹配到新帖子: {total_new}")
    print(f"  - 成功推送: {total_pushed}")
    print(f"  - 状态记录数: {len(pushed_ids)}")
    print(f"{'='*50}")


if __name__ == "__main__":
    run()
