/*
 * 侧栏菜单 —— **全部由后端下发**（v1.1.0 / 设计 50 §4 / 56 §3.3）。
 *
 * ## 为什么必须改成动态（这一版推翻了硬编码菜单）
 *
 * v1.1.0 之前这里写死 4 组 24 个叶子项。硬编码做不到三件事：
 *   ① 「未授权的菜单不显示」—— 硬编码的话，一个只有 `xhs:item:view` 的用户
 *      侧栏里仍然列出全部 24 项，点进去才 403（体验是「看得见摸不着」）；
 *   ② 「平台启停」—— 抖音 / 闲鱼写进种子但 `status=disabled`，
 *      硬编码的菜单根本不读 `status`；
 *   ③ 「新增平台零改动」—— 接入一个新平台要改前端源码 + 重新构建发版。
 *
 * 现在侧栏数据来自 `GET /api/system/menus/mine`，后端已按
 * 「`status=normal` ∧ 该用户被授权 ∧ `is_visible`」过滤好。
 *
 * ## 图标怎么来（后端下发的是字符串）
 *
 * 后端 `menus.icon` 存的是 antd 图标**组件名**（如 `"DashboardOutlined"`），
 * 不是 React 元素。这里用白名单映射成组件 —— **不能用动态 import / eval**：
 * ① 打包器无法静态分析，图标不会进产物；② 后端下发字符串直接当代码执行是注入面。
 * 白名单缺项时不渲染图标（**不报错**）—— 菜单项本身照常显示，
 * 缺图标只是少个装饰，不该让整个侧栏挂掉。
 *
 * ## 三级结构 → antd Menu
 *
 * 后端 M（目录）→ antd `SubMenu`（有 children 时）；
 * 后端 C（页面）→ 叶子项，`key` = 它的 `path`；
 * 后端 F（按钮）→ **不进侧栏**（权限码，按钮那边用 `hasPermission` 判）。
 *
 * ⚠️ 叶子 `key` 用 `path`（路由），因为 `WorkspaceShell` 用 `location.pathname`
 * 匹配高亮。带 `?tab=` 的菜单项在种子里 path 就是 `"...?tab=images"`，
 * `WorkspaceShell` 的 `activeKeys` 已按「含 query 的键比对 pathname+search」处理。
 */

import type { MenuProps } from "antd";
import { useEffect, useMemo, useState } from "react";

import { fetchMyMenus, type SystemMenuNode } from "../../lib/api";
import { menuIcon } from "../../lib/menu-icon";

/** 后端树 → antd Menu items。F 行（按钮权限）不进侧栏。 */
function toMenuItems(nodes: SystemMenuNode[]): MenuProps["items"] {
  const items: MenuProps["items"] = [];
  for (const node of nodes) {
    if (node.menu_type === "F") continue;
    const children = node.children?.length ? toMenuItems(node.children) : [];
    if (children && children.length > 0) {
      items.push({
        key: `m-${node.id}`, // 父级 key 不是路由（antd 父级不可选中）
        icon: menuIcon(node.icon),
        label: node.name,
        children,
      });
      continue;
    }
    // 叶子：key 必须是路由（WorkspaceShell 用它比对 pathname）
    if (!node.path) continue; // 没有 path 的空目录不渲染（点不进去）
    items.push({
      key: node.path,
      icon: menuIcon(node.icon),
      label: node.name,
    });
  }
  return items;
}

export interface DynamicNav {
  status: "loading" | "ready" | "error";
  mainItems: MenuProps["items"];
  footerItems: MenuProps["items"];
  error: string | null;
  reload: () => void;
}

/**
 * 分组判据（后端 `menus.workspace`）：
 *  - 工作区（`xhs` / `ai` / `system`）与平台（`douyin` / `xianyu` …）→ **主导航**
 *  - 公共工具（`/tasks`、`/task-records`、`/models`、`/settings`）→ **底部**（footer）
 *
 * 底部那几项的 `workspace` 在种子里是空串或 `common`，按「是否在 footer 白名单路由里」
 * 判定更稳（路由是唯一标识，`workspace` 只是分类标签）。
 */
const FOOTER_PATHS = new Set([
  "/tasks",
  "/task-records",
  "/models",
  "/settings",
  //: 工作区级任务中心（设计 69）。用户口径（2026-10-25）：
  //: 「应是任务中心放到队列记录一起」「底部菜单中」。
  //: 它和「队列记录」是同一类东西 —— 都是「跑过什么 / 要跑什么」的运维视图，
  //: 不参与业务线导航（内容线 / 店铺线）。
  "/platforms/xhs/tasks",
]);

function splitFooter(nodes: SystemMenuNode[]): {
  main: SystemMenuNode[];
  footer: SystemMenuNode[];
} {
  const main: SystemMenuNode[] = [];
  const footer: SystemMenuNode[] = [];
  // footer 项通常在顶级且没有子节点
  for (const node of nodes) {
    if (node.path && FOOTER_PATHS.has(node.path)) {
      footer.push(node);
    } else {
      main.push(node);
    }
  }
  return { main, footer };
}

export function useDynamicNav(workspace?: string): DynamicNav {
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [nodes, setNodes] = useState<SystemMenuNode[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");
    fetchMyMenus(workspace)
      .then((payload: { items?: SystemMenuNode[]; total?: number }) => {
        if (cancelled) return;
        setNodes(payload.items ?? []);
        setStatus("ready");
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // 加载失败**不静默**：侧栏空白时必须说明原因，否则用户以为是「没有权限」。
        setError(err instanceof Error ? err.message : "菜单加载失败");
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [tick, workspace]); // ⚠️ workspace 变化 ⇒ 重新拉取（AppShell 按路由派生，设计 62）

  const { mainItems, footerItems } = useMemo(() => {
    const { main, footer } = splitFooter(nodes);
    return {
      mainItems: toMenuItems(main),
      footerItems: toMenuItems(footer),
    };
  }, [nodes]);

  return {
    status,
    mainItems,
    footerItems,
    error,
    reload: () => setTick((n) => n + 1),
  };
}
