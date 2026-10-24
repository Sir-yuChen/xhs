/*
 * 系统管理工作区（v1.1.0 / 设计 46 §4 / 49 模块一~九）。
 *
 * ## 每个菜单一个独立页面（设计 62 §2.5，2026-10-25 用户口径「每个菜单一个页面
 *    拆分隔离出来」，推翻早期的「一个页面 + 5 个 Tab」形态）
 *
 * 6 条路由（/system/users|roles|menus|dicts|settings|logs）各自**只渲染
 * 对应的面板**——共享页签条整条移除（用户截图点名）。菜单数据不动（种子里的
 * C 行 path 就是这 6 条路由），层级仍为 工作区 → 目录 → 页面 → 按钮。
 *
 * ## 权限判据（页级）
 *
 * 页面本身要求「至少有一个 `system:*:view` 码」—— 由后端 `menus/mine` 决定
 * 侧栏是否显示它。直接输 URL 时，面板内**每个请求**都会 403
 * （后端 `require_perm`），页面上呈现为面板内的错误提示。
 *
 * ⚠️ 这里**不做前端路由守卫**（不写 `<Navigate to="/403">`）：
 * 因为「能不能进这个页面」本质上由「有没有某个 view 码」决定，
 * 而那件事已经有后端兜底。前端再拦一次只会让「管理员临时收回权限」
 * 时出现两层错误提示（一层前端跳转 + 一层接口 403），反而更糊涂。
 */

import { Card, Typography } from "antd";
import { Navigate, useLocation } from "react-router-dom";

import { usePermission } from "../../hooks/use-permission";
import { SystemDictPanel } from "./system-dict-panel";
import { SystemLogsPanel } from "./system-logs-panel";
import { SystemMenusPanel } from "./system-menus-panel";
import { SystemRolesPanel } from "./system-roles-panel";
import { SystemUsersPanel } from "./system-users-panel";

const { Text } = Typography;

/** 路由 → 面板映射（种子菜单 C 行的 6 条 path）。`perm` 供权限空态判定。 */
const PAGES = [
  { path: "/system/users", perm: "system:user:view", render: () => <SystemUsersPanel /> },
  { path: "/system/roles", perm: "system:role:view", render: () => <SystemRolesPanel /> },
  { path: "/system/menus", perm: "system:menu:view", render: () => <SystemMenusPanel /> },
  { path: "/system/dicts", perm: "system:dict:view", render: () => <SystemDictPanel /> },
  //: 「配置管理」**复用现有的设置页**（`/settings`，已有 9 个 LIVE_KEYS 与
  //: 「应用到所有用户」），不在系统管理里重做一套 ⇒ 直接重定向过去。
  { path: "/system/settings", perm: "system:setting:view", render: () => <Navigate to="/settings" replace /> },
  { path: "/system/logs", perm: "system:log:view", render: () => <SystemLogsPanel /> },
] as const;

export function SystemPage() {
  const location = useLocation();
  const { has, isReady } = usePermission();

  const current = PAGES.find((page) => page.path === location.pathname) ?? PAGES[0];

  if (isReady && !PAGES.some((page) => has(page.perm))) {
    return (
      <Card>
        <Text type="secondary">
          你没有系统管理域的任何查看权限。如需访问，请联系管理员分配对应角色。
        </Text>
      </Card>
    );
  }

  return current.render();
}
