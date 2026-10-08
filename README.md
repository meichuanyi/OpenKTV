# OpenKTV · 开源 AI 家庭 KTV

基于开源项目 [junyao-ktv(骏耀K歌)](https://github.com/ma303973022/junyao-ktv) 二次开发、
融合 [maiba-ktv(麦霸)](https://github.com/xieweicong/maiba-ktv) 能力的家庭 KTV 系统。

**一句话:一台 NAS + 一块大屏,手机扫码点歌;歌不够?现场搜、当场下、AI 生成伴唱;
懒得搜?跟 AI 助手说一句"来首适合现在的歌";不想动手机?让 Claude 通过 MCP 帮你管。**

> 上游致敬:junyao-ktv 的多端角色、曲库管理、HLS/VAAPI 转码体系是本项目的骨架;
> maiba-ktv(其本身是 [pikaraoke](https://github.com/vicwomg/pikaraoke) 的分支)贡献了 B站搜索实现与 Demucs 工具链。
> 上游原始说明见 [UPSTREAM_README.md](UPSTREAM_README.md)。感谢以上作者的出色工作。

## 功能总览

| 功能 | 说明 |
|---|---|
| 🎤 **多端点歌** | 手机扫码点歌、电视大屏演唱、遥控器切歌调音(上游能力,角色可上锁) |
| 🌐 **在线搜索下载** | 手机端搜 B站/YouTube → 选清晰度 → 按钮实时进度 → 自动命名入库 |
| 🎛 **AI 伴奏分离** | Demucs 自动把单音轨 MV 分离出伴奏,合成为原唱+伴唱双音轨,随时切换 |
| 🤖 **AI 点歌助手** | 对话说需求:"来首适合深夜的粤语歌" → 自动选曲入队;曲库没有的给出在线搜索直达 |
| ✨ **AI 推荐** | 按时段 + 点唱历史生成「为你推荐」,一键点唱 |
| 🔌 **MCP 服务** | 内置 MCP(Streamable HTTP)服务,10 个工具:点歌/查曲库/队列管理/MV 增删改查/在线下载,可从 Claude 等任意 MCP 客户端操作你的 KTV |
| 🗂 **MV 增删改查** | 管理后台可视化维护曲库(编辑/删除/扫描);MCP 工具同样支持全部 CRUD |
| 📺 **硬件转码** | Intel/AMD 核显 VAAPI、NVIDIA NVENC 自动探测,HLS 流式播放(上游能力) |

## 快速开始

```bash
git clone https://github.com/meichuanyi/OpenKTV.git && cd OpenKTV

# 1. 主服务镜像
docker build -t openktv:server app/docker

# 2. 工具服务镜像(需先构建 maiba 基础镜像,见其仓库;
#    注意其 Dockerfile 缺一行 COPY README.md,需补上再构建)
docker build -t openktv:tools ktv-tools

# 3. 按 deploy/docker-compose.yml 修改路径/密码/AI配置后启动
cd deploy && docker compose up -d
```

访问 `http://NAS_IP:8083`:`/m` 手机点歌(点歌/队列/遥控/在线/助手 五个 Tab)·
`/tv` 电视大屏 · `/admin` 管理后台。

### 接入 AI 点歌助手

准备任意 OpenAI 兼容接口(litellm / ollama / new-api / 官方 API 均可),在 compose 里配置:

```yaml
- AI_BASE_URL=http://your-llm-host:4000/v1
- AI_API_KEY=sk-xxx
- AI_MODEL=glm-5.3-flash   # 需支持 JSON 输出
```

### 接入 MCP 客户端(如 Claude)

```bash
claude mcp add --transport http openktv http://NAS_IP:8083/mcp
# 设置了 MCP_TOKEN 环境变量时,加 --header "Authorization: Bearer <token>"
```

之后就可以对 Claude 说:"帮我把《晴天》顶到队列第一首"、"搜一下林俊杰的江南下载下来"、
"曲库里把《青花瓷》的歌手改成周杰伦"——它会调用 OpenKTV 的 MCP 工具完成操作。

## 架构

```
[手机 /m]  [电视 /tv]  [后台 /admin]  [MCP 客户端 /mcp]
     │
┌────▼──────────────────────────────┐   ┌───────────────────────────────┐
│ junyao-ktv 主服务 (Node, 8083)     │   │ ktv-tools 工具服务 (FastAPI,   │
│  骨架:队列/角色/HLS/VAAPI转码      │──▶│  仅内网) 基于 maiba 镜像:      │
│  新增:online.js / ai.js / mcp.js  │   │   /search  B站API+YouTube搜索  │
└───────────────────────────────────┘   │   /download yt-dlp 下载        │
        共享曲库目录                      │   demucs_sep Demucs分离子进程   │
┌───────────────────────────────────┐   └───────────────────────────────┘
│ 曲库目录 (双音轨 MV 落地处)         │      串行任务队列 + 进度上报
└───────────────────────────────────┘
```

改动原则:**所有新增代码收敛在独立文件**(`app/docker/server/{online,ai,mcp}.js`、
`ktv-tools/`、手机页新增 Tab),对上游核心逻辑零侵入,方便跟进上游版本合并。

## 目录说明

```
app/docker/          上游 junyao-ktv 原始代码 + 新增 online/ai/mcp 三个服务端模块与手机页 Tab
ktv-tools/           在线搜索/下载/Demucs分离 工具服务(FastAPI,Python)
deploy/              部署样例(compose)
UPSTREAM_README.md   上游 junyao-ktv 原始说明
cmd/ wizard/ config/ manifest/  上游的飞牛 fnOS 应用打包文件(未改动)
```

## 路线图

- [x] Phase 1:在线搜索 + 下载入库(手机端「在线」Tab,按钮实时进度)
- [x] Phase 2:Demucs 伴奏分离 + 双音轨自动合成(下载自动接续;曲库内单音轨可 🎛 补伴唱)
- [x] Phase 3a:🤖 LLM 点歌助手(自然语言点歌、缺失歌曲转在线搜索)
- [x] Phase 3b:✨ AI 推荐(时段 + 历史个性化)
- [x] Phase 3e:🔌 MCP 服务(10 工具:点歌/队列/MV CRUD/在线下载/扫描)
- [ ] Phase 3c:AI 音准评分(利用分离阶段的人声参考轨)
- [ ] Phase 3d:实时混响 / 录音点评

## 开源许可

- 本仓库继承上游 junyao-ktv 的 **MIT License**(见 [LICENSE](LICENSE),Copyright (c) 2026 ma303973022),
  在此基础上二次开发并保留其版权声明。
- `ktv-tools/` 目录包含来自 maiba-ktv(GPL-3.0)的移植代码(如 `app/bilibili.py`、`app/demucs_sep.py`),
  该目录整体以 **GPL-3.0** 授权,与其来源保持一致。
- Demucs 模型权重遵循其官方许可(仅限研究/个人使用),请勿用于商业场景。
- 下载的音视频内容版权归各自权利人,本项目仅供个人局域网学习娱乐使用。
