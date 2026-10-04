/**
 * 商品编辑弹窗（设计 20 §5）—— **操作列独立弹窗**，不塞进商品详情弹窗。
 *
 * ⚠️ 遵从 §13.3.5 既定口径：「商品的 AI 生成详情图主图功能在商品列表操作列使用
 * 单独弹窗实现，不要在编辑或详情弹窗中实现」⇒ 商品编辑同样独立。
 *
 * ## 三个硬口径
 * 1. **平台改价 / 上下架不可得** —— 千帆 SDK 零商品写接口（设计 20 §0），
 *    弹窗里**只读**展示价格 / SKU，并**明确标注**原因（不是留个空按钮）。
 * 2. **润色不落库** —— 备选只在弹窗里暂存，用户点「采纳」才写 `ai_title` /
 *    `ai_selling_point`（`PATCH /items/edit`）。
 * 3. **只提交改过的字段** —— 走 `lib/product-edit.ts::buildEditPayload`，
 *    未碰的字段**不进请求体**（塞 null 会被后端读成**清空**）。
 */
import { App, Button, Empty, Input, Modal, Radio, Skeleton, Space, Tabs, Tag, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";

import {
  editShopItem,
  fetchProductGroups,
  fetchXhsShopItemDetail,
  polishShopItem,
} from "../../../lib/api";
import type {
  XhsItemPolishResult,
  XhsProductGroup,
  XhsShopItem,
} from "../../../types";
import {
  adoptCandidate,
  buildEditPayload,
  type EditFormState,
  UNGROUPED_SENTINEL,
  updatedSummary,
} from "../../../lib/product-edit";

const { Text, Paragraph } = Typography;

export interface ProductEditModalProps {
  open: boolean;
  platformAccountId: number;
  item: XhsShopItem | null;
  onClose: () => void;
  /** 落库成功后通知列表刷新（分组名 / 采纳标题会变）。 */
  onSaved?: () => void;
}

/** 三区块 Tabs —— 用户偏好「分块不混在一起」，不堆在一屏里。 */
const TAB_ITEMS = [
  { key: "polish", label: "标题 / 卖点" },
  { key: "group", label: "分组" },
  { key: "sku", label: "SKU / 价格" },
];

export function ProductEditModal({
  open,
  platformAccountId,
  item,
  onClose,
  onSaved,
}: ProductEditModalProps) {
  const { message } = App.useApp();
  const [tab, setTab] = useState("polish");
  const [form, setForm] = useState<EditFormState>({});
  const [groups, setGroups] = useState<XhsProductGroup[]>([]);
  const [groupsLoaded, setGroupsLoaded] = useState(false);
  const [polish, setPolish] = useState<XhsItemPolishResult | null>(null);
  const [polishing, setPolishing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [detailLoaded, setDetailLoaded] = useState(false);

  const itemId = item?.item_id ?? "";

  // 打开时复位（上一次的草稿不能漏到下一次编辑）
  useEffect(() => {
    if (!open) return;
    setTab("polish");
    setForm({});
    setPolish(null);
    setDetail(null);
    setDetailLoaded(false);
  }, [open, itemId]);

  // 分组清单（同一个弹窗内不重复拉；列表工具栏那次刷新不共享）
  useEffect(() => {
    if (!open || groupsLoaded) return;
    let alive = true;
    fetchProductGroups()
      .then((result) => {
        if (alive) setGroups(result.items);
      })
      .catch(() => {
        // 分组拉不到不阻塞编辑（分组是可选的组织方式），静默退空
        if (alive) setGroups([]);
      })
      .finally(() => {
        if (alive) setGroupsLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [open, groupsLoaded]);

  const loadDetail = useCallback(async () => {
    if (!platformAccountId || !itemId || detailLoaded) return;
    try {
      const result = await fetchXhsShopItemDetail(platformAccountId, itemId);
      setDetail(result.item as unknown as Record<string, unknown>);
    } catch {
      setDetail(null);
    } finally {
      setDetailLoaded(true);
    }
  }, [platformAccountId, itemId, detailLoaded]);

  useEffect(() => {
    if (open && tab === "sku") void loadDetail();
  }, [open, tab, loadDetail]);

  const runPolish = async () => {
    if (!platformAccountId || !itemId) return;
    setPolishing(true);
    try {
      setPolish(await polishShopItem({ platform_account_id: platformAccountId, item_id: itemId }));
    } catch (error) {
      const detailText =
        (error as { response?: { data?: { detail?: { message?: string } } } })?.response?.data
          ?.detail?.message ?? "润色失败，请检查服务商配置后重试。";
      message.error(detailText);
    } finally {
      setPolishing(false);
    }
  };

  const save = async () => {
    if (!platformAccountId || !itemId) return;
    setSaving(true);
    try {
      const result = await editShopItem(
        buildEditPayload({ platform_account_id: platformAccountId, item_id: itemId }, form) as {
          platform_account_id: number;
          item_id: string;
          group_id?: number | null;
          ai_title?: string | null;
          ai_selling_point?: string | null;
        },
      );
      // ⚠️ 空 `updated[]` ⇒ 不弹提示（报「已更新」而其实没动是误导）
      const summary = updatedSummary(result.updated);
      if (summary) message.success(summary);
      onSaved?.();
      onClose();
    } catch (error) {
      const detailText =
        (error as { response?: { data?: { detail?: { message?: string } } } })?.response?.data
          ?.detail?.message ?? "保存失败，请稍后重试。";
      message.error(detailText);
    } finally {
      setSaving(false);
    }
  };

  const renderCandidates = (
    title: string,
    candidates: string[] | undefined,
    field: "ai_title" | "ai_selling_point",
  ) => (
    <div style={{ marginBottom: 16 }}>
      <Text strong>{title}</Text>
      <Space direction="vertical" style={{ width: "100%", marginTop: 8 }}>
        {(candidates ?? []).map((candidate) => (
          <Radio
            key={candidate}
            checked={form[field] === candidate}
            onChange={() => setForm((prev) => adoptCandidate(prev, field, candidate))}
          >
            {candidate}
          </Radio>
        ))}
        {(candidates ?? []).length === 0 ? <Text type="secondary">暂无备选</Text> : null}
      </Space>
    </div>
  );

  return (
    <Modal
      open={open}
      title={`编辑商品${item?.title ? ` · ${item.title}` : ""}`}
      onCancel={onClose}
      onOk={save}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
      width="75vw"
      centered
      destroyOnClose
    >
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={TAB_ITEMS.map((entry) => ({
          key: entry.key,
          label: entry.label,
          children: (
            <div style={{ minHeight: 240 }}>
              {entry.key === "polish" ? (
                <>
                  <Space style={{ marginBottom: 12 }}>
                    <Button type="primary" loading={polishing} onClick={runPolish}>
                      {polish ? "重新润色" : "AI 润色"}
                    </Button>
                    <Text type="secondary">
                      生成 {polish?.model_name || "—"} · 结果暂存，采纳后才会保存
                    </Text>
                  </Space>
                  {polish ? (
                    <>
                      <Paragraph type="secondary" style={{ marginBottom: 8 }}>
                        平台原标题：{polish.source_title || "—"}
                      </Paragraph>
                      {renderCandidates("标题备选（选一个作为 AI 标题）", polish.title_candidates, "ai_title")}
                      {renderCandidates(
                        "卖点备选（选一个作为 AI 卖点）",
                        polish.selling_point_candidates,
                        "ai_selling_point",
                      )}
                    </>
                  ) : (
                    <Empty
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description="点「AI 润色」生成备选（每个标题不超过 60 字）"
                    />
                  )}
                </>
              ) : null}

              {entry.key === "group" ? (
                <Space direction="vertical" style={{ width: "100%" }}>
                  <Text type="secondary">
                    分组只影响本地台账的组织方式，**不影响平台商品**。
                  </Text>
                  <Radio.Group
                    value={form.group_id === undefined ? undefined : form.group_id}
                    onChange={(event) => setForm((prev) => ({ ...prev, group_id: event.target.value }))}
                  >
                    <Space direction="vertical">
                      <Radio value={null}>移出分组</Radio>
                      {groups.map((group) => (
                        <Radio key={group.id} value={group.id}>
                          {group.name}
                          <Tag style={{ marginLeft: 8 }}>{group.product_count} 件</Tag>
                        </Radio>
                      ))}
                    </Space>
                  </Radio.Group>
                  {groups.length === 0 && groupsLoaded ? (
                    <Text type="secondary">还没有分组，先去商品列表建一个。</Text>
                  ) : null}
                </Space>
              ) : null}

              {entry.key === "sku" ? (
                <>
                  <Text type="warning" style={{ display: "block", marginBottom: 12 }}>
                    ⚠️ 商品的**价格 / 库存 / 上下架只能看，不能改** —— 千帆开放接口没有商品写能力
                    （改价 / 上下架要走平台另一个未接入的写接口）。要改请到千帆后台操作。
                  </Text>
                  {!detailLoaded ? (
                    <Skeleton active />
                  ) : detail ? (
                    <Space direction="vertical" style={{ width: "100%" }}>
                      <Text>价格：{String(detail.price_text ?? "—")}</Text>
                      <Text>库存：{String(detail.stock ?? "—")}</Text>
                      <Text>SKU 数：{String(detail.sku_count ?? "—")}</Text>
                    </Space>
                  ) : (
                    <Empty
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description="暂无 SKU / 价格明细（先同步商品补详情）"
                    />
                  )}
                </>
              ) : null}
            </div>
          ),
        }))}
      />
      {/* 未分组哨兵值与后端同值（0）—— 显式引用，避免魔法数字散落 */}
      <Text type="secondary" style={{ display: "none" }}>
        {UNGROUPED_SENTINEL}
      </Text>
    </Modal>
  );
}

export default ProductEditModal;
