// 站内消息 → 目标路由映射（设计 17 §4.5 / 2026-10-13 R2-C 需求）。
//
// 用户口径：「点击通知可跳转到来源页」（提升触发消息提示效率 —— 收到预警后一键到现场，
// 而不是自己去菜单里找）。映射键 = 后端 `notifications.source_type`（见
// `backend/app/services/notification_service.py` 的各 notify_* 与
// `llm_gateway_monitor_service` / `llm_gateway_alert_trigger` 写入时的取值）。
//
// ⚠️ **只映射「有明确落地页」的来源**：找不到落点就返回 `null`，前端只标已读、不跳转
// —— 不猜一个页面把用户带到不相干的地方（同「缺值不造假」纪律）。

/** `source_type` → 目标路径生成器。`id` = `notification.source_id`。 */
const SOURCE_ROUTES: Record<string, (id: number | null) => string> = {
  // LLM 网关预警（llm_gateway_monitor_service / llm_gateway_alert_trigger 写入）
  // ⇒ 大模型网关页（预警规则与用量都在这里看）
  llm_gateway_alert: () => "/llm-gateway",
  // 账号相关（账号被跳过 / 千帆失效等）⇒ 账号页
  account: () => "/platforms/xhs/accounts",
  // 任务失败 / 重试耗尽 ⇒ 任务中心
  task: () => "/tasks",
};

/**
 * 由一条通知解析「点击后应跳转的路径」。
 *
 * @returns 目标路径；无对应落地页 ⇒ `null`（调用方只标已读、不跳转）。
 */
export function notificationTargetPath(
  sourceType: string | null | undefined,
  sourceId: number | null | undefined,
): string | null {
  if (!sourceType) return null;
  const build = SOURCE_ROUTES[sourceType];
  if (!build) return null;
  return build(sourceId ?? null);
}
