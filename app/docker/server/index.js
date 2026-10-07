const express = require('express');
const http = require('http');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { spawn } = require('child_process');
const { WebSocketServer } = require('ws');
const db = require('./db');
const { scanLibrary, MV_DIR } = require('./scanner');
const { ensureHLS, removeHLS, outDir, waitForFile, scheduleHLSCleanup } = require('./hlsgen');
const log = require('./logger');

const PORT = process.env.PORT || 8080;
const app = express();
app.use(express.json());

// ---------- 「曲库管理」管理员登录 ----------
// 管理员密码不再通过安装/升级向导收集、也不写进 docker-compose.yml：改成
// 首次打开「曲库管理」(/admin) 时，由用户自己设置一个密码，哈希后存进
// SQLite 的 settings 表（key='admin_password_hash'，见 db.js），跟随 /data
// 一起持久化，升级、容器重建都不受影响。之后每次打开都是登录，不是设置。
// 登录成功后签发一个随机 session token，保存在内存里（进程重启/容器重建
// 后失效，需要重新登录，符合这类局域网轻量应用的预期），通过 httpOnly
// cookie 下发给浏览器。
// 注意：登录状态只用来保护「曲库管理」页面里真正的管理操作（编辑/删除
// 歌曲、改密码）；/api/scan、/api/songs 等电视端、手机点歌页面同样在用的
// 公共接口不受影响——电视端"扫描曲库"本来就需要有人在电视旁边用遥控器
// 操作，风险和曲库管理网页端裸露在局域网里不是一回事。
const ADMIN_PASSWORD_KEY = 'admin_password_hash';
const ADMIN_SESSION_COOKIE = 'ktv_admin_session';
const adminSessions = new Set();

function getAdminPasswordHash() {
  const row = db.prepare('SELECT value FROM settings WHERE key = ?').get(ADMIN_PASSWORD_KEY);
  return row ? row.value : null;
}

function setAdminPasswordHash(hash) {
  db.prepare('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
    .run(ADMIN_PASSWORD_KEY, hash);
}

function sha256Hex(text) {
  return crypto.createHash('sha256').update(String(text), 'utf8').digest('hex');
}

function hashesMatch(a, b) {
  const bufA = Buffer.from(String(a || '').padEnd(64, '0'));
  const bufB = Buffer.from(String(b || '').padEnd(64, '0'));
  return String(a).length === 64 && crypto.timingSafeEqual(bufA, bufB);
}

// 没有引入 cookie-parser，手动解析 Cookie 请求头即可，避免多引入一个依赖。
function parseCookies(req) {
  const header = req.headers.cookie;
  const out = {};
  if (!header) return out;
  header.split(';').forEach(part => {
    const idx = part.indexOf('=');
    if (idx === -1) return;
    const k = part.slice(0, idx).trim();
    const v = part.slice(idx + 1).trim();
    if (k) out[k] = decodeURIComponent(v);
  });
  return out;
}

function isAdminAuthed(req) {
  const token = parseCookies(req)[ADMIN_SESSION_COOKIE];
  return !!(token && adminSessions.has(token));
}

function requireAdminAuth(req, res, next) {
  if (isAdminAuthed(req)) return next();
  res.status(401).json({ error: '请先登录管理员账号' });
}

function startSession(res) {
  const token = crypto.randomBytes(24).toString('hex');
  adminSessions.add(token);
  res.cookie(ADMIN_SESSION_COOKIE, token, {
    httpOnly: true,
    sameSite: 'lax',
    maxAge: 7 * 24 * 60 * 60 * 1000,
  });
}

// 前端据此判断该弹"设置密码"（首次使用）还是"登录"表单。
app.get('/api/admin/session', (req, res) => {
  res.json({ authed: isAdminAuthed(req), passwordSet: !!getAdminPasswordHash() });
});

// 首次使用：设置管理员密码。已经设置过密码后，这个接口不再允许直接覆盖
// （避免任何人不登录、光靠访问这个接口就能重置密码顶替管理员），改密码
// 走下面需要登录态的 /api/admin/change-password。
app.post('/api/admin/setup', (req, res) => {
  if (getAdminPasswordHash()) {
    return res.status(409).json({ error: '管理员密码已设置过，请使用登录' });
  }
  const { password } = req.body || {};
  if (!password || password.length < 4) {
    return res.status(400).json({ error: '密码至少 4 位' });
  }
  setAdminPasswordHash(sha256Hex(password));
  startSession(res);
  log.info('ADMIN', '首次设置曲库管理密码成功');
  res.json({ ok: true });
});

app.post('/api/admin/login', (req, res) => {
  const stored = getAdminPasswordHash();
  if (!stored) {
    return res.status(400).json({ error: '尚未设置管理员密码，请先设置' });
  }
  const { password } = req.body || {};
  const inputHash = password ? sha256Hex(password) : '';
  if (!hashesMatch(inputHash, stored)) {
    log.warn('ADMIN', '曲库管理登录失败：密码错误');
    return res.status(401).json({ error: '密码错误' });
  }
  startSession(res);
  log.info('ADMIN', '曲库管理登录成功');
  res.json({ ok: true });
});

app.post('/api/admin/logout', (req, res) => {
  const token = parseCookies(req)[ADMIN_SESSION_COOKIE];
  if (token) adminSessions.delete(token);
  res.clearCookie(ADMIN_SESSION_COOKIE);
  res.json({ ok: true });
});

// 登录状态下修改密码：需要正确提供当前密码，防止已经打开着「曲库管理」
// 页面的旁人（会话没过期时）随手把密码改掉。改密码后，为安全起见把其它
// 所有已登录的 session 一起失效，只保留当前这一个。
app.post('/api/admin/change-password', requireAdminAuth, (req, res) => {
  const stored = getAdminPasswordHash();
  const { oldPassword, newPassword } = req.body || {};
  const oldHash = oldPassword ? sha256Hex(oldPassword) : '';
  if (!stored || !hashesMatch(oldHash, stored)) {
    return res.status(401).json({ error: '当前密码不正确' });
  }
  if (!newPassword || newPassword.length < 4) {
    return res.status(400).json({ error: '新密码至少 4 位' });
  }
  setAdminPasswordHash(sha256Hex(newPassword));
  const token = parseCookies(req)[ADMIN_SESSION_COOKIE];
  adminSessions.clear();
  if (token) adminSessions.add(token);
  log.info('ADMIN', '曲库管理密码已修改');
  res.json({ ok: true });
});

// ---------- 静态资源 ----------
app.use('/tv',    express.static(path.join(__dirname, '../web/tv')));
app.use('/m',     express.static(path.join(__dirname, '../web/mobile')));
app.use('/admin', express.static(path.join(__dirname, '../web/admin')));
app.use('/cover', express.static('/data/covers'));

// ---------- 在线搜索/下载(代理 ktv-tools 服务, feature/online-fusion) ----------
app.use('/api/online', require('./online'));

// ---------- HLS 播放 (音轨切换不中断播放、进度可寻址) ----------
// 取代了旧的"?track=0/1 现场 ffmpeg 重新封装"方案：那个方案吐出的新流没有
// Content-Length/Range 支持，所以切音轨、以及切完音轨后拖进度条，都只能从
// 头播放。现在把视频轨和每条音频轨分别切成独立的 HLS 分片(.ts)，用一份
// master.m3u8 通过 EXT-X-MEDIA 把所有音频轨声明成同一个 AUDIO group。前端
// hls.js 加载它后，切音轨只是 hls.audioTrack = 0/1，只重新拉音频分片，视频
// 播放位置、连续性完全不受影响；HLS 分片本身天然可寻址，拖进度条对任意音轨
// 都正常工作。单音轨文件走同一套逻辑，master.m3u8 里只声明 1 条音频轨即可，
// 具体生成逻辑见 hlsgen.js。
// 渐进式：ensureHLS 不会等整首歌转码完成才 resolve —— 如果这首歌还没转过，
// 它会立刻创建输出目录、把 master.m3u8 写出来，然后把真正耗时的 ffmpeg 转码
// 丢到后台异步执行，函数本身几乎立即返回。所以这个路由的响应时间只取决于
// "有没有查到歌"和"磁盘 IO"，跟这首歌要转多久没有关系，不会再出现点歌后
// 卡在这一步转圈的情况。
app.get('/hls/:id/master.m3u8', async (req, res) => {
  const song = db.prepare('SELECT * FROM songs WHERE id = ?').get(req.params.id);
  if (!song || !fs.existsSync(song.filepath)) return res.status(404).end();
  log.info('HLS', `请求播放 master.m3u8: id=${song.id} "${song.title || song.filename}"`);
  try {
    const m3u8Path = await ensureHLS(song);
    res.set({ 'Content-Type': 'application/vnd.apple.mpegurl', 'Cache-Control': 'no-store' });
    fs.createReadStream(m3u8Path).pipe(res);
  } catch (e) {
    log.error('HLS', `master.m3u8 生成失败: id=${song.id} "${song.filename}": ${e.message}`);
    res.status(500).end();
  }
});

// 子播放列表(video.m3u8/audioN.m3u8)与分片(.ts)。file 名做白名单校验防止路径穿越，
// id 也强制要求纯数字，避免拼接出 outDir 之外的路径。
//
// 渐进式转码下，这些文件是随着后台 ffmpeg 进程持续产出的：播放器可能会在
// 某个分片刚好还没转出来的瞬间发出请求。这里不再"文件不存在就直接 404"，
// 而是短暂轮询等待它出现（waitForFile），一旦转码进度追上就立即响应——
// 真正做到"随出随播"，而不是让播放器自己重试或者干等整首歌转完。如果这
// 首歌的转码任务本身已经失败，或者等待太久都没等到（比如源文件损坏、卡在
// 极端情况），才会明确地报错而不是无限期挂起请求。
app.get('/hls/:id/:file', async (req, res) => {
  const { id, file } = req.params;
  if (!/^\d+$/.test(id) || !/^[\w.-]+$/.test(file)) return res.status(400).end();
  const p = path.join(outDir(id), file);

  let ready = fs.existsSync(p);
  if (!ready) {
    try {
      await waitForFile(p, id);
      ready = true;
    } catch (e) {
      if (e.code === 'BUILD_FAILED') {
        log.error('HLS', `分片生成失败: id=${id}, file=${file}: ${e.cause && e.cause.message}`);
        return res.status(500).end();
      }
      log.warn('HLS', `等待分片超时: id=${id}, file=${file}`);
      return res.status(404).end(); // 等待超时，视为确实不存在（例如非法文件名/已被清理）
    }
  }

  if (file.endsWith('.m3u8')) res.set({ 'Content-Type': 'application/vnd.apple.mpegurl', 'Cache-Control': 'no-store' });
  else if (file.endsWith('.ts')) res.set({ 'Content-Type': 'video/mp2t', 'Cache-Control': 'public, max-age=31536000, immutable' });
  fs.createReadStream(p).pipe(res);
});

// ---------- MV 直传流 (Range 请求) ----------
// 历史接口，现已不是 TV 播放器的主路径(见上面的 /hls)。保留作为兼容兜底：
// 例如 hls.js 加载失败、或未来某个场景需要拿到原始文件直传时使用。仍支持
// ?track=0/1（对多音轨文件用 ffmpeg -c copy 现场重新封装出单音轨流），但注意
// 这个分支吐出的流不支持 Range/寻址，只适合"整段从头播完"的用途，不要再用它
// 做音轨切换后还要拖进度条的场景——那正是旧 bug 的根因，具体解释见 /hls 路由。
app.get('/stream/:id', (req, res) => {
  const song = db.prepare('SELECT * FROM songs WHERE id = ?').get(req.params.id);
  if (!song || !fs.existsSync(song.filepath)) return res.status(404).end();

  const trackParam = req.query.track;
  const hasMultiTrack = (song.audio_tracks || 1) >= 2;

  if (trackParam !== undefined && hasMultiTrack) {
    const track = Math.max(0, Math.min(parseInt(trackParam, 10) || 0, song.audio_tracks - 1));
    res.writeHead(200, {
      'Content-Type': 'video/mp4',
      'Accept-Ranges': 'none',   // 现场重新封装，长度未知，无法支持 Range 拖进度
      'Cache-Control': 'no-store',
    });
    const ff = spawn('ffmpeg', [
      '-loglevel', 'error',
      '-i', song.filepath,
      '-map', '0:v:0',
      '-map', `0:a:${track}`,
      '-c', 'copy',
      '-movflags', 'frag_keyframe+empty_moov+faststart',
      '-f', 'mp4',
      'pipe:1',
    ]);
    let responded = false;
    ff.stdout.pipe(res);
    ff.stderr.on('data', d => log.warn('TRANSCODE', `[stream直传兜底][ffmpeg] ${d.toString().trim()}`));
    const cleanup = () => { if (!ff.killed) { try { ff.kill('SIGKILL'); } catch (e) {} } };
    ff.on('error', err => { log.error('TRANSCODE', `[stream直传兜底] ffmpeg 启动失败: ${err.message}`); if (!responded) { responded = true; res.status(500).end(); } cleanup(); });
    res.on('close', cleanup);
    return;
  }

  const stat = fs.statSync(song.filepath);
  const range = req.headers.range;
  if (!range) {
    res.writeHead(200, { 'Content-Length': stat.size, 'Content-Type': 'video/mp4' });
    return fs.createReadStream(song.filepath).pipe(res);
  }
  const [s, e] = range.replace(/bytes=/, '').split('-');
  const start = parseInt(s, 10);
  const end = e ? parseInt(e, 10) : stat.size - 1;
  res.writeHead(206, {
    'Content-Range': `bytes ${start}-${end}/${stat.size}`,
    'Accept-Ranges': 'bytes',
    'Content-Length': end - start + 1,
    'Content-Type': 'video/mp4',
  });
  fs.createReadStream(song.filepath, { start, end }).pipe(res);
});

// ---------- 原唱/伴唱切换状态上报 ----------
// 实际的切换动作(hls.audioTrack=0/1 或者声道复制)完全发生在浏览器端
// (见 web/tv/index.html 的 VoiceManager)，服务端本身并不参与、也就无从
// 知晓用户什么时候切了原唱/伴唱。这里加一个轻量上报接口，由前端在每次
// 切换后调用一次，让这个状态变化也能进 docker 后台日志，方便排查
// "切了没生效"之类的问题。上报失败与否不影响播放本身，前端是 fire-and-forget。
app.post('/api/voice/switch', (req, res) => {
  const { song_id, mode, to } = req.body || {};
  const song = song_id ? db.prepare('SELECT id, title, filename FROM songs WHERE id = ?').get(song_id) : null;
  const songTag = song ? `id=${song.id} "${song.title || song.filename}"` : `id=${song_id || '未知'}`;
  const toName = to === 'original' ? '原唱' : to === 'accompaniment' ? '伴唱' : (to || '未知');
  const modeName = mode === 'tracks' ? '多音轨(HLS audioTrack)' : mode === 'stereo' ? '双声道(Web Audio 声道复制)' : (mode || '未知');
  log.info('VOICE', `切换音轨: ${songTag} -> ${toName} (方式: ${modeName})`);
  res.json({ ok: true });
});

// ---------- 歌曲库 ----------
app.get('/api/songs', (req, res) => {
  const q = (req.query.q || '').trim();
  const artist = (req.query.artist || '').trim();
  let rows;
  if (artist) {
    rows = db.prepare('SELECT * FROM songs WHERE artist = ? ORDER BY title').all(artist);
  } else if (q) {
    rows = db.prepare('SELECT * FROM songs WHERE title LIKE ? OR artist LIKE ? ORDER BY play_count DESC LIMIT 100').all(`%${q}%`, `%${q}%`);
  } else {
    // 原来这里写死 LIMIT 200，曲库超过200首后台管理页面/点歌页首字母浏览就只能看到
    // 前200首，后面的歌完全没法管理。曲库列表没有分页机制，这里不再限制条数，
    // 有多少首歌就返回多少首。
    rows = db.prepare('SELECT * FROM songs ORDER BY play_count DESC, id DESC').all();
  }
  res.json(rows);
});

// 按首字母搜索
app.get('/api/songs/letter/:letter', (req, res) => {
  const letter = req.params.letter.toUpperCase();
  const rows = db.prepare('SELECT * FROM songs WHERE UPPER(SUBSTR(title,1,1)) = ? ORDER BY title LIMIT 100').all(letter);
  res.json(rows);
});

// ---------- 歌手列表 ----------
app.get('/api/artists', (req, res) => {
  const rows = db.prepare("SELECT artist, COUNT(*) as count FROM songs WHERE artist IS NOT NULL AND artist != '' GROUP BY artist ORDER BY artist").all();
  res.json(rows);
});

// ---------- 历史 (常唱) ----------
app.get('/api/history', (req, res) => {
  const rows = db.prepare(`
    SELECT s.*, COUNT(h.id) as times_sung
    FROM songs s JOIN history h ON s.id = h.song_id
    GROUP BY s.id ORDER BY times_sung DESC, s.play_count DESC LIMIT 50
  `).all();
  res.json(rows);
});

// ---------- 爱唱榜 (按播放次数) ----------
app.get('/api/charts', (req, res) => {
  const rows = db.prepare('SELECT * FROM songs WHERE play_count > 0 ORDER BY play_count DESC LIMIT 50').all();
  res.json(rows);
});

// ---------- 收藏 ----------
app.get('/api/favorites', (req, res) => {
  const device = req.query.device || 'default';
  const rows = db.prepare(`
    SELECT s.* FROM songs s
    JOIN favorites f ON s.id = f.song_id
    WHERE f.device_id = ? ORDER BY f.created_at DESC
  `).all(device);
  res.json(rows);
});

app.post('/api/favorites/:song_id', (req, res) => {
  const device = req.body.device || 'default';
  db.prepare('INSERT OR IGNORE INTO favorites (song_id, device_id) VALUES (?,?)').run(req.params.song_id, device);
  res.json({ ok: true });
});

app.delete('/api/favorites/:song_id', (req, res) => {
  const device = req.query.device || 'default';
  db.prepare('DELETE FROM favorites WHERE song_id = ? AND device_id = ?').run(req.params.song_id, device);
  res.json({ ok: true });
});

// ---------- 歌曲管理 (Admin) ----------
// 只有这两个真正的"增删改"动作要求登录；/api/scan、/api/songs 等电视端、
// 手机点歌页面共用的接口保持开放，见文件顶部「曲库管理管理员登录」的说明。
app.delete('/api/songs/:id', requireAdminAuth, (req, res) => {
  db.prepare('DELETE FROM songs WHERE id = ?').run(req.params.id);
  removeHLS(req.params.id);
  res.json({ ok: true });
});

app.put('/api/songs/:id', requireAdminAuth, (req, res) => {
  const { title, artist } = req.body;
  db.prepare('UPDATE songs SET title=?, artist=? WHERE id=?').run(title, artist, req.params.id);
  res.json({ ok: true });
});

// ---------- 扫描 / 统计 ----------
app.post('/api/scan', async (req, res) => {
  try { res.json({ ok: true, ...(await scanLibrary()) }); }
  catch (e) { res.status(500).json({ ok: false, error: e.message }); }
});

app.get('/api/stats', (req, res) => {
  const songCount  = db.prepare('SELECT COUNT(*) c FROM songs').get().c;
  const queueCount = db.prepare("SELECT COUNT(*) c FROM queue WHERE status!='done'").get().c;
  res.json({ songCount, queueCount, mvDir: MV_DIR });
});

// ---------- 点歌队列 ----------
function getQueueWithSongs() {
  return db.prepare(`
    SELECT q.id as queue_id, q.nickname, q.is_top, q.status, q.created_at,
           s.id as song_id, s.title, s.artist, s.filename, s.cover, s.duration,
           s.audio_tracks
    FROM queue q JOIN songs s ON q.song_id = s.id
    WHERE q.status != 'done'
    -- 排序修复：置顶只能把一首歌挪到"正在播放"之后的第一位（即整个队列的第二位），
    -- 不能盖过正在播放的那首。旧排序 'is_top DESC, id ASC' 只按置顶标记排，
    -- 完全没考虑播放状态——如果正在播放的这一行本身 is_top=0，任何一首刚被置顶
    -- 的候选歌都会因为 is_top=1 排到它前面，等于把"正在播放"从队首挤下去，
    -- 界面上会显示成"置顶歌曲排在正在播放的歌前面"，观感和语义都不对。
    -- 现在最优先按 status='playing' 排（true=1 排最前），保证正在播放的
    -- 那一行永远占据第一位，其次才按 is_top、再按 id 排——这样置顶操作实际能
    -- 达到的最靠前位置，就是紧跟在正在播放歌曲后面的"第二位"，不会再越过它。
    ORDER BY (q.status='playing') DESC, q.is_top DESC, q.id ASC
  `).all();
}

app.get('/api/queue', (req, res) => res.json(getQueueWithSongs()));

app.post('/api/queue', (req, res) => {
  const { song_id, nickname } = req.body;
  const song = db.prepare('SELECT * FROM songs WHERE id=?').get(song_id);
  if (!song) return res.status(404).json({ error: '歌曲不存在' });
  const info = db.prepare('INSERT INTO queue (song_id,nickname) VALUES (?,?)').run(song_id, nickname || '匿名歌手');
  db.prepare('UPDATE songs SET play_count=play_count+1 WHERE id=?').run(song_id);
  const playing = db.prepare("SELECT * FROM queue WHERE status='playing'").get();
  if (!playing) db.prepare("UPDATE queue SET status='playing' WHERE id=?").run(info.lastInsertRowid);
  broadcastQueue();
  res.json({ ok: true, id: info.lastInsertRowid });
});

app.post('/api/queue/:id/top', (req, res) => {
  // Bug修复：原来只把这一条设成 is_top=1，从不清除其它行的置顶标记。连续给
  // 不同歌曲点"置顶"后，会有多条 is_top=1 的记录同时存在，这些记录之间只能
  // 按 id ASC 排序——最新点的这首排在更早被置顶的那些后面，界面上看起来就是
  // "点了置顶但完全没反应/挪不动"，也就是卡住无法置顶。
  // 修复为：先把所有非播放中的置顶标记清空，再把当前这条设为置顶，保证同一
  // 时刻只有一首歌处于"置顶"状态，每次点击都能确实把这首歌顶到最前面
  // （紧跟在正在播放的歌曲之后）。
  const tx = db.transaction((id) => {
    db.prepare("UPDATE queue SET is_top=0 WHERE status!='playing'").run();
    db.prepare('UPDATE queue SET is_top=1 WHERE id=?').run(id);
  });
  tx(req.params.id);
  broadcastQueue(); res.json({ ok: true });
});

app.delete('/api/queue/:id', (req, res) => {
  db.prepare('DELETE FROM queue WHERE id=?').run(req.params.id);
  broadcastQueue(); res.json({ ok: true });
});

app.post('/api/queue/next', (req, res) => {
  const cur = db.prepare("SELECT * FROM queue WHERE status='playing' ORDER BY id LIMIT 1").get();
  if (cur) {
    db.prepare("UPDATE queue SET status='done' WHERE id=?").run(cur.id);
    db.prepare('INSERT INTO history (song_id,nickname) VALUES (?,?)').run(cur.song_id, cur.nickname);
  }
  const nxt = db.prepare("SELECT * FROM queue WHERE status='waiting' ORDER BY is_top DESC, id ASC LIMIT 1").get();
  if (nxt) db.prepare("UPDATE queue SET status='playing' WHERE id=?").run(nxt.id);
  broadcastQueue(); res.json({ ok: true });
});

// ---------- WebSocket ----------
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: '/ws' });

function broadcastQueue() {
  const payload = JSON.stringify({ type: 'queue', data: getQueueWithSongs() });
  wss.clients.forEach(c => { if (c.readyState === 1) c.send(payload); });
}

wss.on('connection', ws => {
  ws.send(JSON.stringify({ type: 'queue', data: getQueueWithSongs() }));
  ws.on('message', msg => {
    try {
      const p = JSON.parse(msg);
      if (p.type === 'control')
        wss.clients.forEach(c => { if (c.readyState === 1) c.send(JSON.stringify(p)); });
    } catch(e) {}
  });
});

server.listen(PORT, () => {
  log.info('SERVER', `KTV 服务已启动: http://0.0.0.0:${PORT}`);
});

// Bug修复：原来这行代码写在 server.listen 之前、且同步调用 scanLibrary()，
// 等于让整个 HTTP 服务能不能对外提供响应，都卡在"这一轮曲库扫描有没有跑完"
// 这一点上——MV 目录下堆的曲目越多（尤其首次安装、批量导入曲库的场景），
// 主界面/点歌页面能打开、能看到任何歌曲列表的时间就越晚，用户看到的就是
// 长时间白屏/连不上。
// 现在把启动扫描挪到 server.listen 之后再异步触发：端口立刻开始监听，扫描
// 转为后台任务执行；配合 scanner.js 里改成的"逐个文件探测、逐个立即入库"，
// 这时候查询 /api/songs 看到的列表会随扫描推进逐步变长，不需要等这一整轮
// 扫描全部跑完才第一次看到歌曲。
scanLibrary().catch(e => log.error('SCAN', `初始扫描失败: ${e.message}`));

// HLS 缓存每日清理：传入一个"当前曲库里有效歌曲 id 列表"的取值函数，供
// hlsgen.js 判断哪些 HLS 缓存目录是孤儿（对应歌曲已被删除/曲库文件已缺失）。
// 用函数惰性取值而不是在这里查一次库存起来，是因为清理任务每天才跑一次，
// 曲库内容早就可能变了，每次触发清理时都应该拿当次最新的曲库状态判断，
// 不能用注册时那一刻的旧快照。
scheduleHLSCleanup(() => db.prepare('SELECT id FROM songs').all().map(r => r.id));
