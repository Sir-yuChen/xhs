import {
  AimOutlined,
  BarChartOutlined,
  CloudDownloadOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  KeyOutlined,
  ProfileOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  ScheduleOutlined,
  SearchOutlined,
  SendOutlined,
  SettingOutlined,
  ShoppingOutlined,
  PartitionOutlined,
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
  { key: "/platforms/xhs/crawler", icon: <CloudDownloadOutlined />, label: "数据抓取" },
  { key: "/platforms/xhs/keywords", icon: <KeyOutlined />, label: "关键词组" },
  { key: "/platforms/xhs/analytics", icon: <BarChartOutlined />, label: "数据洞察" },
  { key: "/platforms/xhs/benchmarks", icon: <AimOutlined />, label: "竞品监控" },
  { key: "/platforms/xhs/library", icon: <DatabaseOutlined />, label: "内容库" },
  // 2026-09-25 §36：图片 / 视频 / 草稿三坊合并为「笔记工坊」一项（页内 Tabs 隔离）。
  // 旧菜单项 image-studio / video-studio / drafts 整项移除（含图标 import）。
  { key: "/platforms/xhs/note-workshop", icon: <ToolOutlined />, label: "笔记工坊" },
  { key: "/platforms/xhs/publish", icon: <SendOutlined />, label: "发布中心" },
  { key: "/platforms/xhs/auto-ops", icon: <ThunderboltOutlined />, label: "自动运营" },
  // 设计 19 / M5：编排式工作流（与「自动运营」不同 —— 那个是单点动作，这个是多节点链路）。
  { key: "/platforms/xhs/workflows", icon: <PartitionOutlined />, label: "自动化工作流" },
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
      <KeepAliveRouteOutlet include={[/\/platforms\/xhs\/discovery/, /\/platforms\/xhs\/crawler/]} />
    </WorkspaceShell>
  );
}

export function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: ReactNode; action?: ReactNode }) {
  return (
    <Row justify="space-between" align="top" style={{ marginBottom: 24 }}>
      <Col>
        <Text type="secondary" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>{eyebrow}</Text>
        <Title level={3} style={{ margin: "4px 0 4px" }}>{title}</Title>
        <Text type="secondary">{description}</Text>
      </Col>
      {action && <Col>{action}</Col>}
    </Row>
  );
}
