import {
  AimOutlined,
  BarChartOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  HighlightOutlined,
  HistoryOutlined,
  KeyOutlined,
  LineChartOutlined,
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
  ThunderboltOutlined,
  ToolOutlined,
  VideoCameraOutlined,
} from "@ant-design/icons";
import type { MenuProps } from "antd";
import KeepAliveRouteOutlet from "keepalive-for-react-router";

import { BusinessLineBadge } from "./business-line-badge";
import { WorkspaceShell } from "./workspace-shell";

/*
 * ─────────────────────────────────────────────────────────────
 * 侧栏信息架构（设计 32 §5；2026-10-11 两轮用户口径）
 *
 * 第一轮：17 项平铺 → 按业务线分 4 组（`type: "group"`）。
 * **第二轮（本条，用户口径）**：
 *   > 「运营总览中只有一个菜单不需要做出两级，你参考 antd 中的菜单组件，
 *   >  将当前的菜单调整为合理的二级菜单结构」
 *
 * ⇒ 业务线由**组标题**改为**真正的二级菜单**（antd `SubMenu`，可折叠）；
 *   而「总览」只有一个菜单项，按同一口径**不设层级**，留在一级。
 *
 * ## 为什么这样才是「合理」的（antd 菜单的核心约定）
 *
 * antd `Menu` 里 **父级（SubMenu）不可选中，只有叶子能选中** ——
 * `selectedKeys` 匹配的是叶子的 `key`。而本项目的工作区本来就是三级：
 *
 * ```
 * 侧栏（菜单）  →  页面内 Tabs  →  弹窗 / 抽屉
 * ```
 *
 * 「笔记工坊」正是「一个宿主页 + 6 个页签」的形态。把它做成 SubMenu 的
 * **子节点**，侧栏层级就与页面内页签**一一对应**（页签不再只活在页内、
 * 从侧栏直接可达）；反之若把宿主与页签拉平，就会出现「两个入口指向同一个
 * pathname、只有 query 不同」——那正是 antd 菜单表达不了的形状。
 *
 * ## ⚠️ 路由一条未改，深链全不死
 *
 * `router.tsx` 零改动。SubMenu 子项的 `key` 里，带 `?tab=` 的那 6 项直接就是
 * **宿主页 + 页签**；`workspace-shell` 的高亮判据已同步扩展为
 * 「普通键比对 pathname、带 query 的键比对 pathname+search」，
 * 并在载入时**自动展开**含当前项的父级（见该文件的 `activeKeys` / `openKeys`）。
 *
 * ⚠️ **菜单项只增不减**：「数据洞察」的旧路由 `/platforms/xhs/analytics` 仍在
 * （`analytics-page.tsx` 是重定向页），但它**不是**菜单项 —— 菜单只列该点的入口。
 */

/** 一级：总览 —— **只有一个菜单项，按用户口径不做层级**。 */
const overviewNavItem: NonNullable<MenuProps["items"]>[number] = {
  key: "/platforms/xhs/dashboard",
  icon: <DashboardOutlined />,
  label: "总览",
};

/** 内容线：用**创作者令牌**的页面（笔记的生产与浏览）。 */
const contentLineChildren: MenuProps["items"] = [
  { key: "/platforms/xhs/discovery", icon: <SearchOutlined />, label: "笔记发现" },
  { key: "/platforms/xhs/my-notes", icon: <ProfileOutlined />, label: "我的笔记" },
  { key: "/platforms/xhs/library", icon: <DatabaseOutlined />, label: "内容库" },
  // 2026-09-25 §36：图片 / 视频 / 草稿三坊合并为「笔记工坊」一页（页内 Tabs 隔离）；
  // 2026-10-06 设计 22 §2：「发布中心」并入本页作 tab。
  // ⚠️ 这里把 6 个页签**显式列进菜单**（`?tab=` 深链）—— 侧栏层级 = 页签层级。
  {
    key: "/platforms/xhs/note-workshop",
    icon: <ToolOutlined />,
    label: "笔记工坊",
    children: [
      { key: "/platforms/xhs/note-workshop?tab=images", icon: <PictureOutlined />, label: "图片工坊" },
      { key: "/platforms/xhs/note-workshop?tab=videos", icon: <VideoCameraOutlined />, label: "视频工坊" },
      { key: "/platforms/xhs/note-workshop?tab=drafts", icon: <FileTextOutlined />, label: "草稿工坊" },
      { key: "/platforms/xhs/note-workshop?tab=publish", icon: <SendOutlined />, label: "发布中心" },
      { key: "/platforms/xhs/note-workshop?tab=materials", icon: <FolderOpenOutlined />, label: "素材库" },
      { key: "/platforms/xhs/note-workshop?tab=prompts", icon: <HighlightOutlined />, label: "提示词模板" },
    ],
  },
  { key: "/platforms/xhs/keywords", icon: <KeyOutlined />, label: "关键词组" },
  // 设计 32 §6：内容洞察 = 原「数据洞察」的笔记侧（拆分后归内容线）。
  { key: "/platforms/xhs/insights/content", icon: <BarChartOutlined />, label: "内容洞察" },
];

/** 店铺线：用**千帆（ark）令牌**的页面（商品 / 资产 / 订单）。 */
const shopLineChildren: MenuProps["items"] = [
  { key: "/platforms/xhs/shop-items", icon: <ShoppingOutlined />, label: "商品列表" },
  // 图片空间（设计 22 §1）：千帆云图片资产浏览（与「笔记工坊 → 素材库」那个
  // **本地磁盘**目录不是一回事）。紧邻商品列表 —— 与千帆后台的资产类相邻。
  { key: "/platforms/xhs/picture-space", icon: <PictureOutlined />, label: "图片空间" },
  { key: "/platforms/xhs/orders", icon: <ShoppingCartOutlined />, label: "订单管理" },
  // 设计 32 §6：商品洞察 = 原「数据洞察」的商品侧（拆分后归店铺线）。
  { key: "/platforms/xhs/insights/shop", icon: <LineChartOutlined />, label: "商品洞察" },
  // 设计 35：数据中心 = 千帆数据中心的商业/普通笔记离线统计（从「我的笔记」拆出，独立成页）。
  { key: "/platforms/xhs/datacenter", icon: <BarChartOutlined />, label: "数据中心" },
];

/** 平台级：**不绑定单一业务线**的页面（用户拍板组名 = 「平台」）。 */
const platformChildren: MenuProps["items"] = [
  { key: "/platforms/xhs/accounts", icon: <SafetyCertificateOutlined />, label: "账号矩阵" },
  { key: "/platforms/xhs/monitoring", icon: <AimOutlined />, label: "监控中心" },
  // 自动运营（设计 22 §3）：原「自动化工作流」独立菜单项已并入本页（页内 tab）。
  { key: "/platforms/xhs/auto-ops", icon: <ThunderboltOutlined />, label: "自动运营" },
];

/**
 * 主导航 = **1 个一级项 + 3 个二级菜单**。
 *
 * ⚠️ SubMenu 的 `key`（`content-line` / `shop-line` / `platform`）是**父节点标识**，
 * **不是路由** —— 点父级只展开/收起，不跳转（antd 的既定行为）。
 * 叶子项的 `key` 才是路由（或 `宿主页?tab=`）。
 */
const mainNavItems: MenuProps["items"] = [
  overviewNavItem,
  // ⚠️ 顺序是**用户口径**（2026-10-11）：「菜单把平台放第二位」——
  //    即 总览 → 平台 → 内容线 → 店铺线。守卫按数组顺序逐项断言，别照直觉改回去。
  { key: "platform", icon: <SafetyCertificateOutlined />, label: "平台", children: platformChildren },
  { key: "content-line", icon: <TagsOutlined />, label: "内容线", children: contentLineChildren },
  { key: "shop-line", icon: <ShopOutlined />, label: "店铺线", children: shopLineChildren },
];

const footerNavItems: MenuProps["items"] = [
  { key: "/tasks", icon: <ScheduleOutlined />, label: "任务中心" },
  // 2026-09-25 §35.11 队列记录（执行记录）。与上面的「任务中心」是两件事 ——
  // 任务中心管「什么时候跑」（scheduled_jobs），这里管「跑完了什么」（tasks 表）。
  { key: "/task-records", icon: <HistoryOutlined />, label: "队列记录" },
  { key: "/models", icon: <RobotOutlined />, label: "模型配置" },
  { key: "/settings", icon: <SettingOutlined />, label: "设置" },
];

export function AppShell() {
  return (
    <WorkspaceShell brand="Spider XHS" mainItems={mainNavItems} footerItems={footerNavItems}>
      {/* 业务线标识条（设计 32 §5.2）：**单一挂载点** —— 按当前路由派生，
          业务线页面自动标出「本页属于哪条线、要哪条令牌」。
          ⚠️ 挂在这里而不是逐页各挂一次：多份拷贝必然漂移，且漏挂一处
          在界面上看不出来（那条页面只是「少了个标签」，不报错）。
          路由 → 业务线的映射在 `lib/business-lines.ts`（守卫与侧栏结构双向比对）。 */}
      <BusinessLineBadge />
      {/* 路由缓存：笔记发现（含「搜索发现 / 批量抓取」两 Tab）—— tab 是 query，
          pathname 恒为 /discovery/，故一条正则即覆盖两 Tab（设计 21 切片 B）。 */}
      <KeepAliveRouteOutlet include={[/\/platforms\/xhs\/discovery/]} />
    </WorkspaceShell>
  );
}

/*
 * `PageHeader`（eyebrow + 标题 + 描述 + 右侧操作区）**已整块移除**（2026-10-05 用户口径
 * 「移除所有页面页面头」）—— 全站最后一个引用它的页面（任务中心）在 2026-09-27 就已删掉
 * 页头，`.hermes/skill` 与 README/Docs 里的入口无需再指到这里。
 *
 * ⚠️ 这里**不能留一个空壳导出**：`verify_doc_counts.py` 的死代码判据是「不被 frontend/src
 * 任何 import 引用」，留空壳会让「有没有人用」永远为真，页头就会以「组件还在」为理由
 * 悄悄回来。删干净比留壳更能锁住结果。
 */
