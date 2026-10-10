/**
 * 调度任务的新建 / 编辑弹窗（设计 15 §9.1 / §8）。
 *
 * - 新建：调用目标 + 名称 + 调度字段 + 参数（KV 输入框）+ 备注；
 * - 编辑：同上，调用目标禁用（不可改 key）。
 *
 * ## 调用参数 = KV 输入框（2026-10-06 用户口径；2026-10-07 修订）
 *
 * 「定时任务的调用参数新增和修改使用 kv 输入框配置，kv 必填；编辑弹窗也使
 * key，v 为多个值时使用逗号分隔（如执行账号）」。所以**所有**任务的调用参数
 * 统一成一套 KV 编辑器：
 *
 * * 每一行 = key（自由文本）+ value；
 * * **key / value 都必填**（不能留空行）；
 * * 值多值的键（如 `account_ids`）用**逗号分隔**存储，提交时按 key 类型解析；
 * * key 白名单由后端下发（`params_editable`），前端**不硬编码键名** —— 新增
 *   参数只改注册表，前端零改动。
 *
 * 🔄 **2026-10-07 用户口径「优化定时任务，当调用参数为执行账号时使用下拉复选框」**：
 * `account_ids` 那一行的 value 由**自由文本改为账号下拉多选**（原先另有一行
 * 「账号对照：id=昵称」提示，现在名字就在选项里 ⇒ 那行**删除**，不留第二份真相源）。
 * 其余键的 value 仍是自由文本。
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
import { Alert, Card, Col, Form, Input, Modal, Row, Select, Space, Typography } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";

import { createScheduleJob, fetchAccounts, fetchScheduleTargets, updateScheduleJob } from "../../lib/api";
import type { PlatformAccount, ScheduleIn, ScheduledJob, ScheduleTarget } from "../../types";
import {
  mergeParamDefaults,
  PARAM_LABELS,
  ParamsEditor,
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
  // 引用型参数（`auto_task_id`）无合理默认值 —— 必填，故默认值表为空。
  params_defaults: {},
  default_trigger_type: "cron",
  default_cron: "0 0 9 * * *",
  default_misfire_policy: "once",
  existing_count: 0,
};

/** 参数 key → 展示名（**回退**用；后端将来下发 label 时优先用后端的）。 */
// `PARAM_LABELS` 已下移到 `params-editor.tsx`（2026-10-07）—— 参数行的**中文名**现在
// 由编辑器的行首只读列渲染，那份映射必须与渲染处同源。此处**透传**，
// 既有 `import { PARAM_LABELS } from "./job-form-modal"` 的调用方零改动。
export { PARAM_LABELS } from "./params-editor";

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

  // 注册表下发的参数默认值（键集合 = params_editable）—— 弹窗据此**预填固定行**。
  const selectedTargetDefaults = useMemo(
    () => (job?.params_defaults ?? selectedTarget.params_defaults ?? {}) as Record<string, unknown>,
    [job, selectedTarget]
  );

  // 当前目标的「内置」判定（决定参数行可否增删、任务可否删除）。
  const isBuiltinTarget = useMemo(
    () => (job ? job.is_builtin : Boolean(selectedTarget.builtin)),
    [job, selectedTarget]
  );

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
      // 2026-10-07 用户口径「调用参数不允许随意添加和删除，必须有默认值」：
      // 行的**键集合来自注册表**（`params_defaults` 的键，后端已按白名单下发），
      // 值优先取行里存的 —— 存量行缺的键（早于该参数上线）也会**显示默认值**，
      // 这正是「新增了参数但编辑弹窗里看不见」的修法。
      setParamRows(mergeParamDefaults(job.params ?? {}, selectedTargetDefaults));
      setRemark(job.remark ?? "");
    } else {
      setName("");
      setJobKey(FALLBACK_TARGET.job_key);
      setSchedule(DEFAULT_SCHEDULE);
      setParamRows(mergeParamDefaults({}, selectedTargetDefaults));
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
    // value 必填校验（用户口径「kv 必填」）。
    // ⚠️ 2026-10-07：**不再校验 key** —— key 由注册表决定且渲染成只读，恒有值；
    // 留着那条判据只会给「行没建出来」这种情况一个误导性的错误文案。
    for (const row of paramRows) {
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

  return (
    <Modal
      centered
      title={isEdit ? `编辑任务 · ${job?.name ?? ""}` : "新建调度任务"}
      open={open}
      onCancel={onCancel}
      onOk={() => void submit()}
      confirmLoading={saving}
      okText="保存"
      // 2026-10-07 用户口径「目前太长了，全屏的 70%」：宽 = 视口 70%
      //（与执行详情弹窗同一口径，不写死像素）；高 = 视口 70%，**内容区内滚**
      //（表单再长也不把弹窗顶出屏幕）。
      width="70%"
      styles={{ body: { maxHeight: "70vh", overflowY: "auto", paddingTop: 12 } }}
      destroyOnClose
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {error && <Alert type="error" showIcon message={error} />}

        {/* ── 两栏主体：左「调度配置」/ 右「调用参数」（2026-10-07 用户口径
            「编辑任务和新增弹窗全部重新布局，目前太长了，全屏的 70%」）──
            分栏理由：两块回答的是**不同问题**（什么时候跑 / 跑的时候用什么），
            与执行详情弹窗「日志信息 / 快照配置」分块同一条原则（设计 15 §9.1.1）。 */}
        <Row gutter={16} align="top">
          <Col span={12}>
            <Card size="small" title="调度配置" styles={{ body: { paddingTop: 8 } }}>
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
                {/* R18：已建过任务的目标仍可选，只标一句「已有 N 个」 */}
                {!isEdit && selectedTarget.existing_count > 0 && (
                  <div>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      已有 {selectedTarget.existing_count} 个任务，仍可再建
                    </Text>
                  </div>
                )}
              </Form.Item>

              <ScheduleFields
                value={schedule}
                onChange={setSchedule}
                allowConcurrent={selectedTarget.allow_concurrent}
              />

              <Form.Item label="启用调度">
                <Select
                  value={schedule.enabled ? "on" : "off"}
                  onChange={(value) => setSchedule({ ...schedule, enabled: value === "on" })}
                  style={{ width: 200 }}
                  options={[
                    { value: "on", label: "启用" },
                    { value: "off", label: "暂停" },
                  ]}
                />
              </Form.Item>
            </Card>
          </Col>

          <Col span={12}>
            <Card size="small" title="调用参数" styles={{ body: { paddingTop: 8 } }}>
              {editableKeys.length > 0 ? (
                <>
                                    {/**
                   * 2026-10-07 用户口径「**系统内置**任务的调用参数不可删除不可添加；
                   * **非系统内置**任务可删除、可添加调用参数」：
                   *   * 内置 ⇒ `editable=false` + 白名单过滤（行固定、来自注册表）；
                   *   * 非内置 ⇒ `editable=true` 且**不过滤**（用户自己加键）。
                   */}
                  <ParamsEditor
                    rows={paramRows}
                    onChange={setParamRows}
                    accounts={accounts}
                    editable={!isBuiltinTarget}
                    allowedKeys={isBuiltinTarget ? editableKeys : undefined}
                  />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    可用参数：{editableKeys.map((k) => PARAM_LABELS[k] ?? k).join(" / ")}
                  </Text>
                </>
              ) : (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  该任务没有可配置参数
                </Text>
              )}
            </Card>
          </Col>
        </Row>

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
