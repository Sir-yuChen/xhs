/**
 * 调度字段输入区（设计 15 §9.2 / §9.4）——任务中心弹窗与自动运营页共用。
 *
 * 任务类型三档（2026-09-24 用户口径 R14），新建与编辑均可切换：
 * - 手动执行（manual）：不参与调度，只能靠「执行一次」立刻触发 ⇒ 不显示触发字段；
 * - Cron 周期：手输 + 「构建器」弹窗（预设一键填充）；
 * - 定时一次（at）：DatePicker 选执行时刻。
 *
 * ⚠️「间隔周期」已并入 Cron 周期（如「每 6 小时」写成 6 小时步长式 cron），
 * 不再是一档、`interval_seconds` 字段已从后端删除。
 *
 * 合法性唯一来源是后端实解析（`validateCron`）——这里只做手滑防护。
 * ⚠️ 时间一律发 **naive 本地时间**（"YYYY-MM-DDTHH:mm:ss"）：后端与
 * `shanghai_now()`（naive）比较，带时区的 ISO 串会让 pydantic 解析成 aware
 * 再比较直接 TypeError。
 */
import { ClockCircleOutlined, ToolOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  DatePicker,
  Form,
  Input,
  Radio,
  Select,
  Space,
  Typography,
} from "antd";
import { useEffect, useState } from "react";
import dayjs from "dayjs";

import { validateCron } from "../../lib/api";
import { CRON_PRESETS, cronLooksSane } from "../../lib/cron-builder";
import {
  concurrentLabel,
  misfirePolicyLabel,
  misfirePolicyTip,
  triggerTypeFullLabel,
} from "../../lib/schedule";
import type { ScheduleIn } from "../../types";
import { CronBuilderModal } from "./cron-builder-modal";

const { Text } = Typography;

/** 错过策略三档（顺序即展示顺序；文案来自 lib/schedule.ts） */
const MISFIRE_OPTIONS: Array<{ value: NonNullable<ScheduleIn["misfire_policy"]>; label: string }> = [
  { value: "fire_now", label: misfirePolicyLabel("fire_now") },
  { value: "once", label: misfirePolicyLabel("once") },
  { value: "discard", label: misfirePolicyLabel("discard") },
];

/** 默认 cron（每天 9:00）—— 与后端 AutoTask 的惰性默认同值 */
const DEFAULT_CRON = CRON_PRESETS[2].cron;

export type ScheduleFieldsProps = {
  value: ScheduleIn;
  onChange: (value: ScheduleIn) => void;
  /** 注册表是否允许并发（false 时并发选项禁用并锁 forbid） */
  allowConcurrent?: boolean;
};

export function ScheduleFields({ value, onChange, allowConcurrent = false }: ScheduleFieldsProps) {
  // 新建的行一律 cron；at / manual 由用户显式切换（编辑态保留原类型）
  const triggerType = value.trigger_type ?? "cron";
  const [cronDraft, setCronDraft] = useState(value.cron_expression ?? "");
  const [cronError, setCronError] = useState<string | null>(null);
  const [cronChecking, setCronChecking] = useState(false);
  const [cronNextTimes, setCronNextTimes] = useState<string[]>([]);
  const [builderOpen, setBuilderOpen] = useState(false);

  useEffect(() => {
    setCronDraft(value.cron_expression ?? "");
  }, [value.cron_expression]);

  function patch(next: Partial<ScheduleIn>) {
    onChange({ ...value, ...next });
  }

  async function checkCron(expression: string) {
    if (!cronLooksSane(expression)) {
      setCronError("cron 需要 6 段（秒 分 时 日 月 周）");
      setCronNextTimes([]);
      return;
    }
    setCronChecking(true);
    setCronError(null);
    try {
      const result = await validateCron(expression);
      setCronNextTimes(result.next_fire_times ?? []);
      if (!result.valid) {
        setCronError("cron 表达式不合法");
      }
    } catch (error) {
      // 后端 400 的 detail 形如 "CRON_INVALID: ..."，取冒号后的可读部分
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data
        ?.detail;
      setCronError(String(detail ?? "cron 校验失败").replace(/^CRON_INVALID:\s*/, ""));
      setCronNextTimes([]);
    } finally {
      setCronChecking(false);
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Form.Item label="任务类型" required>
        <Radio.Group
          value={triggerType}
          onChange={(event) => {
            const next = event.target.value as ScheduleIn["trigger_type"];
            if (next === "at") {
              patch({ trigger_type: next });
            } else if (next === "manual") {
              // 手动执行不参与调度：切档时清掉触发器残值（后端同口径）
              patch({ trigger_type: next, cron_expression: "", run_at: null });
            } else {
              patch({
                trigger_type: next,
                cron_expression: value.cron_expression || DEFAULT_CRON,
                run_at: null,
              });
            }
          }}
          optionType="button"
          buttonStyle="solid"
        >
          <Radio.Button value="manual">{triggerTypeFullLabel("manual")}</Radio.Button>
          <Radio.Button value="cron">{triggerTypeFullLabel("cron")}</Radio.Button>
          <Radio.Button value="at">{triggerTypeFullLabel("at")}</Radio.Button>
        </Radio.Group>
        {triggerType === "manual" && (
          <div>
            {/* 用户口径「简要明了」：原文末尾挂着设计文档章节号（设计 15 §3.1），
                那是给开发看的溯源信息，不该出现在界面上。 */}
            <Text type="secondary" style={{ fontSize: 12 }}>
              不自动运行，只能点「执行一次」触发。
            </Text>
          </div>
        )}
      </Form.Item>

      {triggerType === "at" && (
        <Form.Item label="执行时间" required>
          <DatePicker
            showTime
            style={{ width: "100%" }}
            placeholder="选择执行时刻（晚于当前时间）"
            value={value.run_at ? dayjs(value.run_at) : null}
            onChange={(date: dayjs.Dayjs | null) => {
              // naive 本地时间：后端与 shanghai_now()（naive）比较
              patch({ run_at: date ? date.format("YYYY-MM-DDTHH:mm:ss") : null });
            }}
          />
        </Form.Item>
      )}

      {triggerType === "cron" && (
        <>
          <Form.Item label="Cron 表达式" required>
            <Space.Compact style={{ width: "100%" }}>
              <Input
                value={cronDraft}
                placeholder="0 0 9 * * *（秒 分 时 日 月 周）"
                onChange={(event) => {
                  setCronDraft(event.target.value);
                  patch({ cron_expression: event.target.value });
                  setCronError(null);
                  setCronNextTimes([]);
                }}
                onBlur={(event) => {
                  if (event.target.value.trim()) {
                    void checkCron(event.target.value.trim());
                  }
                }}
              />
              <Button icon={<ToolOutlined />} onClick={() => setBuilderOpen(true)}>
                构建器
              </Button>
            </Space.Compact>
          </Form.Item>
          {cronChecking && <Text type="secondary">校验中…</Text>}
          {cronError && <Alert type="error" showIcon message={cronError} />}
          {!cronError && cronNextTimes.length > 0 && (
            <Alert
              type="success"
              showIcon
              message={
                <span>
                  <ClockCircleOutlined /> 未来触发：
                  {cronNextTimes
                    .map((time) => time.replace("T", " ").slice(0, 19))
                    .join(" ｜ ")}
                </span>
              }
            />
          )}
        </>
      )}

      <Form.Item label="错过策略">
        <Select
          value={value.misfire_policy ?? "once"}
          onChange={(policy) => patch({ misfire_policy: policy })}
          style={{ width: "100%" }}
          options={MISFIRE_OPTIONS.map((option) => ({
            value: option.value,
            label: <span title={misfirePolicyTip(option.value)}>{option.label}</span>,
          }))}
        />
      </Form.Item>

      <Form.Item label="并发执行">
        <Select
          value={allowConcurrent ? (value.concurrent ?? "forbid") : "forbid"}
          disabled={!allowConcurrent}
          onChange={(mode) => patch({ concurrent: mode })}
          style={{ width: "100%" }}
          options={[
            { value: "forbid", label: concurrentLabel("forbid") },
            { value: "allow", label: concurrentLabel("allow") },
          ]}
        />
        {!allowConcurrent && (
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              该任务类型由注册表禁止并发
            </Text>
          </div>
        )}
      </Form.Item>

      <CronBuilderModal
        open={builderOpen}
        initialCron={cronDraft || DEFAULT_CRON}
        onCancel={() => setBuilderOpen(false)}
        onOk={(cron) => {
          patch({ cron_expression: cron });
          setBuilderOpen(false);
        }}
      />
    </Space>
  );
}
