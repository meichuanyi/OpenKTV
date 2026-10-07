"""B站搜索:站点 API 直搜(标题/UP主/时长/封面齐全)。

yt-dlp 自带的 bilisearch 只吐 av 号,做不了搜索结果页;B站 API 又对无
cookie 的请求回 -412 风控,所以先用首页暖一个带 cookie 的会话再查。
(移植自 maiba-ktv pikaraoke/lib/bilibili.py,只保留搜索与封面代理。)
"""

import html
import logging
import re
import threading
import time

BILIBILI_HOME = "https://www.bilibili.com/"
SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
VIDEO_URL = "https://www.bilibili.com/video/{bvid}"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_SESSION_TTL_SECONDS = 1800
_TIMEOUT_SECONDS = 15

_EM_TAG = re.compile(r"</?em[^>]*>")
_BV_ID = re.compile(r"BV[0-9A-Za-z]{10}")

_session = None
_session_created = 0.0
_session_lock = threading.Lock()


class BilibiliUnavailable(Exception):
    """B站不可达或拒绝请求。"""


def _new_session():
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": BILIBILI_HOME})
    session.trust_env = False  # B站直连,不走代理环境变量
    try:
        session.get(BILIBILI_HOME, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        logging.warning(f"B站 cookie 预热失败(搜索仍可尝试): {e}")
    return session


def _get_session(force_new: bool = False):
    global _session, _session_created
    with _session_lock:
        stale = time.time() - _session_created > _SESSION_TTL_SECONDS
        if _session is None or stale or force_new:
            _session = _new_session()
            _session_created = time.time()
        return _session


def clean_title(title: str) -> str:
    return html.unescape(_EM_TAG.sub("", title or "")).strip()


def parse_duration(duration: str) -> str:
    duration = (duration or "").strip()
    if not duration:
        return ""
    parts = duration.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return ""
    if len(numbers) == 2:
        return f"{numbers[0]}:{numbers[1]:02d}"
    if len(numbers) == 3:
        return f"{numbers[0]}:{numbers[1]:02d}:{numbers[2]:02d}"
    return duration


def thumbnail_url(pic: str) -> str:
    pic = (pic or "").strip()
    if pic.startswith("//"):
        return "https:" + pic
    return pic


def search(query: str, limit: int = 20, page: int = 1) -> tuple[list[dict], int]:
    """搜B站视频,返回 (结果列表, API 报告的总命中数)。"""
    import requests

    params = {"search_type": "video", "keyword": query, "page": max(page, 1)}
    last_error: Exception | None = None
    for attempt in range(2):
        session = _get_session(force_new=attempt > 0)
        try:
            response = session.get(SEARCH_API, params=params, timeout=_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as e:
            last_error = e
            continue
        if payload.get("code") != 0:
            last_error = BilibiliUnavailable(
                f"code {payload.get('code')}: {payload.get('message')}"
            )
            continue
        results = (payload.get("data") or {}).get("result") or []
        return _shape_results(results, limit), len(results)
    raise BilibiliUnavailable(str(last_error))


def _shape_results(results: list[dict], limit: int) -> list[dict]:
    shaped: list[dict] = []
    for item in results:
        bvid = (item.get("bvid") or "").strip()
        if not bvid:
            continue
        shaped.append(
            {
                "video_id": bvid,
                "title": clean_title(item.get("title", "")),
                "url": VIDEO_URL.format(bvid=bvid),
                "channel": (item.get("author") or "").strip(),
                "duration": parse_duration(item.get("duration", "")),
                "thumbnail": thumbnail_url(item.get("pic", "")),
                "source": "bilibili",
            }
        )
        if len(shaped) >= limit:
            break
    return shaped


def fetch_thumbnail(url: str) -> tuple[bytes, str] | None:
    """服务端代理下载封面;B站 CDN 拒绝跨站直链,必须带 Referer。"""
    import requests

    if not url.startswith(("http://", "https://")):
        return None
    try:
        response = _get_session().get(
            url, timeout=_TIMEOUT_SECONDS, headers={"Referer": BILIBILI_HOME}
        )
        response.raise_for_status()
    except requests.RequestException:
        return None
    content_type = response.headers.get("Content-Type", "image/jpeg")
    if not content_type.startswith("image/"):
        return None
    return response.content, content_type
