import {
  ApiOutlined,
  ClockCircleOutlined,
  FundOutlined,
  ReloadOutlined,
  RiseOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs, { type Dayjs } from "dayjs";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  fetchGatewayModelsTree,
  fetchGatewayUsageBreakdown,
  fetchGatewayUsageDaily,
  fetchGatewayUsageOverview,
} from "../../lib/api";
import type {
  LlmGatewayConfig,
  LlmGatewayModelRow,
  LlmGatewayUsageBreakdownItem,
  LlmGatewayUsageDailyItem,
  LlmGatewayUsageOverview,
} from "../../types";

import { WaitMask } from "../../components/ui/wait-mask";
const { Text } = Typography;

// 2026-10-01 设计 17 §11.3（第二次修订）：用量看板是**大模型网关页的第一个页签**。
// 定位「运营者进网关第一屏」——回答今天用了多少 / 顺不顺 / 花在哪 / 趋势如何；
// 连接配置 / 令牌映射 / 预警规则 是需要动手时才去的后续页签。
//
// ⚠️ 本组件**只渲染内容**（不含 Sider / Header / PageHeader）——
//    壳层由 `LlmGatewayPage` 统一提供，避免同一壳层两份实现。
// 本视图**纯只读**：不提供任何写操作（停用令牌 / 改阈值在后续页签）。
// 数据来自 T1b 三端点（overview / daily / breakdown），**零后端改动**。
// 图表库 = recharts（dependencies 已装 v3.6.0）。

// 拆分维度的中文名 + 调色板（同一维度内按序取色）
// ⚠️ **v1.9**：四维 → **三维** —— `token`（令牌）维度随令牌映射退场而移除
// （设计 17 §7.1a；后端 `usage/breakdown` 的 `dimension=token` 已从白名单删除 ⇒ 传它会 400）。
const DIMENSIONS = [
  { key: "platform", label: "平台" },
  { key: "function", label: "功能" },
  { key: "model", label: "模型" },
] as const;

type DimensionKey = (typeof DIMENSIONS)[number]["key"];

const PIE_COLORS = [
  "#722ed1",
  "#1677ff",
  "#13c2c2",
  "#52c41a",
  "#faad14",
  "#eb2f96",
  "#fa541c",
  "#2f54eb",
  "#a0d911",
  "#f5222d",
];

// 维度原值 → 展示名（台账里 function 存的是常量值，如 product_note_generate）
const DIMENSION_VALUE_LABELS: Record<string, string> = {
  xhs: "小红书",
  product_note_generate: "商品笔记生成",
  note_generate: "笔记生成",
  title_generate: "标题生成",
  image_describe: "图片描述",
  unknown: "未标注",
  "": "未标注",
};

function dimLabel(key: string): string {
  return DIMENSION_VALUE_LABELS[key] ?? (key || "未标注");
}

// 数字格式化：调用量 / Token 都是大数，统一千分位；Token 再压成 k/M
function fmtNum(n: number): string {
  return (n ?? 0).toLocaleString("zh-CN");
}

function fmtTokens(n: number): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
  return String(v);
}

// 空桶（无数据）——按缺值口径渲染 0，不造假（后端 calls=0 三者都回 0）
const EMPTY_BUCKET = {
  calls: 0,
  success: 0,
  fail: 0,
  fail_rate: 0,
  total_tokens: 0,
  avg_latency_ms: 0,
};

type BucketRow = {
  label: string;
  bucket: typeof EMPTY_BUCKET;
};

export function UsageDashboardPanel() {
  const { message } = App.useApp();
  const navigate = useNavigate();

  // 筛选器：默认近 30 日 + 全部网关
  const [range, setRange] = useState<[Dayjs, Dayjs]>(() => [
    dayjs().subtract(29, "day"),
    dayjs(),
  ]);
  const [gatewayId, setGatewayId] = useState<number | undefined>(undefined);
  const [modelName, setModelName] = useState<string | undefined>(undefined);
  const [gateways, setGateways] = useState<{ id: number; name: string }[]>([]);
  // 服务商 → 其下模型行（级联「模型」下拉的数据源）
  const [modelsByGateway, setModelsByGateway] = useState<Record<number, LlmGatewayModelRow[]>>({});

  // 数据
  const [overview, setOverview] = useState<LlmGatewayUsageOverview | null>(null);
  const [daily, setDaily] = useState<LlmGatewayUsageDailyItem[]>([]);
  const [dimension, setDimension] = useState<DimensionKey>("platform");
  const [breakdown, setBreakdown] = useState<LlmGatewayUsageBreakdownItem[]>([]);
  const [detailMode, setDetailMode] = useState<"day" | "model">("day");
  const [modelRows, setModelRows] = useState<LlmGatewayUsageBreakdownItem[]>([]);

  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const startDate = range?.[0]?.format("YYYY-MM-DD");
  const endDate = range?.[1]?.format("YYYY-MM-DD");

  // 网关下拉：拿一次即可（失败不阻塞看板 —— 下拉退化为「全部网关」）。
  // ⚠️ 用 `models-tree`（每项带 `models` 模型行）而非 `fetchGatewayConfigs`（`models` 恒空），
  //    以便「模型」下拉按所选服务商级联列出其下具体模型（§7.5 筛选粒度要求）。
  useEffect(() => {
    let alive = true;
    fetchGatewayModelsTree()
      .then((res) => {
        if (!alive) return;
        const items: LlmGatewayConfig[] = res.items ?? [];
        setGateways(items.map((g) => ({ id: g.id, name: g.name })));
        const byGw: Record<number, LlmGatewayModelRow[]> = {};
        for (const g of items) {
          byGw[g.id] = g.models ?? [];
        }
        setModelsByGateway(byGw);
      })
      .catch(() => {
        if (alive) {
          setGateways([]);
          setModelsByGateway({});
        }
      });
    return () => {
      alive = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      // 总览固定三段（今日/近7日/近30日），不受 RangePicker 影响（与设计 §11.3 口径一致）；
      // daily/breakdown 用筛选器区间。
      const [ov, dy, bd, md] = await Promise.all([
        fetchGatewayUsageOverview({ gateway_id: gatewayId, model_name: modelName }),
        fetchGatewayUsageDaily({
          gateway_id: gatewayId,
          model_name: modelName,
          start_date: startDate,
          end_date: endDate,
        }),
        fetchGatewayUsageBreakdown({
          dimension,
          gateway_id: gatewayId,
          model_name: modelName,
          start_date: startDate,
          end_date: endDate,
        }),
        fetchGatewayUsageBreakdown({
          dimension: "model",
          gateway_id: gatewayId,
          model_name: modelName,
          start_date: startDate,
          end_date: endDate,
        }),
      ]);
      setOverview(ov);
      setDaily(dy ?? []);
      setBreakdown(bd ?? []);
      setModelRows(md ?? []);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "加载失败";
      setLoadError(msg);
      message.error(`用量数据加载失败：${msg}`);
    } finally {
      setLoading(false);
    }
  }, [gatewayId, modelName, startDate, endDate, dimension, message]);

  useEffect(() => {
    void load();
  }, [load]);

  // 总览三段（后端已回三段；缺段兜底空桶）
  const buckets: BucketRow[] = useMemo(
    () => [
      { label: "今日", bucket: overview?.today ?? EMPTY_BUCKET },
      { label: "近 7 日", bucket: overview?.week ?? EMPTY_BUCKET },
      { label: "近 30 日", bucket: overview?.month ?? EMPTY_BUCKET },
    ],
    [overview],
  );

  const hasAnyUsage = useMemo(() => {
    if (daily.some((d) => d.calls > 0)) return true;
    return buckets.some((b) => b.bucket.calls > 0);
  }, [daily, buckets]);

  // 折线数据：X 轴只留 MM-DD，双轴（调用量 + Token）
  const trendData = useMemo(
    () =>
      daily.map((d) => ({
        date: d.date,
        dateShort: d.date.slice(5),
        calls: d.calls,
        tokens: d.total_tokens,
        fail: d.fail,
      })),
    [daily],
  );

  // 环形图数据：只保留 calls > 0 的项，避免图例被空桶刷屏
  const pieData = useMemo(
    () =>
      breakdown
        .filter((b) => b.calls > 0)
        .map((b) => ({ name: dimLabel(b.key), value: b.calls, tokens: b.total_tokens })),
    [breakdown],
  );

  const totalCallsInPie = useMemo(
    () => pieData.reduce((acc, cur) => acc + cur.value, 0),
    [pieData],
  );

  // 明细表：按天 → daily 原始行；按模型 → modelRows
  type DetailRow = {
    key: string;
    name: string;
    calls: number;
    success: number;
    fail: number;
    fail_rate: number;
    tokens: number;
    avg_latency_ms: number | null;
  };

  const detailRows: DetailRow[] = useMemo(() => {
    if (detailMode === "day") {
      return [...daily]
        .sort((a, b) => (a.date < b.date ? 1 : -1))
        .map((d) => ({
          key: d.date,
          name: d.date,
          calls: d.calls,
          success: d.success,
          fail: d.fail,
          fail_rate: d.calls ? d.fail / d.calls : 0,
          tokens: d.total_tokens,
          avg_latency_ms: d.avg_latency_ms,
        }));
    }
    return [...modelRows]
      .sort((a, b) => b.calls - a.calls)
      .map((m) => ({
        key: m.key || "__empty__",
        name: dimLabel(m.key),
        calls: m.calls,
        success: m.success,
        fail: m.fail,
        fail_rate: m.fail_rate,
        tokens: m.total_tokens,
        avg_latency_ms: null, // 模型维度后端不聚合延迟（缺值 → 渲染 "-"）
      }));
  }, [detailMode, daily, modelRows]);

  const detailColumns: ColumnsType<DetailRow> = [
    {
      title: detailMode === "day" ? "日期" : "模型",
      dataIndex: "name",
      key: "name",
      width: 200,
      render: (v: string) => <Text style={{ fontSize: 13 }}>{v}</Text>,
    },
    {
      title: "调用",
      dataIndex: "calls",
      key: "calls",
      align: "right",
      sorter: (a, b) => a.calls - b.calls,
      render: (v: number) => <Text strong>{fmtNum(v)}</Text>,
    },
    {
      title: "成功",
      dataIndex: "success",
      key: "success",
      align: "right",
      render: (v: number) => <Text style={{ color: "#52c41a" }}>{fmtNum(v)}</Text>,
    },
    {
      title: "失败",
      dataIndex: "fail",
      key: "fail",
      align: "right",
      render: (v: number) =>
        v > 0 ? <Text style={{ color: "#ff4d4f" }}>{fmtNum(v)}</Text> : <Text type="secondary">0</Text>,
    },
    {
      title: "失败率",
      dataIndex: "fail_rate",
      key: "fail_rate",
      align: "right",
      sorter: (a, b) => a.fail_rate - b.fail_rate,
      render: (v: number) =>
        v > 0 ? (
          <Tag color={v >= 0.1 ? "red" : v >= 0.03 ? "orange" : "default"} style={{ margin: 0 }}>
            {(v * 100).toFixed(1)}%
          </Tag>
        ) : (
          <Text type="secondary">0%</Text>
        ),
    },
    {
      title: "Token",
      dataIndex: "tokens",
      key: "tokens",
      align: "right",
      sorter: (a, b) => a.tokens - b.tokens,
      render: (v: number) => fmtNum(v),
    },
    {
      title: "平均延迟",
      dataIndex: "avg_latency_ms",
      key: "avg_latency_ms",
      align: "right",
      width: 110,
      render: (v: number | null) =>
        v == null ? <Text type="secondary">-</Text> : <Text>{v} ms</Text>,
    },
  ];

  return (
    <>
      {/* 顶部工具条：健康标记 + 刷新（壳层 Header 由网关页提供，这里只放本页专属动作） */}
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Space size={12}>
            <Text type="secondary" style={{ fontSize: 13 }}>
              LLM 调用用量总览 —— 数据来自本地调用台账（只读视图）
            </Text>
            {overview?.gateway_healthy === false && (
              <Tag color="red" style={{ margin: 0 }}>
                所选网关不健康
              </Tag>
            )}
          </Space>
        </Col>
        <Col>
          <Space size={8}>
            <Button size="small" icon={<ApiOutlined />} onClick={() => navigate("/llm-gateway?tab=gateways")}>
              网关配置
            </Button>
            <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
          </Space>
        </Col>
      </Row>

      {/* ⑤ 筛选器 */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space size={12} wrap>
          <Text type="secondary" style={{ fontSize: 13 }}>
            时间区间
          </Text>
          <DatePicker.RangePicker
            value={range}
            allowClear={false}
            onChange={(v) => {
              if (v && v[0] && v[1]) setRange([v[0], v[1]]);
            }}
          />
          <Text type="secondary" style={{ fontSize: 13 }}>
            网关
          </Text>
          <Select
            style={{ minWidth: 200 }}
            placeholder="全部网关"
            allowClear
            value={gatewayId}
            onChange={(v: number | undefined) => {
              setGatewayId(v);
              // 换网关 ⇒ 模型列表随之切换，已选模型失效清空
              setModelName(undefined);
            }}
            options={gateways.map((g) => ({ value: g.id, label: g.name }))}
          />
          <Text type="secondary" style={{ fontSize: 13 }}>
            模型
          </Text>
          <Select
            style={{ minWidth: 220 }}
            placeholder="全部模型"
            allowClear
            showSearch
            optionFilterProp="label"
            value={modelName}
            disabled={gatewayId == null}
            onChange={(v: string | undefined) => setModelName(v)}
            options={(modelsByGateway[gatewayId ?? -1] ?? []).map((m) => ({
              value: m.model_name,
              label: m.model_name,
            }))}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            （区间作用于趋势 / 拆分 / 明细；总览卡固定为今日 · 近 7 日 · 近 30 日）
          </Text>
        </Space>
      </Card>

      {loadError ? (
        <Card style={{ borderColor: "#ff4d4f", marginBottom: 16 }}>
          <Space align="start">
            <WarningOutlined style={{ color: "#ff4d4f", fontSize: 16, marginTop: 2 }} />
            <div>
              <Text strong style={{ color: "#ff4d4f" }}>
                用量数据加载失败
              </Text>
              <div style={{ marginTop: 4 }}>
                <Text type="secondary" style={{ fontSize: 13 }}>
                  真实原因：{loadError}
                </Text>
              </div>
              <Button size="small" style={{ marginTop: 8 }} onClick={() => void load()}>
                重试
              </Button>
            </div>
          </Space>
        </Card>
      ) : null}

      <Spin indicator={<></>}>
        {/* ① 总览卡：三段时间 × 六指标 */}
        <Row gutter={[16, 16]}>
          {buckets.map(({ label, bucket }) => (
            <Col key={label} xs={24} lg={8}>
              <Card
                title={
                  <Space>
                    <FundOutlined style={{ color: "#722ed1" }} />
                    <span>{label}</span>
                  </Space>
                }
                styles={{ body: { paddingTop: 16 } }}
              >
                <Row gutter={[12, 16]}>
                  <Col span={12}>
                    <Statistic
                      title="调用"
                      value={bucket.calls}
                      prefix={<ThunderboltOutlined />}
                      valueStyle={{ fontSize: 22 }}
                    />
                  </Col>
                  <Col span={12}>
                    <Statistic
                      title="总 Token"
                      value={fmtTokens(bucket.total_tokens)}
                      valueStyle={{ fontSize: 22, color: "#1677ff" }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="成功"
                      value={bucket.success}
                      valueStyle={{ fontSize: 18, color: "#52c41a" }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="失败"
                      value={bucket.fail}
                      valueStyle={{
                        fontSize: 18,
                        color: bucket.fail > 0 ? "#ff4d4f" : undefined,
                      }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="失败率"
                      value={bucket.calls ? (bucket.fail_rate * 100).toFixed(1) : "0.0"}
                      suffix="%"
                      valueStyle={{
                        fontSize: 18,
                        color: bucket.fail_rate >= 0.1 ? "#ff4d4f" : undefined,
                      }}
                    />
                  </Col>
                  <Col span={24}>
                    <Statistic
                      title="平均延迟"
                      value={bucket.avg_latency_ms}
                      suffix="ms"
                      prefix={<ClockCircleOutlined />}
                      valueStyle={{ fontSize: 16 }}
                    />
                  </Col>
                </Row>
              </Card>
            </Col>
          ))}
        </Row>

        {/* ② 趋势折线（双轴）+ ③ 维度拆分环形 */}
        <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
          <Col xs={24} xl={15}>
            <Card
              title={
                <Space>
                  <RiseOutlined style={{ color: "#722ed1" }} />
                  <span>调用趋势（调用量 / Token）</span>
                </Space>
              }
              styles={{ body: { paddingTop: 8 } }}
            >
              {trendData.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={<Text type="secondary">所选区间内没有调用记录</Text>}
                  style={{ padding: "48px 0" }}
                />
              ) : (
                <ResponsiveContainer width="100%" height={300}>
                  <AreaChart data={trendData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                    <defs>
                      <linearGradient id="callsFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#722ed1" stopOpacity={0.35} />
                        <stop offset="95%" stopColor="#722ed1" stopOpacity={0.02} />
                      </linearGradient>
                      <linearGradient id="tokensFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#1677ff" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#1677ff" stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                    <XAxis dataKey="dateShort" tick={{ fontSize: 12 }} minTickGap={24} />
                    <YAxis yAxisId="left" tick={{ fontSize: 12 }} allowDecimals={false} />
                    <YAxis
                      yAxisId="right"
                      orientation="right"
                      tick={{ fontSize: 12 }}
                      tickFormatter={(v: number) => fmtTokens(v)}
                    />
                    <RTooltip
                      formatter={(value) => [fmtNum(Number(value)), ""]}
                      labelFormatter={(label, payload) => {
                        const row = payload?.[0]?.payload as { date?: string } | undefined;
                        return row?.date ?? String(label ?? "");
                      }}
                    />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    <Area
                      yAxisId="left"
                      type="monotone"
                      dataKey="calls"
                      name="调用量"
                      stroke="#722ed1"
                      strokeWidth={2}
                      fill="url(#callsFill)"
                    />
                    <Line
                      yAxisId="right"
                      type="monotone"
                      dataKey="tokens"
                      name="Token"
                      stroke="#1677ff"
                      strokeWidth={2}
                      dot={false}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </Card>
          </Col>

          <Col xs={24} xl={9}>
            <Card
              title={<span>维度拆分</span>}
              extra={
                <Segmented
                  size="small"
                  value={dimension}
                  onChange={(v) => setDimension(v as DimensionKey)}
                  options={DIMENSIONS.map((d) => ({ label: d.label, value: d.key }))}
                />
              }
              styles={{ body: { paddingTop: 8 } }}
            >
              {pieData.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={<Text type="secondary">所选区间内没有调用记录</Text>}
                  style={{ padding: "48px 0" }}
                />
              ) : (
                <ResponsiveContainer width="100%" height={300}>
                  <PieChart>
                    <Pie
                      data={pieData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="45%"
                      innerRadius={52}
                      outerRadius={88}
                      paddingAngle={2}
                    >
                      {pieData.map((entry, idx) => (
                        <Cell key={entry.name} fill={PIE_COLORS[idx % PIE_COLORS.length]} />
                      ))}
                    </Pie>
                    <RTooltip
                      formatter={(value, name) => {
                        const n = Number(value ?? 0);
                        const pct = totalCallsInPie ? ((n / totalCallsInPie) * 100).toFixed(1) : "0.0";
                        return [`${fmtNum(n)} 次（${pct}%）`, String(name ?? "")];
                      }}
                    />
                    <Legend
                      wrapperStyle={{ fontSize: 12 }}
                      formatter={(value: ReactNode, entry) => {
                        const v = (entry as { payload?: { value?: number } })?.payload?.value ?? 0;
                        return `${value}：${fmtNum(v)}`;
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
              )}
            </Card>
          </Col>
        </Row>

        {/* ④ 明细表 */}
        <Card
          style={{ marginTop: 16 }}
          title={<span>用量明细</span>}
          extra={
            <Segmented
              size="small"
              value={detailMode}
              onChange={(v) => setDetailMode(v as "day" | "model")}
              options={[
                { label: "按天", value: "day" },
                { label: "按模型", value: "model" },
              ]}
            />
          }
        >
          <Table<DetailRow>
            rowKey="key"
            size="small"
            columns={detailColumns}
            dataSource={detailRows}
            pagination={
              detailRows.length > 20
                ? { pageSize: 20, size: "small", showSizeChanger: false }
                : false
            }
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={<Text type="secondary">所选区间内没有调用记录</Text>}
                />
              ),
            }}
          />
        </Card>

        {!hasAnyUsage && !loading && !loadError ? (
          <div style={{ marginTop: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              提示：还没有任何调用记录。在创作链路（提示词模板 / 商品笔记生成等）跑一次调用后，这里就会出现数据。
            </Text>
          </div>
        ) : null}
      </Spin>
<WaitMask open={loading} title="正在加载用量数据" />
    </>
  );
}
