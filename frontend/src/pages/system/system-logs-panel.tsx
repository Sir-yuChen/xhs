/*
 * 日志查看页（v1.1.0 / 设计 55 §7 / 49 模块九）。
 *
 * ## 两类日志，别混
 *
 * | 类型 | 来源 | 特征 |
 * |---|---|---|
 * | **应用日志** | `文件`（`.hermes/logs/*.log`） | 运行时报错、堆栈，**尾部 2000 行** |
 * | **审计日志** | `api_logs` 表（DEF-019 中间件写入） | 谁调了哪个写接口、结果码 |
 *
 * 本项目**没有 `app_logs` 表**，应用日志走文件 —— 这是既有事实，页面如实标注
 * 「来自文件日志的尾部，不是数据库」，不假装是数据库查询。
 *
 * ## 审计日志不含 query / body
 *
 * DEF-019 的中间件**刻意不落**这两项（可能含密码 / 令牌）。
 * 详情弹窗里必须写明这一点，否则使用者会以为「日志坏了，内容丢了」。
 */

import { ReloadOutlined } from "@ant-design/icons";
import { App, Button, Card, Input, Select, Space, Table, Tabs, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";

import {
  fetchSystemAppLogs,
  fetchSystemAuditLogs,
  fetchSystemBusinessLogs,
  type SystemAuditLog,
  type SystemBusinessLog,
} from "../../lib/api";
import { TraceTab } from "./system-logs-trace-tab";

const { Text } = Typography;

export function SystemLogsPanel() {
  // 设计 63 §6（2026-10-25 用户截图「应用日志页底部空白」）：Card/Tabs 进撑满
  // 高度链（`.fill-tabs`），两个 Tab 内容各自三区布局 —— 日志区内部滚动、
  // 翻页/分页钉在卡底，底部不再留白。
  //
  // 设计 67：点列表里的「链路」列 ⇒ 切到「链路追踪」页签并自动查该链路
  //（用户不必手抄 ID —— 那是拿不到 ID 的主因）。
  const [activeTab, setActiveTab] = useState("audit");
  const [traceId, setTraceId] = useState("");

  const openTrace = useCallback((id: string) => {
    setTraceId(id);
    setActiveTab("trace");
  }, []);

  return (
    <div style={{ height: "calc(100vh - 87px)", display: "flex", flexDirection: "column" }}>
      <Card
        size="small"
        style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
        styles={{ body: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" } }}
      >
        <Tabs
          className="fill-tabs"
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            { key: "audit", label: "审计日志（API）", children: <AuditLogTab onOpenTrace={openTrace} /> },
            { key: "business", label: "业务日志", children: <BusinessLogTab onOpenTrace={openTrace} /> },
            {
              key: "trace",
              label: "链路追踪",
              // destoryInactive 未开 ⇒ 切走再切回不丢查询结果（用户从列表跳过来看的）。
              children: <TraceTab traceId={traceId} />,
            },
            { key: "app", label: "应用日志（文件）", children: <AppLogTab /> },
          ]}
        />
      </Card>
    </div>
  );
}

function AuditLogTab({ onOpenTrace }: { onOpenTrace: (id: string) => void }) {
  const { message } = App.useApp();
  const [rows, setRows] = useState<SystemAuditLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const [draft, setDraft] = useState<{ endpoint?: string; status_code?: number }>({});
  const [filter, setFilter] = useState<{ endpoint?: string; status_code?: number }>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchSystemAuditLogs({ page, page_size: pageSize, ...filter });
      setRows(res.items);
      setTotal(res.total);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载审计日志失败");
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, filter, message]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: ColumnsType<SystemAuditLog> = [
    { title: "ID", dataIndex: "id", width: 80 },
    { title: "用户", dataIndex: "user_id", width: 80, render: (v: number | null) => v ?? "-" },
    {
      title: "接口",
      dataIndex: "endpoint",
      render: (v: string) => <Text style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: "状态码",
      dataIndex: "status_code",
      width: 90,
      render: (v: number | null) => {
        if (v === null) return <Text type="secondary">-</Text>;
        const color = v >= 500 ? "red" : v >= 400 ? "orange" : "green";
        return <Tag color={color}>{v}</Tag>;
      },
    },
    {
      title: "结果",
      dataIndex: "status",
      width: 90,
      render: (v: string | null) => v || "-",
    },
    {
      title: "时间",
      dataIndex: "created_at",
      width: 180,
      render: (v: string | null) => (v ? v.replace("T", " ").slice(0, 19) : "-"),
    },
    {
      title: "链路",
      dataIndex: "trace_id",
      width: 110,
      render: (v?: string | null) =>
        v ? (
          <Typography.Link
            style={{ fontSize: 12, fontFamily: "monospace" }}
            onClick={() => onOpenTrace(v)}
            title={v}
          >
            {v.slice(0, 8)}
          </Typography.Link>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
  ];

  // 设计 63 §6：三区布局（同用户管理）—— 筛选固定，表格体内部滚动，分页钉底。
  // 根用 height:100%（TabPane 由 `.fill-tabs` 给了确定高度，不再自算视口）。
  return (
    <div style={{ height: "100%", minHeight: 0, display: "flex", flexDirection: "column" }}>
      <Space style={{ marginBottom: 12, flexShrink: 0 }} wrap>
        <Input
          placeholder="接口路径（模糊，如 /api/xhs/items）"
          allowClear
          style={{ width: 280 }}
          value={draft.endpoint ?? ""}
          onChange={(e) => setDraft((d) => ({ ...d, endpoint: e.target.value }))}
        />
        <Select
          placeholder="状态码档位"
          allowClear
          style={{ width: 150 }}
          value={draft.status_code}
          onChange={(v) => setDraft((d) => ({ ...d, status_code: v }))}
          options={[
            { value: 200, label: "200 成功" },
            { value: 400, label: "400 请求错" },
            { value: 401, label: "401 未登录" },
            { value: 403, label: "403 无权限" },
            { value: 500, label: "500 服务端错" },
          ]}
        />
        <Button
          type="primary"
          onClick={() => {
            setFilter(draft);
            setPage(1);
          }}
        >
          查询
        </Button>
        <Button
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() => void load()}
        >
          刷新
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          只记录写操作与失败请求；query / body 不落库（可能含敏感字段）
        </Text>
      </Space>

      <div className="fill-table">
      <Table<SystemAuditLog>
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        scroll={{ x: 900, y: "100%" }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          showTotal: (n) => `共 ${n} 条`,
          onChange: (p, ps) => {
            setPage(p);
            setPageSize(ps);
          },
        }}
      />
      </div>
    </div>
  );
}

/**
 * 业务日志 Tab（设计 64 §2.2）：`business_logs` 表 —— 核心业务节点的语义审计层。
 * 与「审计日志（API）」分工：那边记端点面（谁调了哪个端点），
 * 这边记业务面（登录/登出、系统管理增删改、配置变更、队列终态）。
 */
function BusinessLogTab({ onOpenTrace }: { onOpenTrace: (id: string) => void }) {
  const [rows, setRows] = useState<SystemBusinessLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [event, setEvent] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchSystemBusinessLogs({
        page,
        page_size: pageSize,
        event: event.trim() || undefined,
      });
      setRows(res.items);
      setTotal(res.total);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, event]);

  useEffect(() => {
    void load();
  }, [load]);

  const levelColor = (level: string) =>
    level === "warning" ? "orange" : level === "error" ? "red" : "blue";

  const columns: ColumnsType<SystemBusinessLog> = [
    { title: "ID", dataIndex: "id", width: 80 },
    {
      title: "时间",
      dataIndex: "created_at",
      width: 170,
      render: (v: string | null) => (v ? v.replace("T", " ").slice(0, 19) : "-"),
    },
    {
      title: "级别",
      dataIndex: "level",
      width: 80,
      render: (v: string) => <Tag color={levelColor(v)}>{v}</Tag>,
    },
    { title: "事件", dataIndex: "event", width: 180 },
    { title: "动作", dataIndex: "action", width: 90 },
    { title: "操作人", dataIndex: "actor_name", width: 120 },
    { title: "对象", dataIndex: "target_id", width: 220, ellipsis: true },
    { title: "摘要", dataIndex: "summary", ellipsis: true },
    {
      title: "链路",
      dataIndex: "trace_id",
      width: 110,
      render: (v?: string | null) =>
        v ? (
          <Typography.Link
            style={{ fontSize: 12, fontFamily: "monospace" }}
            onClick={() => onOpenTrace(v)}
            title={v}
          >
            {v.slice(0, 8)}
          </Typography.Link>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
  ];

  return (
    <div style={{ height: "100%", minHeight: 0, display: "flex", flexDirection: "column" }}>
      <Space style={{ marginBottom: 12, flexShrink: 0 }} wrap>
        <Input
          placeholder="事件（模糊，如 auth. / queue. / settings.）"
          allowClear
          style={{ width: 260 }}
          value={event}
          onChange={(e) => {
            setEvent(e.target.value);
            setPage(1);
          }}
        />
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
          刷新
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          认证出入口 / 系统管理增删改 / 配置变更 / 队列终态；保留 90 天
        </Text>
      </Space>
      <div className="fill-table">
        <Table<SystemBusinessLog>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={rows}
          scroll={{ x: 1100, y: "100%" }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (n) => `共 ${n} 条`,
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
        />
      </div>
    </div>
  );
}

function AppLogTab() {
  const { message } = App.useApp();
  const [lines, setLines] = useState<{ line_no: number; text: string }[]>([]);
  const [meta, setMeta] = useState<{ total: number; file?: string; note?: string }>({
    total: 0,
  });
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const pageSize = 200;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchSystemAppLogs({ page, page_size: pageSize });
      setLines(res.items);
      setMeta({ total: res.total, file: res.file, note: res.note });
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载应用日志失败");
    } finally {
      setLoading(false);
    }
  }, [page, message]);

  useEffect(() => {
    void load();
  }, [load]);

  // 设计 63 §6：三区布局 —— 筛选行固定，日志区 flex:1 内部滚动（去掉旧
  // `maxHeight:560` 固定帽 ⇒ 底部空白），翻页行钉在卡底。
  return (
    <div style={{ height: "100%", minHeight: 0, display: "flex", flexDirection: "column" }}>
      <Space style={{ marginBottom: 12, flexShrink: 0 }} wrap>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
          刷新
        </Button>
        {meta.file && <Tag>文件：{meta.file}</Tag>}
        {meta.note && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {meta.note}
          </Text>
        )}
      </Space>

      {lines.length === 0 ? (
        <Text type="secondary">{loading ? "加载中…" : "没有日志行"}</Text>
      ) : (
        <div
          style={{
            fontFamily: "Consolas, Menlo, monospace",
            fontSize: 12,
            lineHeight: 1.6,
            flex: 1,
            minHeight: 0,
            //: ⚠️ `overflow:auto` 是这格的**生命线**（2026-10-25 用户截图实测：
            //   只给 `flex:1` 不给滚动 ⇒ 文本长出容器、**透明地盖在翻页按钮上**
            //   把「较早/较新」顶穿）。flex 子项要滚，overflow 必须亲自给。
            overflow: "auto",
            padding: 8,
            borderRadius: 4,
            border: "1px solid var(--ant-color-border)",
            whiteSpace: "pre-wrap",
            wordBreak: "break-all",
          }}
        >
          {lines.map((line) => (
            <div key={line.line_no}>
              <Text type="secondary" style={{ marginRight: 8, userSelect: "none" }}>
                {String(line.line_no).padStart(5, " ")}
              </Text>
              {line.text}
            </div>
          ))}
        </div>
      )}

      <Space style={{ marginTop: 12, flexShrink: 0 }}>
        <Button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
          较早
        </Button>
        <Text type="secondary">
          第 {page} 页 / 共 {meta.total} 行
        </Text>
        <Button disabled={page * pageSize >= meta.total} onClick={() => setPage((p) => p + 1)}>
          较新
        </Button>
      </Space>
    </div>
  );
}
