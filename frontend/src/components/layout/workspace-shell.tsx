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
  /** 主导航菜单（Sider 中部，可滚动）。支持 `type: "group"` 与 SubMenu（`children`）。 */
  mainItems: MenuProps["items"];
  /** 底部导航菜单（Sider 下部，固定在用户行上方） */
  footerItems?: MenuProps["items"];
  /**
   * 当前高亮菜单的 key。默认由**当前路由**派生（见 :func:`resolveActiveKeys`）——
   * 子路由页（如网关）需要更细的匹配时可自行传入。
   */
  selectedKeys?: string[];
  /** 点击菜单项：默认 `navigate(key)`；子路由页（如网关）需自定义映射时传入。 */
  onNavigate?: (key: string) => void;
  /** 页面内容（顶栏 + Content 之下） */
  children: ReactNode;
}

/** 菜单节点的最小形状（antd `ItemType` 是联合类型，这里只取我们真正用到的字段）。 */
type MenuNode = {
  key?: string;
  children?: MenuNode[];
};

/** 把菜单树摊成**叶子项**（SubMenu 的父节点不可选中，故只收叶子）。 */
function leafKeys(items: MenuProps["items"] | undefined, out: string[] = []): string[] {
  for (const raw of items ?? []) {
    const node = raw as MenuNode | null;
    if (!node || typeof node !== "object") continue;
    const children = Array.isArray(node.children) ? node.children : null;
    // ⚠️ `type: "group"` 的组节点也有 children，但它的子项本身就是叶子 ⇒ 递归即可。
    if (children && children.length > 0) leafKeys(children as MenuProps["items"], out);
    else if (typeof node.key === "string" && node.key.startsWith("/")) out.push(node.key);
  }
  return out;
}

/** 找出某个叶子 key 的**所有祖先 key**（用于自动展开父级）。 */
function ancestorsOf(items: MenuProps["items"] | undefined, target: string, trail: string[] = []): string[] | null {
  for (const raw of items ?? []) {
    const node = raw as MenuNode | null;
    if (!node || typeof node !== "object") continue;
    const key = typeof node.key === "string" ? node.key : "";
    const children = Array.isArray(node.children) ? node.children : null;
    if (children && children.length > 0) {
      const found = ancestorsOf(children as MenuProps["items"], target, key ? [...trail, key] : trail);
      if (found) return found;
    } else if (key === target) {
      return trail;
    }
  }
  return null;
}

/**
 * **高亮判据**（2026-10-11 二级菜单改造）。
 *
 * 菜单里有两种 key，必须分别比对（不能一律拿 `pathname` 去比）：
 *
 * * **普通路由键**（`/platforms/xhs/library`）⇒ 比 `pathname`；
 * * **宿主页 + 页签键**（`/platforms/xhs/note-workshop?tab=videos`）⇒ 比
 *   `pathname + search`。只比 pathname 的话，「笔记工坊」的 6 个页签会**同时**
 *   高亮（它们的 pathname 相同）—— 用户看不出自己在哪个页签。
 *
 * 返回数组（antd 接受多个），但**正常只会命中一个**：带 query 的键优先，
 * 命中后不再回退到无 query 的宿主键。
 */
export function resolveActiveKeys(
  items: MenuProps["items"] | undefined,
  pathname: string,
  search: string,
): string[] {
  const leaves = leafKeys(items);
  const withQuery: string[] = [];
  const plain: string[] = [];
  for (const key of leaves) {
    const idx = key.indexOf("?");
    if (idx === -1) plain.push(key);
    else withQuery.push(key);
  }
  // ① 带 query 的键最具体：`pathname` 与 `search` 都要对得上。
  for (const key of withQuery) {
    const idx = key.indexOf("?");
    const keyPath = key.slice(0, idx);
    const keyQuery = key.slice(idx + 1);
    if (pathname !== keyPath) continue;
    // query 逐项比对（顺序无关）；搜索串可能带额外参数，只要求菜单声明的都在。
    const current = new URLSearchParams(search);
    const wanted = new URLSearchParams(keyQuery);
    let ok = true;
    wanted.forEach((value, name) => {
      if (current.get(name) !== value) ok = false;
    });
    if (ok) return [key];
  }
  // ② 普通键：`pathname` 相等，或它是该路由的**子路径**（嵌套路由）。
  for (const key of plain) {
    if (pathname === key || pathname.startsWith(`${key}/`)) return [key];
  }
  return [];
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
  //: 高亮 + 自动展开（2026-10-11 二级菜单改造）：
  //: ① 高亮走 `resolveActiveKeys`（区分「普通路由键」与「宿主页?tab= 键」——
  //:    只比 pathname 会让同一宿主页的 6 个页签同时亮）；
  //: ② 载入时把当前项的**父级链**塞进 `openKeys`，否则从深链进来父级是收起的
  //:    （界面上看不到自己在哪）。用户手动展开/收起后以用户为准（不强制回写）。
  const activeKeys = selectedKeys ?? resolveActiveKeys(mainItems, location.pathname, location.search);
  const activePath = location.pathname.replace(/\/$/, "");
  const [openKeys, setOpenKeys] = useState<string[]>(() => {
    const key = resolveActiveKeys(mainItems, location.pathname, location.search)[0];
    return key ? (ancestorsOf(mainItems, key) ?? []) : [];
  });
  useEffect(() => {
    // 换页时若当前项不在已展开的分支里，补开它的父级链（不改用户已展开的其它分支）。
    const key = resolveActiveKeys(mainItems, location.pathname, location.search)[0];
    if (!key) return;
    const wanted = ancestorsOf(mainItems, key) ?? [];
    setOpenKeys((prev) => {
      const missing = wanted.filter((k) => !prev.includes(k));
      return missing.length === 0 ? prev : [...prev, ...missing];
    });
  }, [mainItems, activePath, location.search]);

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
            <Menu
              theme={isDark ? "dark" : "light"}
              mode="inline"
              selectedKeys={activeKeys}
              openKeys={openKeys}
              onOpenChange={(keys) => setOpenKeys(keys as string[])}
              onClick={handleMenuClick}
              items={mainItems}
              style={{ borderRight: 0 }}
            />
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
        {/*
          内容区内边距 = **15px**（2026-10-05 用户口径「内容区域与菜单边框，间距太大了，
          上和左间距统一 15px 即可」）。
          ⚠️ 统一的是**上 / 左**两处：`padding: "15px 24px 24px 15px"` ——
          左（与 Sider 相邻）与上（与 48px 顶栏相邻）都收到 15px；右 / 下保持 24px，
          否则页面底部最后一张卡会贴着窗口下沿、右侧表格列也会贴边裁掉滚动条。
        */}
        <Content style={{ padding: "15px 24px 24px 15px", minHeight: "calc(100vh - 48px)", overflow: "auto" }}>
          {children}
        </Content>
      </Layout>
    </Layout>
  );
}
