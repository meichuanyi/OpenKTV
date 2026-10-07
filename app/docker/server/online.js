// 在线搜索/下载 —— 融合改造新增模块(feature/online-fusion)。
// 仅为 junyao 主服务与 ktv-tools 工具服务之间的代理层:搜索、封面、
// 下载下单、任务进度。真正的搜索/yt-dlp 下载都在 ktv-tools 容器里,
// 主服务只转发,不在本镜像里引入 Python/网络依赖。
//
// TOOLS_URL 指向工具服务地址,docker compose 里两个服务同网段时
// 直接用服务名:http://ktv-tools:9000
const express = require('express');
const log = require('./logger');

const router = express.Router();
const TOOLS_URL = (process.env.TOOLS_URL || 'http://ktv-tools:9000').replace(/\/$/, '');

async function toolsJSON(path, options = {}) {
  const res = await fetch(TOOLS_URL + path, options);
  const text = await res.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    body = { error: text.slice(0, 200) };
  }
  return { status: res.status, body };
}

// 搜B站/YouTube。手机点歌页「在线」Tab 的数据源。
router.get('/search', async (req, res) => {
  const q = String(req.query.q || '').trim();
  if (!q) return res.status(400).json({ error: '关键词为空' });
  const source = ['auto', 'bilibili', 'youtube'].includes(req.query.source)
    ? req.query.source : 'auto';
  try {
    const r = await toolsJSON(
      `/search?q=${encodeURIComponent(q)}&source=${source}&limit=${Number(req.query.limit) || 20}`
    );
    res.status(r.status).json(r.body);
  } catch (e) {
    log.warn('ONLINE', `搜索转发失败(${q}): ${e.message}`);
    res.status(502).json({ error: '工具服务不可达: ' + e.message });
  }
});

// 封面代理:B站 CDN 拒绝浏览器直链,必须由服务端带 Referer 转发。
router.get('/thumbnail', async (req, res) => {
  const url = String(req.query.url || '');
  if (!/^https?:\/\//.test(url)) return res.status(400).end();
  try {
    const upstream = await fetch(`${TOOLS_URL}/thumbnail?url=${encodeURIComponent(url)}`);
    if (!upstream.ok) return res.status(upstream.status).end();
    res.set('Content-Type', upstream.headers.get('content-type') || 'image/jpeg');
    res.set('Cache-Control', 'public, max-age=86400');
    res.end(Buffer.from(await upstream.arrayBuffer()));
  } catch {
    res.status(502).end();
  }
});

// 下载下单:ktv-tools 串行队列消化,返回任务 id 供前端轮询。
router.post('/download', async (req, res) => {
  const { url, title, artist, quality } = req.body || {};
  if (!/^https?:\/\//.test(String(url || ''))) {
    return res.status(400).json({ error: '非法 URL' });
  }
  try {
    const r = await toolsJSON('/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, title, artist, quality }),
    });
    log.info('ONLINE', `下载下单: ${artist || '?'} - ${title || '?'} <- ${url}`);
    res.status(r.status).json(r.body);
  } catch (e) {
    log.error('ONLINE', `下载下单失败: ${e.message}`);
    res.status(502).json({ error: '工具服务不可达: ' + e.message });
  }
});

// 对曲库内已有单音轨歌曲补 AI 伴唱(Demucs 分离 + 双音轨合成)。
// 曲库接口里的 filename 形如 "library1/周杰伦 - 晴天.mp4",ktv-tools 的根
// 目录就是曲库目录本身,这里剥掉 libraryN/ 前缀再转发。
router.post('/separate', async (req, res) => {
  const raw = String((req.body || {}).filename || '');
  const rel = raw.replace(/^library[^/]+\//, '');
  if (!rel) return res.status(400).json({ error: 'filename 必填' });
  try {
    const r = await toolsJSON('/separate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: rel }),
    });
    if (!r.body.skipped) log.info('ONLINE', `AI分离下单: ${rel}`);
    res.status(r.status).json(r.body);
  } catch (e) {
    log.error('ONLINE', `分离下单失败: ${e.message}`);
    res.status(502).json({ error: '工具服务不可达: ' + e.message });
  }
});

// 任务列表/详情:进度、失败原因、入库文件名。
router.get('/tasks', async (req, res) => {
  try {
    const r = await toolsJSON('/tasks');
    res.status(r.status).json(r.body);
  } catch (e) {
    res.status(502).json({ error: '工具服务不可达: ' + e.message });
  }
});

router.get('/tasks/:id', async (req, res) => {
  try {
    const r = await toolsJSON(`/tasks/${encodeURIComponent(req.params.id)}`);
    res.status(r.status).json(r.body);
  } catch (e) {
    res.status(502).json({ error: '工具服务不可达: ' + e.message });
  }
});

module.exports = router;
