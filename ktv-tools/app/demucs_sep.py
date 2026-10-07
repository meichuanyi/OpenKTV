"""Demucs 人声分离子进程(核心逻辑移植自 maiba-ktv 的 separate_worker.py,GPL-3.0)。

为什么不用 demucs 官方 CLI:它模块级依赖 sphn,而 maiba 基础镜像为兼容
ARM NAS 用 uv override 把 sphn 排掉了。这里照搬 maiba 的做法——ffmpeg 解码
到 numpy → demucs.apply 低层 API → ffmpeg 从 stdin 原始 PCM 编码 AAC。

独立进程运行的另一个原因:torch 的崩溃/OOM 不该连累 uvicorn 主进程,
一首歌失败可以整首重试,任务队列留在服务进程里不受影响。

用法: python -m app.demucs_sep <源音频/视频> <输出伴奏.m4a> [模型名]
进度: 每完成一个分段向 stdout 打印一行 "[sep]|<percent>"
"""

import subprocess
import sys
import time

import numpy

SAMPLE_RATE = 44100
BITRATE = "192k"
PROGRESS_PREFIX = "[sep]|"


def decode(song_path: str):
    """任意容器解码为 (2, N) float32 @44.1kHz,走 f32le 管道避免临时 WAV。"""
    cmd = [
        "ffmpeg", "-v", "error", "-i", song_path,
        "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1",
    ]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    samples = numpy.frombuffer(raw, dtype="<f4")
    usable = (len(samples) // 2) * 2
    return samples[:usable].reshape(-1, 2).T.copy()


def encode_aac(samples, out_path: str) -> None:
    """(2, N) float32 → AAC m4a,ffmpeg 读 stdin 原始 PCM。"""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0",
        "-c:a", "aac", "-b:a", BITRATE, "-f", "mp4", out_path,
    ]
    interleaved = samples.T.copy(order="C")
    subprocess.run(cmd, input=interleaved.tobytes(), check=True)


def separate(song_path: str, out_path: str, model_name: str = "htdemucs") -> float:
    """分离伴奏(no_vocals = 原混音 - 人声,stems 相加恒等于混音,精确无残差)。"""
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    started = time.time()

    def report(info: dict) -> None:
        if info.get("state") != "end":
            return
        length = info.get("audio_length") or 0
        if not length:
            return
        offset = info.get("segment_offset", 0) + (info.get("segment_length") or 0)
        models = max(info.get("models", 1), 1)
        model_idx = info.get("model_idx_in_bag", 0)
        fraction = (model_idx + min(offset / length, 1.0)) / models
        print(f"{PROGRESS_PREFIX}{int(fraction * 100)}", flush=True)

    model = get_model(model_name)
    model.to("cpu").eval()
    origin = torch.as_tensor(decode(song_path)).unsqueeze(0)
    # Demucs 训练输入做过响度归一,进出各做一次(与 demucs.api 行为一致)。
    reference = origin.mean(dim=1, keepdim=True)
    mean, std = reference.mean(), reference.std()
    normalized = (origin - mean) / (std + 1e-8)
    with torch.no_grad():
        separated = apply_model(
            model, normalized, device="cpu",
            shifts=0, overlap=0.25, progress=False, callback=report,
        )
    separated = separated * std + mean
    vocal_index = model.sources.index("vocals")
    vocals = separated[0, vocal_index].cpu()
    no_vocals = (origin[0] - vocals).clamp(-1, 1).numpy().astype("float32")
    encode_aac(no_vocals, out_path)
    return time.time() - started


def main() -> None:
    if len(sys.argv) < 3:
        print("用法: python -m app.demucs_sep <源文件> <输出.m4a> [模型]", file=sys.stderr)
        sys.exit(2)
    src, out = sys.argv[1], sys.argv[2]
    model = sys.argv[3] if len(sys.argv) > 3 else "htdemucs"
    try:
        seconds = separate(src, out, model)
        print(f"{PROGRESS_PREFIX}100", flush=True)
        print(f"done in {seconds:.1f}s", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 —— 子进程兜底,错误文本带回主进程
        print(f"分离失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
