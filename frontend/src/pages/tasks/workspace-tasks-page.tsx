/**
 * 工作区 → 任务（`/platforms/xhs/tasks`，设计 69 §4.3 补充）。
 *
 * ## 为什么要有这一页（2026-10-25 用户口径）
 *
 * 「任务中心分为两级，系统级和工作区」「工作区域为每个区域中私有」。
 *
 * 两级各有**自己的入口** —— 只在系统管理里放一个带「小红书任务」页签的页面，
 * 用户在小红书工作区里就找不到「我的任务在哪」。这与配置三级的做法同源：
 * **归属哪一级，就在哪一级的页面出现**。
 *
 * ## 与系统管理任务中心的区别（同一套组件，不同参数）
 *
 * | 项 | 系统管理 → 任务中心 | 本页 |
 * |---|---|---|
 * | 看什么 | 系统任务 + 工作区任务 + 全部执行日志 | **只本工作区**的任务与日志 |
 * | 谁能进 | 仅 admin（`system:scheduler:view`）| 有本工作区权限即可（`xhs:taskcenter:view`）|
 * | 能改吗 | admin 能改 Cron / 暂停 / 立即执行 | **只读**（用户口径「暂时不允许用户创建任务」）|
 *
 * ⚠️ 「只读」不是靠前端不给按钮 —— 写端点全部挂 `system:scheduler:manage`，
 * 普通用户即使构造请求也拿不到 200。前端不渲染只是体验层。
 *
 * ## 复用
 *
 * 列定义与日志表格都用公共组件（`task-columns` / `job-log-table`），
 * 本页只传参数（`workspace="xhs"`、`readOnly`）。
 */

import { useCallback, useEffect, useState } from "react";
import { QuestionCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Space, Table, Tabs, Tooltip, Typography } from "antd";

import { fetchScheduleJobs, fetchScheduleLogs } from "../../lib/api";
import type { ScheduledJob, ScheduledJobLog } from "../../types";
import { buildTaskColumns, workspaceLabel } from "../../components/schedule/task-columns";
import { JobLogTable } from "../../components/schedule/job-log-table";
import { JobLogDetailModal } from "../../components/schedule/job-log-detail-modal";

const { Text } = Typography;

const WORKSPACE = "xhs";

/** 本工作区的任务定义（只读）。 */
function WorkspaceJobs() {
  const [rows, setRows] = useState<ScheduledJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      //: 端点已支持 `workspace` 过滤（设计 69 §4.3）—— 服务端筛，
      //: 不把别的工作区的任务传到前端再丢弃。
      const res = await fetchScheduleJobs({ workspace: WORKSPACE, page: 1, page_size: 200 });
      setRows(res.items ?? []);
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

  const columns = buildTaskColumns({ showTriggerDetail: true });

  if (error) {
    return <Text type="danger">{error}</Text>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: 8 }}>
      <Space>
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {rows.length} 个任务
        </Text>
      </Space>
      <div style={{ flex: 1, minHeight: 0 }} className="fill-table">
        <Table<ScheduledJob>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 1400, y: "100%" }}
          pagination={false}
        />
      </div>
    </div>
  );
}

/** 本工作区的执行日志（只读）。 */
function WorkspaceLogs({ onOpen }: { onOpen: (log: ScheduledJobLog) => void }) {
  const [rows, setRows] = useState<ScheduledJobLog[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [total, setTotal] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchScheduleLogs({ page: 1, page_size: 100 });
      setRows(res.items ?? []);
      setTotal(res.total ?? 0);
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

  if (error) {
    return <Text type="danger">{error}</Text>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: 8 }}>
      <Space>
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          共 {total} 条执行记录
        </Text>
      </Space>
      <div style={{ flex: 1, minHeight: 0 }}>
        <JobLogTable
          data={rows}
          loading={loading}
          showJobKey
          showProgress
          scrollY="100%"
          onRowClick={onOpen}
        />
      </div>
    </div>
  );
}

export function WorkspaceTasksPage() {
  const [logOpen, setLogOpen] = useState<ScheduledJobLog | null>(null);

  return (
    <div style={{ height: "calc(100vh - 87px)", display: "flex", flexDirection: "column" }}>
      <Card
        size="small"
        style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
        styles={{
          body: {
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
          },
        }}
        title={
          <span>
            {workspaceLabel(WORKSPACE)}任务
            <Tooltip title="本工作区的定时任务由管理员统一配置，此处只读。">
              <QuestionCircleOutlined style={{ marginLeft: 6, color: "rgba(0,0,0,0.45)" }} />
            </Tooltip>
          </span>
        }
      >
        <Tabs
          className="fill-tabs"
          items={[
            {
              key: "jobs",
              label: "任务",
              children: <WorkspaceJobs />,
            },
            {
              key: "logs",
              label: "执行日志",
              children: <WorkspaceLogs onOpen={setLogOpen} />,
            },
          ]}
        />
      </Card>

      <JobLogDetailModal
        open={logOpen !== null}
        log={logOpen}
        onCancel={() => setLogOpen(null)}
      />
    </div>
  );
}
