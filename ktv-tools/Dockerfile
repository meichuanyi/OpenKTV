# ktv-tools:junyao-ktv 的在线搜索/下载(后续加 Demucs 分离)工具服务。
# 基础镜像直接用 maiba:latest —— 里面 demucs / yt-dlp / deno / ffmpeg / requests
# 全部现成,这里只补一个 FastAPI 的壳。构建 maiba 镜像见 maiba-ktv 仓库
# (注意上游 Dockerfile 漏了 COPY README.md,已在本仓库 maiba-ktv 副本中修复)。
FROM maiba:latest

USER root
ENV PATH="/app/.venv/bin:$PATH" VIRTUAL_ENV=/app/.venv

RUN uv pip install --python /app/.venv/bin/python \
      fastapi uvicorn python-multipart

WORKDIR /srv
COPY app ./app

EXPOSE 9000
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
