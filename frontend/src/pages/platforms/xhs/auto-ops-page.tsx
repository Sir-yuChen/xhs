import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  Typography,
} from "antd";
import { useEffect, useState } from "react";

import { PageHeader } from "../../../components/layout/app-shell";
import { useThemePalette } from "../../../app/providers";
import { ScheduleFields } from "../../../components/schedule/schedule-fields";
import {
  createAutoTask,
  deleteAutoTask,
  fetchAccounts,
  fetchAutoTasks,
  runAutoTask,
  updateAutoTask,
} from "../../../lib/api";
import { triggerSummary } from "../../../lib/schedule";
import { formatShanghaiTime } from "../../../lib/time";
import type { AutoTask, AutoTaskRunResult, PlatformAccount, ScheduleIn } from "../../../types";

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

const STATUS_CONFIG: Record<string, { color: string; label: string }> = {
  active: { color: "green", label: "运行中" },
  paused: { color: "default", label: "已暂停" },
  completed: { color: "blue", label: "已完成" },
};

/** 默认调度：每天 9:00（与后端惰性默认同值；enabled=false = 不自动执行） */
const DEFAULT_SCHEDULE: ScheduleIn = {
  trigger_type: "cron",
  cron_expression: "0 0 9 * * *",
  misfire_policy: "once",
  concurrent: "forbid",
  enabled: false,
};

function scheduleInFromTask(task: AutoTask): ScheduleIn {
  const schedule = task.schedule;
  if (!schedule) {
    return DEFAULT_SCHEDULE;
  }
  return {
    trigger_type: schedule.trigger_type as ScheduleIn["trigger_type"],
    cron_expression: schedule.cron_expression,
    run_at: schedule.run_at ?? null,
    misfire_policy: schedule.misfire_policy as ScheduleIn["misfire_policy"],
    concurrent: schedule.concurrent as ScheduleIn["concurrent"],
    // 调度生效 = 任务本身 active ∧ job 行 active（后端同口径）
    enabled: schedule.status === "active" && task.status === "active",
  };
}

function getStatusTag(s: string) {
  const cfg = STATUS_CONFIG[s] ?? { color: "default", label: s };
  return <Tag color={cfg.color}>{cfg.label}</Tag>;
}

const cardBodyStyle: React.CSSProperties = {
  padding: 16,
};

export function AutoOpsPage() {
  const p = useThemePalette();
  const panelStyle: React.CSSProperties = {
    background: p.cardBg,
    borderRadius: 8,
    border: `1px solid ${p.border}`,
  };
  const [tasks, setTasks] = useState<AutoTask[]>([]);
  const [pcAccounts, setPcAccounts] = useState<PlatformAccount[]>([]);
  const [creatorAccounts, setCreatorAccounts] = useState<PlatformAccount[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径）：
  // 操作结果反馈不再渲染 inline Alert 占版面。
  const { message } = App.useApp();

  // Create form state
  const [showCreate, setShowCreate] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createKeywords, setCreateKeywords] = useState("");
  const [createPcAccountId, setCreatePcAccountId] = useState<number | null>(null);
  const [createCreatorAccountId, setCreateCreatorAccountId] = useState<number | null>(null);
  const [createInstruction, setCreateInstruction] = useState("");
  const [createSchedule, setCreateSchedule] = useState<ScheduleIn>(DEFAULT_SCHEDULE);
  const [isCreating, setIsCreating] = useState(false);

  // Edit modal state
  const [editTask, setEditTask] = useState<AutoTask | null>(null);
  const [editName, setEditName] = useState("");
  const [editKeywords, setEditKeywords] = useState("");
  const [editInstruction, setEditInstruction] = useState("");
  const [editSchedule, setEditSchedule] = useState<ScheduleIn>(DEFAULT_SCHEDULE);
  const [isSaving, setIsSaving] = useState(false);

  // Run state
  const [runningTaskId, setRunningTaskId] = useState<number | null>(null);
  const [lastRunResult, setLastRunResult] = useState<AutoTaskRunResult | null>(null);

  function parseKeywords(text: string): string[] {
    return text
      .split("\n")
      .map((k) => k.trim())
      .filter(Boolean);
  }

  async function loadData() {
    setIsLoading(true);
    try {
      const [tasksRes, accountsRes] = await Promise.all([
        fetchAutoTasks(),
        fetchAccounts("xhs"),
      ]);
      setTasks(tasksRes.items);
      setPcAccounts(accountsRes.filter((a) => a.sub_type === "pc"));
      setCreatorAccounts(accountsRes.filter((a) => a.sub_type === "creator"));
    } catch {
      message.error("加载自动运营任务失败。");
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    void loadData();
  }, []);

  async function handleCreate() {
    const keywords = parseKeywords(createKeywords);
    if (!createName.trim() || keywords.length === 0 || !createPcAccountId || !createCreatorAccountId) {
      message.warning("请填写任务名称、至少一个关键词，并选择 PC 和 Creator 账号。");
      return;
    }
    setIsCreating(true);
    try {
      const created = await createAutoTask({
        name: createName.trim(),
        keywords,
        pc_account_id: createPcAccountId,
        creator_account_id: createCreatorAccountId,
        ai_instruction: createInstruction,
        schedule: createSchedule,
      });
      setTasks((prev) => [created, ...prev]);
      setShowCreate(false);
      setCreateName("");
      setCreateKeywords("");
      setCreatePcAccountId(null);
      setCreateCreatorAccountId(null);
      setCreateInstruction("");
      setCreateSchedule(DEFAULT_SCHEDULE);
      message.success(`自动任务"${created.name}"已创建。`);
    } catch {
      message.error("创建自动任务失败。");
    } finally {
      setIsCreating(false);
    }
  }

  async function handleToggleStatus(task: AutoTask) {
    const newStatus = task.status === "active" ? "paused" : "active";
    try {
      const updated = await updateAutoTask(task.id, { status: newStatus });
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
      message.success(`任务"${updated.name}"已${newStatus === "active" ? "恢复" : "暂停"}。`);
    } catch {
      message.error("更新任务状态失败。");
    }
  }

  async function handleDelete(taskId: number) {
    try {
      await deleteAutoTask(taskId);
      setTasks((prev) => prev.filter((t) => t.id !== taskId));
      message.success("自动任务已删除。");
    } catch {
      message.error("删除自动任务失败。");
    }
  }

  async function handleRun(task: AutoTask) {
    setRunningTaskId(task.id);
    setLastRunResult(null);
    try {
      const result = await runAutoTask(task.id);
      setLastRunResult(result);
      setTasks((prev) => prev.map((t) => (t.id === result.auto_task.id ? result.auto_task : t)));
      message.success(
        `任务"${task.name}"执行完成 -- 关键词: ${result.detail?.keyword ?? "-"}, 已创建发布任务 #${result.detail?.publish_job?.id ?? "-"}。`
      );
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(`执行失败：${detail || "请检查账号和模型配置。"}`);
    } finally {
      setRunningTaskId(null);
    }
  }

  function openEdit(task: AutoTask) {
    setEditTask(task);
    setEditName(task.name);
    setEditKeywords((task.keywords || []).join("\n"));
    setEditInstruction(task.ai_instruction);
    setEditSchedule(scheduleInFromTask(task));
  }

  async function handleSaveEdit() {
    if (!editTask) return;
    setIsSaving(true);
    try {
      const keywords = parseKeywords(editKeywords);
      const updated = await updateAutoTask(editTask.id, {
        name: editName.trim() || undefined,
        keywords: keywords.length > 0 ? keywords : undefined,
        ai_instruction: editInstruction,
        schedule: editSchedule,
      });
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
      setEditTask(null);
      message.success(`任务"${updated.name}"已更新。`);
    } catch {
      message.error("更新任务失败。");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHeader
        eyebrow="XHS Auto Operations"
        title="自动运营"
        description="设置关键词、自动抓取热门笔记、AI 改写后自动创建发布任务，实现全自动内容生产管线。"
        action={
          <Button icon={<ReloadOutlined />} onClick={loadData} loading={isLoading}>
            刷新
          </Button>
        }
      />

      {/* Task List */}
      {isLoading ? (
        <Card style={panelStyle} styles={{ body: cardBodyStyle }}>
          <div style={{ textAlign: "center", padding: 48 }}>
            <Spin size="large" />
            <Paragraph style={{ color: "#8c8c8c", marginTop: 16 }}>正在加载自动运营任务...</Paragraph>
          </div>
        </Card>
      ) : tasks.length === 0 && !showCreate ? (
        <Card style={panelStyle} styles={{ body: cardBodyStyle }}>
          <Empty
            image={<ThunderboltOutlined style={{ fontSize: 48, color: "#8c8c8c" }} />}
            imageStyle={{ height: 64 }}
            description={
              <div>
                <Text strong style={{ fontSize: 16 }}>
                  暂无自动运营任务
                </Text>
                <br />
                <Text type="secondary">点击"新建任务"开始配置关键词自动抓取、AI 改写和发布管线。</Text>
              </div>
            }
          />
        </Card>
      ) : (
        <Row gutter={[16, 16]}>
          {tasks.map((task) => (
            <Col xs={24} md={12} xl={8} key={task.id}>
              <Card
                style={panelStyle}
                styles={{ body: cardBodyStyle, header: { borderBottom: `1px solid ${p.border}` } }}
                title={
                  <Space>
                    <ThunderboltOutlined style={{ color: task.status === "active" ? "#52c41a" : "#8c8c8c" }} />
                    <Text ellipsis style={{ maxWidth: 180 }}>
                      {task.name}
                    </Text>
                  </Space>
                }
                extra={getStatusTag(task.status)}
              >
                {/* Keywords */}
                <div style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
                    关键词
                  </Text>
                  <Space size={4} wrap>
                    {(task.keywords || []).map((kw) => (
                      <Tag key={kw} color="blue">
                        {kw}
                      </Tag>
                    ))}
                  </Space>
                </div>

                {/* Stats */}
                <Row gutter={16} style={{ marginBottom: 12 }}>
                  <Col span={8}>
                    <Statistic
                      title="已发布"
                      value={task.total_published}
                      valueStyle={{ fontSize: 20, color: "#e8e8e8" }}
                    />
                  </Col>
                </Row>

                {/* Time info */}
                <Space direction="vertical" size={2} style={{ width: "100%", marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    <ClockCircleOutlined style={{ marginRight: 4 }} />
                    上次运行：{formatShanghaiTime(task.schedule?.last_run_at ?? task.last_run_at)}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    创建时间：{formatShanghaiTime(task.created_at)}
                  </Text>
                </Space>

                {/* AI instruction preview */}
                {task.ai_instruction && (
                  <div style={{ marginBottom: 12 }}>
                    <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 2 }}>
                      AI 指令
                    </Text>
                    <Paragraph
                      type="secondary"
                      ellipsis={{ rows: 2 }}
                      style={{ fontSize: 12, marginBottom: 0 }}
                    >
                      {task.ai_instruction}
                    </Paragraph>
                  </div>
                )}

                {/* Schedule info（调度语义的唯一权威在 1:1 关联的 job 行，设计 15 §7.1） */}
                <Space direction="vertical" size={2} style={{ width: "100%" }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    调度：
                    {task.schedule
                      ? `${triggerSummary(task.schedule)}${
                          task.schedule.status === "active" ? "" : "（已暂停，仅可手动执行）"
                        }`
                      : "未配置"}
                  </Text>
                  {task.schedule?.next_run_at && task.schedule.status === "active" && (
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      下次执行：{formatShanghaiTime(task.schedule.next_run_at)}
                    </Text>
                  )}
                  {task.schedule && (
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      调度执行 {task.schedule.run_count} 次
                      {task.schedule.last_status ? ` · 上次${task.schedule.last_status}` : ""}
                    </Text>
                  )}
                </Space>

                {/* Actions */}
                <Space wrap style={{ marginTop: 8 }}>
                  <Button
                    type="primary"
                    size="small"
                    icon={<PlayCircleOutlined />}
                    onClick={() => handleRun(task)}
                    loading={runningTaskId === task.id}
                    disabled={runningTaskId !== null && runningTaskId !== task.id}
                  >
                    立即执行
                  </Button>
                  <Button
                    size="small"
                    icon={task.status === "active" ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
                    onClick={() => handleToggleStatus(task)}
                  >
                    {task.status === "active" ? "暂停" : "恢复"}
                  </Button>
                  <Button
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => openEdit(task)}
                  >
                    编辑
                  </Button>
                  <Popconfirm
                    title="确认删除此自动任务？"
                    onConfirm={() => handleDelete(task.id)}
                    okText="删除"
                    cancelText="取消"
                  >
                    <Button size="small" danger icon={<DeleteOutlined />}>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              </Card>
            </Col>
          ))}
        </Row>
      )}

      {/* Last Run Result */}
      {lastRunResult && (
        <Card
          title={
            <Space>
              <CheckCircleOutlined style={{ color: "#52c41a" }} />
              <span>最近一次执行结果</span>
            </Space>
          }
          style={panelStyle}
          styles={{ body: cardBodyStyle, header: { borderBottom: `1px solid ${p.border}` } }}
        >
          <Descriptions column={{ xs: 1, md: 2, lg: 4 }} size="small">
            <Descriptions.Item label="关键词">{lastRunResult.detail?.keyword ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="来源笔记">
              {lastRunResult.detail?.source_note?.title ?? "-"}
            </Descriptions.Item>
            <Descriptions.Item label="互动量">
              {lastRunResult.detail?.source_note
                ? lastRunResult.detail.source_note.likes +
                  lastRunResult.detail.source_note.collects +
                  lastRunResult.detail.source_note.comments
                : "-"}
            </Descriptions.Item>
            <Descriptions.Item label="发布任务">
              #{lastRunResult.detail?.publish_job?.id ?? "-"}
            </Descriptions.Item>
          </Descriptions>
          <div style={{ marginTop: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              改写后标题：{lastRunResult.detail?.draft?.title ?? "-"}
            </Text>
            <Paragraph
              type="secondary"
              ellipsis={{ rows: 3, expandable: true, symbol: "展开" }}
              style={{ fontSize: 12, marginTop: 4, marginBottom: 0 }}
            >
              {lastRunResult.detail?.draft?.body ?? ""}
            </Paragraph>
          </div>
        </Card>
      )}

      {/* Create Form */}
      {showCreate && (
        <Card
          title={
            <Space>
              <PlusOutlined />
              <span>新建自动运营任务</span>
            </Space>
          }
          style={panelStyle}
          styles={{ body: cardBodyStyle, header: { borderBottom: `1px solid ${p.border}` } }}
          extra={
            <Button type="text" onClick={() => setShowCreate(false)}>
              取消
            </Button>
          }
        >
          <Form layout="vertical">
            <Row gutter={16}>
              <Col xs={24} md={12}>
                <Form.Item label="任务名称" required>
                  <Input
                    placeholder="如：低卡早餐自动发布"
                    value={createName}
                    onChange={(e) => setCreateName(e.target.value)}
                    maxLength={128}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={12}>
                <Form.Item label="关键词（每行一个）" required>
                  <TextArea
                    placeholder={"低卡早餐\n减脂食谱\n健康饮食"}
                    value={createKeywords}
                    onChange={(e) => setCreateKeywords(e.target.value)}
                    rows={3}
                  />
                </Form.Item>
              </Col>
            </Row>
            <Row gutter={16}>
              <Col xs={24} md={12}>
                <Form.Item label="PC 账号（用于抓取）" required>
                  <Select
                    placeholder="选择 PC 账号"
                    value={createPcAccountId}
                    onChange={(v) => setCreatePcAccountId(v)}
                    options={pcAccounts.map((a) => ({
                      value: a.id,
                      // 只展示账号名称（2026-09-24 用户口径）：去掉 `(#id)` 后缀。
                      label: a.nickname || `账号 ${a.id}`,
                    }))}
                    allowClear
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={12}>
                <Form.Item label="Creator 账号（用于发布）" required>
                  <Select
                    placeholder="选择 Creator 账号"
                    value={createCreatorAccountId}
                    onChange={(v) => setCreateCreatorAccountId(v)}
                    options={creatorAccounts.map((a) => ({
                      value: a.id,
                      // 只展示账号名称（2026-09-24 用户口径）：去掉 `(#id)` 后缀。
                      label: a.nickname || `账号 ${a.id}`,
                    }))}
                    allowClear
                  />
                </Form.Item>
              </Col>
            </Row>
            <Form.Item label="AI 改写指令（可选）">
              <TextArea
                placeholder="如：改写为种草风格，加入个人体验感受，适合 25-35 岁女性阅读"
                value={createInstruction}
                onChange={(e) => setCreateInstruction(e.target.value)}
                rows={3}
                maxLength={2000}
              />
            </Form.Item>

            <Form.Item label="调度设置">
              <ScheduleFields value={createSchedule} onChange={setCreateSchedule} />
            </Form.Item>

            <Form.Item>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={handleCreate}
                loading={isCreating}
                block
              >
                创建任务
              </Button>
            </Form.Item>
          </Form>
        </Card>
      )}

      {!showCreate && (
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setShowCreate(true)} block style={{ marginTop: 16 }}>
          新建自动运营任务
        </Button>
      )}

      {/* Edit Modal */}
      <Modal
        title="编辑自动运营任务"
        open={editTask !== null}
        onOk={handleSaveEdit}
        onCancel={() => setEditTask(null)}
        confirmLoading={isSaving}
        okText="保存"
        cancelText="取消"
      >
        <Form layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item label="任务名称">
            <Input
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              maxLength={128}
            />
          </Form.Item>
          <Form.Item label="关键词（每行一个）">
            <TextArea
              value={editKeywords}
              onChange={(e) => setEditKeywords(e.target.value)}
              rows={3}
            />
          </Form.Item>
          <Form.Item label="AI 改写指令">
            <TextArea
              value={editInstruction}
              onChange={(e) => setEditInstruction(e.target.value)}
              rows={3}
              maxLength={2000}
            />
          </Form.Item>

          <Form.Item label="调度设置">
            <ScheduleFields value={editSchedule} onChange={setEditSchedule} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
