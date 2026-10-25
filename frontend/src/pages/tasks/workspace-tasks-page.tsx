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
 * ## 能力边界（2026-10-25 用户第二次补充）
 *
 * 「任务中心页面没有新增和编辑入口，点击执行没有每次需要修改的任务参数」
 *
 * | 操作 | 有没有 | 说明 |
 * |---|---|---|
 * | 新增任务 | ✅ | 走 `JobFormModal`；只能选**注册表里已注册**的 job_key（用户口径「暂时不允许用户创建任务」= 不能自造任务类型）|
 * | 编辑任务 | ✅ | 同名弹窗传 `job` 即编辑态（改 Cron / 默认参数）|
 * | 立即执行 | ✅ | **先弹参数确认**（`RunJobModal`）—— 参数只对本次生效，不写回 |
 * | 暂停 / 恢复 | ✅ | |
 * | 看日志 | ✅ | |
 *
 * ⚠️ 「立即执行」必须弹参数窗：后端 `POST /jobs/{id}/run` 的 `params`
 * 覆盖能力一直都在，页面不接入等于把「今天只同步账号 A」这种日常操作弄丢。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { PlusOutlined, QuestionCircleOutlined, ReloadOutlined } from "@ant-design/icons";
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
import { JobFormModal } from "../../components/schedule/job-form-modal";
import { RunJobModal } from "../../components/schedule/run-job-modal";
import { hasPermission } from "../../hooks/use-permission";

const { Text } = Typography;

/** 当前页面所属工作区（`/platforms/xhs/tasks` 挂在 xhs 工作区下）。 */
const WORKSPACE = "xhs";
const WORKSPACE_LABEL = "小红书";

/** 本工作区的任务（可操作：立即执行 / 暂停恢复）。 */
function WorkspaceJobTable({
  onShowLogs,
  onRun,
  onEdit,
  reloadKey,
}: {
  onShowLogs: () => void;
  /** 点「立即执行」⇒ 打开参数确认弹窗（不是直接打接口）。 */
  onRun: (job: ScheduledJob) => void;
  onEdit: (job: ScheduledJob) => void;
  /** 外部触发刷新（执行 / 编辑保存后）。 */
  reloadKey: number;
}) {
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
  }, [load, reloadKey]);

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
              <Button size="small" type="link" onClick={() => onRun(job)}>
                立即执行
              </Button>
            )}
            {hasPermission("xhs:taskcenter:edit") && (
              <Button size="small" type="link" onClick={() => onEdit(job)}>
                编辑
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
    [busyId, toggleStatus, onShowLogs, onRun, onEdit],
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
  //: 新增 / 编辑共用（`job === null` = 新增态）
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ScheduledJob | null>(null);
  //: 立即执行前的参数确认
  const [runJob, setRunJob] = useState<ScheduledJob | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [runSubmitting, setRunSubmitting] = useState(false);

  /** 任务行点「日志」⇒ 切到日志页签。 */
  const showLogs = useCallback(() => {
    setActiveTab("logs");
  }, []);

  const openCreate = useCallback(() => {
    setEditing(null);
    setFormOpen(true);
  }, []);

  const openEdit = useCallback((job: ScheduledJob) => {
    setEditing(job);
    setFormOpen(true);
  }, []);

  /** 执行（由表格或参数弹窗触发）。 */
  const doRun = useCallback(
    async (job: ScheduledJob, params?: Record<string, unknown>) => {
      await runScheduleJobOnce(job.id, params);
      setReloadKey((n) => n + 1);
    },
    [],
  );

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
          tabBarExtraContent={
            //: 无权限 ⇒ 不渲染（用户口径「没有对应权限则不展示」）
            hasPermission("xhs:taskcenter:create") && (
              <Button size="small" type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                新增任务
              </Button>
            )
          }
          items={[
            {
              key: "jobs",
              label: "任务",
              children: (
                <WorkspaceJobTable
                  onShowLogs={showLogs}
                  onRun={setRunJob}
                  onEdit={openEdit}
                  reloadKey={reloadKey}
                />
              ),
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

      {/* 新增 / 编辑共用：`job=null` 是新增态 */}
      <JobFormModal
        open={formOpen}
        job={editing}
        onCancel={() => setFormOpen(false)}
        onSaved={() => {
          setFormOpen(false);
          setReloadKey((n) => n + 1);
        }}
      />

      {/* 立即执行：先确认参数（只对本次生效） */}
      <RunJobModal
        open={runJob !== null}
        job={runJob}
        submitting={runSubmitting}
        onCancel={() => setRunJob(null)}
        onSubmit={(params) => {
          if (!runJob) return;
          setRunSubmitting(true);
          void doRun(runJob, params)
            .then(() => setRunJob(null))
            .finally(() => setRunSubmitting(false));
        }}
      />
    </div>
  );
}
