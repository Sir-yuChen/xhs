/**
 * 平台登录态门禁判据（设计 13 §19）。
 *
 * 只保留「这个平台的令牌现在能不能用」这一条尺子 —— 它在三处被共用：
 * 账号卡的「同步数据」（www 门禁）、商品列表的生成 / 同步前置、违规管理页的千帆门禁。
 *
 * ⚠️ **同步按钮的门禁与结果行文案已随同步链路退场**（设计 75 §4.5，2026-10-27）：
 * `noteSyncPlan` / `goodsNoteSyncPlan` / `goodsGate` / `syncTaskPhase` /
 * `syncResultText` / `goodsResultText` / `syncTerminalNotice` / `syncProgressText`
 * 全部删除 —— 账号卡不再有「同步笔记」「同步商品笔记」按钮，也没有轮询与终态提示。
 *
 * 判据只有一份：`UNAVAILABLE_STATES` / `channelUnavailable` 与 v1.31 的门禁
 * **同一条尺子**（unknown 放行 —— 没问到 ≠ 不可用，DEF-057 红线）。
 */

import { loginChannelRows } from "./login-state";
import type { XhsLoginChannels } from "../types";

/**
 * 平台会话「确定不可用」的三种状态（v1.31，用户 2026-09-21 口径：
 * 「同步笔记和同步商品走千帆，千帆会话不可用时弹窗提示先补登千帆」；
 * 「[同步数据]走pc获取粉丝关注获赞数，如果pc会话不可用则提示登录」）。
 *
 * ⚠️ **`unknown`（未检测 / 探活没得到结论）不在此列** —— 那是 DEF-057 的同型红线：
 * 用「没检查」冒充「不可用」会让用户去做一次根本不需要的重登。unknown 时放行，
 * 让同步真发一次，失败原因由后端结构化地给到卡片。
 */
export const UNAVAILABLE_STATES = ["missing", "guest", "expired"];

/** 笔记源的中文名（§19.3 分源结果行；与后端 `login_state_service.CHANNEL_LABELS` 对齐）。 */
export const SOURCE_LABELS: Record<string, string> = {
  www: "网页版",
  creator: "创作者平台",
  ark: "千帆商品笔记",
};

/**
 * 某个平台的会话现在是否「确定不可用」（同步按钮的前置门禁判据）。
 *
 * 千帆门禁（ark）与网页版门禁（www）**共用这一个判据** —— 两条门禁是同一个
 * 语义（「这个平台的令牌现在能不能用」），写两份必然在其中一份上漂移。
 */
export function channelUnavailable(
  channels: XhsLoginChannels | undefined,
  channel: string
): boolean {
  const row = loginChannelRows(channels).find((item) => item.channel === channel);
  if (!row) {
    // loginChannelRows 保证三个平台永远都在；防御性兜底按「不可用」处理
    // （真缺了这一行，后端也没算这个平台，此时放行等于闭眼同步）。
    return true;
  }
  return UNAVAILABLE_STATES.includes(row.view.state);
}

/** 某个平台行的当前状态（决定续期弹窗标题是「补齐」还是「重新登录」）。 */
export function channelState(
  channels: XhsLoginChannels | undefined,
  channel: string
): string {
  const row = loginChannelRows(channels).find((item) => item.channel === channel);
  return row?.view.state ?? "missing";
}
