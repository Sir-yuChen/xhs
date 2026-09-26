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
import { XhsAccountsPage } from "../pages/platforms/xhs/accounts-page";
import { XhsAnalyticsPage } from "../pages/platforms/xhs/analytics-page";
import { XhsBenchmarksPage } from "../pages/platforms/xhs/benchmarks-page";
import { XhsDashboard } from "../pages/platforms/xhs/xhs-dashboard";
import { XhsCrawlerPage } from "../pages/platforms/xhs/crawler-page";
import { XhsDiscoveryPage } from "../pages/platforms/xhs/discovery-page";
import { XhsKeywordsPage } from "../pages/platforms/xhs/keywords-page";
import { XhsLibraryPage } from "../pages/platforms/xhs/library-page";
import { XhsMonitoringPage } from "../pages/platforms/xhs/monitoring-page";
import { XhsMyNotesPage } from "../pages/platforms/xhs/my-notes-page";
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
          <Route path="/platforms/xhs/crawler" element={<XhsCrawlerPage />} />
          <Route path="/platforms/xhs/keywords" element={<XhsKeywordsPage />} />
          <Route path="/platforms/xhs/library" element={<XhsLibraryPage />} />
          {/* 2026-09-25 §36：图片 / 视频 / 草稿三坊合并为「笔记工坊」一页（Tabs 隔离）。
              旧路由重定向到对应 tab —— 深链、书签、外部链接都不死。 */}
          <Route path="/platforms/xhs/note-workshop" element={<XhsNoteWorkshopPage />} />
          <Route path="/platforms/xhs/image-studio" element={<Navigate to="/platforms/xhs/note-workshop?tab=images" replace />} />
          <Route path="/platforms/xhs/video-studio" element={<Navigate to="/platforms/xhs/note-workshop?tab=videos" replace />} />
          <Route path="/platforms/xhs/drafts" element={<Navigate to="/platforms/xhs/note-workshop?tab=drafts" replace />} />
          <Route path="/platforms/xhs/benchmarks" element={<XhsBenchmarksPage />} />
          <Route path="/platforms/xhs/publish" element={<XhsPublishPage />} />
          <Route path="/platforms/xhs/auto-ops" element={<AutoOpsPage />} />
          <Route path="/platforms/xhs/:section" element={<XhsSectionPage />} />
          <Route path="/tasks" element={<TaskCenterPage />} />
          {/* 2026-09-25 §35.9 ⑥：任务记录（执行记录，复用 /tasks 端点）——
              与「任务中心」（定时任务 scheduled_jobs）不是一回事：那一页管「什么时候跑」，
              这一页管「跑完了什么」。 */}
          <Route path="/task-records" element={<TaskRecordsPage />} />
          <Route path="/models" element={<ModelConfigPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
