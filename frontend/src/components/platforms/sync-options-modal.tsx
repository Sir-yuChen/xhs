/**
 * 同步选项弹窗（设计 13 §23，2026-09-22 用户口径：
 * 「新增弹窗支持根据时间区间或全量同步，让用户自己决定同步哪些数据」）。
 *
 * 为什么要有这个弹窗：以前点「同步」就直接跑，用户没法决定同步哪些数据。
 * 全量一次 30–45 分钟（1961 条笔记 = ~66 页 × 5 次/分钟限流），如果只是想补
 * 上个月的笔记，被迫全量扫一遍既慢又白烧平台额度。
 *
 * 两个选项的口径（与后端 `sync_task_service.resolve_time_window` 严格一致）：
 * * **全量同步**：不设时间界，翻到平台没有下一页为止；
 * * **按时间区间**：只取 `[起始, 结束]` 之间**发布**的数据，结束日期含当天一整天。
 *
 * ⚠️ 区间写反了由后端交换（不漏数据）；日期缺失 / 不可解析时后端退回全量并在
 * 任务行里说明 —— **不静默**按全量跑，那会让用户以为自己在同步一个区间。
 */
import { useEffect, useState } from "react";
import { Alert, Button, DatePicker, Modal, Radio, Space, Typography } from "antd";
import dayjs, { type Dayjs } from "dayjs";

export type SyncScope = "full" | "range";

export interface SyncOptions {
  scope: SyncScope;
  /** `YYYY-MM-DD`；`scope=full` 时为空串。 */
  startDate: string;
  /** `YYYY-MM-DD`；`scope=full` 时为空串。 */
  endDate: string;
}

const { Text } = Typography;

/** 今天（本地时区）的 `YYYY-MM-DD`，用作结束日期的默认值。 */
function today(): string {
  return dayjs().format("YYYY-MM-DD");
}

export function SyncOptionsModal(props: {
  open: boolean;
  title: string;
  /** 说明这轮同步的数据量级，帮用户判断该选全量还是区间。 */
  hint?: string;
  confirmText?: string;
  onCancel: () => void;
  onConfirm: (options: SyncOptions) => void;
}) {
  const { open, title, hint, confirmText = "开始同步", onCancel, onConfirm } = props;
  const [scope, setScope] = useState<SyncScope>("range");
  const [startDate, setStartDate] = useState<string>("");
  const [endDate, setEndDate] = useState<string>(today());

  // 每次打开都回到默认态：不残留上一次的选择（残留 = 用户误同步了别的区间）。
  useEffect(() => {
    if (open) {
      setScope("range");
      setEndDate(today());
      setStartDate("");
    }
  }, [open]);

  const start: Dayjs | null = startDate ? dayjs(startDate) : null;
  const end: Dayjs | null = endDate ? dayjs(endDate) : null;
  const reversed = Boolean(start && end && start.isAfter(end));
  const missing = scope === "range" && !startDate;

  function handleConfirm() {
    if (scope === "full") {
      onConfirm({ scope: "full", startDate: "", endDate: "" });
      return;
    }
    if (missing) {
      return; // 按钮已 disabled，双保险
    }
    onConfirm({
      scope: "range",
      // 起止写反时**在前端就交换**：与后端同一口径，用户看到的就是实际生效的区间。
      startDate: reversed ? endDate : startDate,
      endDate: reversed ? startDate : endDate,
    });
  }

  return (
    <Modal
            centered
            open={open}
      title={title}
      onCancel={onCancel}
      onOk={() => handleConfirm()}
      okText={confirmText}
      cancelText="取消"
      confirmLoading={false}
      okButtonProps={{ disabled: missing }}
      destroyOnClose
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {hint && <Alert type="info" showIcon message={hint} />}

        <Radio.Group
          value={scope}
          onChange={(e) => setScope(e.target.value as SyncScope)}
          style={{ width: "100%" }}
        >
          <Space direction="vertical" style={{ width: "100%" }}>
            <Radio value="range">按时间区间同步</Radio>
            <Radio value="full">全量同步（翻到平台没有下一页为止）</Radio>
          </Space>
        </Radio.Group>

        {scope === "range" && (
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            <Space wrap>
              <DatePicker
                value={start}
                onChange={(d) => setStartDate(d ? d.format("YYYY-MM-DD") : "")}
                placeholder="起始日期"
                allowClear
              />
              <Text type="secondary">至</Text>
              <DatePicker
                value={end}
                onChange={(d) => setEndDate(d ? d.format("YYYY-MM-DD") : today())}
                placeholder="结束日期"
                allowClear={false}
              />
            </Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              只同步这个时间段内发布的内容；结束日期含当天一整天。
            </Text>
            {missing && (
              <Alert type="warning" showIcon message="请选择起始日期，或改用「全量同步」。" />
            )}
            {reversed && (
              <Alert
                type="warning"
                showIcon
                message="起始日期晚于结束日期，将按交换后的区间同步。"
              />
            )}
          </Space>
        )}

        {scope === "full" && (
          <Alert
            type="warning"
            showIcon
            message="全量同步会翻完整个列表，耗时较长（数据多时可能几十分钟），且更耗平台请求额度。"
          />
        )}
      </Space>
    </Modal>
  );
}
