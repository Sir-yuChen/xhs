/**
 * **SKU 规格名称优化预览弹窗**（用户口径 2026-10-20）。
 *
 * 场景：第二步点「从预检通过的 SKU 导入规格 / 价格 / 库存」时，
 * 若某些 SKU 名称超出上限（默认 60 字符，汉字计 2），先调大模型压缩，
 * 再用本弹窗把**优化前后对照**摆出来给用户比较。
 *
 * 用户原话：
 * > 「优化后使用弹窗展示优化前后的对应以供用户预览比较，
 * >   如果不满意可单独对一个 sku 规格名称重新生成并覆盖历史值」
 *
 * 三条实现约束：
 * ① **只影响本次导入**：确认后把 `optimized` 名字交给导入流程，
 *    不回写 SKU 表、不改规格名（用户明确选了这个口径）。
 * ② **单条可重生成**：每行一个「重新生成」，覆盖该行的值（其余行不动）。
 * ③ **失败不造假**：某条压不下来时 `optimized` 退回原文并显示原因，
 *    用户能看出「这条没优化成功」，而不是拿到一个悄悄变截断的名字。
 */
import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Modal, Space, Table, Tag, Tooltip, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";

import { displayWidth } from "../../../lib/sku-name-width";
import type { SkuNameOptimizeItem } from "../../../lib/api";
import { regenerateSkuName } from "../../../lib/api";

const { Text } = Typography;

export interface SkuNameOptimizeModalProps {
  open: boolean;
  /** 上限（字符，汉字计 2）。由后端回传，保证与后端判据同源。 */
  maxChars: number;
  items: SkuNameOptimizeItem[];
  /** 确认：回传**最终采用**的 `key → name` 映射（含用户重生成过的行）。 */
  onConfirm: (applied: Record<string, string>) => void;
  onClose: () => void;
}

export function SkuNameOptimizeModal(props: SkuNameOptimizeModalProps) {
  const { open, maxChars, items, onConfirm, onClose } = props;
  const [rows, setRows] = useState<SkuNameOptimizeItem[]>(items);
  const [regenKey, setRegenKey] = useState<string | null>(null);

  //: 每次打开都用最新结果覆盖（重开不能带着上次的改动）。
  useEffect(() => {
    if (open) {
      setRows(items);
      setRegenKey(null);
    }
  }, [open, items]);

  /** 只展示**真的需要比较**的行：超长且改动过，或失败（失败也要让用户看见）。 */
  const visible = useMemo(
    () => rows.filter((r) => r.changed || !!r.reason),
    [rows],
  );

  const failedCount = visible.filter((r) => !!r.reason).length;

  const regenerate = async (row: SkuNameOptimizeItem) => {
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
    //: 回传**最终值**：没改动的行用原文（导入流程不该因为「没超长」就丢掉它）。
    const applied: Record<string, string> = {};
    for (const r of rows) {
      applied[r.key] = r.changed && !r.reason ? r.optimized : r.original;
    }
    onConfirm(applied);
  };

  return (
    <Modal
      open={open}
      centered
      width="70vw"
      title={`SKU 规格名称优化预览（上限 ${maxChars} 字符，汉字计 2）`}
      onCancel={onClose}
      footer={
        <Space>
          <Button onClick={onClose}>取消导入</Button>
          <Button type="primary" onClick={confirm}>
            使用优化后的名称导入
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
          message={`有 ${failedCount} 条未能压缩到上限内（已保留原文），可点右侧「重新生成」重试。`}
        />
      )}
      <Table<SkuNameOptimizeItem>
        size="small"
        rowKey="key"
        pagination={false}
        dataSource={visible}
        locale={{ emptyText: "没有超长的 SKU 名称，无需优化" }}
        columns={[
          {
            title: "原名称",
            dataIndex: "original",
            render: (value: string) => (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {value}
              </Text>
            ),
          },
          {
            title: "优化后",
            dataIndex: "optimized",
            render: (value: string, row) =>
              row.reason ? (
                <Tooltip title={row.reason}>
                  <Text type="danger" style={{ fontSize: 12 }}>
                    {row.original}
                  </Text>
                </Tooltip>
              ) : (
                <Text style={{ fontSize: 12 }}>{value}</Text>
              ),
          },
          {
            title: "字数",
            width: 120,
            render: (_: unknown, row) => {
              //: 显示宽度与后端**同一口径**（汉字计 2），避免「页面说 40、后端说 72」。
              const width = displayWidth(row.reason ? row.original : row.optimized);
              const ok = width < maxChars;
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
            width: 110,
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
