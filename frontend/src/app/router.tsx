import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";

import { AppShell } from "../components/layout/app-shell";
import { ComingSoonPage } from "../components/platforms/coming-soon";
import { ProtectedRoute, PublicOnlyRoute } from "../components/ui/protected-route";
import { LoginPage } from "../pages/login/login-page";
import { ModelConfigPage } from "../pages/models/model-config-page";
import { PlatformSelectPage } from "../pages/platform-select/platform-select-page";
import { SettingsPage } from "../pages/settings/settings-page";
import { TaskRecordsPage } from "../pages/tasks/task-records-page";
import { WorkspaceTasksPage } from "../pages/tasks/workspace-tasks-page";
import { AutoOpsPage } from "../pages/platforms/xhs/auto-ops-page";
import { OrdersPage } from "../pages/platforms/xhs/orders-page";
import { XhsAccountsPage } from "../pages/platforms/xhs/accounts-page";
import { XhsAnalyticsPage } from "../pages/platforms/xhs/analytics-page";
import { InsightsContentPage } from "../pages/platforms/xhs/insights-content-page";
import { InsightsShopPage } from "../pages/platforms/xhs/insights-shop-page";
import { XhsMonitoringPage } from "../pages/platforms/xhs/monitoring-page";
import { XhsDashboard } from "../pages/platforms/xhs/xhs-dashboard";
import { XhsDiscoveryPage } from "../pages/platforms/xhs/discovery-page";
import { XhsKeywordsPage } from "../pages/platforms/xhs/keywords-page";
import { LlmGatewayPage } from "../pages/llm-gateway/llm-gateway-page";
import { XhsLibraryPage } from "../pages/platforms/xhs/library-page";
import { XhsMyNotesPage } from "../pages/platforms/xhs/my-notes-page";
import { XhsDatacenterPage } from "../pages/platforms/xhs/datacenter-page";
import { PictureSpacePage } from "../pages/platforms/xhs/picture-space-page";
import { XhsNoteWorkshopPage } from "../pages/platforms/xhs/note-workshop-page";
import { XhsShopItemsPage } from "../pages/platforms/xhs/shop-items-page";
import { ViolationManagePage } from "../pages/platforms/xhs/violation-manage-page";
import { XhsPublishPage } from "../pages/platforms/xhs/publish-page";
import { XhsSectionPage } from "../pages/platforms/xhs/xhs-section-page";
import { SystemPage } from "../pages/system/system-page";
import { ForbiddenPage } from "../pages/system/forbidden-page";

/*
 * `/platforms/:platformId` 顶层分发（设计 62 §2.4）：
 * `xhs` → Navigate 到总览（工作区落点 = 侧栏第一项；xhs 的真实页面路由都在
 *   本布局路由内，缺这条分发会让「工作区卡片点进来」落在 ComingSoon —— 2026-10-25 实测）；
 * `system` → Navigate 到 `/system/users`（系统管理独立工作区，侧栏只剩系统管理自己的组）；
 * 其余未知平台维持 ComingSoonPage（现在渲染在壳内，侧栏由该平台的菜单切片决定 ——
 * 无授权即空侧栏，不再出现「看得见摸不着」的跨平台菜单）。
 */
function PlatformRootRoute() {
  const { platformId } = useParams();
  if (platformId === "xhs") {
    return <Navigate to="/platforms/xhs/dashboard" replace />;
  }
  if (platformId === "system") {
    return <Navigate to="/system/users" replace />;
  }
  return <ComingSoonPage />;
}

export function AppRouter() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/platform-select" replace />} />
        <Route
          path="/platform-select"
          element={
            <ProtectedRoute>
              <PlatformSelectPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/login"
          element={
            <PublicOnlyRoute>
              <LoginPage />
            </PublicOnlyRoute>
          }
        />
        {/* `/platforms/:platformId` 已移入下方 AppShell 布局路由（设计 62 §2.4
            —— system 工作区要套壳层；原先的独立 ProtectedRoute 声明删除） */}
        {/* 2026-09-29 设计 17 §7.1 v1.5：大模型网关 = 独立公共模块，有自己的工作区壳（不进 AppShell 平台工作区），
            与 /platform-select 同层的独立全屏页；旧路由（平台内 / 工作区内）重定向。
            2026-10-01 §11.3（第 3 次修订）：网关内 4 个菜单改为**独立页面**（子路由）——
            壳层仍只在 `LlmGatewayPage` 一份，页面路由从这里声明。 */}
        <Route
          path="/llm-gateway"
          element={
            <ProtectedRoute>
              <LlmGatewayPage />
            </ProtectedRoute>
          }
        >
          {/* 子路由 = 3 个独立页面（用量看板 / 服务商 / 预警规则；v1.9 令牌映射页已移除）；
              各自的 index 重定向、旧 ?tab= 兼容、未知段回落都在 LlmGatewayPage 内部。 */}
          <Route path="*" element={null} />
        </Route>
        <Route
          element={
            <ProtectedRoute>
              <AppShell />
            </ProtectedRoute>
          }
        >
          {/* 系统管理工作区（v1.1.0 / 设计 46 §4）：
              6 条 path 共用**一个组件**，Tab 由 `?tab=` 决定（页面内 `SystemPage` 负责）。
              为什么拆成 6 条 path 而不是一条 `/system?tab=`：
                · 种子里的菜单 C 行 `path` 就是这 6 个（`/system/users` 等），
                  侧栏点进来要能直接落到对应 Tab；
                · 每条 path 各自可被收藏 / 分享，刷新后停在同一栏。
              ⚠️ 权限不在这里判：能不能看见这些菜单由后端 `menus/mine` 决定，
                 每个接口再由 `require_perm` 拦 —— 前端路由守卫会多套一层错误提示。 */}
          <Route path="/system" element={<Navigate to="/system/users" replace />} />
          <Route path="/system/users" element={<SystemPage />} />
          <Route path="/system/roles" element={<SystemPage />} />
          <Route path="/system/menus" element={<SystemPage />} />
          <Route path="/system/dicts" element={<SystemPage />} />
          <Route path="/system/settings" element={<SystemPage />} />
          <Route path="/system/logs" element={<SystemPage />} />
          <Route path="/system/scheduler" element={<SystemPage />} />
          {/* 403：直敲未授权 URL 时可达（设计 49 §模块八） */}
          <Route path="/403" element={<ForbiddenPage />} />
          {/* 平台工作区顶层（设计 62 §2.4）：在 AppShell 布局**之内** ——
              `/platforms/system` 套壳后 Navigate 到 /system/users；未知平台 ComingSoon。
              ⚠️ react-router 按静态段优先排序，/platforms/xhs/* 的具体路由不受影响。 */}
          <Route path="/platforms/:platformId" element={<PlatformRootRoute />} />
          <Route path="/platforms/xhs/dashboard" element={<XhsDashboard />} />
          <Route path="/platforms/xhs/accounts" element={<XhsAccountsPage />} />
          <Route path="/platforms/xhs/analytics" element={<XhsAnalyticsPage />} />
          {/* 设计 32 §6（2026-10-11 用户拍板 D2「拆」）：原「数据洞察」一页两 Tab 拆成
              **两条业务线各自的页面** —— 内容洞察（笔记侧）/ 商品洞察（店铺侧）。
              旧路由 `/analytics`（含 `?tab=`）由 `XhsAnalyticsPage` 内部重定向，深链不死。 */}
          <Route path="/platforms/xhs/insights/content" element={<InsightsContentPage />} />
          <Route path="/platforms/xhs/insights/shop" element={<InsightsShopPage />} />
          <Route path="/platforms/xhs/discovery" element={<XhsDiscoveryPage />} />
          <Route path="/platforms/xhs/my-notes" element={<XhsMyNotesPage />} />
          <Route path="/platforms/xhs/datacenter" element={<XhsDatacenterPage />} />
          <Route path="/platforms/xhs/shop-items" element={<XhsShopItemsPage />} />
          {/* 店铺违规管理（设计 43，2026-10-18）：千帆 `店铺体检 → 违规管理` 一比一复刻。
              **只读、零落库**；`?tab=` 选子 Tab，`?view=checkup` 切店铺体检。 */}
          <Route path="/platforms/xhs/violation-manage" element={<ViolationManagePage />} />
          {/* 店铺体检（设计 43 §3.1 的第二个路由）：同一个页面组件，`?view=checkup` 是内部分支。
              ⚠️ 必须注册在 `/violation-manage` **之后**、且用独立 path（不是嵌套 Route）——
              页面组件自带布局与抽屉，嵌套会把整页塞进 Outlet 里渲染两遍。 */}
          <Route
            path="/platforms/xhs/violation-manage/checkup"
            element={<ViolationManagePage />}
          />
          {/* 商品草稿箱 = 商品页内的弹窗（设计 24 §9.4，2026-10-08 用户口径
              「草稿箱弹窗的形式在商品页内展示，账号使用商品页选定的账号」）。
              旧独立路由重定向到商品列表，深链不死。 */}
          <Route
            path="/platforms/xhs/product-drafts"
            element={<Navigate to="/platforms/xhs/shop-items" replace />}
          />
          {/* 图片空间（设计 22 §1）：千帆云图片资产**只读**浏览，本地零落库。 */}
          <Route path="/platforms/xhs/picture-space" element={<PictureSpacePage />} />
          {/* 数据抓取已并入「笔记发现」的「批量抓取」页签（设计 21 切片 B）：旧路由重定向，深链不死。 */}
          <Route path="/platforms/xhs/crawler" element={<Navigate to="/platforms/xhs/discovery?tab=crawl" replace />} />
          <Route path="/platforms/xhs/keywords" element={<XhsKeywordsPage />} />
          <Route path="/platforms/xhs/library" element={<XhsLibraryPage />} />
          {/* 2026-09-25 §36：图片 / 视频 / 草稿三坊合并为「笔记工坊」一页（Tabs 隔离）。
              旧路由重定向到对应 tab —— 深链、书签、外部链接都不死。 */}
          <Route path="/platforms/xhs/note-workshop" element={<XhsNoteWorkshopPage />} />
          <Route path="/platforms/xhs/image-studio" element={<Navigate to="/platforms/xhs/note-workshop?tab=images" replace />} />
          <Route path="/platforms/xhs/video-studio" element={<Navigate to="/platforms/xhs/note-workshop?tab=videos" replace />} />
          <Route path="/platforms/xhs/drafts" element={<Navigate to="/platforms/xhs/note-workshop?tab=drafts" replace />} />
          {/* 竞品监控已并入「监控中心」（设计 21 切片 A）：旧路由重定向，深链不死。
              monitoring-page 是 benchmarks-page 的超集（4 种目标类型 vs 仅 note_url）。 */}
          <Route path="/platforms/xhs/benchmarks" element={<Navigate to="/platforms/xhs/monitoring" replace />} />
          <Route path="/platforms/xhs/monitoring" element={<XhsMonitoringPage />} />
          <Route path="/platforms/xhs/publish" element={<Navigate to="/platforms/xhs/note-workshop?tab=publish" replace />} />
          <Route path="/platforms/xhs/auto-ops" element={<AutoOpsPage />} />
          {/* 自动化工作流（设计 19 / M5）：编排 选题→生成→审核→发布→监控。
              人工审核节点默认不自动放行（§3.3 硬约束）。
              2026-10-06 设计 22 §3：并入「自动运营」宿主页作 tab ⇒ 旧路由重定向，深链不死。 */}
          <Route path="/platforms/xhs/orders" element={<OrdersPage />} />
          {/* 预检表格 —— 独立新页签展示 + 编辑（用户口径 2026-10-09）。
              数据走 sessionStorage 传递（新页签没有父窗口状态）。 */}
          <Route path="/platforms/xhs/:section" element={<XhsSectionPage />} />
          {/* 用户侧任务中心已下线（设计 69：任务收为公共，暂不允许用户创建任务）。
              旧深链重定向到系统任务中心 —— 该页仅 admin 可见，普通用户会看到
              权限说明，而不是 404（「页面壳可见、数据按权限」的既有模式）。 */}
          <Route path="/tasks" element={<Navigate to="/system/scheduler" replace />} />
          {/* 2026-09-25 §35.9 ⑥：任务记录（执行记录，复用 /tasks 端点）——
              与「任务中心」（定时任务 scheduled_jobs）不是一回事：那一页管「什么时候跑」，
              这一页管「跑完了什么」。 */}
          <Route path="/task-records" element={<TaskRecordsPage />} />
          {/* 工作区级任务（设计 69：两级各有独立入口，只读）。 */}
          <Route path="/platforms/xhs/tasks" element={<WorkspaceTasksPage />} />
          <Route path="/models" element={<ModelConfigPage />} />
          {/* 大模型网关已上移为独立公共模块（v1.5，见上方独立路由）；旧路由重定向保深链不死 */}
          <Route path="/platforms/xhs/llm-gateway" element={<Navigate to="/llm-gateway" replace />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
