// 歌词服务 —— Phase 3 新增:网易云 LRC 获取 + 本地缓存 + 解析。
//
// 网易云的搜索经常把翻唱/Live版排在前面,这里做简单评分:歌手名精确包含
// +3、歌名互相包含 +2、疑似伴奏/翻唱 -2,分数<=0 视为没找到(宁缺毋滥,
// 免得给《晴天》配上小爱翻唱版的时间轴)。
// LRC 是"行级"时间戳,逐字扫光由前端按行内进度线性插值——不是真实逐字
// 对齐,但视觉上就是 KTV 那个味道;时间轴整体偏移可用电视端 ±0.5s 校准。

const express = require('express');
const fs = require('fs');
const path = require('path');

module.exports = function createLyricsRouter({ db, log }) {
  const router = express.Router();
  const CACHE_DIR = '/data/lyrics';
  try { fs.mkdirSync(CACHE_DIR, { recursive: true }); } catch (_) { /* 已存在 */ }

  function parseLrc(lrc) {
    const out = [];
    for (const line of String(lrc || '').split('\n')) {
      const m = line.match(/^\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\](.*)$/);
      if (!m) continue;
      const t = (+m[1]) * 60 + (+m[2]) + (m[3] ? parseFloat(`0.${m[3]}`) : 0);
      const text = m[4].trim();
      if (text) out.push({ t: Math.round(t * 1000) / 1000, text });
    }
    out.sort((a, b) => a.t - b.t);
    return out;
  }

  async function netease(pathname) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const res = await fetch('https://music.163.com' + pathname, {
        headers: {
          Referer: 'https://music.163.com',
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120',
        },
        signal: controller.signal,
      });
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  // QQ 音乐兜底:周杰伦等艺人的版权不在网易云,网易搜不到官方版时走这里。
  async function qq(pathname) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const res = await fetch('https://c.y.qq.com' + pathname, {
        headers: { Referer: 'https://y.qq.com', 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120' },
        signal: controller.signal,
      });
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  router.get('/:song_id', async (req, res) => {
    const song = db.prepare('SELECT id,title,artist FROM songs WHERE id=?').get(req.params.song_id);
    if (!song) return res.status(404).json({ error: '歌曲不存在' });
    const cacheFile = path.join(CACHE_DIR, `${song.id}.json`);
    try {
      const cached = JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
      // 命中的永久用缓存;空结果一天内不重试,避免每首都白等一次超时
      if (cached.lyrics || Date.now() - (cached.fetched_at || 0) < 86400000) {
        return res.json(cached);
      }
    } catch (_) { /* 无缓存 */ }

    try {
      const wantTitle = song.title.replace(/\s*[(【].*?[)】]\s*/g, '').trim();
      // 网易云搜索爱把翻唱/Live顶到前面(搜「晴天 周杰伦」首页全是翻唱),
      // 策略:逐页找,歌手已知时必须命中歌手(+3)才算数,找不到宁可无歌词。
      let best = null;
      for (let page = 0; page < 2 && !best; page++) {
        const q = encodeURIComponent(`${song.title} ${song.artist || ''}`.trim());
        const search = await netease(`/api/search/get/web?s=${q}&type=1&offset=${page * 10}&limit=10`);
        for (const s of (search.result || {}).songs || []) {
          const name = s.name || '';
          const artists = (s.artists || []).map(a => a.name).join(',');
          let score = 0;
          if (song.artist && artists.includes(song.artist)) score += 3;
          if (name.includes(wantTitle) || wantTitle.includes(name)) score += 2;
          // 翻唱/Live/伴奏版降分;「XX版」要单独锚定歌名尾部,拼上歌手后 $ 会失效
          if (/伴奏|翻唱|cover|live/i.test(name) || /版$/.test(name.trim())) score -= 2;
          const need = song.artist ? 5 : 2; // 有歌手信息时必须歌手+歌名双命中
          if (score >= need && (!best || score > best.score)) best = { ...s, score };
        }
      }
      let lyrics = null;
      let source = '';
      if (best) {
        const lrc = await netease(`/api/song/lyric?id=${best.id}&lv=1&kv=1&tv=-1`);
        const parsed = parseLrc((lrc.lrc || {}).lyric);
        if (parsed.length >= 5) {
          lyrics = parsed;
          source = `网易云 · ${(best.artists || [])[0]?.name || ''}《${best.name}》`;
        }
      }
      if (!lyrics) {
        // QQ 音乐兜底:要求歌手命中且歌名匹配,跳过 Live/伴奏版
        const q = encodeURIComponent(`${song.title} ${song.artist || ''}`.trim());
        const qqSearch = await qq(`/soso/fcgi-bin/client_search_cp?w=${q}&format=json&n=10`);
        const qqList = ((qqSearch.data || {}).song || {}).list || [];
        const qqBest = qqList.find(s => {
          const name = s.songname || '';
          const singers = (s.singer || []).map(a => a.name).join(',');
          const titleOk = name.includes(wantTitle) || wantTitle.includes(name);
          const artistOk = !song.artist || singers.includes(song.artist);
          const junk = /live|伴奏|翻唱|版$|cover/i.test(name.trim());
          return titleOk && artistOk && !junk;
        });
        if (qqBest) {
          const qly = await qq(`/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid=${qqBest.songmid}&format=json&nobase64=1`);
          const parsed = parseLrc(qly.lyric);
          if (parsed.length >= 5) {
            lyrics = parsed;
            source = `QQ音乐 · ${(qqBest.singer || [])[0]?.name || ''}《${qqBest.songname}》`;
          }
        }
      }
      const payload = { lyrics, source, fetched_at: Date.now() };
      try { fs.writeFileSync(cacheFile, JSON.stringify(payload)); } catch (_) { /* 缓存失败不影响返回 */ }
      log.info('LYRICS', `《${song.title}》${lyrics ? `取到 ${lyrics.length} 行(${source})` : '未找到匹配歌词'}`);
      res.json(payload);
    } catch (e) {
      log.warn('LYRICS', `歌词获取失败(《${song.title}》): ${e.message}`);
      res.status(502).json({ lyrics: null, error: e.message });
    }
  });

  return router;
};
