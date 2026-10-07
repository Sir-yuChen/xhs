import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "../components/layout/app-shell";
import { ComingSoonPage } from "../components/platforms/coming-soon";
import { ProtectedRoute, PublicOnlyRoute } from "../components/ui/protected-route";
import { LoginPage } from "../pages/login/login-page";
import { ModelConfigPage } from "../pages/models/model-config-page";
import { PlatformSelectPage } from "../pages/platform-select/platform-select-page";
import { SettingsPage } from "../pages/settings/settings-page";
import { TaskCenterPage } from "../pages/tasks/task-center-page";
import { TaskRecordsPage } from "../pages/tasks/task-records-page";
import { AutoOpsPage } from "../pages/platforms/xhs/auto-ops-page";
import { OrdersPage } from "../pages/platforms/xhs/orders-page";
import { XhsAccountsPage } from "../pages/platforms/xhs/accounts-page";
import { XhsAnalyticsPage } from "../pages/platforms/xhs/analytics-page";
import { XhsMonitoringPage } from "../pages/platforms/xhs/monitoring-page";
import { XhsDashboard } from "../pages/platforms/xhs/xhs-dashboard";
import { XhsDiscoveryPage } from "../pages/platforms/xhs/discovery-page";
import { XhsKeywordsPage } from "../pages/platforms/xhs/keywords-page";
import { LlmGatewayPage } from "../pages/llm-gateway/llm-gateway-page";
import { XhsLibraryPage } from "../pages/platforms/xhs/library-page";
import { XhsMyNotesPage } from "../pages/platforms/xhs/my-notes-page";
import { PictureSpacePage } from "../pages/platforms/xhs/picture-space-page";
import { XhsNoteWorkshopPage } from "../pages/platforms/xhs/note-workshop-page";
import { XhsShopItemsPage } from "../pages/platforms/xhs/shop-items-page";
import { XhsPublishPage } from "../pages/platforms/xhs/publish-page";
import { XhsSectionPage } from "../pages/platforms/xhs/xhs-section-page";

export function AppRouter() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/platform-select" replace />} />
        <Route
          path="/login"
          element={
            <PublicOnlyRoute>
              <LoginPage />
            </PublicOnlyRoute>
          }
        />
        <Route
          path="/platform-select"
          element={
            <ProtectedRoute>
              <PlatformSelectPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/platforms/:platformId"
          element={
            <ProtectedRoute>
              <ComingSoonPage />
            </ProtectedRoute>
          }
        />
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
          <Route path="/platforms/xhs/dashboard" element={<XhsDashboard />} />
          <Route path="/platforms/xhs/accounts" element={<XhsAccountsPage />} />
          <Route path="/platforms/xhs/analytics" element={<XhsAnalyticsPage />} />
          <Route path="/platforms/xhs/discovery" element={<XhsDiscoveryPage />} />
          <Route path="/platforms/xhs/my-notes" element={<XhsMyNotesPage />} />
          <Route path="/platforms/xhs/shop-items" element={<XhsShopItemsPage />} />
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
          <Route path="/platforms/xhs/workflows" element={<Navigate to="/platforms/xhs/auto-ops?tab=workflows" replace />} />
          <Route path="/platforms/xhs/orders" element={<OrdersPage />} />
          <Route path="/platforms/xhs/:section" element={<XhsSectionPage />} />
          <Route path="/tasks" element={<TaskCenterPage />} />
          {/* 2026-09-25 §35.9 ⑥：任务记录（执行记录，复用 /tasks 端点）——
              与「任务中心」（定时任务 scheduled_jobs）不是一回事：那一页管「什么时候跑」，
              这一页管「跑完了什么」。 */}
          <Route path="/task-records" element={<TaskRecordsPage />} />
          <Route path="/models" element={<ModelConfigPage />} />
          {/* 大模型网关已上移为独立公共模块（v1.5，见上方独立路由）；旧路由重定向保深链不死 */}
          <Route path="/platforms/xhs/llm-gateway" element={<Navigate to="/llm-gateway" replace />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
