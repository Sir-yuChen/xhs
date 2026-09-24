/**
 * 调度任务的新建 / 编辑弹窗（设计 15 §9.1 / §8）。
 *
 * - 新建：调用目标 + 名称 + 调度字段 + 参数 + 备注；
 * - **账号维度目标**（`params_editable` 含 `account_ids`，首批 5 个账号维度系统任务）：
 *   **建 / 改同款「执行账号」多选**（DEF-066；空 = 本人全部账号）—— 归属校验上线后
 *   手抄 account_ids 只会换来 400 `ACCOUNT_NOT_OWNED`，控件必须跟上；其余键由代码
 *   拥有，不发（后端同口径 400 `JOB_PARAMS_NOT_EDITABLE`）。
 * - 非账号维度目标的新建：参数仍是高级用法 JSON 框（必填键提示照旧）。
 *
 * R18（2026-09-24 用户口径「调用目标应支持所有的任务目标，包括已经创建过任务
 * 的目标」）：调用目标下拉**不再硬编码**，改从 `GET /schedule/targets` 拉注册表
 * 全量；已建过任务的目标标注「已创建 N 个」但**仍可选**（同一目标可有多条不同
 * 调度的任务行）。原先只列 1 项、内置键一律 400 拒建的两条限制都已移除。
 *
 * R20（2026-09-24 用户口径「这个分组就不需要了」）：**分组字段整条移除** ——
 * 分组是代码侧归属（注册表声明），不是用户可填的东西，摆在表单里只会误导。
 */
import { Alert, Form, Input, Modal, Select, Space, Typography } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";

import { createScheduleJob, fetchAccounts, fetchScheduleTargets, updateScheduleJob } from "../../lib/api";
import type { PlatformAccount, ScheduleIn, ScheduledJob, ScheduleTarget } from "../../types";
import { ScheduleFields } from "./schedule-fields";

const { Text } = Typography;

const DEFAULT_SCHEDULE: ScheduleIn = {
  trigger_type: "cron",
  cron_expression: "0 0 9 * * *",
  misfire_policy: "once",
  concurrent: "forbid",
  enabled: true,
};

/** 兜底默认目标（targets 还没拉到时的占位；正常路径必有真值）。 */
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
  const [paramsText, setParamsText] = useState("{}");
  const [remark, setRemark] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // R18：可调度目标清单（注册表全量 + 各目标已建任务数）
  const [targets, setTargets] = useState<ScheduleTarget[]>([]);

  const selectedTarget = useMemo(
    () =>
      targets.find((item) => item.job_key === (job?.job_key ?? jobKey)) ??
      targets[0] ??
      FALLBACK_TARGET,
    [targets, job?.job_key, jobKey]
  );

  // 自定义参数：账号多选（**账号维度目标**的建 / 改同款 —— DEF-066 起新建态也有，
  // 归属校验上线后手抄 account_ids 只会换来 400，控件必须跟上；空 = 本人全部账号）
  const accountScoped = Boolean(
    (job?.params_editable ?? selectedTarget.params_editable)?.includes("account_ids")
  );
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [accountIds, setAccountIds] = useState<number[]>([]);

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
      setParamsText(JSON.stringify(job.params ?? {}, null, 2));
      setRemark(job.remark ?? "");
      setAccountIds(
        Array.isArray(job.params?.account_ids)
          ? (job.params.account_ids as number[])
          : []
      );
    } else {
      setName("");
      setJobKey(FALLBACK_TARGET.job_key);
      setSchedule(DEFAULT_SCHEDULE);
      setParamsText("{}");
      setRemark("");
      setAccountIds([]);
    }
  }, [open, job, loadTargets]);

  useEffect(() => {
    if (open && accountScoped) {
      void loadAccounts();
    }
  }, [open, accountScoped, loadAccounts]);

  async function submit() {
    setError(null);
    if (!name.trim()) {
      setError("请填写任务名称");
      return;
    }
    let params: Record<string, unknown> | undefined;
    if (accountScoped) {
      // 账号维度目标（建 / 改同一把尺子）：只发白名单里的 account_ids（空 = 本人全部账号）
      params = { account_ids: accountIds };
    } else if (!isEdit) {
      // 新建（非账号维度）：完整参数由用户给（JSON，高级用法）
      try {
        const parsed = JSON.parse(paramsText || "{}");
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("参数必须是 JSON 对象");
        }
        params = parsed;
      } catch (err) {
        setError(`参数不是合法 JSON 对象：${(err as Error).message}`);
        return;
      }
    }
    setSaving(true);
    try {
      let saved: ScheduledJob;
      if (isEdit && job) {
        saved = await updateScheduleJob(job.id, {
          name: name.trim(),
          schedule,
          ...(params ? { params } : {}),
          remark: remark.trim(),
        });
      } else {
        saved = await createScheduleJob({
          name: name.trim(),
          job_key: jobKey,
          schedule,
          params: params ?? {},
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

  return (
    <Modal
      title={isEdit ? `编辑任务 · ${job?.name ?? ""}` : "新建调度任务"}
      open={open}
      onCancel={onCancel}
      onOk={() => void submit()}
      confirmLoading={saving}
      okText="保存"
      width={640}
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
          {/* R20：分组字段整条移除，不再展示 */}
        </Form.Item>

        <ScheduleFields
          value={schedule}
          onChange={setSchedule}
          allowConcurrent={selectedTarget.allow_concurrent}
        />

        {accountScoped && (
          <Form.Item label="执行账号" extra="不选 = 本人全部账号（默认行为）">
            <Select
              mode="multiple"
              value={accountIds}
              onChange={(values: number[]) => setAccountIds(values)}
              placeholder="选择要执行的账号（可多选，仅本人账号）"
              options={accounts.map((account) => ({
                value: account.id,
                // 只展示账号名称（账号下拉纪律）：类型/状态/店铺名不进 label
                label: account.nickname || `账号 ${account.id}`,
              }))}
              optionFilterProp="label"
              style={{ width: "100%" }}
            />
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

        {!isEdit && !accountScoped && (
          <Form.Item label="调用参数">
            <Input.TextArea
              value={paramsText}
              rows={4}
              spellCheck={false}
              onChange={(event) => setParamsText(event.target.value)}
            />
            {/* R18：提示由注册表数据组装，不再写死单个目标的文案 */}
            {selectedTarget.params_required.length > 0 && (
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  格式提示：必填键 {selectedTarget.params_required.join(" / ")}（JSON
                  对象）
                </Text>
              </div>
            )}
          </Form.Item>
        )}

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
