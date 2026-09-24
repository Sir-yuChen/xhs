/**
 * 同步按钮的**门禁与结果行**纯逻辑（设计 13 §19）。
 *
 * 为什么单独一个模块：账号卡片上的「同步笔记 / 同步商品」点下去之后发生什么，
 * 是**有契约的决策**（三态门禁、缺哪些平台、结果行长什么样），不是页面装饰。
 * 决策留在页面组件里 ⇒ 只能靠源码断言验证，而源码断言证明不了「函数怎么跑」
 * （DEF-058 的教训）—— 于是这里与 `login-state.ts` 同一分工：纯函数、
 * 可被 esbuild 打包后直接驱动（见 `tests/frontend/sync_gates.cjs`）。
 *
 * 判据只有一份：`UNAVAILABLE_STATES` / `channelUnavailable` 与 v1.31 的门禁
 * **同一条尺子**（unknown 放行 —— 没问到 ≠ 不可用，DEF-057 红线）。
 */

import { loginChannelRows } from "./login-state";
import { formatDuration } from "./time";
import type { XhsSyncLastResult, XhsSyncProgress } from "./api";
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

/** 同步笔记的三态决策（§19.3，用户 2026-09-21 口径）。 */
export interface NoteSyncPlan {
  /** 一个可用源都没有 ⇒ 不发起同步，弹窗提示分别登录。 */
  blocked: boolean;
  /** PC 账号走网页版源前要先录主页链接（§6.1.1：URL 级 xsec_token）。 */
  needsLink: boolean;
  /** 本次预期会跑的源（展示用；后端按令牌现状最终自决）。 */
  sources: string[];
}

/**
 * 点「同步笔记」时的决策（纯函数，页面只负责照做）：
 *
 * | 创作者平台会话 | 行为 |
 * |---|---|
 * | 可用 | 直接同步（creator 源 + ark 源） |
 * | 确定不可用（missing / guest / expired） | 不发起同步，弹窗提示**补齐创作者平台登录后再触发** |
 * | unknown（未检测） | 放行，让同步真发一次（DEF-057 纪律） |
 *
 * ⚠️ **2026-09-22 起 www 源不再参与任何账号的笔记同步**（用户口径：「www 源在这里是
 * 冗余的，则移除不要，使用 creator 源」+「同步笔记按钮该账号必须存在有效的创作者
 * 登录会话，不存在有效创作者平台登录会话的提示补齐后再触发」）。因此：
 *
 * * `blocked` 的判据从「三源全不可用」收紧为「**创作者平台会话不可用**」——
 *   这是**单一硬门禁**，不再看 www / ark 的脸色（ark 是附属的商品笔记源，
 *   撑不起一次「同步笔记」）；
 * * `needsLink` **恒为 false**：主页链接录入窗要的是 www 链路的 URL 级
 *   `xsec_token`，没有 www 源就没有它的用武之地。UI 暂留（不会触发），
 *   以免恢复 www 源时要重写一遍。
 */
export function noteSyncPlan(
  channels: XhsLoginChannels | undefined,
  _subType?: string | null
): NoteSyncPlan {
  const creatorOk = !channelUnavailable(channels, "creator");
  const arkOk = !channelUnavailable(channels, "ark");
  const sources: string[] = [];
  if (creatorOk) {
    sources.push("creator");
    // ark 只在 creator 成立时才列 —— 它是附属源，不是独立入口。
    if (arkOk) sources.push("ark");
  }
  return {
    blocked: !creatorOk,
    needsLink: false,
    sources,
  };
}

/** 同步商品门禁（§19.4）：千帆必须可用；文案按状态区分「失效」与「未登录」。 */
export function goodsGate(
  channels: XhsLoginChannels | undefined
): { blocked: boolean; arkState: string } {
  return {
    blocked: channelUnavailable(channels, "ark"),
    arkState: channelState(channels, "ark"),
  };
}

/** 同步**进行中**的进度行（设计 13 §6.2）。 */
export function syncProgressText(progress: XhsSyncProgress | null, unit: string): string {
  const stored = progress?.stored ?? 0;
  const total = progress?.total;
  return `已落库 ${stored} / 共 ${total ?? "-"} ${unit}`;
}

/** 同步**已结束**的结果行：优先用后端 `note`（限流/部分完成的说明由后端给，前端不硬编码业务判断）。 */
export function syncResultText(result: XhsSyncLastResult | null, unit: string): string {
  if (!result) {
    return "同步已结束";
  }
  // §24.8：终态都带耗时（后端算好的 duration_ms；缺值时不拼，不渲染 0 秒）。
  const duration = formatDuration(result.duration_ms);
  const withDuration = (text: string) =>
    duration === "-" ? text : `${text} · 耗时 ${duration}`;
  // §24.8：取消是终态的一种（后端 latest_finished_task 已含 cancelled）——
  // 用户刚点过取消，结果行必须说「已取消」而不是退回「同步已结束」。
  if (result.status === "cancelled") {
    const parts = ["已取消"];
    if (result.note && result.note !== "已被用户取消") {
      parts.push(result.note);
    }
    return withDuration(parts.join(" · "));
  }
  if (result.status === "failed") {
    return withDuration(result.error ? `同步失败：${result.error}` : "同步失败");
  }
  const head = `上次同步 ${syncProgressText(result.progress, unit)}`;
  const parts = [head];
  // §19.3：分源摘要（每个源一行「源 名 N 条」；有失败的源带上原因）。
  // 前端**不重新判断**业务，只把后端给的 sources 摆出来。
  if (result.sources && result.sources.length > 0) {
    const summary = result.sources
      .map((entry) => {
        const name = SOURCE_LABELS[entry.source] || entry.source;
        if (entry.error) {
          return `${name}：${entry.error}`;
        }
        // §19.11 真机教训：被限流 ⇒ 0 条是「没跑完」不是「没数据」，
        // 必须分开说（否则用户以为千帆真的没笔记）。
        if (entry.limited) {
          return `${name}：被限流未跑完`;
        }
        return `${name} ${entry.stored} 条`;
      })
      .join("；");
    parts.push(summary);
  }
  if (result.note) {
    parts.push(result.note);
  }
  return withDuration(parts.join(" · "));
}

/** 商品同步结果行：列表进度 + SKU 规格详情进度（内联补齐，2026-09-23 重构）。 */
export function goodsResultText(result: XhsSyncLastResult | null): string {
  const base = syncResultText(result, "件");
  if (!result || result.status === "failed" || result.detail_done == null) {
    return base;
  }
  // 全店还剩多少商品没详情（增量同步可能只覆盖到新商品，老商品要等下次全量同步）。
  // 取不到时渲染 `-` 不渲染 0（缺值口径：「不知道还剩多少」与「还剩 0 件」是两件事）。
  const remaining = result.detail_total == null ? "-" : String(result.detail_total);
  return `${base} · 规格详情 ${result.detail_done} 件（剩余 ${remaining}）`;
}
