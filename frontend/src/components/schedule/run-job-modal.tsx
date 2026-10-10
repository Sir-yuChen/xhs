/**
 * 「执行一次」参数弹窗（设计 22 §5，2026-10-06 用户口径）。
 *
 * ## 什么时候弹
 *
 * 只有任务**有可编辑参数**（`params_editable` 非空）时才弹本弹窗；
 * 无参数任务走 `Popconfirm` 二次确认直接执行（用户口径「无调用参数的二次校验」）。
 *
 * ## 关键语义（必须让用户看懂）
 *
 * 弹窗里改的参数 **只对这一次执行生效，不改任务配置** —— 下一轮定时/手动仍按
 * 任务行原配置跑。所以标题下方有一行明确说明，避免用户以为改了配置。
 *
 * ## 预填
 *
 * 打开时预填任务当前 `params`（用户看到的就是「如果不改会用什么参数」），
 * 改哪个就覆盖哪个（`params_override` 与行配置合并后执行）。
 */
import { Alert, Form, Modal } from "antd";
import { useEffect, useState } from "react";

import { fetchAccounts } from "../../lib/api";
import type { PlatformAccount, ScheduledJob } from "../../types";
import { mergeParamDefaults, ParamsEditor, paramsRowsToObject } from "./params-editor";
import type { ParamRow } from "./params-editor";

export interface RunJobModalProps {
  open: boolean;
  job: ScheduledJob | null;
  submitting: boolean;
  onCancel: () => void;
  onSubmit: (params: Record<string, unknown>) => void;
}

export function RunJobModal({ open, job, submitting, onCancel, onSubmit }: RunJobModalProps) {
  const [rows, setRows] = useState<ParamRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  // 执行账号下拉的数据源（2026-10-07 用户口径）。拉不到就退空列表 ——
  // 下拉空着比拦住整次执行更合理（后端仍有归属校验兜底，不是第二道防线）。
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);

  useEffect(() => {
    if (!open) return;
    setError(null);
    // 预填任务当前参数 —— 用户看到的就是「不改会用什么」。
    // ⚠️ 2026-10-07 用户口径「调用参数不允许随意添加和删除，必须有默认值」：
    // 用**注册表默认值**作骨架（存量行缺的键也显示出来），行里已有值优先。
    setRows(mergeParamDefaults(job?.params ?? {}, job?.params_defaults ?? {}));
    void (async () => {
      try {
        setAccounts(await fetchAccounts("xhs"));
      } catch {
        setAccounts([]);
      }
    })();
  }, [open, job]);

  function handleOk() {
    // 空 key 行不发出去（后端按 key 收敛，空 key 是噪声）。
    const cleaned = rows.filter((row) => row.key.trim().length > 0);
    const emptyValue = cleaned.find((row) => !row.value.trim());
    if (emptyValue) {
      setError(`参数「${emptyValue.key}」的值不能为空。`);
      return;
    }
    setError(null);
    onSubmit(paramsRowsToObject(cleaned));
  }

  return (
    <Modal
      open={open}
      title={`执行一次：${job?.name ?? ""}`}
      okText="确定执行"
      cancelText="取消"
      centered
      confirmLoading={submitting}
      onOk={handleOk}
      onCancel={onCancel}
      // 2026-10-07 用户口径「编辑任务和新增弹窗全部重新布局，目前太长了，
      // 全屏的 70%」：与新建 / 编辑弹窗**同一尺寸口径**（宽 70% / 内容区 70vh 内滚），
      // 两个弹窗形态一致，切来切去不会觉得是两个东西。
      width="70%"
      styles={{ body: { maxHeight: "70vh", overflowY: "auto", paddingTop: 12 } }}
      destroyOnClose
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="参数只对本次执行生效，不改任务配置。"
      />
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      <Form layout="vertical">
        <Form.Item label="调用参数" extra="留空 = 按任务当前配置执行">
                    {/* 内置任务的参数行固定（不可增删）；非内置可自由增删（2026-10-07 口径）。 */}
          <ParamsEditor
            rows={rows}
            onChange={setRows}
            accounts={accounts}
            editable={!job?.is_builtin}
            allowedKeys={job?.is_builtin ? job?.params_editable ?? [] : undefined}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
