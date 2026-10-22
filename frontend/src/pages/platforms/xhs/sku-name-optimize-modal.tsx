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
import { App, Button, Input, Modal, Space, Table, Tag, Tooltip, Typography } from "antd";
import { ReloadOutlined, ThunderboltOutlined, WarningOutlined } from "@ant-design/icons";

import { displayWidth, isTooLong } from "../../../lib/sku-name-width";
import type { SkuNameOptimizeItem } from "../../../lib/api";
import { optimizeSkuNames, regenerateSkuName } from "../../../lib/api";

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

/** 一条名字是否**合规**（非空且未超上限）。判据与后端 `display_width` 同口径。
 *
 *  ⚠️⚠️ **空值必须判为不合规**（用户口径 2026-10-22：「调用失败新规格名
 *  应该还是空不应该使用原规格名」+「点击重试生成没有红色标注」）。
 *
 *  旧实现只判 `displayWidth(name) <= maxChars` ⇒ 空串宽度 0 ⇒ **恒合格**
 *  ⇒ 失败行（新名为空）既不红框、也不计入 `blocking`（「0 条待处理」）
 *  ⇒ 用户看到「有 1 条失败」但界面上没有任何红框，也点不动「使用这些名称导入」的禁用理由。
 *  ⇒ 空 = 还没结果 = 不合规。 */
function isNameOk(name: string, maxChars: number): boolean {
  const text = String(name ?? "").trim();
  if (!text) return false;
  return displayWidth(text) <= maxChars;
}

export function SkuNameOptimizeModal(props: SkuNameOptimizeModalProps) {
  //: ⚠️ 用 `App.useApp()` 拿 message（用户口径：全站消息统一走 antd message 组件，
  //: 且静态方法不吃 ConfigProvider 主题 —— 项目守卫 `test_static_antd_message_import_*`
  //: 明令禁止页面静态 `import { message } from "antd"`）。
  const { message } = App.useApp();
  const { open, maxChars, items, imageByValue, onConfirm, onClose } = props;
  const [rows, setRows] = useState<Row[]>([]);
  const [regenKey, setRegenKey] = useState<string | null>(null);
  /** 批量优化进行中（「统一优化规格名称」按钮的 loading）。 */
  const [bulkOptimizing, setBulkOptimizing] = useState(false);

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

  /** 展示**所有超长的行**（不只是改过的）。
   *
   *  ⚠️ 用户口径 2026-10-22：「长度超出则弹窗展示**旧规格名 / 新规格名 / 操作列**，
   *  新规格名**默认为空输入框**」——
   *  ⇒ 判据不能是 `changed`（那要求模型已经跑过），而是「这一行原值本身就超长」。
   *  旧写法 `r.changed || !!r.reason` 只在**自动批量调模型之后**才成立，
   *  改成用户触发后表里一行都不会显示（用户看到的就是「没弹窗」）。 */
  const visible = useMemo(
    () => rows.filter((r) => isTooLong(r.original, maxChars) || r.changed || !!r.reason),
    [rows, maxChars],
  );

  const failedCount = visible.filter((r) => !!r.reason).length;

  /** **还不合规**的行（调模型失败 / 用户手改后又超长）—— 它们决定能否确认。 */
  const blocking = useMemo(
    () => visible.filter((r) => !isNameOk(r.optimized, maxChars)),
    [visible, maxChars],
  );

  /** 批量优化（**只能由用户点按钮触发**，用户口径 2026-10-22）。 */
  const optimizeAll = async () => {
    const targets = visible.filter((r) => !!r.original);
    if (targets.length === 0) return;
    setBulkOptimizing(true);
    try {
      const result = await optimizeSkuNames({
        items: targets.map((r) => ({ key: r.key, name: r.original })),
      });
      //: 按 key 回填（后端保证逐条返回；失败项带 reason，新名退回原文）。
      const byKey = new Map(result.items.map((it) => [it.key, it]));
      setRows((prev) =>
        prev.map((it) => {
          const hit = byKey.get(it.key);
          if (!hit) return it;
          return {
            ...it,
            optimized: hit.optimized,
            changed: hit.changed,
            truncated: hit.truncated,
            reason: hit.reason,
          };
        }),
      );
      const failed = result.items.filter((it) => !!it.reason).length;
      if (failed > 0) {
        message.warning(`${failed} 条调用大模型失败（已标红），请手动修改这几行`);
      } else {
        message.success(`已统一优化 ${result.items.length} 条规格名称`);
      }
    } catch (error) {
      //: ⚠️ 整体失败（网络 / 无模型配置）也要说清楚，**不静默** ——
      //: 用户口径「调用大模型优化失败红框展示」。
      message.error(
        `优化失败：${error instanceof Error ? error.message : String(error)}`,
      );
      //: 全部标红（把失败原因带进每一行，红框由 `!isNameOk` 兜住）。
      setRows((prev) => prev.map((it) => ({ ...it, reason: "调用大模型失败" })));
    } finally {
      setBulkOptimizing(false);
    }
  };

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
      {/* ⚠️ 用户口径 2026-10-22：「提示音**不要占用页面太多空间**」——
          原来两条 Alert（信息条 + 失败条）+ 一行按钮各占一整行，把表格挤掉半屏。
          ⇒ 合并成**一条紧凑操作行**：按钮 + 计数 + 失败提示全在一行，
            失败提示用 `<Text type="warning">` + Tooltip 承载全文，
            不再用带图标垫高 40px+ 的 `<Alert>`；表格随之能多显示 2~3 行。 */}
      <Space wrap size={8} style={{ marginBottom: 8 }}>
        <Button
          size="small"
          type="primary"
          icon={<ThunderboltOutlined />}
          loading={bulkOptimizing}
          disabled={visible.length === 0}
          onClick={() => void optimizeAll()}
        >
          统一优化规格名称
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          共 {visible.length} 条，{blocking.length} 条待处理
        </Text>
        {failedCount > 0 && (
          <Tooltip title="调用大模型失败的行，新规格名已标红；请手动填写或重新生成。">
            <Text type="warning" style={{ fontSize: 12 }}>
              <WarningOutlined style={{ marginRight: 4 }} />
              {failedCount} 条调用失败，请手动修改
            </Text>
          </Tooltip>
        )}
      </Space>
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
              //: ⚠️ 2026-10-22 补：「点击重试生成**没有红色标注**」——
              //: 判据必须**同时**看「失败原因」与「长度」：
              //:   · `row.reason` 非空 = 这一行模型失败 ⇒ 必红框（哪怕值碰巧不超长）；
              //:   · 否则再看长度（用户手改后又超长同样要红）。
              const bad = !!row.reason || !isNameOk(value, maxChars);
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
              //: ⚠️ 新规格名**默认为空**（用户口径 2026-10-22）⇒ 空串宽度是 0，
              //: 直接显示会变成「0 绿标」，看起来像已经合规了。
              //: ⇒ 没填新名时**显示原值宽度**（那才是这条被拦下的原因）。
              const text = row.optimized || row.original;
              const width = displayWidth(text);
              const ok = !!row.optimized && width <= maxChars;
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
