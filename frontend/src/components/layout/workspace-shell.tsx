import {
  BellOutlined,
  CloudDownloadOutlined,
  SettingOutlined,
  ToolOutlined,
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
  Modal,
  Empty,
  Layout,
  List,
  Menu,
  Segmented,
  Skeleton,
  Space,
  Tag,
  Typography,
} from "antd";
import type { MenuProps } from "antd";
import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ProfilePage } from "../../pages/system/profile-page";
import { workspaceOf } from "../../lib/workspaces";


import { useAuth } from "../../hooks/use-auth";
import { useThemeMode } from "../../app/providers";
import {
  fetchNotificationUnreadCount,
  fetchNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from "../../lib/api";
import type { AppNotification } from "../../types";
import { notificationTargetPath } from "../../lib/notification-routes";
import { UpdateDialog, ManualUpdateDialog } from "../system/update-dialog";
import { EnvBootstrapDialog, EnvBootstrapGate } from "../system/env-bootstrap-dialog";
import { useDictVersion } from "../../lib/system-dicts";
import { usePermission } from "../../hooks/use-permission";

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
  /**
   * 当前工作区标识（`xhs` / `system` / `ai` / 未来平台）。
   *
   * 用途：**只有业务工作区**才显示「工作区配置」入口 —— 工作区私有配置
   * 必须回它自己的工作区（2026-10-25 用户口径「私有配置只在对应工作区域内展示」）。
   * `system` / `ai` 是管理/公共工作区，没有私有配置 ⇒ 不显示。
   */
  workspace?: string;
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
  /**
   * 菜单是否加载中（v1.1.0 动态菜单）。
   *
   * 为 `true` 时侧栏显示加载态而**不是空菜单** —— 空菜单会被用户理解成
   * 「我没有权限」，那是最容易导致误报「管理员把我权限删了」的形状。
   */
  menuLoading?: boolean;
  /** 菜单加载失败的原因（有值 ⇒ 显示错误提示 + 重试入口）。 */
  menuError?: string | null;
  /** 点「重试」时的回调（重新拉一次菜单）。 */
  onReloadMenu?: () => void;
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
  workspace,
  mainItems,
  footerItems,
  selectedKeys,
  onNavigate,
  menuLoading = false,
  menuError = null,
  onReloadMenu,
  children,
}: WorkspaceShellProps) {
  const auth = useAuth();
  const { mode: themeMode, toggle: toggleTheme } = useThemeMode();
  //: 工作区私有配置**写**由后端收口（admin 专属）；非管理员看得到但改不了。
  const { isAdmin } = usePermission();
  // 字典版本订阅（设计 63 §2.2）：字典管理页改了枚举文案/颜色 ⇒ 版本递增
  // ⇒ 外壳重渲染 ⇒ 全部子页面重渲染，同步读 `dictLabel` 的表格文案即时更新。
  useDictVersion();
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const [notifications, setNotifications] = useState<AppNotification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  //: 通知下拉的过滤档（R2-C 2026-10-13）：全部 / 未读。（按来源/等级的细分过滤走
  //: 后端 `source_type` / `level` 参数，下拉里给「未读」这一个最常用的粗筛。）
  const [notifyFilter, setNotifyFilter] = useState<"all" | "unread">("all");
  // 手动「检查版本更新」弹窗（2026-10-13 用户口径）：顶栏图标点开，**不强制**。
  const [manualUpdateOpen, setManualUpdateOpen] = useState(false);
  //: 个人资料弹窗（设计 62 §2.3 / 2026-10-25 用户口径：弹窗展示，不用单独页）
  const [profileOpen, setProfileOpen] = useState(false);
  // 设计 64 §1.6：三级配置的两处弹窗入口 —— 用户级挂头像下拉，工作区级挂顶栏齿轮。
  //: 依赖检查弹窗（用户口径 2026-10-23：「在页面顶部的版本更新图标旁新增一个
  //: 依赖检查，点击弹窗展示检查本地依赖是否完整，不完整则自动触发下载」）。
  const [depCheckOpen, setDepCheckOpen] = useState(false);

  const c = useShellPalette();
  const isDark = c.isDark;

  const loadNotifications = useCallback(async () => {
    try {
      // ⚠️ 列表与未读计数**分开取**（R2-C 2026-10-13）：
      //   旧实现用「当页 20 条里未读的条数」当未读数 ⇒ 未读超过 20 条时恒显示 20（少算）。
      //   未读数走后端 `GET /notifications/unread-count` 全量计数；列表只取本页。
      const [res, countRes] = await Promise.all([
        fetchNotifications(notifyFilter === "unread" ? { unread: true, page_size: 20 } : { page_size: 20 }),
        fetchNotificationUnreadCount(),
      ]);
      setNotifications(res.items);
      setUnreadCount(countRes.unread);
    } catch { /* silent */ }
  }, [notifyFilter]);

  useEffect(() => {
    void loadNotifications();
    const timer = setInterval(() => void loadNotifications(), 30_000);
    return () => clearInterval(timer);
  }, [loadNotifications]);

  const handleMarkRead = async (id: number) => { await markNotificationRead(id); void loadNotifications(); };
  const handleMarkAllRead = async () => { await markAllNotificationsRead(); void loadNotifications(); };
  //: 点击某条通知：标已读 + 跳到来源页（没落点则只标已读）。R2-C 2026-10-13。
  const handleNotificationClick = async (n: AppNotification) => {
    if (!n.read) await handleMarkRead(n.id);
    const target = notificationTargetPath(n.source_type, n.source_id);
    if (target) navigate(target);
  };
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
    <div style={{ width: 380, background: c.dropBg, borderRadius: 8, border: `1px solid ${c.dropBorder}`, overflow: "hidden" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 16px", borderBottom: `1px solid ${c.dropBorder}` }}>
        <Space size={8} align="center">
          <Text strong style={{ fontSize: 14 }}>通知</Text>
          {unreadCount > 0 && <Tag color="red" style={{ marginInlineEnd: 0 }}>{unreadCount} 未读</Tag>}
        </Space>
        <Space size={4} align="center">
          <Segmented
            size="small"
            value={notifyFilter}
            onChange={(v) => setNotifyFilter(v as "all" | "unread")}
            options={[{ label: "全部", value: "all" }, { label: "未读", value: "unread" }]}
          />
          {unreadCount > 0 && <Button type="link" size="small" onClick={() => void handleMarkAllRead()}>全部已读</Button>}
        </Space>
      </div>
      <div style={{ maxHeight: 400, overflowY: "auto" }}>
        {notifications.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={notifyFilter === "unread" ? "没有未读通知" : "暂无通知"} style={{ padding: "24px 0" }} />
        ) : (
          <List
            dataSource={notifications}
            renderItem={(n) => {
              const target = notificationTargetPath(n.source_type, n.source_id);
              return (
                <List.Item
                  key={n.id}
                  style={{ padding: "10px 16px", cursor: target || !n.read ? "pointer" : "default", background: n.read ? "transparent" : c.dropUnreadBg, borderBottom: `1px solid ${c.borderSoft}` }}
                  onClick={() => void handleNotificationClick(n)}
                >
                  <List.Item.Meta
                    avatar={<span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: levelColor(n.level), marginTop: 6 }} />}
                    title={<Text style={{ fontSize: 13 }}>{n.title}</Text>}
                    description={<div>{n.body && <Text type="secondary" style={{ fontSize: 12, display: "block" }}>{n.body}</Text>}<Space size={6}><Text type="secondary" style={{ fontSize: 11 }}>{new Date(n.created_at).toLocaleString("zh-CN")}</Text>{target && <Text type="secondary" style={{ fontSize: 11 }}>· 点击查看</Text>}</Space></div>}
                  />
                </List.Item>
              );
            }}
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
            {/* 菜单加载态（v1.1.0 动态菜单）：**不渲染空菜单** ——
                空菜单会被理解成「我没有权限」，进而误报「管理员把我权限删了」。
                加载中给骨架，失败给原因 + 重试。 */}
            {menuLoading && (
              <div style={{ padding: collapsed ? "12px 8px" : "12px 16px" }}>
                <Skeleton
                  active
                  title={false}
                  paragraph={{ rows: 6, width: "100%" }}
                />
              </div>
            )}
            {!menuLoading && menuError && (
              <div style={{ padding: "12px 16px" }}>
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      菜单加载失败：{menuError}
                    </Text>
                  }
                >
                  {onReloadMenu && (
                    <Button size="small" onClick={onReloadMenu}>
                      重试
                    </Button>
                  )}
                </Empty>
              </div>
            )}
            {!menuLoading && !menuError && (
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
            )}
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
            {/* 检查版本更新（2026-10-13 用户口径）：与主题切换并排的**手动**入口。
                ⚠️ 与启动检查的区别：本入口**不强制** —— 弹窗可关、可「暂不更新」；
                只有「每次软件打开时」的启动检查才强制（见 update-dialog.tsx）。 */}
            <Button
              type="text"
              icon={<CloudDownloadOutlined style={{ fontSize: 16 }} />}
              onClick={() => setManualUpdateOpen(true)}
              title="检查版本更新"
              style={{ display: "flex", alignItems: "center", justifyContent: "center" }}
            />
            {/* 依赖检查（用户口径 2026-10-23）：与版本更新图标并排的手动入口。
                点击 ⇒ 弹窗列出本地依赖（node / ffmpeg / OCR 模型）是否完整，
                **不完整则自动触发下载**（不是只报状态）。 */}
            <Button
              type="text"
              icon={<ToolOutlined style={{ fontSize: 16 }} />}
              onClick={() => setDepCheckOpen(true)}
              title="检查运行依赖"
              style={{ display: "flex", alignItems: "center", justifyContent: "center" }}
            />
            <Dropdown dropdownRender={() => notificationDropdownContent} trigger={["click"]} placement="bottomRight">
              <Badge count={unreadCount} size="small" offset={[-2, 2]}>
                <Button type="text" icon={<BellOutlined style={{ fontSize: 16 }} />} style={{ display: "flex", alignItems: "center", justifyContent: "center" }} />
              </Badge>
            </Dropdown>
            {/* 头像下拉（设计 62 §2.3 / 2026-10-25 用户口径「个人资料使用弹窗展示，
                不使用单独页面」）：个人资料点开 Modal（复用 ProfilePage 内容组件），
                退出登录保留在菜单里（左下角用户条的退出不受影响）。 */}
            <Dropdown
              trigger={["click"]}
              placement="bottomRight"
              menu={{
                items: [
                  { key: "profile", icon: <UserOutlined />, label: "个人资料" },
                  { key: "user-config", icon: <SettingOutlined />, label: "个人配置" },
                  { key: "logout", icon: <LogoutOutlined />, label: "退出登录" },
                ],
                onClick: ({ key }) => {
                  if (key === "profile") setProfileOpen(true);
                  //: 个人配置只有**一份**实现（`/settings` 页）；下拉只是快捷入口，
                  //: 不再弹第二个内容相同的弹窗（2026-10-25 用户截图指出两处内容一样）。
                  else if (key === "user-config") navigate("/settings");
                  else if (key === "logout") void auth.logout();
                },
              }}
            >
              <Avatar size={28} style={{ background: "#1668dc", fontSize: 12, cursor: "pointer" }}>{(auth.user?.username ?? "U")[0].toUpperCase()}</Avatar>
            </Dropdown>
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
      {/* 系统更新弹窗（设计 36，2026-10-13）：打开软件即检查；有新版本**强制**弹窗 */}
      {/* 初始化环境（设计 24 §27，2026-10-23）：缺依赖时弹窗，展示下载进度 */}
      <EnvBootstrapGate />
      {depCheckOpen ? (
        <EnvBootstrapDialog autoStart onDone={() => setDepCheckOpen(false)} />
      ) : null}
      <UpdateDialog />
      {/* 手动检查更新弹窗（2026-10-13 用户口径）：顶栏图标点开，**可关闭 / 可暂不更新** */}
      <ManualUpdateDialog open={manualUpdateOpen} onClose={() => setManualUpdateOpen(false)} />
      {/* 个人资料弹窗（设计 62 §2.3）：内容复用 ProfilePage（账号信息 + 改密码）。
          改密成功 = window.location.href="/login" 整页跳走，无需额外关闭接线。 */}
      <Modal
        open={profileOpen}
        title="个人资料"
        width={920}
        centered
        footer={null}
        onCancel={() => setProfileOpen(false)}
        destroyOnHidden
      >
        <ProfilePage />
      </Modal>


    </Layout>
  );
}
