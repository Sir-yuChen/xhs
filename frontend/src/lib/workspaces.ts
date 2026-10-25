/*
 * 平台工作区（设计 62）—— 路由 ↔ workspace 的**纯函数**映射。
 *
 * ## 为什么抽成 lib
 *
 * `AppShell` 要用 `workspaceOf(location.pathname)` 决定向后端要哪一片菜单
 * （`fetchMyMenus(workspace)`），vitest 也要直接测分支。组件文件里写纯函数
 * 会让测试被迫 import 整个壳（keepalive / antd 全量进模块图），按项目惯例
 * （`lib/notification-routes.ts`、`lib/account-scope.ts` 同款）放 lib 并就近测试。
 *
 * ## 映射口径（设计 62 §2.1）
 *
 * * `/system/...` → `"system"`（系统管理独立工作区，不是小红书平台内的菜单）；
 * * `/platforms/:platformId/...` → 该 platformId（`/platforms/xhs/*` → xhs，
 *   `/platforms/system` → system，未知平台 → 它自己）；
 * * `/tasks` `/task-records` `/models` `/settings` `/403` → `"xhs"`
 *   （footer 工具页历史上挂在小红书工作区）；
 * * 其余（含 `/`）→ `"xhs"`（兜底，登录后的默认工作区）。
 *
 * ⚠️ 只吃 `pathname`（不含 query）—— 菜单键里带 `?tab=` 的高亮由
 * `WorkspaceShell.resolveActiveKeys` 管，与工作区判定是两件事。
 */

/** AppShell 布局内、不属于 `/system` 也不属于 `/platforms/:id` 的路由 → xhs。 */
const XHS_FALLBACK_ROUTES = new Set([
  "/tasks",
  "/task-records",
  "/models",
  "/settings",
  "/403",
]);

export function workspaceOf(pathname: string): string {
  if (pathname === "/system" || pathname.startsWith("/system/")) {
    return "system";
  }
  const match = /^\/platforms\/([^/]+)/.exec(pathname);
  if (match) return match[1];
  // footer 工具页显式归 xhs（与其余兜底同值，但分开列是为了一眼看出口径）
  if (XHS_FALLBACK_ROUTES.has(pathname)) return "xhs";
  return "xhs";
}

/** 「公共」工作区（设计 62 §2.2）：不隶属任何平台的平台级能力。 */
export const PUBLIC_WORKSPACES: ReadonlySet<string> = new Set(["ai", "system"]);

/**
 * 工作区卡片点击落点（设计 62 §2.2）：
 * `ai` → `/llm-gateway`（独立公共模块，v1.4 起脱离平台前缀）；
 * 其余（xhs / system / 未来新平台）→ `/platforms/<workspace>`。
 */
export function workspaceEntryPath(workspace: string): string {
  if (workspace === "ai") return "/llm-gateway";
  return `/platforms/${workspace}`;
}

/** 工作区标识 → 界面显示名（2026-10-25）。 */
export const WORKSPACE_LABELS: Readonly<Record<string, string>> = {
  xhs: "小红书",
  ai: "AI 网关",
  system: "系统管理",
};

/** 工作区的中文显示名（未知标识原样返回 —— 不造名）。 */
export function workspaceLabel(workspace: string): string {
  return WORKSPACE_LABELS[workspace] ?? workspace;
}
