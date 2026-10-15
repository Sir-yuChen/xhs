/**
 * cron 表达式构建器（设计 15 §9.2）。
 *
 * - 预设行一键填充（覆盖旧 auto-tasks 四档的一键等价）；
 * - 六个字段点选（每 / 指定值 / 范围 / 步长 / 列表）；
 * - 原文可随时手改；点选改动会重写原文；
 * - 「校验并预览」走后端实解析，显示未来 3 次触发时间。
 *
 * 解析/组装是纯函数（`lib/cron-builder.ts`），本组件只做状态与展示。
 */
import { Alert, Button, InputNumber, Modal, Select, Space, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";

import { validateCron } from "../../lib/api";
import {
  CRON_FIELD_LABELS,
  CRON_FIELD_OPTIONS,
  CRON_FIELD_ORDER,
  CRON_PRESETS,
  buildCron,
  emptyCronDraft,
  parseCron,
  type CronDraft,
  type FieldMode,
} from "../../lib/cron-builder";

const { Text } = Typography;

const MODE_OPTIONS: Array<{ value: FieldMode; label: string }> = [
  { value: "every", label: "每" },
  { value: "value", label: "指定值" },
  { value: "range", label: "范围" },
  { value: "step", label: "步长" },
  { value: "list", label: "列表" },
];

/** 每个模式的值输入框占位符（提示合法形态） */
const MODE_PLACEHOLDERS: Record<FieldMode, string> = {
  every: "*",
  value: "如 9",
  range: "如 1-5",
  step: "如 10/2 或 */5",
  list: "如 1,15",
};

export type CronBuilderModalProps = {
  open: boolean;
  initialCron: string;
  onOk: (cron: string) => void;
  onCancel: () => void;
};

export function CronBuilderModal({ open, initialCron, onOk, onCancel }: CronBuilderModalProps) {
  const [raw, setRaw] = useState(initialCron);
  const [draft, setDraft] = useState<CronDraft>(() => parseCron(initialCron) ?? emptyCronDraft());
  const [structured, setStructured] = useState(() => parseCron(initialCron) !== null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nextTimes, setNextTimes] = useState<string[]>([]);

  useEffect(() => {
    if (!open) {
      return;
    }
    setRaw(initialCron);
    const parsed = parseCron(initialCron);
    setStructured(parsed !== null);
    setDraft(parsed ?? emptyCronDraft());
    setError(null);
    setNextTimes([]);
  }, [open, initialCron]);

  const built = useMemo(() => buildCron(draft), [draft]);

  function updateField(key: keyof CronDraft, next: Partial<CronDraft[keyof CronDraft]>) {
    setDraft((current) => {
      const updated = { ...current, [key]: { ...current[key], ...next } };
      setRaw(buildCron(updated));
      return updated;
    });
    setError(null);
    setNextTimes([]);
  }

  async function check() {
    setChecking(true);
    setError(null);
    try {
      const result = await validateCron(raw.trim());
      setNextTimes(result.next_fire_times ?? []);
      if (!result.valid) {
        setError("cron 表达式不合法");
      }
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(String(detail ?? "校验失败").replace(/^CRON_INVALID:\s*/, ""));
      setNextTimes([]);
    } finally {
      setChecking(false);
    }
  }

  return (
    <Modal
            centered
            title="Cron 表达式构建器"
      open={open}
      // 2026-10-07 用户口径「编辑任务和新增弹窗全部重新布局……全屏的 70%」：
      // 它是新建 / 编辑弹窗里打开的**子弹窗** ⇒ 同一尺寸口径（宽 70% / 内容区 70vh 内滚）。
      width="70%"
      styles={{ body: { maxHeight: "70vh", overflowY: "auto" } }}
      onCancel={onCancel}
      onOk={() => onOk(raw.trim())}
      okText="使用该表达式"
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <div>
          <Text type="secondary">常用预设（一键填充）：</Text>
          <div style={{ marginTop: 8 }}>
            <Space wrap>
              {CRON_PRESETS.map((preset) => (
                <Button
                  key={preset.cron}
                  size="small"
                  type={raw.trim() === preset.cron ? "primary" : "default"}
                  onClick={() => {
                    setRaw(preset.cron);
                    const parsed = parseCron(preset.cron);
                    setStructured(parsed !== null);
                    setDraft(parsed ?? emptyCronDraft());
                    setError(null);
                    setNextTimes([]);
                  }}
                >
                  {preset.label}
                </Button>
              ))}
            </Space>
          </div>
        </div>

        {structured ? (
          <div>
            <Text type="secondary">点选构建（秒 分 时 日 月 周）：</Text>
            {CRON_FIELD_ORDER.map((key) => {
              const state = draft[key];
              const options = CRON_FIELD_OPTIONS[key];
              // step 形态拆成「起始值 + 步长」两个控件，组装成 `起始/步长`
              const [stepStart = "*", stepEvery = "1"] = state.value.includes("/")
                ? state.value.split("/")
                : ["*", "1"];
              return (
                <div
                  key={key}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    marginTop: 8,
                  }}
                >
                  <Text style={{ width: 32 }}>{CRON_FIELD_LABELS[key]}</Text>
                  <Select
                    size="small"
                    value={state.mode}
                    style={{ width: 110 }}
                    onChange={(mode: FieldMode) => {
                      if (mode === "step") {
                        updateField(key, { mode, value: `*/${stepEvery}` });
                      } else {
                        updateField(key, { mode });
                      }
                    }}
                    options={MODE_OPTIONS}
                  />
                  {state.mode === "step" && (
                    <>
                      <Select
                        size="small"
                        style={{ width: 110 }}
                        value={stepStart}
                        onChange={(value: string) =>
                          updateField(key, { value: `${value}/${stepEvery}` })
                        }
                        options={[
                          { value: "*", label: "从每段起" },
                          ...options.filter(
                            (option) => Number(option.value) % 1 === 0
                          ),
                        ]}
                      />
                      <Text type="secondary">每</Text>
                      <InputNumber
                        size="small"
                        style={{ width: 72 }}
                        min={1}
                        value={Number(stepEvery) || 1}
                        onChange={(value) => {
                          const every = String(Number(value) || 1);
                          updateField(key, { value: `${stepStart}/${every}` });
                        }}
                      />
                      <Text type="secondary">{CRON_FIELD_LABELS[key]}一次</Text>
                    </>
                  )}
                  {state.mode === "list" && (
                    <Select
                      size="small"
                      mode="multiple"
                      style={{ minWidth: 220, flex: 1 }}
                      placeholder="可多选"
                      value={state.value ? state.value.split(",") : []}
                      onChange={(values: string[]) =>
                        updateField(key, { value: values.join(",") })
                      }
                      options={options}
                    />
                  )}
                  {(state.mode === "value" || state.mode === "range") && (
                    <Select
                      size="small"
                      style={{ minWidth: 220, flex: 1 }}
                      placeholder={MODE_PLACEHOLDERS[state.mode]}
                      value={
                        options.some((option) => option.value === state.value)
                          ? state.value
                          : undefined
                      }
                      onChange={(value: string) => updateField(key, { value })}
                      options={options}
                    />
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <Alert
            type="info"
            showIcon
            message="表达式含混合形态，已切换为原文编辑"
          />
        )}

        <div>
          <Text type="secondary">表达式原文：</Text>
          <Space.Compact style={{ width: "100%", marginTop: 8 }}>
            <input
              className="ant-input"
              value={raw}
              onChange={(event) => {
                setRaw(event.target.value);
                const parsed = parseCron(event.target.value);
                setStructured(parsed !== null);
                if (parsed) {
                  setDraft(parsed);
                }
                setError(null);
                setNextTimes([]);
              }}
            />
            <Button loading={checking} onClick={() => void check()}>
              校验并预览
            </Button>
          </Space.Compact>
        </div>

        {error && <Alert type="error" showIcon message={error} />}
        {!error && nextTimes.length > 0 && (
          <Alert
            type="success"
            showIcon
            message={`未来触发：${nextTimes
              .map((time) => time.replace("T", " ").slice(0, 19))
              .join(" ｜ ")}`}
          />
        )}
        {structured && <Text type="secondary">点选结果：{built}</Text>}
      </Space>
    </Modal>
  );
}
