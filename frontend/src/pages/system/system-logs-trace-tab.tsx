import { useCallback, useEffect, useState } from "react";
// 2026-10-26：静态导入的 message 不吃 ConfigProvider 主题（antd 官方不推荐），
// 统一改用 App.useApp() 的实例（本仓纪律：所有消息提醒走 App.useApp）。
import { Alert, App, Button, Empty, Input, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { SearchOutlined } from "@ant-design/icons";
import { fetchTraceTimeline, type TraceEvent } from "../../lib/api";

const { Text } = Typography;

const SOURCE_LABEL: Record<string, string> = {
  api: "端点",
  business: "业务",
  job: "任务",
};

const SOURCE_COLOR: Record<string, string> = {
  api: "blue",
  business: "green",
  job: "purple",
};

const LEVEL_COLOR: Record<string, string> = {
  info: "default",
  warning: "orange",
  error: "red",
};

/** 按链路 id 查该链路的全部日志（设计 67）。 */
export function TraceTab({ traceId: incoming = "" }: { traceId?: string }) {
  const { message } = App.useApp();
  const [input, setInput] = useState(incoming);
  const [traceId, setTraceId] = useState("");
  const [loading, setLoading] = useState(false);
  const [found, setFound] = useState<boolean | null>(null);
  const [rows, setRows] = useState<TraceEvent[]>([]);
  const [bySource, setBySource] = useState<Record<string, number>>({});

  const search = useCallback(async (id: string) => {
    const clean = id.trim();
    if (!clean) {
      message.warning("请输入链路 ID");
      return;
    }
    setLoading(true);
    try {
      const data = await fetchTraceTimeline(clean);
      setTraceId(clean);
      setRows(data.events ?? []);
      setBySource(data.by_source ?? {});
      setFound(data.found);
    } catch {
      message.error("查询失败");
    } finally {
      setLoading(false);
    }
  }, []);

  // 从日志列表点「链路」跳过来时自动查（用户不必再按一次查询）。
  useEffect(() => {
    if (!incoming) return;
    setInput(incoming);
    void search(incoming);
  }, [incoming, search]);

  const columns: ColumnsType<TraceEvent> = [
    {
      title: "时间",
      dataIndex: "at",
      width: 170,
      render: (v: string | null) => (v ? v.replace("T", " ").slice(0, 19) : "-"),
    },
    {
      title: "来源",
      dataIndex: "source",
      width: 80,
      render: (v: string) => (
        <Tag color={SOURCE_COLOR[v] ?? "default"}>{SOURCE_LABEL[v] ?? v}</Tag>
      ),
    },
    {
      title: "操作人",
      dataIndex: "actor_name",
      width: 130,
      render: (v: string, r) => v || (r.actor_id ? `#${r.actor_id}` : "-"),
    },
    {
      title: "级别",
      dataIndex: "level",
      width: 80,
      render: (v: string) => <Tag color={LEVEL_COLOR[v] ?? "default"}>{v}</Tag>,
    },
    { title: "内容", dataIndex: "title", ellipsis: true },
    {
      title: "结果",
      dataIndex: "summary",
      width: 260,
      ellipsis: true,
      render: (v: string) => <Text type="secondary">{v || "-"}</Text>,
    },
  ];

  const summary = traceId
    ? Object.entries(bySource)
        .map(([k, n]) => `${SOURCE_LABEL[k] ?? k} ${n}`)
        .join(" · ")
    : "";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: 12 }}>
      <Space.Compact style={{ width: 560, maxWidth: "100%" }}>
        <Input
          placeholder="粘贴链路 ID（响应头 X-Trace-Id）"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onPressEnter={() => search(input)}
          allowClear
        />
        <Button
          type="primary"
          icon={<SearchOutlined />}
          loading={loading}
          onClick={() => search(input)}
        >
          查询
        </Button>
      </Space.Compact>

      {found === false && (
        <Alert
          type="warning"
          showIcon
          message="该链路没有日志"
          description="确认 ID 是否完整（32 位），或该操作是更早之前发生的（迁移前的记录没有链路 ID）。"
        />
      )}

      {found && summary && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          共 {rows.length} 条 · {summary}
        </Text>
      )}

      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
        <Table<TraceEvent>
          rowKey={(r, i) => `${r.source}-${r.at}-${i}`}
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={false}
          scroll={{ x: 900, y: "100%" }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={traceId ? "无记录" : "输入链路 ID 查询"}
              />
            ),
          }}
        />
      </div>
    </div>
  );
}
