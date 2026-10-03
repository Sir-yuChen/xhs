import {
  CopyOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  FileAddOutlined,
  PictureOutlined,
  RobotOutlined,
  ReloadOutlined,
  SearchOutlined,
  ShoppingOutlined,
  SyncOutlined,
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
  Empty,
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
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import type { Dayjs } from "dayjs";
import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  batchGenerateProductNotes,
  deleteXhsShopItems,
  EMPTY_GENERATE_QUEUE_SNAPSHOT,
  fetchAccounts,
  fetchGenerateQueue,
  fetchXhsItemNotes,
  fetchPromptTemplates,
  fetchXhsQianfanBinding,
  fetchXhsShopItemDetail,
  fetchXhsShopItems,
  generateProductNote,
  generateProductDetailImage,
  syncShopGoods,
} from "../../../lib/api";
import type { GenerateQueueFinishedEntry, GenerateQueueSnapshot } from "../../../lib/api";
import { yuan } from "../../../lib/format";
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
import { LlmGenerateModal } from "./llm-generate-modal";
import { LlmGenerateProgressCard } from "./llm-generate-progress-card";
import {
  accountLabel,
  batchDeleteBlock,
  batchDeleteHint,
  DEFAULT_SHOP_ITEMS_PAGE_SIZE,
  DEFAULT_SORT,
  ledgerPrerequisite,
  MAX_BATCH_DELETE,
  NOTE_COUNT_OP_OPTIONS,
  noteCountFilter,
  normalizeDeleteIds,
  QIANFAN_PREREQUISITE_HINT,
  resolveSort,
  sanitizeSortState,
  SHOP_ITEMS_PAGE_SIZES,
  SORT_FIELD_BY_COLUMN,
  selectableAccounts,
  skuCell,
  sortSummary,
  statusFilterHint,
  type SortState,
} from "../../../lib/shop-items-view";
import type {
  GenerateDetailImageResult,
  PlatformAccount,
  PromptTemplate,
  XhsItemNote,
  XhsItemNotesResponse,
  XhsLoginChannels,
  XhsNoteCountOp,
  XhsQianfanBinding,
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
}: {
  groupKey: string;
  groups: GenerateDetailImageResult["groups"];
  regenerating: { group: string; index: number } | null;
  generating: boolean;
  onRegenerate: (index: number) => void;
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
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          {group.assets.map((asset) => {
            const busy =
              regenerating?.group === groupKey &&
              regenerating?.index === (asset.group_index || 1);
            return (
              <div key={asset.id}>
                <Image
                  src={asset.url || asset.local_path}
                  width="100%"
                  style={{ objectFit: "contain", borderRadius: 4 }}
                />
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 2 }}>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    第 {asset.group_index || 1} 张 · 模型 {asset.model_name || "-"} · 素材库（#
                    {asset.id}）
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

type QueryForm = {
  keyword?: string;
  status: XhsShopItemStatus;
  price_from?: number;
  price_to?: number;
  stock_from?: number;
  stock_to?: number;
  sales_from?: number;
  sales_to?: number;
  /** 创建时间区间（千帆的「创建时间」筛选）。 */
  create_time_range?: [Dayjs | null, Dayjs | null] | null;
  /** 挂链笔记数：比较符（默认「小于」）+ 指定篇数（留空 = 不筛）。v1.34 */
  note_count_op?: XhsNoteCountOp;
  note_count?: number;
};

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
  const [selectedAccountId, setSelectedAccountId] = usePersistedFilter<number | null>(
    FILTER_CACHE_KEYS.shopItems.accountId, null, asFiniteNumber
  );
  const [binding, setBinding] = useState<XhsQianfanBinding | null>(null);
  const [bindingLoaded, setBindingLoaded] = useState(false);
  const [data, setData] = useState<XhsShopItemsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorText, setErrorText] = useState("");
  const [pageNo, setPageNo] = useState(1);
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
   * 「同步商品」按钮态（v2.13，设计 13 §30.3.2）。
   *
   * 同步是**后台任务**（全量翻页 + 限流等待，可能几十秒到几分钟）⇒ 按钮只表示
   * 「这次点击的触发请求在途」，**不代表同步跑完**。跑完与否看台账「同步于」时间
   * 与账号矩阵的任务日志 —— 不在这里假装有进度（进度卡的判据在后端任务行）。
   */
  const [goodsSyncing, setGoodsSyncing] = useState(false);
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
   * 但基础文案 `"生成AI商品图"` 保持不变（它是页面守卫切范围的锚点）。
   */
  const detailImageTitle = detailImageGenerating
    ? `生成AI商品图 · 生成中 ${detailImageElapsed}s（请勿关闭，关闭会看不到结果）`
    : "生成AI商品图";
  const [detailImage, setDetailImage] = useState<GenerateDetailImageResult | null>(null);
  // v2.15：商品图集（主图 / 原详情图）与勾选的参考图。
  const [detailImageDetail, setDetailImageDetail] = useState<XhsShopItemDetail | null>(null);
  const [detailImageRefs, setDetailImageRefs] = useState<string[]>([]);
  /** v2.21：单张重做的目标（`null` = 整组生成中/空闲）—— 只让那一张转圈。 */
  const [detailImageRegenerating, setDetailImageRegenerating] = useState<{
    group: string;
    index: number;
  } | null>(null);
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

  const load = useCallback(
    async (accountId: number, page = pageNo, size = pageSize, sortArg = sort) => {
      const values = form.getFieldsValue();
      const range = values.create_time_range;
      const query: XhsShopItemsQuery = {
        platform_account_id: accountId,
        page_no: page,
        page_size: size,
        keyword: values.keyword?.trim() || undefined,
        status: values.status || "on_shelf",
        price_from: values.price_from ?? undefined,
        price_to: values.price_to ?? undefined,
        stock_from: values.stock_from ?? undefined,
        stock_to: values.stock_to ?? undefined,
        sales_from: values.sales_from ?? undefined,
        sales_to: values.sales_to ?? undefined,
        // 日期按天传给后端：结束日含当天（后端统一补 23:59:59.999 并转毫秒）
        create_time_from: range?.[0] ? dayjs(range[0]).format("YYYY-MM-DD") : undefined,
        create_time_to: range?.[1] ? dayjs(range[1]).format("YYYY-MM-DD") : undefined,
        // 挂链笔记数（v1.34）：留空 = **不带这个参数**（= 不筛），不是「小于 0」
        ...noteCountFilter(values.note_count_op, values.note_count),
        // v1.32：排序由表头箭头驱动（状态在 sort），回服务端排。
        // v1.36：**无排序态不带这两个参数** —— 让后端用它自己的稳定默认序
        // （`create_time` 倒序）；antd 三级循环的「取消排序」也落回这里。
        // 前端自己编一个默认字段（或送空串）都等于替后端做主 = 第二份真相源。
        ...(sortArg
          ? { sort_field: sortArg.field, order: sortArg.order === "ascend" ? "asc" : "desc" }
          : {}),
      };
      setLoading(true);
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

  /**
   * 账号 → 三个平台的登录态（**零平台请求**：随账号列表一起下发，设计 13 §18.5）。
   *
   * ⚠️ 2026-09-30 用户口径「商品列表页查询的本地数据库，无需校验千帆是否可用」：
   * 本页是**纯本地台账读**，`ark` 会话可用性**不再拦列表请求**。这个 map 仍保留，
   * 用途改成「ark 失效时给一行安静说明 / 禁用打平台的按钮」，不再是前置门禁。
   */
  const channelsByAccount = useMemo(() => {
    const map: Record<number, XhsLoginChannels> = {};
    for (const account of accounts) map[account.id] = account.login_channels || {};
    return map;
  }, [accounts]);

  /**
   * 台账前置校验（`ledgerPrerequisite`）。**ADR-021 + 2026-09-30 口径后 `ok` 恒为
   * true**（本地读零门禁）—— 保留这个 `useMemo` 是为了让「要不要发列表请求」这个
   * 决策仍然只有一个改动点；`arkOk` 单独取出来给提示语用。
   */
  const prereq = useMemo(
    () =>
      ledgerPrerequisite(
        selectedAccountId === null ? undefined : channelsByAccount[selectedAccountId]
      ),
    [selectedAccountId, channelsByAccount]
  );

  /** 本账号的 `ark`（千帆）会话此刻是否「确定不可用」。 */
  const arkUnavailable = prereq.reason === "ark_unavailable";

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
    if (!prereq.ok) return;
    void load(selectedAccountId, pageNo, pageSize);
    // ⚠️ 依赖里**不再有 `binding?.bound`**：切账号时 `binding` 先被清成 null 再回来，
    // 带上它会让「绑定后拉一页」的语义与实际（零门禁、切账号即拉）不符。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountId, bindingLoaded, prereq.ok]);

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

  /** LLM 生成入队成功 → 登记进轮询集合（操作列按钮转圈，进度卡开跑）。 */
  function handleLlmQueued(itemId: string) {
    setLlmQueueItemIds((current) => new Set(current).add(itemId));
    setLlmGenerating(null);
  }

  /** LLM 生成某条终态 → 失败如实报原因（原因只在快照 finished 里给一次）。 */
  function handleLlmFinished(entry: { item_id: string; status: string; reason?: string }) {
    setLlmQueueItemIds((current) => {
      if (!current.has(entry.item_id)) return current;
      const next = new Set(current);
      next.delete(entry.item_id);
      return next;
    });
    if (entry.status === "failed" && entry.reason) {
      message.error(`生成失败：${entry.reason}`);
    }
    // 成功不弹消息：卡片上的「生成成功」计数已经在说话（避免每篇一条弹窗）
  }

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
  async function syncAllStatusGoods() {
    if (!selectedAccountId) return;
    setGoodsSyncing(true);
    try {
      const result = await syncShopGoods({ platformAccountId: selectedAccountId });
      if (result.reused) {
        message.info("该账号已有商品同步任务在执行，未重复触发");
      } else {
        message.success("已开始同步全部状态商品（后台执行，进度见账号矩阵任务日志）");
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : "触发同步失败");
    } finally {
      setGoodsSyncing(false);
    }
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
    Modal.confirm({
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
  function openDetailImage(item: XhsShopItem) {
    setDetailImageItem(item);
    setDetailImage(null);
    setDetailImageLoading(true);
    setDetailImageOpen(true);
    setDetailImageRefs([]); // 待详情回来后默认勾首图
    // v2.22：各组配置（提示词 / 张数）与当前 Tab 一并复位（同一条「跨商品不串」纪律）。
    setDetailImageGroupConfigs({});
    setActiveGroup("product");
    setDetailImageRegenerating(null); // v2.21：重做态一并复位
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
  const activeGenerateLabel = activeGroup === "product" ? "主图" : "详情图";
  const activeGroupUrls =
    activeGroup === "product"
      ? detailImageDetail?.product_images || []
      : detailImageDetail?.detail_images || [];
  const activeGroupRefs = detailImageRefs.filter((url) => activeGroupUrls.includes(url));

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

  const statusHint = statusFilterHint(form.getFieldValue("status") || "on_shelf");

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
                <Tag color={item.buyable ? "green" : "default"}>{item.status_label}</Tag>
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
    {
      title: "操作",
      key: "action",
      width: 130,
      render: (_value, item) => (
        <Space size={4}>
          {/* 生成笔记（设计 13 §31.4，用户需求③；§31.10 起入队即返回）：
              调千帆后台智能生成一篇挂本商品的笔记（待发布，之后到内容库发布）。
              loading 判据 = 快照的 running + queued（队列在执行这一项），
              不只是「本次点击在等回执」—— 回执毫秒级，执行在后台。 */}
          <Tooltip title="调用千帆后台智能生成一篇挂本商品的笔记（入队后后台执行，待发布，之后到内容库发布）">
            <Button
              type="link"
              size="small"
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
              是两个队列、两条链路）。开的是**生成配置弹窗**（选模板 + 媒体来源四选一）。 */}
          <Tooltip title="用项目自己的大模型链路生成笔记（选模板 / 媒体来源，入队后台执行）">
            <Button
              type="link"
              size="small"
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
          {/* 生成商品详情图（设计 17 §13.3.3，v2.1；v2.12 移到操作列独立弹窗）：
              AI 出图给**商品**用（与「生成笔记」的正文配图是两个业务点）。
              开的是独立弹窗（`openDetailImage`），不在编辑 / 详情弹窗里。 */}
          <Tooltip title="AI 生成该商品的详情图 / 主图（独立弹窗，可填补充要求）">
            <Button
              type="link"
              size="small"
              icon={<PictureOutlined />}
              onClick={() => openDetailImage(item)}
            >
              生成AI商品图
            </Button>
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Card
        title={
          <Space>
            <ShoppingOutlined />
            商品列表（千帆）
          </Space>
        }
        extra={
          <Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              账号
            </Text>
            <Select
              style={{ minWidth: 260 }}
              placeholder={accountsLoaded ? "选择小红书账号" : "加载中…"}
              value={selectedAccountId ?? undefined}
              options={accountOptions}
              notFoundContent="没有小红书账号"
              onChange={(value) => {
                setSelectedAccountId(value);
                setPageNo(1);
              }}
            />
          </Space>
        }
      >
        {accountsLoaded && accounts.length === 0 ? (
          <Alert
            type="warning"
            showIcon
            message="没有小红书账号可选"
            description={
              <Space direction="vertical" size={6}>
                <Text style={{ fontSize: 13 }}>
                  商品台账按**小红书账号**隔离；下拉不按账号类型过滤，所有账号都能选。
                  千帆相关的「同步商品 / 生成笔记」需要该账号自己有可用的千帆登录态。
                </Text>
                <Button
                  size="small"
                  href="/platforms/xhs/accounts"
                  onClick={(event) => {
                    event.preventDefault();
                    window.location.assign("/platforms/xhs/accounts");
                  }}
                >
                  去「账号矩阵」添加账号
                </Button>
              </Space>
            }
          />
        ) : !bindingLoaded ? (
          <Spin />
        ) : arkUnavailable ? (
          /*
            千帆（ark）会话「确定不可用」时的**一行安静说明**（2026-09-30 起）。
            ⚠️ **不再拦请求、不再拦页面**（用户口径「查询本地数据库无需校验千帆是否
            可用」）：下面那个拉列表的 effect 已按 `prereq.ok`（恒 true）放行，本地台账
            照常展示；这里只提醒「想同步/生成笔记的话得先补登千帆」，并给一个去账号矩阵
            的入口。`unknown`（未检测/探活无结论）**不进这里** —— DEF-057 红线
            （没问到 ≠ 不可用），与 `sync-gates.channelUnavailable` 同一把尺子。
          */
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="本账号的千帆登录态不可用（不影响查看本地台账）"
            description={
              <Space direction="vertical" size={8}>
                <Text style={{ fontSize: 13 }}>{QIANFAN_PREREQUISITE_HINT}</Text>
                <Button
                  size="small"
                  href="/platforms/xhs/accounts"
                  onClick={(event) => {
                    event.preventDefault();
                    window.location.assign("/platforms/xhs/accounts");
                  }}
                >
                  去「账号矩阵」扫码登录千帆
                </Button>
              </Space>
            }
          />
        ) : statusHint ? (
          /*
            审核态筛选的安静说明（§30.5，25–30 字一行）。
            v1.32 已把「店铺：xxx」「本地台账 N 件 · 同步于 X」「数据说明」三个文本
            移除（店铺与状态口径在底部 Descriptions，同步时效出口在账号矩阵页），
            这里只剩状态说明这一种内容。

            ⚠️ **必须裸 `Text`，不能再套 `<Space direction="vertical">`**（v1.35 修）：
            antd `Card` 只在 `childNodes.length > 0` 时才渲染 `.ant-card-body`
            （`antd/es/card/Card.js`：`loading || childNodes.length ? <div …/> : null`），
            而 `React.Children.toArray` 会把 `null` / `false` **过滤掉**。原先那个
            `Space` 即使一个子项都没有也是**真元素** ⇒ body 照渲、上下 24px 内边距照留，
            默认状态（`statusHint` 为空）就在「商品列表（千帆）」标题下留出一条空白。
            改成裸 `Text` + `null` 兜底后，没说明时 body **整块不渲染**，空白消失
            （用户 2026-09-23 上机口径：「商品列表（千帆）下的空白处移除」）。
          */
          <Text type="secondary" style={{ fontSize: 12 }}>
            {statusHint}
          </Text>
        ) : null}

        {errorText && (
          <Alert
            style={{ marginTop: 12 }}
            type="error"
            showIcon
            message="商品列表加载失败"
            description={errorText}
          />
        )}
      </Card>

      {/* 前置门禁不通过（`prereq.ok === false`）⇒ 搜索区与台账都不渲染：
          没有可查的对象，摆出来只会让人以为能查。
          注意这是**渲染层**的拦，真正的「零请求」由上面那个按 `prereq.ok`
          守住的 effect 保证 —— 少写任何一半都只是半道防线。 */}
      {prereq.ok && (
        <>
        {/* 搜索条件：与千帆商品列表页一一对应（关键词 / 状态 / 价格 / 库存 / 销量）。
            **排序不在这里** —— v1.32 起挪到表头（上下箭头，服务端排序）。

            2026-09-23 上机口径（「搜索条件区域重新布局，布局在两行内，搜索重置等按钮
            放条件的最后」）：9 个条件 + 按钮组**恰好两行排完**，按钮组落在第二行末尾。
            原来按钮分散在两行各占一列，视线要在两行之间来回找，且第二行被 4 列窄
            `InputNumber` 挤得半行空半行满。 */}
        <Card size="small" style={{ marginTop: 12 }} title="搜索条件">
        <Form<QueryForm>
          form={form}
          layout="vertical"
          initialValues={formInitialValues}
          // 每次改动即回写缓存（不等点「搜索」）：用户填了一半切走再回来，条件还在。
          // 「重置」走 `form.resetFields()` ⇒ 回到 `initialValues`（也就是**缓存里的值**），
          // 所以下面那一拍必须显式把**清空态**写回缓存（否则重置后重进页面又长回来了）。
          onValuesChange={() => writeFilterCache(FILTER_CACHE_KEYS.shopItems.form, formValuesToDraft(form.getFieldsValue()))}
        >
            {/* 第一行：关键词(6) + 状态(4) + 价格区间(4+4) + 创建时间(6) = 24 */}
            <Row gutter={12}>
              <Col span={6}>
                <Form.Item label="关键词" name="keyword" style={{ marginBottom: 12 }}>
                  <Input
                    allowClear
                    placeholder="商品名称 / 商品 ID / 商品货号，多条用逗号隔开"
                    onPressEnter={() => selectedAccountId && void load(selectedAccountId, 1, pageSize)}
                  />
                </Form.Item>
              </Col>
              <Col span={4}>
                <Form.Item label="状态" name="status" style={{ marginBottom: 12 }}>
                  <Select options={STATUS_OPTIONS} />
                </Form.Item>
              </Col>
              <Col span={4}>
                <Form.Item label="价格下限（元）" name="price_from" style={{ marginBottom: 12 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最低售价" />
                </Form.Item>
              </Col>
              <Col span={4}>
                <Form.Item label="价格上限（元）" name="price_to" style={{ marginBottom: 12 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最高售价" />
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item label="创建时间" name="create_time_range" style={{ marginBottom: 12 }}>
                  <RangePicker
                    style={{ width: "100%" }}
                    placeholder={["开始日期", "截止日期"]}
                    allowClear
                  />
                </Form.Item>
              </Col>
            </Row>
            {/* 第二行：库存区间(3+3) + 销量区间(3+3) + 挂链笔记数(6) + 按钮组(6，右对齐收尾) = 24 */}
            <Row gutter={12}>
              <Col span={3}>
                <Form.Item label="库存下限" name="stock_from" style={{ marginBottom: 8 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最小库存" />
                </Form.Item>
              </Col>
              <Col span={3}>
                <Form.Item label="库存上限" name="stock_to" style={{ marginBottom: 8 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最大库存" />
                </Form.Item>
              </Col>
              <Col span={3}>
                <Form.Item label="销量下限" name="sales_from" style={{ marginBottom: 8 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最低销量" />
                </Form.Item>
              </Col>
              <Col span={3}>
                <Form.Item label="销量上限" name="sales_to" style={{ marginBottom: 8 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最高销量" />
                </Form.Item>
              </Col>
              {/*
                挂链笔记数（v1.34，用户口径「添加挂链笔记数，默认小于指定篇数」）：
                比较符默认「小于」+ 指定篇数。值**留空 = 不筛**（不是「小于 0」）——
                `0` 是有意义的阈值（「一篇都没挂」），缺省态不该偷偷筛掉数据。
                口径 = `note_goods` 关系表，与本列显示的篇数是**同一个数**（§33.8）。
              */}
              <Col span={6}>
                <Form.Item label="挂链笔记数" style={{ marginBottom: 8 }}>
                  <Space.Compact style={{ width: "100%" }}>
                    <Form.Item name="note_count_op" noStyle>
                      <Select
                        style={{ width: 96, flexShrink: 0 }}
                        options={NOTE_COUNT_OP_OPTIONS}
                      />
                    </Form.Item>
                    <Form.Item name="note_count" noStyle>
                      <InputNumber
                        style={{ width: "100%" }}
                        min={0}
                        placeholder="篇数（留空不筛）"
                      />
                    </Form.Item>
                  </Space.Compact>
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item label=" " style={{ marginBottom: 8 }}>
                  <Space style={{ width: "100%", justifyContent: "flex-end" }} wrap>
                    <Button
                      type="primary"
                      icon={<SearchOutlined />}
                      loading={loading}
                      onClick={() => runSearch(false)}
                    >
                      搜索
                    </Button>
                    <Button onClick={() => runSearch(true)}>重置</Button>
                    <Tooltip title="重新拉取当前条件的第一页">
                      <Button
                        icon={<ReloadOutlined />}
                        onClick={() => {
                          setPageNo(1);
                          if (selectedAccountId) void load(selectedAccountId, 1, pageSize);
                        }}
                      />
                    </Tooltip>
                    {/* 「同步全部状态商品」（设计 13 §30.3.2，v2.13 / 2026-10-02 用户口径
                        「商品同步更新需求为同步全部状态的商品」）：显式一条入口 ——
                        同步商品是**台账的唯一数据来源**，而台账是「本地唯一真相源」，
                        所以默认同步**平台侧全部状态**（不传 card_type），
                        不让用户以为「只同步了售卖中的」。 */}
                    <Tooltip title="从千帆同步该账号的全部状态商品（售卖中 / 已下架 / 已冻结 / 整改 / 审核中 / 审核驳回 / 审核通过）到本地台账，后台执行">
                      <Button
                        icon={<SyncOutlined spin={goodsSyncing} />}
                        loading={goodsSyncing}
                        disabled={!selectedAccountId}
                        onClick={() => void syncAllStatusGoods()}
                      >
                        同步商品
                      </Button>
                    </Tooltip>
                  </Space>
                </Form.Item>
              </Col>
            </Row>
          </Form>
        </Card>

        <Card
          size="small"
          style={{ marginTop: 12 }}
          bodyStyle={{ padding: 12 }}
          title="商品台账"
          extra={
            <Space size={8}>
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
              <Tooltip
                title={
                  selectedItemIds.length > 0
                    ? `对选中的 ${selectedItemIds.length} 个商品逐个入队生成笔记（≤20 个/次，单项失败不阻断；后台顺序执行）`
                    : "先在表格里勾选商品（最多 20 个/次）"
                }
              >
                <Button
                  type="primary"
                  icon={<FileAddOutlined spin={genQueueItemIds.size > 0} />}
                  disabled={selectedItemIds.length === 0}
                  loading={batchGenerating}
                  onClick={() => void batchGenerate()}
                >
                  批量生成智能笔记{selectedItemIds.length > 0 ? `（${selectedItemIds.length}）` : ""}
                </Button>
              </Tooltip>
              {/* 批量同步商品（v2.13，设计 13 §30.3.2）：触发该账号的**全量全部状态**同步。
                  ⚠️ 平台侧没有「只同步这几个商品」的读接口（`search_item_v2` 是按页翻，
                  `get_skus_by_item_ids` 只补规格）⇒ 批量语义落到「同步整店」，并在提示里
                  如实说明「勾选只是让你知道待同步的是哪几个，实际会刷新整店台账」——
                  不假装能按勾选裁剪（那是界面在说一件不会发生的事）。 */}
              <Tooltip
                title={
                  selectedItemIds.length > 0
                    ? `同步该账号的全部状态商品（勾选的 ${selectedItemIds.length} 个会在其中；平台按页同步，不支持只同步几个）`
                    : "先在表格里勾选商品"
                }
              >
                <Button
                  icon={<SyncOutlined />}
                  disabled={selectedItemIds.length === 0}
                  loading={goodsSyncing}
                  onClick={() => void syncAllStatusGoods()}
                >
                  批量同步商品{selectedItemIds.length > 0 ? `（${selectedItemIds.length}）` : ""}
                </Button>
              </Tooltip>
              {/* 批量删除（v1.36）：**只删本地台账行** —— 平台商品不动、关系行不动。
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
                  批量删除{selectedItemIds.length > 0 ? `（${selectedItemIds.length}）` : ""}
                </Button>
              </Tooltip>
            </Space>
          }
        >
          {/* LLM 生成进度卡（T9，设计 17 §7.4）：有待处理 / 跑过才渲染，空闲不占版面。 */}
          {llmQueueItemIds.size > 0 && (
            <div style={{ marginBottom: 12 }}>
              <LlmGenerateProgressCard
                activeItemIds={llmQueueItemIds}
                onFinishedEntry={handleLlmFinished}
              />
            </div>
          )}
          {data ? (
            <Table<XhsShopItem>
              rowKey="item_id"
              size="small"
              loading={loading}
              columns={columns}
              dataSource={data.items}
              scroll={{ x: 1300 }}
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
                showTotal: (total) => `共 ${total} 件商品`,
                onChange: (page, size) => {
                  setPageNo(page);
                  setPageSize(size);
                  if (selectedAccountId) void load(selectedAccountId, page, size);
                },
              }}
            />
          ) : (
            <Empty description={bindingLoaded ? "点「搜索」拉取商品列表" : "正在加载账号信息"} />
          )}
          {data && (
            <Descriptions size="small" column={4} style={{ marginTop: 8 }}>
              <Descriptions.Item label="商品来源">本地台账（同步商品任务落库）</Descriptions.Item>
              <Descriptions.Item label="店铺">{data.shop_name || "-"}</Descriptions.Item>
              <Descriptions.Item label="状态口径">{data.status_label}</Descriptions.Item>
              <Descriptions.Item label="命中总数">{data.total}</Descriptions.Item>
              {/* 当前排序（设计 13 §32.1）：取**后端回传的生效口径**，不是前端自己的猜想 */}
              <Descriptions.Item label="当前排序">
                {sortSummary(data.sort_field, data.order)}
              </Descriptions.Item>
              <Descriptions.Item label="台账同步时间">{data.queried_at || "-"}</Descriptions.Item>
              <Descriptions.Item label="平台商品总数">
                {data.products_total > 0 ? data.products_total : "-"}
              </Descriptions.Item>
            </Descriptions>
          )}
        </Card>
        </>
      )}

      {/* 商品详情弹窗（v1.32，设计 13 §30.3.6）：基本信息 + 图集 + 规格详情 */}
      <Modal
        open={detailOpen}
        title="商品详情"
        width="80%"
        footer={
          <Space>
            <Button onClick={() => setDetailOpen(false)}>关闭</Button>
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
                  description="规格详情还没补齐（同步商品任务会自动多轮补足，届时这里展示各规格的价格 / 库存 / 条形码）"
                />
              )}
            </div>
          </Space>
        ) : null}
      </Modal>

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
                activeGroupRefs.length === 0
                  ? `请先在「${activeGroupLabel}」里勾选至少一张参考图`
                  : ""
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
        {/* v2.28：**生成中蒙版**（用户口径「点击生成图按钮后，弹窗加蒙版并统计耗时，
            防止被关闭」）。
            ⚠️ 蒙版只盖**配置区**（商品信息 + 三个 Tab），**结果区不盖** ——
            用户要能看见新图一张张出来（盖住结果等于把「在跑」变成「黑屏」）。
            `pointerEvents: none` 是有意的：蒙版是「别改了」的视觉提示，
            真正的门禁是各控件的 `disabled={detailImageGenerating}`（已在位）。 */}
        {detailImageGenerating && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "8px 12px",
              marginBottom: 12,
              borderRadius: 6,
              background: "rgba(0,0,0,0.04)",
              border: "1px solid rgba(0,0,0,0.08)",
            }}
          >
            <Spin size="small" />
            <Text strong>正在出图… 已耗时 {detailImageElapsed} 秒</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              逐张串行调用上游，单张约 30–100 秒；生成期间弹窗不可关闭（关闭会看不到结果）
            </Text>
          </div>
        )}
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
          onChange={(key) => setActiveGroup(key as "product" | "detail")}
          items={detailImageGroupTabs.map((tab) => ({
            key: tab.key,
            label: (
              <span>
                {tab.label}
              </span>
            ),
            children: (
              <Spin spinning={detailImageLoading}>
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
                    {/* ⚠️ 标题与说明**各占一行**（用户口径「不要和图片一行」）：
                        两者都是行内元素，不加 `display: block` 会挤在同一行。 */}
                    <Text strong style={{ fontSize: 12, display: "block" }}>
                      本类原始图预览
                    </Text>
                    <Text
                      type="secondary"
                      style={{ fontSize: 11, display: "block", marginBottom: 6 }}
                    >
                      {tab.label} · 共 {tab.urls.length} 张 · 仅供预览，不参与勾选
                    </Text>
                    {tab.urls.length === 0 ? (
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {tab.emptyHint}
                      </Text>
                    ) : (
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

                  {/* ③ 公共提示词模板（本组独立，v2.24） */}
                  <div>
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
                    <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 4 }}>
                      模板在「笔记工坊 → 提示词模板」维护（含一致性约束），选它就按模板出图；
                      下方「补充要求」会追加在模板末尾。
                    </Text>
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

                  {/* ④ 生成张数（本组独立） */}
                  <div>
                    <Text strong style={{ fontSize: 13 }}>
                      生成张数
                    </Text>
                    <div style={{ marginTop: 4 }}>
                      <Space size={6} align="center">
                        <InputNumber
                          min={3}
                          max={10}
                          value={detailImageGroupConfigs[tab.key]?.count ?? 3}
                          onChange={(value) => {
                            const next = Math.max(3, Math.min(10, Number(value) || 3));
                            setDetailImageGroupConfigs((current) => ({
                              ...current,
                              [tab.key]: { ...current[tab.key], count: next },
                            }));
                          }}
                          disabled={detailImageGenerating}
                          style={{ width: 110 }}
                          addonAfter="张"
                        />
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          3 ~ 10 张（每张 = 一次生成请求）
                        </Text>
                      </Space>
                    </div>
                  </div>

                  {/* ⑤ 本组结果 */}
                  <DetailImageGroupResult
                    groupKey={tab.key}
                    groups={detailImage?.groups || []}
                    regenerating={detailImageRegenerating}
                    generating={detailImageGenerating}
                    onRegenerate={(index) =>
                      void generateDetailImage({ group: tab.key, index })
                    }
                  />
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
      </Modal>

      {/* LLM 生成配置弹窗（T9，设计 17 §7.4）：选模板 + 媒体来源四选一。
          与「生成商品详情图」同型：弹窗自带商品上下文，打开即重置（跨商品不串）。 */}
      <LlmGenerateModal
        open={llmGenerateOpen}
        accountId={selectedAccountId}
        itemId={llmGenerateItem?.item_id || ""}
        itemTitle={llmGenerateItem?.title || ""}
        onClose={() => setLlmGenerateOpen(false)}
        onQueued={handleLlmQueued}
      />

      {/* ⚠️ 「绑定千帆商家后台」弹窗已整块移除（ADR-021，2026-09-30）：
          `POST /xhs/qianfan/bind` 退场（后端 410 `QIANFAN_BINDING_REMOVED`），
          千帆能力改由「该账号自己的 ark 登录态」决定 —— 入口在账号矩阵。
          被取代的原弹窗（保留备查）：
            <Modal open={bindOpen} title="绑定千帆商家后台（商品列表来源）" …>
              … 粘贴 Cookie（须含 access-token-ark.xiaohongshu.com）…
            </Modal> */}

      {/*
        商品 → 挂链笔记弹窗（设计 13 §31.6）：**关系表源**（note_goods join notes），
        零平台请求。多篇笔记用 tab 自然隔离；内容区复用内容库详情弹窗的同一份
        两栏版式（NoteDetailBody）—— 同一语义不留两份实现。
      */}
      <Modal
        open={notesOpen}
        // 尺寸（v1.36 用户口径「挂链笔记弹窗全屏 75%」）：宽高都取视口的 75%。
        // 原来的 80vw 是「尽量大」，与商品详情弹窗（80%）几乎同宽 —— 两个弹窗
        // 看起来一样大、层级感反而没了；收到 75% 后它是「一个更大的详情浮层」，
        // 与列表的对比更清楚。**不再设 maxWidth**：设了它就不是 75% 了
        // （宽屏上 75vw > 1600 会被截回，口径与实际不符）。
        width="75vw"
        centered
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
            <Empty description="关系表里这个商品还没有挂链笔记（可能是未同步，也可能确实没挂；「生成笔记」会建立新的映射）" />
          )}
        </div>
      </Modal>
    </div>
  );
}
