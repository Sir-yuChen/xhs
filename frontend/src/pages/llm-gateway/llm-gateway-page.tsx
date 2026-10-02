import {
  AlertOutlined,
  ApiOutlined,
  ExportOutlined,
  FundOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  ReloadOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { App, Avatar, Button, Card, Form, Input, InputNumber, Layout, Menu, Modal, Popconfirm, Select, Space, Switch, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { PageHeader } from "../../components/layout/app-shell";
import { useAuth } from "../../hooks/use-auth";
import { UsageDashboardPanel } from "./usage-dashboard-panel";
import {
  createGatewayConfig,
  createGatewayAlert,
  deleteGatewayAlert,
  fetchGatewayAlerts,
  fetchGatewayConfigs,
  fetchGatewayPlatforms,
  fetchGatewayStats,
  previewGatewayModels,
  testGatewayConfig,
  updateGatewayConfig,
  updateGatewayAlert,
} from "../../lib/api";
import type {
  LlmGatewayAlert,
  LlmGatewayAlertPayload,
  LlmGatewayConfig,
  LlmGatewayConfigPayload,
  LlmGatewayPlatformOption,
  LlmGatewayStats,
} from "../../types";

const { Sider, Header, Content } = Layout;
const { Text } = Typography;

// 行内简短展示用
function statsLine(stats: LlmGatewayStats["today"]): string {
  if (!stats) return "-";
  return `调用 ${stats.calls} · 成功 ${stats.success_calls} · Tokens ${stats.total_tokens} · 均耗 ${stats.avg_latency_ms}ms`;
}

// 2026-09-29 设计 17 §7.1 v1.5：大模型网关 = 独立公共模块，有自己的工作区壳，
// **壳层布局与小红书工作区（AppShell）保持一致**（Sider 220 可折叠 + logo 行 / 48px 顶栏 /
// Content 24 内边距 + PageHeader / 底部用户行）。
//
// ⚠️ **v1.7（2026-09-30）重做**：取消 New API 外挂进程与 iframe 控制台（那是 8 个控制台页
// 全空白的根因）；页面回归**项目自有页签**，全部为本项目原生渲染，不再内嵌任何上游页面。
//
// ⚠️ **2026-10-01 设计 17 §11.3（三次修订，以本次为准）**：
//   ① 原第 2 个页签「用量看板」（6 个裸数字，无图表）先被删除，用量迁到独立首页 `/usage-dashboard`；
//   ② 随后**独立首页撤销** —— 用量看板回到本页，作为第一个页签；
//   ③ **再改为「菜单 = 独立页面」（用户口径：网关内的 4 个菜单改成独立页面）**：
//      取消 `Tabs`，4 项改为**子路由**（`/llm-gateway/usage` 等），侧栏 = 真正的导航菜单
//      （点击切换页面 + 高亮当前项）。**壳层只此一份**（Sider / 顶栏 / PageHeader 不重复实现），
//      旧 `?tab=` 深链**重定向**到对应子路由，链接不死。
// ⚠️ **v1.9（2026-10-01）**：用户口径「连接配置应是新增服务商」「令牌映射页面移除」
//      ⇒ 4 项变 **3 项**：`gateways` 文案改「服务商」、**`tokens` 项整条删除**。
const PAGE_ITEMS: { key: string; path: string; icon: ReactNode; label: string }[] = [
  { key: "usage", path: "usage", icon: <FundOutlined />, label: "用量看板" },
  { key: "gateways", path: "gateways", icon: <ApiOutlined />, label: "服务商" },
  { key: "alerts", path: "alerts", icon: <AlertOutlined />, label: "预警规则" },
];

// 旧 `?tab=` 值 → 新子路由路径（v1.7 的四页签深链），以及无 tab 时的默认页。
// ⚠️ 加新菜单时**这里也要加**（否则旧链接落到默认页），故与 PAGE_ITEMS 同源推导。
const TAB_TO_PATH: Record<string, string> = Object.fromEntries(
  PAGE_ITEMS.map((p) => [p.key, p.path])
);
const DEFAULT_PATH = PAGE_ITEMS[0].path;

// 挂载在 `/llm-gateway` 下的壳：一个 Sider + 一个 Header + 一个 Content，
// 4 个页面各自是这条路由的子路由 —— 「菜单 = 独立页面」，但壳层只有一份。
export function LlmGatewayPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  // 当前段：`/llm-gateway/<seg>` 的 `<seg>`；空 = 尚未选页（由内部 index 重定向兜底）
  const seg = location.pathname.replace(/^\/llm-gateway\/?/, "").split("/")[0] ?? "";
  const [collapsed, setCollapsed] = useState(false);

  const siderWidth = collapsed ? 64 : 220;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider
        collapsed={collapsed}
        width={220}
        collapsedWidth={64}
        theme="dark"
        trigger={null}
        style={{
          height: "100vh",
          position: "fixed",
          left: 0,
          top: 0,
          bottom: 0,
          borderRight: "1px solid #303030",
          overflow: "hidden",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
          {/* Logo —— 与 AppShell 一致，点击回平台选择 */}
          <div
            style={{ padding: collapsed ? "14px 0" : "14px 16px", display: "flex", alignItems: "center", justifyContent: collapsed ? "center" : "space-between", borderBottom: "1px solid #303030", flexShrink: 0, cursor: "pointer" }}
            onClick={() => navigate("/platform-select")}
          >
            <Space align="center" size={collapsed ? 0 : 8}>
              <img src="/logo.jpg" alt="Logo" style={{ width: 28, height: 28, borderRadius: 6, objectFit: "cover", flexShrink: 0 }} />
              {!collapsed && <span style={{ fontWeight: 600, fontSize: 14, color: "rgba(255,255,255,.85)" }}>大模型网关</span>}
            </Space>
            {!collapsed && <Button type="text" size="small" icon={<MenuFoldOutlined />} onClick={(e) => { e.stopPropagation(); setCollapsed(true); }} style={{ color: "rgba(255,255,255,.35)" }} />}
          </div>
          {collapsed && (
            <div style={{ textAlign: "center", padding: "6px 0", borderBottom: "1px solid #262626", flexShrink: 0 }}>
              <Button type="text" size="small" icon={<MenuUnfoldOutlined />} onClick={() => setCollapsed(false)} style={{ color: "rgba(255,255,255,.35)" }} />
            </div>
          )}

          {/* Main nav —— 3 个**独立页面**（用量看板 / 服务商 / 预警规则；v1.9 令牌映射页已移除），
              点击即切换子路由，`selectedKeys` 高亮当前页。 */}
          <div style={{ flex: 1, overflowY: "auto", overflowX: "hidden" }}>
            <Menu
              theme="dark"
              mode="inline"
              selectedKeys={[seg]}
              onClick={({ key }) => navigate(`/llm-gateway/${key}`)}
              style={{ borderRight: 0 }}
              items={PAGE_ITEMS.map((p) => ({ key: p.key, icon: p.icon, label: p.label }))}
            />
          </div>

          {/* Footer —— 与 AppShell 一致：底部菜单 + 用户行 */}
          <div style={{ flexShrink: 0, borderTop: "1px solid #303030" }}>
            <div style={{ padding: collapsed ? "8px 0" : "8px 16px", borderTop: "1px solid #262626", display: "flex", alignItems: "center", gap: 8, justifyContent: collapsed ? "center" : "flex-start" }}>
              <Avatar size={22} icon={<UserOutlined />} style={{ background: "#722ed1", flexShrink: 0, fontSize: 11 }}>{(auth.user?.username ?? "U")[0].toUpperCase()}</Avatar>
              {!collapsed && (
                <>
                  <Text type="secondary" ellipsis style={{ fontSize: 12, flex: 1, lineHeight: "22px" }}>{auth.user?.username ?? "用户"}</Text>
                  <Button type="text" icon={<LogoutOutlined />} onClick={() => void auth.logout()} size="small" style={{ color: "rgba(255,255,255,0.35)", flexShrink: 0 }} />
                </>
              )}
            </div>
          </div>
        </div>
      </Sider>

      <Layout style={{ marginLeft: siderWidth, transition: "margin-left 0.2s" }}>
        <GatewayRoutes />
      </Layout>
    </Layout>
  );
}

// ─────────────────────────────────────────────────────────────
// 子路由区：每条子路由 = 一个独立页面（顶栏 + 自己的内容）。
// 各页自己声明需要的顶栏动作，共用的只有 Layout/Header/Content 三个容器。
// ─────────────────────────────────────────────────────────────
function GatewayRoutes() {
  const navigate = useNavigate();
  const location = useLocation();
  // 旧 `?tab=xxx` 深链兼容（v1.7 的四页签形态）。带 query 时 `location.pathname` 仍不含 tab，
  // 故这里显式读一次 —— 见下方 index 分支。
  const legacyTab = new URLSearchParams(location.search).get("tab") ?? "";

  return (
    <Routes>
      {/* index：① 旧 `?tab=` 深链 → 对应子路由；② 无 tab → 默认页（用量看板） */}
      <Route
        index
        element={
          <Navigate
            to={legacyTab && TAB_TO_PATH[legacyTab] ? TAB_TO_PATH[legacyTab] : DEFAULT_PATH}
            replace
          />
        }
      />
      <Route path="usage" element={<UsagePage onGoGateways={() => navigate("/llm-gateway/gateways")} />} />
      <Route path="gateways" element={<GatewaysPage />} />
      {/* ⚠️ v1.9：`tokens` 子路由**已删除**（令牌映射整页退场，设计 17 §7.1a）。
          旧 `/llm-gateway/tokens` 深链由下方 `path="*"` 兜住 → 落到默认页（不 404）。 */}
      <Route path="alerts" element={<AlertsPage />} />
      {/* 未知子路由 → 默认页（不 404，符合项目「空态/回落而非报错」的呈现纪律） */}
      <Route path="*" element={<Navigate to={DEFAULT_PATH} replace />} />
    </Routes>
  );
}

// 顶栏 + 内容容器：三个页面共用这一份骨架（避免每页各写一遍 Header/Content）。
function GatewaySection({
  eyebrow,
  title,
  description,
  action,
  children,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <>
      <Header style={{ padding: "0 24px", display: "flex", alignItems: "center", justifyContent: "flex-end", borderBottom: "1px solid #303030", height: 48, lineHeight: "48px" }} />
      <Content style={{ padding: 24, minHeight: "calc(100vh - 48px)", overflow: "auto" }}>
        <PageHeader eyebrow={eyebrow} title={title} description={description} action={action} />
        {children}
      </Content>
    </>
  );
}

// ── ① 用量看板（独立页面，只读） ──
function UsagePage({ onGoGateways }: { onGoGateways: () => void }) {
  return (
    <GatewaySection
      eyebrow="LLM Gateway"
      title="用量看板"
      description="本地调用台账的只读视图 —— 今日 / 近 7 日 / 近 30 日调用量、成功率、Token 与延迟，按平台 / 功能 / 模型三维拆分。"
      action={
        <Button icon={<ApiOutlined />} onClick={onGoGateways}>
          服务商配置
        </Button>
      }
    >
      {/* 内容组件与壳层解耦（`UsageDashboardPanel` 只渲染内容） */}
      <UsageDashboardPanel />
    </GatewaySection>
  );
}

// ── ② 服务商（独立页面；v1.9 由「连接配置」改名） ──
function GatewaysPage() {
  const { message } = App.useApp();
  const [form] = Form.useForm<LlmGatewayConfigPayload>();

  const [gateways, setGateways] = useState<LlmGatewayConfig[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<LlmGatewayConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [togglingId, setTogglingId] = useState<number | null>(null);
  const [statsMap, setStatsMap] = useState<Record<number, LlmGatewayStats>>({});
  const [selectedId, setSelectedId] = useState<number | null>(null);
  // 「使用平台」枚举**由后端下发**（v1.9 D3）—— 前端不写死选项
  const [platformOptions, setPlatformOptions] = useState<LlmGatewayPlatformOption[]>([]);
  // v1.9：模型下拉的候选（由**服务商**决定 ⇒ 点「拉取模型」时按表单当前地址+令牌现拉）
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const data = await fetchGatewayPlatforms();
        if (alive) setPlatformOptions(data.items);
      } catch {
        // 取不到 = 下拉无选项（空态）；加载失败由全局拦截器报
        if (alive) setPlatformOptions([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchGatewayConfigs();
      setGateways(data.items);
      if (data.items.length > 0 && selectedId === null) {
        setSelectedId(data.items[0].id);
      }
    } catch {
      // 取不到由全局拦截器报；这里保证表格空态可见
    } finally {
      setLoading(false);
    }
  }, [selectedId]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadStats = useCallback(async (id: number) => {
    try {
      const body = await fetchGatewayStats(id);
      setStatsMap((prev) => ({ ...prev, [id]: body }));
    } catch {
      // 取不到 = 空态（不是错误）：统计列显示占位，不弹红
      setStatsMap((prev) => ({ ...prev, [id]: { id, available: false, today: null, week: null } }));
    }
  }, []);

  // 选中服务商时拉统计（轻型，行内展示）
  useEffect(() => {
    if (selectedId === null) return;
    void loadStats(selectedId);
  }, [selectedId, loadStats]);

  const selectedGateway = useMemo(
    () => gateways.find((g) => g.id === selectedId) ?? gateways[0] ?? null,
    [gateways, selectedId]
  );

  const openCreate = () => {
    setEditing(null);
    setModelOptions([]); // v1.9：新增态没有目录 ⇒ 清空候选（用户点「拉取模型」现拉）
    form.resetFields();
    setModalOpen(true);
  };

  const openEdit = (g: LlmGatewayConfig) => {
    setEditing(g);
    // 已有目录直接做候选（用户不一定重拉；改了地址/令牌再点「拉取模型」）
    setModelOptions(g.models ?? []);
    form.setFieldsValue({
      name: g.name,
      platform: g.platform,
      model_type: g.model_type,
      base_url: g.base_url,
      default_model: g.default_model,
      // ⚠️ 编辑态**不回填令牌**（后端语义是「留空 = 保持不变」，见 submit 的注释）
      access_token: "",
    });
    setModalOpen(true);
  };

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (editing) {
        await updateGatewayConfig(editing.id, {
          ...(values.name ? { name: values.name } : {}),
          ...(values.platform ? { platform: values.platform } : {}),
          ...(values.model_type ? { model_type: values.model_type } : {}),
          ...(values.base_url ? { base_url: values.base_url } : {}),
          ...(values.access_token ? { access_token: values.access_token } : {}),
          ...(values.default_model !== undefined ? { default_model: values.default_model } : {}),
        });
        message.success("已更新服务商");
      } else {
        await createGatewayConfig({
          name: values.name,
          platform: values.platform || "",
          model_type: values.model_type || "text",
          base_url: values.base_url || "http://127.0.0.1:3000",
          access_token: values.access_token || "",
          default_model: values.default_model || "",
        });
        message.success("已新增服务商");
      }
      setModalOpen(false);
      await load();
    } catch {
      // 失败由全局拦截器报
    } finally {
      setSaving(false);
    }
  };

  // ⚠️ v1.9：`remove()` **已删除**（用户口径「移除删除功能」）。
  // 后端 DELETE 是 410 墓碑；服务商一旦被业务引用就不能删，
  // 替代入口 = 下面的启停开关（`enabled`）。
  const toggleEnabled = async (g: LlmGatewayConfig, next: boolean) => {
    setTogglingId(g.id);
    try {
      await updateGatewayConfig(g.id, { enabled: next });
      message.success(next ? `已开启 ${g.name}` : `已停用 ${g.name}`);
      await load();
    } catch {
      // 失败由拦截器报；本地状态不乐观更新，故表格仍显示后端值
    } finally {
      setTogglingId(null);
    }
  };

  const runTest = async (g: LlmGatewayConfig) => {
    setTestingId(g.id);
    try {
      const result = await testGatewayConfig(g.id);
      message.success(`${g.name}：${result.message}`);
      await load();
      if (selectedId === g.id) await loadStats(g.id);
    } catch {
      // 失败由拦截器报
    } finally {
      setTestingId(null);
    }
  };

  // v1.9：按**表单当前值**拉服务商模型目录（下拉候选）。
  // ⚠️ 不写库 —— 预览语义；用户可反复拉（改地址/改令牌后重拉）。
  // ⚠️ 编辑态令牌留空（= 不修改）时传 `id`，让后端用库里已存的令牌兜底。
  const loadModels = async () => {
    const values = form.getFieldsValue();
    const baseUrl = (values.base_url || "").trim();
    if (!baseUrl) {
      message.warning("请先填写模型调用地址");
      return;
    }
    setLoadingModels(true);
    try {
      const result = await previewGatewayModels({
        base_url: baseUrl,
        access_token: (values.access_token || "").trim() || undefined,
        ...(editing && !(values.access_token || "").trim() ? { id: editing.id } : {}),
      });
      setModelOptions(result.models);
      message.success(result.message);
    } catch {
      // 失败由全局拦截器报（401/400 都带结构化原因）
      setModelOptions([]);
    } finally {
      setLoadingModels(false);
    }
  };

  const columns: ColumnsType<LlmGatewayConfig> = [
    {
      title: "名称",
      dataIndex: "name",
      width: 140,
      render: (name: string) => <Text strong>{name}</Text>,
    },
    {
      // ⚠️ v1.9 新增（用户口径「连接配置页面新增唯一KEY，新增后不可编辑」）：
      // 表格里是**只读展示 + 可复制**（业务代码要用它，抄错代价高）；
      // 弹窗内不提供输入框 —— 服务端也拒改（PATCH 传它 ⇒ 422）。
      title: "服务编码",
      dataIndex: "provider_key",
      width: 190,
      render: (key: string) => (
        <Typography.Text
          code
          copyable={key ? { text: key, tooltips: ["复制 KEY", "已复制"] } : false}
        >
          {key || "-"}
        </Typography.Text>
      ),
    },
    {
      // ⚠️ v1.9 新增（用户口径「新增使用平台，后端新增枚举下拉选择必选」）：
      // 显示后端枚举的**中文名**（label），值仍是枚举键。
      title: "使用平台",
      dataIndex: "platform",
      width: 110,
      render: (value: string) => {
        const label = platformOptions.find((o) => o.value === value)?.label;
        // 枚举未加载/值不在枚举内 ⇒ 原样显示枚举键（**不造名**，缺值口径）
        return <Tag>{label ?? value ?? "-"}</Tag>;
      },
    },
    {
      title: "类型",
      dataIndex: "model_type",
      width: 90,
      render: (value: string) => (
        <Tag color={value === "image" ? "purple" : "blue"}>
          {value === "image" ? "图像" : "文本"}
        </Tag>
      ),
    },
    {
      title: "地址",
      dataIndex: "base_url",
      render: (url: string) => <Text type="secondary">{url}</Text>,
    },
    {
      title: "默认模型",
      dataIndex: "default_model",
      width: 180,
      render: (m: string) => (m ? <Text code>{m}</Text> : <Text type="secondary">-</Text>),
    },
    {
      title: "调用量（今日/近7日）",
      key: "stats",
      width: 260,
      render: (_v, record) => {
        const s = statsMap[record.id];
        if (!s?.available) return <Text type="secondary">{s?.message ?? "-"}</Text>;
        return (
          <Typography.Paragraph style={{ margin: 0, fontSize: 12 }} type="secondary" ellipsis={{ rows: 2 }}>
            {statsLine(s.today)}
            <br />
            {statsLine(s.week)}
          </Typography.Paragraph>
        );
      },
    },
    {
      title: "令牌",
      key: "tokens",
      width: 90,
      render: (_v, record) =>
        record.has_access_token ? <Tag color="green">已配置</Tag> : <Text type="secondary">无</Text>,
    },
    {
      title: "健康",
      dataIndex: "healthy",
      width: 90,
      render: (healthy: boolean, record) => (
        <Tooltip title={record.last_health_at ? `最近探测 ${record.last_health_at}` : "尚未探测"}>
          <Tag color={healthy ? "green" : "orange"}>{healthy ? "正常" : "未探测"}</Tag>
        </Tooltip>
      ),
    },
    {
      // ⚠️ v1.9 新增（用户口径「新增停用开启功能」）：替代被移除的「删除」。
      // 停用语义 = **业务取不到该服务商**（后端抛 LLM_PROVIDER_DISABLED，不回落）。
      title: "启用",
      key: "enabled",
      width: 90,
      render: (_v, record) => (
        <Tooltip title={record.enabled ? "点击停用（业务将取不到该服务商）" : "点击启用"}>
          <Switch
            size="small"
            checked={record.enabled}
            loading={togglingId === record.id}
            onChange={(next) => void toggleEnabled(record, next)}
          />
        </Tooltip>
      ),
    },
    {
      title: "操作",
      key: "actions",
      width: 130,
      render: (_v, record) => (
        <Space size="small">
          <Button type="link" size="small" loading={testingId === record.id} onClick={() => void runTest(record)}>
            测试
          </Button>
          <Button type="link" size="small" onClick={() => openEdit(record)}>
            编辑
          </Button>
          {/* ⚠️ v1.9：「删除」按钮 + Popconfirm **整段移除**（用户口径「移除删除功能」）。
              替代入口 = 上面的启用开关。后端 DELETE 保留为 410 墓碑，前端不再调用。 */}
        </Space>
      ),
    },
  ];

  return (
    <GatewaySection
      eyebrow="LLM Gateway"
      title="服务商"
      description="任意 OpenAI 兼容端点（三方服务商直连，或自托管的兼容网关）。服务商在所有平台间共用；业务代码按「服务编码」引用它们。"
      action={
        <Button type="primary" onClick={openCreate}>
          新增服务商
        </Button>
      }
    >
      <Card
        extra={
          <Space size={12} align="center">
            {selectedGateway ? (
              <>
                <Text type="secondary" style={{ fontSize: 12 }}>选中：</Text>
                <Select
                  size="small"
                  style={{ minWidth: 180 }}
                  value={selectedGateway.id}
                  onChange={(id: number) => setSelectedId(id)}
                  options={gateways.map((g) => ({ value: g.id, label: g.name }))}
                />
                <Button
                  size="small"
                  icon={<ExportOutlined />}
                  onClick={() => window.open(selectedGateway.base_url, "_blank", "noopener")}
                >
                  新窗口打开
                </Button>
              </>
            ) : null}
            <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
          </Space>
        }
      >
        <Table<LlmGatewayConfig>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={gateways}
          pagination={false}
          onRow={(record) => ({ onClick: () => setSelectedId(record.id), style: { cursor: "pointer" } })}
          rowClassName={(record) => (record.id === selectedId ? "gw-row-selected" : "")}
          locale={{ emptyText: "还没有服务商 —— 点右上「新增服务商」添加第一个" }}
        />
      </Card>

      <Modal
        open={modalOpen}
        title={editing ? "编辑服务商" : "新增服务商"}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onOk={() => void submit()}
        onCancel={() => setModalOpen(false)}
        destroyOnClose
      >
        <Form form={form} layout="vertical" autoComplete="off">
          {/* ⚠️ v1.9：服务编码**不在弹窗内出现**（新增/编辑都不显示）。
              它由系统生成、创建后不可修改，且**列表页已有可复制列** ——
              弹窗里再放一份只读文本，既占位又无用（用户不会在填表时抄编码）。
              ⚠️ 服务端仍拒改（PATCH 带 `provider_key` ⇒ 422），前端不显示 ≠ 无约束。 */}
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]}>
            <Input autoComplete="off" placeholder="如 DeepSeek 官方" />
          </Form.Item>
          <Form.Item
            name="platform"
            label="使用平台"
            rules={[{ required: true, message: "请选择使用平台" }]}
          >
            <Select placeholder="选择平台" options={platformOptions.map((o) => ({ value: o.value, label: o.label }))} />
          </Form.Item>
          {/* ⚠️ v2.0（设计 17 §13.2.1）：模型类型**必选**。
              它决定「模型配置页」里这个服务商能挂在哪些业务点下（图像业务点只列 image 型）。
              ⚠️ 编辑态**可改**，但改了会让**已指向它的映射失效** —— 后端类型校验会拦
              （业务点 kind ≠ 服务商 model_type ⇒ 400），所以这里如实提示后果，不静默。 */}
          <Form.Item
            name="model_type"
            label="模型类型"
            rules={[{ required: true, message: "请选择模型类型" }]}
            extra="决定哪些业务点可以选用该服务商；改动可能让已指向它的业务点失效"
          >
            <Select
              placeholder="选择模型类型"
              options={[
                { value: "text", label: "文本（chat/completions）" },
                { value: "image", label: "图像（images/generations）" },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="模型调用地址"
            rules={[{ required: true, message: "请填写模型调用地址" }]}
            extra="含路径段，如 https://api.deepseek.com/v1"
          >
            <Input autoComplete="off" placeholder="https://api.deepseek.com/v1" />
          </Form.Item>
          {/* ⚠️ v1.9 必填口径：**只约束新增**。
              编辑态后端语义是「留空 = 保持不变」——若这里也 required，
              「只改个名字」会被自己的必填卡死（用户无从绕过）。 */}
          <Form.Item
            name="access_token"
            label="访问令牌"
            rules={editing ? [] : [{ required: true, message: "请填写访问令牌" }]}
            extra={editing ? "留空则不修改" : undefined}
          >
            <Input.Password autoComplete="new-password" placeholder="sk-..." />
          </Form.Item>
          {/* ⚠️ v1.9：默认模型**必填**（用户口径「访问令牌和默认模型必填」）。
              模型名由**服务商**决定 ⇒ 改为**下拉选择**（用户口径「模型输入框改为下拉框选择」）：
              点「拉取模型」按当前地址+令牌现拉候选，避免手输拼错。
              ⚠️ 下拉**不禁止手输**（`showSearch` + 自由值）：服务商 `/models` 可能不返回
              全部可用模型（部分中转站只列白名单），硬禁手输会把能用的模型挡在外面。
              与 access_token 同理 —— 必填只约束新增（编辑态后端「不传 = 不变」）。 */}
          <Form.Item label="默认模型" required={!editing}>
            <Space.Compact style={{ width: "100%" }}>
              <Form.Item
                name="default_model"
                noStyle
                rules={editing ? [] : [{ required: true, message: "请选择或填写默认模型" }]}
              >
                <Select
                  showSearch
                  allowClear
                  placeholder="点右侧「拉取模型」后选择，或直接输入模型名"
                  options={modelOptions.map((m) => ({ value: m, label: m }))}
                  // 自由输入：目录不全时仍可手填（见上方注释）
                  mode={undefined}
                  style={{ width: "100%" }}
                />
              </Form.Item>
              <Button loading={loadingModels} onClick={() => void loadModels()}>
                拉取模型
              </Button>
            </Space.Compact>
          </Form.Item>
        </Form>
      </Modal>
    </GatewaySection>
  );
}

// 令牌映射 / 预警规则都要先有一个网关才能选（下拉里选网关）。
// 这一小段逻辑两边一样，抽出来避免各写一遍（同一语义不留两份实现）。
function useGatewayOptions() {
  const [gateways, setGateways] = useState<LlmGatewayConfig[]>([]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const data = await fetchGatewayConfigs();
        if (alive) setGateways(data.items);
      } catch {
        if (alive) setGateways([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return {
    gateways,
    options: gateways.map((g) => ({ value: g.id, label: g.name })),
  };
}

// ⚠️ ~~③ 令牌映射（独立页面）~~ **v1.9 整页移除**（用户口径「令牌映射页面移除」）：
// `TokensPage` 组件 + `PAGE_ITEMS` 的 tokens 项 + /llm-gateway/tokens 子路由
// + lib/api.ts 4 个请求函数 + LlmGatewayToken 两个类型，全部随
// llm_gateway_tokens 表退场（设计 17 §7.1a）。

// ── ④ 预警规则（独立页面） ──
function AlertsPage() {
  const { message } = App.useApp();
  const { gateways, options: gatewayOptions } = useGatewayOptions();
  const [form] = Form.useForm<LlmGatewayAlertPayload>();
  const [alerts, setAlerts] = useState<LlmGatewayAlert[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedGatewayId, setSelectedGatewayId] = useState<number | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<LlmGatewayAlert | null>(null);

  useEffect(() => {
    if (selectedGatewayId === null && gateways.length > 0) {
      setSelectedGatewayId(gateways[0].id);
    }
  }, [gateways, selectedGatewayId]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchGatewayAlerts({ gateway_id: selectedGatewayId ?? undefined });
      setAlerts(data.items);
    } catch {
      setAlerts([]);
    } finally {
      setLoading(false);
    }
  }, [selectedGatewayId]);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  };

  const openEdit = (a: LlmGatewayAlert) => {
    setEditing(a);
    form.setFieldsValue({
      gateway_id: a.gateway_id,
      platform: a.platform,
      function: a.function,
      metric: a.metric,
      operator: a.operator,
      threshold: a.threshold,
      window_seconds: a.window_seconds,
      level: a.level,
      enabled: a.enabled,
    });
    setModalOpen(true);
  };

  const submit = async () => {
    const values = await form.validateFields();
    try {
      if (editing) {
        await updateGatewayAlert(editing.id, {
          threshold: values.threshold,
          operator: values.operator,
          window_seconds: values.window_seconds,
          level: values.level,
          enabled: values.enabled,
        });
        message.success("已更新预警规则");
      } else {
        await createGatewayAlert({
          gateway_id: values.gateway_id,
          platform: values.platform ?? "",
          function: values.function ?? "",
          metric: values.metric,
          operator: values.operator ?? "gt",
          threshold: values.threshold,
          window_seconds: values.window_seconds ?? 86400,
          level: values.level ?? "warning",
          enabled: values.enabled ?? true,
        });
        message.success("已新增预警规则");
      }
      setModalOpen(false);
      await load();
    } catch {
      // 失败由拦截器报
    }
  };

  const remove = async (a: LlmGatewayAlert) => {
    try {
      await deleteGatewayAlert(a.id);
      message.success("已删除");
      await load();
    } catch {
      // 同上
    }
  };

  const columns: ColumnsType<LlmGatewayAlert> = [
    {
      title: "范围",
      key: "scope",
      width: 160,
      render: (_v, r) => <Text>{`${r.platform || "全平台"} / ${r.function || "全功能"}`}</Text>,
    },
    {
      title: "指标",
      dataIndex: "metric",
      width: 100,
      render: (m: string) => {
        const label: Record<string, string> = { calls: "调用量", fail_rate: "失败率", health: "健康" };
        return <Tag color="blue">{label[m] ?? m}</Tag>;
      },
    },
    {
      title: "条件",
      key: "cond",
      width: 140,
      render: (_v, r) => {
        const op: Record<string, string> = { gt: ">", gte: "≥", lt: "<", eq: "=" };
        const val = r.metric === "fail_rate" ? `${(r.threshold * 100).toFixed(1)}%` : `${r.threshold}`;
        return <Text code>{`${op[r.operator] ?? r.operator} ${val}`}</Text>;
      },
    },
    {
      title: "窗口",
      dataIndex: "window_seconds",
      width: 100,
      render: (s: number) => <Text type="secondary">{Math.round(s / 3600)}h</Text>,
    },
    {
      title: "级别",
      dataIndex: "level",
      width: 80,
      render: (l: string) => (l === "error" ? <Tag color="red">严重</Tag> : <Tag color="orange">警告</Tag>),
    },
    {
      title: "状态",
      dataIndex: "enabled",
      width: 80,
      render: (enabled: boolean) => (enabled ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
    },
    {
      title: "最近触发",
      dataIndex: "last_fired_at",
      width: 180,
      render: (v: string | null) => (v ? <Text type="secondary">{v}</Text> : <Text type="secondary">-</Text>),
    },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_v, record) => (
        <Space size="small">
          <Button type="link" size="small" onClick={() => openEdit(record)}>
            编辑
          </Button>
          <Popconfirm title="删除该预警规则？" okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => void remove(record)}>
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <GatewaySection
      eyebrow="LLM Gateway"
      title="预警规则"
      description="用量 / 失败率 / 健康 三类阈值规则。由定时任务「LLM 网关预警」轮询本地台账，超阈值即写站内通知。"
      action={
        <Button type="primary" onClick={openCreate}>
          新增预警规则
        </Button>
      }
    >
      <Card
        extra={
          <Space size={12} align="center">
            <Text type="secondary" style={{ fontSize: 12 }}>网关：</Text>
            <Select
              size="small"
              style={{ minWidth: 180 }}
              value={selectedGatewayId ?? undefined}
              onChange={(id: number) => setSelectedGatewayId(id)}
              options={gatewayOptions}
              placeholder="选择网关"
            />
            <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
          </Space>
        }
      >
        {gateways.length === 0 ? (
          <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
            还没有服务商 —— 请先在「服务商」页新增一个。
          </Typography.Paragraph>
        ) : (
          <Table<LlmGatewayAlert>
            rowKey="id"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={alerts}
            pagination={false}
            locale={{ emptyText: "还没有预警规则 —— 配置后由定时任务「LLM 网关预警」轮询触发站内通知" }}
          />
        )}
      </Card>

      <Modal
        open={modalOpen}
        title={editing ? "编辑预警规则" : "新增预警规则"}
        okText="保存"
        cancelText="取消"
        onOk={() => void submit()}
        onCancel={() => setModalOpen(false)}
        destroyOnClose
      >
        <Form form={form} layout="vertical" autoComplete="off">
          <Form.Item name="gateway_id" label="网关" rules={[{ required: true, message: "请选择网关" }]}>
            <Select disabled={!!editing} placeholder="选择网关" options={gatewayOptions} />
          </Form.Item>
          <Form.Item name="platform" label="平台（留空 = 全平台）">
            <Input autoComplete="off" placeholder="xhs" />
          </Form.Item>
          <Form.Item name="function" label="功能（留空 = 全功能）">
            <Input autoComplete="off" placeholder="generate" />
          </Form.Item>
          <Form.Item name="metric" label="指标" rules={[{ required: true, message: "请选择指标" }]}>
            <Select
              placeholder="选择指标"
              options={[
                { value: "calls", label: "调用量" },
                { value: "fail_rate", label: "失败率" },
                { value: "health", label: "健康状态" },
              ]}
            />
          </Form.Item>
          <Form.Item name="operator" label="操作符" initialValue="gt">
            <Select
              options={[
                { value: "gt", label: "大于 >" },
                { value: "gte", label: "大于等于 ≥" },
                { value: "lt", label: "小于 <" },
                { value: "eq", label: "等于 =" },
              ]}
            />
          </Form.Item>
          <Form.Item name="threshold" label="阈值" rules={[{ required: true, message: "请填写阈值" }]} extra="失败率填 0~1（如 0.3 = 30%）；其余填绝对值">
            <InputNumber style={{ width: "100%" }} min={0} />
          </Form.Item>
          <Form.Item name="window_seconds" label="统计窗口（秒）" initialValue={86400} extra="默认 86400 = 24 小时">
            <InputNumber style={{ width: "100%" }} min={60} />
          </Form.Item>
          <Form.Item name="level" label="通知级别" initialValue="warning">
            <Select
              options={[
                { value: "warning", label: "警告" },
                { value: "error", label: "严重" },
                { value: "info", label: "提示" },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </GatewaySection>
  );
}
