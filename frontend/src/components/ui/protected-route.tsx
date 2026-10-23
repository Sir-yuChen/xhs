import type { ReactNode } from "react";
import { WaitMask } from "./wait-mask";
import { Navigate, useLocation } from "react-router-dom";

import { useThemePalette } from "../../app/providers";
import { useAuth } from "../../hooks/use-auth";
import { ForcePasswordPage } from "../../pages/system/force-password-page";

type RouteGuardProps = {
  children: ReactNode;
};

export function ProtectedRoute({ children }: RouteGuardProps) {
  const location = useLocation();
  const auth = useAuth();
  const p = useThemePalette();

  if (auth.isChecking) {
    return (
      <div style={{ minHeight: "100vh", background: p.pageBg }}>
        {/* 统一蒙版：白卡居中（用户口径 2026-10-20「全项目蒙版样式一样」）。 */}
        <WaitMask open mask title="正在验证登录状态" />
      </div>
    );
  }

  if (!auth.isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  //: **强制改密**（v1.1.0 / 设计 55 §1.4）：出厂 admin 是 `admin/admin123`，
  //: 不改密等于公开口令。`must_change_password=true` 时只有改密页可进 ——
  //: 这里**直接渲染**改密页而不是 `Navigate` 到某个路由：
  //: 用跳转会有「先渲染目标页再跳走」的一帧（那个组件可能已经在发请求了）。
  if (auth.user?.must_change_password) {
    return <ForcePasswordPage />;
  }

  return <>{children}</>;
}

export function PublicOnlyRoute({ children }: RouteGuardProps) {
  const auth = useAuth();
  const p = useThemePalette();

  if (auth.isChecking) {
    return (
      <div style={{ minHeight: "100vh", background: p.pageBg }}>
        {/* 统一蒙版：白卡居中（用户口径 2026-10-20「全项目蒙版样式一样」）。 */}
        <WaitMask open mask title="正在验证登录状态" />
      </div>
    );
  }

  if (auth.isAuthenticated) {
    return <Navigate to="/platform-select" replace />;
  }

  return <>{children}</>;
}
