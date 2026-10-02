import {
  BellOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MoonOutlined,
  SunOutlined,
  UserOutlined,
} from "@ant-design/icons";
import {
  Avatar,
  Badge,
  Button,
  Dropdown,
  Layout,
  List,
  Menu,
  Space,
  Typography,
} from "antd";
import type { MenuProps } from "antd";
import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../../hooks/use-auth";
import { useThemeMode } from "../../app/providers";
import { fetchNotifications, markAllNotificationsRead, markNotificationRead } from "../../lib/api";
import type { AppNotification } from "../../types";

const { Sider, Header, Content } = Layout;
const { Text } = Typography;

// ─────────────────────────────────────────────────────────────
// 通用「工作区壳」：Sider + 顶栏（主题切换 / 消息提醒 / 头像）+ Content。
//
// 背景（2026-10-02）：大模型网关最初自己重写了一份壳（Sider / Header / Footer），
// 与小红书工作区（AppShell）视觉一致但**顶栏是空的** —— 丢了「主题切换 + 消息提醒」。
// 抽成公共组件后，任何工作区只负责声明「菜单项 + 页面内容」，壳层只此一份：
// 顶栏右上角（主题 / 通知 / 头像）、Sider 折叠、底部用户行、主题色板随主题派生，
// 全部复用，不再各写一遍。
// ─────────────────────────────────────────────────────────────

export interface WorkspaceShellProps {
  /** 侧边栏 Logo 旁的标题文案（折叠时隐藏） */
  brand: string;
  /** 主导航菜单（Sider 中部，可滚动） */
  mainItems: MenuProps["items"];
  /** 底部导航菜单（Sider 下部，固定在用户行上方） */
  footerItems?: MenuProps["items"];
  /**
   * 当前高亮菜单的 key。默认取 `location.pathname`（子路由页需要更细的匹配时可自行传入）。
   * 传给 antd Menu 的 `selectedKeys`。
   */
  selectedKeys?: string[];
  /** 点击菜单项：默认 `navigate(key)`；子路由页（如网关）需自定义映射时传入。 */
  onNavigate?: (key: string) => void;
  /** 页面内容（顶栏 + Content 之下） */
  children: ReactNode;
}

/**
 * 主题感知的壳层色板（2026-10-02 从 AppShell 内联色板抽出，供两个工作区共用）。
 * 浅色模式下 Sider 走白底、深色下走黑底，边框 / 文字对比度随主题派生，
 * 避免「切主题后侧边栏黑底、内容区白底」的割裂。
 */
export function useShellPalette() {
  const { mode } = useThemeMode();
  const isDark = mode === "dark";
  return {
    isDark,
    border: isDark ? "#303030" : "#f0f0f0",
    borderSoft: isDark ? "#262626" : "#e5e5e5",
    sideText: isDark ? "rgba(255,255,255,.85)" : "rgba(0,0,0,.85)",
    sideTextMuted: isDark ? "rgba(255,255,255,.35)" : "rgba(0,0,0,.35)",
    dropBg: isDark ? "#1f1f1f" : "#ffffff",
    dropBorder: isDark ? "#303030" : "#e8e8e8",
    dropEmptyText: isDark ? "rgba(255,255,255,0.35)" : "rgba(0,0,0,0.45)",
    dropUnreadBg: isDark ? "rgba(22,104,220,0.06)" : "rgba(22,104,220,0.04)",
  };
}

function levelColor(level: string): string {
  if (level === "error") return "#ef4444";
  if (level === "warning") return "#eab308";
  return "#666";
}

export function WorkspaceShell({
  brand,
  mainItems,
  footerItems,
  selectedKeys,
  onNavigate,
  children,
}: WorkspaceShellProps) {
  const auth = useAuth();
  const { mode: themeMode, toggle: toggleTheme } = useThemeMode();
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const [notifications, setNotifications] = useState<AppNotification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);

  const c = useShellPalette();
  const isDark = c.isDark;

  const loadNotifications = useCallback(async () => {
    try {
      const res = await fetchNotifications({ page_size: 20 });
      setNotifications(res.items);
      setUnreadCount(res.items.filter((n) => !n.read).length);
    } catch { /* silent */ }
  }, []);

  useEffect(() => {
    void loadNotifications();
    const timer = setInterval(() => void loadNotifications(), 30_000);
    return () => clearInterval(timer);
  }, [loadNotifications]);

  const handleMarkRead = async (id: number) => { await markNotificationRead(id); void loadNotifications(); };
  const handleMarkAllRead = async () => { await markAllNotificationsRead(); void loadNotifications(); };
  const handleMenuClick: MenuProps["onClick"] = ({ key }) => {
    if (onNavigate) onNavigate(key);
    else navigate(key);
  };
  const activeKeys = selectedKeys ?? [location.pathname];

  const notificationDropdownContent = (
    <div style={{ width: 360, background: c.dropBg, borderRadius: 8, border: `1px solid ${c.dropBorder}`, overflow: "hidden" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 16px", borderBottom: `1px solid ${c.dropBorder}` }}>
        <Text strong style={{ fontSize: 14 }}>通知</Text>
        {unreadCount > 0 && <Button type="link" size="small" onClick={() => void handleMarkAllRead()}>全部已读</Button>}
      </div>
      <div style={{ maxHeight: 400, overflowY: "auto" }}>
        {notifications.length === 0 ? (
          <div style={{ padding: "32px 16px", textAlign: "center", color: c.dropEmptyText }}>暂无通知</div>
        ) : (
          <List
            dataSource={notifications}
            renderItem={(n) => (
              <List.Item key={n.id} style={{ padding: "10px 16px", cursor: n.read ? "default" : "pointer", background: n.read ? "transparent" : c.dropUnreadBg, borderBottom: `1px solid ${c.borderSoft}` }} onClick={() => !n.read && void handleMarkRead(n.id)}>
                <List.Item.Meta
                  avatar={<span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: levelColor(n.level), marginTop: 6 }} />}
                  title={<Text style={{ fontSize: 13 }}>{n.title}</Text>}
                  description={<div>{n.body && <Text type="secondary" style={{ fontSize: 12, display: "block" }}>{n.body}</Text>}<Text type="secondary" style={{ fontSize: 11 }}>{new Date(n.created_at).toLocaleString("zh-CN")}</Text></div>}
                />
              </List.Item>
            )}
          />
        )}
      </div>
    </div>
  );

  const siderWidth = collapsed ? 64 : 220;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider
        collapsed={collapsed}
        width={220}
        collapsedWidth={64}
        theme={isDark ? "dark" : "light"}
        trigger={null}
        style={{
          height: "100vh",
          position: "fixed",
          left: 0,
          top: 0,
          bottom: 0,
          borderRight: `1px solid ${c.border}`,
          overflow: "hidden",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
          {/* Logo —— 点击回平台选择 */}
          <div
            style={{ padding: collapsed ? "14px 0" : "14px 16px", display: "flex", alignItems: "center", justifyContent: collapsed ? "center" : "space-between", borderBottom: `1px solid ${c.border}`, flexShrink: 0, cursor: "pointer" }}
            onClick={() => navigate("/platform-select")}
          >
            <Space align="center" size={collapsed ? 0 : 8}>
              <img src="/logo.jpg" alt="Logo" style={{ width: 28, height: 28, borderRadius: 6, objectFit: "cover", flexShrink: 0 }} />
              {!collapsed && <span style={{ fontWeight: 600, fontSize: 14, color: c.sideText }}>{brand}</span>}
            </Space>
            {!collapsed && <Button type="text" size="small" icon={<MenuFoldOutlined />} onClick={(e) => { e.stopPropagation(); setCollapsed(true); }} style={{ color: c.sideTextMuted }} />}
          </div>
          {collapsed && (
            <div style={{ textAlign: "center", padding: "6px 0", borderBottom: `1px solid ${c.borderSoft}`, flexShrink: 0 }}>
              <Button type="text" size="small" icon={<MenuUnfoldOutlined />} onClick={() => setCollapsed(false)} style={{ color: c.sideTextMuted }} />
            </div>
          )}

          {/* Main nav — scrollable */}
          <div style={{ flex: 1, overflowY: "auto", overflowX: "hidden" }}>
            <Menu theme={isDark ? "dark" : "light"} mode="inline" selectedKeys={activeKeys} onClick={handleMenuClick} items={mainItems} style={{ borderRight: 0 }} />
          </div>

          {/* Footer — pinned to bottom */}
          <div style={{ flexShrink: 0, borderTop: `1px solid ${c.border}` }}>
            {footerItems && footerItems.length > 0 && (
              <Menu theme={isDark ? "dark" : "light"} mode="inline" selectedKeys={activeKeys} onClick={handleMenuClick} items={footerItems} style={{ borderRight: 0 }} />
            )}
            <div style={{ padding: collapsed ? "8px 0" : "8px 16px", borderTop: `1px solid ${c.borderSoft}`, display: "flex", alignItems: "center", gap: 8, justifyContent: collapsed ? "center" : "flex-start" }}>
              <Avatar size={22} icon={<UserOutlined />} style={{ background: "#1668dc", flexShrink: 0, fontSize: 11 }}>{(auth.user?.username ?? "U")[0].toUpperCase()}</Avatar>
              {!collapsed && (
                <>
                  <Text type="secondary" ellipsis style={{ fontSize: 12, flex: 1, lineHeight: "22px" }}>{auth.user?.username ?? "用户"}</Text>
                  <Button type="text" icon={<LogoutOutlined />} onClick={() => void auth.logout()} size="small" style={{ color: c.sideTextMuted, flexShrink: 0 }} />
                </>
              )}
            </div>
          </div>
        </div>
      </Sider>

      <Layout style={{ marginLeft: siderWidth, transition: "margin-left 0.2s" }}>
        <Header style={{ padding: "0 24px", display: "flex", alignItems: "center", justifyContent: "flex-end", borderBottom: `1px solid ${c.border}`, height: 48, lineHeight: "48px" }}>
          <Space size={12} align="center">
            <Button
              type="text"
              icon={themeMode === "dark" ? <SunOutlined style={{ fontSize: 16 }} /> : <MoonOutlined style={{ fontSize: 16 }} />}
              onClick={toggleTheme}
              title={themeMode === "dark" ? "切换为浅色模式" : "切换为暗色模式"}
              style={{ display: "flex", alignItems: "center", justifyContent: "center" }}
            />
            <Dropdown dropdownRender={() => notificationDropdownContent} trigger={["click"]} placement="bottomRight">
              <Badge count={unreadCount} size="small" offset={[-2, 2]}>
                <Button type="text" icon={<BellOutlined style={{ fontSize: 16 }} />} style={{ display: "flex", alignItems: "center", justifyContent: "center" }} />
              </Badge>
            </Dropdown>
            <Avatar size={28} style={{ background: "#1668dc", fontSize: 12, cursor: "default" }}>{(auth.user?.username ?? "U")[0].toUpperCase()}</Avatar>
          </Space>
        </Header>
        <Content style={{ padding: 24, minHeight: "calc(100vh - 48px)", overflow: "auto" }}>
          {children}
        </Content>
      </Layout>
    </Layout>
  );
}
