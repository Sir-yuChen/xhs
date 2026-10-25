/**
 * 系统管理 → 定时任务（`/system/scheduler`，设计 64 §3.3）。
 *
 * 决策：调度器是**公共单例**（不按工作区拆分 —— 企业级对照与理由见设计 64 §3.1），
 * 行级 `user_id` 隔离「我的任务中心」（`/tasks`，不变）；本页是 **admin 的跨工作区
 * 总览**：按工作区聚合、看到归属人与运行计数。普通用户进来会看到后端 403 的
 * 安静说明（与用户管理等系统页同款"页面壳可见、数据按权限"模式）。
 */

import { useCallback, useEffect, useState } from "react";
import { Card, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

import { fetchScheduleOverview, type ScheduleOverviewItem } from "../../lib/api";

const { Text, Paragraph } = Typography;

const WORKSPACE_LABELS: Record<string, string> = {
  system: "系统",
  xhs: "小红书",
};

export function SystemSchedulerPanel() {
  const [rows, setRows] = useState<ScheduleOverviewItem[]>([]);
  const [byWorkspace, setByWorkspace] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchScheduleOverview();
      setRows(res.items);
      setByWorkspace(res.by_workspace ?? {});
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: ColumnsType<ScheduleOverviewItem> = [
    { title: "ID", dataIndex: "id", width: 70 },
    {
      title: "工作区",
      dataIndex: "workspace",
      width: 100,
      render: (v: string | null) => (
        <Tag color={v === "system" ? "purple" : "blue"}>
          {WORKSPACE_LABELS[v ?? ""] ?? v ?? "-"}
        </Tag>
      ),
    },
    { title: "任务", dataIndex: "name", width: 180 },
    {
      title: "归属人",
      dataIndex: "owner_name",
      width: 120,
      render: (v: string) => <Text>{v || "-"}</Text>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (v: string) => (
        <Tag color={v === "active" ? "green" : v === "paused" ? "default" : "blue"}>{v}</Tag>
      ),
    },
    {
      title: "触发",
      dataIndex: "trigger_type",
      width: 80,
      render: (v: string) => (v === "cron" ? "Cron" : v === "at" ? "定时" : "手动"),
    },
    {
      title: "Cron",
      dataIndex: "cron_expression",
      width: 140,
      render: (v: string) => <Text code style={{ fontSize: 12 }}>{v || "-"}</Text>,
    },
    {
      title: "上次运行",
      dataIndex: "last_run_at",
      width: 160,
      render: (v: string | null) => (v ? v.replace("T", " ").slice(0, 19) : "-"),
    },
    {
      title: "下次运行",
      dataIndex: "next_run_at",
      width: 160,
      render: (v: string | null) => (v ? v.replace("T", " ").slice(0, 19) : "-"),
    },
    { title: "运行", dataIndex: "run_count", width: 70, align: "right" },
    {
      title: "失败",
      dataIndex: "fail_count",
      width: 70,
      align: "right",
      render: (v: number) => (
        <Text type={v > 0 ? "danger" : undefined}>{v}</Text>
      ),
    },
  ];

  return (
    <Card title="定时任务（全站总览）">
      <Paragraph type="secondary" style={{ marginBottom: 12 }}>
        调度器是**全站公共单例**（企业级取舍见设计 64 §3：拆分每工作区独立调度器
        会带来多实例抢行与看门狗复杂化，无收益）；本页按**工作区归属**聚合全部任务。
        各工作区自己的任务入口仍是「任务中心」（按本人隔离）。
      </Paragraph>

      {error ? (
        <Text type="danger" style={{ display: "block", marginBottom: 12 }}>
          {error}（总览只有超级管理员可以查看）
        </Text>
      ) : (
        <>
          <div style={{ marginBottom: 12 }}>
            {Object.entries(byWorkspace).map(([ws, count]) => (
              <Tag key={ws}>
                {WORKSPACE_LABELS[ws] ?? ws}：{count} 个任务
              </Tag>
            ))}
          </div>
          <div className="fill-table">
            <Table<ScheduleOverviewItem>
              rowKey="id"
              size="small"
              loading={loading}
              columns={columns}
              dataSource={rows}
              scroll={{ x: 1200, y: "100%" }}
              pagination={false}
            />
          </div>
        </>
      )}
    </Card>
  );
}
