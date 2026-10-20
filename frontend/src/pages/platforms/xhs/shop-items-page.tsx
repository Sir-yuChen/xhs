import {
  CopyOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DownloadOutlined,
  ContainerOutlined,
  EditOutlined,
  EllipsisOutlined,
  FileAddOutlined,
  PlusOutlined,
  PictureOutlined,
  RobotOutlined,
  SearchOutlined,
  SendOutlined,
  SyncOutlined,
  HistoryOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Divider,
  Dropdown,
  Empty,
  Flex,
  Form,
  Image,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Checkbox,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { useAccountScope } from "../../../lib/account-scope";
import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  batchGenerateProductNotes,
  deleteXhsShopItems,
  EMPTY_GENERATE_QUEUE_SNAPSHOT,
  exportShopItems,
  fetchAccounts,
  fetchGenerateQueue,
  fetchLlmGenerateQueue,
  fetchXhsItemNotes,
  fetchPromptTemplates,
  fetchXhsQianfanBinding,
  fetchXhsShopItemDetail,
  fetchXhsShopItems,
  generateProductNote,
  generateProductDetailImage,
  syncShopGoods,
  saveDetailImages,
  fetchMaterialFolders,
  fetchItemGenerations,
  readErrorDetail,
  syncSpecifiedShopItems,
  syncShopGoodsStatus,
  operateShopItems,
  fetchShopItemOperateResult,
  type PrecheckMediaItem,
  type PrecheckTable,
} from "../../../lib/api";
//: ⚠️ 「同步商品」的按钮态判据与账号卡片页**共用同一个** `syncTaskPhase`（需求 5
//: 「逻辑应与账号卡片页的商品同步UI和逻辑一致，只是多了一个入口」）——
//: 页面里不重写一份「什么算在跑」，否则两处迟早分叉。
import { goodsGate, syncTaskPhase } from "../../../lib/sync-gates";
import type {
  GenerateQueueFinishedEntry,
  GenerateQueueSnapshot,
  SyncNotesOptions,
} from "../../../lib/api";
import { yuan } from "../../../lib/format";
import ProductEditModal from "./product-edit-modal";
import { exportBlockReason } from "../../../lib/product-edit";
import {
  EMPTY_SHOP_ITEMS_DRAFT,
  FILTER_CACHE_KEYS,
  asFiniteNumber,
  asNumberEnum,
  readFilterCache,
  sanitizeShopItemsDraft,
  usePersistedFilter,
  writeFilterCache,
  type ShopItemsFilterDraft,
} from "../../../lib/filter-cache";
import { NoteDetailBody } from "../../../components/platforms/note-detail-modal";
import { formValuesToQuery, type QueryForm } from "../../../lib/shop-items-query";
import { LlmGenerateModal } from "./llm-generate-modal";
import { WaitMask } from "../../../components/ui/wait-mask";
import { SyncSpecifiedItemsModal } from "./sync-specified-items-modal";
import { ProductDraftsModal } from "./product-drafts-modal";
import { ForbiddenWordsModal } from "./forbidden-words-modal";
import { ProductPublishWizard } from "./product-publish-wizard";
//: ⚠️ 默认导出（`export default`），不是具名 —— 具名导出的 `PackagePrecheckHandle`
//: 只是给第二步留的取值句柄类型，别误当成组件名 import。
import { SyncOptionsModal, type SyncOptions } from "../../../components/platforms/sync-options-modal";
import {
  accountLabel,
  batchDeleteBlock,
  batchDeleteHint,
  DEFAULT_SHOP_ITEMS_PAGE_SIZE,
  DEFAULT_SORT,
  MAX_BATCH_DELETE,
  NOTE_COUNT_OP_OPTIONS,
  noteCountFilter,
  normalizeDeleteIds,
  resolveSort,
  sanitizeSortState,
  SHOP_ITEMS_PAGE_SIZES,
  SORT_FIELD_BY_COLUMN,
  MAX_OPERATE_ITEMS,
  OPERATE_LABELS,
  batchOperateBlock,
  batchOperateHint,
  selectableAccounts,
  skuCell,
  sortSummary,
  statusFilterHint,
  statusTagColor,
  type SortState,
} from "../../../lib/shop-items-view";
import type {
  GenerateDetailImageResult,
  MaterialFolder,
  PlatformAccount,
  PromptTemplate,
  XhsItemNote,
  XhsItemNotesResponse,
  XhsLoginChannels,
  XhsQianfanBinding,
  ShopItemOperateEntry,
  ShopItemOperateResult,
  ShopItemsSyncResult,
  XhsShopItem,
  XhsShopItemDetail,
  XhsShopItemStatus,
  XhsShopItemsQuery,
  XhsShopItemsResponse,
} from "../../../types";

const { Text, Paragraph } = Typography;
const { RangePicker } = DatePicker;

/**
 * 「商品列表」页：千帆店铺商品的本地台账视图。
 *
 * 设计真相源 `docs/02_设计文档/12_功能设计_我的笔记与互动数据.md` §25 +
 * `docs/02_设计文档/13_功能设计_账号矩阵同步.md` §30（v1.32 本地化改造）。
 *
 * 四条口径（都是实测定的，别改）：
 * 1. **走本地数据**（§30.3）：列表查 `shop_products` 台账（「同步商品」任务落库），
 *    **零平台请求**；展示契约与实时查询时代逐字段一致；时效由「同步于 X」明示；
 * 2. **账号不过滤类型、本页零门禁**（§30.1 + ADR-021，2026-09-30）：本地台账读
 *    不打平台 ⇒ 不看 ark 会话脸色，ark 失效时只加一行说明；打平台的动作用后端
 *    ark 门禁（`ARK_SESSION_REQUIRED`）把关，前端不预判；
 * 3. **筛选参数名照抄千帆页面自己发的请求**（`whole_keywords` / `card_type` /
 *    `price_from|price_to`（分）/ `stock_from|stock_to` / `sales_from|sales_to`）；
 * 4. **金额进出都按分**（后端契约），展示层除以 100 —— 只有一处换算，不会两头乘。
 */

/** 状态选项与后端 `QIANFAN_ITEM_STATUSES` 一一对应（顺序照千帆页面的 tab）。 */
const STATUS_OPTIONS: { value: XhsShopItemStatus; label: string }[] = [
  { value: "on_shelf", label: "售卖中" },
  { value: "all", label: "全部" },
  { value: "off_shelf", label: "已下架" },
  { value: "frozen", label: "已冻结" },
  { value: "rectify", label: "整改" },
  { value: "auditing", label: "审核中" },
  { value: "audit_rejected", label: "审核驳回" },
  { value: "audit_passed", label: "审核通过" },
];

/** 分 → 元（展示口径）。`null` 保持「取不到」，不显示成 0。 */
function priceText(item: XhsShopItem): string {
  const lo = yuan(item.price_min_fen);
  const hi = yuan(item.price_max_fen);
  if (lo === "-" && hi === "-") return "-";
  if (lo === hi) return `¥${lo}`;
  return `¥${lo} ~ ¥${hi}`;
}

/**
 * 某一组的生成结果块（v2.22，设计 17 §13.3.7.9）。
 *
 * ⚠️ 按组渲染，**不在前端 groupBy** —— 分组口径由后端下发（`groups`），
 * 且「某组一张没成时该组仍要出现」也是后端保证的（前端只负责画出来）。
 * ⚠️ 只画**本组**：主图 Tab 不该看到详情图的结果（反之亦然）—— 那是「混搭」。
 */
function DetailImageGroupResult({
  groupKey,
  groups,
  regenerating,
  generating,
  onRegenerate,
  selectedIds,
  onToggleSelect,
}: {
  groupKey: string;
  groups: GenerateDetailImageResult["groups"];
  regenerating: { group: string; index: number } | null;
  generating: boolean;
  onRegenerate: (index: number) => void;
  /** v2.29：已勾选待保存的结果 id（跨组共用一份 —— 「全部保存」要能一次拿全）。 */
  selectedIds: number[];
  onToggleSelect: (assetId: number) => void;
}) {
  const group = (groups || []).find((item) => item.group === groupKey);
  if (!group) {
    // 本组还没生成过（或本次没勾本组）⇒ 如实说明，不留白。
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        本组还没有生成结果。勾选参考图后点右下角「生成」。
      </Text>
    );
  }
  return (
    <div>
      <Space size={8} align="center" style={{ marginBottom: 6 }}>
        <Text strong style={{ fontSize: 13 }}>
          生成结果
        </Text>
        <Text
          type={group.succeeded < group.requested ? "warning" : "secondary"}
          style={{ fontSize: 11 }}
        >
          {group.succeeded}/{group.requested} 张
          {group.succeeded < group.requested ? "（有失败）" : ""}
        </Text>
      </Space>
      {group.assets.length === 0 ? (
        <Text type="secondary" style={{ fontSize: 11 }}>
          这一组没有生成成功（原因见下方失败信息）
        </Text>
      ) : (
        // v2.29：**缩略图网格 + 勾选**（用户口径「生成的图使用缩小图展示」）。
        // ⚠️ 旧版是 width="100%" 的整幅大图 —— 出 10 张要滚很久，且
        // 「选择性保存」没有勾选载体。改成固定 150×150 的网格，
        // 点缩略图切换选中（选中项描边 + 角标），与「参考图勾选」同一套交互。
        // ⚠️ 这段注释必须用 `//` 而**不能**用 `{/* */}` —— 它处在 `? :` 的
        // **表达式位置**，`{...}` 会被解析成对象字面量（实测报 TS1005/TS1382）。
        <Space size={10} wrap>
          {group.assets.map((asset) => {
            const busy =
              regenerating?.group === groupKey &&
              regenerating?.index === (asset.group_index || 1);
            const picked = selectedIds.includes(asset.id);
            return (
              <div key={asset.id} style={{ width: 150 }}>
                {/*
                  ⚠️ **勾选不能挂在外层 div 的 onClick 上**（v2.29 真机实测）：
                  antd `Image` 的 `preview` **自己接管点击**（内部 `stopPropagation`）
                  ⇒ 外层 onClick 根本收不到，现象就是用户说的
                  「生成的图点击就会预览大图，**无法被选中**」。

                  ⇒ 拆成两个**互不抢**的入口：
                    * **点图片** = 看大图（`preview` 保留，这是有用行为）；
                    * **点右上角复选框** = 选择/取消（勾选语义明确，无歧义）。
                  比「点图切换选择」更清楚 —— 那种做法永远不会知道用户是想看还是想选。
                */}
                <div
                  style={{
                    position: "relative",
                    border: picked
                      ? "2px solid #1677ff"
                      : "1px solid rgba(0,0,0,0.12)",
                    borderRadius: 4,
                    overflow: "hidden",
                    lineHeight: 0,
                  }}
                >
                  <Image
                    src={asset.url || asset.local_path}
                    width={picked ? 146 : 148}
                    height={146}
                    style={{ objectFit: "contain" }}
                    preview={{ src: asset.url || asset.local_path }}
                  />
                  <Checkbox
                    checked={picked}
                    onChange={() => onToggleSelect(asset.id)}
                    style={{
                      position: "absolute",
                      top: 4,
                      left: 4,
                      zIndex: 2,
                      background: "rgba(255,255,255,0.9)",
                      borderRadius: 4,
                      padding: "0 4px",
                      lineHeight: "18px",
                    }}
                    title={picked ? "取消选择" : "选择（可保存到素材库）"}
                  />
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 2 }}>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    第 {asset.group_index || 1} 张
                  </Text>
                  {/* v2.21：单张重做 —— 只重跑这一张（不整组重跑）。 */}
                  <Button
                    type="link"
                    size="small"
                    style={{ padding: 0, fontSize: 11 }}
                    loading={busy}
                    disabled={generating && !busy}
                    onClick={() => onRegenerate(asset.group_index || 1)}
                  >
                    重做这张
                  </Button>
                </div>
              </div>
            );
          })}
        </Space>
      )}

    </div>
  );
}


/**
 * 某一组的失败信息（v2.22）—— **只显示与本组相关**的失败项。
 *
 * ⚠️ 后端的 `errors` 是全局列表（形如「商品主图 第 2 张：…」），本组件按组名前缀过滤。
 * 不过滤的话，主图 Tab 会显示详情图的失败原因 —— 用户会以为是自己这组出的问题。
 */
function DetailImageGroupErrors({
  groupKey,
  errors,
  label,
}: {
  groupKey: string;
  errors: string[];
  label: string;
}) {
  const mine = (errors || []).filter((text) => text.startsWith(label));
  if (mine.length === 0) return null;
  return (
    <Text type="warning" style={{ fontSize: 11 }}>
      {mine.length} 张失败：{mine[0]}
    </Text>
  );
}

/**
 * 参考图勾选组（v2.15，设计 17 §13.3.6；v2.20 改为**独立卡片块**）。
 *
 * 一行缩略图，点一张切换勾选（边框高亮表示已选）。**不引入 antd Checkbox 组** ——
 * 图的「勾选」最直观的呈现就是缩略图本身带上选中边框，加一个 checkbox 反而要在
 * 「看图」与「找勾」之间来回看。
 *
 * ⚠️ 空组不隐藏、给一行说明：用户要能分清「这个商品没有详情图」与「详情图加载失败」。
 *
 * ⚠️ **v2.20 用户口径「主图和详情图分为两大块，不要混搭在一起」**：
 * 两组各自包成**带边框 + 淡背景的独立卡片**（含组名、已选计数、本组全选/清空）。
 * v2.22 起每组进一步独立到各自的 Tab 里（§13.3.7.9）。
 */
function ReferenceImagePicker({
  label,
  urls,
  selected,
  onToggle,
  onClearGroup,
  maxSelectable,
  emptyHint = "暂无图片",
}: {
  label: string;
  urls: string[];
  selected: string[];
  onToggle: (url: string) => void;
  onClearGroup: () => void;
  /** v2.23：本组最多可选几张（`undefined` = 不限制）。达上限时未选中的图置灰。 */
  maxSelectable?: number;
  emptyHint?: string;
}) {
  const pickedInGroup = urls.filter((url) => selected.includes(url)).length;
  const reachedLimit = maxSelectable !== undefined && selected.length >= maxSelectable;
  return (
    <div
      style={{
        border: "1px solid rgba(0,0,0,0.1)",
        borderRadius: 6,
        padding: "8px 10px",
        background: "rgba(0,0,0,0.015)",
      }}
    >
      {/* 组头：组名 + 本组计数 + 本组全选/清空（操作**只影响本组**，不跨组串） */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <Text strong style={{ fontSize: 12 }}>
          {label}
        </Text>
        <Text type="secondary" style={{ fontSize: 11 }}>
          共 {urls.length} 张 · 已选 {pickedInGroup}
          {maxSelectable !== undefined ? ` / 最多 ${maxSelectable} 张` : ""}
        </Text>
        {urls.length > 0 ? (
          <span style={{ marginLeft: "auto", fontSize: 11 }}>
            {/* v2.23：**取消「全选」**（用户口径）—— 参考图是有上限的（最多 6 张），
                「全选」在这类有约束的选择里容易一次性顶满、再逐个取消，比逐个勾更费事。
                只保留「清空」。 */}
            <Button
              type="link"
              size="small"
              style={{ padding: 0 }}
              onClick={onClearGroup}
              disabled={pickedInGroup === 0}
            >
              清空
            </Button>
          </span>
        ) : null}
      </div>
      {urls.length === 0 ? (
        <div style={{ marginTop: 4 }}>
          <Text type="secondary" style={{ fontSize: 11, display: "block" }}>
            {emptyHint}
          </Text>
        </div>
      ) : (
        <Space size={6} wrap style={{ marginTop: 4 }}>
          {urls.map((url) => {
            const active = selected.includes(url);
            // v2.23：达上限后，未选中的图不可再点（置灰 + 不给 pointer）。
            const blocked = reachedLimit && !active;
            return (
              <div
                key={url}
                role="button"
                tabIndex={0}
                onClick={() => (blocked ? undefined : onToggle(url))}
                onKeyDown={(event) => {
                  if (blocked) return;
                  if (event.key === "Enter" || event.key === " ") onToggle(url);
                }}
                style={{
                  cursor: blocked ? "not-allowed" : "pointer",
                  opacity: blocked ? 0.45 : 1,
                  border: active ? "2px solid #1677ff" : "1px solid rgba(0,0,0,0.15)",
                  borderRadius: 4,
                  padding: 1,
                  lineHeight: 0,
                }}
                title={
                  blocked
                    ? `最多选 ${maxSelectable} 张，已达上限`
                    : active
                      ? "点击取消选择"
                      : "点击选择作为参考图"
                }
              >
                <Image
                  src={url}
                  width={76}
                  height={76}
                  style={{ objectFit: "cover", borderRadius: 2 }}
                  preview={false}
                />
              </div>
            );
          })}
        </Space>
      )}
    </div>
  );
}

/** 轮询间隔：与内容库三张进度卡同一节奏（用户口径「数据 5 秒更新一次」）。 */
const QUEUE_POLL_INTERVAL_MS = 5000;

/** 连续「本轮无任务」的轮数上限（空转兜底：快照被新批次清空重开时定时器不泄漏）。 */
const QUEUE_IDLE_ROUNDS_LIMIT = 6;

/**
 * 商品缩略图边长（v1.35）。
 *
 * 用户 2026-09-23 上机口径：「商品图**放大 15px**」——原 56 → **71**。
 * 只有一处尺寸常量：占位块（无图时）与真图共用，改一个数两处一起动，
 * 不会出现「有图 71 / 没图 56」的错位。
 */
const ITEM_THUMB_SIZE = 71;

/**
 * 「N 图」角标的样式（v1.35）：**压在缩略图左上角**。
 *
 * 原先是跟在标题后的行内 Tag —— 标题单行化后它会把标题挤掉一截，且
 * 「图片有几张」本该贴着图片说。`pointerEvents: "none"` 是为了让点击
 * 穿透到图片上（角标不该吃掉「点开大图」的手感）；圆角只留左下 + 右上，
 * 与左上角贴合图片圆角（4px）。
 */
const IMAGE_COUNT_BADGE_STYLE: CSSProperties = {
  position: "absolute",
  top: 0,
  left: 0,
  margin: 0,
  padding: "0 4px",
  fontSize: 11,
  lineHeight: "16px",
  borderTopLeftRadius: 4,
  borderTopRightRadius: 0,
  borderBottomRightRadius: 6,
  borderBottomLeftRadius: 0,
  pointerEvents: "none",
};

/** 复制反馈的 message 实例由调用方（组件内 `App.useApp()`）传入 —— 模块级函数拿不到 hook。 */
async function copyToClipboard(value: string, okText: string, msg: { success: (text: string) => void; error: (text: string) => void }): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      const holder = document.createElement("textarea");
      holder.value = value;
      holder.style.position = "fixed";
      holder.style.opacity = "0";
      document.body.appendChild(holder);
      holder.select();
      document.execCommand("copy");
      document.body.removeChild(holder);
    }
    msg.success(okText);
  } catch {
    msg.error("复制失败，请手动选中后复制");
  }
}


/**
 * 缓存的**草稿 → antd Form 初值**（2026-09-26 「搜索条件落本地缓存」）。
 *
 * 空值统一成 `undefined`（antd 的空态就是 `undefined`，塞 `null` 会让 `allowClear`
 * 的清除按钮与占位符行为不一致）；日期区间还原成 dayjs 对象对（`RangePicker` 只认
 * dayjs，缓存里存的是 `YYYY-MM-DD` 字符串 —— 见 `ShopItemsFilterDraft`）。
 */
function draftToFormValues(draft: ShopItemsFilterDraft): QueryForm {
  return {
    keyword: draft.keyword || undefined,
    status: draft.status as XhsShopItemStatus,
    price_from: draft.price_from ?? undefined,
    price_to: draft.price_to ?? undefined,
    stock_from: draft.stock_from ?? undefined,
    stock_to: draft.stock_to ?? undefined,
    sales_from: draft.sales_from ?? undefined,
    sales_to: draft.sales_to ?? undefined,
    create_time_range:
      draft.create_from || draft.create_to
        ? [
            draft.create_from ? dayjs(draft.create_from) : null,
            draft.create_to ? dayjs(draft.create_to) : null,
          ]
        : undefined,
    note_count_op: draft.note_count_op,
    note_count: draft.note_count ?? undefined,
  };
}

/**
 * antd Form 值 → 可缓存草稿（`formValuesToDraft` 是 `draftToFormValues` 的逆运算，
 * 两者**成对维护**：形状对不上的值会被 `sanitizeShopItemsDraft` 在校验时丢掉）。
 *
 * ⚠️ 它只服务**本地筛选缓存**，**不是请求体** —— 键名故意与后端不同
 * （缓存里存 `create_from`，请求里必须是 `create_time_from`）。
 * 请求一律走 `formValuesToQuery()`，别再从这里取字段发出去（DEF-093 就是这么来的）。
 */
function formValuesToDraft(values: QueryForm): ShopItemsFilterDraft {
  const range = values.create_time_range;
  return {
    keyword: values.keyword?.trim() || "",
    status: values.status || EMPTY_SHOP_ITEMS_DRAFT.status,
    price_from: values.price_from ?? null,
    price_to: values.price_to ?? null,
    stock_from: values.stock_from ?? null,
    stock_to: values.stock_to ?? null,
    sales_from: values.sales_from ?? null,
    sales_to: values.sales_to ?? null,
    create_from: range?.[0] ? dayjs(range[0]).format("YYYY-MM-DD") : null,
    create_to: range?.[1] ? dayjs(range[1]).format("YYYY-MM-DD") : null,
    note_count_op: values.note_count_op || EMPTY_SHOP_ITEMS_DRAFT.note_count_op,
    note_count: values.note_count ?? null,
  };
}


/** 挂链笔记弹窗右栏信息区（同步链路笔记带互动指标；本地生成笔记带发布态）。 */
function renderItemNoteInfo(note: XhsItemNote) {
  // 全站消息提醒统一走 antd message（设计 04 §3.6）：复制反馈用继承主题的实例。
  const { message } = App.useApp();
  const publishLabel: Record<string, { text: string; color: string }> = {
    pending: { text: "待发布", color: "orange" },
    publishing: { text: "发布中", color: "blue" },
    published: { text: "已发布", color: "green" },
    failed: { text: "发布失败", color: "red" },
  };
  const status = publishLabel[note.publish_status];
  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <div>
        <Text strong style={{ fontSize: 15 }}>
          {note.title || "（无标题）"}
        </Text>
      </div>
      <Space size={6} wrap>
        {note.note_type === "local_generated" ? (
          <Tag color="purple">本地生成</Tag>
        ) : note.note_type === "synced" ? (
          <Tag color="blue">三方同步</Tag>
        ) : (
          <Tag>本地</Tag>
        )}
        {status && <Tag color={status.color}>{status.text}</Tag>}
      </Space>
      {note.publish_status === "failed" && note.publish_error && (
        <Alert
          type="error"
          showIcon
          message="发布失败原因"
          description={note.publish_error}
        />
      )}
      <Descriptions size="small" column={1} style={{ marginTop: 4 }}>
        <Descriptions.Item label="笔记 ID">{note.note_id}</Descriptions.Item>
        <Descriptions.Item label="发布时间">{note.publish_time || "-"}</Descriptions.Item>
        {(note.browse_num != null || note.like_num != null) && (
          <Descriptions.Item label="互动">
            <Space size={10} wrap>
              <Text style={{ fontSize: 12 }}>阅读 {note.browse_num ?? "-"}</Text>
              <Text style={{ fontSize: 12 }}>赞 {note.like_num ?? "-"}</Text>
              <Text style={{ fontSize: 12 }}>藏 {note.collect_num ?? "-"}</Text>
              <Text style={{ fontSize: 12 }}>评 {note.comment_num ?? "-"}</Text>
              <Text style={{ fontSize: 12 }}>分享 {note.share_num ?? "-"}</Text>
            </Space>
          </Descriptions.Item>
        )}
        <Descriptions.Item label="笔记链接">
          <Tooltip title="点击复制">
            <Text
              style={{ fontSize: 12, cursor: "pointer", color: "#1677ff" }}
              ellipsis
              onClick={() => void copyToClipboard(note.note_url, "笔记链接已复制", message)}
            >
              {note.note_url}
            </Text>
          </Tooltip>
        </Descriptions.Item>
      </Descriptions>
      {note.content && (
        <div>
          <Divider style={{ margin: "8px 0" }} />
          <Paragraph style={{ fontSize: 13, whiteSpace: "pre-wrap", marginBottom: 0 }}>
            {note.content}
          </Paragraph>
        </div>
      )}
    </Space>
  );
}

/**
 * 「生成记录」面板（2026-10-05 需求 1）。
 *
 * ⚠️ 它原来是**独立弹窗**（v2.30 的 `generationsOpen`），现改为 `AI商品图` 弹窗里的
 * 第二个顶层 Tab —— 用户口径「将商品操作列的生成记录合并到AI商品图弹窗内，使用tab布局」。
 * 抽成组件（而不是把 JSX 内联进 Tabs）的理由：那段 JSX 有 80 行，内联进已经三层嵌套的
 * 弹窗里会让人看不出「哪一层属于哪个 Tab」。
 *
 * 空态必须**说清为什么空**（老图没有关联，v2.30 之前生成的 `item_id` 为空）——
 * 干巴巴的「暂无数据」会让用户以为是坏了（缺值口径：不猜、也不沉默）。
 */
function GenerationsPanel({
  item,
  loading,
  rows,
  total,
}: {
  item: XhsShopItem | null;
  loading: boolean;
  rows: { id: number; url: string; prompt: string; model_name: string; created_at: string }[];
  total: number;
}) {
  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: 24 }}>
        <Spin />
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          <span style={{ fontSize: 12 }}>
            {item ? "该商品还没有 AI 生成记录。" : "打开商品后这里显示它的历次 AI 生成结果。"}
            <br />
            （v2.30 之前生成的图没有商品关联，此处不计入 —— 去「生成配置」生成一次即可看到）
          </span>
        }
      />
    );
  }
  return (
    <>
      <Text type="secondary" style={{ fontSize: 12 }}>
        共 {total} 张，按时间倒序
      </Text>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(6, minmax(0, 1fr))",
          gap: 10,
          marginTop: 10,
        }}
      >
        {rows.map((row) => (
          <div key={row.id}>
            <div
              style={{
                position: "relative",
                aspectRatio: "1 / 1",
                background: "rgba(0,0,0,0.03)",
                borderRadius: 4,
                overflow: "hidden",
              }}
            >
              {/* ⚠️ 这里**不能复用素材库的 `MaterialThumb`** —— 那个走
                  `/api/xhs/materials/folders/{id}/thumbs/{file_id}`（需要 token）。
                  生成记录里的 `url` 是**上游返回的带签名直链**（OSS 预签名，
                  自带鉴权、有有效期）⇒ 直接 `<img src>` 就行。
                  ⚠️ 有效期是真实约束：过期的签名会 403 —— 想长期留存要
                  「保存到素材库」（那时走素材库那套带 token 的取图）。 */}
              <img
                src={row.url}
                alt=""
                style={{
                  width: "100%",
                  height: "100%",
                  objectFit: "contain",
                  display: "block",
                  cursor: "zoom-in",
                }}
                onClick={() => {
                  window.open(row.url, "_blank", "noopener,noreferrer");
                }}
              />
            </div>
            <Text type="secondary" style={{ fontSize: 11, display: "block" }}>
              {row.created_at}
            </Text>
            <Text type="secondary" style={{ fontSize: 11 }} ellipsis>
              {row.model_name || "-"}
            </Text>
          </div>
        ))}
      </div>
    </>
  );
}

/*
 * ⚠️ 「同步指定商品」弹窗**已抽到独立文件** `sync-specified-items-modal.tsx`
 * （2026-10-05）：它自带无限滚动取数与一套结果状态机，留在本文件里会让
 * 页面的 hook 依赖再长一截。这里只 import 它的组件。
 * 原内联实现（保留备查，勿恢复）：一个 `Modal` + `Input.TextArea` + 结果 Table。
 */

export function XhsShopItemsPage() {
  const navigate = useNavigate();
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径：
  // 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）—— `App.useApp()`
  // 替代静态 `message` 导入，实例继承 ConfigProvider 的主题与 locale。
  const { message } = App.useApp();
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [accountsLoaded, setAccountsLoaded] = useState(false);
  // 账号 / 每页条数 / 排序 / 搜索条件草稿**落本地缓存**（2026-09-26 用户口径
  // 「筛选条件和账号选择后放本地缓存，避免每次均需要重新选择」）：账号走
  // `asFiniteNumber`（数字或 null）、每页条数走 `asNumberEnum`（只能从候选里挑）、
  // 排序走 `sanitizeSortState`（形状对不上就回「无排序」）、条件草稿走
  // `sanitizeShopItemsDraft`（见下面 `Form` 的 initialValues）。
  // ⚠️ `pageNo`（第几页）**不缓存** —— 每次进页面从第一页开始是刻意的。
  // ⚠️ 账号改用**业务线级**的 `useAccountScope("shop")`（设计 32 §5.4 / A5，2026-10-11）：
  // 与「图片空间」「订单管理」共用同一个账号（同属店铺线），切页不再换人。
  // 旧键 `FILTER_CACHE_KEYS.shopItems.accountId` 由 `readAccountScope` 一次性迁移。
  const [selectedAccountId, setSelectedAccountId] = useAccountScope("shop");
  const [binding, setBinding] = useState<XhsQianfanBinding | null>(null);
  const [bindingLoaded, setBindingLoaded] = useState(false);
  const [data, setData] = useState<XhsShopItemsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorText, setErrorText] = useState("");
  const [pageNo, setPageNo] = useState(1);
  /**
   * 搜索条件是否展开（用户口径 2026-10-11「搜索条件默认展示一行支持折叠，默认为折叠」）。
   *
   * 折叠态 = 只剩卡头一行（标题 + 账号下拉 + 「展开」）；展开态 = 追加那两行条件与按钮。
   * ⚠️ **不落缓存** —— 它是视图开关（与「我的笔记」页签那种「用户的选择」不同），
   *    每次进页面回到「折叠」才是那句「默认为折叠」的字面意思。
   */
  const [searchExpanded, setSearchExpanded] = useState(false);
  const [pageSize, setPageSize] = usePersistedFilter(
    FILTER_CACHE_KEYS.shopItems.pageSize, DEFAULT_SHOP_ITEMS_PAGE_SIZE, asNumberEnum(SHOP_ITEMS_PAGE_SIZES)
  );
  // v1.32：排序挪到表头（上下箭头），不再在搜索区单独放字段/方向下拉。
  // 状态即「当前生效的排序」—— 服务端分页，排序必须回服务端做（客户端
  // 只有当前页，本地排只会重排一页，是假排序）。
  //
  // v1.36（用户 2026-09-23 口径「默认不排序，只有点击触发后进行排序，参考 antd
  // 组件中的排序」）：初值是 **null = 无排序** —— 进来表头没有箭头、请求里也不带
  // `sort_field`（后端用它的稳定默认序）。点表头才开始有排序态（升 → 降 → 取消）。
  // 2026-09-26：排序态**也落缓存**（用户点过的排序 = 他的选择，不该每次重来）。
  const [sort, setSort] = usePersistedFilter<SortState | null>(
    FILTER_CACHE_KEYS.shopItems.sort, DEFAULT_SORT, sanitizeSortState
  );
  // 搜索条件表单：初值取**缓存草稿**（`sanitizeShopItemsDraft` 校验形状与取值域），
  // 每次改动即回写（`onValuesChange`）—— 用户填了一半就切页面，回来还在。
  const [form] = Form.useForm<QueryForm>();
  const cachedDraft = useMemo(
    () => sanitizeShopItemsDraft(readFilterCache(FILTER_CACHE_KEYS.shopItems.form)),
    []
  );
  const formInitialValues = useMemo(() => draftToFormValues(cachedDraft), [cachedDraft]);
  //: 折叠态下表单不挂载 ⇒ `initialValues` 不会自动写进 store。这里**显式写一次**，
  //: 这样卡头的 `Form.useWatch("status")` 与「折叠时按缓存条件查询」（`formSnapshot`）
  //: 在表单还没渲染时也拿得到正确条件。展开后 `initialValues` 再次生效（同值，不冲突）。
  useEffect(() => {
    form.setFieldsValue(formInitialValues);
  }, [form, formInitialValues]);
  // ⚠️ `bindOpen` / `bindCookie` / `binding_` 三个绑定弹窗状态已删除（ADR-021）。
  // 商品 → 挂链笔记反查（关系表源，设计 13 §31.6；弹窗 + tabs）
  const [notesOpen, setNotesOpen] = useState(false);
  const [notesLoading, setNotesLoading] = useState(false);
  const [notesData, setNotesData] = useState<XhsItemNotesResponse | null>(null);
  const [notesItem, setNotesItem] = useState<XhsShopItem | null>(null);
  /** 挂链笔记弹窗当前 tab（一篇笔记一个 tab；单篇时隐藏 tab 栏）。 */
  const [notesTab, setNotesTab] = useState("0");
  // 商品笔记智能生成（设计 13 §31；§31.10 起单 / 批量都走队列）
  const [selectedItemIds, setSelectedItemIds] = useState<string[]>([]);
  const [generatingItemId, setGeneratingItemId] = useState<string | null>(null);
  const [batchGenerating, setBatchGenerating] = useState(false);
  // 生成进度（队列快照 + 轮询三件）：入队后按钮 loading 交给快照的
  // `running + queued`（`genQueueItemIds`），终态由轮询报一次（与批量同一个出口）。
  // 2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：快照只给队列
  // 实时态（running / queued / total / finished），**本轮成败数由本页按自己入队
  // 的商品累计**（`genRound`，空闲起轮归零、忙时并入 —— 与旧「空闲即新开一批」
  // 同口径）。
  const [genQueue, setGenQueue] = useState<GenerateQueueSnapshot>(EMPTY_GENERATE_QUEUE_SNAPSHOT);
  const [genRound, setGenRound] = useState({ ok: 0, failed: 0 });
  const genPollRef = useRef<number | null>(null);
  const genTrackedRef = useRef<Set<string>>(new Set());
  const genResultsRef = useRef<Map<string, GenerateQueueFinishedEntry>>(new Map());
  const genIdleRoundsRef = useRef(0);
  /** 批量删除进行中（v1.36）：只用来给按钮上 loading，判据本身在 lib（`batchDeleteBlock`）。 */
  const [batchDeleting, setBatchDeleting] = useState(false);
  /**
   * 「同步商品」按钮态（v2.13，设计 13 §30.3.2；**2026-10-05 需求 5 改为**首查驱动。
   *
   * ⚠️ 原先是「点击时 true、触发请求返回就 false」的**一次性 loading** —— 那表达的是
   * 「这个 HTTP 请求在途」，而同步本身是后台任务（全量翻页 + 限流等待，几十秒到几分钟）。
   * 用户看到的是「点了转一下、然后按钮恢复」，完全不知道同步还在跑。
   *
   * 现在与**账号卡片页的商品同步按钮逐字同口径**（需求 5「逻辑应与账号卡片页的商品
   * 同步UI和逻辑一致，只是多了一个入口」）：
   * * `goodsSyncIds` —— 该账号此刻**有进行中的同步任务**（首查 + 5s 轮询维护）；
   * * `goodsSyncing` —— **只有**触发请求在途的那一小会儿（防连点）。
   * 按钮文案取两者：任务在跑 ⇒ 「同步中…」，否则「同步商品」。
   */
  const [goodsSyncIds, setGoodsSyncIds] = useState<Set<number>>(() => new Set());
  const goodsSyncPollRef = useRef<number | null>(null);
  const [goodsSyncing, setGoodsSyncing] = useState(false);
  /**
   * **页面级等待**（2026-10-06 用户口径：「商品列表的所有功能的等待均应在合适的时机
   * 添加页面**蒙版**和**统计耗时**，避免结果窗口关闭」）。
   *
   * ⚠️ 统一成一个标签而不是每个动作各写一份：文案 + 耗时 + 蒙版三件事必须**同时**
   * 出现与消失，分散写就会出现「有的转圈有的不转」「有的有耗时有的没有」。
   * `null` = 没有等待；非空 = 正在等待（值是给用户看的动作名）。
   */
  const [waitLabel, setWaitLabel] = useState<string | null>(null);
  /** 等待起算时刻 + 1s 一跳的秒表（与结果弹窗同一套做法；结束即停表）。 */
  const waitStartedAtRef = useRef<number | null>(null);
  const waitTickRef = useRef<number | null>(null);
  const [waitElapsedSec, setWaitElapsedSec] = useState(0);
  /** ark 门禁提示：千帆会话确定不可用时给一句准话（**不**打开选项弹窗）。
   *  判据 = `goodsGate`（与账号卡片页同一把尺子，需求 5）。 */
  const [arkPromptOpen, setArkPromptOpen] = useState(false);
  /** 这次「批量同步商品信息」要按 ID 同步的那批（主表勾选；空 ⇒ 走整店）。 */
  const selectedForSync = normalizeDeleteIds(selectedItemIds);
  /** 行内「同步商品信息」点的那一件（非空 ⇒ 弹窗只针对它，打开即同步）。 */
  const [syncSingleTarget, setSyncSingleTarget] = useState<string | null>(null);
  /** 该账号是否**有进行中的同步商品任务**（首查 + 5s 轮询维护）。
   *  2026-10-06 用户口径：「存在同步商品的任务时同步商品按钮不支持点击，应显示为商品同步中」。 */
  const goodsSyncRunning = selectedAccountId !== null && goodsSyncIds.has(selectedAccountId);
  // LLM 生成（T9，设计 17 §7.4）：与一期「生成笔记」是两个队列，状态各自独立。
  const [llmGenerateOpen, setLlmGenerateOpen] = useState(false);
  const [llmGenerateItem, setLlmGenerateItem] = useState<XhsShopItem | null>(null);
  /** 已登记进 LLM 生成轮询的商品（操作列按钮据此转圈）。 */
  const [llmQueueItemIds, setLlmQueueItemIds] = useState<Set<string>>(new Set());
  /** 正在提交 LLM 生成弹窗的商品 id（防重复点开）。 */
  const [llmGenerating, setLlmGenerating] = useState<string | null>(null);
  // 商品详情弹窗（v1.32：点标题打开；数据来自 /item-detail，纯本地）
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<XhsShopItemDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  // 商品详情图生成（设计 17 §13.3.3，v2.1；**v2.12 移到列表操作列独立弹窗**）——
  // 三态：生成中 / 已出图 / 无。
  // ⚠️ 出图结果**不落 detail**（它不是台账字段，是本次生成动作的产物）：单独一个 state，
  // 关弹窗即清，避免把「刚生成的图」误当成「商品自己的图集」。
  // ⚠️ v2.12（2026-10-02 用户口径「商品的 AI 生成详情图主图功能在商品列表操作列使用
  //    单独弹窗实现，不要在编辑或详情弹窗中实现」）⇒ 弹窗自带 `item` 上下文，
  //    **不再依赖 `detail`**（详情弹窗那条链路已整段移除）。
  const [detailImageOpen, setDetailImageOpen] = useState(false);
  /**
   * v2.30（§13.3.11）：**生成记录弹窗**（用户口径「商品列表操作列新增查看入口」）。
   * 解决「进入素材库后无法再区分与产品的关系」—— 这里按商品查历次 AI 生成结果。
   */
  const [generationsOpen, setGenerationsOpen] = useState(false);
  const [generationsItem, setGenerationsItem] = useState<XhsShopItem | null>(null);
  /**
   * M4 商品编辑（设计 20 §5）：操作列独立弹窗 + 列表工具栏的**分组筛选 / 导出 CSV**。
   *
   * ⚠️ 编辑弹窗**不塞进详情弹窗**（§13.3.5 既定口径：操作列的独立弹窗）。
   * `editingItem` 非空即弹窗打开 —— 「打开」与「编辑哪个商品」是同一件事，
   * 不拆成两个 state（拆了就会出现「打开着但不指向任何商品」的中间态）。
   */
  const [editingItem, setEditingItem] = useState<XhsShopItem | null>(null);
  // ⚠️ 分组状态（`groups` / `groupFilter`）已随「彻底移除商品分组」退场（2026-10-05）。
  const [exporting, setExporting] = useState(false);
  // ------------------------------------------------------------------ #
  // 商品操作（2026-10-05 需求 7/8）：批量上架 / 下架 / 删除**平台商品**。
  // 走手动触发式任务 ⇒ 状态是「任务号 + 轮询到的逐条结果」，不是一次请求的返回值。
  // ------------------------------------------------------------------ #
  /** 正在提交的操作动作（按钮转圈；`null` = 空闲）。 */
  const [operatingAction, setOperatingAction] = useState<string | null>(null);
  /** 操作结果弹窗（提交成功即打开，轮询到全终态停）。 */
  const [operateTask, setOperateTask] = useState<ShopItemOperateResult | null>(null);
  const operatePollRef = useRef<number | null>(null);
  /**
   * 商品操作结果弹窗的**耗时统计**（2026-10-06 用户口径：「结果等待弹窗要加蒙版
   * 统计耗时，避免结果弹窗关闭，执行结果返回后再支持弹窗关闭」）。
   *
   * ⚠️ 耗时是**前端自计时**（`startedAt` ~ 现在），不是后端下发的 —— 后端只给
   * 任务进度（`executed` / `total`）。两边都不假装对方的数据。
   * ⚠️ 非终态时每 1 秒重渲染一次（秒表要动）；终态即停（不留定时器）。
   */
  const operateStartedAtRef = useRef<number | null>(null);
  const operateTickRef = useRef<number | null>(null);
  const [operateElapsedSec, setOperateElapsedSec] = useState(0);
  /** 同步指定商品弹窗（需求 6）。 */
  const [syncItemsOpen, setSyncItemsOpen] = useState(false);
  //: 从详情弹窗发起「重抓此商品」时记下那件商品 —— 同步完成后回到它的详情
  //: （用户口径 2026-10-09：重抓的意义就是让眼前这份数据变新）。
  const [syncReturnItem, setSyncReturnItem] = useState<XhsShopItem | null>(null);
  //: 草稿箱弹窗（设计 24 §9.4，2026-10-08 用户口径）：与商品列表同一账号。
  const [draftsModalOpen, setDraftsModalOpen] = useState(false);
  //: 违规词库弹窗（2026-10-08 用户口径）：全屏 50%，管理本地违规词库。
  const [forbiddenWordsOpen, setForbiddenWordsOpen] = useState(false);
  //: 发布商品向导（设计 25 §4；用户 2026-10-08 口径）：antd Steps 三步
  //: —— 预检 → 配置商品参数+预览 → 发布。⚠️ 真实上架（红线：成功后立即下架）。
  const [publishWizardOpen, setPublishWizardOpen] = useState(false);
  const [generations, setGenerations] = useState<
    { id: number; url: string; prompt: string; model_name: string; created_at: string }[]
  >([]);
  const [generationsLoading, setGenerationsLoading] = useState(false);
  /** 该商品生成了多少张（**总数，不是当前页**）。 */
  const [generationsTotal, setGenerationsTotal] = useState(0);
  const [detailImageItem, setDetailImageItem] = useState<XhsShopItem | null>(null);
  const [detailImageGenerating, setDetailImageGenerating] = useState(false);
  /**
   * v2.28：**生成耗时**（用户口径「弹窗加蒙版并统计耗时，防止被关闭」）。
   *
   * ⚠️ 为什么要有它：出图是**逐张串行**的真上游调用（实测单张 29–96 秒，
   * 10 张可能十几分钟）。此前只有一个转圈，用户既不知道「跑了多久」、
   * 也不知道「还要多久」—— 而弹窗**随时可关**，关掉就看不到结果了。
   *
   * `null` = 未在生成；数字 = 起始时间戳（`Date.now()`）。
   */
  const [detailImageStartedAt, setDetailImageStartedAt] = useState<number | null>(null);
  //: 每 500ms 刷一次的「已耗时（秒）」——只为显示，不参与判据。
  const [detailImageElapsed, setDetailImageElapsed] = useState(0);
  /**
   * v2.29：**已勾选待保存的结果 id**（用户口径「支持选择性保存到指定素材库」）。
   * 跨组共用一份 —— 「全部保存」要能一次拿全两组的图；按组各存一份会让
   * 「全选」变成两个独立的集合（前端要合并，且两处状态可能不同步）。
   */
  const [detailImageSelectedIds, setDetailImageSelectedIds] = useState<number[]>([]);
  /** v2.29：保存进行中（按钮 loading；与「生成中」分开 —— 两者互不阻塞）。 */
  const [detailImageSaving, setDetailImageSaving] = useState(false);
  /** v2.29：保存目标素材库文件夹（默认第一个；用户可改）。 */
  const [detailImageMaterialFolderId, setDetailImageMaterialFolderId] = useState<number | null>(null);
  /** v2.29：素材库文件夹清单（保存目标下拉的数据源）。 */
  const [materialFolders, setMaterialFolders] = useState<MaterialFolder[]>([]);

  useEffect(() => {
    if (detailImageStartedAt === null) {
      setDetailImageElapsed(0);
      return;
    }
    setDetailImageElapsed(Math.floor((Date.now() - detailImageStartedAt) / 1000));
    const timer = window.setInterval(() => {
      setDetailImageElapsed(Math.floor((Date.now() - detailImageStartedAt) / 1000));
    }, 500);
    return () => window.clearInterval(timer);
  }, [detailImageStartedAt]);

  /**
   * v2.28：弹窗标题。生成中**追加耗时**（用户口径「统计耗时」）——
   * 但基础文案 `"AI商品图"` 保持不变（它是页面守卫切范围的锚点）。
   */
  const detailImageTitle = detailImageGenerating
    ? `AI商品图 · 生成中 ${detailImageElapsed}s（请勿关闭，关闭会看不到结果）`
    : "AI商品图";
  const [detailImage, setDetailImage] = useState<GenerateDetailImageResult | null>(null);
  // v2.15：商品图集（主图 / 原详情图）与勾选的参考图。
  const [detailImageDetail, setDetailImageDetail] = useState<XhsShopItemDetail | null>(null);
  const [detailImageRefs, setDetailImageRefs] = useState<string[]>([]);
  /** v2.21：单张重做的目标（`null` = 整组生成中/空闲）—— 只让那一张转圈。 */
  const [detailImageRegenerating, setDetailImageRegenerating] = useState<{
    group: string;
    index: number;
  } | null>(null);
  /**
   * 2026-10-05（需求 1）：`AI商品图` 弹窗的**顶层** Tab —— 「生成配置」/「生成记录」。
   *
   * ⚠️ 与下面的 `activeGroup`（主图 / 详情图）是**两层不同的 Tab**：
   * 顶层分「做什么」（配置 vs 看历史），`activeGroup` 分「生成哪一类图」。
   * 合成一层会让「看生成记录」和「生成详情图」变成互斥的两个状态（用户没法边看边配）。
   */
  const [detailImageTab, setDetailImageTab] = useState<"config" | "generations">("config");
  /** v2.22：当前 Tab（主图 / 详情图**完全拆开**，各自一套配置）。 */
  const [activeGroup, setActiveGroup] = useState<"product" | "detail">("product");
  /**
   * v2.22：**每组各自的配置**（提示词 / 张数）。
   *
   * ⚠️ 按组存（不是共用一个 prompt/count）—— 共用一个 state 时，用户在「主图」Tab
   * 填的内容会在切到「详情图」后被顶掉，那正是「混搭」在状态层的形态。
   */
  const [detailImageGroupConfigs, setDetailImageGroupConfigs] = useState<
    Record<string, { prompt?: string; count?: number; templateId?: number }>
  >({});
  /**
   * v2.24：**公共提示词模板**（用户口径「生成图片时应选择公共提示词模板，保证图片商品的
   * 一致性」）。打开弹窗时拉一次（与 `llm-generate-modal` 同一范式：模板是用户级的，
   * 与具体商品无关 ⇒ 只拉一次，切商品不重复拉）。
   */
  const [detailImageTemplates, setDetailImageTemplates] = useState<PromptTemplate[]>([]);
  const [detailImageTemplatesLoaded, setDetailImageTemplatesLoaded] = useState(false);
  const [detailImageLoading, setDetailImageLoading] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const list = await fetchAccounts();
        // v1.32（设计 13 §30.1）：**不再按 sub_type 过滤**；ADR-021 后连「有没有
        // 绑店」这一维也不存在了 —— 台账属于账号，所有已添加的小红书账号都能选。
        // 前端按类型/归属预判可用性 = 第二份真相源（能力判据在后端的 ark 令牌）。
        const selectable = selectableAccounts(list);
        setAccounts(selectable);
        // 账号选择的**缓存自愈**（2026-09-26）：缓存里的账号可能已被删除、或已不在
        // 可选集合里 ⇒ 此时退回第一个账号；否则下拉里会挂着一个候选里找不到的值，
        // 列表恒空而用户看不出原因。
        setSelectedAccountId((current) =>
          current !== null && selectable.some((account) => account.id === current)
            ? current
            : selectable[0]?.id ?? null
        );
      } catch (error) {
        message.error(error instanceof Error ? error.message : "账号列表加载失败");
      } finally {
        setAccountsLoaded(true);
      }
    })();
    // 仅首屏加载账号列表
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ⚠️ 2026-09-24：店铺列表状态整块移除。它过去的唯一读者是账号下拉的店铺标注，
  // 而标签现在「只展示账号名称」（用户口径）⇒ 再拉一次 `/qianfan/shops` 就是一次
  // 纯浪费的请求（还是每次进页面都发）。绑定态另有 `fetchXhsQianfanBinding` 回答。

  const loadBinding = useCallback(async (accountId: number) => {
    try {
      setBinding(await fetchXhsQianfanBinding(accountId));
    } catch {
      setBinding(null);
    } finally {
      setBindingLoaded(true);
    }
  }, []);

  /**
   * 搜索条件表单值快照。
   *
   * ⚠️ 必须 `getFieldsValue(true)`（读**整个 store**），不能用默认的 `getFieldsValue()`：
   * 后者走 field entities，而「折叠态不渲染表单」（用户口径 2026-10-11）会让 entities
   * 不存在 ⇒ 返回空对象 ⇒ 关键词 / 价格 / 日期等条件**静默全丢**（列表按默认态查，
   * 用户以为自己还筛着）。整个 store 在表单未挂载时依然保留着缓存里那份条件。
   */
  const formSnapshot = useCallback((): QueryForm => form.getFieldsValue(true) as QueryForm, [form]);

  const load = useCallback(
    async (accountId: number, page = pageNo, size = pageSize, sortArg = sort) => {
      // DEF-093：列表与导出**共用同一处构造**（`formValuesToQuery`），不在这里另拼一份。
      const query: XhsShopItemsQuery = formValuesToQuery(formSnapshot(), {
        accountId,
        pageNo: page,
        pageSize: size,
        sort: sortArg,
      });
      setLoading(true);
      beginWait("加载商品列表");
      setErrorText("");
      try {
        setData(await fetchXhsShopItems(query));
      } catch (error) {
        setData(null);
        const text = error instanceof Error ? error.message : "商品列表加载失败";
        setErrorText(text);
        message.error(text);
      } finally {
        setLoading(false);
        endWait();
      }
    },
    [form, pageNo, pageSize, sort]
  );

  /**
   * 搜索 / 重置的统一入口（v1.36，顺带**清掉勾选**）。
   *
   * 为什么要清：跨页保留勾选只该在**同一次查询内**成立。换了条件就是一个新集合，
   * 留着上一批勾选会让「我删的是不是我看到的」变成一个用户猜不出来的问题 ——
   * 按钮上那个「（N）」还在，人却看不见那 N 行。
   *
   * v1.36：**「重置」把排序也退回去**（用户口径「默认不排序」）—— 重置 = 回初始态，
   * 而初始态就是「没有排序」；「搜索」则保留用户当前点的排序（他刚点了列，不该被
   * 一次条件搜索悄悄清掉）。⚠️ `load` 的 `sortArg` 默认值取自 state，`setSort` 这一
   * 拍还没生效 ⇒ 必须显式把目标值传进去，否则会按**旧**排序重拉。
   */
  function runSearch(resetForm: boolean) {
    if (resetForm) {
      // ⚠️ 「重置」必须回**清空态**，不能回 `initialValues`：后者现在是缓存里那份
      // 「上次的筛选」（2026-09-26 起表单初值取自缓存）—— 用 `resetFields()` 会让
      // 重置把筛选条件原样保留，语义直接反了。这里显式写一份全空草稿，并把清空态
      // **覆盖进缓存**（否则下次进页面它又长回来）。
      form.setFieldsValue(draftToFormValues(EMPTY_SHOP_ITEMS_DRAFT));
      writeFilterCache(FILTER_CACHE_KEYS.shopItems.form, EMPTY_SHOP_ITEMS_DRAFT);
    }
    setSelectedItemIds([]);
    setPageNo(1);
    const nextSort = resetForm ? DEFAULT_SORT : sort;
    if (resetForm) setSort(nextSort);
    if (selectedAccountId) void load(selectedAccountId, 1, pageSize, nextSort);
  }

  /* 2026-10-05 用户口径「商品列表页移除对千帆是否有效的校验和 UI，商品查询的是本地
     数据库，无需校验账号是否有效」：
     ① `channelsByAccount`（账号 → 三平台登录态 map）与 `prereq`（`ledgerPrerequisite`
        前置校验）**整块删除** —— 本页是纯本地台账读（`/items` / `/item-detail` /
        `/item-notes` 零平台请求），千帆会话可用性既不拦请求、也不该上屏；
     ② `arkUnavailable`（ark 确定不可用时的告警行）随之删除；
     ③ 拉列表的 effect 不再按 `prereq.ok` 判（它恒为 true，是道假防线）。
     ⚠️ lib 里的 `ledgerPrerequisite` / `QIANFAN_PREREQUISITE_HINT` 保留但**本页不再引用**
        （直删 lib 会连带改 `shop_items_view.cjs` 的 F1–F5 断言 —— 那是 lib 级契约，
        与本页 UI 解耦；本页不引用即达成「移除校验和 UI」）。 */

  useEffect(() => {
    if (selectedAccountId === null) return;
    // 切账号 = 换一个台账归属：旧账号的响应**必须先清掉**（否则新账号会短暂看到
    // 上一个账号的商品行 —— 隔离键已收敛到账号，串号比串店更严重）。
    setBinding(null);
    setBindingLoaded(false);
    setData(null);
    setErrorText("");
    // 勾选也是「上一个账号的」：删除请求按 item_id 打到**新账号的台账**只会删 0 行，
    // 但按钮上还挂着「批量删除（30）」= 界面在说一件不会发生的事。一起清掉。
    setSelectedItemIds([]);
    void loadBinding(selectedAccountId);
  }, [selectedAccountId, loadBinding]);

  useEffect(() => {
    if (selectedAccountId === null || !bindingLoaded) return;
    void load(selectedAccountId, pageNo, pageSize);
    // ⚠️ 依赖里**不再有 `binding?.bound`**：切账号时 `binding` 先被清成 null 再回来，
    // 带上它会让「绑定后拉一页」的语义与实际（切账号即拉）不符。
    // ⚠️ 2026-10-05 起也**不再有 `prereq.ok`** —— 前置校验已整块移除（本地读本就没有门禁）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountId, bindingLoaded]);

  const accountOptions = useMemo(
    () =>
      accounts.map((account) => ({
        value: account.id,
        // 只展示账号名称（2026-09-24 用户口径）：类型后缀（v1.32 已去）与店铺标注
        // （本次去掉）都不再进 label。账号 → 店铺是 1:1，店名由列表主体展示。
        label: accountLabel(account.nickname),
      })),
    [accounts]
  );

  /**
   * ⚠️ **`handleBind()` 已删除**（ADR-021 / 设计 18，2026-09-30）：
   * `POST /xhs/qianfan/bind` 已退场（后端显式 `410 QIANFAN_BINDING_REMOVED`），
   * 页面上的「绑定千帆（粘贴 Cookie）」按钮与那个弹窗一并移除。
   *
   * 千帆能力现在完全由「本账号有没有可用的 `ark` 会话」决定 —— 要开通就去
   * **账号矩阵**给该账号导入千帆（ark）登录态，那是凭据操作，不是这里的关系操作。
   * 被取代的原逻辑（保留备查）：
   *
   *   const bound = await bindXhsQianfanShop(selectedAccountId, bindCookie.trim());
   *   message.success(`已绑定千帆店铺：${bound.shop_name || "（未命名）"}`);
   */

  /** 打开「这个商品挂了哪些笔记」弹窗（关系表源，零平台请求；设计 13 §31.6）。
   *
   * 多篇笔记用 **tab 自然隔离**（一篇一个 tab），内容区复用内容库详情弹窗的
   * 同一份两栏版式（`NoteDetailBody`）—— 同一语义不留两份实现。
   */
  async function openItemNotes(item: XhsShopItem) {
    if (!selectedAccountId) return;
    setNotesItem(item);
    setNotesOpen(true);
    setNotesLoading(true);
    setNotesData(null);
    setNotesTab("0");
    try {
      setNotesData(await fetchXhsItemNotes(selectedAccountId, item.item_id));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "挂链笔记反查失败");
    } finally {
      setNotesLoading(false);
    }
  }

  /**
   * 单商品「生成笔记」（设计 13 §31.4 + §31.10）：入队即返回，worker 后台跑。
   *
   * 门禁失败（无令牌 / 台账无此行）仍是结构化 400/404，由全局拦截器
   * 摆真实原因；平台侧失败（502）发生在 worker 里，只在快照 `finished` 给一次。
   */
  async function generateNote(item: XhsShopItem) {
    if (!selectedAccountId) return;
    setGeneratingItemId(item.item_id);
    try {
      const result = await generateProductNote(selectedAccountId, item.item_id);
      // 2026-09-26 用户口径「优化提示内容，简单一些，提示太长了」：一句说清
      // 「入队了」+「发布在哪」，不复述「才会真正发布」的长句。
      const name = item.title || item.item_id;
      message.success(
        result.already_queued ? `《${name}》已在队列中` : `《${name}》已入队，发布请到内容库`
      );
      trackGenItems([item.item_id]);
      // 刷新挂链笔记数（生成成功后该商品的映射多了本地生成那篇）
      if (notesItem && notesItem.item_id === item.item_id && notesOpen) {
        void openItemNotes(item);
      }
    } catch (error) {
      const detail = (error as { response?: { data?: { detail?: { message?: string } } } })?.response?.data
        ?.detail;
      message.error(
        (detail && typeof detail === "object" && detail.message) ||
          (error instanceof Error ? error.message : "生成笔记失败")
      );
    } finally {
      setGeneratingItemId(null);
    }
  }

  /** 批量「生成笔记」（设计 13 §31.4 步 6 + §31.10）：逐项过门禁，通过项入队。 */
  async function batchGenerate() {
    if (!selectedAccountId || selectedItemIds.length === 0) return;
    setBatchGenerating(true);
    try {
      const result = await batchGenerateProductNotes(selectedAccountId, selectedItemIds);
      const enqueued = result.results.filter((r) => r.ok).map((r) => r.item_id);
      if (result.failed === 0) {
        message.success(`已入队 ${result.started} 项，发布请到内容库`);
      } else {
        const firstError = result.results.find((r) => !r.ok)?.error || "";
        message.warning(
          `已入队 ${result.started} 项，门禁未过 ${result.failed} 项${firstError ? `（首例：${firstError}）` : ""}`
        );
      }
      if (enqueued.length) trackGenItems(enqueued);
      setSelectedItemIds([]);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "批量生成失败");
    } finally {
      setBatchGenerating(false);
    }
  }

  // --------------------------------------------------------------------------- #
  // 生成队列轮询（§31.10；与内容库同步进度卡同一套骨架：5s 一轮、待处理为 0 即停）
  // --------------------------------------------------------------------------- #

  /** 起轮询（直到**待处理为 0**）：把入队的 item_id 交给同一个轮询器。
   *
   * 2026-09-26 常驻化：本轮成败在本页累计，**空闲过（没在轮询）才归零** ——
   * 忙时到来的新入队并入同一轮继续计数，不把用户上半场的成果从卡片上抹掉。
   */
  function trackGenItems(itemIds: string[]) {
    for (const id of itemIds) genTrackedRef.current.add(id);
    if (genPollRef.current === null) {
      setGenRound({ ok: 0, failed: 0 });
      genPollRef.current = window.setInterval(() => {
        void pollGenQueue();
      }, QUEUE_POLL_INTERVAL_MS);
    }
  }

  function stopGenPolling() {
    if (genPollRef.current !== null) {
      window.clearInterval(genPollRef.current);
      genPollRef.current = null;
    }
    genTrackedRef.current.clear();
    genResultsRef.current.clear();
    genIdleRoundsRef.current = 0;
    setBatchGenerating(false);
  }

  /** 轮询一轮：刷新快照 → 累积逐项结果 → 离开队列的项报一次终态 → 待处理为 0 收尾。 */
  async function pollGenQueue() {
    let snapshot: GenerateQueueSnapshot;
    try {
      snapshot = await fetchGenerateQueue();
    } catch {
      return; // 安静重试（下一轮）
    }
    setGenQueue(snapshot);
    // 累积逐项结果：快照 `finished` 可能被新批次清空重开而变短，累积才能每项都报一次。
    for (const entry of snapshot.finished) {
      if (entry.item_id) genResultsRef.current.set(entry.item_id, entry);
    }
    const stillActive = new Set<string>([
      ...(snapshot.running ? [snapshot.running.item_id] : []),
      ...snapshot.queued.map((q) => q.item_id),
    ]);
    const finishedNow = [...genTrackedRef.current].filter((id) => !stillActive.has(id));
    if (finishedNow.length) {
      const okTitles: string[] = [];
      const failLines: string[] = [];
      for (const itemId of finishedNow) {
        genTrackedRef.current.delete(itemId);
        const result = genResultsRef.current.get(itemId);
        if (result && result.ok) okTitles.push(result.title || itemId);
        else if (result) {
          failLines.push(`${itemId}：${result.reason || "平台未给原因"}`);
        }
        // 第三种：离开了队列但拿不到结果（快照被新批次清空 / 进程重启）——
        // 只刷新列表，不宣称成功也不宣称失败（缺值口径：不造假）。
      }
      const parts: string[] = [];
      if (okTitles.length) parts.push(`已生成 ${okTitles.length} 篇待发布笔记`);
      if (failLines.length) parts.push(`生成失败 ${failLines.length} 项（${failLines.join("；")}）`);
      // 本轮成败累计（常驻化后没有批次记账，卡片上的数字由本页算）
      if (okTitles.length || failLines.length) {
        setGenRound((prev) => ({ ok: prev.ok + okTitles.length, failed: prev.failed + failLines.length }));
      }
      if (parts.length) {
        const text = `${parts.join("；")}。`;
        if (failLines.length) message.warning(text); else message.success(text);
      }
      // 生成成功会多出本地生成笔记 ⇒ 刷新列表统计（挂链笔记数等）
      if (okTitles.length && selectedAccountId) void load(selectedAccountId, pageNo, pageSize);
    }
    // 停止条件（与另两张进度卡同口径）：快照 total > 0 且待处理为 0 ⇒ 收尾；
    // 空转兜底防定时器泄漏。
    genIdleRoundsRef.current = snapshot.total > 0 ? 0 : genIdleRoundsRef.current + 1;
    const pending = (snapshot.running ? 1 : 0) + snapshot.queued.length;
    if ((snapshot.total > 0 && pending === 0) || genIdleRoundsRef.current >= QUEUE_IDLE_ROUNDS_LIMIT) {
      stopGenPolling();
    }
  }

  /** 队列中的商品 ID 集合（running + queued）—— 生成按钮的 loading 判据。 */
  const genQueueItemIds = useMemo(() => {
    const ids = new Set<string>(genQueue.queued.map((q) => q.item_id));
    if (genQueue.running?.item_id) ids.add(genQueue.running.item_id);
    return ids;
  }, [genQueue]);

  /**
   * 批量删除本地台账行（v1.36，用户口径「商品列表新增批量删除操作」）。
   *
   * 口径与两个连带后果**只在 lib 里**（`batchDeleteBlock` / `batchDeleteHint`），
   * 这里只负责编排：拒绝 → 说明，允许 → 确认框 → 调接口 → 清勾选 → 重拉本页。
   *
   * 三个「不许静默」的点：
   * 1. 拒绝原因分两种（没勾 / 超上限）给**不同**文案 —— 压成一句「无法删除」等于
   *    让用户自己猜；
   * 2. `deleted < requested` 时如实说（传进来不存在的 item_id 会少删），
   *    不把「请求了几条」当「删了几条」；
   * 3. 本页被整页删空且不在第一页 ⇒ **回退一页**再拉（否则停在一张空表上，
   *    用户以为整个列表没了）。
   */
  async function batchDelete() {
    if (!selectedAccountId) return;
    const ids = normalizeDeleteIds(selectedItemIds);
    setBatchDeleting(true);
    try {
      const result = await deleteXhsShopItems(selectedAccountId, ids);
      setSelectedItemIds([]);
      if (result.deleted === result.requested) {
        message.success(`已删除本地台账 ${result.deleted} 行`);
      } else {
        message.warning(
          `已删除 ${result.deleted} 行（提交 ${result.requested} 个，其余在本地台账里不存在）`
        );
      }
      const removedOnPage = data
        ? data.items.filter((item) => ids.includes(item.item_id)).length
        : 0;
      const nextPage =
        data && removedOnPage >= data.items.length && pageNo > 1 ? pageNo - 1 : pageNo;
      setPageNo(nextPage);
      await load(selectedAccountId, nextPage, pageSize);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "批量删除失败");
    } finally {
      setBatchDeleting(false);
    }
  }

  /**
   * 批量**商品操作**（上架 / 下架 / 删除平台商品；2026-10-05 需求 7/8）。
   *
   * 编排（判据住在 lib，页面只负责「拒绝 → 说明 / 允许 → 二次确认 → 发请求 → 开结果弹窗」）：
   * 1. 勾选为空 / 超上限 ⇒ **两句话分开说**（压成一句「无法执行」等于让用户自己猜）；
   * 2. 确认框必须讲清「这是**真实修改平台**」（`batchOperateHint`）；
   * 3. 提交成功只拿到 `task_id` ⇒ 打开结果弹窗开始轮询 —— **不在这里假装有进度**。
   */
  async function handleBatchItemOperate(action: string) {
    if (!selectedAccountId) return;
    const block = batchOperateBlock(selectedItemIds);
    if (block === "empty") {
      message.info("先勾选要操作的商品");
      return;
    }
    if (block === "too_many") {
      message.warning(
        `单次最多操作 ${MAX_OPERATE_ITEMS} 个商品，本次选了 ${normalizeDeleteIds(selectedItemIds).length} 个，请分批操作`
      );
      return;
    }
    const ids = normalizeDeleteIds(selectedItemIds);
    const label = OPERATE_LABELS[action] || action;
    Modal.confirm({
      centered: true,
      title: `对 ${ids.length} 个平台商品执行「${label}」？`,
      content: (
        <div style={{ whiteSpace: "pre-wrap" }}>{batchOperateHint(action, ids.length)}</div>
      ),
      okText: `确认${label}`,
      cancelText: "取消",
      okButtonProps: { danger: action === "delete" },
      onOk: async () => {
        setOperatingAction(action);
        try {
          const result = await operateShopItems(selectedAccountId, action as never, ids);
          if (result.status === "pending" || result.status === "running") {
            message.success(`已开始${label} ${ids.length} 个商品（后台逐条执行，结果见下方弹窗）`);
          }
          setOperateTask(result);
          // 有任务号才轮询（`reused` 的既有任务也带 task_id ⇒ 照样接管它的进度）。
          if (result.task_id) startOperatePolling(result.task_id);
        } catch (error) {
          message.error(readErrorDetail(error, `${label}失败`));
        } finally {
          setOperatingAction(null);
        }
      },
    });
  }

  /**
   * 进入等待：起表 + 挂蒙版。**幂等**（已在等待时不重起秒表 —— 否则耗时被反复清零）。
   */
  function beginWait(label: string) {
    setWaitLabel(label);
    if (waitStartedAtRef.current === null) {
      waitStartedAtRef.current = Date.now();
      setWaitElapsedSec(0);
      if (waitTickRef.current !== null) window.clearInterval(waitTickRef.current);
      waitTickRef.current = window.setInterval(() => {
        if (waitStartedAtRef.current !== null) {
          setWaitElapsedSec(Math.floor((Date.now() - waitStartedAtRef.current) / 1000));
        }
      }, 1000);
    }
  }
  /** 结束等待：**清标签 + 停表**（两个都要，漏一个就是「蒙版没了数字还在跳」）。 */
  function endWait() {
    setWaitLabel(null);
    waitStartedAtRef.current = null;
    if (waitTickRef.current !== null) {
      window.clearInterval(waitTickRef.current);
      waitTickRef.current = null;
    }
  }

  /** 轮询商品操作任务（5s 一轮，终态即停 —— 与三张队列进度卡同一节奏）。 */
  function startOperatePolling(taskId: number) {
    stopOperatePolling();
    //: 秒表从这里起算（每次新操作重置）；1s 一跳，终态由 `pollOperateTask` 停。
    operateStartedAtRef.current = Date.now();
    setOperateElapsedSec(0);
    if (operateTickRef.current !== null) window.clearInterval(operateTickRef.current);
    operateTickRef.current = window.setInterval(() => {
      if (operateStartedAtRef.current !== null) {
        setOperateElapsedSec(Math.floor((Date.now() - operateStartedAtRef.current) / 1000));
      }
    }, 1000);
    operatePollRef.current = window.setInterval(() => {
      void pollOperateTask(taskId);
    }, QUEUE_POLL_INTERVAL_MS);
    void pollOperateTask(taskId);
  }

  function stopOperatePolling() {
    if (operatePollRef.current !== null) {
      window.clearInterval(operatePollRef.current);
      operatePollRef.current = null;
    }
    //: 秒表也要停（任务终态了还继续跳会让用户以为还在跑）。
    if (operateTickRef.current !== null) {
      window.clearInterval(operateTickRef.current);
      operateTickRef.current = null;
    }
  }

  /** 非终态 = 结果还没出来 ⇒ 弹窗**不许关**（用户口径「执行结果返回后再支持关闭」）。 */
  function operateTaskRunning(): boolean {
    return (
      operateTask !== null &&
      (operateTask.status === "pending" || operateTask.status === "running")
    );
  }

  /** 弹窗关闭的唯一出口（只在非终态之外被调用 —— 三条关闭路径共用一份收尾）。 */
  function closeOperateResult() {
    if (operateTaskRunning()) {
      //: 兜底：即便某条路径漏判，也不许在结果出来前关掉（用户会丢掉结果）。
      message.info("操作还在执行，结果返回后即可关闭");
      return;
    }
    stopOperatePolling();
    setOperateTask(null);
    setSelectedItemIds([]);
  }

  async function pollOperateTask(taskId: number) {
    try {
      const snapshot = await fetchShopItemOperateResult(taskId);
      setOperateTask(snapshot);
      if (snapshot.status !== "pending" && snapshot.status !== "running") {
        stopOperatePolling();
        // 终态：**如实分层报账**（成功 / 未执行 / 失败三数）——
        // 只报「已完成」会把「门禁拦下的那些」藏起来。
        const parts = [`${OPERATE_LABELS[snapshot.action] || snapshot.action}完成：成功 ${snapshot.succeeded}`];
        if (snapshot.skipped) parts.push(`未执行 ${snapshot.skipped}`);
        if (snapshot.failed) parts.push(`失败 ${snapshot.failed}`);
        const text = parts.join("，");
        if (snapshot.failed) message.warning(text);
        else message.success(text);
        // 操作改的是平台事实 ⇒ 重拉当前页（本地状态已被服务端刷新过）。
        if (selectedAccountId) void load(selectedAccountId, pageNo, pageSize);
      }
    } catch {
      // 安静重试（下一轮）—— 轮询失败不该打断用户看结果
    }
  }

  /** 离开页面时停轮询（定时器泄漏会让返回后再也停不下来）。 */
  useEffect(() => () => stopOperatePolling(), []);
  /** 离开页面必须停表（定时器泄漏会让返回后数字还在跳）。 */
  useEffect(() => () => {
    if (waitTickRef.current !== null) window.clearInterval(waitTickRef.current);
  }, []);

  /** LLM 生成入队成功 → 登记进轮询集合（操作列按钮转圈）。 */
  function handleLlmQueued(itemId: string) {
    setLlmQueueItemIds((current) => new Set(current).add(itemId));
    setLlmGenerating(null);
  }

  //: **旋转图标的清除**（2026-10-18）：进度卡移除后，`llmQueueItemIds` 就没人删了
  //: ⇒ 按钮会**永远转下去**（看着像卡死）。
  //:
  //: 判据用 `finished`（近期终态窗口的 `item_id`）：某个 item 出现在 `finished` 里
  //: ⇒ 它已出结果 ⇒ 停转。**不弹任何消息** —— 失败原因仍在版本记录的详情抽屉里，
  //: 不在列表页做「加载时把昨天的失败又弹一遍」那种事（那正是被删掉的进度卡的毛病）。
  //:
  //: ⚠️ 用 `finished` 而不是「不在 running/queued 里」：`running` / `queued` 是**计数**
  //: 不是清单，拿不到 item_id。
  useEffect(() => {
    if (llmQueueItemIds.size === 0) return;
    let alive = true;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const snap = await fetchLlmGenerateQueue();
          if (!alive) return;
          const settled = new Set(snap.finished.map((entry) => entry.item_id));
          setLlmQueueItemIds((current) => {
            const next = new Set([...current].filter((id) => !settled.has(id)));
            return next.size === current.size ? current : next;
          });
        } catch {
          //: 取不到不停（瞬时网络问题），但也不清 —— 宁可多转几秒，别误判「已完成」
        }
      })();
    }, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [llmQueueItemIds]);

  /**
   * 同步该账号的**全部状态**商品到本地台账（v2.13，设计 13 §30.3.2）。
   *
   * 2026-10-02 用户口径「商品同步更新需求为同步全部状态的商品」：不传 `itemStatus`
   * ⇒ 后端用默认 `all`（平台侧不传 `card_type`，翻页拿全部状态）。台账是「商品列表
   * 页的唯一数据源」，少了已下架 / 已冻结 / 审核态，列表的状态筛选与批量删除的
   * 连带判断都会静默失真。
   *
   * 触发是**异步回执**（后台任务）：成功后只报「已开始同步」，进度看账号矩阵的
   * 任务日志 / 台账「同步于」时间。⚠️ 不做前端乐观刷新 —— 同步没跑完就重拉列表，
   * 只是把同一份旧数据渲染一遍（看起来像「同步没好」）。
   */
  async function syncAllStatusGoods(options?: SyncNotesOptions) {
    if (!selectedAccountId) return;
    setGoodsSyncing(true);
    beginWait("同步商品");
    try {
      const result = await syncShopGoods({
        platformAccountId: selectedAccountId,
        ...(options ?? {}),
      });
      //: ⚠️ 文案与账号卡片页 `startGoodsSync` **逐字一致**（需求 5：只是多一个入口）。
      if (result.reused) {
        message.info("该账号已有同步任务在跑，本次未触发新执行。");
      }
      //: 触发响应本身就是「第一个拿到的信息」⇒ 立刻入集（按钮马上变「同步中…」），
      //: 不用等 5 秒后的第一轮轮询；入轮询集是为了**任务结束时把按钮切回来**。
      setGoodsSyncIds((current) => new Set(current).add(selectedAccountId));
    } catch (error) {
      message.error(readErrorDetail(error));
    } finally {
      setGoodsSyncing(false);
      endWait();
    }
  }

  /** 停「同步商品」的 5s 轮询（离开页面 / 任务终态时调用）。 */
  function stopGoodsSyncPolling() {
    if (goodsSyncPollRef.current !== null) {
      window.clearInterval(goodsSyncPollRef.current);
      goodsSyncPollRef.current = null;
    }
  }

  /**
   * 查一次同步状态：**延续**与账号卡片页同一条判据 `syncTaskPhase`。
   *
   * * `active` ⇒ 该账号入集（按钮显示「同步中…」+ 继续轮询）；
   * * 其余 ⇒ 出集（没有任务 / 任务已结束都不会再变）。
   *
   * ⚠️ 首查与轮询共用本函数（同一份判据，两处调用才算「一致」而不是「各写一遍」）。
   */
  const queryGoodsSyncStatus = useCallback(async (accountId: number) => {
    try {
      const status = await syncShopGoodsStatus(accountId);
      const active = syncTaskPhase(status) === "active";
      setGoodsSyncIds((current) => {
        if (active === current.has(accountId)) return current;
        const next = new Set(current);
        if (active) next.add(accountId);
        else next.delete(accountId);
        return next;
      });
      return active;
    } catch {
      // 状态查不到不打扰用户（下次轮询 / 下次进页面会再查）。
      return false;
    }
  }, []);

  /**
   * 有账号在跑同步 ⇒ 每 5s 复查一次（与账号卡片页同节拍）。
   *
   * ⚠️ 轮询**只在有 active 账号时**存在：全空闲时不留定时器（否则页面开着就在
   * 无谓地打后端）。集合变空 ⇒ 自动停。
   */
  useEffect(() => {
    stopGoodsSyncPolling();
    if (!selectedAccountId || !goodsSyncIds.has(selectedAccountId)) return;
    goodsSyncPollRef.current = window.setInterval(() => {
      void queryGoodsSyncStatus(selectedAccountId);
    }, 5000);
    return () => stopGoodsSyncPolling();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountId, goodsSyncIds, queryGoodsSyncStatus]);

  /** 离开页面停轮询（定时器泄漏会让返回后再也停不下来，同操作轮询的教训）。 */
  useEffect(() => () => stopGoodsSyncPolling(), []);

  /**
   * **首查**：选中的账号变了 / 进页面时先问一次状态。
   *
   * 为什么必须有：没有它，换台设备 / 刷新页面 / 别人（或定时任务）触发的同步，
   * 本页一律显示成「空闲」—— 而任务其实在跑。账号卡片页 §35.17.4 为同一个原因
   * 加了首查，这里沿用（需求 5 的「逻辑一致」包括这一条）。
   */
  useEffect(() => {
    if (!selectedAccountId) {
      setGoodsSyncIds(new Set());
      return;
    }
    void queryGoodsSyncStatus(selectedAccountId);
  }, [selectedAccountId, queryGoodsSyncStatus]);

  /**
   * **单行**商品操作（操作列的按钮；2026-10-05 需求 8）。
   *
   * ⚠️ 与 `handleBatchItemOperate` 是**两个入口、一份判据**：
   * * 不去复用批量那个函数 —— 它的 id 来源是**勾选集**，单行点它会「顺带操作所有勾选的商品」
   *   （用户只想动这一行，结果动了 5 行，写操作还撤不回来）；
   * * 但仍共用 lib 的 `batchOperateHint` / 后端下发的 `allowed_actions`，
   *   所以「这是真实平台写操作」的措辞与「这个状态能不能做」的结论两处一致。
   *
   * 按钮的**可点性**由后端 `allowed_actions` 决定（不可做时按钮已置灰 + Tooltip 给原因），
   * 这里只做「防连点」与二次确认。
   */
  async function operateOneItem(item: XhsShopItem, action: string) {
    if (!selectedAccountId || operatingAction !== null) return;
    const label = OPERATE_LABELS[action] || action;
    Modal.confirm({
      centered: true,
      title: `对「${item.title || item.item_id}」执行「${label}」？`,
      content: <div style={{ whiteSpace: "pre-wrap" }}>{batchOperateHint(action, 1)}</div>,
      okText: `确认${label}`,
      cancelText: "取消",
      okButtonProps: { danger: action === "delete" },
      onOk: async () => {
        setOperatingAction(action);
        try {
          const result = await operateShopItems(selectedAccountId, action as never, [item.item_id]);
          setOperateTask(result);
          if (result.task_id) startOperatePolling(result.task_id);
        } catch (error) {
          message.error(readErrorDetail(error, `${label}失败`));
        } finally {
          setOperatingAction(null);
        }
      },
    });
  }

  /** 删除按钮的入口：先按 lib 判据分流（拒绝就给一句准话），再弹确认框。 */
  function handleBatchDelete() {
    const block = batchDeleteBlock(selectedItemIds);
    if (block === "empty") {
      message.info("先勾选要删除的商品");
      return;
    }
    if (block === "too_many") {
      message.warning(
        `单次最多删除 ${MAX_BATCH_DELETE} 个商品，本次选了 ${normalizeDeleteIds(selectedItemIds).length} 个，请分批删除`
      );
      return;
    }
    const count = normalizeDeleteIds(selectedItemIds).length;
    // ⚠️ `centered: true` 必须**逐点显式传**：`Modal.confirm` 走的是静态方法分支
    //（`modal/confirm.js` 自建 ConfirmDialog），`ConfigProvider modal={{centered:true}}`
    //（`providers.tsx` 里给的是 ConfigContext）**到不了它** ⇒ 不传就还是顶部对齐。
    Modal.confirm({
      centered: true,
      title: `删除本地台账的 ${count} 个商品？`,
      // 确认框是**唯一**能讲清两个连带后果的地方（见 `batchDeleteHint` docstring）：
      // 换行靠 `whiteSpace: pre-wrap`，别把 ①② 挤成一行。
      content: (
        <div style={{ whiteSpace: "pre-wrap" }}>{batchDeleteHint(count)}</div>
      ),
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => batchDelete(),
    });
  }

  /**
   * 打开商品详情弹窗（v1.32，设计 13 §30.3.6）。
   *
   * 数据来自 `/item-detail`（**纯本地**：基本信息 + 图集 + SKU 规格明细）。
   * SKU 没补过 ⇒ `skus=[]` + `detail_synced_at=""`，渲染空态 + 说明，
   * **不渲染成 0 行**（缺值口径）。
   */
  async function openDetail(item: XhsShopItem) {
    if (!selectedAccountId) return;
    setDetailOpen(true);
    setDetailLoading(true);
    beginWait("加载商品详情");
    setDetail(null);
    setDetailError("");
    // 每次打开都清掉上一次的出图结果：那是**上一次动作**的产物，
    // 留着会让用户以为「这个商品已经有详情图了」。
    setDetailImage(null);
    try {
      setDetail(await fetchXhsShopItemDetail(selectedAccountId, item.item_id));
    } catch (error) {
      setDetail(null);
      setDetailError(error instanceof Error ? error.message : "商品详情加载失败");
    } finally {
      setDetailLoading(false);
      endWait();
    }
  }

  /**
   * 打开「生成商品详情图」独立弹窗（v2.12，列表操作列入口）。
   *
   * v2.15：打开时**拉一次商品详情**取两套图（主图 / 原商品详情图）供勾选 ——
   * 图集是只读本地台账的（`/item-detail` 零平台请求），所以这个请求很轻。
   *
   * 每次打开**重置全部状态**（各组配置 / 出图 / 报错 / 勾选）——
   * 弹窗是「一次生成动作」的载体，带着上一个商品的结果重开会把 A 的图挂在 B 名下
   * （与 `batchDeleteHint` 同一条纪律：跨商品的操作上下文不能串）。
   */
  /**
   * v2.30：打开某商品的**生成记录**弹窗（用户口径「商品列表操作列新增查看入口」）。
   *
   * ⚠️ 空态要**说清为什么空**：老图没有关联（v2.30 之前生成的 `item_id` 为空）
   * ⇒ 显示「该商品还没有生成记录（v2.30 之前生成的不计入）」，
   * 而不是一句干巴巴的「暂无数据」（用户会以为是坏了）。
   */
  async function openGenerations(item: XhsShopItem) {
    setGenerationsItem(item);
    setGenerationsOpen(true);
    setGenerations([]);
    setGenerationsTotal(0);
    setGenerationsLoading(true);
    beginWait("加载生成记录");
    try {
      const result = await fetchItemGenerations({
        platform_account_id: selectedAccountId || 0,
        item_id: item.item_id,
        page: 1,
        page_size: 60,
      });
      setGenerations(result.items);
      setGenerationsTotal(result.total);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "生成记录加载失败");
    } finally {
      setGenerationsLoading(false);
      endWait();
    }
  }

  /**
   * 导出当前筛选条件下的 CSV（设计 20 §2.1 端点 6）。
   *
   * ⚠️ 超限**不发请求**（`exportBlockReason` 与后端上限同值）—— 后端会整批 400，
   * 前端先拦省一次往返；且判据同值（两份不同上限 = 用户看到的与实际的不是一回事）。
   */
  async function exportCsv() {
    if (!selectedAccountId) return;
    const total = data?.total ?? 0;
    const blocked = exportBlockReason(total);
    if (blocked) {
      message.warning(blocked);
      return;
    }
    setExporting(true);
    beginWait("导出 CSV");
    try {
      // DEF-093：导出与列表**同一个构造**（含同一份筛选 / 排序 / 分组），
      // 只是不带分页 —— 导出的就是「当前筛选命中的全部」。
      const blob = await exportShopItems(
        formValuesToQuery(formSnapshot(), {
          accountId: selectedAccountId,
          sort,
        })
      );
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      // ⚠️ 中文文件名让浏览器自己定（后端响应头已按 RFC 5987 带了 UTF-8 名）
      anchor.download = "";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      message.success(`已导出 ${total} 行`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "导出失败，请稍后重试。");
    } finally {
      setExporting(false);
      endWait();
    }
  }

  function openDetailImage(item: XhsShopItem) {
    setDetailImageItem(item);
    // 2026-10-05：顶层 Tab 每次打开回「生成配置」（跨商品不串 —— 同上面那条纪律）。
    setDetailImageTab("config");
    setDetailImage(null);
    setDetailImageLoading(true);
    setDetailImageOpen(true);
    setDetailImageRefs([]); // 待详情回来后默认勾首图
    // v2.22：各组配置（提示词 / 张数）与当前 Tab 一并复位（同一条「跨商品不串」纪律）。
    setDetailImageGroupConfigs({});
    setActiveGroup("product");
    setDetailImageRegenerating(null); // v2.21：重做态一并复位
    // v2.29：勾选与保存目标一并复位（跨商品不串 —— 同上面那条纪律）
    setDetailImageSelectedIds([]);
    setDetailImageMaterialFolderId(null);
    // v2.29：拉素材库清单（保存目标下拉的数据源；与模板一样**只在首次打开时拉**）
    if (materialFolders.length === 0) {
      void (async () => {
        try {
          setMaterialFolders(await fetchMaterialFolders());
        } catch {
          // 拉不到就保持空 —— 保存时会给「还没有素材库文件夹」的可执行提示，
          // 不在这里弹错（弹了也做不了什么，反而干扰主流程）
        }
      })();
    }
    // v2.24：拉公共提示词模板（首次打开才拉 —— 模板是用户级的，与商品无关）
    if (!detailImageTemplatesLoaded) {
      void (async () => {
        try {
          const page = await fetchPromptTemplates({ pageSize: 100 });
          setDetailImageTemplates(page.items);
          // 有默认模板时**预选上**：用户口径是「应选择公共提示词模板」——
          // 默认不选等于让人自己去找，一致性约束就落不了地（但**不强制**：
          // 用户清空后仍可走「手输 / 内置默认」，那种情况后端行为不变）。
          setDetailImageGroupConfigs((current) => {
            const next = { ...current };
            for (const key of ["product", "detail"] as const) {
              const taskType = key === "product" ? "cover" : "image";
              const fallback = page.items.find((t) => t.is_default && t.task_type === taskType);
              if (fallback) next[key] = { ...next[key], templateId: fallback.id };
            }
            return next;
          });
        } catch (error) {
          message.warning(
            error instanceof Error ? error.message : "提示词模板加载失败，可继续手写提示词"
          );
        } finally {
          setDetailImageTemplatesLoaded(true);
        }
      })();
    }
    void (async () => {
      if (!selectedAccountId) return;
      try {
        const detail = await fetchXhsShopItemDetail(selectedAccountId, item.item_id);
        setDetailImageDetail(detail);
        // 默认勾**商品主图首张**（与旧行为一致：不勾也能生成，见后端回落）
        const first = (detail.product_images || [])[0] || (detail.images || [])[0] || "";
        setDetailImageRefs(first ? [first] : []);
      } catch (error) {
        // 取不到图集 ⇒ 不阻断弹窗（用户仍可点生成，后端会给结构化缺图原因）
        setDetailImageDetail(null);
        message.warning(
          error instanceof Error ? error.message : "商品图集加载失败，仍可尝试生成"
        );
      } finally {
        setDetailImageLoading(false);
      }
    })();
  }

  /**
   * 生成商品详情图（设计 17 §13.3.3，v2.1；v2.12 改由独立弹窗驱动；v2.15 支持勾选参考图）。
   *
   * 只传 `accountId + itemId + prompt + 勾选的参考图`：候选图来自服务端下发的台账图集，
   * 后端按白名单校验（不接受任意 URL —— SSRF 防线）。
   * 失败信息原样报出 —— 最常见的两种（没配服务商 / 服务商不是图像类）后端给的是
   * 带 `code` 的可执行提示，吞成「生成失败」会让用户不知道该去配什么。
   */
  async function generateDetailImage(regenerate?: { group: string; index: number }) {
    if (!selectedAccountId || !detailImageItem) return;
    setDetailImageGenerating(true);
    beginWait("生成商品AI图");
    setDetailImageStartedAt(Date.now()); // v2.28：开始计时（弹窗里显示已耗时）
    // v2.21：记住重做目标（只让**那一张**转圈，不整组变灰 —— 用户要能看出在重做哪张）。
    setDetailImageRegenerating(regenerate || null);
    try {
      // v2.22/v2.23：**每组各自配置**随请求下发。
      // ⚠️ 组的「存在性」= 本组有勾图 **或** 它是本次生成的目标组。
      // 目标组没勾本组图时，后端回落到全部已勾图（v2.23 用户口径「参考图列出所有图」）。
      const targetGroup = regenerate?.group || activeGroup;
      const groupSettings: Record<string, { prompt: string; count: number }> = {};
      // v2.24：组级模板（与 groupSettings **平行**的另一张表 —— 提示词与模板是两件事，
      // 混在一张表里会让「选了模板又填了提示词」的语义变模糊）。
      const groupTemplates: Record<string, { template_id: number }> = {};
      for (const tab of detailImageGroupTabs) {
        const picked = detailImageRefs.some((url) => tab.urls.includes(url));
        if (!picked && tab.key !== targetGroup) continue;
        groupSettings[tab.key] = {
          prompt: detailImageGroupConfigs[tab.key]?.prompt || "",
          count: detailImageGroupConfigs[tab.key]?.count ?? 3,
        };
        const tid = detailImageGroupConfigs[tab.key]?.templateId;
        if (tid) groupTemplates[tab.key] = { template_id: tid };
      }
      const result = await generateProductDetailImage(
        selectedAccountId,
        detailImageItem.item_id,
        "",
        detailImageRefs,
        3,
        regenerate,
        groupSettings,
        undefined,
        groupTemplates
      );
      setDetailImage(result);
      // v2.18/v2.20：半成功（部分张失败）如实报出来 —— 不能只报「已生成」，
      // 否则用户以为都出来了（缺值口径：不假报完成）。
      const partial = result.errors?.length || 0;
      const totalDone = result.assets?.length || 1;
      if (partial) {
        message.warning(
          `已生成 ${totalDone} 张，${partial} 张失败：${result.errors?.[0] || ""}`
        );
      } else {
        message.success(`已生成 ${totalDone} 张`);
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : "生成商品AI图失败");
    } finally {
      setDetailImageGenerating(false);
      endWait();
      setDetailImageStartedAt(null); // v2.28：停表
      setDetailImageRegenerating(null);
    }
  }

  /** v2.22：当前 Tab 的组名 / 参考图（footer 按钮与守卫都读它）。 */
  const activeGroupLabel = activeGroup === "product" ? "原始主图" : "原始详情图";
  /**
   * v2.25：**生成按钮的标签**（用户口径「底部按钮应『生成主图/生成详情图』，
   * 不是『生成原始图/生成原始详情图』」）。
   *
   * ⚠️ **不能复用 `activeGroupLabel`**：那个词描述的是**参考图的来源**
   * （「原始主图」= 商品原始图，用于「可选参考图」那块的分组标题），
   * 而按钮说的是**要生成什么**（「生成主图」）。
   * 同一变量担两个语义 ⇒ 改一处会同时改错的另一处（本轮就是这么来的）。
   */
  /**
   * v2.29：把生成结果保存到素材库（用户口径「支持选择性保存到指定素材库，
   * 支持全部保存到指定素材库」）。
   *
   * `scope="selected"` 存勾选的、`scope="all"` 存本次全部结果 ——
   * **同一个后端端点**（`asset_ids` 给几张就存几张）：不另开 `save-all`，
   * 那是同一件事的两份实现（DEF-044），且「全部」的定义在前端手里。
   *
   * ⚠️ **`asset_groups` 必须一起传**：分组事实只在生成响应里（每张的 `group`），
   * 后端反查不到（`Task.payload` 没有 asset 明细、`AigcMediaAsset` 没有 group 列）
   * ⇒ 不传就只能写 `图-NN`，用户看不出哪张是主图、哪张是详情图。
   */
  async function saveDetailImagesToMaterial(scope: "selected" | "all") {
    const allAssets = (detailImage?.groups || []).flatMap((g) => g.assets || []);
    const targets = scope === "all" ? allAssets.map((a) => a.id) : detailImageSelectedIds;
    if (targets.length === 0) {
      message.warning(scope === "all" ? "本次还没有可保存的结果" : "请先勾选要保存的图");
      return;
    }
    const folderId = detailImageMaterialFolderId ?? materialFolders[0]?.id;
    if (!folderId) {
      message.warning("还没有素材库文件夹，请先到「笔记工坊 → 素材库」新建");
      return;
    }
    setDetailImageSaving(true);
    try {
      const groups: Record<string, string> = {};
      for (const asset of allAssets) {
        if (asset.group) groups[String(asset.id)] = asset.group;
      }
      const result = await saveDetailImages({
        folder_id: folderId,
        asset_ids: targets,
        asset_groups: groups,
      });
      const failed = result.errors?.length ?? 0;
      if (failed) {
        message.warning(
          `已保存 ${result.saved.length} 张到「${result.folder_name}」，${failed} 张失败`
        );
      } else {
        message.success(`已保存 ${result.saved.length} 张到「${result.folder_name}」`);
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存到素材库失败");
    } finally {
      setDetailImageSaving(false);
    }
  }

  const activeGenerateLabel = activeGroup === "product" ? "主图" : "详情图";
  const activeGroupUrls =
    activeGroup === "product"
      ? detailImageDetail?.product_images || []
      : detailImageDetail?.detail_images || [];
  /**
   * ⚠️ **不能按组过滤**（v2.29.7 修「详情图 Tab 勾了图按钮还是灰」）。
   *
   * 历史：勾选区（`ReferenceImagePicker`）曾经**按组**显示（主图一组、详情图一组），
   * 那时「本组勾了几张」= 「按钮能不能点」。后来（v2.23 用户口径）两组合并成
   * **一个「可选参考图」**（`product_images ++ detail_images`），但**按钮判据没跟着改** ——
   * 于是「在详情图 Tab 里勾了主图那组的图」会被 `filter` 全部滤掉 ⇒ `length === 0`
   * ⇒ **按钮恒灰**（用户现象：「详情图中已经选择了参考图…但右下角的生成详情图按钮
   * 无法点击是灰色」）。
   *
   * ⇒ 判据与勾选区**同一口径**：**勾了就能生成**（送哪几张由图本身决定，
   * 与当前 Tab 无关 —— 后端本来就按 `reference_image_urls` 收，不按组收）。
   */
  const activeGroupRefs = detailImageRefs;

  /**
   * v2.22：Tab 清单（**唯一来源**）—— 主图 / 详情图各自一套配置与结果。
   *
   * ⚠️ 用数组驱动 `Tabs`，不在 JSX 里写两份 —— 两份会在「加第三种图」时漏改一处。
   */
  const detailImageGroupTabs = [
    {
      key: "product" as const,
      label: "原始主图",
      urls: detailImageDetail?.product_images || [],
      emptyHint: "这个商品没有主图（先执行「同步商品」补齐图集）",
    },
    {
      key: "detail" as const,
      label: "原始详情图",
      urls: detailImageDetail?.detail_images || [],
      emptyHint: "这个商品没有详情图（平台侧未提供，或还没同步到）",
    },
  ];

  //: ⚠️ 用 `Form.useWatch` 订阅状态字段，**不能** `form.getFieldValue("status")` ——
  //:    后者是一次性读取，不进渲染依赖 ⇒ 折叠态展开后改状态时不重算（说明文案不出现）。
  const watchedStatus = Form.useWatch("status", form);
  const statusHint = statusFilterHint(watchedStatus || "all");

  /**
   * 某列当前是否处于排序激活态（antd 的 sortOrder：'ascend' | 'descend' | null）。
   *
   * 2026-09-23 起只有「上架时间」一列会调它（其余列不再挂 `sorter`）—— 保留成
   * 通用形态，是为了下次若真要放开某列，改的是 `SORT_FIELD_BY_COLUMN` 与列定义
   * 两处，而不是在这里补一段新判断。
   *
   * v1.36：`sort` 可为 `null`（无排序态）⇒ 此时所有列都返回 `null`，进来不带箭头。
   */
  const sortOrderFor = (dataIndex: string) =>
    sort && sort.field === SORT_FIELD_BY_COLUMN[dataIndex] ? sort.order : null;

  /**
   * 表头箭头点击 → 换排序并回第一页重拉（**服务端排序**）。
   *
   * ⚠️ antd 的 Table.onChange 在**分页/筛选变化时也会带当前 sorter 回调** ——
   * 用「新状态与旧状态相同则不动作」挡掉（否则翻到第 2 页会被打回第 1 页）。
   *
   * v1.36：新状态可能是 `null`（第三下「取消排序」）⇒ 比较必须按 `?? null` 归一，
   * 否则「取消」会被误判成「没变化」而**吞掉这一次回到默认序的请求**。
   */
  function handleSortChange(sorter: unknown) {
    const next = (Array.isArray(sorter) ? sorter[0] : sorter) as
      | { field?: string | number; order?: string | null }
      | undefined;
    const nextSort = resolveSort(
      next?.field != null ? String(next.field) : undefined,
      next?.order
    );
    const unchanged =
      (nextSort?.field ?? null) === (sort?.field ?? null) &&
      (nextSort?.order ?? null) === (sort?.order ?? null);
    if (unchanged) return;
    setSort(nextSort);
    setPageNo(1);
    if (selectedAccountId) void load(selectedAccountId, 1, pageSize, nextSort);
  }

  /**
   * 列定义。
   *
   * ⚠️ **只有「上架时间」一列带 `sorter`**（用户 2026-09-23 上机口径：「商品台账的
   * 只能根据上架时间列排序，其它列不支持排序」）。判据与 `SORT_FIELD_BY_COLUMN`
   * 同源：那里只有一个键、这里只有一列挂箭头，**两处必须一起改**（表头箭头是
   * 「后端真会按它排」的承诺，多一个箭头就多一份误导）。
   *
   * v1.36：`sorter: true` 是 antd 的**服务端排序**语义（`getSortFunction` 对
   * `true` 返回 `false` ⇒ antd 不本地排序，只回调 `onChange`），配合受控 `sortOrder`
   * 就得到原生三态：升 → 降 → 取消。**别给它传比较函数**，那会变成对当前页的假排序。
   */
  const columns: ColumnsType<XhsShopItem> = [
    {
      title: "商品",
      dataIndex: "title",
      // 缩略图 56 → 71（+15px）后内容宽度跟着长：71 + 10(gap) + 300(文本) = 381，
      // 小号表格单元格左右各 8px ⇒ 380 会挤到换行，故放宽到 400。
      width: 400,
      render: (_value, item) => (
        <Space align="start" size={10}>
          {/*
            缩略图容器（v1.35）：相对定位，专门用来承载「N 图」角标。
            `lineHeight: 0` 是必要的 —— antd `Image` 外层的 `.ant-image` 是
            行内元素，行高会在图片下方留一条基线缝隙，角标 `top: 0` 就会跟着偏。
          */}
          <div style={{ position: "relative", flex: "0 0 auto", lineHeight: 0 }}>
            {item.image ? (
              <Image
                src={item.image}
                width={ITEM_THUMB_SIZE}
                height={ITEM_THUMB_SIZE}
                style={{ objectFit: "cover", borderRadius: 4 }}
                preview={{ src: item.image }}
              />
            ) : (
              <div
                style={{
                  width: ITEM_THUMB_SIZE,
                  height: ITEM_THUMB_SIZE,
                  background: "#f5f5f5",
                  borderRadius: 4,
                }}
              />
            )}
            {/*
              「N 图」角标：**图片左上角**（v1.35，原先是标题后的行内 Tag）。
              口径不变：只有多图（>1）才提示，单图不占位置。
            */}
            {item.image_count > 1 && (
              <Tag style={IMAGE_COUNT_BADGE_STYLE}>{item.image_count} 图</Tag>
            )}
          </div>
          <div style={{ minWidth: 0, maxWidth: 300 }}>
            {/*
              标题行（v1.35）：**单行展示**。
              商品名普遍偏长，换行会把整行商品卡撑高、右边几列基线全错开；
              改用 `…` 收起 + Tooltip 悬停看全文（与下面分类行、挂链笔记 Tab
              同一口径，ADR-018）。原「N 图」Tag 已移到缩略图左上角，不再挤占标题宽度。

              ⚠️ 这里用「Tooltip + 手写 CSS」而不是 `Typography.Link` 的
              `ellipsis={{ tooltip }}`：antd 6.3.7 的 `LinkProps.ellipsis`
              类型只收 `boolean`（`Text` 才收配置对象），传对象会 TS2322。
            */}
            <Tooltip title={item.title || "（无标题）"}>
              <a
                onClick={() => void openDetail(item)}
                style={{
                  fontSize: 13,
                  display: "block",
                  maxWidth: 300,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {item.title || "（无标题）"}
              </a>
            </Tooltip>
            {/* 商品 ID 行（与货号分两行，v1.32） */}
            <div style={{ marginTop: 2 }}>
              <Space size={4}>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  ID {item.item_id}
                </Text>
                <Tooltip title="点击复制商品 ID">
                  <CopyOutlined
                    style={{ fontSize: 11, cursor: "pointer" }}
                    onClick={() => void copyToClipboard(item.item_id, "商品 ID 已复制", message)}
                  />
                </Tooltip>
              </Space>
            </div>
            {/* 货号行 */}
            <div style={{ marginTop: 2 }}>
              <Text type="secondary" style={{ fontSize: 11 }}>
                货号 {item.article_no || "-"}
              </Text>
            </div>
            {/*
              分类行（合并到货号下一行，v1.32）。

              2026-09-23 上机口径：**分类不自动换行**。千帆的分类是「/」串起来的
              多级路径（如「户外/登山/野营/旅行用品 / 露营/野炊装备 / 饮水用具/
              盛水容器 / 水盆」），换行后这一格会顶成两行甚至三行，把整行商品卡
              撑高、和右边几列的基线全错开。改用 `…` 收起 + Tooltip：**完整分类
              悬停可见、不丢信息**（ADR-018 的口径：长文案不换行，收起而不是截断）。
            */}
            <div style={{ marginTop: 2 }}>
              <Text
                type="secondary"
                ellipsis={{ tooltip: item.category || "未分类" }}
                style={{ fontSize: 11, display: "block", maxWidth: 300 }}
              >
                {item.category || "未分类"}
              </Text>
            </div>
          </div>
        </Space>
      ),
    },
    {
      title: "价格",
      dataIndex: "price_min_fen",
      width: 150,
      render: (_value, item) => <Text strong>{priceText(item)}</Text>,
    },
    {
      title: "库存 / SKU",
      dataIndex: "stock",
      width: 130,
      render: (_value, item) => (
        <div>
          <div>
            <Text>{item.stock ?? "-"}</Text>
          </div>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {item.on_sale_sku_count ?? "-"}/{item.sku_count ?? "-"} 在售
          </Text>
          {item.stock_warning_sku_count > 0 && (
            <div>
              <Tag color="orange" style={{ fontSize: 11 }}>
                {item.stock_warning_sku_count} 个规格库存告警
              </Tag>
            </div>
          )}
        </div>
      ),
    },
    {
      title: "销量",
      dataIndex: "sales_30d",
      width: 110,
      render: (_value, item) => (
        <div>
          <Text>近 30 天 {item.sales_30d ?? "-"}</Text>
          <div>
            <Text type="secondary" style={{ fontSize: 11 }}>
              累计 {item.sales_total ?? "-"}
            </Text>
          </div>
        </div>
      ),
    },
    {
      title: "挂链笔记",
      dataIndex: "note_count",
      width: 100,
      // 2026-09-23 上机口径：「查看这些笔记（关系表口径，零平台请求）。本地关系 N 篇；
      // 平台侧记 M 篇…」这条 Tooltip 文案**移除** —— 与弹窗那条说明同一批清掉，
      // 挂链列就只剩一个可点的篇数（`platform_note_count` 仍在响应里，留给需要对照的
      // 消费者，界面上不再解释两个口径为什么不等）。
      render: (_value, item) =>
        item.note_count > 0 ? (
          <Tag
            color="geekblue"
            style={{ cursor: "pointer" }}
            onClick={() => void openItemNotes(item)}
          >
            {item.note_count} 篇
          </Tag>
        ) : (
          <Text type="secondary">0</Text>
        ),
    },
    {
      // 上架时间与状态合并为一列两行（v1.32）；表头箭头按上架时间排。
      // 宽度按「秒级时间」留足（设计 13 §32.1：`YYYY-MM-DD HH:MM:SS` 共 19 字符，
      // 原来按分钟精度配的 140px 会折行）。
      title: "上架时间 / 状态",
      dataIndex: "on_shelf_time",
      width: 168,
      sorter: true,
      sortOrder: sortOrderFor("on_shelf_time"),
      render: (_value, item) => (
        <div>
          <div>
            <Text style={{ fontSize: 12 }}>{item.on_shelf_time || "-"}</Text>
          </div>
          <Space direction="vertical" size={2} style={{ marginTop: 2 }}>
            <Space size={4} wrap>
              {item.status_label ? (
                <Tag color={statusTagColor(item.status_key || "")}>{item.status_label}</Tag>
              ) : (
                <Text type="secondary" style={{ fontSize: 11 }}>
                  状态 -
                </Text>
              )}
              {item.frozen && <Tag color="red">已冻结</Tag>}
              {item.stock_warning_sku_count > 0 && <Tag color="orange">库存风险</Tag>}
            </Space>
          </Space>
        </div>
      ),
    },
    // ⚠️ 「分组」列已随「彻底移除商品分组」退场（2026-10-05）。
    {
      title: "操作",
      key: "action",
      // v2.31（用户口径「商品操作列：一行两个操作功能」）：操作排 **2 列**。
      // 2026-10-05：新增 上架/下架/删除（按状态各显其一）+ 移除「生成记录」
      // ⇒ 列宽 190 → 260（2 列放 6 个按钮 = 3 行，窄了会挤成竖排单列）。
      // ⚠️ 用 grid 而不是 `Flex wrap` —— 换行位置由**列宽**决定（浏览器按剩余空间算），
      // 容器宽度一变就可能变成 3+1；grid 是**声明式两列**，与容器宽度无关。
      width: 260,
      render: (_value, item) => {
        //: 后端下发的可执行动作（**判据不在前端复算** —— `supported` 直接读后端，
        //: 所以「按钮为什么点不了」永远能回答；见需求 8 / 设计 20 §11）。
        //: ⚠️ `([])` 的写法是**故意的**：守卫用 `_slice_between(page, 'title: "操作"', "];")`
        //: 切操作列，裸写 `|| [];` 会让切片在**这里**就被截断（`[];` 里含 `];`），
        //: 后面的 grid 容器与按钮全部读不到 ⇒ 守卫报「不是 grid 容器」（本脚本踩过）。
        const allowed = (item.allowed_actions || []);
        //: 上架 / 下架 合成一个按钮：优先取**可执行的那个**；两个都不可执行
        //: （店铺冻结等）时回落取 on_shelf 那条，渲染置灰按钮 + 后端原因（不留空白）。
        const shelfAction =
          allowed.find(
            (e) => e.supported && (e.action === "on_shelf" || e.action === "off_shelf"),
          ) || allowed.find((e) => e.action === "on_shelf" || e.action === "off_shelf");
        const deleteAction = allowed.find((e) => e.action === "delete");
        return (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 4,
            alignItems: "center",
          }}
        >
          {/* 生成笔记（设计 13 §31.4，用户需求③；§31.10 起入队即返回）：
              调千帆后台智能生成一篇挂本商品的笔记（待发布，之后到内容库发布）。
              loading 判据 = 快照的 running + queued（队列在执行这一项），
              不只是「本次点击在等回执」—— 回执毫秒级，执行在后台。 */}
          <Tooltip title="千帆智能生成一篇挂本商品的笔记">
            <Button
              type="link"
              size="small"
              block
              icon={<FileAddOutlined spin={genQueueItemIds.has(item.item_id)} />}
              loading={generatingItemId === item.item_id || genQueueItemIds.has(item.item_id)}
              disabled={
                (generatingItemId !== null && generatingItemId !== item.item_id) ||
                (genQueueItemIds.size > 0 && !genQueueItemIds.has(item.item_id))
              }
              onClick={() => void generateNote(item)}
            >
              生成笔记
            </Button>
          </Tooltip>
          {/* LLM 生成（T9，设计 17 §7.4）：走项目自己的 LLM 生成链路（与千帆后台智能生成
              是两个队列、两条链路）。开的是**生成配置弹窗** —— 弹窗内用两大 Tab
              拆分「图文笔记 / 视频笔记」（用户口径 2026-10-16）。 */}
          <Tooltip title="用本项目的 LLM 链路生成笔记">
            <Button
              type="link"
              size="small"
              block
              icon={<RobotOutlined spin={llmQueueItemIds.has(item.item_id)} />}
              disabled={llmGenerating !== null}
              onClick={() => {
                setLlmGenerateItem(item);
                setLlmGenerateOpen(true);
              }}
            >
              LLM 生成
            </Button>
          </Tooltip>
          {/* 2026-10-06 需求 8：**上架 / 下架** —— 按后端下发的 `allowed_actions` 渲染
              （可做的给按钮，不可做的给**置灰 + 原因 Tooltip**；判据不在前端复算）。
              ⚠️ 两个动作**合成一个按钮**：`on_shelf` / `off_shelf` 是同一状态位的两面
              （在架 ⇒ 只能下架；下架 ⇒ 只能上架），对任一商品不可能同时可执行 ——
              原实现两个都渲、其中一个恒置灰，既占位又让人以为「两个都能点」。 */}
          {shelfAction ? (
            <Tooltip title={shelfAction.reason || `对平台商品执行「${shelfAction.label}」`}>
              <Button
                type="link"
                size="small"
                block
                disabled={!shelfAction.supported || operatingAction !== null}
                icon={
                  shelfAction.action === "on_shelf" ? (
                    <ArrowUpOutlined />
                  ) : (
                    <ArrowDownOutlined />
                  )
                }
                onClick={() => void operateOneItem(item, shelfAction.action)}
              >
                {shelfAction.label}
              </Button>
            </Tooltip>
          ) : null}
          {/* 2026-10-06 用户口径「正常只展示三个常用功能按钮和一个更多」：
              **AI商品图 / 编辑 / 删除** 收进「更多」下拉。

              ⚠️ AI商品图 与 编辑 是**无状态的一次性入口**（打开弹窗即完事），
              不像「生成笔记 / LLM 生成」那样带转圈进度、也不像上架/下架那样由商品状态驱动。
              删除是**破坏性**动作且不是高频操作 —— 收起来也少一次误触。

              ⚠️ 用 `dropdownRender` 而不是 `menu={{items}}`：后者会把按钮从 `Button`
              变成 `MenuItem`，同一列出现两种渲染形态（可点性 / 禁用判据分叉）。
              这里**按钮本身一个字符没改**，只是换了位置。 */}
          <Dropdown
            trigger={["click"]}
            dropdownRender={() => (
              <div
                style={{
                  background: "#fff",
                  borderRadius: 6,
                  boxShadow: "0 6px 16px rgba(0,0,0,0.12)",
                  padding: 6,
                  minWidth: 140,
                  display: "grid",
                  gap: 4,
                }}
              >
                {/* 生成商品详情图（设计 17 §13.3.3，v2.1；v2.12 移到操作列独立弹窗）：
                    AI 出图给**商品**用（与「生成笔记」的正文配图是两个业务点）。 */}
                <Tooltip title="AI 生成该商品的详情图 / 主图">
                  <Button
                    type="link"
                    size="small"
                    block
                    icon={<PictureOutlined />}
                    onClick={() => openDetailImage(item)}
                  >
                    AI商品图
                  </Button>
                </Tooltip>
                {/* 2026-10-06 用户口径：「列表操作列新增**同步商品信息**按钮，将顶部的
                    同步执行商品按钮功能合并到同步商品按钮中……注意合并后两个按钮的功能
                    需全部保留」⇒ 行内这一件走**同一套**同步链路（同一个弹窗 + 同一个
                    请求函数），只是 ID 清单缩成一行；打开即同步、状态在弹窗里逐条给。
                    ⚠️ 放「更多」里：它是**单件补数据**的次级入口，与顶部「批量同步」区分。 */}
                <Tooltip title="同步这一件商品的最新数据">
                  <Button
                    type="link"
                    size="small"
                    block
                    icon={<SyncOutlined />}
                    onClick={() => {
                      //: 单件不带勾选集语义 ⇒ 显式清掉勾选传递，避免误列别的行。
                      setSyncSingleTarget(item.item_id);
                      setSyncItemsOpen(true);
                    }}
                  >
                    同步商品信息
                  </Button>
                </Tooltip>
                {/* M4（设计 20 §5）：**商品编辑** —— 操作列独立弹窗。
                    ⚠️ 分组 Tab 已随分组功能退场（2026-10-05）。 */}
                <Tooltip title="编辑商品（AI 润色标题 / 卖点）">
                  <Button
                    type="link"
                    size="small"
                    block
                    icon={<EditOutlined />}
                    onClick={() => setEditingItem(item)}
                  >
                    编辑
                  </Button>
                </Tooltip>
                {/* 删除（需求 8）：仍是**状态驱动**的 —— `supported=false` 时置灰 + 后端原因
                    （如推广中不可删）。`danger` 保持红色，位置在下拉最后一位。 */}
                {deleteAction ? (
                  <Tooltip title={deleteAction.reason || `对平台商品执行「${deleteAction.label}」`}>
                    <Button
                      type="link"
                      size="small"
                      block
                      danger
                      disabled={!deleteAction.supported || operatingAction !== null}
                      icon={<DeleteOutlined />}
                      onClick={() => void operateOneItem(item, deleteAction.action)}
                    >
                      {deleteAction.label}
                    </Button>
                  </Tooltip>
                ) : null}
              </div>
            )}
          >
            <Button type="link" size="small" block icon={<EllipsisOutlined />}>
              更多
            </Button>
          </Dropdown>
          {/* ⚠️ 「生成记录」按钮**已移除**（2026-10-05 用户口径「将商品操作列的生
              成记录合并到AI商品图弹窗内，使用tab布局」）⇒ 入口搬进「AI商品图」弹窗的
              第二个顶层 Tab（见 `detailImageTab`）。被移除的原按钮（保留备查，勿恢复）：
                <Button icon={<HistoryOutlined />} onClick={() => void openGenerations(item)}>生成记录</Button> */}
        </div>
        );
      },
    },
  ];

  const waitRunning = waitLabel !== null;
  return (
    //: **页面级等待 = `<WaitMask>` 的居中白卡片**（用户口径 2026-10-20
    //: 「可以不要蒙版只要居中的弹窗提示」），不再有半透明底、也不再挡交互。
    //:
    //: ⚠️ **为什么这里不用 antd `<Spin spinning={false}>` 包整页**（实测根因，别再改回去）：
    //: antd 6 的 `Spin` **无条件渲染 `Indicator`** —— 即使 `spinning={false}` 且
    //: `indicator` 传空，也会多渲染一个 section。表现就是页面上多出一个「只有圈、
    //: 没有字」的转圈，与 `WaitMask` 的卡片并排 = 用户看到的「两个居中提示」。
    //: ⇒ 换成**同名类名的普通 div**（`.ant-spin` / `.ant-spin-container`）：
    //: `global.css` 里的三层高度链规则逐字不变（台账卡三区固定布局靠它），
    //: 而 antd 完全不参与渲染。
    <div className="ant-spin">
    <div className="ant-spin-container">
    <Flex
      vertical
      gap={10}
      //: **上中下三段式布局**（用户口径 2026-10-11「布局为上中下布局」「搜索区域固定；
      //: 商品台账顶部按钮与底部分页和统计数据固定不动，只有列表内的数据支持表格列滚动」）：
      //:   上 = 搜索条件卡（`flexShrink:0`，固定）
      //:   中 = 商品台账卡（`flex:1`，卡内再切「卡头按钮 / 表格数据 / 统计+分页」）
      //:   下 = 累计到台账卡底部（统计 + 分页，`flexShrink:0`，固定）
      //: 高度与图片空间同一套口径（视口 - 顶栏 48 - 内容上下内边距 15+24）。
      //: ⚠️ 用 antd `Flex`（不是手写 `display:flex`）—— 间距 / 方向 / 换行都由组件收口。
      //: 高度 = 视口 - 顶栏(48) - 内容区上下内边距(15 + 24) = `100vh - 87px`（用户口径
      //: 「底部撑满」：算准这一格才不会在底部留一条缝）。
      style={{ height: "calc(100vh - 87px)" }}
    >
      {/* ⚠️ **顶部「商品列表（千帆）」整卡已移除**（2026-10-05 用户口径
          「顶部的商品列表（千帆）整块移除」）。原卡片只剩两件事：
          ① 账号下拉（**搬到「搜索条件」卡右上角**，见下面 `搜索条件` 的 `extra`）；
          ② 空账号时的引导 Alert —— 它的判据（`accountsLoaded && accounts.length === 0`）
             与文案一并搬进「搜索条件」卡（账号缺失时整块表单本就没意义）。
          被移除的原结构（保留备查，勿恢复）：
            <Card title={<Space><ShoppingOutlined />商品列表（千帆）</Space>} extra={…账号下拉…}>
              {accountsLoaded && accounts.length === 0 ? <Alert …去「账号矩阵」添加账号…/> : …}
            </Card> */}

      {/* **搜索区**（用户口径 2026-10-11「搜索区域使用卡片」）：**只有一层卡**（不套第二层
          卡框 —— 条件与按钮直接排布在卡体里），标题「搜索条件」在卡头。
          **常驻一行** = 账号 + 关键词 + 状态 + 查询 / 重置 / 更多筛选 ⌄；点「更多筛选」**追加**
          其余条件（`Form layout="inline"` 自动换行），末位按钮变「收起 ^」。
          ⚠️ 折叠的只有**追加条件**，按钮始终可见 —— 收起时也要能按当前条件「查询 / 重置」。
          ⚠️ `marginTop: -5` 是刻意的：内容区自带 `padding-top: 15px`，两者相加才是
          「距顶部栏 10px」（用户同轮口径）。
          ⚠️ `flexShrink: 0`：整页是限定高度的三段式 flex，这一块固定不动。 */}
      <Card
        size="small"
        style={{ marginTop: -5, flexShrink: 0 }}
        title="搜索条件"
      >
        {accountsLoaded && accounts.length === 0 && (
          <Alert
            style={{ marginBottom: 12 }}
            type="warning"
            showIcon
            message="没有小红书账号可选"
            description="商品台账按账号隔离；请先添加账号并导入千帆登录态，才能同步商品。"
          />
        )}

      <Form<QueryForm>
        form={form}
        //: 用户口径 2026-10-11「条件和标题一行展示」⇒ `inline` 布局：label 在左、控件在右，一行排不下自动换行（不再手写 `Row/Col`）。
        layout="inline"
        initialValues={formInitialValues}
        // 每次改动即回写缓存（不等点「搜索」）：用户填了一半切走再回来，条件还在。
        // 「重置」走 `form.resetFields()` ⇒ 回到 `initialValues`（也就是**缓存里的值**），
        // 所以下面那一拍必须显式把**清空态**写回缓存（否则重置后重进页面又长回来了）。
        onValuesChange={() => writeFilterCache(FILTER_CACHE_KEYS.shopItems.form, formValuesToDraft(form.getFieldsValue()))}
        style={{ rowGap: 8 }}
      >
        {/* statusHint 是「状态 = 审核态」时的安静说明（§30.5，25–30 字一行），
            紧贴条件行之上。 */}
        {statusHint ? (
          <Text type="secondary" style={{ fontSize: 12, display: "block", width: "100%", marginBottom: 4 }}>
            {statusHint}
          </Text>
        ) : null}

        {/* ⚠️ 所有条件控件统一 `width: 180`（用户当轮口径：所有搜索条件输入框 180px）。`RangePicker` 也照此收窄，不复用整行宽度。 */}
        {/* 账号（每次查询都要看的一项，台账按账号隔离）：2026-10-05 从顶部卡搬进搜索区，2026-10-11 去卡片后并入同一行。 */}
        <Form.Item label="账号">
          <Select
            style={{ width: 180 }}
            placeholder={accountsLoaded ? "选择小红书账号" : "加载中…"}
            value={selectedAccountId ?? undefined}
            options={accountOptions}
            notFoundContent="没有小红书账号"
            onChange={(value) => {
              setSelectedAccountId(value);
              setPageNo(1);
            }}
          />
        </Form.Item>
        <Form.Item label="关键词" name="keyword">
          <Input
            allowClear
            style={{ width: 180 }}
            placeholder="商品名称 / ID / 货号，逗号隔开"
            onPressEnter={() => selectedAccountId && void load(selectedAccountId, 1, pageSize)}
          />
        </Form.Item>
        <Form.Item label="状态" name="status">
          <Select style={{ width: 180 }} options={STATUS_OPTIONS} />
        </Form.Item>

        {searchExpanded ? (
          <>
            {/* 区间类条件**合并成一个输入框分两段**（用户口径 2026-10-11「库存上限和库存下限类似的搜索条件应合并成一个输入框分两段」）—— 价格 / 库存 / 销量三组同形，总宽仍 180。
            ⚠️ 字段名**不变**（`price_from|price_to` / `stock_from|stock_to` / `sales_from|sales_to`）：拆成两段只改版式，不动请求契约（`formValuesToQuery` 照旧）。 */}
            <Form.Item label="库存区间">
              <Space.Compact style={{ width: 180 }}>
                <Form.Item name="stock_from" noStyle>
                  <InputNumber style={{ width: 80 }} min={0} placeholder="最低" />
                </Form.Item>
                <Text type="secondary" style={{ lineHeight: "30px", padding: "0 4px" }}>~</Text>
                <Form.Item name="stock_to" noStyle>
                  <InputNumber style={{ width: 80 }} min={0} placeholder="最高" />
                </Form.Item>
              </Space.Compact>
            </Form.Item>
            <Form.Item label="销量区间">
              <Space.Compact style={{ width: 180 }}>
                <Form.Item name="sales_from" noStyle>
                  <InputNumber style={{ width: 80 }} min={0} placeholder="最低" />
                </Form.Item>
                <Text type="secondary" style={{ lineHeight: "30px", padding: "0 4px" }}>~</Text>
                <Form.Item name="sales_to" noStyle>
                  <InputNumber style={{ width: 80 }} min={0} placeholder="最高" />
                </Form.Item>
              </Space.Compact>
            </Form.Item>
            {/*
              挂链笔记数（v1.34，用户口径「添加挂链笔记数，默认小于指定篇数」）：
              比较符默认「小于」+ 指定篇数。值**留空 = 不筛**（不是「小于 0」）——
              `0` 是有意义的阈值（「一篇都没挂」），缺省态不该偷偷筛掉数据。
              口径 = `note_goods` 关系表，与本列显示的篇数是**同一个数**（§33.8）。
            */}
            <Form.Item label="挂链笔记数">
              <Space.Compact style={{ width: 180 }}>
                <Form.Item name="note_count_op" noStyle>
                  <Select style={{ width: 72 }} options={NOTE_COUNT_OP_OPTIONS} />
                </Form.Item>
                <Form.Item name="note_count" noStyle>
                  <InputNumber style={{ width: 108 }} min={0} placeholder="篇数" />
                </Form.Item>
              </Space.Compact>
            </Form.Item>
          </>
        ) : null}

        {/* 价格区间 / 创建时间也在**常驻行**（用户口径 2026-10-11「搜索条件第一行再展示两个」）：这两个是商品列表最常用的筛选，折起来反而要多点一次「更多筛选」。 */}
        <Form.Item label="价格区间">
          <Space.Compact style={{ width: 180 }}>
            <Form.Item name="price_from" noStyle>
              <InputNumber style={{ width: 80 }} min={0} placeholder="最低" />
            </Form.Item>
            <Text type="secondary" style={{ lineHeight: "30px", padding: "0 4px" }}>~</Text>
            <Form.Item name="price_to" noStyle>
              <InputNumber style={{ width: 80 }} min={0} placeholder="最高" />
            </Form.Item>
          </Space.Compact>
        </Form.Item>
        <Form.Item label="创建时间" name="create_time_range">
          <RangePicker
            style={{ width: 180 }}
            placeholder={["开始日期", "截止日期"]}
            allowClear
          />
        </Form.Item>

        {/* 动作组（始终在末位）：查询 / 重置 / 更多筛选（展开态变「收起」）。 */}
        <Form.Item>
          <Space wrap>
            <Button
              type="primary"
              icon={<SearchOutlined />}
              loading={loading}
              onClick={() => runSearch(false)}
            >
              查询
            </Button>
            <Button onClick={() => runSearch(true)}>重置</Button>
            {/* 展开 / 收起是纯视图开关（不参与查询）。 */}
            <Button
              type="link"
              icon={searchExpanded ? <ArrowUpOutlined /> : <ArrowDownOutlined />}
              iconPosition="end"
              onClick={() => setSearchExpanded((prev) => !prev)}
            >
              {searchExpanded ? "收起" : "更多筛选"}
            </Button>
          </Space>
        </Form.Item>
      </Form>
      </Card>

      <Card
        size="small"
        //: 用户口径 2026-10-11「商品台账顶部按钮与底部分页和统计数据固定不动，
        //: 只有列表内的数据支持表格列滚动」：整页是**限定高度的纵向 flex** ——
        //:   ① 搜索条件（上一张卡）`flexShrink: 0` 内容固定；
        //:   ② 本台账卡 `flex: 1; minHeight: 0` 吃掉剩余高度，卡内再切成
        //:      「卡头（按钮）`flexShrink:0` / 表格体 `flex:1; overflow:auto` /
        //:       统计 `flexShrink:0`」三段；
        //:   ③ 分页由 `Table` 自带（`ant-table-pagination`，它是表格容器的**兄弟**、
        //:      贴在卡体底部）⇒ 天然不随表格体滚动。
        //: 高度 = 视口 - 顶栏(48) - 内容上下内边距(15+24) - 搜索条件卡(折叠态约 52)
        //:        - 两卡间距(10+12)，取 `calc(100vh - 190px)`（与图片空间同一套做法）。
        //: 用户口径 2026-10-11「搜索条件与商品台账间隔 10」「底部撑满」：
        //: `marginTop: 0`（间距由外层 `Flex gap={10}` 统一给，两处都给会翻倍）；
        //: `flex: 1` 吃掉剩余高度 ⇒ 卡片底边贴到视口底部。
        style={{ marginTop: 0, flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
        styles={{ body: { padding: 12, flex: 1, minHeight: 0, display: "flex", flexDirection: "column" } }}
        title="商品台账"
        extra={
          <Space size={8}>
            {/* 2026-10-06 用户口径：「同步指定商品按钮放商品台账顶部」。
                ⚠️ 与「同步商品」**并列在前**（同一族动作：都是把平台数据拉回本地台账，
                区别只是「整店」还是「按 ID 点名」）—— 散在两个卡里会让人以为它们无关。

                2026-10-06 二次口径：「选中商品后，点击同步指定商品按钮，后端立刻开始同步
                拉取指定的商品信息」⇒ 本按钮把**主表勾选的 ID** 交给弹窗，弹窗打开即开跑。
                ⚠️ 没勾选时**不打开弹窗**：空手进去只能看到一个空列表（用户的原话是
                「选中商品后」），先给一句话让他去勾 —— 比开个空弹窗有用。 */}
            <Tooltip
              title={
                normalizeDeleteIds(selectedItemIds).length > 0
                  ? `按 ID 同步选中的 ${normalizeDeleteIds(selectedItemIds).length} 个商品`
                  : "未勾选则同步整个店铺的商品"
              }
            >
              <Button
                icon={<SyncOutlined />}
                disabled={!selectedAccountId || goodsSyncRunning}
                onClick={() => {
                  //: 2026-10-06 合并（用户口径「将顶部的同步执行商品按钮功能合并到同步商品
                  //: 按钮中，该按钮文本优化为**批量同步商品信息**，注意合并后两个按钮的功能
                  //: 需全部保留」）：两个平级按钮合成一个入口 —— 弹窗里用 **Tab** 分成
                  //: 「按条件同步」（整店，后台任务）与「按 ID 同步商品信息」（前台等结果）。
                  if (!selectedAccountId || goodsSyncRunning) return;
                  //: 门禁与账号卡片页同一把尺子（`goodsGate`）；千帆确定不可用 ⇒ 提示补登。
                  const gate = goodsGate(
                    accounts.find((a) => a.id === selectedAccountId)?.login_channels,
                  );
                  if (gate.blocked) {
                    setArkPromptOpen(true);
                    return;
                  }
                  setSyncSingleTarget(null);
                  setSyncItemsOpen(true);
                }}
              >
                {goodsSyncRunning ? "商品同步中" : "批量同步商品信息"}
              </Button>
            </Tooltip>

            <Tooltip title="按当前筛选条件导出 CSV">
              <Button
                icon={<DownloadOutlined />}
                loading={exporting}
                disabled={!selectedAccountId}
                onClick={() => void exportCsv()}
              >
                导出 CSV
              </Button>
            </Tooltip>
            {/* 2026-10-08 用户口径：「移除新增商品按钮和逻辑，保留发布商品按钮功能」。
                ⚠️ **保留 `ProductDraftModal` 的 import 但不再挂入口** —— 草稿箱
                里的草稿仍要能发布（`ProductDraftsModal` → 详情 → 走发布链路），
                所以「新增」入口退场 ≠ 删组件。真正退场的只有顶部这个按钮
                与 `setDraftModalOpen` 的唯一调用点。 */}
            {/* 2026-10-08 用户口径：「商品台账新增功能按钮违规词」——
                弹窗全屏 50%（与发布弹窗的 75% 区分开），管理本地违规词库
                （新增 / 修改 / 停用 / 启用）。词库供数据包图片 OCR 精确过滤。 */}
            <Tooltip title="管理违规词库（图片 OCR 过滤）">
              <Button
                icon={<SafetyCertificateOutlined />}
                onClick={() => setForbiddenWordsOpen(true)}
              >
                违规词
              </Button>
            </Tooltip>
            {/* 2026-10-08 用户口径：「草稿箱弹窗的形式在商品页内展示，
                账号使用商品页选定的账号」—— 草稿箱与商品列表是同一件事的
                两个视图（草稿 = 还没上架的商品），故做成商品页里的弹窗，
                账号跟随本页顶部选定的店铺。 */}
            <Tooltip
              title={
                selectedAccountId
                  ? "查看本店铺的千帆草稿箱（数据实时来自平台，不落库）"
                  : "先选择店铺账号"
              }
            >
              <Button
                icon={<ContainerOutlined />}
                disabled={!selectedAccountId}
                onClick={() => setDraftsModalOpen(true)}
              >
                草稿箱
              </Button>
            </Tooltip>
            {/* 2026-10-08 用户口径：「发布商品到线上」+ 红线「发布成功第一时间下架」。
                ⚠️ 与「新增商品（存草稿）」是两个独立入口：这条会**真的上架**
                （后端发布成功后自动立即下架）。弹窗里常驻红线说明 + 二次确认。 */}
            <Tooltip
              title={
                selectedAccountId
                  ? "把商品提交到线上（成功后系统自动立即下架）"
                  : "先选择店铺账号"
              }
            >
              <Button
                danger
                icon={<SendOutlined />}
                disabled={!selectedAccountId}
                onClick={() => setPublishWizardOpen(true)}
              >
                发布商品
              </Button>
            </Tooltip>
            {/* 生成队列实时态（§31.10）：有待处理时一行小字，转圈即清（无 Tip，
                与另两张进度卡同口径）。队列空不占位。 */}
            {genQueueItemIds.size > 0 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                <FileAddOutlined spin /> 生成队列：{genQueueItemIds.size} 项待处理
                {genRound.failed > 0 || genRound.ok > 0
                  ? `（已生成 ${genRound.ok} · 失败 ${genRound.failed}）`
                  : ""}
              </Text>
            )}
            {/* ⚠️ 「批量生成智能笔记」**已移除**（2026-10-18 用户口径：
                *「暂时不支持批量生成，改成同步等待」*）。
                被移除的原按钮（保留备查，勿恢复）：
                  <Button type="primary" icon={<FileAddOutlined />}
                          disabled={selectedItemIds.length === 0}
                          loading={batchGenerating}
                          onClick={() => void batchGenerate()}>批量生成智能笔记（N）</Button>

                理由：生成改成**同步等待**后，单篇要点完等几十秒才出结果；
                20 篇串行必然超过任何 HTTP 超时。用户要的是「生成完就地改」，
                不是「批量铺开」。端点侧留了 410 墓碑（`LLM_BATCH_GENERATE_GONE`）。 */}
            {/* ⚠️ 「批量同步商品」按钮**已移除**（2026-10-05）：它与 header 的
                「同步商品」是同一件事（都触发整店全量同步），两个入口 = 两份说法。
                被移除的原按钮（保留备查，勿恢复）：
                  <Button icon={<SyncOutlined />} …>批量同步商品（N）</Button> */}
            {/* 2026-10-05 需求 7：**批量上架 / 批量下架 / 批量删除（平台商品）**。
                ⚠️ 三者走**手动触发式任务**（不是同步循环）：点完只建任务、立即返回，
                逐条结果在「商品操作结果」弹窗里轮询展示 —— 任务中心**任务列表不展示**
                （与「清理无流量笔记」同款）。 */}
            {([
              { key: "on_shelf", label: "批量上架", danger: false },
              { key: "off_shelf", label: "批量下架", danger: false },
              { key: "delete", label: "批量删除商品", danger: true },
            ] as const).map((entry) => (
              <Tooltip
                key={entry.key}
                title={
                  selectedItemIds.length > 0
                    ? `${entry.label}：对选中的 ${selectedItemIds.length} 个平台商品执行（≤${MAX_OPERATE_ITEMS} 个/次；不支持该操作的商品只记录不执行）`
                    : "先在表格里勾选商品"
                }
              >
                <Button
                  danger={entry.danger}
                  icon={entry.danger ? <DeleteOutlined /> : <SyncOutlined />}
                  disabled={selectedItemIds.length === 0}
                  loading={operatingAction === entry.key}
                  onClick={() => void handleBatchItemOperate(entry.key)}
                >
                  {entry.label}
                  {selectedItemIds.length > 0 ? `（${selectedItemIds.length}）` : ""}
                </Button>
              </Tooltip>
            ))}
            {/* 批量删除**本地台账行**（v1.36）：**只删本地台账行** —— 平台商品不动、关系行不动。
                ⚠️ 2026-10-05 改名「删除台账行」并把 Tooltip 改成显式对照：它与上面新增的
                「批量删除商品」（**删平台**）是两件完全不同的事，同名会让用户点错。
                两个连带后果写在确认框正文里（`batchDeleteHint`），按钮上的 Tooltip
                只说「删什么、上限多少」，不重复那两句（换成两处说同一件事）。 */}
            <Tooltip
              title={
                selectedItemIds.length > 0
                  ? `从本地台账删除选中的 ${selectedItemIds.length} 个商品行（≤${MAX_BATCH_DELETE} 个/次，平台商品不动）`
                  : "先在表格里勾选商品"
              }
            >
              <Button
                danger
                icon={<DeleteOutlined />}
                disabled={selectedItemIds.length === 0}
                loading={batchDeleting}
                onClick={handleBatchDelete}
              >
                删除台账行{selectedItemIds.length > 0 ? `（${selectedItemIds.length}）` : ""}
              </Button>
            </Tooltip>
          </Space>
        }
      >
        {/* ⚠️ LLM 生成进度卡**已移除**（2026-10-18 用户口径：「不展示进度表」）。
            原挂载点（保留备查，勿恢复）：
              <LlmGenerateProgressCard activeItemIds={llmQueueItemIds}
                                       onFinishedEntry={handleLlmFinished} />
            移除理由：它是「当日三数（成功/失败/待处理）+ 进度条」，而三数里的
            `running` / `queued` 契约是**对象 / 数组**（底座 `queued` 是列表），
            进度卡却按数字相加 ⇒ 待处理显示成 `[Object Object]`、进度条恒 0%。
            待处理态改由**操作列的旋转图标**表达（`llmQueueItemIds` 仍驱动它），
            失败原因在各自的操作列行内提示，不再单独占用一张卡片。 */}
        {data ? (
          //: `className="shop-items-table"` + `scroll={{ y: "100%" }}`：只有**表格内**
          //: 这一块滚动（用户口径「只有列表内的数据支持表格列滚动」）。
          //: ⚠️ `y: "100%"` 而不是固定像素 —— 表体 `max-height:100%` 恰好等于外层的
          //:    `flex:1` 高度，行多就在**表格体内部**滚、行少也不留空。
          <div className="shop-items-table" style={{ flex: 1, minHeight: 0 }}>
          <Table<XhsShopItem>
            rowKey="item_id"
            size="small"
            //: ⚠️ **不要 `loading={loading}`**（2026-10-20 用户实测「两层等待图标」的根因）：
            //: antd `Table` 的 loading 会自己渲染一层 `Spin` 覆盖（四点转圈），
            //: 与页面级 `<WaitMask>` 的白卡片叠在一起 ⇒ 看起来是「两个等待」。
            //: 等待表现统一交给 `<WaitMask>`，表格这里只负责把数据画出来。
            columns={columns}
            dataSource={data.items}
            //: ⚠️ 类名挂**外层 div**（蚂蚁表格的 `className` 落到最内层的 `.ant-table`，
            //: 拿它做 flex 布局够不到分页器与 `Spin` 包裹层）；`rootClassName` 才落在
            //: `.ant-table-wrapper`（分页器与表格体都在它里面）。
            rootClassName="shop-items-table"
            scroll={{ x: 1300, y: "100%" }}
            sortDirections={["descend", "ascend"]}
            rowSelection={{
              selectedRowKeys: selectedItemIds,
              onChange: (keys) => setSelectedItemIds(keys as string[]),
              // 跨页勾选要留得住（v1.36）：批量删除/生成是**跨页**操作，翻页就丢勾选
              // 会让用户以为「我勾了 30 个」而实际只剩当前页那几个。数量始终在
              // 按钮上可见（`批量删除（N）`），所以留住所选不会变成隐形状态。
              preserveSelectedRowKeys: true,
            }}
            onChange={(_pagination, _filters, sorter) => handleSortChange(sorter)}
            locale={{ emptyText: <Empty description="当前条件下没有商品" /> }}
            pagination={{
              current: pageNo,
              pageSize,
              total: data.total,
              showSizeChanger: true,
              pageSizeOptions: SHOP_ITEMS_PAGE_SIZES.map(String),
              //: **核心数据与分页同行**（用户口径 2026-10-11「只保留核心数据，展示一行，与分页同行」）：`showTotal` 渲染在分页行的**左侧**，与页码同一个 flex 行 ⇒ 天然「同行」；去掉原先那两行 `Descriptions`（它把分页推下去，且 7 项里大半与别处重复）。
              //:   「商品来源」恒为本地台账（常量）、「状态口径」= 每行的状态列、
              //:   「当前排序」= 表头箭头、同步时间 / 平台总数属账号级信息 —— 都撤了。
              showTotal: (total) =>
                `命中 ${total} 件商品${data.shop_name ? ` · ${data.shop_name}` : ""}`,
              onChange: (page, size) => {
                setPageNo(page);
                setPageSize(size);
                if (selectedAccountId) void load(selectedAccountId, page, size);
              },
            }}
          />
          </div>
        ) : (
          <Empty description={bindingLoaded ? "点「搜索」拉取商品列表" : "正在加载账号信息"} />
        )}
      </Card>

      {/* 商品详情弹窗（v1.32，设计 13 §30.3.6）：基本信息 + 图集 + 规格详情 */}
      <Modal
                centered
                open={detailOpen}
        title="商品详情"
        width="85%"
        //: 宽高都取视口 **85%**（用户口径 2026-10-20「太占用全屏的宽高85%」）。
        //: ⚠️ 只给 body 定高度不够 —— 标题栏 + footer 会再叠上去，整体超过 85vh。
        //: `styles.container` 才是 `.ant-modal-content`（含 header/body/footer 的那层），
        //: 在它上面做 flex 列；`style` 作用在更外层 `.ant-modal`，拿它定高会把
        //: 标题栏/按钮区一起顶出视口。`minHeight: 0` 是承重的 —— 少了它 flex 子项
        //: 不肯收缩，滚动会跑到弹窗外面。
        styles={{
          container: { height: "85vh", display: "flex", flexDirection: "column" },
          body: { flex: 1, minHeight: 0, overflowY: "auto" },
        }}
        footer={
          <Space>
            <Button onClick={() => setDetailOpen(false)}>关闭</Button>
            {/* 重抓单品入口（2026-10-09 用户口径「重抓单品入口」）：详情弹窗里就能
                重新拉这一件商品的最新数据，不必关弹窗再去「更多」里找。

                ⚠️ 走的是**同一条**同步链路（`POST /items/sync` + `SyncSpecifiedItemsModal`），
                只是 ID 清单缩成 `detail.item.item_id` 一行 —— DEF-044：一个语义一份实现，
                不为详情弹窗另写一套抓取。弹窗关掉详情再开同步弹窗（两弹窗不同时在线，
                避免蒙版叠蒙版）。同步完成后回到详情弹窗并**重新拉一次详情**（重抓的意义
                就是让眼前这份数据变新）。 */}
            <Button
              icon={<SyncOutlined />}
              disabled={!detail}
              onClick={() => {
                if (!detail) return;
                setDetailOpen(false);
                setSyncSingleTarget(detail.item.item_id);
                setSyncItemsOpen(true);
                //: 记下要重开的商品 —— 同步完成后回到它的详情（`syncReturnItem`）。
                setSyncReturnItem(detail.item);
              }}
            >
              重抓此商品
            </Button>
            <Button
              type="primary"
              disabled={!detail}
              onClick={() => {
                if (!detail) return;
                setDetailOpen(false);
                void openItemNotes(detail.item);
              }}
            >
              查看挂链笔记
            </Button>
          </Space>
        }
        onCancel={() => setDetailOpen(false)}
      >
        {detailLoading ? (
          <Spin />
        ) : detailError ? (
          <Alert type="error" showIcon message="商品详情加载失败" description={detailError} />
        ) : detail ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            {detail.platform_missing_at && (
              <Alert
                type="warning"
                showIcon
                message={`对账发现平台已没有这件商品（标记于 ${detail.platform_missing_at}）`}
                description="本地台账仍保留此行；平台重新上架后下一轮同步会自愈清除标记。"
              />
            )}
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="商品名称">{detail.item.title || "（无标题）"}</Descriptions.Item>
              <Descriptions.Item label="商品 ID">
                <Space size={4}>
                  {detail.item.item_id}
                  <CopyOutlined
                    style={{ fontSize: 11, cursor: "pointer" }}
                    onClick={() => void copyToClipboard(detail.item.item_id, "商品 ID 已复制", message)}
                  />
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="货号">{detail.item.article_no || "-"}</Descriptions.Item>
              <Descriptions.Item label="分类">{detail.item.category || "未分类"}</Descriptions.Item>
              <Descriptions.Item label="价格">{priceText(detail.item)}</Descriptions.Item>
              <Descriptions.Item label="库存">
                {detail.item.stock ?? "-"}
                {detail.item.sku_count != null ? `（${detail.item.sku_count} 个规格）` : ""}
              </Descriptions.Item>
              <Descriptions.Item label="销量">
                近 30 天 {detail.item.sales_30d ?? "-"} · 累计 {detail.item.sales_total ?? "-"}
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <Space size={4} wrap>
                  {detail.item.status_label || "状态 -"}
                  {detail.item.frozen && <Tag color="red">已冻结</Tag>}
                  {detail.item.buyable ? <Tag color="green">可购买</Tag> : <Tag>不可购买</Tag>}
                  {detail.item.stock_warning_sku_count > 0 && (
                    <Tag color="orange">{detail.item.stock_warning_sku_count} 个规格库存告警</Tag>
                  )}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">{detail.item.create_time || "-"}</Descriptions.Item>
              <Descriptions.Item label="上架时间">{detail.item.on_shelf_time || "-"}</Descriptions.Item>
              <Descriptions.Item label="更新时间">{detail.item.update_time || "-"}</Descriptions.Item>
              <Descriptions.Item label="挂链笔记">{detail.item.note_count} 篇</Descriptions.Item>
              <Descriptions.Item label="台账同步于">
                {detail.synced_at || "-"}
              </Descriptions.Item>
            </Descriptions>

            {detail.images.length > 0 && (
              <div>
                <Text strong style={{ fontSize: 13 }}>
                  商品图集（{detail.images.length} 张）
                </Text>
                <div style={{ marginTop: 6 }}>
                  <Image.PreviewGroup>
                    <Space size={8} wrap>
                      {detail.images.map((link) => (
                        <Image
                          key={link}
                          src={link}
                          width={84}
                          height={84}
                          style={{ objectFit: "cover", borderRadius: 4 }}
                        />
                      ))}
                    </Space>
                  </Image.PreviewGroup>
                </div>
              </div>
            )}

            {/* 生成商品详情图（设计 17 §13.3.3，v2.1；**v2.12 移到列表操作列独立弹窗**）
                —— 整段从详情弹窗移除（用户口径「不要在编辑或详情弹窗中实现」），
                入口改到列表「操作」列的按钮（`openDetailImage`）。
                原实现（内联区块 + 结果缩略图）保留在 git 历史与设计 17 §13.3.3 的
                被取代记录里，不在此处留半份。 */}

            <div>
              <Text strong style={{ fontSize: 13 }}>
                规格详情
              </Text>
              {detail.detail_synced_at ? (
                <Text type="secondary" style={{ fontSize: 11, marginInlineStart: 8 }}>
                  （补于 {detail.detail_synced_at}
                  {detail.sku_count != null ? ` · ${detail.sku_count} 个规格` : ""}）
                </Text>
              ) : null}
              {detail.skus.length > 0 ? (
                <Table
                  rowKey={(row) => row.sku_id || row.sku_name}
                  size="small"
                  style={{ marginTop: 6 }}
                  dataSource={detail.skus}
                  pagination={false}
                  scroll={{ y: 240 }}
                  columns={[
                    { title: "规格", dataIndex: "sku_name", render: (v: string) => v || "-" },
                    { title: "变体", dataIndex: "variant_name", render: (v: string) => v || "-" },
                    { title: "条形码", dataIndex: "barcode", render: (v: string) => v || "-" },
                    { title: "ERP 编码", dataIndex: "erp_code", render: (v: string) => v || "-" },
                    {
                      title: "价格",
                      dataIndex: "price_fen",
                      render: (v: number | null) => (v == null ? "-" : `¥${yuan(v)}`),
                    },
                    {
                      title: "原价",
                      dataIndex: "original_price_fen",
                      render: (v: number | null) => (v == null ? "-" : `¥${yuan(v)}`),
                    },
                    { title: "库存", dataIndex: "stock", render: (v: number | null) => skuCell(v) },
                    { title: "重量(g)", dataIndex: "weight", render: (v: number | null) => skuCell(v) },
                    {
                      title: "可售",
                      key: "buyable",
                      render: (_v, row) =>
                        row.buyable === null ? (
                          "-"
                        ) : (
                          <Tag color={row.buyable ? "green" : "default"}>
                            {row.buyable ? "可售" : "停售"}
                          </Tag>
                        ),
                    },
                  ]}
                />
              ) : (
                <Empty
                  style={{ marginTop: 6 }}
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="规格详情还没补齐（同步商品任务会自动补足）"
                />
              )}
            </div>
          </Space>
        ) : null}
      </Modal>

      {/* ⚠️ **独立「生成记录」弹窗已移除**（2026-10-05 用户口径：「将商品操作列的
          生成记录合并到AI商品图弹窗内，使用tab布局」）—— 它的内容搬进了
          `AI商品图` 弹窗的第二个顶层 Tab（组件 `GenerationsPanel`，定义在本文件上方）。
          被移除的原弹窗（保留备查，勿恢复）：`Modal open={generationsOpen}` 那个。 */}

      {/* 「生成商品详情图」独立弹窗（设计 17 §13.3.3，v2.1；v2.12 从详情弹窗迁出；
          v2.16 重排为**左右两栏**）。
          用户口径（2026-10-02）：「商品的 AI 生成详情图主图功能在商品列表操作列使用
          单独弹窗实现，不要在编辑或详情弹窗中实现」。
          弹窗自带 item 上下文（`detailImageItem`）—— 打开即重置三态，跨商品不串结果。
          商品无图时**不拦按钮**（服务端会拿到 item_id 后按台账取图，
          取不到给 `PRODUCT_IMAGE_MISSING`）—— 如实报缺，不造图。

          ⚠️ **v2.16 重排理由**（用户口径「重新布局一下」）：原版 560 宽、从上到下一路堆叠
          （商品信息 → 参考图两组 → 提示词 → 结果）⇒ 参考图缩略图挤成窄条、出图结果被
          压在弹窗最底部（要滚动才看得到）。改为**左右两栏**：
          左栏 = 配置（商品 + 参考图 + 提示词），右栏 = 出图结果（生成后即时可见，不滚）。
          宽度 **`60vw`**（全屏 60%，用户口径）—— 60vw 在宽屏上会很高，
          故同时给内容区 `maxHeight` + 滚动，弹窗本体不超出视口。
          `Row/Col` 而非 `flex`：与全站栅格口径一致（窄视口自动折行单列）。 */}
      <Modal
        // 2026-10-05 修正：`centered` 与 `open` 的**书写顺序**与语义无关
        //  （antd 解构 props，不看顺序）。原先 `centered` 排在 `open` 之后，
        //  只是让「所有弹窗都居中」这条规则在源码上少一处显式锚点 —— 统一提到 `open` 之前。
        centered
        open={detailImageOpen}
        // v2.28：生成中**禁止关闭**（用户口径「弹窗加蒙版并统计耗时，防止被关闭」）。
        // ⚠️ 出图是逐张串行的真上游调用（实测单张 29–96 秒）—— 关掉弹窗不会取消后端
        //    请求，只是让用户看不到结果、白等一场（还可能重复点一次、白烧额度）。
        //    `closable={false}` 去掉右上角 ×；`maskClosable={false}` 挡住点蒙版关；
        //    footer 的「关闭」按钮在生成中禁用 —— **三个口子全堵**。
        closable={!detailImageGenerating}
        maskClosable={!detailImageGenerating}
        title={detailImageTitle}
        data-generating={detailImageGenerating ? "1" : "0"}
        // v2.28.2：**垂直居中**（用户口径「生成AI商品图没有居中，你调整一下」；
        // 2026-10-05 再次确认「全站弹窗一律居中」）。
        // ⚠️ antd 的 Modal **默认是顶部对齐**（`top: 100px`），加 `centered` 才垂直居中
        // （官方文档：centered 设为 true 时使用 flex 布局使弹窗垂直居中）。
        width="75vw"
        onCancel={() => {
          if (detailImageGenerating) return; // 兜底：键盘 Esc 也走这里
          setDetailImageOpen(false);
        }}
        footer={
          <Space>
            <Button onClick={() => setDetailImageOpen(false)} disabled={detailImageGenerating}>关闭</Button>
            {/* v2.19：**必须勾选原始参考图**才能生成（用户口径「必须选择原始参考图才可以
                点击生成按钮」）。禁用态给出原因（Tooltip），不让用户对着灰按钮猜。
                ⚠️ 只按**当前 Tab** 的勾选判断 —— 各 Tab 独立配置，按钮语义也是各自的。 */}
            <Tooltip
              title={
                activeGroupRefs.length === 0 ? "请先在「可选参考图」里勾选至少一张图" : ""
              }
            >
              <Button
                type="primary"
                loading={detailImageGenerating && !detailImageRegenerating}
                disabled={activeGroupRefs.length === 0 || detailImageLoading}
                onClick={() => void generateDetailImage()}
              >
                生成{activeGenerateLabel}
              </Button>
            </Tooltip>
          </Space>
        }
      >
        {/* ⚠️ 蒙版是 `position: absolute; inset: 0` ⇒ **必须有定位父级**，
            否则会以最近的 positioned 祖先（可能是页面）为基准铺满全屏。
            这里包一层 `position: relative` 的容器把蒙版**约束在弹窗内容区**。 */}
        <div style={{ position: "relative" }}>
        {/* 2026-10-05（需求 1）：**顶层 Tabs**——「生成配置」+「生成记录」。
            ⚠️ 生成记录**打开该 Tab 时才拉**（`openGenerations` 会重置状态并发请求），
            避免每次开弹窗都多发一次请求（多数用户进来是为了生成）。 */}
        <Tabs
          activeKey={detailImageTab}
          onChange={(key) => {
            const next = key as "config" | "generations";
            setDetailImageTab(next);
            if (next === "generations" && detailImageItem) {
              void openGenerations(detailImageItem);
            }
          }}
          items={[
            {
              key: "config",
              label: "生成配置",
              children: (
                <div>
        {/* v2.29：**整弹窗蒙版**（用户口径「改成弹窗整体加蒙版」）。
            ⚠️ v2.28 是一条约 30px 高的**提示条**（不挡任何东西）——
            用户要的是「整个弹窗被盖住」的视觉阻断。现在用绝对定位铺满内容区，
            半透明遮罩 + 居中的转圈与耗时。
            ⚠️ **`pointerEvents: "none"` 保留**：遮罩自己不拦点击，门禁仍由
            各控件的 `disabled={detailImageGenerating}` 承担（两处都拦 = 一份判据
            两个实现，DEF-044）；且**不盖 Modal 的关闭按钮区**（那在 header，
            内容区之外）—— 关闭与否由 `closable` / `onCancel` 决定，与遮罩无关。 */}
        {/* v2.30（2026-10-20 用户口径「第二个红框还有一个等待蒙版转圈」）：
            原来这里是**自建 div + antd `<Spin size="large" />`** 的独立蒙版 ——
            全站统一蒙版后它成了漏网的一个（页面里同时存在两套转圈：`WaitMask` 圆环
            + 这里 antd 的圈）⇒ 改用 `<WaitMask scope="panel">`，与其它等待一致。
            `sub` 承载原来那几条灰字说明（串行耗时 / 不可关闭），文案不丢。
            ⚠️ `scope="panel"` 要求父元素 `position: relative`（Modal body 已满足）。 */}
        <WaitMask
          open={detailImageGenerating}
          mask
          scope="panel"
          title="正在出图"
          elapsed={detailImageElapsed}
          sub="逐张串行调用上游，单张约 30–100 秒；生成期间弹窗不可关闭（关闭会看不到结果）"
        />
        {/* 详情图数据加载中的提示（原来由 Tab 内 `Spin spinning` 画，已统一改到这里）。 */}
        <WaitMask
          open={detailImageLoading}
          scope="panel"
          title="正在加载详情图数据"
        />
        <Descriptions size="small" column={2} style={{ marginBottom: 12 }}>
          <Descriptions.Item label="商品">
            {detailImageItem?.title || detailImageItem?.item_id || "-"}
          </Descriptions.Item>
          <Descriptions.Item label="商品ID">
            {detailImageItem?.item_id || "-"}
          </Descriptions.Item>
        </Descriptions>

        {/* v2.22（§13.3.7.9）：**主图 / 详情图完全拆分为两个 Tab**（用户口径
            「将生成主图和详情图完全拆分开，使用 tab，各自的提示词模板、补充要求、
            生成张数」）。

            ⚠️ 每个 Tab 是**一套完整且独立的配置**：参考图勾选 / 提示词 / 张数 / 结果。
            切 Tab 不丢各自的状态（`groupConfigs` 按组存）—— 若共用一个 state，
            用户在 A 填的提示词会在切到 B 后被顶掉（「混搭」在状态层的形态）。 */}
        <Tabs
          activeKey={activeGroup}
          onChange={(key) => {
            const next = key as "product" | "detail";
            if (next === activeGroup) return;
            setActiveGroup(next);
            /* ⚠️ **切换 Tab 后的复位口径**（用户口径 2026-10-03，两轮澄清后定稿）：
               > 「切换tab时要清除历史tab中的内容」
               > 「每次切换tab后，**参考图、补充要求清空**，**其它恢复默认值**」

               | 项 | 处理 |
               |---|---|
               | 参考图勾选 | **清空**（用户明确） |
               | 补充要求（手输提示词） | **清空**（用户明确） |
               | 提示词模板 | **回该 Tab 的默认模板**（`is_default`，与首次打开一致） |
               | 生成张数 | **回默认 3** |
               | 生成结果 / 勾选待保存 / 失败信息 | **清空**（上一个 Tab 的结果不该留在这里） |

               ⚠️ 这**取代**了 v2.22 的「每组状态按组存、切 Tab 不丢」——
               那时担心「在 A 填的提示词切到 B 后被顶掉」；但 v2.23 把两个勾选区
               合并成一个之后，「留着上一个 Tab 的勾选与结果」反而误导：
               用户以为「这是在本 Tab 配的」，其实是从另一个 Tab 带过来的。
               ⇒ 现在「换 Tab = 换一套全新配置」。 */
            const defaultTemplateId = (taskType: string) =>
              detailImageTemplates.find((t) => t.is_default && t.task_type === taskType)?.id;
            setDetailImageRefs([]);
            setDetailImageGroupConfigs({
              // 「恢复默认值」= 模板回默认、张数回 3 —— 与首次打开弹窗时的预填一致
              [next]: {
                templateId: defaultTemplateId(next === "product" ? "cover" : "image"),
                prompt: "",
                count: 3,
              },
            });
            setDetailImage(null);
            setDetailImageSelectedIds([]);
            setDetailImageRegenerating(null);
          }}
          items={detailImageGroupTabs.map((tab) => ({
            key: tab.key,
            label: (
              <span>
                {tab.label}
              </span>
            ),
            children: (
              //: ⚠️ 2026-10-20 统一口径：转圈不再由 antd Spin 画（会叠加/压字），
              //: 交给 `<WaitMask>` 画居中提示（与全站一致）。Spin 只留结构。
              <Spin spinning={false} indicator={<></>}>
                <Space direction="vertical" size={12} style={{ width: "100%" }}>
                  {/* ① 可选参考图（v2.23 用户口径：「把原始主图、原始详情图**放一组内**，
                      该组为**可选参考图**，只有用户选了就一并扔给大模型，**最多选 6 张**」）——
                      ⇒ **合并成一个组**（不再分两个小卡片），组内顺序 = 主图 → 详情图，
                      上限 6 张（前后端同一上限）。选中哪些就一并送模型。 */}
                  <ReferenceImagePicker
                    label="可选参考图"
                    urls={[
                      ...(detailImageDetail?.product_images || []),
                      ...(detailImageDetail?.detail_images || []),
                    ]}
                    selected={detailImageRefs}
                    maxSelectable={6}
                    onToggle={(url) =>
                      setDetailImageRefs((current) =>
                        current.includes(url)
                          ? current.filter((u) => u !== url)
                          : current.length >= 6
                            ? current // 已达上限 ⇒ 不再追加（按钮已置灰，这里兜底）
                            : [...current, url]
                      )
                    }
                    onClearGroup={() => setDetailImageRefs([])}
                    emptyHint="这个商品还没有图片（先执行「同步商品」补齐图集）"
                  />
                  {/* ② 本类预览（v2.23 用户口径：「可选参考图**新增一块**展示该 Tab
                      对应的原始主图/详情图，**只是给用户预览**」）——
                      ⇒ 只展示、**不可勾选**（不是第二个选择区）：让用户看清「本 Tab
                      生成的是哪一类图」以及「这一类原始图长什么样」，避免与实际送出的
                      参考图混淆（可勾选的那组在上面）。 */}
                  <div
                    style={{
                      border: "1px dashed rgba(0,0,0,0.14)",
                      borderRadius: 6,
                      padding: "8px 10px",
                    }}
                  >
                    {/* 定稿（用户口径 2026-10-03，三轮澄清）：
                        ① 标题 + 说明 **同一行**（「本类原始图预览  原始主图 · 共 10 张 ·
                           仅供预览，不参与勾选」）—— 用户原话「文本放一行内展示」；
                        ② 图片 **另起一行**铺开（`Space wrap` 按宽度自己折行）。
                        ⇒ 用横向 `Space` 装两个 `Text`（`align="baseline"` 让字号不同的
                        两段文字基线对齐，不靠 `lineHeight` 硬凑）。 */}
                    {/* ⚠️ **必须 `display: block`**：antd 的 `Space` 默认是
                        `display: inline-flex` ⇒ 它与下面那个装图片的 `Space`
                        **会并排在同一行**（用户实测报「图片还是和说明文案在一行中」）。
                        文字行是块级独占一行，图片行自然落到下一行。 */}
                    <Space
                      size={8}
                      align="baseline"
                      style={{ display: "flex", marginBottom: 6 }}
                    >
                      <Text strong style={{ fontSize: 12 }}>
                        本类原始图预览
                      </Text>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {tab.urls.length === 0
                          ? `${tab.label} · 共 0 张 · ${tab.emptyHint}`
                          : `${tab.label} · 共 ${tab.urls.length} 张 · 仅供预览，不参与勾选`}
                      </Text>
                    </Space>
                    {tab.urls.length > 0 && (
                      <Space size={6} wrap>
                        {tab.urls.map((url) => (
                          <Image
                            key={url}
                            src={url}
                            width={64}
                            height={64}
                            style={{
                              objectFit: "cover",
                              borderRadius: 2,
                              border: "1px solid rgba(0,0,0,0.12)",
                            }}
                          />
                        ))}
                      </Space>
                    )}
                  </div>

                  {/* ③④ **提示词模板 + 生成张数同一行**（用户口径 2026-10-03：
                      「生成张数与提示词模板下拉放一行中展示」）。
                      ⚠️ 模板是长下拉、张数是窄数字框 ⇒ **不要**用两个 `flex: 1`
                      平分（张数框会被拉得很宽、数字孤零零居中）。模板 `flex: 1`
                      吃掉剩余宽度，张数固定 ~110px。两者 `align="flex-end"`
                      让输入框底边对齐（标签字号不同、高度不同）。 */}
                  <div>
                    <Space size={12} align="end" style={{ width: "100%" }}>
                      {/* 用户口径 2026-10-03：「提示词模板 **一行的 70% 宽**」
                          ⇒ `flex: "0 0 70%"`（不伸缩、固定占 70%），右侧张数靠左排。 */}
                      <div style={{ flex: "0 0 70%", minWidth: 0 }}>
                        <Text strong style={{ fontSize: 13 }}>
                          提示词模板
                        </Text>
                        <Select
                      style={{ width: "100%", marginTop: 4 }}
                      allowClear
                      placeholder="选择公共提示词模板（留空则用手写提示词）"
                      value={detailImageGroupConfigs[tab.key]?.templateId}
                      onChange={(value) =>
                        setDetailImageGroupConfigs((current) => ({
                          ...current,
                          [tab.key]: { ...current[tab.key], templateId: value ?? undefined },
                        }))
                      }
                      disabled={detailImageGenerating}
                      options={detailImageTemplates
                        .filter((t) => t.task_type === (tab.key === "product" ? "cover" : "image"))
                        .map((t) => ({
                          value: t.id,
                          label: t.is_default ? `${t.name}（默认）` : t.name,
                        }))}
                      notFoundContent="还没有模板，可在「笔记工坊 → 提示词模板」新建"
                        />
                      </div>

                      {/* ④ 生成张数（本组独立）—— 与模板同行，见上方注释 */}
                      <div>
                        <Text strong style={{ fontSize: 13 }}>
                          生成张数
                        </Text>
                        <div style={{ marginTop: 4 }}>
                          <InputNumber
                            // 下限 1（用户口径 2026-10-03「生成张数调整最低一张」）；
                            // 默认仍 3 —— 与后端 MIN/DEFAULT 保持一致（改一边就是漂移）。
                            min={1}
                            max={10}
                            value={detailImageGroupConfigs[tab.key]?.count ?? 3}
                            onChange={(value) => {
                              const next = Math.max(1, Math.min(10, Number(value) || 3));
                              setDetailImageGroupConfigs((current) => ({
                                ...current,
                                [tab.key]: { ...current[tab.key], count: next },
                              }));
                            }}
                            disabled={detailImageGenerating}
                            style={{ width: 110 }}
                            addonAfter="张"
                          />
                        </div>
                      </div>
                    </Space>
                  </div>

                  {/* ③b 提示词 / 补充要求（本组独立） */}
                  <div>
                    <Text strong style={{ fontSize: 13 }}>
                      {detailImageGroupConfigs[tab.key]?.templateId ? "补充要求（可留空）" : "提示词 / 补充要求"}
                    </Text>
                    <Input.TextArea
                      style={{ marginTop: 4 }}
                      value={detailImageGroupConfigs[tab.key]?.prompt ?? ""}
                      onChange={(e) =>
                        setDetailImageGroupConfigs((current) => ({
                          ...current,
                          [tab.key]: { ...current[tab.key], prompt: e.target.value },
                        }))
                      }
                      placeholder={
                        tab.key === "product"
                          ? "主图口径：干净背景、突出商品本体（留空则按商品信息自动生成）"
                          : "详情图口径：卖点拆解、细节特写、场景化（留空则按商品信息自动生成）"
                      }
                      disabled={detailImageGenerating}
                      maxLength={2000}
                      rows={3}
                    />
                  </div>


                  {/* ⑤ 本组结果 */}
                  <DetailImageGroupResult
                    groupKey={tab.key}
                    groups={detailImage?.groups || []}
                    regenerating={detailImageRegenerating}
                    generating={detailImageGenerating}
                    selectedIds={detailImageSelectedIds}
                    onToggleSelect={(id) =>
                      setDetailImageSelectedIds((current) =>
                        current.includes(id)
                          ? current.filter((x) => x !== id)
                          : [...current, id]
                      )
                    }
                    onRegenerate={(index) =>
                      void generateDetailImage({ group: tab.key, index })
                    }
                  />

        {/* v2.29：**保存到素材库**（弹窗级一条，不属某一组 —— 用户口径
            「支持选择性保存…支持全部保存」，两种语义共用这一个入口）。
            ⚠️ 素材库下拉在这里（而不是每组各一个）：保存目标是**本次动作**的属性，
            不是某一组图的属性 —— 按组放会让人以为「两组能存到不同地方」。 */}
        {detailImage && (detailImage.groups || []).some((g) => (g.assets || []).length > 0) && (
          <div
            style={{
              marginTop: 10,
              paddingTop: 10,
              borderTop: "1px solid rgba(0,0,0,0.08)",
              display: "flex",
              alignItems: "center",
              gap: 10,
              flexWrap: "wrap",
            }}
          >
            <Text strong style={{ fontSize: 13 }}>
              保存到素材库
            </Text>
            <Select
              style={{ width: 220 }}
              size="small"
              placeholder="选择素材库文件夹"
              value={detailImageMaterialFolderId ?? materialFolders[0]?.id}
              onChange={(value) => setDetailImageMaterialFolderId(value ?? null)}
              options={materialFolders.map((f) => ({
                value: f.id,
                label: `${f.name}（${f.path}）`,
              }))}
              notFoundContent="还没有素材库文件夹，请先到「笔记工坊 → 素材库」新建"
            />
            <Button
              size="small"
              loading={detailImageSaving}
              disabled={detailImageSelectedIds.length === 0}
              onClick={() => void saveDetailImagesToMaterial("selected")}
            >
              保存勾选（{detailImageSelectedIds.length}）
            </Button>
            <Button
              size="small"
              type="primary"
              loading={detailImageSaving}
              onClick={() => void saveDetailImagesToMaterial("all")}
            >
              全部保存
            </Button>
            <Text type="secondary" style={{ fontSize: 11 }}>
              每次保存会新建一个子目录（ai-生成/时间戳），组名进文件名
            </Text>
          </div>
        )}
                  <DetailImageGroupErrors
                    groupKey={tab.key}
                    errors={detailImage?.errors || []}
                    label={tab.label}
                  />
                </Space>
              </Spin>
            ),
          }))}
        />
                </div>
              ),
            },
            {
              key: "generations",
              label: "生成记录",
              children: (
                <GenerationsPanel
                  item={detailImageItem}
                  loading={generationsLoading}
                  rows={generations}
                  total={generationsTotal}
                />
              ),
            },
          ]}
        />
              </div>
</Modal>

      {/* LLM 生成配置弹窗（T9，设计 17 §7.4）：选模板 + 媒体来源四选一。
          与「生成商品详情图」同型：弹窗自带商品上下文，打开即重置（跨商品不串）。 */}
      <LlmGenerateModal
        open={llmGenerateOpen}
        accountId={selectedAccountId}
        itemId={llmGenerateItem?.item_id || ""}
        itemTitle={llmGenerateItem?.title || ""}
        productImages={llmGenerateItem?.images || []}
        onClose={() => setLlmGenerateOpen(false)}
        onQueued={handleLlmQueued}
      />

      {/* ⚠️ 「绑定千帆商家后台」弹窗已整块移除（ADR-021，2026-09-30）：
          `POST /xhs/qianfan/bind` 退场（后端 410 `QIANFAN_BINDING_REMOVED`），
          千帆能力改由「该账号自己的 ark 登录态」决定 —— 入口在账号矩阵。
          被取代的原弹窗（保留备查）：
            <Modal centered open={bindOpen} title="绑定千帆商家后台（商品列表来源）" …>
              … 粘贴 Cookie（须含 access-token-ark.xiaohongshu.com）…
            </Modal> */}

      {/*
        商品 → 挂链笔记弹窗（设计 13 §31.6）：**关系表源**（note_goods join notes），
        零平台请求。多篇笔记用 tab 自然隔离；内容区复用内容库详情弹窗的同一份
        两栏版式（NoteDetailBody）—— 同一语义不留两份实现。
      */}
      <Modal
                open={notesOpen}
        centered
        // 尺寸（v1.36 用户口径「挂链笔记弹窗全屏 75%」）：宽高都取视口的 75%。
        // 原来的 80vw 是「尽量大」，与商品详情弹窗（85%）几乎同宽 —— 两个弹窗
        // 看起来一样大、层级感反而没了；收到 75% 后它是「一个更大的详情浮层」，
        // 与列表的对比更清楚。**不再设 maxWidth**：设了它就不是 75% 了
        // （宽屏上 75vw > 1600 会被截回，口径与实际不符）。
        width="75vw"
        // `overflow: "auto"` 而不是 `"hidden"`：75vw 下两栏装不下的视口区间只剩
        // ~900–918px（75vw−48 要 ≥ 300+320+20），与 `.note-detail-body` 那个
        // 900px 媒体查询几乎首尾相接 —— 留 `auto` 只是把那 18px 的缝补上：
        // 装得下时子列自己滚（`overflow-y: auto`），body 永不溢出，表现与
        // `hidden` 一致；装不下时它正好接住，不会把右栏裁掉。
        styles={{ body: { height: "75vh", overflow: "auto", paddingRight: 12 } }}
        title={notesItem ? `「${notesItem.title}」的挂链笔记` : "挂链笔记"}
        footer={null}
        onCancel={() => setNotesOpen(false)}
        destroyOnClose
      >
        <div>
          {/* 2026-09-23 上机口径：顶部那条「本地已映射 1937/1960 条商品笔记 ——
              反查走笔记↔商品关系表…」说明**整块移除**（弹窗里只要笔记本体）。
              覆盖率与口径解释留在列表页「挂链笔记」列的 Tooltip 里，不在这里重复。 */}
          {/*
            「在内容库中查看」（设计 13 §25.8）：这个弹窗证明「该商品有 N 篇
            挂链笔记」，但内容库 1964 条按时间倒排、老笔记排在第几百位，
            关键词也未必对得上（用户两次报告「内容库里少一篇」都是这个原因）。
            跳转带 `?item_id=` 直达内容库的挂链商品筛选视图 —— 从商品到
            「该商品的笔记」不再靠碰运气。先关弹窗再跳（路由切换不会自动关它）。
          */}
          {!notesLoading && notesData && notesData.notes.length > 0 && notesItem && (
            <div style={{ marginBottom: 12, textAlign: "right" }}>
              <Button
                size="small"
                icon={<DatabaseOutlined />}
                onClick={() => {
                  setNotesOpen(false);
                  navigate(`/platforms/xhs/library?item_id=${encodeURIComponent(notesItem.item_id)}`);
                }}
              >
                在内容库中查看（{notesData.count} 篇）
              </Button>
            </div>
          )}
          {notesLoading ? (
            <div style={{ padding: "48px 0", textAlign: "center" }}>
              <Spin />
            </div>
          ) : notesData && notesData.notes.length ? (
            notesData.notes.length === 1 ? (
              <NoteDetailBody
                mediaUrls={notesData.notes[0].images}
                videoUrl={notesData.notes[0].video_url}
                isVideo={Boolean(notesData.notes[0].video_url)}
                mediaNamePrefix={notesData.notes[0].note_id}
              >
                {renderItemNoteInfo(notesData.notes[0])}
              </NoteDetailBody>
            ) : (
              <Tabs
                activeKey={notesTab}
                onChange={setNotesTab}
                items={notesData.notes.map((note, index) => ({
                  key: String(index),
                  label: (
                    <Tooltip title={note.title || "（无标题）"}>
                      <span style={{ maxWidth: 180, display: "inline-block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {note.title || "（无标题）"}
                      </span>
                    </Tooltip>
                  ),
                  children: (
                    <NoteDetailBody
                      mediaUrls={note.images}
                      videoUrl={note.video_url}
                      isVideo={Boolean(note.video_url)}
                      mediaNamePrefix={note.note_id}
                    >
                      {renderItemNoteInfo(note)}
                    </NoteDetailBody>
                  ),
                }))}
              />
            )
          ) : (
            <Empty description="这个商品还没有挂链笔记（可能未同步，也可能确实没挂）" />
          )}
        </div>
      </Modal>
      {/* M4（设计 20 §5）：商品编辑弹窗 —— 操作列独立入口，不塞进详情弹窗。
          落库成功（分组/采纳标题）后刷新列表，让「分组」列与新标题即时可见。 */}
      <ProductEditModal
        open={editingItem !== null}
        platformAccountId={selectedAccountId ?? 0}
        item={editingItem}
        onClose={() => setEditingItem(null)}
        onSaved={() => {
          if (selectedAccountId) void load(selectedAccountId, pageNo, pageSize);
        }}
      />
      {/* 商品操作结果弹窗（2026-10-05 需求 7）：任务逐条结果，5s 轮询到全终态。
          ⚠️ 2026-10-06 用户口径：「结果等待弹窗要加**蒙版**统计**耗时**，**避免结果
          弹窗关闭**，执行结果返回后再支持弹窗关闭」—— 修正了此前的折中（那时运行中
          也能关，用户一点就把结果丢了）：

          * `maskClosable={false}` — 点蒙版不关（误触最多的关闭路径）；
          * `closable={!running}` — 运行中连右上角 × 都不给；
          * `onCancel` 走 `closeOperateResult()` — 它自己会判非终态并拒绝；
          * `mask` 常显 — 结果没出来之前，下面那张列表本来也不该被操作。 */}
      <Modal
        centered
        open={operateTask !== null}
        maskClosable={false}
        closable={!operateTaskRunning()}
        title={
          operateTask
            ? `${operateTask.action_label}结果 · ${
                operateTask.status === "pending" || operateTask.status === "running"
                  ? "执行中…"
                  : `成功 ${operateTask.succeeded} / 未执行 ${operateTask.skipped} / 失败 ${operateTask.failed}`
              }`
            : "商品操作结果"
        }
        footer={
          <Button
            //: 执行中**不给点**（用户口径「执行结果返回后再支持弹窗关闭」）——
            //: 判据与 `closeOperateResult` 同一份，按钮只是把它显性化。
            disabled={operateTaskRunning()}
            onClick={closeOperateResult}
          >
            {operateTaskRunning() ? "执行中…" : "关闭"}
          </Button>
        }
        width="60vw"
        onCancel={closeOperateResult}
        styles={{ body: { maxHeight: "60vh", overflow: "auto" } }}
      >
        {operateTask ? (
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            <Space size={8} wrap>
              <Text type="secondary" style={{ fontSize: 12 }}>
                共 {operateTask.total} 件 · 已发请求 {operateTask.executed} 件
                {operateTask.status === "pending" || operateTask.status === "running"
                  ? "（后台逐条执行中，每 5 秒刷新）"
                  : ""}
              </Text>
              {/* 耗时统计（2026-10-06 用户口径「结果等待弹窗要加蒙版统计耗时」）：
                  前端自计时（秒表每 1s 一跳，终态停）—— 后端只给任务进度，
                  不假装它有耗时字段；终态后固定为**总耗时**。 */}
              <Tag color={operateTaskRunning() ? "processing" : "default"}>
                耗时 {operateElapsedSec}s
              </Tag>
            </Space>
            <Table<ShopItemOperateEntry>
              rowKey="item_id"
              size="small"
              pagination={false}
              dataSource={operateTask.items}
              locale={{ emptyText: <Empty description="还没有逐条结果" /> }}
              columns={[
                { title: "商品", dataIndex: "title", render: (v: string, row) => v || row.item_id },
                { title: "商品 ID", dataIndex: "item_id", width: 220 },
                {
                  title: "结果",
                  dataIndex: "result",
                  width: 90,
                  render: (v: string) =>
                    v === "ok" ? (
                      <Tag color="green">已执行</Tag>
                    ) : v === "skipped" ? (
                      <Tag color="orange">未执行</Tag>
                    ) : (
                      <Tag color="red">失败</Tag>
                    ),
                },
                { title: "说明", dataIndex: "reason", render: (v: string) => v || "-" },
              ]}
            />
          </Space>
        ) : null}
      </Modal>

      {/* 同步指定商品弹窗（2026-10-05 需求 6）：无限列表展示「要同步的商品 + 同步状态」。
          ⚠️ 平台没有按 id 拉商品的读接口 ⇒ 后端要**整店翻页扫描**才能确认命中 / 未命中。
          弹窗里**如实标注这个代价**（用户会等很久，不说清就是骗他）。 */}
      <SyncSpecifiedItemsModal
        open={syncItemsOpen}
        platformAccountId={selectedAccountId}
        //: 主表勾选的商品 ID（弹窗里**以列表展示**，勾选后点同步才跑 —— 2026-10-06 v2 口径）。
        //: ⚠️ 行内「同步商品信息」也走这个弹窗，它通过 `syncSingleTarget` 注入单个 ID。
        //: ⚠️ **必须带名称 / 缩略图**：此前只传 ID ⇒ 弹窗「商品信息」列的名称与图全空
        //: （2026-10-06 用户实测报的 bug）。行内单件用 `syncSingleTarget` 那一行。
        items={
          syncSingleTarget
            ? (data?.items ?? [])
                .filter((row) => row.item_id === syncSingleTarget)
                .map((row) => ({
                  item_id: row.item_id,
                  title: row.title || "",
                  image: row.image || "",
                }))
            : (data?.items ?? [])
                .filter((row) => selectedItemIds.includes(row.item_id))
                .map((row) => ({
                  item_id: row.item_id,
                  title: row.title || "",
                  image: row.image || "",
                }))
        }
        onClose={() => {
          setSyncItemsOpen(false);
          //: ⚠️ 必须清：否则下一次「批量同步商品信息」进来时 `syncSingleTarget`
          //: 还是上一件商品 ⇒ 弹窗只列那一件（静默丢批量）。
          setSyncSingleTarget(null);
          //: 从详情发起的「重抓此商品」：关弹窗即回到它的详情
          //: （用户口径 2026-10-09：重抓就是为了让眼前这份数据变新）。
          const target = syncReturnItem;
          setSyncReturnItem(null);
          if (target) void openDetail(target);
        }}
        onSynced={() => {
          if (selectedAccountId) void load(selectedAccountId, pageNo, pageSize);
        }}
        //: 「按条件同步」= 整店 + **后台任务**（弹窗那边点同步即关，这里接手触发）。
        onConditionSync={(options: SyncOptions) => {
          const payload: SyncNotesOptions =
            options.scope === "range"
              ? { scope: "range", startDate: options.startDate, endDate: options.endDate }
              : { scope: "full" };
          void syncAllStatusGoods(payload);
        }}
      />
      {/* ⚠️ 2026-10-08 用户口径「移除新增商品按钮和逻辑」⇒ `ProductDraftModal`
          **整块退场**（按钮 / 状态 / import / 挂载点四件全删）。
          草稿箱里的既有草稿仍可查看与发布 —— 走下面的 `ProductDraftsModal`，
          所以退场的是「**从数据包新建草稿**」这条入口，不是草稿本身。 */}
      {/* 违规词库弹窗（2026-10-08 用户口径）：全屏 50%，新增/修改/停用/启用。 */}
      <ForbiddenWordsModal open={forbiddenWordsOpen} onClose={() => setForbiddenWordsOpen(false)} />
      {/* 草稿箱弹窗（设计 24 §9.4，2026-10-08 用户口径）：账号跟随本页选定店铺。 */}
      <ProductDraftsModal
        open={draftsModalOpen}
        platformAccountId={selectedAccountId}
        shopName={
          accounts.find((account) => account.id === selectedAccountId)?.nickname ?? ""
        }
        onClose={() => setDraftsModalOpen(false)}
      />
      {/* 发布商品向导（设计 25 §4；用户口径 2026-10-08）：
          antd `Steps` 三步 —— 预检 → 配置商品参数+预览 → 发布。
          ⚠️ 真实上架；后端发布成功后自动立即下架（红线）。 */}
      <ProductPublishWizard
        open={publishWizardOpen}
        platformAccountId={selectedAccountId}
        shopName={accounts.find((account) => account.id === selectedAccountId)?.nickname ?? ""}
        onClose={() => setPublishWizardOpen(false)}
        onPublished={() => {
          //: 发布后商品进台账（也可能已下架）—— 刷新本页列表让用户看到真实状态。
          if (selectedAccountId) void load(selectedAccountId, 1, pageSize, sort);
        }}
      />
      {/* ark 门禁提示（需求 5：与账号卡片页「同步商品」同一提示口径）。
          ⚠️ `unknown`（未检测）**不拦**（DEF-057：没问到 ≠ 不可用）—— 只有确定不可用才提示。 */}
      <Modal
        centered
        open={arkPromptOpen}
        title="需要先登录千帆"
        onCancel={() => setArkPromptOpen(false)}
        onOk={() => setArkPromptOpen(false)}
        okText="知道了"
        cancelButtonProps={{ style: { display: "none" } }}
      >
        同步商品需要可用的千帆令牌，请到「账号矩阵」登录 / 重新登录千帆后再试。
      </Modal>
    </Flex>
    </div>
      {/* 页面级等待蒙版：与全站同一套视觉（fixed 铺满 + 白卡居中 + 已耗时）。 */}
      {/* 用户口径 2026-10-20：「可以不要蒙版只要居中的弹窗提示」
          ⇒ `mask={false}`：不画半透明底（页面不被"洗白"两层），只显示居中白卡片 + 耗时。 */}
      <WaitMask
        open={waitRunning}
        title={waitLabel ?? ""}
        elapsed={waitElapsedSec}
      />
    </div>
  );
}
