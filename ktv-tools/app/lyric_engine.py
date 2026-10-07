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


# ---------- 人声短语检测与对齐(移植自 maiba-ktv lyrics_align.py,GPL-3.0) ----------

NOISE_FLOOR_DB = -30      # 人声低于此读作静音(分离残留的底噪在其上)
MIN_SILENCE_SECONDS = 0.2  # 短于此的间隙是句中换气,不是分行
MIN_SPAN_SECONDS = 0.3     # 短于此的片段是伪影,不是唱句
ANCHOR_TOLERANCE = 1.2     # 歌词行与唱句起点相距此内才锚定
MIN_ANCHOR_RATIO = 0.2
MIN_ANCHORS = 3
_REFINE_PASSES = 2
MAX_SHIFT_SECONDS = 90.0


def _decode_mono(path: str, track_index: int | None = None) -> "numpy.ndarray":
    import numpy

    # -map 是输出选项,必须放在 -i 之后;放反了 ffmpeg 静默返回 234(第一版对齐
    # 从未生效的根因),异常被上层吞掉后直接用了未对齐的 LRC 时间轴。
    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if track_index is not None:
        cmd += ["-map", f"0:a:{track_index}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return numpy.frombuffer(raw, dtype="<f4")


def _vocals_wav(path: str, out_wav: str) -> bool:
    """双音轨相减得人声,写 mono wav 供 silencedetect 用。"""
    import numpy as np

    try:
        a = _decode_mono(path, 0)
        b = _decode_mono(path, 1)
    except Exception:
        return False
    n = min(len(a), len(b))
    if n < SR * 10:
        return False
    vocal = np.clip(a[:n] - b[:n], -1.0, 1.0)
    pcm = (vocal * 32767).astype("<i2").tobytes()
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1",
         "-i", "pipe:0", out_wav],
        input=pcm, capture_output=True, timeout=180,
    )
    return r.returncode == 0


def detect_spans(vocals_wav: str) -> list[tuple[float, float]]:
    """ffmpeg silencedetect 提取唱句区间(起,止)。伴奏已减掉,非静音即演唱。"""
    import re as _re

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", vocals_wav,
        "-af", f"silencedetect=noise={NOISE_FLOOR_DB}dB:d={MIN_SILENCE_SECONDS}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    starts = [(float(m[1]), "s") for m in _re.finditer(r"silence_start: ([\d.]+)", result.stderr)]
    ends = [(float(m[1]), "e") for m in _re.finditer(r"silence_end: ([\d.]+)", result.stderr)]
    events = sorted(starts + ends)
    if not events:
        return []
    spans = []
    open_at = 0.0 if events[0][1] == "s" else None
    for at, kind in events:
        if kind == "s":
            if open_at is not None and at > open_at:
                spans.append((open_at, at))
            open_at = None
        else:
            open_at = at
    if open_at is not None and events[-1][0] > open_at:
        spans.append((open_at, events[-1][0]))
    return [sp for sp in spans if sp[1] - sp[0] >= MIN_SPAN_SECONDS]


def first_phrase(spans, min_sound: float = 6.0, window: float = 15.0):
    """第一处"真在唱"的位置:跳过前奏里孤零零的一句哼唱。"""
    for i, (start, _) in enumerate(spans):
        covered = sum(
            min(e, start + window) - max(b, start)
            for b, e in spans[i:] if b < start + window and e > start
        )
        if covered >= min_sound:
            return start
    return spans[0][0] if spans else None


def _monotonic_anchors(times, starts, tol):
    anchors = []
    cursor = 0
    for idx, t in enumerate(times):
        while cursor < len(starts) and starts[cursor] < t - tol:
            cursor += 1
        if cursor < len(starts) and abs(starts[cursor] - t) <= tol:
            anchors.append((idx, starts[cursor]))
            cursor += 1
    return anchors


def _warp(times, anchors):
    warped = list(times)
    for pos, (idx, target) in enumerate(anchors):
        warped[idx] = target
        prev = anchors[pos - 1] if pos else None
        if prev is None:
            off = target - times[idx]
            for i in range(idx):
                warped[i] = times[i] + off
            continue
        pi, pt = prev
        old_span = times[idx] - times[pi]
        new_span = target - pt
        for i in range(pi + 1, idx):
            warped[i] = pt + (times[i] - times[pi]) / old_span * new_span if old_span > 0 else pt
    if anchors:
        li, lt = anchors[-1]
        off = lt - times[li]
        for i in range(li + 1, len(times)):
            warped[i] = times[i] + off
    running = 0.0
    for i, v in enumerate(warped):
        running = max(running, max(v, 0.0))
        warped[i] = running
    return warped


def align_lines(lines: list[dict], spans: list[tuple[float, float]]) -> tuple[list[dict], str]:
    """LRC 行时间贴合实际演唱:整体平移 → 短语锚点 → 分段线性变形(两轮重匹配)。

    返回 (对齐后的行, 拟合摘要)。行内附带 spans 供扫光区间使用。
    """
    times = [l["t"] for l in lines]
    if not times or not spans:
        for l in lines:
            l["_end"] = l["t"] + _est_line_dur(l["text"])
        return lines, "fit=none"

    starts = [s for s, _ in spans]
    onset = first_phrase(spans)
    shift = 0.0
    if onset is not None:
        cand = round(onset - times[0], 2)
        if abs(cand) <= MAX_SHIFT_SECONDS:
            shift = cand
    shifted = [max(t + shift, 0.0) for t in times]
    anchors = _monotonic_anchors(shifted, starts, ANCHOR_TOLERANCE)
    method = "shift"
    enough = len(anchors) >= MIN_ANCHORS and len(anchors) >= len(times) * MIN_ANCHOR_RATIO
    if enough:
        method = "warp"
        times2 = _warp(shifted, anchors)
        for _ in range(_REFINE_PASSES):
            refined = _monotonic_anchors(times2, starts, ANCHOR_TOLERANCE)
            if len(refined) <= len(anchors):
                break
            anchors = refined
            times2 = _warp(times2, refined)
        shifted = times2
    drift = sorted(abs(t - s) for (i, s), t in zip(anchors, [shifted[i] for i, _ in anchors]))
    med = drift[len(drift) // 2] if drift else 0.0

    out = []
    for i, l in enumerate(lines):
        t = shifted[i]
        nxt = shifted[i + 1] if i + 1 < len(shifted) else t + _est_line_dur(l["text"]) + 2
        # 扫光区间 = 该行窗口内实际演唱的范围(首唱句起点 → 末唱句终点),
        # 间奏不爬行、换气不冲刺;推算语速不合理时回退按字数估时。
        win_s, win_e = t, max(nxt, t + 1.2)
        s0, s1 = win_s, min(win_e, win_s + _est_line_dur(l["text"]) + 1.5)
        in_win = [(max(b, win_s), min(e, win_e)) for b, e in spans if e > win_s and b < win_e]
        if in_win:
            cand_s, cand_e = in_win[0][0], in_win[-1][1]
            n_units = _line_units(l["text"])
            if cand_e > cand_s and 0.6 <= n_units / (cand_e - cand_s) <= 9.0:
                s0, s1 = cand_s, cand_e
        out.append({"t": round(t, 2), "text": l["text"], "_sweep": (round(s0, 2), round(s1, 2))})
    return out, f"fit={method} shift={shift:+.2f}s anchors={len(anchors)}/{len(lines)} drift={med:.2f}s"


def _est_line_dur(text: str) -> float:
    return max(1.5, _line_units(text) * 0.42 + 0.4)


def _line_units(text: str) -> float:
    return sum(0.25 if c == " " else (1.0 if ord(c) > 0x2E7F else 0.55) for c in text)


# ---------- ASS 逐字字幕 ----------

def build_ass(lines: list[dict], title: str, artist: str) -> str:
    r"""\kf 逐字:字宽按 CJK=1 / ASCII=0.55 / 空格=0.25 分配 _sweep 区间时长。"""

    def ts(t: float) -> str:
        t = max(0.0, t)
        return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

    events = []
    for ln in lines:
        text = ln["text"]
        s0, s1 = ln.get("_sweep") or (ln["t"], ln["t"] + _est_line_dur(text))
        start, end = s0 + 0.15, max(s1 - 0.1, s0 + 1.0)
        weights = [0.25 if c == " " else (1.0 if ord(c) > 0x2E7F else 0.55) for c in text]
        total = sum(weights) or 1
        kparts = []
        for c, w in zip(text, weights):
            cs = max(int(round((end - start) * 100 * w / total)), 4)
            kparts.append(rf"{{\kf{cs}}}{c}")
        events.append(f"Dialogue: 0,{ts(ln['t'] - 0.15)},{ts(end + 1.2)},KTV,,0,0,0,,{ ''.join(kparts)}")

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


def process(path: Path, title: str, artist: str,
            vocals_path: str | None = None) -> tuple[str, str, str] | None:
    """取词 → 人声短语检测 → 对齐 → ASS。返回 (ass文本, 来源, 拟合摘要)。

    vocals_path 优先(demucs 真·人声stem);缺省退回"双轨相减"(有鼓点残留,
    对齐质量打折,仅兜底)。
    """
    got = fetch_lrc(title, artist)
    if not got:
        return None
    lrc, source = got
    lines = parse_lrc(lrc)
    if len(lines) < 5:
        return None
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        wav = vocals_path
        if not wav:
            fallback = f"{td}/vocals.wav"
            wav = fallback if _vocals_wav(str(path), fallback) else None
        spans = detect_spans(wav) if wav else []
        if spans:
            lines, summary = align_lines(lines, spans)
        else:
            summary = "fit=none(无人声参考)"
            for l in lines:
                l["_sweep"] = (l["t"], l["t"] + _est_line_dur(l["text"]))
    return build_ass(lines, title, artist), source, summary
