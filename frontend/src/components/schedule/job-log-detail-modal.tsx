/**
 * 调度日志详情弹窗（设计 15 §9.1；2026-09-24 用户口径 R19）。
 *
 * 布局：**上下两块**，用标题条分开（不再是原来「一块 Descriptions 里夹一个
 * 嵌套表格」的混排）——
 * 1. **执行日志信息**（当次运行的客观事实）：状态 / 触发方式 / 起止时间 /
 *    耗时 / 进度 / 消息 / 异常 / 执行结果；
 * 2. **快照配置**（当时那个任务是怎么配的）：任务名称 / **调用目标（中文名 +
 *    代码键，与任务列表同口径）** / 触发器 / 策略 / 参数（含执行账号）/ 备注。
 *    任务行之后被改被删都不影响。
 *
 * ⚠️ 调用目标在本弹窗**曾经只显示代码键**（`system.queue_record_cleanup` 这种）
 * —— 2026-09-25 用户口径「调用目标需要配置中文名称」后与列表列拉齐：中文名在前、
 * 代码键在后。中文名由 `targetNames` prop 传入（同一份 `/schedule/targets` 数据源，
 * 不另写映射表）。
 *
 * ⚠️ 块顺序刻意是「日志在前、快照在后」：打开弹窗第一眼要看的是**这次跑得怎么样**，
 * 配置是次要的对照信息。
 *
 * 缺值口径：快照为 null（该列新增之前的执行）时如实说明，不补一个
 * “看起来像当时”的配置（硬约束⑤：不造假）。
 */
import { Card, Descriptions, Modal, Progress, Space, Tag, Typography } from "antd";

import {
  concurrentLabel,
  logStatusTag,
  logTriggerLabel,
  misfirePolicyLabel,
  triggerSummary,
  triggerTypeLabel,
} from "../../lib/schedule";
import { formatDuration, formatShanghaiTime } from "../../lib/time";
import type { ScheduledJobLog } from "../../types";

const { Text, Paragraph } = Typography;

function JsonBlock({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <Text type="secondary">-</Text>;
  }
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (!text.trim()) {
    return <Text type="secondary">-</Text>;
  }
  return (
    <Paragraph
      style={{
        fontSize: 12,
        fontFamily: "monospace",
        whiteSpace: "pre-wrap",
        wordBreak: "break-all",
        marginBottom: 0,
        maxHeight: 200,
        overflow: "auto",
      }}
    >
      {text}
    </Paragraph>
  );
}

/** 区块标题条（两块共用，保证视觉一致） */
function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <Text strong style={{ fontSize: 13 }}>
      {children}
    </Text>
  );
}

export type JobLogDetailModalProps = {
  open: boolean;
  log: ScheduledJobLog | null;
  /**
   * `job_key → 中文名` 表（来自 `GET /schedule/targets`）。
   *
   * ⚠️ **必须由调用方传入，弹窗里不许另写一份映射表**（DEF-044：同一语义不留两份
   * 实现）—— 与任务列表「调用目标」列共用同一份数据源，改一处两处同步。
   */
  targetNames?: Record<string, string>;
  onCancel: () => void;
};

export function JobLogDetailModal({
  open,
  log,
  targetNames = {},
  onCancel,
}: JobLogDetailModalProps) {
  const snapshot = log?.config_snapshot ?? null;
  const params = (snapshot?.params ?? null) as Record<string, unknown> | null;
  const accountIds = Array.isArray(params?.account_ids) ? (params.account_ids as number[]) : null;
  // 调用目标：**中文名在前、代码键在后**（与任务列表同口径）。
  // 快照里的 job_key 若已被注册表移除（不可能：注册表是代码侧白名单），
  // 中文名取不到 ⇒ 显示 `-`（缺值不猜，硬约束⑤）。
  const jobKey = String(snapshot?.job_key ?? "").trim()

  return (
    <Modal
      title={log ? `执行详情 · ${log.job_name}` : "执行详情"}
      open={open}
      onCancel={onCancel}
      onOk={onCancel}
      okText="关闭"
      width="70%"
      destroyOnClose
    >
      {log && (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          {/* ── 第一块：执行日志信息 ───────────────────────────────── */}
          <Card
            size="small"
            title={<SectionTitle>执行日志信息</SectionTitle>}
            styles={{ body: { paddingTop: 12 } }}
          >
            <Descriptions column={{ xs: 1, md: 2 }} size="small">
              <Descriptions.Item label="执行状态">
                {(() => {
                  const tag = logStatusTag(log.status);
                  return <Tag color={tag.color}>{tag.label}</Tag>;
                })()}
              </Descriptions.Item>
              <Descriptions.Item label="触发方式">
                {logTriggerLabel(log.trigger_type)}
              </Descriptions.Item>
              <Descriptions.Item label="开始时间">
                {formatShanghaiTime(log.started_at)}
              </Descriptions.Item>
              <Descriptions.Item label="结束时间">
                {log.finished_at ? (
                  formatShanghaiTime(log.finished_at)
                ) : (
                  <Text type="secondary">-</Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="耗时">{formatDuration(log.duration_ms)}</Descriptions.Item>
              <Descriptions.Item label="任务进度">
                <Progress
                  percent={Math.max(0, Math.min(100, log.progress ?? 0))}
                  size="small"
                  style={{ maxWidth: 220 }}
                />
              </Descriptions.Item>
              <Descriptions.Item label="消息" span={2}>
                <JsonBlock value={log.message} />
              </Descriptions.Item>
              {log.exception_info && (
                <Descriptions.Item label="异常" span={2}>
                  <Text type="danger" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
                    {log.exception_info}
                  </Text>
                </Descriptions.Item>
              )}
              {Object.keys(log.result ?? {}).length > 0 && (
                <Descriptions.Item label="执行结果" span={2}>
                  <JsonBlock value={log.result} />
                </Descriptions.Item>
              )}
            </Descriptions>
          </Card>

          {/* ── 第二块：快照配置（执行时那个任务是怎么配的） ─────────── */}
          <Card
            size="small"
            title={<SectionTitle>快照配置</SectionTitle>}
            styles={{ body: { paddingTop: 12 } }}
          >
            {snapshot ? (
              <Descriptions column={{ xs: 1, md: 2 }} size="small">
                <Descriptions.Item label="任务名称">
                  {String(snapshot.name ?? "-")}
                </Descriptions.Item>
                <Descriptions.Item label="调用目标">
                  <Space direction="vertical" size={0}>
                    <Text>{targetNames[jobKey] ?? "-"}</Text>
                    <Text code type="secondary" style={{ fontSize: 12 }}>
                      {jobKey || "-"}
                    </Text>
                  </Space>
                </Descriptions.Item>
                <Descriptions.Item label="任务类型">
                  {triggerTypeLabel(String(snapshot.trigger_type ?? ""))}
                </Descriptions.Item>
                <Descriptions.Item label="触发器" span={2}>
                  {triggerSummary({
                    trigger_type: String(snapshot.trigger_type ?? ""),
                    cron_expression: String(snapshot.cron_expression ?? ""),
                    run_at: (snapshot.run_at as string | null) ?? null,
                  })}
                </Descriptions.Item>
                <Descriptions.Item label="错过策略">
                  {misfirePolicyLabel(String(snapshot.misfire_policy ?? ""))}
                </Descriptions.Item>
                <Descriptions.Item label="并发">
                  {concurrentLabel(String(snapshot.concurrent ?? ""))}
                </Descriptions.Item>
                {accountIds !== null && (
                  <Descriptions.Item label="执行账号" span={2}>
                    {accountIds.length === 0 ? "全部账号" : `${accountIds.length} 个账号`}
                  </Descriptions.Item>
                )}
                <Descriptions.Item label="调用参数" span={2}>
                  <JsonBlock value={params} />
                </Descriptions.Item>
                {snapshot.remark ? (
                  <Descriptions.Item label="备注" span={2}>
                    {String(snapshot.remark)}
                  </Descriptions.Item>
                ) : null}
              </Descriptions>
            ) : (
              <Text type="secondary" style={{ fontSize: 12 }}>
                该次执行发生在「配置快照」功能上线之前，没有留下当时的配置记录（不补猜）。
              </Text>
            )}
          </Card>
        </Space>
      )}
    </Modal>
  );
}
