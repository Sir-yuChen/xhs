/**
 * 内容洞察（设计 32 §6 / A6，2026-10-11 用户拍板 D2「拆」）。
 *
 * ## 为什么从「数据洞察」拆出来
 *
 * 原来的 `/platforms/xhs/analytics` 把**两条业务线**的指标塞在一个页面的两个 Tab 里：
 * 「内容洞察」（笔记侧）与「指标监控」（笔记 + 商品混在一起）。
 * 用户口径（市场共识）：**两条线的成绩先分开看，组合视图只做汇总，
 * 不可把不可比的成绩揉成一个数**。
 *
 * 拆分后：
 *  * **本页 = 内容线**：内容库笔记指标 + 热点话题 + 评论洞察；
 *  * **商品洞察**（`insights-shop-page.tsx`）= 店铺线：商品指标趋势 + 商品监控。
 *
 * ⚠️ 旧路由 `/platforms/xhs/analytics`（含 `?tab=`）**重定向保深链不死**
 * （沿用设计 21 / 22 已验证范式，见 `router.tsx`）。
 */
import {
  BarChartOutlined,
  CommentOutlined,
  FileTextOutlined,
  FireOutlined,
  TagsOutlined,
  UserOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Avatar,
  Card,
  Col,
  Divider,
  Empty,
  List,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useThemePalette } from "../../../app/providers";
import {
  fetchXhsCommentInsights,
  fetchXhsHotTopics,
  fetchXhsOverview,
  fetchXhsTopContent,
} from "../../../lib/api";
import type {
  AnalyticsCommentInsight,
  AnalyticsHotTopic,
  AnalyticsTopContent,
  DashboardOverview,
} from "../../../types";
// 「指标监控」面板（设计 17 §13.10）：本页只要**笔记侧**（`scope="note"`）。
// ⚠️ 拆页**不是**复制面板 —— 面板收 `scope` 参数、两侧共用一份图表实现
// （两份拷贝必然漂移，且漂移后「同一指标两个数」这种最难查）。
import { MetricsDashboardPanel } from "./metrics-dashboard-panel";

const { Text } = Typography;

const fallbackOverview: DashboardOverview = {
  platform: "xhs",
  today_crawls: 0,
  saved_notes: 0,
  pending_publishes: 0,
  healthy_accounts: 0,
  at_risk_accounts: 0,
  comment_count: 0,
  total_engagement: 0,
  hot_topics: [],
  recent_activity: [],
};

const fallbackComments: AnalyticsCommentInsight = {
  total_comments: 0,
  question_count: 0,
  top_terms: [],
  top_comments: [],
};

function formatNumber(value = 0): string {
  return value.toLocaleString();
}

const topContentColumns: ColumnsType<AnalyticsTopContent> = [
  {
    title: "标题",
    dataIndex: "title",
    key: "title",
    ellipsis: true,
    render: (text: string, record: AnalyticsTopContent) => text || record.note_id,
  },
  {
    title: "作者",
    dataIndex: "author_name",
    key: "author_name",
    width: 120,
    render: (text: string) => text || "-",
  },
  {
    title: "赞藏评转",
    key: "stats",
    width: 200,
    render: (_: unknown, record: AnalyticsTopContent) =>
      `${formatNumber(record.likes)} / ${formatNumber(record.collects)} / ${formatNumber(record.comments)} / ${formatNumber(record.shares)}`,
  },
  {
    title: "互动",
    dataIndex: "engagement",
    key: "engagement",
    width: 100,
    render: (value: number) => formatNumber(value),
    sorter: (a: AnalyticsTopContent, b: AnalyticsTopContent) => a.engagement - b.engagement,
  },
];

const metricIconColors = ["#1668dc", "#52c41a", "#faad14", "#eb2f96"];

export function InsightsContentPage() {
  const p = useThemePalette();
  const [overview, setOverview] = useState<DashboardOverview>(fallbackOverview);
  const [topContent, setTopContent] = useState<AnalyticsTopContent[]>([]);
  const [hotTopics, setHotTopics] = useState<AnalyticsHotTopic[]>([]);
  const [commentInsights, setCommentInsights] =
    useState<AnalyticsCommentInsight>(fallbackComments);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function loadAnalytics() {
    setIsLoading(true);
    setError(null);
    try {
      const [overviewResult, topResult, topicsResult, commentsResult] =
        await Promise.all([
          fetchXhsOverview(),
          fetchXhsTopContent(),
          fetchXhsHotTopics(),
          fetchXhsCommentInsights(),
        ]);
      setOverview(overviewResult);
      setTopContent(topResult.items);
      setHotTopics(topicsResult.items);
      setCommentInsights(commentsResult);
    } catch {
      setError("内容洞察加载失败。");
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    void loadAnalytics();
  }, []);

  const metrics = [
    { label: "内容库笔记", value: overview.saved_notes, icon: <FileTextOutlined /> },
    { label: "总互动", value: overview.total_engagement ?? 0, icon: <BarChartOutlined /> },
    { label: "已存评论", value: overview.comment_count ?? 0, icon: <CommentOutlined /> },
    { label: "话题数", value: hotTopics.length, icon: <TagsOutlined /> },
  ];

  const maxTopicEngagement = hotTopics.length > 0
    ? Math.max(...hotTopics.map((t) => t.engagement))
    : 1;

  const termSizes = [18, 16, 15, 14, 13, 12];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {/* 2026-10-05 用户口径「移除所有页面页面头」：原「XHS Analytics / 数据洞察 /
          一句话说明」三行整块移除。2026-10-06 设计 22 §4：移除「刷新」「生成报告」
          两个按钮（对基础版内容洞察无增量价值）。⚠️ `loadAnalytics` 仍在首查跑。 */}

      {/* ---- 顶部指标卡 ---- */}
      <Row gutter={[16, 16]}>
        {metrics.map((metric, idx) => (
          <Col xs={12} sm={12} md={6} key={metric.label}>
            <Card
              size="small"
              style={{
                background: p.cardBg,
                borderColor: p.border,
                borderTop: `2px solid ${metricIconColors[idx]}`,
              }}
            >
              <Statistic
                title={
                  <span style={{ color: p.textTertiary, fontSize: 13 }}>{metric.label}</span>
                }
                value={metric.value}
                prefix={
                  <span style={{ color: metricIconColors[idx], marginRight: 4 }}>
                    {metric.icon}
                  </span>
                }
                valueStyle={{ fontSize: 28, fontWeight: 600, color: p.text }}
              />
            </Card>
          </Col>
        ))}
      </Row>

      {error && (
        <Alert type="error" message={error} showIcon closable />
      )}

      {isLoading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin tip="正在加载内容洞察..." />
        </div>
      ) : (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={24} lg={16}>
              <Card
                title={
                  <Space>
                    <FireOutlined style={{ color: "#f5222d" }} />
                    <span>高潜内容</span>
                  </Space>
                }
                extra={<Link to="/platforms/xhs/library">进入内容库</Link>}
                style={{ background: p.cardBg, borderColor: p.border, height: "100%" }}
                styles={{ body: { padding: "12px 16px" } }}
              >
                <Table<AnalyticsTopContent>
                  columns={topContentColumns}
                  dataSource={topContent}
                  rowKey="id"
                  size="small"
                  pagination={{ pageSize: 10, size: "small" }}
                  locale={{ emptyText: "暂无保存笔记。" }}
                />
              </Card>
            </Col>

            <Col xs={24} lg={8}>
              <div style={{ display: "flex", flexDirection: "column", gap: 16, height: "100%" }}>
                <Card
                  title={
                    <Space>
                      <TagsOutlined style={{ color: "#1668dc" }} />
                      <span>热点话题</span>
                    </Space>
                  }
                  extra={<Tag color="blue">{hotTopics.length} 个</Tag>}
                  style={{ background: p.cardBg, borderColor: p.border, flex: 1 }}
                  styles={{ body: { padding: "8px 16px", maxHeight: 320, overflowY: "auto" } }}
                >
                  {hotTopics.length === 0 ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无标签或话题数据。" />
                  ) : (
                    <List
                      dataSource={hotTopics}
                      split={false}
                      renderItem={(topic) => {
                        const pct = Math.round((topic.engagement / maxTopicEngagement) * 100);
                        return (
                          <List.Item style={{ padding: "8px 0", border: "none" }}>
                            <div style={{ width: "100%" }}>
                              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                                <Text style={{ color: p.text, fontSize: 13 }}>#{topic.keyword}</Text>
                                <Text type="secondary" style={{ fontSize: 12 }}>
                                  {topic.notes} 篇 / {formatNumber(topic.engagement)} 互动
                                </Text>
                              </div>
                              <Progress
                                percent={pct}
                                showInfo={false}
                                strokeColor="#1668dc"
                                trailColor={p.borderStrong}
                                size="small"
                              />
                            </div>
                          </List.Item>
                        );
                      }}
                    />
                  )}
                </Card>

                <Card
                  title={
                    <Space>
                      <CommentOutlined style={{ color: "#faad14" }} />
                      <span>评论关键词</span>
                    </Space>
                  }
                  extra={
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {commentInsights.question_count} 个提问
                    </Text>
                  }
                  style={{ background: p.cardBg, borderColor: p.border }}
                  styles={{ body: { padding: "12px 16px" } }}
                >
                  {commentInsights.top_terms.length === 0 ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无评论数据。" />
                  ) : (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                      {commentInsights.top_terms.map((term, idx) => {
                        const fontSize = termSizes[Math.min(idx, termSizes.length - 1)];
                        const colors = ["#1668dc", "#13c2c2", "#52c41a", "#faad14", "#eb2f96", "#722ed1"];
                        const color = colors[idx % colors.length];
                        return (
                          <Tag
                            key={term.term}
                            color={color}
                            style={{ fontSize, padding: "4px 10px", lineHeight: 1.4, border: "none" }}
                          >
                            {term.term} x{term.count}
                          </Tag>
                        );
                      })}
                    </div>
                  )}
                </Card>
              </div>
            </Col>
          </Row>

          <Divider style={{ borderColor: p.border }} />

          <Card
            title={
              <Space>
                <CommentOutlined style={{ color: "#52c41a" }} />
                <span>高赞评论</span>
              </Space>
            }
            extra={<Tag>{commentInsights.top_comments.length} 条</Tag>}
            style={{ background: p.cardBg, borderColor: p.border }}
            styles={{ body: { padding: "8px 16px" } }}
          >
            <List
              dataSource={commentInsights.top_comments}
              locale={{ emptyText: "暂无已保存评论。" }}
              split={false}
              renderItem={(comment) => (
                <List.Item style={{ padding: "10px 0", borderBottom: `1px solid ${p.borderSoft}` }}>
                  <List.Item.Meta
                    avatar={
                      <Avatar size={36} icon={<UserOutlined />} style={{ backgroundColor: p.border }} />
                    }
                    title={
                      <Text style={{ color: p.textSecondary, fontSize: 13 }}>{comment.content}</Text>
                    }
                    description={
                      <Space size={16} style={{ marginTop: 2 }}>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {comment.user_name || "未知用户"}
                        </Text>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {comment.like_count} likes
                        </Text>
                      </Space>
                    }
                  />
                </List.Item>
              )}
            />
          </Card>
        </>
      )}

      <Divider style={{ borderColor: p.border }} />

      {/* ---- 笔记指标趋势（面板的笔记侧） ---- */}
      <MetricsDashboardPanel scope="note" />
    </Space>
  );
}
