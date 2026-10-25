/**
 * 工作区级任务中心（`/platforms/xhs/tasks`，侧栏「任务」）。
 *
 * ## 两级独立（2026-10-25 用户口径）
 *
 * 「任务中心分为两级，系统级和工作区域级」
 * 「系统任务在系统管理中展示操作等，工作区域任务在对应工作区内展示，
 *   以及相关的操作等，**相互独立展示不集中在系统管理中**」
 *
 * | 级别 | 入口 | 内容 | 权限 |
 * |---|---|---|---|
 * | 系统级 | 系统管理 → 任务中心 | `workspace="system"`（清理本机资源）| admin |
 * | **工作区级** | **本页** | `workspace="xhs"`（只作用于本工作区数据）| 工作区成员 |
 *
 * ## 两处曾经做错的地方（记录，别再犯）
 *
 * ① 把工作区级做成**系统管理里的一个页签**（「小红书任务」）—— 用户截图指出
 *    「小红书中的任务中心没有了」。与配置那次同一种错法：私有内容塞进系统级
 *    页面 + 标注「只对该工作区生效」。**标注 ≠ 归属**。
 * ② 改成独立页后又做成**严格只读** —— 用户第二次纠正「以及相关的操作等」。
 *    把「暂时不允许用户创建任务」读成了「什么都不能做」：
 *    不许**新建**（那是系统注册表的事）≠ 不许**操作已注册的任务**。
 *
 * 本页能力边界：可「立即执行 / 暂停恢复 / 看日志」，不可新建或改 key。
 *
 * ## 与配置页同形态
 *
 * 单卡片 + 标题「小红书任务」。说明只进问号图标（用户口径「说明简短」）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { QuestionCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { App, Button, Card, Space, Table, Tabs, Tooltip, Typography } from "antd";

import {
  changeScheduleJobStatus,
  fetchScheduleJobs,
  fetchScheduleLogs,
  runScheduleJobOnce,
} from "../../lib/api";
import type { ScheduledJob, ScheduledJobLog } from "../../types";
import { buildTaskColumns } from "../../components/schedule/task-columns";
import { JobLogTable } from "../../components/schedule/job-log-table";
import { JobLogDetailModal } from "../../components/schedule/job-log-detail-modal";
import { hasPermission } from "../../hooks/use-permission";

const { Text } = Typography;

/** 当前页面所属工作区（`/platforms/xhs/tasks` 挂在 xhs 工作区下）。 */
const WORKSPACE = "xhs";
const WORKSPACE_LABEL = "小红书";

/** 本工作区的任务（可操作：立即执行 / 暂停恢复）。 */
function WorkspaceJobTable({ onShowLogs }: { onShowLogs: () => void }) {
  const { message } = App.useApp();
  const [rows, setRows] = useState<ScheduledJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      //: 服务端筛选（端点支持 `workspace`）—— 不把别的工作区的任务传到前端再丢。
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

  const runOnce = useCallback(
    async (job: ScheduledJob) => {
      setBusyId(job.id);
      try {
        await runScheduleJobOnce(job.id);
        message.success(`已触发「${job.name}」`);
        await load();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "触发失败");
      } finally {
        setBusyId(null);
      }
    },
    [load, message],
  );

  const toggleStatus = useCallback(
    async (job: ScheduledJob) => {
      const next = job.status === "active" ? "paused" : "active";
      setBusyId(job.id);
      try {
        await changeScheduleJobStatus(job.id, next);
        message.success(next === "active" ? "已恢复" : "已暂停");
        await load();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "操作失败");
      } finally {
        setBusyId(null);
      }
    },
    [load, message],
  );

  //: 列由公共工厂产出（用户口径「共用的提取成公共组件，不同参数和页面调用展示对应内容」）。
  const columns = useMemo(
    () =>
      buildTaskColumns({
        showWorkspace: false,
        showTriggerDetail: true,
        renderActions: (job) => (
          <Space size={0}>
            {/* 一按钮一码（2026-10-25 红线）：三个按钮各判各的，不共用权限码。 */}
            {hasPermission("xhs:taskcenter:run") && (
              <Button
                size="small"
                type="link"
                loading={busyId === job.id}
                onClick={() => void runOnce(job)}
              >
                立即执行
              </Button>
            )}
            {hasPermission("xhs:taskcenter:toggle") && (
              <Button
                size="small"
                type="link"
                loading={busyId === job.id}
                onClick={() => void toggleStatus(job)}
              >
                {job.status === "active" ? "暂停" : "恢复"}
              </Button>
            )}
            {hasPermission("xhs:taskcenter:viewLogs") && (
              <Button size="small" type="link" onClick={onShowLogs}>
                日志
              </Button>
            )}
          </Space>
        ),
      }),
    [busyId, runOnce, toggleStatus, onShowLogs],
  );

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
          scroll={{ x: 1500, y: "100%" }}
          pagination={false}
        />
      </div>
    </div>
  );
}

/** 本工作区的执行日志。 */
function WorkspaceLogTab({ onOpenLog }: { onOpenLog: (log: ScheduledJobLog) => void }) {
  const [rows, setRows] = useState<ScheduledJobLog[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [total, setTotal] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      //: 必须带 workspace —— 服务端筛，否则系统级任务（清理记录表等）
      //: 的执行记录会混进本工作区页面。
      const res = await fetchScheduleLogs({ workspace: WORKSPACE, page: 1, page_size: 100 });
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
          onRowClick={onOpenLog}
        />
      </div>
    </div>
  );
}

export function WorkspaceTasksPage() {
  const [logOpen, setLogOpen] = useState<ScheduledJobLog | null>(null);
  const [activeTab, setActiveTab] = useState("jobs");

  /** 任务行点「日志」⇒ 切到日志页签。 */
  const showLogs = useCallback(() => {
    setActiveTab("logs");
  }, []);

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
            {WORKSPACE_LABEL}任务
            <Tooltip title="仅作用于本工作区的数据。系统级任务在「系统管理 → 任务中心」。">
              <QuestionCircleOutlined style={{ marginLeft: 6, color: "rgba(0,0,0,0.45)" }} />
            </Tooltip>
          </span>
        }
      >
        <Tabs
          className="fill-tabs"
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: "jobs",
              label: "任务",
              children: <WorkspaceJobTable onShowLogs={showLogs} />,
            },
            {
              key: "logs",
              label: "执行日志",
              children: <WorkspaceLogTab onOpenLog={setLogOpen} />,
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
