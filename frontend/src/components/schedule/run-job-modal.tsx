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
import { Alert, Form, Modal, Typography } from "antd";
import { useEffect, useState } from "react";

import type { ScheduledJob } from "../../types";
import { ParamsEditor, paramsObjectToRows, paramsRowsToObject } from "./params-editor";
import type { ParamRow } from "./params-editor";

const { Text } = Typography;

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

  useEffect(() => {
    if (!open) return;
    setError(null);
    // 预填任务当前参数 —— 用户看到的就是「不改会用什么」。
    setRows(paramsObjectToRows(job?.params ?? {}));
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
      width={640}
      destroyOnClose
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="这里的参数只对本次执行生效，不会修改任务的定时配置。"
      />
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      <Form layout="vertical">
        <Form.Item label="调用参数">
          <ParamsEditor rows={rows} onChange={setRows} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            留空表示按任务当前配置执行；多个值用逗号分隔（如执行账号 `1,2,3`）。
          </Text>
        </Form.Item>
      </Form>
    </Modal>
  );
}
