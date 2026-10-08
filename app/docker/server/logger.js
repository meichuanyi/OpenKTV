// 统一日志格式：[时间] [标签] 内容
// 所有日志都走 console.log/warn/error，会原样进 stdout/stderr，
// 因此 `docker logs -f openktv-server` 就能看到，不需要额外配置。
//
// 标签约定（方便 grep 排查问题）：
//   [VAAPI]     核显(VAAPI)硬件加速探测/初始化相关
//   [TRANSCODE] 单首歌/单条轨道的转码生命周期(开始/结束/耗时/失败)
//   [VOICE]     原唱/伴唱切换事件
//   [HLS]       播放请求/分片等待相关
//   [HLS_CLEAN] HLS 缓存每日清理任务相关

function ts() {
  const d = new Date();
  const pad = (n, l = 2) => String(n).padStart(l, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

function fmt(tag, msg) {
  return `[${ts()}] [${tag}] ${msg}`;
}

module.exports = {
  info(tag, msg) { console.log(fmt(tag, msg)); },
  warn(tag, msg) { console.warn(fmt(tag, msg)); },
  error(tag, msg) { console.error(fmt(tag, msg)); },
};
