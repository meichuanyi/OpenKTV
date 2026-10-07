# ktv-tools:junyao-ktv 的在线搜索/下载/Demucs分离 工具服务。
# 基础镜像直接用 maiba:latest —— 里面 demucs(torch) / yt-dlp / deno / ffmpeg /
# requests 全部现成,这里只补一个 FastAPI 的壳。
# 分离走 app/demucs_sep.py 子进程(demucs.apply 低层 API),不依赖 demucs CLI
# (CLI 模块级 import sphn,maiba 上游为兼容 ARM NAS 把 sphn 排掉了)。
# 构建 maiba 镜像见 maiba-ktv 仓库:注意其 Dockerfile 漏了 COPY README.md,
# 构建前需补上,否则 hatchling 打包阶段必失败。
FROM maiba:latest

USER root
ENV PATH="/app/.venv/bin:$PATH" VIRTUAL_ENV=/app/.venv

# 系统 ffmpeg(带 libass 字幕滤镜,静态版没有)+ 中文字体(烧录逐字字幕用)
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fontconfig fonts-noto-cjk \
    && fc-cache -f >/dev/null 2>&1 \
    && rm -rf /var/lib/apt/lists/*

RUN uv pip install --python /app/.venv/bin/python \
      fastapi uvicorn python-multipart

WORKDIR /srv
COPY app ./app

EXPOSE 9000
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
