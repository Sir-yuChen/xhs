export function formatShanghaiTime(value?: string | null): string {
  if (!value) {
    return "-";
  }
  const normalized = value.replace("T", " ").replace(/\.\d+$/, "");
  return normalized.length >= 19 ? normalized.slice(0, 19) : normalized;
}

/**
 * 耗时（毫秒）的人类可读形式（设计 13 §24.8：起止时间与耗时展示）。
 *
 * 放在 time.ts 而不是某个页面里：账号卡片结果行与任务中心表**共用一份口径**
 * （同一语义两份实现必漂移，DEF-044/047/058 同款纪律）。后端算好 `duration_ms`
 * 传过来，前端只格式化、**不做时间算术**（缺值 ⇒ "-"，不渲染 0 秒）。
 */
export function formatDuration(durationMs?: number | null): string {
  if (durationMs == null || durationMs < 0 || !Number.isFinite(durationMs)) {
    return "-";
  }
  const totalSeconds = Math.floor(durationMs / 1000);
  if (totalSeconds < 1) {
    return "不到 1 秒";
  }
  if (totalSeconds < 60) {
    return `${totalSeconds} 秒`;
  }
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) {
    return seconds > 0 ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes > 0 ? `${hours} 小时 ${restMinutes} 分` : `${hours} 小时`;
}
