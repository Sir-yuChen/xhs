/**
 * 数据仪表盘面板（设计 17 §13.10，M2 切片 3，2026-10-04）。
 *
 * ## 为什么是独立文件
 *
 * 宿主 `analytics-page.tsx`（430 行）已承担一期四个区块（TOP 内容 / 评论洞察 / 报告导出）；
 * 就地改会同时碰「一期布局 + 新面板」两块风险面。本文件独立，宿主只加**一页签**。
 *
 * ## 三条判据（详见 `lib/metrics-format.ts` 的注释）
 *
 * 1. **不得补零**：`trend` 只返回有快照的天 ⇒ 折线照原样画；
 * 2. **空态而非 0 线**：空序列走 `Empty`，**不喂给 recharts**；
 * 3. **价格是分 ⇒ 换元展示且保留区间语义**（`priceRangeText`，**不是求和**）。
 *
 * ⚠️ 采集端点**纯本地零平台请求**（数据源是已落库的笔记/商品行），
 * 所以「立即采集」按钮可以随手点，不消耗平台额度。
 */

import {
  BarChartOutlined,
  EyeOutlined,
  InteractionOutlined,
  ReloadOutlined,
  ShoppingOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Segmented,
  Space,
  Spin,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useCallback, useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  collectNoteMetrics,
  collectShopMetrics,
  fetchNoteMetricsTrend,
  fetchShopMetricsTrend,
} from "../../../lib/api";
import {
  engagementPieData,
  isEmptySeries,
  latestNoteTotals,
  latestShopTotals,
  priceRangeText,
  snapshotDayCount,
  toNoteChartData,
  toShopChartData,
  type NoteTrendPoint,
  type ShopTrendPoint,
} from "../../../lib/metrics-format";

const { Text } = Typography;

/** 窗口选项（设计 16 §4.4 原型：近 7 / 30 天）。 */
const WINDOWS = [
  { label: "近 7 天", value: 7 },
  { label: "近 30 天", value: 30 },
] as const;

/** 互动构成环形图配色（与图例顺序一一对应）。 */
const PIE_COLORS = ["#1677ff", "#722ed1", "#13c2c2", "#fa8c16"];

function fmtNum(value: number): string {
  return value.toLocaleString();
}

export function MetricsDashboardPanel({ scope = "both" }: { scope?: "note" | "shop" | "both" }) {
  const { message } = App.useApp();
  const showNote = scope === "note" || scope === "both";
  const showShop = scope === "shop" || scope === "both";
  const [days, setDays] = useState<number>(7);
  const [noteSeries, setNoteSeries] = useState<NoteTrendPoint[]>([]);
  const [shopSeries, setShopSeries] = useState<ShopTrendPoint[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isCollecting, setIsCollecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadTrends = useCallback(
    async (windowDays: number) => {
      setIsLoading(true);
      setError(null);
      try {
        // ⚠️ 只拉本页要的那一侧（拆分后省一次请求）。`Promise.all` 两边都传空数组
        // 时仍会发两次请求 —— 所以按 `showNote` / `showShop` 分别组装。
        const [notes, shops] = await Promise.all([
          showNote ? fetchNoteMetricsTrend({ days: windowDays }) : Promise.resolve([]),
          showShop ? fetchShopMetricsTrend({ days: windowDays }) : Promise.resolve([]),
        ]);
        setNoteSeries(notes);
        setShopSeries(shops);
      } catch {
        setError("指标趋势加载失败。");
      } finally {
        setIsLoading(false);
      }
    },
    [showNote, showShop],
  );

  useEffect(() => {
    void loadTrends(days);
  }, [days, loadTrends]);

  /** 立即采集（笔记 + 商品）—— 纯本地操作，不消耗平台额度。 */
  async function handleCollect() {
    setIsCollecting(true);
    try {
      const [notes, shops] = await Promise.all([
        showNote ? collectNoteMetrics() : Promise.resolve({ collected: 0, stat_date: "-" }),
        showShop ? collectShopMetrics() : Promise.resolve({ collected: 0, stat_date: "-" }),
      ]);
      message.success(
        `采集完成：笔记 ${fmtNum(notes.collected)} 条 / 商品 ${fmtNum(shops.collected)} 条（${notes.stat_date}）`,
      );
      await loadTrends(days);
    } catch {
      message.error("采集失败，请稍后重试。");
    } finally {
      setIsCollecting(false);
    }
  }

  // 图例 / 饼图数据（纯逻辑在 lib，组件只渲染）。
  const noteChart = toNoteChartData(noteSeries);
  const shopChart = toShopChartData(shopSeries);
  const noteTotals = latestNoteTotals(noteSeries);
  const shopTotals = latestShopTotals(shopSeries);
  const pieData = engagementPieData(noteTotals);
  const noteDays = snapshotDayCount(noteSeries);
  const shopDays = snapshotDayCount(shopSeries);

  const noteEmpty = isEmptySeries(noteSeries);
  const shopEmpty = isEmptySeries(shopSeries);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {/* ---- 工具栏：窗口 + 采集（设计 16 §4.4：账号 / 时间 / 刷新）---- */}
      <Space wrap>
        <Segmented
          value={days}
          options={WINDOWS.map((item) => ({ label: item.label, value: item.value }))}
          onChange={(value) => setDays(value as number)}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void loadTrends(days)} loading={isLoading}>
          刷新
        </Button>
        <Tooltip title="采集本地已同步的笔记/商品指标快照（不请求平台，不消耗额度）">
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            onClick={() => void handleCollect()}
            loading={isCollecting}
          >
            立即采集
          </Button>
        </Tooltip>
        <Text type="secondary">
          已有快照：
          {showNote ? <>笔记 <Tag color="blue">{noteDays}</Tag> 天</> : null}
          {showNote && showShop ? " / " : null}
          {showShop ? <>商品 <Tag color="purple">{shopDays}</Tag> 天</> : null}
        </Text>
      </Space>

      {error ? <Text type="danger">{error}</Text> : null}

      <Spin spinning={isLoading}>
        {/* ---- 总览卡 ---- */}
        <Row gutter={[16, 16]}>
          {showNote ? (
            <>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic
                    title="阅读（最新快照）"
                    value={noteTotals.views}
                    prefix={<EyeOutlined />}
                  />
                </Card>
              </Col>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic
                    title="互动（赞藏评转）"
                    value={noteTotals.engagement}
                    prefix={<InteractionOutlined />}
                  />
                </Card>
              </Col>
            </>
          ) : null}
          {showShop ? (
            <>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic
                    title="商品库存"
                    value={shopTotals.stock}
                    prefix={<ShoppingOutlined />}
                  />
                </Card>
              </Col>
              <Col xs={12} md={6}>
                <Card size="small">
                  {/* ⚠️ 价格用区间文案（不是求和），且已换元 */}
                  <Statistic
                    title="商品价格区间（元）"
                    value={priceRangeText(shopTotals.price_min_fen, shopTotals.price_max_fen)}
                    prefix={<BarChartOutlined />}
                  />
                </Card>
              </Col>
            </>
          ) : null}
        </Row>

        {/* ---- 笔记监控（仅内容洞察页）---- */}
        {showNote ? (
        <Card
          size="small"
          title="笔记监控"
          extra={<Text type="secondary">只画有快照的天（缺采不补零）</Text>}
        >
          {noteEmpty ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                <Text type="secondary">
                  所选区间内没有快照数据 —— 采集任务每小时自动运行，也可点上方「立即采集」
                </Text>
              }
              style={{ padding: "32px 0" }}
            />
          ) : (
            <>
              <ResponsiveContainer width="100%" height={260}>
                <AreaChart data={noteChart} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis dataKey="stat_date" tick={{ fontSize: 12 }} minTickGap={24} />
                  <YAxis tick={{ fontSize: 12 }} allowDecimals={false} />
                  <RTooltip />
                  <Legend />
                  <Area
                    type="monotone"
                    dataKey="阅读"
                    stroke="#1677ff"
                    fill="#1677ff"
                    fillOpacity={0.18}
                  />
                  <Area
                    type="monotone"
                    dataKey="互动"
                    stroke="#722ed1"
                    fill="#722ed1"
                    fillOpacity={0.18}
                  />
                </AreaChart>
              </ResponsiveContainer>

              <Row gutter={[16, 16]} style={{ marginTop: 8 }}>
                <Col xs={24} md={12}>
                  <ResponsiveContainer width="100%" height={220}>
                    <PieChart>
                      <Pie
                        data={pieData}
                        dataKey="value"
                        nameKey="name"
                        innerRadius={48}
                        outerRadius={78}
                        paddingAngle={2}
                      >
                        {pieData.map((entry, index) => (
                          <Cell
                            key={entry.name}
                            fill={PIE_COLORS[index % PIE_COLORS.length]}
                          />
                        ))}
                      </Pie>
                      <RTooltip />
                      <Legend />
                    </PieChart>
                  </ResponsiveContainer>
                </Col>
                <Col xs={24} md={12}>
                  <ResponsiveContainer width="100%" height={220}>
                    <LineChart
                      data={noteChart}
                      margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                      <XAxis dataKey="stat_date" tick={{ fontSize: 12 }} minTickGap={24} />
                      <YAxis tick={{ fontSize: 12 }} allowDecimals={false} />
                      <RTooltip />
                      <Legend />
                      <Line type="monotone" dataKey="点赞" stroke="#1677ff" dot={false} />
                      <Line type="monotone" dataKey="收藏" stroke="#722ed1" dot={false} />
                      <Line type="monotone" dataKey="评论" stroke="#13c2c2" dot={false} />
                      <Line type="monotone" dataKey="转发" stroke="#fa8c16" dot={false} />
                    </LineChart>
                  </ResponsiveContainer>
                </Col>
              </Row>
            </>
          )}
        </Card>
        ) : null}

        {/* ---- 商品监控（仅商品洞察页）---- */}
        {showShop ? (
        <Card
          size="small"
          title="商品监控"
          extra={<Text type="secondary">价格是区间（下界/上界），不做求和</Text>}
        >
          {shopEmpty ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                <Text type="secondary">
                  所选区间内没有商品快照 —— 需先同步店铺商品，采集任务会逐日沉淀
                </Text>
              }
              style={{ padding: "32px 0" }}
            />
          ) : (
            <>
              <ResponsiveContainer width="100%" height={240}>
                <BarChart data={shopChart} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis dataKey="stat_date" tick={{ fontSize: 12 }} minTickGap={24} />
                  <YAxis tick={{ fontSize: 12 }} allowDecimals={false} />
                  <RTooltip />
                  <Legend />
                  <Bar dataKey="库存" fill="#1677ff" />
                  <Bar dataKey="销量" fill="#722ed1" />
                </BarChart>
              </ResponsiveContainer>

              <ResponsiveContainer width="100%" height={220} style={{ marginTop: 8 }}>
                <LineChart data={shopChart} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis dataKey="stat_date" tick={{ fontSize: 12 }} minTickGap={24} />
                  <YAxis
                    tick={{ fontSize: 12 }}
                    tickFormatter={(value: number) => `${value}`}
                  />
                  {/* ⚠️ 不写参数类型：recharts 的 `Formatter` 签名含 `ValueType`（含数组），
                      显式标注必然不匹配。既有范式（usage-dashboard-panel）同样是
                      `(value) => …` + 内部 `Number()` 兜底。 */}
                  <RTooltip formatter={(value) => `${Number(value).toFixed(2)} 元`} />
                  <Legend />
                  <Line type="monotone" dataKey="价格下界" stroke="#13c2c2" dot={false} />
                  <Line type="monotone" dataKey="价格上界" stroke="#fa8c16" dot={false} />
                </LineChart>
              </ResponsiveContainer>

              <Space size={16} wrap style={{ marginTop: 8 }}>
                <Text type="secondary">
                  最新快照：{shopTotals.stat_date || "-"} · 销量 {fmtNum(shopTotals.sales)} · 近 30
                  天 {fmtNum(shopTotals.sales_30d)}
                </Text>
              </Space>
            </>
          )}
        </Card>
        ) : null}
      </Spin>
    </Space>
  );
}
