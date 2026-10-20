import { WaitMask } from "../../components/ui/wait-mask";
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

import { WorkspaceShell } from "../../components/layout/workspace-shell";
import {
  buildGatewayTree,
  isExpandableProvider,
  type GatewayModelNode,
  type GatewayProviderNode,
  type GatewayTreeRow,
} from "./gateway-tree";
import { UsageDashboardPanel } from "./usage-dashboard-panel";
import {
  createGatewayConfig,
  createGatewayAlert,
  deleteGatewayAlert,
  fetchBusinessProviderKeys,
  fetchGatewayAlerts,
  fetchGatewayConfigs,
  fetchGatewayModelsTree,
  fetchGatewayPlatforms,
  fetchGatewayStats,
  syncGatewayModels,
  testGatewayConfig,
  updateGatewayConfig,
  updateGatewayModel,
  updateGatewayAlert,
} from "../../lib/api";
import type {
  LlmGatewayAlert,
  LlmGatewayAlertPayload,
  LlmGatewayAlertScope,
  LlmGatewayConfig,
  LlmGatewayConfigPayload,
  LlmGatewayModelRow,
  LlmGatewayPlatformOption,
  LlmGatewayStats,
  ModelType,
} from "../../types";

const { Text } = Typography;

// 行内简短展示用
function statsLine(stats: LlmGatewayStats["today"]): string {
  if (!stats) return "-";
  return `调用 ${stats.calls} · 成功 ${stats.success_calls} · Tokens ${stats.total_tokens} · 均耗 ${stats.avg_latency_ms}ms`;
}

/**
 * 类型枚举值 → 中文标签（v2.9）。选项由**后端下发**（`GET /llm/business-provider-keys`）。
 *
 * ⚠️ 枚举未加载 / 值不在枚举内 ⇒ **原样显示枚举键**（缺值口径：不造名、不猜）；
 * 这是「取不到就留白」在标签场景的落地 —— 宁可显示 `text` 也不显示「文本」，
 * 以免后端加了第五种类型时前端把它错标成已有的某种。
 */
function typeLabel(
  options: { value: string; label: string }[],
  value: string,
): string {
  return options.find((o) => o.value === value)?.label ?? value;
}

/** 类型标签配色：图片紫 / 多态青 / 视频橙 / 文本（及其余）蓝。 */
function typeTagColor(value: string): string {
  if (value === "image") return "purple";
  if (value === "multimodal") return "cyan";
  if (value === "video") return "orange";
  return "blue";
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

// 内容骨架：三个页面共用的容器（顶栏与 Content 容器由 `WorkspaceShell` 提供，这里不再重复）。
//
// 2026-10-05 用户口径「移除所有页面页面头」：原 `GatewaySection` 要传
// eyebrow / title / description / action 并渲染一个 `PageHeader` —— **整块页头已移除**
// （三行与侧栏菜单项重复），`GatewaySection` 收成**纯透传容器**。
// ⚠️ 刻意**保留**这个组件而不是在三个页面里各写一次 `<>…</>`：它是「网关页内容骨架」
//    的唯一锚点，删掉就等于把「壳层只此一份」的边界擦掉（日后加壳层级布局会各写一份）。
function GatewaySection({ children }: { children: ReactNode }) {
  return <>{children}</>;
}

// ── ① 用量看板（独立页面，只读） ──
function UsagePage({ onGoGateways }: { onGoGateways: () => void }) {
  return (
    <GatewaySection>
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
  /** v2.9：正在同步模型的服务商 id（操作列「同步服务商模型」按钮的 loading） */
  const [syncingId, setSyncingId] = useState<number | null>(null);
  /** v2.9：正在改类型的模型行 id（「类型」列下拉的 loading） */
  const [typeSavingId, setTypeSavingId] = useState<number | null>(null);
  /** v2.10：正在启停的模型行 id（子表「启用」列开关的 loading） */
  const [togglingModelId, setTogglingModelId] = useState<number | null>(null);
  /** v2.10：正在做健康检查的模型行 id（子表「健康」列检查按钮的 loading）。
   *  ⚠️ 探测的粒度是**服务商**（连通性由凭据+地址决定），检查完顺带写回该服务商下
   *  全部模型行的健康戳 —— 故这个 id 只是「哪个按钮在转」，不代表探测只针对该模型。 */
  const [checkingModelId, setCheckingModelId] = useState<number | null>(null);
  const [statsMap, setStatsMap] = useState<Record<number, LlmGatewayStats>>({});
  // 「使用平台」枚举**由后端下发**（v1.9 D3）—— 前端不写死选项
  const [platformOptions, setPlatformOptions] = useState<LlmGatewayPlatformOption[]>([]);
  // v2.9：**模型类型枚举由后端下发**（`GET /llm/business-provider-keys` 的 `model_types`）——
  // 与「业务点映射」页共用同一份真相源（`business_provider_keys.MODEL_TYPE_LABELS`），
  // 前端零硬编码（加第五种类型时前端不用改）。
  const [modelTypeOptions, setModelTypeOptions] = useState<{ value: string; label: string }[]>([]);
  // ⚠️ v2.9：`modelOptions` / `loadingModels`（弹窗内「拉取模型」候选池）**随
  // `Form.List` 模型编辑器一并删除** —— 模型不再手工挑，改为「同步服务商模型」拉全量。

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

  // v2.9：模型类型枚举（同源 `fetchBusinessProviderKeys`，业务点映射页也在用）
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const data = await fetchBusinessProviderKeys();
        if (alive) setModelTypeOptions(data.model_types);
      } catch {
        // 取不到 = 类型下拉无选项；不写死兜底（缺值不造假，宁可空）
        if (alive) setModelTypeOptions([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // ⚠️ v2.9（2026-10-02 用户口径「服务商使用父子列表展示」）：
      // 走**一个**端点拿到「服务商 + 其全部模型行」（`GET /llm/gateway-configs/models-tree`），
      // 不再自己拼「列表 + 每行再拉一次模型」—— 一次请求一个结果，
      // 中间的失败语义由后端定（详见该端点 docstring）。
      const data = await fetchGatewayModelsTree();
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

  // 服务商列表 → 父子树（父 = 服务商，子 = 其模型目录）。数据源 = `gateways`（每项含 `models`）。
  const treeData = useMemo(() => buildGatewayTree<LlmGatewayConfig>(gateways), [gateways]);
  // 父表的数据源 = **只有服务商行**（子模型不再混在同一张表里）。
  // ⚠️ `buildGatewayTree` 仍产出 children（展开行要用）——父表靠 `expandedRowRender`
  // 拿 `row.children` 渲染子表，所以这里**不能**把 children 摘掉（摘了子表就没数据了）。
  const providerRows = useMemo(
    () => treeData.filter((r): r is GatewayProviderNode<LlmGatewayConfig> => r.kind === "provider"),
    [treeData]
  );

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  };

  const openEdit = (g: LlmGatewayConfig) => {
    setEditing(g);
    form.setFieldsValue({
      name: g.name,
      base_url: g.base_url,
      // v2.9：**弹窗里不再有模型行编辑器**（用户口径「不需要再进入编辑后添加模型」）——
      // 模型列表在**主页面的父子树**里直接展示，类型在树上**就地改**，模型集合由
      // 「同步服务商模型」按钮刷新。故这里不再 setFieldsValue 回填 `models`，
      // 也不再读 `g.catalog` 填候选池（那是给被删掉的「拉取模型」下拉用的）。
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
          ...(values.base_url ? { base_url: values.base_url } : {}),
          ...(values.access_token ? { access_token: values.access_token } : {}),
          // ⚠️ **不传 `models`**（v2.9）：模型集合由「同步服务商模型」维护，
          // 编辑弹窗只改服务商本体（名称 / 地址 / 令牌）。传了 `models` 是**整表替换**，
          // 会把树上用户手工改过的类型全冲掉。
        });
        message.success("已更新服务商");
      } else {
        // v2.9：创建时**后端自动同步一次**模型目录 + AI 归纳类型（用户口径
        // 「添加服务商时默认拉取一次」）。同步是否成功看响应上的 `sync`：
        // ⚠️ **同步失败不阻断创建**（服务商照样建成），故这里按 `sync.ok` 分开提示，
        // 失败时明确引导用户去操作列重试 —— 不静默、也不谎报成功。
        const created = await createGatewayConfig({
          name: values.name,
          base_url: values.base_url || "http://127.0.0.1:3000",
          access_token: values.access_token || "",
          models: [],
        });
        const sync = created.sync;
        if (sync?.ok) {
          message.success(`已新增服务商；${sync.reason}`);
        } else if (sync) {
          message.warning(`已新增服务商，模型同步未完成：${sync.reason}`);
        } else {
          message.success("已新增服务商");
        }
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
  // 后端 DELETE 是 410 墓碑；服务商一旦被业务引用就不能删。
  // ⚠️ ~~服务商级启停 `toggleEnabled`~~ **v2.10 删除**（用户口径「启用状态开关放具体模型维度，
  // 不要放服务商维度」）：开关下沉到子表「启用」列（见 `toggleModelEnabled`），
  // 父表不再有这一列 ⇒ 本方法与配套的 `togglingId` state 一并退场（避免留死代码）。
  // 后端服务商级 `enabled` 字段保留（模型级可用性的前提之一），只是前端不再提供入口。

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

  // v2.9：同步服务商模型（操作列按钮）—— 拉目录 + AI 归纳类型 + upsert 模型行。
  // ⚠️ 同步「没成功」不是异常：接口恒 200，看 `sync.ok` 分流提示（网络/令牌问题如实说）。
  const runSync = async (g: LlmGatewayConfig) => {
    setSyncingId(g.id);
    try {
      const result = await syncGatewayModels(g.id);
      const s = result.sync;
      if (s.ok) {
        message.success(`${g.name}：${s.reason}`);
      } else {
        message.warning(`${g.name} 同步未完成：${s.reason}`);
      }
      await load();
    } catch {
      // 真异常（越权 404 等）由全局拦截器报
    } finally {
      setSyncingId(null);
    }
  };

  // v2.9：「类型」列**就地改类型**（用户口径「模型类型列表中支持修改，下拉框」）。
  // AI 归纳只是**初始值**，用户有最终决定权 ⇒ 改完立即 PATCH 落库，成功才刷新列表。
  // ⚠️ 入参取**模型子节点整行**（`GatewayTreeRow` 的 model 分支）而不是拆开的
  // `(gateway, modelId)`：类型断言会因此自然收窄，不需要在 render 里 `as number` 硬转。
  const changeModelType = async (
    row: Extract<GatewayTreeRow<LlmGatewayConfig>, { kind: "model" }>,
    next: ModelType,
  ) => {
    const modelId = row.modelId;
    if (modelId === null) return; // 无 id 发不出 PATCH（理论上表格里不会出现）
    setTypeSavingId(modelId);
    try {
      // 父服务商 id 直接取子节点上的 `parentId`（树构建时已带回，不必再查表）
      await updateGatewayModel(row.parentId, modelId, { model_type: next });
      message.success(`已改为${modelTypeOptions.find((o) => o.value === next)?.label ?? next}`);
      await load();
    } catch {
      // 失败由拦截器报；本地不乐观更新 ⇒ 表格仍显示后端旧值
    } finally {
      setTypeSavingId(null);
    }
  };

  // v2.10：「启用」下沉到**模型维度**（用户口径「健康状态和启用状态开关放具体模型维度，
  // 不要放服务商维度」）。与 `changeModelType` 同套路：就地 PATCH，成功才 `load()` 刷新，
  // 不做本地乐观更新（失败了表格继续显示后端真值，不会被 UI 骗）。
  // ⚠️ 父服务商 id 取子节点带回的 `parentId`，不必再查表。
  const toggleModelEnabled = async (
    row: Extract<GatewayTreeRow<LlmGatewayConfig>, { kind: "model" }>,
    next: boolean,
  ) => {
    const modelId = row.modelId;
    if (modelId === null) return; // 无 id 发不出 PATCH
    setTogglingModelId(modelId);
    try {
      await updateGatewayModel(row.parentId, modelId, { enabled: next });
      message.success(next ? `已启用 ${row.model}` : `已停用 ${row.model}`);
      await load();
    } catch {
      // 失败由拦截器报；本地不乐观更新 ⇒ 表格仍显示后端旧值
    } finally {
      setTogglingModelId(null);
    }
  };

  // v2.10：「健康」列**检查按钮**（用户口径「子表健康列未检查的展示检测检查按钮，触发可健康检查」）。
  // 「未检查」= 该模型从未探测过（`lastHealthAt == null`）⇒ 显示「检查」按钮，点一下触发探测；
  // 已探测过的显示健康标签（正常 / 异常），不再重复给按钮（信息已足）。
  // ⚠️ 探测端点复用服务商级 `POST /{config_id}/test`（连通性由凭据 + 地址决定，模型共用同一凭据），
  // 后端探测成功后会**写回该服务商下所有模型行**的健康戳 ⇒ 一处检查、整组刷新。
  const runModelCheck = async (
    row: Extract<GatewayTreeRow<LlmGatewayConfig>, { kind: "model" }>,
  ) => {
    const modelId = row.modelId;
    if (modelId === null) return; // 无 id ⇒ 无法定位服务商（理论上表格里不会出现）
    setCheckingModelId(modelId);
    try {
      const result = await testGatewayConfig(row.parentId);
      message.success(`${row.parentName}：${result.message}`);
      await load();
    } catch {
      // 失败由拦截器报；不乐观更新 ⇒ 表格仍显示后端真值（保持「未探测」）
    } finally {
      setCheckingModelId(null);
    }
  };

  // ⚠️ ~~`loadModels`（弹窗内「拉取模型」预览）~~ **v2.9 删除**（用户口径
  // 「配置服务商时应直接父子树展示该服务商下所有模型，不需要再进入编辑后添加模型」）。
  // 拉取目录的动作改由**服务商本体**承担：① 创建时后端自动同步一次；
  // ② 之后在列表操作列点「同步服务商模型」（`runSync`）—— 都会真落库、AI 归纳类型。
  // 「预览候选池（不写库）」这个中间态不再需要，故连同该预览调用一起退场
  // （`previewGatewayModels` 函数本身保留在 `lib/api.ts`，本页不再 import/调用）。

  // ⚠️ **2026-10-02 用户口径「父子表的表头独立设置」**：父子**各用一套列定义**，
  // 不再共用一张表（此前共表 ⇒ 子行在服务商专有列上处处显示 `-`，表头也是服务商的，
  // 例如子行「类型」列头上写着服务商语境的字，读起来割裂）。
  // 现在的结构 = **父表（服务商）× 子表（模型）两张独立表**，各写各的列与表头。
  //
  // ⛔ **实现红线（踩过坑，2026-10-02）**：`expandedRowRender` 里**可以放 `<Table>`**
  // （antd 支持嵌套 Table），但**必须同时关掉父表的「树数据自动探测」** ——
  // 否则点开箭头**整页白屏**（React 渲染直接崩）。
  //
  // 根因：`buildGatewayTree` 产出的**服务商行自带 `children` 字段**，而 `children` 正是
  // antd `Table` 树形数据的**内置字段名**（`childrenColumnName` 默认 `"children"`）。
  // 于是 antd 认为「这是一棵要用内置树模式渲染的树」⇒ 同时跑 ① 内置树递归渲染 +
  // ② 我们的 `expandedRowRender` ⇒ 两条渲染路径互相打架，React 崩白屏。
  //
  // ✅ 修复 = 在父 `<Table>` 上设 **`childrenColumnName="__no_tree__"`**（一个数据里
  // 不存在的 key）：antd 探测不到子节点，自动放弃内置树渲染，只走 `expandedRowRender`。
  // ⇒ 这样嵌套 Table 保住、父子表头各自独立，真正由两张表定义。
  // （**不要**靠摘掉 `children` 字段来绕 —— 展开区还指着 `row.children` 取数据。）
  //
  // 生成器函数 `providerColumns` 的理由：列定义要用到 `statsMap` / `togglingId` 等
  // 组件内状态，闭包捕获它们才能把列定义留在组件内（而不是提到模块级再传一堆 props）。
  const providerColumns: ColumnsType<GatewayProviderNode<LlmGatewayConfig>> = [
    {
      title: "服务商名称",
      key: "name",
      width: 260,
      render: (_v, row) => <Text strong>{row.gateway.name}</Text>,
    },
    {
      // ⚠️ v1.9 新增（用户口径「连接配置页面新增唯一KEY，新增后不可编辑」）：
      // 表格里是**只读展示 + 可复制**（业务代码要用它，抄错代价高）；
      // 弹窗内不提供输入框 —— 服务端也拒改（PATCH 传它 ⇒ 422）。
      // v2.9 父子分开：这一列属于**父表（服务商）** ⇒ 子表另有自己的列定义（见 `modelColumns`）。
      title: "服务编码",
      key: "provider_key",
      width: 190,
      render: (_v, row) => {
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
      // ⚠️ ~~「覆盖平台」「模型类型」两列~~ **2026-10-02 移除**（用户口径
      // 「服务商列表页的覆盖平台跟模型类型都不需要」）。
      // 这两列本就是**其模型行的聚合**（多标签），信息已在子表里逐模型给出 ⇒
      // 父表留着只是重复，且聚合标签读起来不如子表明细清楚。父表回归「服务商本体」字段。
      // （`platformOptions` / `modelTypeOptions` / `typeLabel` / `typeTagColor` 仍被子表用，
      //   故这些定义保留，只是不再出现在父表列里。）
      title: "模型调用地址",
      key: "base_url",
      ellipsis: true,
      render: (_v, row) => <Text type="secondary">{row.gateway.base_url}</Text>,
    },
    {
      title: "调用量（今日/近7日）",
      key: "stats",
      width: 250,
      render: (_v, row) => {
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
        row.gateway.has_access_token ? (
          <Tag color="green">已配置</Tag>
        ) : (
          <Text type="secondary">无</Text>
        ),
    },
    {
      // ⚠️ ~~「健康」「启用」两列~~ **2026-10-02 下沉到模型维度**（用户口径
      // 「健康状态和启用状态开关放具体模型维度，不要放服务商维度」）。
      // 理由：同一个服务商下的模型是独立可用的（某模型停用不影响别的），
      // 摆一行服务商级开关表达不出来这层粒度；健康探测同理 —— 探测是拿服务商凭据发的，
      // 但通不通最终要落到「这个模型能不能用」上。
      // ⇒ 两列移到子表（`modelColumns`），父表只留服务商本体字段。
      title: "操作",
      key: "actions",
      width: 230,
      render: (_v, row) => {
        const g = row.gateway;
        return (
          <Space size="small">
            <Button type="link" size="small" loading={testingId === g.id} onClick={() => void runTest(g)}>
              测试
            </Button>
            {/* v2.9：拉目录 + AI 归纳类型 + upsert 模型行（用户口径「操作列新增同步服务商模型」） */}
            <Button type="link" size="small" loading={syncingId === g.id} onClick={() => void runSync(g)}>
              同步服务商模型
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

  // ── 子表列定义（**模型**；与父表列定义完全独立，表头不共用） ──
  // ⚠️ 用户口径「父和子的表头分开，不公用」：子表只列**模型自己的属性**
  // （模型 / 类型 / 使用平台 / 启用 / 备注），不出现服务商专有列。
  // 父表列的语义反过来 = 其模型行的聚合（覆盖平台 / 模型类型是多标签）。
  const modelColumns: ColumnsType<GatewayModelNode<LlmGatewayConfig>> = [
    {
      title: "模型",
      key: "model",
      width: 320,
      render: (_v, row) => <Text>{row.model}</Text>,
    },
    {
      // v2.9：**就地改类型**（用户口径「模型类型列表中支持修改，下拉框」）。
      // AI 归纳只是初始值 ⇒ 下拉选项由后端下发（`modelTypeOptions`），改完立即 PATCH。
      title: "类型",
      key: "model_type",
      width: 150,
      render: (_v, row) => (
        <Select
          size="small"
          style={{ width: "100%" }}
          value={row.modelType || undefined}
          placeholder={row.modelType ? undefined : "-"}
          loading={typeSavingId === row.modelId}
          disabled={row.modelId === null}
          options={modelTypeOptions}
          onChange={(next) => void changeModelType(row, next as ModelType)}
        />
      ),
    },
    {
      title: "使用平台",
      key: "platform",
      width: 180,
      render: (_v, row) => {
        // v2.28：展示**引用计数**（`xhs × 2`）—— 用户口径「平台标识和使用次数」。
        // 计数为 0 的平台后端已丢弃 ⇒ 这里不会出现「xhs × 0」，也就不会误导
        // 用户以为「这个模型还在被 xhs 用」。
        const items = row.platforms;
        if (items.length === 0) return <Text type="secondary">-</Text>;
        return (
          <Space size={4} wrap>
            {items.map((p) => {
              const label = platformOptions.find((o) => o.value === p)?.label ?? p;
              const n = row.platformCounts[p] ?? 0;
              return (
                <Tag key={p} title={`${label}：${n} 个业务点正在使用`}>
                  {label}
                  {n > 1 ? ` × ${n}` : ""}
                </Tag>
              );
            })}
          </Space>
        );
      },
    },
    {
      // v2.10：**健康下沉到模型维度**（用户口径「健康状态…放具体模型维度」）。
      // ⛔ 判据必须是 `last_health_at is not None AND healthy`（与后端模型级字段同形）——
      // 只看 `healthy` 会把「从未探测过」误报成「异常」（`healthy` 默认 False），
      // 这是本项目在服务商级 `healthy` 上踩过的老坑（DEF-067 类：判活只认探测过的那次）。
      // 故三态：正常 / 未探测 / 异常。
      title: "健康",
      key: "healthy",
      width: 130,
      render: (_v, row) => {
        const probed = row.lastHealthAt !== null && row.lastHealthAt !== undefined;
        if (!probed) {
          // v2.10（用户口径「子表健康列未检查的展示检测检查按钮，触发可健康检查」）：
          // 未探测 ⇒ **给一个「检查」按钮**（而不是只显示一个「未探测」死标签）——
          // 用户看到空状态时最自然的动作就是「点一下试试」，让入口就在那里。
          return (
            <Tooltip title="尚未探测连通性">
              <Button
                type="link"
                size="small"
                style={{ padding: 0 }}
                loading={checkingModelId === row.modelId}
                disabled={row.modelId === null}
                onClick={() => void runModelCheck(row)}
              >
                检查
              </Button>
            </Tooltip>
          );
        }
        return (
          <Tooltip title={`最近探测 ${row.lastHealthAt}`}>
            <Tag color={row.healthy ? "green" : "red"}>{row.healthy ? "正常" : "异常"}</Tag>
          </Tooltip>
        );
      },
    },
    {
      // v2.10：**启用下沉到模型维度**（用户口径「启用状态开关放具体模型维度」）。
      // 与父表原来的服务商级开关同语义，但作用域收窄到「这一个模型」：
      // 停用 ⇒ 业务按模型名取时取不到（父服务商仍可用，只是少了这个模型）。
      title: "启用",
      key: "enabled",
      width: 120,
      render: (_v, row) => {
        // ⚠️ v2.10（用户 2026-10-02 口径「**已在使用的模型无法停用**」）：
        // 有业务点映射指向它 ⇒ 开关**置灰**（不管当前是开还是关）——
        // 因为「停用」这条路被业务引用堵死了，开关留着可点只会误导。
        // ⚠️ 前端置灰只是体验，**判据在后端**（PATCH 会回 400 `LLM_MODEL_IN_USE_BY_BUSINESS`）
        // —— 前端状态可能过期（别人在另一个标签页改了映射），靠前端拦会漏。
        const inUse = row.usages.length > 0;
        if (inUse) {
          return (
            <Tooltip title={`正被 ${row.usages.join("、")} 使用，请先在模型配置页改到别的模型`}>
              <Switch size="small" checked={row.enabled} disabled />
            </Tooltip>
          );
        }
        return (
          <Tooltip title={row.enabled ? "点击停用（业务将取不到该模型）" : "点击启用"}>
            <Switch
              size="small"
              checked={row.enabled}
              loading={togglingModelId === row.modelId}
              disabled={row.modelId === null}
              onChange={(next) => void toggleModelEnabled(row, next)}
            />
          </Tooltip>
        );
      },
    },
    {
      title: "备注",
      key: "remark",
      ellipsis: true,
      render: (_v, row) =>
        row.remark ? <Text type="secondary">{row.remark}</Text> : <Text type="secondary">-</Text>,
    },
  ];


  return (
    <GatewaySection>
      <Card
        title="服务商"
        extra={
          <Space>
            <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
            <Button type="primary" size="small" onClick={openCreate}>
              新增服务商
            </Button>
          </Space>
        }
      >
        {/* 父表 = **服务商**（表头见 `providerColumns`）。展开行里是子表 = **模型**。 */}
        {/* 2026-10-20 统一：表格等待由公共 WaitMask 表达。 */}
        <WaitMask open={loading} mask={false} title="正在加载" />
        <Table<GatewayProviderNode<LlmGatewayConfig>>
          rowKey="key"
          size="small"
          //: ⚠️ 2026-10-20 统一：Table 自带的 loading 会渲染一层 antd Spin 覆盖，与公共 WaitMask 叠成两层 ⇒ 移除，等待交给 WaitMask。
          //: 等待提示由同状态驱动的 WaitMask 承担（见函数返回处）。
          columns={providerColumns}
          dataSource={providerRows}
          pagination={false}
          // ⛔ **白屏修复的关键一行（2026-10-02）**：把 antd 的「树数据自动探测」关掉。
          // `children` 是 antd Table 树形模式的**内置字段名**，而我们 `buildGatewayTree`
          // 产出的服务商行恰好带 `children` ⇒ antd 会同时跑内置树渲染 + `expandedRowRender`，
          // 两条路径冲突 ⇒ 点开箭头整页白屏。
          // 指向一个数据里不存在的 key（`__no_tree__`）⇒ antd 认不出子节点，放弃树模式，
          // 只走 `expandedRowRender`（`row.children` 仍在数据里，展开区照样取得到）。
          childrenColumnName="__no_tree__"
          // ⚠️ **2026-10-02 用户口径「父子表的表头独立设置」**：父子不再共用一张表 ——
          // 父表每行展开时渲染**一张独立的子表**（自带表头），而不是把子行塞进父表行里
          // （那样子行必须沿用父表的列，服务商专有列只显示 `-`，表头也是服务商语境的）。
          // 展开箭头只给含模型的父行（`rowExpandable`），空目录不出现「点了没反应」的箭头。
          expandable={{
            defaultExpandAllRows: true,
            rowExpandable: isExpandableProvider,
            expandedRowRender: (row) => (
              <Table<GatewayModelNode<LlmGatewayConfig>>
                rowKey="key"
                size="small"
                columns={modelColumns}
                // `row.children` 的静态类型是父/子联合 —— 这里按 kind 收窄成纯模型节点
                // （`buildGatewayTree` 保证第二层只可能是 model，收窄不是「猜」）。
                dataSource={row.children.filter(
                  (c): c is GatewayModelNode<LlmGatewayConfig> => c.kind === "model"
                )}
                pagination={false}
                // 子表不再有「展开」概念（模型下没有第三层）
                showHeader
              />
            ),
          }}
          locale={{ emptyText: "还没有服务商 —— 点右上「新增服务商」添加第一个" }}
        />
      </Card>

      <Modal
                centered
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
          {/* ⚠️ v2.7/v2.8：服务商级的「使用平台」/「模型类型」/「默认模型」三件**全部移除**
              （用户口径「直接移除输入框与列」）——前两者下沉到每条模型行，
              后者整列删除。这里改为「模型行列表」编辑器：一行一个模型，
              各自带 名称 + 类型 + 平台 + 启用，可增删。 */}
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
          {/* ⚠️ **v2.9：模型行编辑器整段移除**（用户口径「服务商模型不允许手动添加，
              只能拉取三方」「配置服务商时应直接父子树展示该服务商下所有模型，
              不需要再进入编辑后添加模型」）。
              模型的来源与展示改为：
                ① 新增服务商后，**后端自动同步一次**（拉目录 + AI 归纳类型）；
                ② 之后在列表操作列点「同步服务商模型」重拉；
                ③ 模型在**主页面的父子树**里按服务商展开查看，类型可在树上就地改。
              故弹窗只留服务商本体的三个字段（名称 / 地址 / 令牌）。 */}
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

  /**
   * 模型下拉候选 = 所选服务商的**已配置模型行**（v2.7/v2.8）。
   *
   * ⚠️ 老实现读 `gw.models` 并将其视为 `string[]`；现在 `models` 是结构数组，
   * 候选池（上游 `/models` 拉的字符串数组）在同级的 `catalog` 里。
   * 预警规则的作用域模型**该是已配置的模型行**（台账里记的就是这些名字），
   * 故这里读 `models[*].model_name`，**不读 catalog** —— 未配置的候选无从产出用量。
   */
  const modelOptions = useMemo(() => {
    if (formGatewayId === null) return [];
    const gw = gateways.find((g) => g.id === formGatewayId);
    const models = (gw?.models ?? []).map((m) => m.model_name).filter(Boolean);
    return models.map((m) => ({ value: m, label: m }));
  }, [gateways, formGatewayId]);

  return (
    <GatewaySection>
      <Card
        title="预警规则"
        extra={
          <Space>
            <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
            <Button type="primary" size="small" onClick={openCreate}>
              新增预警规则
            </Button>
          </Space>
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
          //: ⚠️ 2026-10-20 统一：Table 自带的 loading 会渲染一层 antd Spin 覆盖，与公共 WaitMask 叠成两层 ⇒ 移除，等待交给 WaitMask。
            columns={columns}
            dataSource={alerts}
            pagination={false}
            locale={{ emptyText: "还没有预警规则 —— 配置后由定时任务「LLM 网关预警」轮询触发站内通知" }}
          />
        )}
      </Card>

      <Modal
                centered
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
