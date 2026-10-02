import {
  AlertOutlined,
  ApiOutlined,
  FundOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { App, Button, Card, Col, Form, Input, InputNumber, Modal, Popconfirm, Row, Select, Space, Switch, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { PageHeader } from "../../components/layout/app-shell";
import { WorkspaceShell } from "../../components/layout/workspace-shell";
import { buildGatewayTree, isExpandableProvider, type GatewayTreeRow } from "./gateway-tree";
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
  LlmGatewayAlertScope,
  LlmGatewayConfig,
  LlmGatewayConfigPayload,
  LlmGatewayPlatformOption,
  LlmGatewayStats,
} from "../../types";

const { Text } = Typography;

// 行内简短展示用
function statsLine(stats: LlmGatewayStats["today"]): string {
  if (!stats) return "-";
  return `调用 ${stats.calls} · 成功 ${stats.success_calls} · Tokens ${stats.total_tokens} · 均耗 ${stats.avg_latency_ms}ms`;
}

// 服务商列表 = **父子树**（2026-10-02 用户口径「服务商列表使用父子树，父是服务商，
// 子是服务商下的具体模型」）—— 构建逻辑抽到 `./gateway-tree.ts`，见该文件顶部注释
// 说明「为何用 Table 可展开子行而非 Tree」以及「子行为何只承载模型名」。

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
// 2026-10-02：壳层改为复用 `WorkspaceShell`（与小红书 AppShell 同一份），
// 顺带补齐了此前网关页缺失的「主题切换 + 消息提醒」顶栏动作。
export function LlmGatewayPage() {
  const navigate = useNavigate();
  const location = useLocation();

  // 当前段：`/llm-gateway/<seg>` 的 `<seg>`；空 = 尚未选页（由内部 index 重定向兜底）
  const seg = location.pathname.replace(/^\/llm-gateway\/?/, "").split("/")[0] ?? "";

  return (
    <WorkspaceShell
      brand="大模型网关"
      mainItems={PAGE_ITEMS.map((p) => ({ key: p.key, icon: p.icon, label: p.label }))}
      selectedKeys={[seg]}
      onNavigate={(key) => navigate(`/llm-gateway/${key}`)}
    >
      <GatewayRoutes />
    </WorkspaceShell>
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

// 内容骨架：三个页面共用的页头（顶栏与 Content 容器由 `WorkspaceShell` 提供，这里不再重复）。
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
      <PageHeader eyebrow={eyebrow} title={title} description={description} action={action} />
      {children}
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
      // 列表挂载即拉**全部**服务商的调用量（调用量列按行展示，不再依赖「选中」交互）
      const items = data.items;
      void Promise.all(
        items.map(async (g) => {
          try {
            const body = await fetchGatewayStats(g.id);
            setStatsMap((prev) => ({ ...prev, [g.id]: body }));
          } catch {
            // 取不到 = 空态（不是错误）：统计列显示占位，不弹红
            setStatsMap((prev) => ({ ...prev, [g.id]: { id: g.id, available: false, today: null, week: null } }));
          }
        })
      );
    } catch {
      // 取不到由全局拦截器报；这里保证表格空态可见
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 服务商 → 父子树（父 = 服务商，子 = 其模型目录）。数据源 = `gateways`（每项含 `models`）。
  const treeData = useMemo(() => buildGatewayTree<LlmGatewayConfig>(gateways), [gateways]);

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

  // 列定义按**树节点**（`GatewayTreeRow`）书写，而不是 `LlmGatewayConfig`：
  // 父行（服务商）渲染完整字段；子行（模型）只在「名称」列显示模型名、其余列以 `-` 占位。
  // ⚠️ 每个 `render` 都用 `row.kind === "provider"` 早返回 —— 这是**同一个表格两套行形态**
  // 的唯一分叉点；不抽两份列定义（同一语义不留两份实现，DEF-044 同型风险）。
  const columns: ColumnsType<GatewayTreeRow<LlmGatewayConfig>> = [
    {
      title: "名称",
      key: "name",
      width: 260,
      render: (_v, row) => {
        if (row.kind === "model") {
          // 子行：模型名（缩进由表格的展开层级自带，这里只标一个图标区分「这是模型」）
          return (
            <Space size={6}>
              <span style={{ color: "#8c8c8c" }}>└</span>
              <Text code>{row.model}</Text>
            </Space>
          );
        }
        return <Text strong>{row.gateway.name}</Text>;
      },
    },
    {
      // ⚠️ v1.9 新增（用户口径「连接配置页面新增唯一KEY，新增后不可编辑」）：
      // 表格里是**只读展示 + 可复制**（业务代码要用它，抄错代价高）；
      // 弹窗内不提供输入框 —— 服务端也拒改（PATCH 传它 ⇒ 422）。
      // ⚠️ 子行（模型）**没有**服务编码 —— 它属于父级服务商，这里留 `-`。
      title: "服务编码",
      key: "provider_key",
      width: 190,
      render: (_v, row) => {
        if (row.kind === "model") return <Text type="secondary">-</Text>;
        const key = row.gateway.provider_key;
        return (
          <Typography.Text
            code
            copyable={key ? { text: key, tooltips: ["复制 KEY", "已复制"] } : false}
          >
            {key || "-"}
          </Typography.Text>
        );
      },
    },
    {
      // ⚠️ v1.9 新增（用户口径「新增使用平台，后端新增枚举下拉选择必选」）：
      // 显示后端枚举的**中文名**（label），值仍是枚举键。
      title: "使用平台",
      key: "platform",
      width: 110,
      render: (_v, row) => {
        if (row.kind === "model") return <Text type="secondary">-</Text>;
        const value = row.gateway.platform;
        const label = platformOptions.find((o) => o.value === value)?.label;
        // 枚举未加载/值不在枚举内 ⇒ 原样显示枚举键（**不造名**，缺值口径）
        return <Tag>{label ?? value ?? "-"}</Tag>;
      },
    },
    {
      title: "类型",
      key: "model_type",
      width: 90,
      render: (_v, row) => {
        if (row.kind === "model") return <Text type="secondary">-</Text>;
        const value = row.gateway.model_type;
        return (
          <Tag color={value === "image" ? "purple" : "blue"}>
            {value === "image" ? "图像" : "文本"}
          </Tag>
        );
      },
    },
    {
      title: "地址",
      key: "base_url",
      render: (_v, row) =>
        row.kind === "model" ? (
          <Text type="secondary">-</Text>
        ) : (
          <Text type="secondary">{row.gateway.base_url}</Text>
        ),
    },
    {
      title: "默认模型",
      key: "default_model",
      width: 180,
      render: (_v, row) => {
        if (row.kind === "model") return <Text type="secondary">-</Text>;
        const m = row.gateway.default_model;
        return m ? <Text code>{m}</Text> : <Text type="secondary">-</Text>;
      },
    },
    {
      title: "调用量（今日/近7日）",
      key: "stats",
      width: 260,
      render: (_v, row) => {
        // 调用量是**服务商维度**的台账聚合（模型维度不单独统计）—— 子行留 `-`。
        if (row.kind === "model") return <Text type="secondary">-</Text>;
        const s = statsMap[row.gateway.id];
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
      render: (_v, row) =>
        row.kind === "model" ? (
          <Text type="secondary">-</Text>
        ) : row.gateway.has_access_token ? (
          <Tag color="green">已配置</Tag>
        ) : (
          <Text type="secondary">无</Text>
        ),
    },
    {
      title: "健康",
      key: "healthy",
      width: 90,
      render: (_v, row) => {
        // 健康探测是**服务商维度**的（`/{id}/test` 打的是服务商地址）—— 模型不单独探活。
        if (row.kind === "model") return <Text type="secondary">-</Text>;
        const g = row.gateway;
        return (
          <Tooltip title={g.last_health_at ? `最近探测 ${g.last_health_at}` : "尚未探测"}>
            <Tag color={g.healthy ? "green" : "orange"}>{g.healthy ? "正常" : "未探测"}</Tag>
          </Tooltip>
        );
      },
    },
    {
      // ⚠️ v1.9 新增（用户口径「新增停用开启功能」）：替代被移除的「删除」。
      // 停用语义 = **业务取不到该服务商**（后端抛 LLM_PROVIDER_DISABLED，不回落）。
      // ⚠️ 只作用于**服务商**（父行）；模型没有独立的启停（服务商停用即其下模型全不可用）。
      title: "启用",
      key: "enabled",
      width: 90,
      render: (_v, row) => {
        if (row.kind === "model") return <Text type="secondary">-</Text>;
        const g = row.gateway;
        return (
          <Tooltip title={g.enabled ? "点击停用（业务将取不到该服务商）" : "点击启用"}>
            <Switch
              size="small"
              checked={g.enabled}
              loading={togglingId === g.id}
              onChange={(next) => void toggleEnabled(g, next)}
            />
          </Tooltip>
        );
      },
    },
    {
      // 操作同样是**服务商维度**的（测试打的是服务商地址、编辑编辑的是服务商行）。
      // 子行（模型）不提供任何操作 —— 模型由服务商「拉取模型」刷新，不是独立可编辑对象。
      title: "操作",
      key: "actions",
      width: 130,
      render: (_v, row) => {
        if (row.kind === "model") return <Text type="secondary">-</Text>;
        const g = row.gateway;
        return (
          <Space size="small">
            <Button type="link" size="small" loading={testingId === g.id} onClick={() => void runTest(g)}>
              测试
            </Button>
            <Button type="link" size="small" onClick={() => openEdit(g)}>
              编辑
            </Button>
            {/* ⚠️ v1.9：「删除」按钮 + Popconfirm **整段移除**（用户口径「移除删除功能」）。
                替代入口 = 上面的启用开关。后端 DELETE 保留为 410 墓碑，前端不再调用。 */}
          </Space>
        );
      },
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
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        }
      >
        <Table<GatewayTreeRow<LlmGatewayConfig>>
          rowKey="key"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={treeData}
          pagination={false}
          // ⚠️ 父子树（2026-10-02 用户口径）：父 = 服务商，子 = 服务商下的具体模型。
          // 每行自带 `children`（见 `buildGatewayTree`）⇒ 表格自动渲染展开箭头。
          // `defaultExpandAllRows` 只在**首次挂载**生效，因此把「全展开」设成默认
          // （服务商数量有限，模型目录正是用户要看的）；用户可手动折叠。
          expandable={{
            defaultExpandAllRows: true,
            // 无模型的父行不给展开箭头（避免出现「点了没反应」的空箭头）
            rowExpandable: isExpandableProvider,
          }}
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

// ── ④ 预警规则（独立页面；v2.3 三级作用域） ──
//
// 作用域三级（设计 17 §7.1b）：
//   ① 全网关  → 逐服务商独立评估，谁的指标超阈值就给谁发通知（Plan A）
//   ② 服务商  → 只评估指定服务商
//   ③ 具体模型 → 只评估指定服务商的指定模型
// 唯一非法组合 = 全网关 + 指定模型（后端 400 `LLM_ALERT_SCOPE_CONFLICT`）。
//
// ⚠️ 列表**不按网关过滤**：后端 `gateway_id` 过滤会把 `gateway_id=null` 的**全网关规则滤掉**，
// 所以这里全量拉取，只在渲染时把「所属服务商」列拍平（全网关行显示「全网关」标签）。
function AlertsPage() {
  const { message } = App.useApp();
  const { gateways, options: gatewayOptions } = useGatewayOptions();
  const [form] = Form.useForm<LlmGatewayAlertPayload>();
  const [alerts, setAlerts] = useState<LlmGatewayAlert[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<LlmGatewayAlert | null>(null);
  /** 表单内**作用域单选**（不提交给后端，只驱动 gateway_id / model_name 两字段） */
  const [formScope, setFormScope] = useState<LlmGatewayAlertScope>("all");
  /** 表单内**当前选中的服务商**（模型下拉候选来自它） */
  const [formGatewayId, setFormGatewayId] = useState<number | null>(null);

  const gatewayNameOf = useMemo(() => {
    const m = new Map<number, string>();
    gateways.forEach((g) => m.set(g.id, g.name));
    return m;
  }, [gateways]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // 全量拉取（**不传 gateway_id**），让全网关规则也在列表里可见
      const data = await fetchGatewayAlerts();
      setAlerts(data.items);
    } catch {
      setAlerts([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    // 默认「全网关」（覆盖所有服务商，最不容易漏配）
    setFormScope("all");
    setFormGatewayId(null);
    form.setFieldsValue({ operator: "gt", window_seconds: 86400, level: "warning", metric: "fail_rate" } as LlmGatewayAlertPayload);
    setModalOpen(true);
  };

  const openEdit = (a: LlmGatewayAlert) => {
    setEditing(a);
    const scope: LlmGatewayAlertScope = a.scope ?? (a.gateway_id === null ? "all" : a.model_name ? "model" : "gateway");
    setFormScope(scope);
    setFormGatewayId(a.gateway_id ?? null);
    form.setFieldsValue({
      platform: a.platform,
      function: a.function,
      metric: a.metric,
      operator: a.operator,
      threshold: a.threshold,
      window_seconds: a.window_seconds,
      level: a.level,
      enabled: a.enabled,
    } as LlmGatewayAlertPayload);
    setModalOpen(true);
  };

  const submit = async () => {
    const values = await form.validateFields();
    const gatewayId = formScope === "all" ? null : formGatewayId;
    const modelName = formScope === "model" ? (values.model_name ?? "").trim() : "";
    if (formScope !== "all" && gatewayId === null) {
      message.warning("请选择服务商");
      return;
    }
    if (formScope === "model" && !modelName) {
      message.warning("请选择具体模型");
      return;
    }
    try {
      if (editing) {
        // 作用域不可改（同网关不可改的旧纪律）：只允许改阈值类字段
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
          gateway_id: gatewayId,
          model_name: modelName,
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
      title: "作用域",
      key: "scope",
      width: 130,
      render: (_v, r) => {
        const scope: LlmGatewayAlertScope = r.scope ?? (r.gateway_id === null ? "all" : r.model_name ? "model" : "gateway");
        if (scope === "all") return <Tag color="geekblue">全网关</Tag>;
        if (scope === "model") return <Tag color="purple">具体模型</Tag>;
        return <Tag color="blue">指定服务商</Tag>;
      },
    },
    {
      title: "所属服务商 / 模型",
      key: "target",
      width: 220,
      render: (_v, r) => {
        const scope: LlmGatewayAlertScope = r.scope ?? (r.gateway_id === null ? "all" : r.model_name ? "model" : "gateway");
        if (scope === "all") {
          return (
            <Text type="secondary" style={{ fontSize: 12 }}>
              全部服务商（逐个独立评估）
            </Text>
          );
        }
        const gwName = r.gateway_id === null ? "-" : gatewayNameOf.get(r.gateway_id) ?? `#${r.gateway_id}`;
        return (
          <Space direction="vertical" size={0}>
            <Text>{gwName}</Text>
            {scope === "model" && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                模型：{r.model_name}
              </Text>
            )}
          </Space>
        );
      },
    },
    {
      title: "平台 / 功能",
      key: "pf",
      width: 140,
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
      width: 90,
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
      width: 170,
      render: (v: string | null) => (v ? <Text type="secondary">{v}</Text> : <Text type="secondary">-</Text>),
    },
    {
      title: "操作",
      key: "actions",
      width: 130,
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

  /** 模型下拉候选 = 所选服务商的模型目录（未选服务商 / 目录为空 ⇒ 空态） */
  const modelOptions = useMemo(() => {
    if (formGatewayId === null) return [];
    const gw = gateways.find((g) => g.id === formGatewayId);
    const models: string[] = gw?.models ?? [];
    return models.map((m) => ({ value: m, label: m }));
  }, [gateways, formGatewayId]);

  return (
    <GatewaySection
      eyebrow="LLM Gateway"
      title="预警规则"
      description="用量 / 失败率 / 健康 三类阈值规则，作用域支持「全网关 / 指定服务商 / 指定服务商 + 具体模型」三级。由定时任务「LLM 网关预警」轮询本地台账，超阈值即写站内通知。"
      action={
        <Button type="primary" onClick={openCreate}>
          新增预警规则
        </Button>
      }
    >
      <Card
        extra={
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        }
      >
        {gateways.length === 0 && alerts.length === 0 ? (
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
          <Form.Item label="作用域" required>
            <Select
              value={formScope}
              disabled={!!editing}
              onChange={(v: LlmGatewayAlertScope) => {
                setFormScope(v);
                if (v === "all") {
                  setFormGatewayId(null);
                }
              }}
              options={[
                { value: "all", label: "全网关（所有服务商逐个独立评估）" },
                { value: "gateway", label: "指定服务商" },
                { value: "model", label: "指定服务商 + 具体模型" },
              ]}
            />
          </Form.Item>
          {formScope !== "all" && (
            <Form.Item label="服务商" required extra={editing ? "作用域不可修改" : undefined}>
              <Select
                value={formGatewayId ?? undefined}
                disabled={!!editing}
                placeholder="选择服务商"
                options={gatewayOptions}
                onChange={(id: number) => {
                  setFormGatewayId(id);
                  // 换服务商 ⇒ 已选模型可能不在新目录里 ⇒ 清掉，避免提交一个不存在的模型
                  form.setFieldValue("model_name", undefined);
                }}
              />
            </Form.Item>
          )}
          {formScope === "model" && (
            <Form.Item
              name="model_name"
              label="具体模型"
              required
              extra={modelOptions.length === 0 ? "该服务商暂无模型目录，请先到「服务商」页拉取模型" : undefined}
              rules={[{ required: true, message: "请选择具体模型" }]}
            >
              <Select
                placeholder={formGatewayId === null ? "请先选择服务商" : "选择模型"}
                disabled={formGatewayId === null || modelOptions.length === 0}
                options={modelOptions}
                showSearch
              />
            </Form.Item>
          )}
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="platform" label="平台（留空 = 全平台）">
                <Input autoComplete="off" placeholder="xhs" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="function" label="功能（留空 = 全功能）">
                <Input autoComplete="off" placeholder="generate" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
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
            </Col>
            <Col span={12}>
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
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="threshold" label="阈值" rules={[{ required: true, message: "请填写阈值" }]} extra="失败率填 0~1（如 0.3 = 30%）；其余填绝对值">
                <InputNumber style={{ width: "100%" }} min={0} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="window_seconds" label="统计窗口（秒）" initialValue={86400} extra="默认 86400 = 24 小时">
                <InputNumber style={{ width: "100%" }} min={60} />
              </Form.Item>
            </Col>
          </Row>
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
