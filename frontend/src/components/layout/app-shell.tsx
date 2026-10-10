import {
  AimOutlined,
  BarChartOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  KeyOutlined,
  PictureOutlined,
  ProfileOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  ScheduleOutlined,
  SearchOutlined,
  SettingOutlined,
  ShoppingOutlined,
  ContainerOutlined,
  ShoppingCartOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import {
  Col,
  Row,
  Typography,
} from "antd";
import type { MenuProps } from "antd";
import type { ReactNode } from "react";
import KeepAliveRouteOutlet from "keepalive-for-react-router";

import { WorkspaceShell } from "./workspace-shell";

const { Text, Title } = Typography;

const mainNavItems: MenuProps["items"] = [
  { key: "/platforms/xhs/dashboard", icon: <DashboardOutlined />, label: "总览" },
  { key: "/platforms/xhs/accounts", icon: <SafetyCertificateOutlined />, label: "账号矩阵" },
  { key: "/platforms/xhs/discovery", icon: <SearchOutlined />, label: "笔记发现" },
  { key: "/platforms/xhs/my-notes", icon: <ProfileOutlined />, label: "我的笔记" },
  { key: "/platforms/xhs/shop-items", icon: <ShoppingOutlined />, label: "商品列表" },
  // 图片空间（设计 22 §1）：千帆云图片资产**只读**浏览（与「笔记工坊 → 素材库」
  // 那个**本地磁盘**目录不是一回事）。放在商品列表之后 —— 与千帆后台的资产类相邻。
  { key: "/platforms/xhs/picture-space", icon: <PictureOutlined />, label: "图片空间" },
  { key: "/platforms/xhs/keywords", icon: <KeyOutlined />, label: "关键词组" },
  { key: "/platforms/xhs/analytics", icon: <BarChartOutlined />, label: "数据洞察" },
  { key: "/platforms/xhs/monitoring", icon: <AimOutlined />, label: "监控中心" },
  { key: "/platforms/xhs/library", icon: <DatabaseOutlined />, label: "内容库" },
  // 2026-09-25 §36：图片 / 视频 / 草稿三坊合并为「笔记工坊」一项（页内 Tabs 隔离）。
  // 旧菜单项 image-studio / video-studio / drafts 整项移除（含图标 import）。
  // 2026-10-06 设计 22 §2：「发布中心」并入本页作 tab ⇒ 主菜单删该项（`SendOutlined` import 一并迁走）。
  { key: "/platforms/xhs/note-workshop", icon: <ToolOutlined />, label: "笔记工坊" },
  // 自动运营 = 宿主页（页内两 tab：自动运营 / 自动化工作流）。
  // 2026-10-06 设计 22 §3：原「自动化工作流」独立菜单项并入本页 ⇒ 该项删除。
  { key: "/platforms/xhs/auto-ops", icon: <ThunderboltOutlined />, label: "自动运营" },
  { key: "/platforms/xhs/orders", icon: <ShoppingCartOutlined />, label: "订单管理" },
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
