/**
 * 调度任务的新建 / 编辑弹窗（设计 15 §9.1 / §8）。
 *
 * - 新建：调用目标 + 名称 + 调度字段 + 参数（KV 输入框）+ 备注；
 * - 编辑：同上，调用目标禁用（不可改 key）。
 *
 * ## 调用参数 = KV 输入框（2026-10-06 用户口径）
 *
 * 「定时任务的调用参数新增和修改使用 kv 输入框配置，kv 必填；编辑弹窗也使
 * key，v 为多个值时使用逗号分隔（如执行账号）」。所以**所有**任务的调用参数
 * 统一成一套 KV 编辑器：
 *
 * * 每一行 = key（从注册表 `params_editable ∪ params_required` 选）+ value；
 * * **key / value 都必填**（不能留空行）；
 * * value 多值用**逗号分隔**（如 `account_ids = "1,2,3"`），后端按 key 类型解析；
 * * key 白名单由后端下发（`params_editable`），前端**不硬编码键名** —— 新增
 *   参数只改注册表，前端零改动。
 *
 * 键的**展示名**（label）这里给一份回退映射；后端没下发展示名时用 key 本身。
 * 已知键：`account_ids` 执行账号 / `retention_days` 记录表保留天数 /
 * `days` 同步时间区间（天）/ `auto_task_id` 自动任务 ID。
 *
 * R18（2026-09-24 用户口径「调用目标应支持所有的任务目标，包括已经创建过任务
 * 的目标」）：调用目标下拉**不再硬编码**，改从 `GET /schedule/targets` 拉注册表
 * 全量；已建过任务的目标标注「已创建 N 个」但**仍可选**。
 *
 * R20（2026-09-24 用户口径「这个分组就不需要了」）：**分组字段整条移除**。
 */
import { Alert, Button, Form, Input, Modal, Select, Space, Typography } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";

import { createScheduleJob, fetchAccounts, fetchScheduleTargets, updateScheduleJob } from "../../lib/api";
import type { PlatformAccount, ScheduleIn, ScheduledJob, ScheduleTarget } from "../../types";
import {
  ParamsEditor,
  paramsObjectToRows,
  paramsRowsToObject,
} from "./params-editor";
import type { ParamRow } from "./params-editor";
import { ScheduleFields } from "./schedule-fields";

const { Text } = Typography;

const DEFAULT_SCHEDULE: ScheduleIn = {
  trigger_type: "cron",
  cron_expression: "0 0 9 * * *",
  misfire_policy: "once",
  concurrent: "forbid",
  enabled: true,
};

const FALLBACK_TARGET: ScheduleTarget = {
  job_key: "auto_ops.keyword_pipeline",
  name: "自动运营管线",
  builtin: false,
  allow_concurrent: false,
  params_required: ["auto_task_id"],
  params_editable: [],
  default_trigger_type: "cron",
  default_cron: "0 0 9 * * *",
  default_misfire_policy: "once",
  existing_count: 0,
};

/** 参数 key → 展示名（**回退**用；后端将来下发 label 时优先用后端的）。 */
export const PARAM_LABELS: Record<string, string> = {
  account_ids: "执行账号",
  retention_days: "记录表保留天数",
  days: "同步时间区间（天）",
  auto_task_id: "自动任务 ID",
};

export type JobFormModalProps = {
  open: boolean;
  /** 传了 = 编辑模式；不传 = 新建 */
  job?: ScheduledJob | null;
  onCancel: () => void;
  onSaved: (job: ScheduledJob) => void;
};

export function JobFormModal({ open, job, onCancel, onSaved }: JobFormModalProps) {
  const isEdit = Boolean(job);
  const [name, setName] = useState("");
  const [jobKey, setJobKey] = useState(FALLBACK_TARGET.job_key);
  const [schedule, setSchedule] = useState<ScheduleIn>(DEFAULT_SCHEDULE);
  const [paramRows, setParamRows] = useState<ParamRow[]>([]);
  const [remark, setRemark] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // R18：可调度目标清单（注册表全量 + 各目标已建任务数）
  const [targets, setTargets] = useState<ScheduleTarget[]>([]);
  // 账号多选的数据源（仅 `account_ids` 键用）；KV 输入框 value 填逗号分隔的 id，
  // 但给用户一个账号名提示（extra 里列「账号 id = 昵称」）。
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);

  const selectedTarget = useMemo(
    () =>
      targets.find((item) => item.job_key === (job?.job_key ?? jobKey)) ??
      targets[0] ??
      FALLBACK_TARGET,
    [targets, job?.job_key, jobKey]
  );

  // 可编辑参数键（白名单 + 必填键）—— KV 编辑器据此渲染每一行。
  const editableKeys = useMemo(() => {
    const keys = new Set<string>();
    (job?.params_editable ?? selectedTarget.params_editable ?? []).forEach((k) => keys.add(k));
    (job ? [] : selectedTarget.params_required ?? []).forEach((k) => keys.add(k));
    return [...keys];
  }, [job, selectedTarget]);

  const loadTargets = useCallback(async () => {
    try {
      const items = await fetchScheduleTargets();
      setTargets(items);
    } catch {
      setTargets([]);
    }
  }, []);

  const loadAccounts = useCallback(async () => {
    try {
      const items = await fetchAccounts("xhs");
      setAccounts(items);
    } catch {
      setAccounts([]);
    }
  }, []);

  useEffect(() => {
    if (!open) {
      return;
    }
    setError(null);
    void loadTargets();
    if (job) {
      setName(job.name);
      setJobKey(job.job_key);
      setSchedule({
        trigger_type: job.trigger_type as ScheduleIn["trigger_type"],
        cron_expression: job.cron_expression,
        run_at: job.run_at ?? null,
        misfire_policy: job.misfire_policy as ScheduleIn["misfire_policy"],
        concurrent: job.concurrent as ScheduleIn["concurrent"],
        enabled: job.status === "active",
      });
      setParamRows(paramsObjectToRows(job.params ?? {}));
      setRemark(job.remark ?? "");
    } else {
      setName("");
      setJobKey(FALLBACK_TARGET.job_key);
      setSchedule(DEFAULT_SCHEDULE);
      setParamRows([]);
      setRemark("");
    }
  }, [open, job, loadTargets]);

  useEffect(() => {
    if (open && editableKeys.includes("account_ids")) {
      void loadAccounts();
    }
  }, [open, editableKeys, loadAccounts]);

  async function submit() {
    setError(null);
    if (!name.trim()) {
      setError("请填写任务名称");
      return;
    }
    // KV 必填校验（用户口径「kv 必填」）：key 和 value 都不能空。
    for (const row of paramRows) {
      if (!row.key.trim()) {
        setError("参数 key 不能为空");
        return;
      }
      if (!row.value.trim()) {
        setError(`参数「${PARAM_LABELS[row.key] ?? row.key}」的 value 不能为空`);
        return;
      }
    }
    const params = paramsRowsToObject(paramRows);
    setSaving(true);
    try {
      let saved: ScheduledJob;
      if (isEdit && job) {
        saved = await updateScheduleJob(job.id, {
          name: name.trim(),
          schedule,
          ...(Object.keys(params).length > 0 ? { params } : {}),
          remark: remark.trim(),
        });
      } else {
        saved = await createScheduleJob({
          name: name.trim(),
          job_key: jobKey,
          schedule,
          params,
          remark: remark.trim(),
        });
      }
      onSaved(saved);
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data
        ?.detail;
      setError(String(detail ?? "保存失败"));
    } finally {
      setSaving(false);
    }
  }

  // 账号名提示（account_ids 值逗号分隔时，给用户一个「id = 昵称」对照）。
  const accountHint = useMemo(() => {
    if (!editableKeys.includes("account_ids") || accounts.length === 0) return "";
    return accounts
      .map((a) => `${a.id}=${a.nickname || `账号${a.id}`}`)
      .join("，");
  }, [editableKeys, accounts]);

  return (
    <Modal
      centered
      title={isEdit ? `编辑任务 · ${job?.name ?? ""}` : "新建调度任务"}
      open={open}
      onCancel={onCancel}
      onOk={() => void submit()}
      confirmLoading={saving}
      okText="保存"
      width={680}
      destroyOnClose
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {error && <Alert type="error" showIcon message={error} />}

        <Form.Item label="任务名称" required>
          <Input
            value={name}
            maxLength={128}
            placeholder="给任务起个能认出来的名字"
            onChange={(event) => setName(event.target.value)}
          />
        </Form.Item>

        <Form.Item label="调用目标">
          <Select
            value={job?.job_key ?? jobKey}
            disabled={isEdit}
            onChange={(value) => setJobKey(value)}
            showSearch
            optionFilterProp="label"
            options={targets.map((item) => ({
              value: item.job_key,
              label: item.name,
            }))}
          />
          {/* R18：已建过任务的目标仍可选，只在下方标一句「已创建 N 个」 */}
          {!isEdit && selectedTarget.existing_count > 0 && (
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                该目标已有 {selectedTarget.existing_count} 个任务，仍可再建（同一目标可以有
                多条不同调度的任务）
              </Text>
            </div>
          )}
        </Form.Item>

        <ScheduleFields
          value={schedule}
          onChange={setSchedule}
          allowConcurrent={selectedTarget.allow_concurrent}
        />

        {/* 调用参数 = KV 输入框（**key 与 value 都是自由文本**，2026-10-06 用户口径
            「k 不要使用下拉框选择」）。共用 `ParamsEditor`，与「执行一次」弹窗同一实现。 */}
        {editableKeys.length > 0 && (
          <Form.Item
            label="调用参数"
            required
            extra={
              editableKeys.includes("account_ids") && accountHint
                ? `账号对照：${accountHint}`
                : `可用参数：${editableKeys.join(" / ")}`
            }
          >
            <ParamsEditor rows={paramRows} onChange={setParamRows} />
          </Form.Item>
        )}

        <Form.Item label="启用调度">
          <Select
            value={schedule.enabled ? "on" : "off"}
            onChange={(value) => setSchedule({ ...schedule, enabled: value === "on" })}
            style={{ width: 200 }}
            options={[
              { value: "on", label: "启用（按触发器自动执行）" },
              { value: "off", label: "暂停（仅保留配置，可手动执行一次）" },
            ]}
          />
        </Form.Item>

        <Form.Item label="备注">
          <Input
            value={remark}
            maxLength={500}
            onChange={(event) => setRemark(event.target.value)}
          />
        </Form.Item>
      </Space>
    </Modal>
  );
}
