# ktv-fusion · 家庭 KTV 融合增强版

基于开源项目 [junyao-ktv(骏耀K歌)](https://github.com/ma303973022/junyao-ktv) 二次开发的家庭 KTV 系统,
融合了 [maiba-ktv(麦霸)](https://github.com/xieweicong/maiba-ktv) 的在线搜索与 AI 伴奏分离能力。
一台 NAS + 一块大屏,手机扫码点歌;**歌不够?现场搜、当场下、AI 生成伴唱、双音轨直接唱。**

> 上游致敬:junyao-ktv 的多端角色、曲库管理、HLS/VAAPI 转码体系是本项目的骨架;
> maiba-ktv(其本身是 [pikaraoke](https://github.com/vicwomg/pikaraoke) 的分支)贡献了 B站搜索实现与 Demucs 工具链。
> 上游原始说明见 [UPSTREAM_README.md](UPSTREAM_README.md)。感谢以上作者的出色工作。

## 与上游 junyao-ktv 相比的改进

| 能力 | 上游 junyao-ktv | 本项目 |
|---|---|---|
| 曲库来源 | 本地目录 / 网盘挂载,文件自行准备 | **新增:手机端在线搜索(B站/YouTube),一键下载入库** |
| 伴奏切换 | 依赖素材本身是双音轨 MV | **新增:单音轨 MV 下载后自动 AI 分离(Demucs)生成伴奏轨,合成为双音轨** |
| 下载体验 | — | 按钮上实时进度(排队/百分比/入库),失败可重试;CDN 坏节点看门狗自动终止 |
| 命名入库 | 手动命名 `歌手 - 歌名` | 自动从视频标题智能解析(《歌手 - 歌名》/ `A - B` 等模式) |

**路线图**(开发中):
- [x] Phase 1:在线搜索 + 下载入库(手机端「在线」Tab)
- [x] Phase 2:Demucs 伴奏分离 + 双音轨自动合成(下载后自动接续;曲库内单音轨歌曲可手动补伴唱 🎛)
- [x] Phase 3a:🤖 LLM 点歌助手 —— 手机上说一句「来首适合深夜的粤语歌」,自动选曲入队;曲库没有的给出在线搜索直达
- [x] Phase 3b:✨ AI 推荐 —— 按时段和点唱历史生成「为你推荐」,一键点唱
- [ ] Phase 3c:AI 音准评分(利用分离阶段保留的人声参考轨)
- [ ] Phase 3d:实时混响 / 录音点评

## 架构

```
[手机 /m]  [电视 /tv]  [后台 /admin]
     │
┌────▼──────────────────────────────┐   ┌───────────────────────────────┐
│ junyao-ktv 主服务 (Node, 8083)     │   │ ktv-tools 工具服务 (FastAPI,   │
│  骨架不动:队列/角色/HLS/VAAPI转码  │──▶│  仅内网) 基于 maiba 镜像:      │
│  新增 online.js /api/online/* 代理 │   │   /search  B站API+YouTube搜索  │
└───────────────────────────────────┘   │   /download yt-dlp 下载        │
        共享曲库目录                      │   /separate Demucs→双音轨合成  │
┌───────────────────────────────────┐   └───────────────────────────────┘
│ 曲库目录 (双音轨 MV 落地处)         │      串行任务队列 + 进度上报
└───────────────────────────────────┘
```

改动原则:**所有新增代码收敛在独立文件**(`app/docker/server/online.js`、`ktv-tools/`、手机页新增 Tab),
对上游核心逻辑零侵入,方便跟进上游版本合并。

## 快速开始

```bash
git clone https://github.com/meichuanyi/ktv-fusion.git && cd ktv-fusion

# 1. 构建主服务镜像(基于上游 Dockerfile,无改动)
docker build -t junyao-ktv:fused app/docker

# 2. 构建工具服务镜像(需先构建 maiba 基础镜像,见其仓库;
#    注意其 Dockerfile 缺一行 COPY README.md,构建前需补上)
docker build -t ktv-tools:latest ktv-tools

# 3. 按 deploy/docker-compose.yml 修改路径/密码后启动
cd deploy && docker compose up -d
```

访问 `http://NAS_IP:8083`:
- `/m` 手机点歌(含「在线」Tab:搜索 → 下载 → 进度 → 自动入库)
- `/tv` 电视大屏 · `/admin` 管理后台

## 目录说明

```
app/docker/          上游 junyao-ktv 原始代码 + 新增 server/online.js 与手机页「在线」Tab
ktv-tools/           新增:在线搜索/下载/分离工具服务(FastAPI,Python)
deploy/              部署样例(compose)
UPSTREAM_README.md   上游 junyao-ktv 原始说明
cmd/ wizard/ config/ manifest/  上游的飞牛 fnOS 应用打包文件(未改动)
```

## 开源许可

- 本仓库继承上游 junyao-ktv 的 **MIT License**(见 [LICENSE](LICENSE),Copyright (c) 2026 ma303973022),
  在此基础上二次开发并保留其版权声明。
- `ktv-tools/` 目录包含来自 maiba-ktv(GPL-3.0)的移植代码(如 `app/bilibili.py`),
  该目录整体以 **GPL-3.0** 授权,与其来源保持一致。
- Demucs 模型权重遵循其官方许可(仅限研究/个人使用),请勿用于商业场景。
- 下载的音视频内容版权归各自权利人,本项目仅供个人局域网学习娱乐使用。
