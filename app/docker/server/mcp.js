// MCP(Model Context Protocol)服务 —— 让 Claude Desktop / 任何 MCP 客户端
// 直接操作这台 KTV:查曲库、点歌、看队列、切歌、扫描、搜索下载、MV 增删改。
//
// 实现说明:走 MCP Streamable HTTP 传输(JSON-RPC 2.0 over POST,无状态),
// 不引入 SDK 依赖——协议面很窄(initialize / tools/list / tools/call),
// 两条路由即可与官方客户端互通。
//
// 安全:默认仅限局域网内使用;设置 MCP_TOKEN 后,客户端必须带
//   Authorization: Bearer <token>  才能调用。
//
// 客户端接入示例(Claude Desktop / claude mcp):
//   claude mcp add --transport http openktv http://NAS_IP:8083/mcp
//   (设置了 MCP_TOKEN 时加 --header "Authorization: Bearer <token>")

const express = require('express');

const PROTOCOL_VERSION = '2024-11-05';
const SERVER_INFO = { name: 'OpenKTV', version: '1.0.0' };

const TOOLS = [
  {
    name: 'list_songs',
    description: '搜索KTV曲库,返回歌曲列表(可按歌名/歌手关键词过滤)',
    inputSchema: {
      type: 'object',
      properties: {
        q: { type: 'string', description: '关键词(歌名或歌手),留空返回全部' },
        limit: { type: 'number', description: '返回条数上限,默认20' },
      },
    },
  },
  {
    name: 'queue_song',
    description: '点歌:把一首歌加入KTV播放队列。可用 song_id 精确点,或用关键词模糊匹配(歌名/歌手)',
    inputSchema: {
      type: 'object',
      properties: {
        song_id: { type: 'number', description: '歌曲ID(list_songs 返回的 id)' },
        keyword: { type: 'string', description: '如「周杰伦 晴天」,按标题+歌手模糊匹配最接近的一首' },
        nickname: { type: 'string', description: '点歌人昵称,默认 MCP 观众' },
      },
    },
  },
  {
    name: 'list_queue',
    description: '查看当前播放队列(谁点了什么、正在唱哪首)',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'next_song',
    description: '切歌:结束当前演唱,播放队列里的下一首',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'update_song',
    description: '改歌:修改曲库里某首歌的歌名/歌手(比如下载解析错了)',
    inputSchema: {
      type: 'object',
      properties: {
        song_id: { type: 'number', description: '歌曲ID' },
        title: { type: 'string', description: '新歌名' },
        artist: { type: 'string', description: '新歌手' },
      },
      required: ['song_id'],
    },
  },
  {
    name: 'delete_song',
    description: '删歌:从曲库移除一首歌(不删除磁盘上的MV文件,可通过重新扫描找回)',
    inputSchema: {
      type: 'object',
      properties: { song_id: { type: 'number', description: '歌曲ID' } },
      required: ['song_id'],
    },
  },
  {
    name: 'scan_library',
    description: '扫描曲库目录,让新放进来的 MV 文件入库',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'online_search',
    description: '在线搜索 MV(B站/YouTube),返回可下载的候选列表',
    inputSchema: {
      type: 'object',
      properties: {
        q: { type: 'string', description: '关键词,如「周杰伦 晴天 KTV」' },
        source: { type: 'string', enum: ['auto', 'bilibili', 'youtube'], description: '搜索源,默认 auto' },
      },
      required: ['q'],
    },
  },
  {
    name: 'download_mv',
    description: '下载在线MV入库(单音轨会自动AI分离成双音轨,几分钟后可点唱)',
    inputSchema: {
      type: 'object',
      properties: {
        url: { type: 'string', description: '视频页URL(B站/YouTube)' },
        title: { type: 'string', description: '歌名(或完整视频标题,会自动解析)' },
        artist: { type: 'string', description: '歌手(留空自动解析)' },
        quality: { type: 'string', enum: ['480', '720', '1080'], description: '清晰度,默认720' },
      },
      required: ['url', 'title'],
    },
  },
  {
    name: 'audio_to_mv',
    description: '纯音频转MV:把MP3等音频文件(曲库目录或musicdl音乐目录)包装成静态背景MV,自动AI分离双音轨+逐字字幕,一步入库',
    inputSchema: {
      type: 'object',
      properties: {
        filename: { type: 'string', description: '音频文件名,如 "许嵩 - 灰色头像.mp3";musicdl目录可带子路径' },
      },
      required: ['filename'],
    },
  },
  {
    name: 'download_tasks',
    description: '查看下载/分离任务进度',
    inputSchema: { type: 'object', properties: {} },
  },
];

module.exports = function createMcp({ db, broadcastQueue, log, scanLibrary, removeHLS }) {
  const router = express.Router();
  const TOOLS_URL = (process.env.TOOLS_URL || 'http://ktv-tools:9000').replace(/\/$/, '');
  const MCP_TOKEN = process.env.MCP_TOKEN || '';

  router.use((req, res, next) => {
    if (!MCP_TOKEN) return next();
    const auth = req.headers.authorization || '';
    if (auth === `Bearer ${MCP_TOKEN}`) return next();
    return res.status(401).json({ jsonrpc: '2.0', error: { code: -32001, message: '未授权' }, id: null });
  });

  const ok = (id, result) => ({ jsonrpc: '2.0', id, result });
  const err = (id, code, message) => ({ jsonrpc: '2.0', id, error: { code, message } });

  function enqueue(song, nickname) {
    db.prepare('INSERT INTO queue (song_id,nickname) VALUES (?,?)').run(song.id, nickname || 'MCP观众');
    db.prepare('UPDATE songs SET play_count=play_count+1 WHERE id=?').run(song.id);
    const playing = db.prepare("SELECT * FROM queue WHERE status='playing'").get();
    if (!playing) db.prepare("UPDATE queue SET status='playing' WHERE id=?")
      .run(db.prepare('SELECT last_insert_rowid() id').get().id);
    broadcastQueue();
  }

  function matchKeyword(keyword) {
    const kw = String(keyword || '').trim();
    if (!kw) return null;
    const words = kw.split(/\s+/).filter(Boolean);
    // 「周杰伦 晴天」式:每个词都LIKE命中者优先,退而求其次任一命中
    const all = db.prepare(
      'SELECT * FROM songs WHERE ' + words.map(() => '(title LIKE ? OR artist LIKE ?)').join(' AND ') + ' LIMIT 5'
    ).all(...words.flatMap(w => [`%${w}%`, `%${w}%`]));
    if (all.length) return all[0];
    const any = db.prepare(
      'SELECT * FROM songs WHERE ' + words.map(() => '(title LIKE ? OR artist LIKE ?)').join(' OR ') + ' ORDER BY play_count DESC LIMIT 1'
    ).get(...words.flatMap(w => [`%${w}%`, `%${w}%`]));
    return any || null;
  }

  async function toolsJSON(path, options) {
    const res = await fetch(TOOLS_URL + path, options);
    return { status: res.status, body: await res.json().catch(() => ({})) };
  }

  const impl = {
    list_songs: async (a) => {
      const limit = Math.min(Number(a.limit) || 20, 100);
      const rows = a.q
        ? db.prepare('SELECT id,title,artist,play_count,audio_tracks FROM songs WHERE title LIKE ? OR artist LIKE ? ORDER BY play_count DESC LIMIT ?')
            .all(`%${a.q}%`, `%${a.q}%`, limit)
        : db.prepare('SELECT id,title,artist,play_count,audio_tracks FROM songs ORDER BY id DESC LIMIT ?').all(limit);
      return { content: [{ type: 'text', text: JSON.stringify(rows, null, 2) }] };
    },
    queue_song: async (a) => {
      let song = null;
      if (a.song_id) song = db.prepare('SELECT * FROM songs WHERE id=?').get(a.song_id);
      if (!song && a.keyword) song = matchKeyword(a.keyword);
      if (!song) return { content: [{ type: 'text', text: '曲库里没找到这首歌。可先用 list_songs 查,或用 online_search + download_mv 下载。' }] };
      enqueue(song, a.nickname);
      return { content: [{ type: 'text', text: `已点唱:${song.artist} - ${song.title}` }] };
    },
    list_queue: async () => {
      const rows = db.prepare(
        `SELECT q.id, q.status, q.nickname, s.title, s.artist FROM queue q
         JOIN songs s ON s.id=q.song_id ORDER BY (q.status='playing') DESC, q.is_top DESC, q.id ASC`).all();
      return { content: [{ type: 'text', text: rows.length ? JSON.stringify(rows, null, 2) : '队列是空的' }] };
    },
    next_song: async () => {
      const cur = db.prepare("SELECT * FROM queue WHERE status='playing' ORDER BY id LIMIT 1").get();
      if (cur) {
        db.prepare("UPDATE queue SET status='done' WHERE id=?").run(cur.id);
        db.prepare('INSERT INTO history (song_id,nickname) VALUES (?,?)').run(cur.song_id, cur.nickname);
      }
      const nxt = db.prepare("SELECT * FROM queue WHERE status='waiting' ORDER BY is_top DESC, id ASC LIMIT 1").get();
      if (nxt) db.prepare("UPDATE queue SET status='playing' WHERE id=?").run(nxt.id);
      broadcastQueue();
      const now = nxt
        ? db.prepare('SELECT * FROM songs WHERE id=?').get(nxt.song_id)
        : null;
      return { content: [{ type: 'text', text: now ? `切歌!正在播放:${now.artist} - ${now.title}` : '已切歌,队列空了' }] };
    },
    update_song: async (a) => {
      const song = db.prepare('SELECT * FROM songs WHERE id=?').get(a.song_id);
      if (!song) throw new Error('歌曲不存在');
      db.prepare('UPDATE songs SET title=?, artist=? WHERE id=?')
        .run(a.title || song.title, a.artist || song.artist, a.song_id);
      return { content: [{ type: 'text', text: `已更新:${a.artist || song.artist} - ${a.title || song.title}` }] };
    },
    delete_song: async (a) => {
      const song = db.prepare('SELECT * FROM songs WHERE id=?').get(a.song_id);
      if (!song) throw new Error('歌曲不存在');
      // queue 表对 song_id 有外键约束,直接删会 FOREIGN KEY 失败(上游删除接口同样
      // 有这个坑):先清排队,收藏一并清掉,历史记录保留(统计不丢)。
      const removedFromQueue = db.prepare('DELETE FROM queue WHERE song_id=?').run(a.song_id).changes;
      db.prepare('DELETE FROM favorites WHERE song_id=?').run(a.song_id);
      db.prepare('DELETE FROM songs WHERE id=?').run(a.song_id);
      removeHLS(a.song_id);
      broadcastQueue();
      return { content: [{ type: 'text', text: `已移除:${song.artist} - ${song.title}` + (removedFromQueue ? `(同时清掉${removedFromQueue}条排队)` : '') + ';文件保留,重新扫描可找回' }] };
    },
    scan_library: async () => {
      const r = await scanLibrary();
      return { content: [{ type: 'text', text: `扫描完成:共 ${r.total} 首,新增 ${r.added},移除 ${r.removed}` }] };
    },
    online_search: async (a) => {
      const r = await toolsJSON(`/search?q=${encodeURIComponent(a.q)}&source=${a.source || 'auto'}&limit=10`);
      return { content: [{ type: 'text', text: JSON.stringify(r.body, null, 2) }] };
    },
    download_mv: async (a) => {
      const r = await toolsJSON('/download', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: a.url, title: a.title, artist: a.artist || '', quality: a.quality || '720' }),
      });
      return { content: [{ type: 'text', text: JSON.stringify(r.body, null, 2) }] };
    },
    audio_to_mv: async (a) => {
      const r = await toolsJSON('/audio2mv', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: a.filename }),
      });
      return { content: [{ type: 'text', text: r.body.task_id
        ? `已加入队列(task ${r.body.task_id}):包装画面→AI分离→字幕烧录,完成后自动入库,可在「任务」页看进度`
        : JSON.stringify(r.body) }] };
    },
    download_tasks: async () => {
      const r = await toolsJSON('/tasks');
      return { content: [{ type: 'text', text: JSON.stringify(r.body, null, 2) }] };
    },
  };

  router.post('/', async (req, res) => {
    const msg = req.body;
    if (!msg || msg.jsonrpc !== '2.0') return res.json(err(null, -32600, 'Invalid Request'));
    const { id, method, params } = msg;
    try {
      if (method === 'initialize') {
        return res.json(ok(id, {
          protocolVersion: PROTOCOL_VERSION,
          capabilities: { tools: {} },
          serverInfo: SERVER_INFO,
        }));
      }
      if (method === 'notifications/initialized' || method.startsWith('notifications/')) {
        return res.status(202).end();
      }
      if (method === 'tools/list') {
        return res.json(ok(id, { tools: TOOLS }));
      }
      if (method === 'tools/call') {
        const fn = impl[params?.name];
        if (!fn) return res.json(err(id, -32602, `未知工具: ${params?.name}`));
        const result = await fn(params.arguments || {});
        return res.json(ok(id, result));
      }
      if (method === 'ping') return res.json(ok(id, {}));
      return res.json(err(id, -32601, `Method not found: ${method}`));
    } catch (e) {
      log.error('MCP', `工具执行失败(${method}): ${e.message}`);
      return res.json(err(id, -32000, e.message));
    }
  });

  router.get('/', (req, res) => res.status(405).json(
    err(null, -32000, '本服务仅支持 Streamable HTTP(POST /mcp)')));

  return router;
};
