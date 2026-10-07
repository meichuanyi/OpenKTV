"""ktv-tools:junyao-ktv 的在线搜索 / 下载工具服务。

职责刻意收窄:B站 + YouTube 搜索、yt-dlp 下载、文件名规范化后落曲库目录。
下载/后续的分离任务走同一条串行队列(单 worker),适配 NAS 的有限算力;
任务状态存内存,junyao 前端通过 /tasks 轮询展示进度。
"""

import os
import shutil
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from . import bilibili

LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/library"))
STAGING_DIR = Path("/tmp/ktv-staging")
QUALITIES = {"480": 480, "720": 720, "1080": 1080}
# 仅 YouTube 走代理(B站等国内源必须直连,全局代理会被 CDN 掐 TLS)。
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()
_PROXY_DOMAINS = ("youtube.com", "youtu.be", "googlevideo.com")


def _proxy_args(url: str) -> list[str]:
    if YTDLP_PROXY and any(d in url for d in _PROXY_DOMAINS):
        return ["--proxy", YTDLP_PROXY]
    return []
# 文件名里的非法字符(NTFS/Windows 习惯 + junyao scanner 的分隔符歧义)
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

app = FastAPI(title="ktv-tools")

# ---------- 任务队列(串行:一次一首,不跟转码抢 CPU) ----------

tasks: dict[str, dict] = {}
_task_q: "queue.Queue[str]" = queue.Queue()
_tasks_lock = threading.Lock()


def _register(payload: dict) -> str:
    tid = uuid.uuid4().hex[:12]
    task = {
        "id": tid,
        "created_at": time.time(),
        "status": "queued",
        "progress": 0,
        **payload,
    }
    with _tasks_lock:
        tasks[tid] = task
    _task_q.put(tid)
    return tid


def _worker():
    while True:
        tid = _task_q.get()
        task = tasks.get(tid)
        if not task:
            continue
        try:
            task["status"] = "downloading"
            downloaded = _ytdlp_download(task)
            task["status"] = "moving"
            task["result"] = _finalize(downloaded, task)
            task["progress"] = 100
            task["status"] = "done"
        except Exception as e:  # noqa: BLE001 —— 队列 worker 兜一切异常
            task["status"] = "failed"
            task["error"] = str(e)[:500]
        finally:
            _cleanup_staging()


threading.Thread(target=_worker, daemon=True).start()


# ---------- 搜索 ----------


@app.get("/search")
def search(q: str, source: str = "auto", limit: int = 20):
    if not q.strip():
        raise HTTPException(400, "关键词为空")
    limit = max(1, min(limit, 30))
    bili_err = None
    if source in ("auto", "bilibili"):
        try:
            hits, _ = bilibili.search(q, limit=limit)
            if hits or source == "bilibili":
                return {"source": "bilibili", "results": hits}
        except bilibili.BilibiliUnavailable as e:
            bili_err = str(e)
    yt = _youtube_search(q, limit)
    if yt:
        return {"source": "youtube", "results": yt}
    if bili_err:
        raise HTTPException(502, f"B站搜索失败: {bili_err},YouTube 也没有结果")
    return {"source": source, "results": []}


def _youtube_search(q: str, limit: int) -> list[dict]:
    """yt-dlp 的 ytsearch 平铺抽取:只要元数据,不下载。"""
    cmd = [
        "yt-dlp", "-J", "--flat-playlist", "--no-warnings",
        *_proxy_args("youtube.com"),
        f"ytsearch{limit}:{q}",
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, env=_clean_env()
        )
        if out.returncode != 0:
            return []
        import json
        data = json.loads(out.stdout or "{}")
    except Exception:
        return []
    results = []
    for e in data.get("entries") or []:
        if not e.get("id"):
            continue
        secs = e.get("duration") or 0
        duration = (
            f"{secs // 60}:{secs % 60:02d}" if isinstance(secs, (int, float)) and secs else ""
        )
        thumb = ""
        for t in e.get("thumbnails") or []:
            thumb = t.get("url", "") or thumb
        results.append(
            {
                "video_id": e["id"],
                "title": (e.get("title") or "").strip(),
                "url": f"https://www.youtube.com/watch?v={e['id']}",
                "channel": (e.get("channel") or e.get("uploader") or "").strip(),
                "duration": duration,
                "thumbnail": thumb,
                "source": "youtube",
            }
        )
    return results


@app.get("/thumbnail")
def thumbnail(url: str):
    got = bilibili.fetch_thumbnail(url)
    if not got:
        raise HTTPException(404, "封面获取失败")
    body, content_type = got
    return Response(content=body, media_type=content_type)


# ---------- 下载 ----------


class DownloadBody(BaseModel):
    url: str
    title: str
    artist: str = ""
    quality: str = "720"


@app.post("/download")
def download(body: DownloadBody):
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "非法 URL")
    if body.quality not in QUALITIES:
        body.quality = "720"
    artist, title = _guess_artist_title(body.artist.strip(), body.title.strip())
    if not title:
        raise HTTPException(400, "歌名为空")
    tid = _register(
        {
            "url": body.url,
            "artist": artist,
            "title": title,
            "quality": body.quality,
        }
    )
    return {"task_id": tid}


def _clean_env() -> dict:
    """子进程环境去掉代理变量,防止国内源被宿主机代理误伤。"""
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    return env


def _ytdlp_download(task: dict) -> Path:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(STAGING_DIR / "dl.%(ext)s")
    height = QUALITIES[task["quality"]]
    fmt = (
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/best"
    )
    cmd = [
        "yt-dlp",
        "--newline",
        "--progress",
        "--progress-template", "download:PROG %(progress._percent_str)s",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-part",
        "--concurrent-fragments", "8",
        "--socket-timeout", "20",
        "--retries", "8",
        "--fragment-retries", "8",
        *_proxy_args(task["url"]),
        "-o", out_tmpl,
        task["url"],
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(STAGING_DIR), env=_clean_env(),
        start_new_session=True,
    )

    def _feed(task: dict, proc) -> None:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("PROG"):
                try:
                    task["progress"] = max(
                        0, min(95, int(float(line[4:].strip().rstrip("%"))))
                    )
                    task["_last_progress_ts"] = time.time()
                except ValueError:
                    pass

    reader = threading.Thread(target=_feed, args=(task, proc), daemon=True)
    reader.start()
    # 看门狗:B站 CDN 偶尔抽到坏节点会无限悬挂,120 秒没有任何输出就杀掉报错。
    last_output = time.time()
    stall_limit = int(os.environ.get("DOWNLOAD_STALL_TIMEOUT", "120"))
    while proc.poll() is None:
        time.sleep(2)
        with _tasks_lock:
            idle = time.time() - last_output
        # reader 线程更新过进度即视为活跃(用任务进度键的写入时间近似)
        if task.get("_last_progress_ts", 0) > last_output:
            last_output = task["_last_progress_ts"]
        if time.time() - last_output > stall_limit:
            os.killpg(os.getpgid(proc.pid), 15)
            raise RuntimeError(f"下载停滞超过{stall_limit}秒,已终止(通常是 CDN 节点悬挂),可重试")
    reader.join(timeout=5)
    code = proc.returncode
    files = [p for p in STAGING_DIR.iterdir() if p.is_file()]
    if code != 0 or not files:
        raise RuntimeError(f"yt-dlp 退出码 {code},未产出文件")
    return max(files, key=lambda p: p.stat().st_mtime)


def _sanitize(name: str) -> str:
    return _UNSAFE_CHARS.sub(" ", name).strip(" .") or "未命名"


def _guess_artist_title(artist: str, title: str) -> tuple[str, str]:
    """歌手没填时,从视频标题里猜:优先 "歌手《歌名》",其次 "歌手 - 歌名"。

    B站标题常见 "周杰伦《晴天》MV官方版"、"晴天- 周杰伦(KTV版)" 这类;
    猜不出就整段当歌名,junyao scanner 本来也接受纯歌名文件。
    """
    title = _sanitize(title)
    if artist:
        return _sanitize(artist), title
    cleaned = re.sub(r"^[【\[][^】\]]{1,12}[】\]]\s*", "", title)
    m = re.search(r"([\w\u4e00-\u9fff·&'()]+?)\s*《(.+?)》", cleaned)
    if m:
        return m.group(1).strip(), _clean_noise(m.group(2))
    for sep in (" - ", " – ", " — ", "- ", " -"):
        if sep in cleaned:
            head, tail = cleaned.split(sep, 1)
            head, tail = head.strip(), tail.strip()
            if head and tail:
                return head, _clean_noise(tail)
    return "", _clean_noise(cleaned)


def _clean_noise(title: str) -> str:
    """去掉歌名尾巴上的 MV/KTV/官方版 等宣传词。"""
    return re.sub(
        r"\s*[\(\[【]?(MV|KTV|官方版|官方MV|完整版|高清)(版)?[\)\]】]?\s*$",
        "", title, flags=re.IGNORECASE,
    ).strip() or title


def _finalize(file: Path, task: dict) -> dict:
    """规范化命名 → 探测音轨数 → 落曲库目录。"""
    name = _sanitize(f"{task['artist']} - {task['title']}" if task["artist"] else task["title"])
    ext = file.suffix.lower() or ".mp4"
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    target = LIBRARY_DIR / f"{name}{ext}"
    if target.exists():
        target = LIBRARY_DIR / f"{name} [{task.get('url', '').rstrip('/').split('/')[-1][:24]}]{ext}"
    shutil.move(str(file), str(target))
    target = Path(target)
    return {
        "filename": target.name,
        "audio_tracks": _probe_audio_tracks(target),
    }


def _probe_audio_tracks(file: Path) -> int:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(file),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return len([l for l in out.stdout.splitlines() if l.strip()])
    except Exception:
        return 1


def _cleanup_staging():
    for p in STAGING_DIR.glob("*") if STAGING_DIR.exists() else []:
        try:
            p.unlink()
        except OSError:
            pass


# ---------- 任务状态 ----------


@app.get("/tasks")
def list_tasks():
    with _tasks_lock:
        items = sorted(tasks.values(), key=lambda t: t["created_at"], reverse=True)
    return [
        {k: t.get(k) for k in ("id", "title", "artist", "status", "progress", "error", "result")}
        for t in items[:50]
    ]


@app.get("/tasks/{tid}")
def get_task(tid: str):
    task = tasks.get(tid)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {k: task.get(k) for k in ("id", "title", "artist", "status", "progress", "error", "result")}


@app.get("/health")
def health():
    return {"ok": True, "library": str(LIBRARY_DIR)}
