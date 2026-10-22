/**
 * **SKU 规格名称优化预览弹窗**（用户口径 2026-10-20，2026-10-21 升级）。
 *
 * 场景：第二步点「从预检通过的 SKU 导入规格 / 价格 / 库存」时，
 * 若某些 SKU 名称超出上限（默认 60 字符，汉字计 2），先调大模型压缩，
 * 再用本弹窗把**优化前后对照**摆出来给用户比较。
 *
 * 用户原话（2026-10-20）：
 * > 「优化后使用弹窗展示优化前后的对应以供用户预览比较，
 * >   如果不满意可单独对一个 sku 规格名称重新生成并覆盖历史值」
 *
 * 用户原话（2026-10-21，**本轮升级**）：
 * > 「上传前先对规格名称长度校验，小于 60 字符和 30 字，如果长度超出，调用大模型精简，
 * >   如果调用大模型失败则在预览弹窗中展示**规格图、原规格名、新规格名**，
 * >   调用失败的**新规格名红框展示且必须手动修改**，前端加长度校验，
 * >   **通过才可以关闭弹窗导入规格数据**」
 *
 * 四条实现约束：
 * ① **只影响本次导入**：确认后把最终名字交给导入流程，不回写 SKU 表。
 * ② **失败不造假**：调模型失败时 `optimized` 退回原文、带 `reason`，
 *    该行**红框**且**禁止直接确认**（必须手动改到合规）。
 * ③ **前端也要校验长度**（与后端同一口径 `displayWidth`，汉字计 2）——
 *    用户手改的新名同样可能超长，不能只信模型那一侧。
 * ④ **不合规时不许导入**：「确认导入」按钮禁用；连「取消」都要二次确认
 *    （否则用户以为「取消 = 先放着」，实际已经放弃本次导入）。
 */
import { useEffect, useMemo, useState } from "react";
import { App, Alert, Button, Input, Modal, Space, Table, Tag, Tooltip, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";

import { displayWidth } from "../../../lib/sku-name-width";
import type { SkuNameOptimizeItem } from "../../../lib/api";
import { regenerateSkuName } from "../../../lib/api";

const { Text } = Typography;

/** 弹窗里的一行：在接口结果上补「规格图」。 */
interface Row extends SkuNameOptimizeItem {
  /** 该规格名对应的规格图 URL（有则展示缩略图）。 */
  imageUrl?: string;
}

export interface SkuNameOptimizeModalProps {
  open: boolean;
  /** 上限（字符，汉字计 2）。由后端回传，保证与后端判据同源。 */
  maxChars: number;
  items: SkuNameOptimizeItem[];
  /** 规格名 → 规格图 URL（用户口径 2026-10-21：弹窗里要能看到规格图）。 */
  imageByValue?: Record<string, string>;
  /** 确认：回传**最终采用**的 `key → name` 映射（含用户重生成 / 手改过的行）。 */
  onConfirm: (applied: Record<string, string>) => void;
  onClose: () => void;
}

/** 一条名字是否**合规**（未超上限）。判据与后端 `display_width` 同口径。 */
function isNameOk(name: string, maxChars: number): boolean {
  return displayWidth(name) <= maxChars;
}

export function SkuNameOptimizeModal(props: SkuNameOptimizeModalProps) {
  //: ⚠️ 用 `App.useApp()` 拿 message（用户口径：全站消息统一走 antd message 组件，
  //: 且静态方法不吃 ConfigProvider 主题 —— 项目守卫 `test_static_antd_message_import_*`
  //: 明令禁止页面静态 `import { message } from "antd"`）。
  const { message } = App.useApp();
  const { open, maxChars, items, imageByValue, onConfirm, onClose } = props;
  const [rows, setRows] = useState<Row[]>([]);
  const [regenKey, setRegenKey] = useState<string | null>(null);

  //: 每次打开都用最新结果覆盖（重开不能带着上次的改动）。
  useEffect(() => {
    if (!open) return;
    setRows(
      items.map((it) => ({
        ...it,
        imageUrl: imageByValue?.[it.key],
      })),
    );
    setRegenKey(null);
  }, [open, items, imageByValue]);

  /** 只展示**真的需要比较**的行：超长且改动过，或失败（失败也要让用户看见）。 */
  const visible = useMemo(() => rows.filter((r) => r.changed || !!r.reason), [rows]);

  const failedCount = visible.filter((r) => !!r.reason).length;

  /** **还不合规**的行（调模型失败 / 用户手改后又超长）—— 它们决定能否确认。 */
  const blocking = useMemo(
    () => visible.filter((r) => !isNameOk(r.optimized, maxChars)),
    [visible, maxChars],
  );

  const regenerate = async (row: Row) => {
    setRegenKey(row.key);
    try {
      const result = await regenerateSkuName({ key: row.key, name: row.original });
      //: 覆盖该行（其余行不动）—— 用户口径「重新生成并覆盖历史值」。
      setRows((prev) =>
        prev.map((it) =>
          it.key === row.key
            ? {
                ...it,
                optimized: result.optimized,
                changed: result.changed,
                truncated: result.truncated,
                reason: result.reason,
              }
            : it,
        ),
      );
    } finally {
      setRegenKey(null);
    }
  };

  const confirm = () => {
    //: ⚠️ 用户口径 2026-10-21：「**通过才可以关闭弹窗导入规格数据**」——
    //: 还有超长的行时**不许**确认（不是弹个提示然后照样导）。
    if (blocking.length > 0) {
      message.error(
        `还有 ${blocking.length} 行的新规格名超出上限（${maxChars} 字符，汉字计 2），请手动修改后再导入`,
      );
      return;
    }
    //: 回传**最终值**：没改动的行用原文（导入流程不该因为「没超长」就丢掉它）。
    const applied: Record<string, string> = {};
    for (const r of rows) {
      applied[r.key] = r.optimized || r.original;
    }
    onConfirm(applied);
  };

  const handleClose = () => {
    //: 有不合规的行时，取消也要说清后果（用户可能以为「取消 = 先放着」）。
    if (blocking.length > 0) {
      Modal.confirm({
        title: "还有未通过的规格名",
        content: `有 ${blocking.length} 行超出上限，取消将放弃本次导入。确定取消？`,
        okText: "确定取消",
        cancelText: "继续修改",
        onOk: onClose,
      });
      return;
    }
    onClose();
  };

  return (
    <Modal
      open={open}
      centered
      width="76vw"
      title={`SKU 规格名称优化预览（上限 ${maxChars} 字符，汉字计 2）`}
      onCancel={handleClose}
      //: ⚠️ 有不合规行时「确认导入」按钮**禁用**（用户口径：通过才可以导入）。
      footer={
        <Space>
          <Button onClick={handleClose}>取消导入</Button>
          <Button type="primary" disabled={blocking.length > 0} onClick={confirm}>
            使用这些名称导入
          </Button>
        </Space>
      }
      className="hermes-hide-scrollbar"
      styles={{ body: { maxHeight: "62vh", overflowY: "auto" } }}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="只压缩长度，不改变语义。确认后仅本次导入使用，不会改动 SKU 表里的原始名称。"
      />
      {failedCount > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`有 ${failedCount} 条调用大模型失败（新规格名已标红），请手动修改后再导入。`}
        />
      )}
      <Table<Row>
        size="small"
        rowKey="key"
        pagination={false}
        dataSource={visible}
        locale={{ emptyText: "没有超长的 SKU 名称，无需优化" }}
        columns={[
          {
            title: "规格图",
            width: 72,
            render: (_: unknown, row) =>
              row.imageUrl ? (
                <img
                  src={row.imageUrl}
                  alt=""
                  style={{
                    width: 48,
                    height: 48,
                    objectFit: "cover",
                    borderRadius: 4,
                    border: "1px solid #d9d9d9",
                  }}
                />
              ) : (
                <Text type="secondary" style={{ fontSize: 11 }}>
                  无图
                </Text>
              ),
          },
          {
            title: "原规格名",
            dataIndex: "original",
            render: (value: string) => (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {value}
              </Text>
            ),
          },
          {
            title: "新规格名",
            dataIndex: "optimized",
            render: (value: string, row) => {
              //: ⚠️ 用户口径 2026-10-21：「调用失败的**新规格名红框展示且必须手动修改**」。
              //: 判据用**长度**（而不是只看 `reason`）—— 用户手改后可能又超长，
              //: 那种情况同样要红框（红框 = 「这一格还不合规」）。
              const bad = !isNameOk(value, maxChars);
              return (
                <Tooltip title={row.reason || undefined}>
                  <Input
                    size="small"
                    value={value}
                    status={bad ? "error" : undefined}
                    onChange={(e) => {
                      const next = e.target.value;
                      setRows((prev) =>
                        prev.map((it) =>
                          it.key === row.key
                            ? {
                                ...it,
                                optimized: next,
                                //: 改到合规 ⇒ 失败原因清掉（用户已经处理了）。
                                reason: isNameOk(next, maxChars) ? "" : it.reason,
                              }
                            : it,
                        ),
                      );
                    }}
                  />
                </Tooltip>
              );
            },
          },
          {
            title: "字数",
            width: 100,
            render: (_: unknown, row) => {
              //: 显示宽度与后端**同一口径**（汉字计 2），避免「页面说 40、后端说 72」。
              const width = displayWidth(row.optimized);
              const ok = width <= maxChars;
              return (
                <Space size={4}>
                  <Tag color={ok ? "green" : "red"}>{width}</Tag>
                  {row.truncated && (
                    <Tooltip title="大模型压得不够，已本地截断（可能略损语义）">
                      <Tag color="orange">截断</Tag>
                    </Tooltip>
                  )}
                </Space>
              );
            },
          },
          {
            title: "操作",
            width: 108,
            render: (_: unknown, row) => (
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={regenKey === row.key}
                onClick={() => void regenerate(row)}
              >
                重新生成
              </Button>
            ),
          },
        ]}
      />
    </Modal>
  );
}

export default SkuNameOptimizeModal;
