import KeepAliveRouteOutlet from "keepalive-for-react-router";
import { useLocation } from "react-router-dom";

import { WorkspaceShell } from "./workspace-shell";
import { useDynamicNav } from "./use-dynamic-nav";
import { workspaceOf } from "../../lib/workspaces";

/*
 * ─────────────────────────────────────────────────────────────
 * 侧栏信息架构（v1.1.0 起**全部动态**）
 *
 * ## 这一版推翻了什么
 *
 * 之前这里硬编码 4 组 24 个叶子项（`overviewNavItem` / `contentLineChildren` /
 * `shopLineChildren` / `platformChildren`）。硬编码有三个做不到：
 *
 *   ① **未授权菜单照样显示** —— 只有 `xhs:item:view` 的用户也能在侧栏看到
 *      「角色管理」，点进去 403。用户看到的是「看得见摸不着」；
 *   ② **平台启停无效** —— 抖音 / 闲鱼在种子里 `status=disabled`，
 *      硬编码的菜单不读 `status`，照样列出来；
 *   ③ **接入新平台要改前端源码 + 重新发版**。
 *
 * 现在数据来自 `GET /api/system/menus/mine`，后端已按
 * 「`status=normal` ∧ 该用户被授权 ∧ `is_visible`」过滤。前端只负责渲染。
 * 菜单的增删改在「系统管理 → 菜单权限」页面，不需要改代码。
 *
 * ## 保留的东西
 *
 * * `WorkspaceShell` 负责布局与高亮（`activeKeys` 支持 `?tab=` 深链）；
 * * `KeepAliveRouteOutlet` 仍只缓存「笔记发现」一处。
 *
 * ## 加载中 / 失败怎么显示
 *
 * 侧栏为 `Spin` 时**不显示空白菜单**：空白菜单会被理解成「我没有权限」，
 * 那是最容易导致误报「管理员把我权限删了」的形状。失败时给一条可点的重试。
 */

export function AppShell() {
  // 平台工作区隔离（设计 62 §2.1）：当前 workspace 由**路由**派生
  // （`/system/*`→system；`/platforms/:id/*`→id；footer 工具页与兜底 → xhs），
  // 传给 useDynamicNav ⇒ 后端只回该工作区的菜单切片。
  // 映射是纯函数（`lib/workspaces.ts`），vitest 直接测分支。
  const location = useLocation();
  const workspace = workspaceOf(location.pathname);
  const nav = useDynamicNav(workspace);

  return (
    <WorkspaceShell
      brand="Spider XHS"
      mainItems={nav.mainItems}
      footerItems={nav.footerItems}
      menuLoading={nav.status === "loading"}
      menuError={nav.status === "error" ? nav.error : null}
      onReloadMenu={nav.reload}
    >
      {/* 路由缓存：笔记发现（含「搜索发现 / 批量抓取」两 Tab）—— tab 是 query，
          pathname 恒为 /discovery/，故一条正则即覆盖两 Tab（设计 21 切片 B）。 */}
      <KeepAliveRouteOutlet include={[/\/platforms\/xhs\/discovery/]} />
    </WorkspaceShell>
  );
}

/*
 * `PageHeader`（eyebrow + 标题 + 描述 + 右侧操作区）**已整块移除**（2026-10-05 用户口径
 * 「移除所有页面页面头」）—— 全站最后一个引用它的页面（任务中心）在 2026-09-27 就已删掉
 * 页头，`.hermes/skill` 与 README/Docs 里的入口无需再指到这里。
 *
 * ⚠️ 这里**不能留一个空壳导出**：`verify_doc_counts.py` 的死代码判据是「不被 frontend/src
 * 任何 import 引用」，留空壳会让「有没有人用」永远为真，页头就会以「组件还在」为理由
 * 悄悄回来。删干净比留壳更能锁住结果。
 */
