/*
 * 菜单图标渲染的**唯一实现**（2026-10-25，用户口径「图标可以是 antd 图标名，
 * 也可以是图」）：侧栏（use-dynamic-nav）、菜单管理、编辑表单预览三处共用，
 * 不许各写一份（DEF-044）。
 *
 * ## `menus.icon` 的三种合法取值
 *
 * | 取值 | 例 | 渲染 |
 * |---|---|---|
 * | kebab 短名 | `safety-certificate` | antd 图标（短名 → 全名翻译后查白名单） |
 * | antd 全名 | `UserOutlined` | antd 图标（直接查白名单） |
 * | 图片地址 | `https://…` / `/icon.png` / `data:` | `<img>`（16×18 圆角） |
 *
 * 平台品牌名（xiaohongshu / douyin …）antd 没有对应图标 ⇒ 色块首字兜底
 * （与工作区卡片同款视觉）。
 *
 * ⚠️ 白名单 = **静态 import 的图标集**（打包器可静态分析），不能用动态
 * import / eval —— 后端下发的字符串直接当代码执行是注入面。
 */

import {
  AlertOutlined,
  AimOutlined,
  ApiOutlined,
  AppstoreOutlined,
  BarChartOutlined,
  BellOutlined,
  BookOutlined,
  CloudServerOutlined,
  ClusterOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  FundOutlined,
  GlobalOutlined,
  HighlightOutlined,
  HistoryOutlined,
  KeyOutlined,
  LineChartOutlined,
  MenuOutlined,
  MessageOutlined,
  PictureOutlined,
  ProfileOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  ScheduleOutlined,
  SearchOutlined,
  SendOutlined,
  SettingOutlined,
  ShopOutlined,
  ShoppingCartOutlined,
  ShoppingOutlined,
  TagsOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  UserOutlined,
  VideoCameraOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import type { ReactNode } from "react";

/** antd 图标白名单（键 = 组件名；全部为静态 import，见文件头注释）。 */
const ICONS: Record<string, ReactNode> = {
  AimOutlined: <AimOutlined />,
  AlertOutlined: <AlertOutlined />,
  ApiOutlined: <ApiOutlined />,
  AppstoreOutlined: <AppstoreOutlined />,
  BarChartOutlined: <BarChartOutlined />,
  BellOutlined: <BellOutlined />,
  BookOutlined: <BookOutlined />,
  CloudServerOutlined: <CloudServerOutlined />,
  ClusterOutlined: <ClusterOutlined />,
  DashboardOutlined: <DashboardOutlined />,
  DatabaseOutlined: <DatabaseOutlined />,
  FileTextOutlined: <FileTextOutlined />,
  FolderOpenOutlined: <FolderOpenOutlined />,
  FundOutlined: <FundOutlined />,
  GlobalOutlined: <GlobalOutlined />,
  HighlightOutlined: <HighlightOutlined />,
  HistoryOutlined: <HistoryOutlined />,
  KeyOutlined: <KeyOutlined />,
  LineChartOutlined: <LineChartOutlined />,
  MenuOutlined: <MenuOutlined />,
  MessageOutlined: <MessageOutlined />,
  PictureOutlined: <PictureOutlined />,
  ProfileOutlined: <ProfileOutlined />,
  RobotOutlined: <RobotOutlined />,
  SafetyCertificateOutlined: <SafetyCertificateOutlined />,
  ScheduleOutlined: <ScheduleOutlined />,
  SearchOutlined: <SearchOutlined />,
  SendOutlined: <SendOutlined />,
  SettingOutlined: <SettingOutlined />,
  ShopOutlined: <ShopOutlined />,
  ShoppingCartOutlined: <ShoppingCartOutlined />,
  ShoppingOutlined: <ShoppingOutlined />,
  TagsOutlined: <TagsOutlined />,
  TeamOutlined: <TeamOutlined />,
  ThunderboltOutlined: <ThunderboltOutlined />,
  ToolOutlined: <ToolOutlined />,
  UserOutlined: <UserOutlined />,
  VideoCameraOutlined: <VideoCameraOutlined />,
  WarningOutlined: <WarningOutlined />,
};

/** 平台品牌图标（antd 没有平台 logo）：色块 + 首字，与工作区卡片同款视觉。 */
const BRAND_ICONS: Record<string, { color: string; label: string }> = {
  xiaohongshu: { color: "#ff2442", label: "小" },
  douyin: { color: "#111111", label: "抖" },
  kuaishou: { color: "#ff7900", label: "快" },
  weibo: { color: "#e6162d", label: "微" },
  xianyu: { color: "#ffe600", label: "闲" },
  taobao: { color: "#ff5200", label: "淘" },
};

/** 种子图标名是 kebab 短名（`safety-certificate`）→ antd 组件名（`SafetyCertificateOutlined`）。 */
/** antd 图标白名单的名称清单（菜单编辑的下拉选项；全部静态 import 可安全渲染）。 */
export function menuIconNames(): string[] {
  return Object.keys(ICONS);
}

export function pascalOutlined(kebab: string): string {
  return (
    kebab
      .split("-")
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join("") + "Outlined"
  );
}

/** 图片地址判定：绝对地址 / 站内绝对路径 / data URI。 */
export function isImageIcon(name: string): boolean {
  return /^(https?:)?\/\//.test(name) || name.startsWith("/") || name.startsWith("data:");
}

/** 图标尺寸（侧栏 / 表格 / 预览通用）。 */
export const MENU_ICON_SIZE = 18;

export function menuIcon(name: string | null | undefined): ReactNode {
  if (!name) return null;
  // ① 图片：直接 <img>（椭圆不裁切，圆角与 antd 图标视觉对齐）
  if (isImageIcon(name)) {
    return (
      <img
        src={name}
        alt=""
        style={{
          width: MENU_ICON_SIZE,
          height: MENU_ICON_SIZE,
          borderRadius: 4,
          objectFit: "cover",
          flexShrink: 0,
        }}
      />
    );
  }
  // ② 平台品牌名：色块 + 首字（不抛错）
  const brand = BRAND_ICONS[name];
  if (brand) {
    return (
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: MENU_ICON_SIZE,
          height: MENU_ICON_SIZE,
          borderRadius: 4,
          background: brand.color,
          color: "#fff",
          fontSize: 11,
          lineHeight: 1,
          flexShrink: 0,
        }}
      >
        {brand.label}
      </span>
    );
  }
  // ③ antd 图标：kebab 短名翻译后查白名单；全名直接查。两级都未命中 ⇒ null
  //    （不渲染图标，不抛错 —— 缺图标只是少装饰，不该让侧栏挂掉）。
  return ICONS[name] ?? ICONS[pascalOutlined(name)] ?? null;
}
