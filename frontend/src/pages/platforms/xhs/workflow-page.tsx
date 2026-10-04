/**
 * 自动化工作流页（设计 19 / M5，2026-10-04）。
 *
 * ## 结构
 *
 * 三个区块，按「先看全局、再看单条、再看历史」分块（用户口径：同一件事
 * 要**分块 / 分 Tab**，不混在一起）：
 *
 * 1. **工作流列表**：名称 / 触发器 / 节点链 / 状态 / 最近一次 run；
 * 2. **运行记录抽屉**：单条 run 的节点执行明细 + 取消 / 重跑 / 审核；
 * 3. **待我处理**：`paused` 的 run（卡在人工审核，§3.3 硬约束：不自动放行）。
 *
 * ## ⚠️ 三条口径
 *
 * - **节点类型 / 状态词表的唯一真相源在后端**（`models/workflow.py`）——
 *   本页只经 `lib/workflow-format.ts` 做展示映射，**表外值原样显示**（不造名）；
 * - **未结束的 run 耗时显示 `-`**，不补 0（缺值口径）；
 * - **`paused` 必须人工放行**，页面用醒目标识把它推到用户面前 ——
 *   自动放行是设计明令禁止的。
 */

import {
  ClockCircleOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  PlayCircleOutlined,
  ExclamationCircleOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  theme,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import { PageHeader } from "../../../components/layout/app-shell";
import {
  cancelWorkflowRun,
  createWorkflow,
  deleteWorkflow,
  fetchWorkflowDetail,
  fetchWorkflowRuns,
  fetchWorkflowRunDetail,
  fetchWorkflows,
  rerunWorkflowNode,
  reviewWorkflowNode,
  runWorkflow,
  updateWorkflow,
} from "../../../lib/api";
import type {
  WorkflowDetail,
  WorkflowListItem,
  WorkflowNodePayload,
  WorkflowRunDetail,
  WorkflowRunSummary,
} from "../../../lib/api";
import { formatShanghaiTime } from "../../../lib/time";
import {
  canCancelRun,
  canRerunNode,
  chainText,
  durationText,
  needsHumanReview,
  nodeLabel,
  runStatusColor,
  runStatusText,
  selectableNodeTypes,
  sortChain,
  triggerText,
} from "../../../lib/workflow-format";

const { Text, Paragraph } = Typography;

/** 节点默认配置（占位，用户按需改；空配置也能保存 —— 服务端有 schema 校验）。 */
function emptyNode(nodeType: string, seq: number): WorkflowNodePayload {
  return {
    node_type: nodeType,
    node_key: `${nodeType}_${seq}`,
    node_name: nodeLabel(nodeType),
    seq,
    config: {},
    enabled: true,
  };
}

export function WorkflowPage() {
  const { token } = theme.useToken();
  const { message, modal } = App.useApp();

  const [items, setItems] = useState<WorkflowListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [form] = Form.useForm();

  // 详情 / 运行记录
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]);
  const [runDetail, setRunDetail] = useState<WorkflowRunDetail | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchWorkflows({ page: 1, page_size: 50 });
      setItems(data.items ?? []);
    } catch {
      message.error("加载工作流失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
  }, [load]);

  /** 待人工审核的 run（`paused`）—— 页面第一眼要看到的告警。 */
  const paused = useMemo(
    () => items.filter((it) => it.last_run && needsHumanReview(it.last_run.status)),
    [items],
  );

  const openDetail = async (workflowId: number) => {
    try {
      const [wf, runList] = await Promise.all([
        fetchWorkflowDetail(workflowId),
        fetchWorkflowRuns(workflowId, { page: 1, page_size: 30 }),
      ]);
      setDetail(wf);
      setRuns(runList.items ?? []);
      setDrawerOpen(true);
    } catch {
      message.error("加载工作流详情失败");
    }
  };

  const onCreate = async () => {
    try {
      const values = await form.validateFields();
      const nodeTypes: string[] = values.node_types ?? [];
      const nodes: WorkflowNodePayload[] = [
        emptyNode("trigger", 0),
        ...nodeTypes.filter((t) => t !== "trigger").map((t, i) => emptyNode(t, i + 1)),
      ];
      const created = await createWorkflow({
        name: values.name,
        trigger_type: values.trigger_type,
        cron_expression: values.cron_expression ?? "",
        misfire_policy: "once",
        remark: values.remark ?? "",
        nodes,
      });
      message.success(`已创建工作流「${created.name}」`);
      setCreateOpen(false);
      form.resetFields();
      await load();
    } catch (error) {
      // 校验失败（DAG / 节点 schema）由后端给结构化原因 —— 原样透出，
      // 不吞成「创建失败」（用户需要知道是哪条规则不过）。
      const detailText =
        (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      if (detailText) {
        message.error(typeof detailText === "string" ? detailText : JSON.stringify(detailText));
      }
    }
  };

  const onToggle = async (row: WorkflowListItem) => {
    try {
      await updateWorkflow(row.id, { status: row.status === "enabled" ? "disabled" : "enabled" });
      message.success(row.status === "enabled" ? "已停用" : "已启用");
      await load();
    } catch {
      message.error("切换状态失败");
    }
  };

  const onRun = async (row: WorkflowListItem) => {
    try {
      await runWorkflow(row.id);
      message.success("已触发执行，稍后刷新查看进度");
      await load();
    } catch (error) {
      const detailText =
        (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      message.error(typeof detailText === "string" ? detailText : "触发失败");
    }
  };

  const onDelete = (row: WorkflowListItem) => {
    modal.confirm({
      title: `删除工作流「${row.name}」？`,
      content: "运行历史会保留（run 的 workflow_id 置空），节点配置一并删除。",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteWorkflow(row.id);
          message.success("已删除");
          setDrawerOpen(false);
          await load();
        } catch {
          message.error("删除失败");
        }
      },
    });
  };

  const openRun = async (runId: number) => {
    try {
      setRunDetail(await fetchWorkflowRunDetail(runId));
    } catch {
      message.error("加载运行详情失败");
    }
  };

  const onCancelRun = async (run: WorkflowRunDetail) => {
    try {
      await cancelWorkflowRun(run.id);
      message.success("已取消");
      setRunDetail(await fetchWorkflowRunDetail(run.id));
      await load();
    } catch {
      message.error("取消失败");
    }
  };

  const onRerun = async (run: WorkflowRunDetail, nodeKey: string) => {
    try {
      await rerunWorkflowNode(run.id, nodeKey);
      message.success("已重新入队");
      setRunDetail(await fetchWorkflowRunDetail(run.id));
    } catch {
      message.error("重跑失败");
    }
  };

  const onReview = async (run: WorkflowRunDetail, nodeKey: string, decisions: Record<number, string>) => {
    try {
      const result = await reviewWorkflowNode(run.id, nodeKey, decisions);
      message.success(`已处理 ${result.applied} 篇`);
      setRunDetail(await fetchWorkflowRunDetail(run.id));
      await load();
    } catch {
      message.error("审核提交失败");
    }
  };

  const columns: ColumnsType<WorkflowListItem> = [
    {
      title: "名称",
      dataIndex: "name",
      render: (name: string, row) => (
        <Space direction="vertical" size={0}>
          <a onClick={() => void openDetail(row.id)}>{name}</a>
          {row.status === "disabled" ? <Text type="secondary">已停用</Text> : null}
        </Space>
      ),
    },
    {
      title: "触发器",
      dataIndex: "trigger_type",
      width: 180,
      render: (t: string, row) => <Text>{triggerText(t, row.cron_expression)}</Text>,
    },
    {
      title: "节点",
      dataIndex: "node_count",
      width: 90,
      render: (n: number) => `${n} 个`,
    },
    {
      title: "最近一次",
      dataIndex: "last_run",
      width: 220,
      // ⚠️ 缺值渲染 `-`，不渲染 0（缺值口径）。
      render: (_, row) =>
        row.last_run ? (
          <Space direction="vertical" size={0}>
            <Tag color={runStatusColor(row.last_run.status)}>
              {runStatusText(row.last_run.status)}
            </Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {durationText(row.last_run.started_at, row.last_run.finished_at)}
            </Text>
          </Space>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
    {
      title: "操作",
      key: "actions",
      width: 200,
      render: (_, row) => (
        <Space>
          <Button size="small" icon={<PlayCircleOutlined />} onClick={() => void onRun(row)}>
            执行
          </Button>
          <Button size="small" onClick={() => void onToggle(row)}>
            {row.status === "enabled" ? "停用" : "启用"}
          </Button>
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => onDelete(row)} />
        </Space>
      ),
    },
  ];

  const runsColumns: ColumnsType<WorkflowRunSummary> = [
    {
      title: "运行",
      dataIndex: "id",
      width: 80,
      render: (id: number) => <a onClick={() => void openRun(id)}>#{id}</a>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (s: string) => <Tag color={runStatusColor(s)}>{runStatusText(s)}</Tag>,
    },
    {
      title: "触发源",
      dataIndex: "trigger_source",
      width: 100,
    },
    {
      title: "开始",
      dataIndex: "started_at",
      render: (v: string | null) => (v ? formatShanghaiTime(v) : "-"),
    },
    {
      title: "耗时",
      key: "duration",
      width: 100,
      render: (_, row) => durationText(row.started_at, row.finished_at),
    },
  ];

  return (
    <>
      <PageHeader
        eyebrow="XHS Workflow"
        title="自动化工作流"
        description="编排「选题 → 生成 → 审核 → 发布 → 监控」链路；人工审核节点默认不自动放行。"
        action={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              新建工作流
            </Button>
          </Space>
        }
      />

      <Row gutter={[16, 16]}>
        <Col span={6}>
          <Card>
            <Statistic title="工作流总数" value={items.length} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="已启用"
              value={items.filter((it) => it.status === "enabled").length}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="待我审核"
              value={paused.length}
              valueStyle={paused.length ? { color: token.colorWarning } : undefined}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="最近有失败"
              value={items.filter((it) => it.last_run?.status === "failed").length}
            />
          </Card>
        </Col>
      </Row>

      {paused.length > 0 ? (
        <Alert
          style={{ marginTop: 16 }}
          type="warning"
          showIcon
          icon={<ExclamationCircleOutlined />}
          message={`有 ${paused.length} 个工作流卡在人工审核`}
          description="审核节点默认人工放行（设计 19 §3.3）—— 打开运行记录逐篇通过或驳回。"
          action={
            <Button size="small" onClick={() => void openDetail(paused[0].id)}>
              去处理
            </Button>
          }
        />
      ) : null}

      <Card style={{ marginTop: 16 }} title="工作流列表">
        {items.length === 0 && !loading ? (
          <Empty description="还没有工作流。新建一个，节点链会自动补上触发器。" />
        ) : (
          <Table
            rowKey="id"
            loading={loading}
            dataSource={items}
            columns={columns}
            pagination={false}
            size="middle"
          />
        )}
      </Card>

      {/* 新建 */}
      <Modal
        open={createOpen}
        title="新建工作流"
        centered
        onCancel={() => setCreateOpen(false)}
        onOk={() => void onCreate()}
        okText="创建"
        width="40vw"
      >
        <Form form={form} layout="vertical" initialValues={{ trigger_type: "manual", node_types: [] }}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "给工作流起个名字" }]}>
            <Input placeholder="例如：秋季新品每日种草" />
          </Form.Item>
          <Form.Item name="trigger_type" label="触发器" rules={[{ required: true }]}>
            <Select
              options={[
                { value: "manual", label: "手动" },
                { value: "cron", label: "定时" },
                { value: "at", label: "一次性" },
              ]}
            />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(p, c) => p.trigger_type !== c.trigger_type}>
            {({ getFieldValue }) =>
              getFieldValue("trigger_type") === "cron" ? (
                <Form.Item name="cron_expression" label="cron 表达式">
                  <Input placeholder="0 0 9 * * *" />
                </Form.Item>
              ) : null
            }
          </Form.Item>
          <Form.Item
            name="node_types"
            label="节点链"
            extra="触发器会自动加在链首；审核节点默认人工放行"
          >
            <Select
              mode="multiple"
              placeholder="选择需要的节点（不选也行，之后再改）"
              options={[
                { value: "topic", label: nodeLabel("topic") },
                { value: "generate", label: nodeLabel("generate") },
                { value: "review", label: nodeLabel("review") },
                { value: "schedule", label: nodeLabel("schedule") },
                { value: "publish", label: nodeLabel("publish") },
                { value: "monitor", label: nodeLabel("monitor") },
              ]}
            />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 详情 + 运行记录 */}
      <Drawer
        open={drawerOpen}
        onClose={() => {
          setDrawerOpen(false);
          setRunDetail(null);
        }}
        width="60vw"
        title={detail ? detail.name : "工作流"}
      >
        {detail ? (
          <>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="状态">{workflowStatusTextOf(detail.status)}</Descriptions.Item>
              <Descriptions.Item label="触发器">
                {triggerText(detail.trigger_type, detail.cron_expression)}
              </Descriptions.Item>
              <Descriptions.Item label="节点链" span={2}>
                {chainText(sortChain(detail.nodes))}
              </Descriptions.Item>
              {detail.remark ? (
                <Descriptions.Item label="备注" span={2}>
                  {detail.remark}
                </Descriptions.Item>
              ) : null}
            </Descriptions>

            <Card size="small" title="运行记录" style={{ marginTop: 16 }}>
              {runs.length === 0 ? (
                <Empty description="还没有运行记录" />
              ) : (
                <Table
                  rowKey="id"
                  dataSource={runs}
                  columns={runsColumns}
                  pagination={false}
                  size="small"
                />
              )}
            </Card>
          </>
        ) : null}

        {runDetail ? (
          <Card
            size="small"
            style={{ marginTop: 16 }}
            title={
              <Space>
                <span>
                  运行 #{runDetail.id} · {runStatusText(runDetail.status)}
                </span>
                {needsHumanReview(runDetail.status) ? (
                  <Badge status="warning" text="待人工审核" />
                ) : null}
              </Space>
            }
            extra={
              canCancelRun(runDetail.status) ? (
                <Button size="small" danger onClick={() => void onCancelRun(runDetail)}>
                  取消
                </Button>
              ) : (
                <Text type="secondary">耗时 {durationText(runDetail.started_at, runDetail.finished_at)}</Text>
              )
            }
          >
            <List
              size="small"
              dataSource={runDetail.nodes}
              renderItem={(node) => (
                <List.Item
                  actions={[
                    canRerunNode(runDetail.status) ? (
                      <Button
                        key="rerun"
                        size="small"
                        onClick={() => void onRerun(runDetail, node.node_key)}
                      >
                        重跑
                      </Button>
                    ) : null,
                  ].filter(Boolean)}
                >
                  <List.Item.Meta
                    avatar={<ClockCircleOutlined />}
                    title={
                      <Space>
                        <span>{nodeLabel(node.node_type)}</span>
                        <Tag color={runStatusColor(node.status)}>{runStatusText(node.status)}</Tag>
                      </Space>
                    }
                    description={
                      <Space direction="vertical" size={0}>
                        <Text type="secondary">
                          {node.started_at ? formatShanghaiTime(node.started_at) : "-"} ·{" "}
                          {durationText(node.started_at, node.finished_at)}
                        </Text>
                        {node.error_message ? <Text type="danger">{node.error_message}</Text> : null}
                      </Space>
                    }
                  />
                </List.Item>
              )}
            />

            {runDetail.pending_reviews.length > 0 ? (
              <Card
                size="small"
                type="inner"
                style={{ marginTop: 12 }}
                title="待审核笔记"
              >
                {/* ⚠️ 审核**必须逐篇显式选择**：不给「全选通过」——
                    那是把「默认人工放行」变成「默认批量放行」。 */}
                <PendingReviewList
                  items={runDetail.pending_reviews}
                  onSubmit={(nodeKey, decisions) => void onReview(runDetail, nodeKey, decisions)}
                />
              </Card>
            ) : null}
          </Card>
        ) : null}
      </Drawer>
    </>
  );
}

/** 待审核清单：逐篇选「通过 / 驳回」，全部选完才允许提交。 */
function PendingReviewList({
  items,
  onSubmit,
}: {
  items: { node_key: string; note_id: number; note_title: string }[];
  onSubmit: (nodeKey: string, decisions: Record<number, string>) => void;
}) {
  const [decisions, setDecisions] = useState<Record<number, string>>({});
  const nodeKey = items[0]?.node_key ?? "";
  const decided = Object.keys(decisions).length;

  return (
    <>
      <List
        size="small"
        dataSource={items}
        renderItem={(item) => (
          <List.Item>
            <Space style={{ width: "100%", justifyContent: "space-between" }}>
              <Text ellipsis style={{ maxWidth: "60%" }}>
                {item.note_title || `笔记 #${item.note_id}`}
              </Text>
              <Select
                size="small"
                style={{ width: 100 }}
                placeholder="选择"
                value={decisions[item.note_id]}
                onChange={(value) => setDecisions((prev) => ({ ...prev, [item.note_id]: value }))}
                options={[
                  { value: "approve", label: "通过" },
                  { value: "reject", label: "驳回" },
                ]}
              />
            </Space>
          </List.Item>
        )}
      />
      <Space style={{ marginTop: 8 }}>
        <Button
          type="primary"
          size="small"
          // ⚠️ 未全部选择 ⇒ 禁用：避免「漏选的那几篇」被当成默认放行。
          disabled={decided !== items.length}
          onClick={() => onSubmit(nodeKey, decisions)}
        >
          提交（{decided}/{items.length}）
        </Button>
        <Text type="secondary">逐篇选择后才能提交</Text>
      </Space>
    </>
  );
}

/** 工作流状态文案（本页内联，避免为一个映射再引一层）。 */
function workflowStatusTextOf(status: string): string {
  return status === "enabled" ? "已启用" : status === "disabled" ? "已停用" : status;
}
