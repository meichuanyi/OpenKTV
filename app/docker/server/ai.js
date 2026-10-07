// AI 点歌助手 + 推荐 —— Phase 3a/3b(feature 分支新增,不触碰上游核心逻辑)。
//
// 设计:LLM 只负责"翻译"——把自然语言请求转成 {artist,title} 候选与一句话回复,
// 曲库匹配、去重、入队全在本地做。不把整张曲库塞给模型(家庭曲库会涨到几千首),
// 只给歌手名单(几百行封顶),模型按歌手出它成名曲,本地 LIKE 匹配命中的才入队。
//
// 配置走环境变量(部署样例见 deploy/docker-compose.yml):
//   AI_BASE_URL  OpenAI 兼容接口,如 http://192.168.x.x:4000/v1 (litellm/ollama/new-api 均可)
//   AI_API_KEY   对应密钥
//   AI_MODEL     模型名,需支持 JSON 输出(实测 glm-5.3-flash / gpt-4o-mini / qwen 均可)

const express = require('express');

module.exports = function createAiRouter({ db, broadcastQueue, log }) {
  const router = express.Router();
  const AI_BASE_URL = (process.env.AI_BASE_URL || '').replace(/\/$/, '');
  const AI_API_KEY = process.env.AI_API_KEY || '';
  const AI_MODEL = process.env.AI_MODEL || 'glm-5.3-flash';

  const aiEnabled = () => !!(AI_BASE_URL && AI_API_KEY);

  // ---------- 曲库目录与匹配 ----------

  function artistCatalog() {
    const rows = db.prepare(
      "SELECT DISTINCT artist FROM songs WHERE artist != '' ORDER BY artist LIMIT 300"
    ).all();
    const total = db.prepare('SELECT COUNT(*) c FROM songs').get().c;
    return { artists: rows.map(r => r.artist), total };
  }

  function matchSong(artist, title) {
    const clean = s => String(s || '').replace(/[《》"'!?:;,~]/g, '').trim();
    const t = clean(title), a = clean(artist);
    if (!t && !a) return null;
    return (
      (t && a && db.prepare('SELECT * FROM songs WHERE title=? AND artist=?').get(t, a)) ||
      (t && a && db.prepare('SELECT * FROM songs WHERE title LIKE ? AND artist LIKE ?').get(`%${t}%`, `%${a}%`)) ||
      (t && db.prepare('SELECT * FROM songs WHERE title LIKE ? ORDER BY play_count DESC').get(`%${t}%`)) ||
      (a && !t && db.prepare('SELECT * FROM songs WHERE artist LIKE ? ORDER BY play_count DESC LIMIT 1').get(`%${a}%`)) ||
      null
    );
  }

  // ---------- LLM 调用 ----------

  async function chatJSON(system, user) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60000);
    try {
      const res = await fetch(`${AI_BASE_URL}/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${AI_API_KEY}` },
        body: JSON.stringify({
          model: AI_MODEL,
          messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
          temperature: 0.4,
          max_tokens: 800,
        }),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`LLM HTTP ${res.status}`);
      const data = await res.json();
      const text = (data.choices?.[0]?.message?.content || '').trim();
      const m = text.match(/\{[\s\S]*\}/); // 容忍模型在 JSON 外带的废话
      if (!m) throw new Error('LLM 未返回 JSON');
      return JSON.parse(m[0]);
    } finally {
      clearTimeout(timer);
    }
  }

  const SYSTEM_PROMPT = [
    '你是家庭KTV的点歌助手,说话轻松简短(不超过40字)。',
    '曲库里现有的歌手名单会提供给你,你只能推荐这些歌手的歌(选他们的代表作)。',
    '严格只输出 JSON,格式:{"songs":[{"artist":"歌手","title":"歌名"}],"reply":"给点歌人的一句话"}',
    'songs 最多4首;如果名单里实在没有合适的歌手,songs 给空数组,reply 里说明并建议换个方向。',
  ].join('\n');

  // ---------- 3a 点歌助手 ----------

  router.post('/request', async (req, res) => {
    if (!aiEnabled()) return res.status(503).json({ error: '未配置 AI 服务(AI_BASE_URL / AI_API_KEY)' });
    const prompt = String((req.body || {}).prompt || '').trim().slice(0, 200);
    if (!prompt) return res.status(400).json({ error: '想唱什么?说一句吧' });
    const nickname = String((req.body || {}).nickname || '').slice(0, 20) || 'AI助手';
    try {
      const { artists, total } = artistCatalog();
      if (!total) return res.status(400).json({ error: '曲库还是空的,先去「在线」Tab 下载几首歌吧' });
      const llm = await chatJSON(SYSTEM_PROMPT,
        `曲库歌手名单(${total}首歌):${artists.join('、')}\n\n点歌人说:「${prompt}」`);
      const added = [], missing = [];
      for (const s of (llm.songs || []).slice(0, 4)) {
        const song = matchSong(s.artist, s.title);
        if (song) {
          if (added.some(x => x.id === song.id)) continue;
          db.prepare('INSERT INTO queue (song_id,nickname) VALUES (?,?)').run(song.id, nickname);
          db.prepare('UPDATE songs SET play_count=play_count+1 WHERE id=?').run(song.id);
          const playing = db.prepare("SELECT * FROM queue WHERE status='playing'").get();
          if (!playing) db.prepare("UPDATE queue SET status='playing' WHERE id=?")
            .run(db.prepare('SELECT last_insert_rowid() id').get().id);
          added.push(song);
        } else {
          missing.push({ artist: s.artist || '', title: s.title || '' });
        }
      }
      if (added.length) {
        broadcastQueue();
        log.info('AI', `助手点歌: 「${prompt}」→ 入队 ${added.map(s => s.title).join('/')}`);
      }
      res.json({
        reply: llm.reply || '',
        added: added.map(s => ({ id: s.id, title: s.title, artist: s.artist })),
        missing,
      });
    } catch (e) {
      log.error('AI', `助手请求失败: ${e.message}`);
      res.status(502).json({ error: 'AI 开小差了: ' + e.message });
    }
  });

  // ---------- 3b 推荐(不入队,前端展示后手动加) ----------

  router.get('/recommend', async (req, res) => {
    if (!aiEnabled()) return res.status(503).json({ error: '未配置 AI 服务' });
    try {
      const total = db.prepare('SELECT COUNT(*) c FROM songs').get().c;
      if (total < 3) return res.json({ reply: '曲库再多几首才好推荐', songs: [] });
      const hist = db.prepare(
        `SELECT s.artist, s.title, COUNT(*) c FROM history h JOIN songs s ON s.id=h.song_id
         GROUP BY s.artist ORDER BY c DESC LIMIT 8`).all();
      const { artists } = artistCatalog();
      const hour = new Date().getHours();
      const ctx = [
        `现在是${hour < 6 ? '深夜' : hour < 12 ? '上午' : hour < 18 ? '下午' : '晚上'}${hour}点。`,
        hist.length ? `最近常唱:${hist.map(h => `${h.artist}《${h.title}》x${h.c}`).join('、')}。` : '还没有点唱记录。',
        `曲库歌手:${artists.join('、')}`,
      ].join('\n');
      const llm = await chatJSON(SYSTEM_PROMPT, `${ctx}\n\n推荐 3-4 首适合现在气氛的歌,reply 一句话说明理由。`);
      const songs = [];
      for (const s of (llm.songs || []).slice(0, 4)) {
        const m = matchSong(s.artist, s.title);
        if (m && !songs.some(x => x.id === m.id)) songs.push(m);
      }
      res.json({ reply: llm.reply || '', songs });
    } catch (e) {
      log.error('AI', `推荐失败: ${e.message}`);
      res.status(502).json({ error: 'AI 开小差了: ' + e.message });
    }
  });

  return router;
};
