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
 *
 * ⚠️ **「执行账号」分两态**（2026-09-27 用户口径「执行账号应是当前账号信息卡的店铺」）：
 * 账号卡按钮建的那条日志带 `params_override`（定向执行）⇒ 显示**那一个账号的名称**；
 * 定时 / 手动「执行一次」的日志没有这一层 ⇒ 仍按任务配置显示「全部账号 / N 个账号」。
 * 详见下方 `targetAccountId` 处的注释。
 */
import { Card, Descriptions, Modal, Space, Tag, Typography } from "antd";

import { JobLogProgress } from "./job-log-progress";
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
import type { AccountNameMap } from "../../lib/task-records";

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
  /**
   * `账号 id → 账号名` 表（来自 `fetchAccounts()` + lib 的 `accountNameMap()`）。
   *
   * 用户口径（2026-10-07）：「快照配置区域的执行账号应展示**具体执行了哪些账号**」——
   * 快照里只有 id（`params.account_ids`），名字得由调用方注入。
   * ⚠️ 与任务记录页**同一份实现**（`AccountNameMap` + `accountNameMap()`），不另写映射。
   * 拉不到 ⇒ 退 `账号 #id`（缺值不猜，硬约束⑤）。
   */
  accountNames?: AccountNameMap;
  onCancel: () => void;
};

export function JobLogDetailModal({
  open,
  log,
  targetNames = {},
  accountNames = {},
  onCancel,
}: JobLogDetailModalProps) {
  const snapshot = log?.config_snapshot ?? null;
  const params = (snapshot?.params ?? null) as Record<string, unknown> | null;
  /** 本次点击实际生效的语义（账号卡链路才有；cron 执行没有这一层）。 */
  const override = (snapshot?.params_override ?? null) as Record<string, unknown> | null;
  const accountIds = Array.isArray(params?.account_ids) ? (params.account_ids as number[]) : null;
  /**
   * **定向执行**（账号卡「同步笔记 / 同步商品」按钮）本次实际跑的账号。
   *
   * 唯一来源是 `config_snapshot.params_override`（设计 13 §35.17.2 的记账口径）——
   * 快照块里的 `params.account_ids` 在定向执行时已被后端收窄成同一个账号，但那里只有
   * **id 数量**，说不出「是哪个账号」；`account_name` 是执行那一刻的账号名称快照。
   *
   * ⚠️ 不能再按 `params.account_ids` 判「全部账号」：2026-09-27 用户实测报障
   * 「账号信息卡点同步笔记，执行详情的执行账号依旧展示的是全部账号」—— 那是 job 行的
   * **任务配置**（空 = 本人全部账号），而这一次只跑了一个账号。
   */
  const targetAccountId =
    typeof override?.account_id === "number" ? (override.account_id as number) : null;
  const targetAccountName = targetAccountId === null ? "" : String(override?.account_name ?? "").trim();
  // 调用目标：**中文名在前、代码键在后**（与任务列表同口径）。
  // 快照里的 job_key 若已被注册表移除（不可能：注册表是代码侧白名单），
  // 中文名取不到 ⇒ 显示 `-`（缺值不猜，硬约束⑤）。
  const jobKey = String(snapshot?.job_key ?? "").trim()

  return (
    <Modal
            centered
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
                {/* ⚠️ **必须与任务日志列表列用同一个组件**（DEF-044，设计 13 §43）：
                    旧实现这里直接读 `log.progress ?? 0`，于是「在跑且后端还没上报」时
                    弹窗显示 0%，而列表列当时画的是满格条（看起来 100%）——
                    同一行数据两个相反读数，用户 2026-09-28 实测报障。
                    共用一个组件后，「未知」在两处都是同一种呈现（不定量滑动条）。 */}
                <JobLogProgress log={log} size="default" maxWidth={220} />
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
                {targetAccountId !== null ? (
                  // 定向执行（账号卡按钮）⇒ 说清**这一个**账号是谁（名称取执行那一刻的快照；
                  // 名称缺失（存量日志 / 账号已改名前的行）⇒ 退「1 个账号」，不猜名字）。
                  <Descriptions.Item label="执行账号" span={2}>
                    {targetAccountName || "1 个账号"}
                  </Descriptions.Item>
                ) : accountIds !== null ? (
                  // 定时 / 手动「执行一次」：**逐账号列出具体是谁**（2026-10-07 用户口径
                  // 「快照配置区域的执行账号应展示具体执行了哪些账号」）。
                  //
                  // 口径与上面那条的差异：任务配置里的 `account_ids` **空 = 本人全部账号**
                  // —— 那种情况说不出具体是谁（快照里没有账号集合），**照旧显「全部账号」**，
                  // 不猜、不临时去查库（详情弹窗是只读回看，不该产生新的取数语义）。
                  <Descriptions.Item label="执行账号" span={2}>
                    {accountIds.length === 0 ? (
                      "全部账号"
                    ) : (
                      <Space direction="vertical" size={0}>
                        {accountIds.map((id) => (
                          // ⚠️ **只展示账号名称，不展示 id**（2026-10-07 用户口径）。
                          // 名字取自 `accountNames`（lib 的 `accountNameMap`）；
                          // 取不到才退 `账号 #id` —— 那是**兜底**不是常态（缺值不猜，
                          // 但也不能显示空白，否则用户看不出这条日志跑了哪个账号）。
                          <Text key={id}>{accountNames[String(id)] || `账号 #${id}`}</Text>
                        ))}
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          共 {accountIds.length} 个账号
                        </Text>
                      </Space>
                    )}
                  </Descriptions.Item>
                ) : null}
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
