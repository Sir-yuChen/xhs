/**
 * 系统管理 → 任务中心（`/system/scheduler`，设计 69 §4.3）。
 *
 * ## 两级归属（2026-10-25 用户口径）
 *
 * 「任务中心分为系统级别和工作区域级别，工作区域为每个区域中私有」
 * 「暂时不允许用户创建任务」「收为公共」。
 *
 * | 页签 | 内容 |
 * |---|---|
 * | **系统任务** | `workspace='system'` —— 清理本系统自己的资源（记录表 / 暂存目录 / 未引用图片），与接哪个平台无关 |
 * | **工作区任务** | `workspace='xhs'` —— 作用于小红书数据的任务；**工作区内只有一份定义**，不是"每个用户一份" |
 * | **执行日志** | 所有执行记录；fan-out 后一次触发会产生**每个目标用户一行** |
 *
 * ## 与旧版的三处不同
 *
 * 1. **不再是「全站总览」一张表**：旧版把 31 用户 × 13 任务 = 403 行平铺，
 *    同一个「清理记录表」重复 31 次，看不出任务内容。现在每种任务**一行**。
 * 2. **列定义来自公共工厂** `buildTaskColumns`（用户口径「组件可以共用的提取成
 *    公共组件，不同参数和页面调用展示对应内容」）—— 本页只传参数。
 * 3. **加了执行日志页签**：旧版只有任务定义，用户说的"任务中心只有任务日志"
 *    反过来也成立过 —— 两侧都必须能看。
 *
 * ## 调度器是公共单例（设计 64 §3.1 的决策不变）
 *
 * 不按工作区拆成多个调度器进程：拆分会带来多实例抢行与看门狗复杂化，
 * 而归属只影响"这个任务服务于谁"，不影响"谁来定时"。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { QuestionCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Space, Table, Tabs, Tag, Tooltip, Typography } from "antd";

import { fetchScheduleJobs, fetchScheduleLogs } from "../../lib/api";
import type { ScheduledJob, ScheduledJobLog } from "../../types";
import { buildTaskColumns, workspaceLabel } from "../../components/schedule/task-columns";
import { JobLogTable } from "../../components/schedule/job-log-table";
import { JobLogDetailModal } from "../../components/schedule/job-log-detail-modal";

const { Text } = Typography;

/** 任务定义表（一个页签一张，按 `workspace` 过滤）。 */
function JobTable({
  workspace,
  onOpenLog,
}: {
  workspace: string;
  onOpenLog: (log: ScheduledJobLog) => void;
}) {
  const [rows, setRows] = useState<ScheduledJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      //: ⚠️ `ScheduleJobQuery` 目前没有 `workspace` 参数。设计 69 后任务总量
      //: 只有「注册表条数」级（十几个），一次取全量在前端按 workspace 分组
      //: 比给端点加一个参数更简单、也不会两处判据漂移。
      //: （将来任务数上百时再给后端加 `workspace` 过滤，此处改一行。）
      const res = await fetchScheduleJobs({ page: 1, page_size: 200 });
      setRows((res.items ?? []).filter((job) => (job.workspace ?? "") === workspace));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [workspace]);

  useEffect(() => {
    void load();
  }, [load]);

  //: 列由公共工厂产出，本页只传参数（不在这里复写列的渲染逻辑）。
  const columns = useMemo(() => buildTaskColumns({ showWorkspace: false, showTriggerDetail: true }), []);

  if (error) {
    return (
      <Text type="danger">
        {error}（任务中心只有超级管理员可以查看）
      </Text>
    );
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
        <>

          <JobTableBody columns={columns} rows={rows} loading={loading} />
        </>
      </div>
    </div>
  );
}

/** 表格本体（拆出来只是为了让上面的分支更短）。 */
function JobTableBody({
  columns,
  rows,
  loading,
}: {
  columns: ReturnType<typeof buildTaskColumns>;
  rows: ScheduledJob[];
  loading: boolean;
}) {
  return (
    <Table<ScheduledJob>
      rowKey="id"
      size="small"
      loading={loading}
      columns={columns}
      dataSource={rows}
      scroll={{ x: 1400, y: "100%" }}
      pagination={false}
    />
  );
}

/** 执行日志页签。 */
function LogTab({ onOpenLog }: { onOpenLog: (log: ScheduledJobLog) => void }) {
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
          showExecutedFor
          showWorkspace
          showJobKey
          showProgress
          scrollY="100%"
          onRowClick={onOpenLog}
        />
      </div>
    </div>
  );
}

export function SystemSchedulerPanel() {
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
            任务中心
            <Tooltip title="全站共用一套调度器。系统任务清理本系统自己的资源；工作区任务只作用于该工作区的数据。">
              <QuestionCircleOutlined style={{ marginLeft: 6, color: "rgba(0,0,0,0.45)" }} />
            </Tooltip>
            <Tag color="purple" style={{ marginLeft: 8, fontSize: 11 }}>
              仅管理员
            </Tag>
          </span>
        }
      >
        <Tabs
          className="fill-tabs"
          items={[
            {
              key: "system",
              label: `系统任务`,
              children: <JobTable workspace="system" onOpenLog={setLogOpen} />,
            },
            {
              key: "xhs",
              label: `${workspaceLabel("xhs")}任务`,
              children: <JobTable workspace="xhs" onOpenLog={setLogOpen} />,
            },
            {
              key: "logs",
              label: "执行日志",
              children: <LogTab onOpenLog={setLogOpen} />,
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
