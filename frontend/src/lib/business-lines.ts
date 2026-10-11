/**
 * 业务线的**呈现映射**（设计 32 §5.2 / §5.3；2026-10-11）。
 *
 * ## 为什么单独成一个模块
 *
 * 这里的文案要被**源码守卫**断言（「这一页必须标出它属于哪条业务线、要哪条令牌」），
 * 而这类「不许出现某种映射」的规则**写在页面里就测不了**：页面是 `.tsx`，
 * 没有前端组件测试框架，断言只能落在**能被 import 的纯模块**上
 * （先例：`lib/login-state.ts` 与 `tests/frontend/login_state.cjs`）。
 *
 * ## 判据来自后端，不在这里重判
 *
 * 「谁是店铺线」是 `backend/app/core/business_lines.py` 的答案（能力派生：
 * 店铺线 = `ark` 通道有令牌）。本模块只管**怎么显示**，不重新判一次 ——
 * 前端重判必然与后端漂移（先例：设计 12 §17.8 删掉 `capabilityFor`）。
 *
 * ## 与侧栏分组的关系
 *
 * 同一份分组（`components/layout/app-shell.tsx` 的 `type: "group"`）在**页面内**
 * 再显式标一次：市场共识是「让上下文在选择时可见，不靠记忆」——
 * 用户不该靠「刚才点的是哪一组」来推断本页要哪条令牌。
 */

import type { BusinessLine } from "../types";

/** 一条业务线的呈现信息。 */
export type BusinessLineView = {
  /** 线名（与侧栏分组标题逐字一致）。 */
  label: string;
  /** 该线用的令牌通道（`shared` 为空 —— 它不绑定单一令牌）。 */
  channel: string;
  /** 通道的展示名（用 `login-state.ts` 的全名，不在本文件另造）。 */
  channelLabel: string;
  /** 一行说明（页面顶部展示）。 */
  caption: string;
};

/**
 * 三条业务线的呈现信息。**唯一实现** —— 页面不许各写一份文案。
 *
 * ⚠️ `shared`（「平台」）**没有通道**：给它配一个通道等于承认「平台线也要令牌」，
 * 而平台级页面（账号矩阵 / 数据洞察 / 监控中心 / 自动运营）恰恰是**跨线**的。
 */
export const BUSINESS_LINE_VIEWS: Record<BusinessLine, BusinessLineView> = {
  content: {
    label: "内容线",
    channel: "creator",
    channelLabel: "创作者平台",
    caption: "内容线 · 创作者令牌",
  },
  shop: {
    label: "店铺线",
    channel: "ark",
    channelLabel: "千帆商家后台",
    caption: "店铺线 · 千帆令牌",
  },
  shared: {
    label: "平台",
    channel: "",
    channelLabel: "",
    caption: "平台 · 跨业务线",
  },
};

/**
 * 路由前缀 → 业务线（设计 32 §5.1 的侧栏分组，逐项对应）。
 *
 * ⚠️ **必须是全量映射**：任何一条业务线页面在这里漏登记，页面顶部就会**没有**
 * 业务线标识 —— 而这种「少一个」在界面上看起来跟正常页面一模一样，不会报错。
 * 守卫 `tests/backend/test_sidebar_business_lines.py` 拿它与侧栏分组做**双向**比对。
 */
export const ROUTE_BUSINESS_LINES: Record<string, BusinessLine> = {
  // 内容线（用创作者令牌）
  "/platforms/xhs/discovery": "content",
  "/platforms/xhs/my-notes": "content",
  "/platforms/xhs/library": "content",
  "/platforms/xhs/note-workshop": "content",
  "/platforms/xhs/keywords": "content",
  "/platforms/xhs/insights/content": "content",
  // 店铺线（用千帆 ark 令牌）
  "/platforms/xhs/shop-items": "shop",
  "/platforms/xhs/picture-space": "shop",
  "/platforms/xhs/orders": "shop",
  "/platforms/xhs/insights/shop": "shop",
  // 平台（跨线）
  "/platforms/xhs/accounts": "shared",
  "/platforms/xhs/monitoring": "shared",
  "/platforms/xhs/auto-ops": "shared",
};

/**
 * 当前路径属于哪条业务线；不属于任何业务线页面（如总览）时返回 `null`。
 *
 * 用**最长前缀**匹配：路由有嵌套（`/platforms/xhs/analytics` 与将来的子路由），
 * 短前缀先命中的话，子路由会被判成父路由的线。
 */
export function businessLineForPath(pathname: string): BusinessLine | null {
  let best: string | null = null;
  for (const route of Object.keys(ROUTE_BUSINESS_LINES)) {
    if (pathname === route || pathname.startsWith(`${route}/`)) {
      if (best === null || route.length > best.length) best = route;
    }
  }
  return best === null ? null : ROUTE_BUSINESS_LINES[best];
}

/**
 * 未登录该线令牌时的**可执行提示**（灰显 + 说明原因，**不隐藏**）。
 *
 * 依据 `docs/02_设计文档/08` §4.5：*「隐藏起来只会被当成 bug 报过来」*。
 * 返回空串表示这条线不需要补登（`shared`）。
 */
export function businessLineGateHint(line: BusinessLine): string {
  const view = BUSINESS_LINE_VIEWS[line];
  if (!view.channel) return "";
  return `本页使用${view.channelLabel}令牌；未登录时页面会提示补登，不会静默失败。`;
}
