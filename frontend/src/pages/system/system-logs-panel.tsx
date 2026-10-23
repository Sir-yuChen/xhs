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
  type SystemAuditLog,
} from "../../lib/api";

const { Text } = Typography;

export function SystemLogsPanel() {
  return (
    <Card size="small">
      <Tabs
        items={[
          { key: "audit", label: "审计日志（API）", children: <AuditLogTab /> },
          { key: "app", label: "应用日志（文件）", children: <AppLogTab /> },
        ]}
      />
    </Card>
  );
}

function AuditLogTab() {
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
  ];

  return (
    <>
      <Space style={{ marginBottom: 12 }} wrap>
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

      <Table<SystemAuditLog>
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        scroll={{ x: 900 }}
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
    </>
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

  return (
    <>
      <Space style={{ marginBottom: 12 }} wrap>
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
            maxHeight: 560,
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

      <Space style={{ marginTop: 12 }}>
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
    </>
  );
}
