"""歌词引擎:获取 → 对齐 → ASS 逐字字幕生成。

对齐思路(不依赖真实逐字时间戳的来源,用分离产物反推):
  双音轨 MV 里 音轨1=原唱、音轨2=纯伴奏,两者相减 ≈ 人声轨(压缩域不能减,
  解码到 PCM 再减)。人声包络(短时 RMS)与 LRC 的"演唱活动窗口"做互相关,
  找全局偏移;再对每一行在 ±2.5s 内找最近的人声起始(包络上升沿)做行级微调。
  这样 MV 前奏比录音室版长/剪辑过的问题都被吸收掉。

ASS 用 \\kf 逐字 karaoke 标签,libass 渲染,ffmpeg subtitles 滤镜烧进视频。
"""

import math
import re
import subprocess
from pathlib import Path

SR = 22050
HOP = 1024  # ~46ms

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"


# ---------- 歌词获取(网易云严格匹配 → QQ音乐兜底,与 junyao lyrics.js 同策略) ----------

def fetch_lrc(title: str, artist: str) -> tuple[str, str] | None:
    """返回 (lrc文本, 来源描述) 或 None。"""
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": _UA})

    def netease(path):
        r = s.get("https://music.163.com" + path, headers={"Referer": "https://music.163.com"}, timeout=10)
        return r.json()

    want = re.sub(r"\s*[(【].*?[)】]\s*", "", title).strip()
    best = None
    try:
        for page in range(2):
            q = f"{title} {artist}".strip()
            d = netease(f"/api/search/get/web?s={q}&type=1&offset={page*10}&limit=10")
            for it in (d.get("result") or {}).get("songs") or []:
                name = it.get("name") or ""
                arts = ",".join(a.get("name", "") for a in it.get("artists") or [])
                score = 0
                if artist and artist in arts:
                    score += 3
                if want in name or name in want:
                    score += 2
                if re.search(r"伴奏|翻唱|cover|live", name, re.I) or name.strip().endswith("版"):
                    score -= 2
                need = 5 if artist else 2
                if score >= need and (best is None or score > best[0]):
                    best = (score, it)
            if best:
                break
    except Exception:
        best = None
    if best:
        try:
            it = best[1]
            d = netease(f"/api/song/lyric?id={it['id']}&lv=1&kv=1&tv=-1")
            lrc = (d.get("lrc") or {}).get("lyric") or ""
            if lrc.count("[") >= 5:
                arts = ",".join(a.get("name", "") for a in it.get("artists") or [])
                return lrc, f"网易云 · {arts}《{it.get('name')}》"
        except Exception:
            pass

    # QQ 音乐兜底(周杰伦等网易无版权艺人)
    try:
        r = s.get(
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
            params={"w": f"{title} {artist}".strip(), "format": "json", "n": 10},
            headers={"Referer": "https://y.qq.com"}, timeout=10,
        )
        lst = ((r.json().get("data") or {}).get("song") or {}).get("list") or []
        pick = None
        for it in lst:
            name = it.get("songname") or ""
            arts = ",".join(a.get("name", "") for a in it.get("singer") or [])
            if not (want in name or name in want):
                continue
            if artist and artist not in arts:
                continue
            if re.search(r"live|伴奏|翻唱|cover", name, re.I) or name.strip().endswith("版"):
                continue
            pick = it
            break
        if pick:
            r = s.get(
                "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                params={"songmid": pick["songmid"], "format": "json", "nobase64": 1},
                headers={"Referer": "https://y.qq.com"}, timeout=10,
            )
            lrc = r.json().get("lyric") or ""
            if lrc.count("[") >= 5:
                arts = ",".join(a.get("name", "") for a in pick.get("singer") or [])
                return lrc, f"QQ音乐 · {arts}《{pick.get('songname')}》"
    except Exception:
        pass
    return None


def parse_lrc(lrc: str) -> list[dict]:
    out = []
    for line in lrc.splitlines():
        m = re.match(r"^\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\](.*)$", line)
        if not m:
            continue
        t = int(m[1]) * 60 + int(m[2]) + (float(f"0.{m[3]}") if m[3] else 0)
        text = m[4].strip()
        if text and not re.match(r"^(作词|作曲|编曲|制作人|和声|吉他|贝斯|鼓|键盘|弦乐|录音|混音|母带|词曲|出版|OP|SP|MV|Vocal|Guitar|Bass|Drum|Producer|Lyricist|Composer)", text):
            out.append({"t": round(t, 3), "text": text})
    out.sort(key=lambda x: x["t"])
    return out


# ---------- 人声包络与对齐 ----------

def _decode_mono(path: str, track_index: int | None = None) -> "numpy.ndarray":
    import numpy

    cmd = ["ffmpeg", "-v", "error"]
    if track_index is not None:
        cmd += ["-map", f"0:a:{track_index}"]
    cmd += ["-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return numpy.frombuffer(raw, dtype="<f4")


def vocal_envelope_from_dual(path: str) -> "numpy.ndarray | None":
    """双音轨文件:音轨0-音轨1 ≈ 人声,返回短时 RMS 包络。单音轨返回 None。"""
    import numpy

    try:
        a = _decode_mono(path, 0)
        b = _decode_mono(path, 1)
    except Exception:
        return None
    n = min(len(a), len(b))
    if n < SR * 10:
        return None
    import numpy as np

    vocal = np.abs(a[:n] - b[:n])
    frames = len(vocal) // HOP
    if frames < 50:
        return None
    rms = np.sqrt((vocal[: frames * HOP].reshape(frames, HOP) ** 2).mean(axis=1))
    rms /= rms.max() + 1e-9
    return rms


def _expected_activity(lines: list[dict], frames: int, hop_sec: float) -> "numpy.ndarray":
    import numpy as np

    e = np.zeros(frames)
    for i, ln in enumerate(lines):
        start = ln["t"]
        end = lines[i + 1]["t"] if i + 1 < len(lines) else ln["t"] + 6
        dur = min(end - start, 5.0)
        i0, i1 = int(start / hop_sec), int((start + dur) / hop_sec)
        if 0 <= i0 < frames:
            e[i0:max(i0, min(i1, frames))] = 1.0
    return e


def align_lines(lines: list[dict], env: "numpy.ndarray") -> list[dict]:
    """全局偏移(互相关) + 行级上升沿微调,返回对齐后的行。"""
    import numpy as np

    hop = HOP / SR
    frames = len(env)
    exp = _expected_activity(lines, frames, hop)
    if exp.sum() < 20:
        return lines

    best_off, best_score = 0.0, -1.0
    max_shift = int(20 / hop)  # ±20s 搜索窗
    for shift in range(-max_shift, max_shift, 2):  # 步进2帧(~92ms)
        e = np.roll(exp, shift)
        score = float((e * env).sum())
        if score > best_score:
            best_score, best_off = score, shift * hop

    out = []
    # 平滑包络找上升沿:窗口能量超过局部阈值视为"唱起来了"
    smooth = np.convolve(env, np.ones(8) / 8, mode="same")
    for i, ln in enumerate(lines):
        t = ln["t"] + best_off
        nxt = lines[i + 1]["t"] + best_off if i + 1 < len(lines) else t + 6
        lo, hi = max(0, int((t - 2.5) / hop)), min(frames, int((t + 1.5) / hop))
        snapped = t
        if hi > lo + 3:
            seg = smooth[lo:hi]
            thr = max(0.25, float(np.quantile(env, 0.55)))
            rise = np.where(seg > thr)[0]
            if len(rise):
                cand = (lo + rise[0]) * hop
                if abs(cand - t) <= 2.5:
                    snapped = cand
        out.append({"t": round(snapped, 2), "text": ln["text"], "_end": round(nxt, 2)})
    # 单调化:后一行不得早于前一行
    for i in range(1, len(out)):
        if out[i]["t"] < out[i - 1]["t"] + 0.2:
            out[i]["t"] = out[i - 1]["t"] + 0.2
    return out


# ---------- ASS 逐字字幕 ----------

def build_ass(lines: list[dict], title: str, artist: str) -> str:
    """\\kf 逐字:字宽按 CJK=1 / ASCII=0.5 / 空格=0.25 分配行内时长。"""

    def ts(t: float) -> str:
        t = max(0.0, t)
        h = int(t // 3600)
        m = int(t % 3600 // 60)
        s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    def char_w(ch: str) -> float:
        if ch == " ":
            return 0.25
        return 1.0 if ord(ch) > 0x2E7F else 0.55

    events = []
    for ln in lines:
        text = ln["text"]
        dur = max(ln.get("_end", ln["t"] + 3) - ln["t"], 1.2)
        # 歌词行留 0.15s 起始缓冲,末尾提前 0.1s 收,观感更贴唱
        start, end = ln["t"] + 0.15, ln["t"] + dur - 0.1
        weights = [char_w(c) for c in text]
        total = sum(weights) or 1
        kparts, acc = [], 0.0
        for c, w in zip(text, weights):
            cs = int(round((end - start) * 100 * w / total))
            kparts.append(rf"{{\kf{max(cs, 4)}}}{c}")
            acc += cs
        events.append(f"Dialogue: 0,{ts(start)},{ts(end + 0.1)},KTV,,0,0,0,,{ ''.join(kparts)}")

    header = f"""[Script Info]
Title: {artist} - {title}
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: KTV,Noto Sans CJK SC,58,&H0000D7FF,&H00FFFFFF,&H00002060,&H96000000,-1,0,0,0,100,100,0,0,1,2.6,1.2,2,60,60,52,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return header + "\n".join(events) + "\n"


def process(path: Path, title: str, artist: str) -> tuple[str, str] | None:
    """完整流程:取词 → 对齐 → 生成 ASS。返回 (ass文本, 来源) 或 None(没歌词/没对上)。"""
    got = fetch_lrc(title, artist)
    if not got:
        return None
    lrc, source = got
    lines = parse_lrc(lrc)
    if len(lines) < 5:
        return None
    env = vocal_envelope_from_dual(str(path))
    if env is not None:
        lines = align_lines(lines, env)
    return build_ass(lines, title, artist), source
