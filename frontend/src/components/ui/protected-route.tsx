import { Spin } from "antd";
import type { ReactNode } from "react";
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
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "100vh",
          background: p.pageBg,
        }}
      >
        <Spin size="large" tip="正在验证登录状态..." />
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
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "100vh",
          background: p.pageBg,
        }}
      >
        <Spin size="large" tip="正在验证登录状态..." />
      </div>
    );
  }

  if (auth.isAuthenticated) {
    return <Navigate to="/platform-select" replace />;
  }

  return <>{children}</>;
}
