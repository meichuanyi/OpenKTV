"""ktv-tools:OpenKTV 的在线搜索 / 下载工具服务。

职责刻意收窄:B站 + YouTube 搜索、yt-dlp 下载、文件名规范化后落曲库目录。
下载/后续的分离任务走同一条串行队列(单 worker),适配 NAS 的有限算力;
任务状态存内存,主服务前端通过 /tasks 轮询展示进度。
"""

import os
import shutil
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from . import bilibili
from . import lyric_engine

LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/library"))
STAGING_DIR = Path("/tmp/ktv-staging")
QUALITIES = {"480": 480, "720": 720, "1080": 1080}
# 仅 YouTube 走代理(B站等国内源必须直连,全局代理会被 CDN 掐 TLS)。
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()
_PROXY_DOMAINS = ("youtube.com", "youtu.be", "googlevideo.com")
# Demucs 分离配置:模型、单首限时、下载完是否自动接续分离(合成双音轨)。
DEMUCS_MODEL = os.environ.get("DEMUCS_MODEL", "htdemucs")
SEPARATE_TIMEOUT = int(os.environ.get("SEPARATE_TIMEOUT", "1800"))
AUTO_SEPARATE = os.environ.get("AUTO_SEPARATE", "1") == "1"
# 自动烧录歌词开关:烧进画面后播放时无法关闭,不想字幕就关掉这个,
# 电视浮层歌词(可隐藏/可校准)会自动接管。手动 /lyricize 不受此开关限制。
AUTO_LYRICS = os.environ.get("AUTO_LYRICS", "1") == "1"


def _proxy_args(url: str) -> list[str]:
    if YTDLP_PROXY and any(d in url for d in _PROXY_DOMAINS):
        return ["--proxy", YTDLP_PROXY]
    return []
# 文件名里的非法字符(NTFS/Windows 习惯 + 曲库 scanner 的分隔符歧义)
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
            if task.get("kind") == "separate":
                _run_separate_task(task)
            elif task.get("kind") == "lyricize":
                _run_lyricize_task(task)
            else:
                _run_download_task(task)
        except Exception as e:  # noqa: BLE001 —— 队列 worker 兜一切异常
            task["status"] = "failed"
            task["error"] = str(e)[:500]
        finally:
            _cleanup_staging()


def _run_download_task(task: dict):
    task["status"] = "downloading"
    downloaded = _ytdlp_download(task)
    task["status"] = "moving"
    task["result"] = _finalize(downloaded, task)
    # 单音轨且开启自动分离:同一任务接续 Demucs 阶段,产出双音轨后才算 done。
    if task["result"].get("audio_tracks") == 1 and AUTO_SEPARATE:
        merged = _separate_and_merge(LIBRARY_DIR / task["result"]["filename"], task)
        task["result"].update(merged)
    final_path = LIBRARY_DIR / task["result"]["filename"]
    if not AUTO_LYRICS:
        task["result"]["lyrics"] = "off"  # 全局开关关闭,浮层歌词接管
    elif task.get("ktv_source"):
        # KTV版素材自带专业字幕(通常还自带双音轨),不需要 AI 烧录
        (LIBRARY_DIR / f"{final_path.name}.ktv-ok").write_text("KTV版素材自带字幕", encoding="utf-8")
        task["result"]["lyrics"] = "native-ktv"
    elif task["result"].get("audio_tracks") == 2:
        task["result"].update(_burn_lyrics(final_path, task))
    task["progress"] = 100
    task["status"] = "done"


def _run_separate_task(task: dict):
    merged = _separate_and_merge(LIBRARY_DIR / task["file"], task)
    result = task.get("result") or {}
    result.update(merged)
    task["result"] = result
    if AUTO_LYRICS and result.get("audio_tracks") == 2:
        result.update(_burn_lyrics(LIBRARY_DIR / task["file"], task))
    task["progress"] = 100
    task["status"] = "done"


def _vocals_sidecar(file: Path) -> Path | None:
    """真·人声stem旁车文件;没有则 None(烧录时退回相减法,质量打折)。"""
    p = LIBRARY_DIR / f"{Path(file).stem}.vocals.m4a"
    return p if p.is_file() else None


def _ensure_vocals_stem(src: Path, task: dict) -> Path | None:
    """给存量双音轨MV补人声stem:没有就跑一次 demucs(只取人声,不动曲库文件)。"""
    existing = _vocals_sidecar(src)
    if existing:
        return existing
    task["status"] = "separating"
    task["progress"] = 40
    out = LIBRARY_DIR / f"{src.stem}.vocals.m4a"
    tmp = STAGING_DIR / "vocals.m4a"
    STAGING_DIR.mkdir(parents=True, exist_ok=True)  # demucs 直接写这个路径,目录不存在会静默失败
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.demucs_sep", str(src),
         str(STAGING_DIR / "instrumental.m4a"), DEMUCS_MODEL, str(tmp)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd="/srv", env=_clean_env(),
    )
    timer = threading.Timer(SEPARATE_TIMEOUT, proc.kill)
    timer.start()
    for line in proc.stdout:
        digits = line.strip().rsplit("|", 1)[-1]
        if line.startswith("[sep]|") and digits.isdigit():
            task["progress"] = 40 + int(digits) * 45 // 100
    code = proc.wait()
    timer.cancel()
    if code != 0 or not tmp.is_file():
        return None
    shutil.move(str(tmp), str(out))
    return out


def _run_lyricize_task(task: dict):
    src = LIBRARY_DIR / task["file"]
    _ensure_vocals_stem(src, task)
    task["status"] = "lyrics"
    task["progress"] = 10
    info = _burn_lyrics(src, task)
    task["result"] = {"filename": src.name, "audio_tracks": _probe_audio_tracks(src), **info}
    task["progress"] = 100
    task["status"] = "done"


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
    ktv: bool = False  # KTV版素材:自带烧录字幕,跳过歌词内嵌;双音轨则连分离也跳过


@app.post("/download")
def download(body: DownloadBody):
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "非法 URL")
    if body.quality not in QUALITIES:
        body.quality = "720"
    artist, title = _guess_artist_title(body.artist.strip(), body.title.strip())
    if not title:
        raise HTTPException(400, "歌名为空")
    ktv_source = body.ktv or bool(re.search(r"KTV|卡拉OK|卡拉ok| Karaoke", body.title, re.I))
    tid = _register(
        {
            "url": body.url,
            "artist": artist,
            "title": title,
            "quality": body.quality,
            "ktv_source": ktv_source,
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
            # 留最后 800 字符输出,失败时附在错误里(否则 yt-dlp 的报错无从排查)
            task["_tail"] = ((task.get("_tail") or "") + "\n" + line)[-800:]
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
        tail = (task.get("_tail") or "").strip()[-400:]
        raise RuntimeError(f"yt-dlp 退出码 {code},未产出文件。输出尾部: {tail}")
    return max(files, key=lambda p: p.stat().st_mtime)


def _sanitize(name: str) -> str:
    return _UNSAFE_CHARS.sub(" ", name).strip(" .") or "未命名"


def _guess_artist_title(artist: str, title: str) -> tuple[str, str]:
    """歌手没填时,从视频标题里猜:优先 "歌手《歌名》",其次 "歌手 - 歌名"。

    B站标题常见 "周杰伦《晴天》MV官方版"、"晴天- 周杰伦(KTV版)" 这类;
    猜不出就整段当歌名,曲库 scanner 本来也接受纯歌名文件。
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
    """递归清空暂存区(分离阶段会产生子目录)。"""
    if not STAGING_DIR.exists():
        return
    for p in STAGING_DIR.iterdir():
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        except OSError:
            pass


def _run_ffmpeg(args: list[str], timeout: int) -> None:
    out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=_clean_env())
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {out.stderr[-400:]}")


def _separate_and_merge(src: Path, task: dict) -> dict:
    """Demucs 分离伴奏并合成双音轨 MKV:音轨1=原唱,音轨2=AI 伴奏。

    幂等:已是双音轨的文件直接返回,不重复算(NAS 上一首 5-10 分钟,浪费不起)。
    分离跑在独立子进程里(app/demucs_sep.py):torch 的 OOM/崩溃不连累本服务,
    且带真实分段进度([sep]|NN → 40%~85%)。中间产物用完即删。
    """
    if not src.is_file():
        raise RuntimeError(f"曲库中没有该文件: {src.name}")
    if _probe_audio_tracks(src) >= 2:
        return {"filename": src.name, "audio_tracks": 2, "skipped": True}

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    instrumental = STAGING_DIR / "instrumental.m4a"

    task["status"] = "separating"
    task["progress"] = 40
    vocals_sidecar = LIBRARY_DIR / f"{src.stem}.vocals.m4a"
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "app.demucs_sep",
            str(src), str(instrumental), DEMUCS_MODEL,
            *( [str(STAGING_DIR / "vocals.m4a")] if not vocals_sidecar.exists() else [] ),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd="/srv", env=_clean_env(),
    )
    # 总超时看门狗:readline 会阻塞,首次运行还要下模型权重,靠 Timer 兜底。
    timer = threading.Timer(SEPARATE_TIMEOUT, proc.kill)
    timer.start()
    for line in proc.stdout:
        digits = line.strip().rsplit("|", 1)[-1]
        if line.startswith("[sep]|") and digits.isdigit():
            task["progress"] = 40 + int(digits) * 45 // 100
    code = proc.wait()
    timer.cancel()
    if code != 0 or not instrumental.is_file():
        err = (proc.stderr.read() or "")[-300:] if proc.stderr else ""
        raise RuntimeError(f"Demucs 分离失败(退出码 {code}): {err}")

    task["status"] = "merging"
    task["progress"] = 90
    merged = STAGING_DIR / "merged.mkv"
    _run_ffmpeg(
        [
            "ffmpeg", "-y", "-i", str(src), "-i", str(instrumental),
            "-map", "0:v?", "-map", "0:a:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a:0", "aac", "-b:a", "192k", "-c:a:1", "copy",
            "-disposition:a:0", "default", "-disposition:a:1", "none",
            str(merged),
        ],
        900,
    )

    final = LIBRARY_DIR / f"{src.stem}.mkv"
    src.unlink(missing_ok=True)
    if final.exists():
        final.unlink()
    shutil.move(str(merged), str(final))
    staged_vocals = STAGING_DIR / "vocals.m4a"
    if staged_vocals.is_file() and not vocals_sidecar.exists():
        shutil.move(str(staged_vocals), str(vocals_sidecar))  # 歌词对齐/AI评分的人声参考
    return {"filename": final.name, "audio_tracks": _probe_audio_tracks(final)}


def _burn_lyrics(file: Path, task: dict) -> dict:
    """取词→人声对齐→ASS逐字→烧进视频(音轨无损copy)。成功后写 .ktv-ok 标记,
    主服务的歌词接口看到标记就让浮层让位(字幕已经在画面里了)。"""
    try:
        title = task.get("title") or file.stem
        artist = task.get("artist") or ""
        task["status"] = "lyrics"
        task["progress"] = 92
        got = lyric_engine.process(file, title, artist, vocals_path=_vocals_sidecar(file))
        if not got:
            return {"lyrics": "no-lyrics"}
        ass_text, source, fit_summary = got
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        ass_path = STAGING_DIR / "lyric.ass"
        ass_path.write_text(ass_text, encoding="utf-8")
        out = STAGING_DIR / "burned.mkv"
        # 用系统 ffmpeg(带 libass);静态 ffmpeg 无字幕滤镜。中文字体由镜像内置。
        cmd = [
            "/usr/bin/ffmpeg", "-y", "-v", "error",
            "-i", str(file),
            "-map", "0",  # 默认流选择只会留一条音轨,-map 0 保住双音轨
            "-vf", f"ass={ass_path}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "copy",
            "-max_muxing_queue_size", "4096",
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=_clean_env())
        if r.returncode != 0 or not out.is_file():
            raise RuntimeError(f"烧录失败: {r.stderr[-300:]}")
        shutil.move(str(out), str(file))  # 跨设备(/tmp→卷)不能用 rename/replace
        (LIBRARY_DIR / f"{file.name}.ktv-ok").write_text(source, encoding="utf-8")
        return {"lyrics": "burned", "lyric_source": source, "lyric_fit": fit_summary}
    except Exception as e:  # 烧录失败不影响歌曲可用性
        return {"lyrics": "failed", "lyric_error": str(e)[:200]}


class LyricizeBody(BaseModel):
    filename: str


@app.post("/lyricize")
def lyricize(body: LyricizeBody):
    """给已有双音轨 MV 补内嵌逐字歌词(需先分离,对齐依赖原唱-伴奏的人声差)。"""
    fname = Path(str(body.filename).replace("\\", "/")).name
    src = LIBRARY_DIR / fname
    if not src.is_file():
        raise HTTPException(404, f"曲库中没有该文件: {fname}")
    if _probe_audio_tracks(src) < 2:
        raise HTTPException(400, "先补伴唱(双音轨)才能精准对齐歌词")
    if (LIBRARY_DIR / f"{fname}.ktv-ok").exists():
        return {"task_id": None, "skipped": True}
    artist, title = _guess_artist_title("", src.stem)
    tid = _register({"kind": "lyricize", "file": fname, "artist": artist, "title": title or fname})
    return {"task_id": tid}


class SeparateBody(BaseModel):
    filename: str


@app.post("/separate")
def separate(body: SeparateBody):
    """对曲库内已有的单音轨文件补伴唱(主服务传来的 filename 可能带 library1/ 前缀)。"""
    fname = Path(str(body.filename).replace("\\", "/")).name  # 只取文件名,防目录穿越
    src = LIBRARY_DIR / fname
    if not src.is_file():
        raise HTTPException(404, f"曲库中没有该文件: {fname}")
    if _probe_audio_tracks(src) >= 2:
        return {"task_id": None, "skipped": True}
    artist, title = _guess_artist_title("", src.stem)
    tid = _register(
        {"kind": "separate", "file": fname, "artist": artist, "title": title or fname}
    )
    return {"task_id": tid}


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
