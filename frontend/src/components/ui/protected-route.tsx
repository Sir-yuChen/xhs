import type { ReactNode } from "react";
import { WaitMask } from "./wait-mask";
import { Navigate, useLocation } from "react-router-dom";

import { useThemePalette } from "../../app/providers";
import { useAuth } from "../../hooks/use-auth";

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
        <WaitMask open title="正在验证登录状态" />
      </div>
    );
  }

  if (!auth.isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
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
        <WaitMask open title="正在验证登录状态" />
      </div>
    );
  }

  if (auth.isAuthenticated) {
    return <Navigate to="/platform-select" replace />;
  }

  return <>{children}</>;
}
