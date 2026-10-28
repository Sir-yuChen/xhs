import {
  DatabaseOutlined,
  HeartOutlined,
  SafetyCertificateOutlined,
  ScheduleOutlined,
} from "@ant-design/icons";
import { Card, Col, Row, Statistic } from "antd";
import { useEffect, useState } from "react";

import { useThemePalette } from "../../../app/providers";
import { fetchXhsOverview } from "../../../lib/api";
import type { DashboardOverview } from "../../../types";

const fallbackOverview: DashboardOverview = {
  platform: "xhs",
  today_crawls: 0,
  saved_notes: 0,
  pending_publishes: 0,
  healthy_accounts: 0,
  at_risk_accounts: 0,
};

export function XhsDashboard() {
  const [overview, setOverview] = useState<DashboardOverview>(fallbackOverview);
  const p = useThemePalette();

  useEffect(() => {
    fetchXhsOverview()
      .then(setOverview)
      .catch(() => setOverview(fallbackOverview));
  }, []);

  const metrics = [
    { label: "今日抓取", value: overview.today_crawls, icon: <DatabaseOutlined /> },
    { label: "内容库笔记", value: overview.saved_notes, icon: <HeartOutlined /> },
    { label: "待发布", value: overview.pending_publishes, icon: <ScheduleOutlined /> },
    { label: "健康账号", value: overview.healthy_accounts, icon: <SafetyCertificateOutlined /> },
  ];

  return (
    <Row gutter={[16, 16]}>
      {metrics.map((metric) => (
        <Col xs={12} sm={12} md={6} key={metric.label}>
          <Card size="small" style={{ background: p.cardBg, borderColor: p.border }}>
            <Statistic title={metric.label} value={metric.value} prefix={metric.icon} />
          </Card>
        </Col>
      ))}
    </Row>
  );
}
