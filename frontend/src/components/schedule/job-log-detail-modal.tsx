/**
 * 调度日志详情弹窗（设计 15 §9.1；2026-09-24 用户口径 R19）。
 *
 * 布局：**上下两块**，用标题条分开（不再是原来「一块 Descriptions 里夹一个
 * 嵌套表格」的混排）——
 * 1. **执行日志信息**（当次运行的客观事实）：状态 / 触发方式 / 起止时间 /
 *    耗时 / 进度 / 消息 / 异常 / 执行结果；
 * 2. **快照配置**（当时那个任务是怎么配的）：触发器 / 策略 / 参数（含执行账号）
 *    / 备注。任务行之后被改被删都不影响。
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
  onCancel: () => void;
};

export function JobLogDetailModal({ open, log, onCancel }: JobLogDetailModalProps) {
  const snapshot = log?.config_snapshot ?? null;
  const params = (snapshot?.params ?? null) as Record<string, unknown> | null;
  const accountIds = Array.isArray(params?.account_ids) ? (params.account_ids as number[]) : null;

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
                  <Text code style={{ fontSize: 12 }}>
                    {String(snapshot.job_key ?? "-")}
                  </Text>
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
