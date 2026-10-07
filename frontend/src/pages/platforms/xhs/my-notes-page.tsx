import dayjs from "dayjs";
import {
  CommentOutlined,
  CopyOutlined,
  DeleteOutlined,
  FileTextOutlined,
  HeartOutlined,
  HistoryOutlined,
  LinkOutlined,
  LoadingOutlined,
  PictureOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  ShareAltOutlined,
  ShoppingOutlined,
  StarOutlined,
  SyncOutlined,
  UploadOutlined,
  UserOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Avatar,
  Button,
  Card,
  Checkbox,
  Col,
  DatePicker,
  Descriptions,
  Divider,
  Empty,
  Image,
  InputNumber,
  Input,
  Modal,
  Popconfirm,
  Radio,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Table,
  Upload,
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { useThemePalette } from "../../../app/providers";
import {
  FILTER_CACHE_KEYS,
  MY_NOTES_TAB_VALUES,
  asEnum,
  asFields,
  asFiniteNumber,
  usePersistedFilter,
} from "../../../lib/filter-cache";
import { loginChannelRows } from "../../../lib/login-state";
import {
  fetchAccounts,
  fetchXhsInteractionMessages,
  fetchXhsNoteComments,
  fetchXhsNoteDetail,
  fetchXhsSelfCapabilities,
  fetchXhsSelfCollectedNotes,
  fetchXhsSelfIdentity,
  fetchXhsSelfLikedNotes,
  fetchXhsSelfNotes,
  fetchXhsSelfSearch,
  fetchXhsQianfanBinding,
  checkXhsQianfanShopHealth,
  fetchOfflineNotes,
  importNoteData,
  syncNoteData,
  hydrateOfflineNoteDetail,
  fetchScheduleLogs,
  batchDeleteNotes,
  fetchNoTrafficCandidates,
  fetchNoTrafficCleanupActive,
  fetchNoTrafficCleanupLogs,
  startNoTrafficCleanup,
  fetchNoTrafficCleanupStatus,
  fetchXhsAccountLoginStates,
  type NoTrafficThresholds,
  type NoTrafficCandidate,
  type NoTrafficCleanupItemRow,
  type NoTrafficCleanupLogItem,
  type NoTrafficCleanupStatus,
  type NoTrafficQuery,
} from "../../../lib/api";
import type {
  NoteComment,
  NoteGoods,
  PlatformAccount,
  XhsGoodsSource,
  XhsInteractionKind,
  XhsInteractionMessage,
  XhsLoginChannels,
  XhsQianfanBinding,
  XhsSearchNote,
  XhsSelfCapabilities,
  XhsSelfContentScope,
  XhsSelfIdentity,
  XhsSelfSearchResponse,
  XhsSelfSearchScopeMeta,
} from "../../../types";
// 笔记详情弹窗的**共用实现**（版式 / 媒体区 / 失败三分法 / 评论四态都在里面）：
// 内容库页用同一份，避免「同一个看笔记的动作」在两页各写一遍（DEF-044 / DEF-058）。
import {
  NoteCommentList,
  NoteDetailModal,
  formatRemoteTime,
  quietError,
  readError,
  renderFailure,
} from "../../../components/platforms/note-detail-modal";
import type { QuietError } from "../../../components/platforms/note-detail-modal";

const { Text, Paragraph, Link: AntLink } = Typography;

/**
 * 详情响应与列表行的合并（DEF-047）。
 *
 * `POST /xhs/pc/notes/detail` 走的是 www feed，那个 payload **结构上没有商品容器**
 * （设计 12 §19.1 实测），所以它的 `goods` 恒为 `null`；而列表项由后端的
 * `_attach_shop_goods` 带上了完整的 `NoteGoods`。裸 `{ ...note, ...detail }` 会让
 * 那个 `null` **覆盖掉已经补齐的商品** —— 现象是「正文加载完之后，挂载商品从完整
 * 卡片变成一行空图」（用户 2026-09-20 报出）。
 *
 * 缺值与「没有」必须分开（§18.2）：**详情没带回这个字段时保留列表行的那一份**。
 */
function mergeNoteDetail(
  note: XhsSearchNote,
  detail: XhsSearchNote,
  fallbackUrl: string,
): XhsSearchNote {
  // 详情端点回填的 `goods` 只带标题/图/规格，**不带价格**（补价只在列表端点做）；
  // 若直接 `detail.goods ?? note.goods`，非空的 `detail.goods` 会用空 price 覆盖掉
  // 列表行补过价的 price —— 现象就是「价格闪现一下又消失」（2026-10-06 用户报出）。
  // 这里做**字段级回填**：detail.goods 的 price 为空时，保留 note.goods 的那一份。
  const goods: NoteGoods | null =
    detail.goods && note.goods
      ? { ...detail.goods, price: detail.goods.price || note.goods.price }
      : (detail.goods ?? note.goods ?? null);
  return {
    ...note,
    ...detail,
    goods,
    shop: detail.shop ?? note.shop ?? null,
    note_url: detail.note_url || fallbackUrl,
  };
}

/** 弹窗里一条挂载商品的**渲染态**（两个来源合成后的同形状，DEF-047）。 */
type MountedGood = {
  key: string;
  goodsId: string;
  title: string;
  variant: string;
  price: string;
  image: string;
  url: string;
  shopName: string;
};

/**
 * 把两处「挂载商品」合成**一份**列表，弹窗里**只渲染一块**。
 *
 * 两个来源其实是同一份数据：`goods`（列表行带上来的 `NoteGoods`，信息最全 ——
 * 含店铺名与补到的价格，**优先**）与 `shop.goods`（详情按 `note_ids` 查千帆得到的
 * 数组，用来补前者没有的商品）。早前两个来源各渲染一块、版式还互不相同，
 * 于是正文一到、`goods` 被覆盖，版式当场变样。
 */
function mountGoods(goods: NoteGoods | null, shop: XhsSearchNote["shop"]): MountedGood[] {
  const list: MountedGood[] = [];
  const seen = new Set<string>();
  if (goods) {
    list.push({
      key: goods.goods_id || "goods",
      goodsId: goods.goods_id || "",
      title: goods.title || "",
      variant: goods.variant || "",
      price: goods.price || "",
      image: goods.image || "",
      url: goods.url || "",
      shopName: goods.shop_name || "",
    });
    if (goods.goods_id) seen.add(goods.goods_id);
  }
  const shopName = shop?.source || "";
  for (const item of shop?.goods ?? []) {
    if (!item.item_id || seen.has(item.item_id)) continue;
    seen.add(item.item_id);
    list.push({
      key: item.item_id,
      goodsId: item.item_id,
      title: item.item_title || "",
      variant: item.variant_desc || "",
      price: item.price || "",
      image: item.item_image || "",
      url: `https://www.xiaohongshu.com/goods-detail/${item.item_id}`,
      shopName,
    });
  }
  return list;
}

// ---------------------------------------------------------------------------
// 2026-10-06：离线笔记（商业/普通）详情弹窗的**指标全面展示**。
// metrics 键来自后端 `note_data_service._note_row_from_butterfly`（24+ 个指标，
// 覆盖 xlsx 全部列）。按分组渲染，缺值不显示（硬约束⑤缺值口径）。
// ---------------------------------------------------------------------------

/** 指标键 → 中文标签，按「互动 / 观看 / 商品」三组。缺值（undefined/null）不渲染。 */
const OFFLINE_METRICS_GROUPS: { label: string; metrics: [string, string][] }[] = [
  {
    label: "互动",
    metrics: [
      ["read_count", "阅读"],
      ["like_count", "点赞"],
      ["collect_count", "收藏"],
      ["comment_count", "评论"],
      ["share_count", "分享"],
      ["follow_count", "关注"],
      ["danmaku_count", "弹幕"],
    ],
  },
  {
    label: "观看",
    metrics: [
      ["avg_view_time", "平均观看时长"],
      ["full_view_rate", "完播率"],
      ["video_duration", "视频时长"],
    ],
  },
  {
    label: "商品",
    metrics: [
      ["pay_gmv", "支付金额"],
      ["pay_order_cnt", "支付订单"],
      ["pay_user_cnt", "支付人数"],
      ["item_click_cnt", "商品点击"],
      ["item_click_uv", "点击人数"],
      ["item_click_pv_rate", "点击率(PV)"],
      ["pay_pv_rate", "支付转化率(PV)"],
      ["pay_uv_rate", "支付转化率(UV)"],
      ["refund_gmv", "退款金额"],
      ["refund_order_cnt", "退款订单"],
      ["refund_user_cnt", "退款人数"],
      ["cart_item_cnt", "加购件数"],
      ["lead_user_page_view_cnt", "引流店铺主页次数"],
      ["lead_user_page_pay_gmv", "引流店铺主页支付金额"],
      ["lead_live_view_cnt", "引流直播间次数"],
      ["lead_live_pay_gmv", "引流直播间支付金额"],
    ],
  },
];

// ---------------------------------------------------------------------------
// Self content page: my notes / liked / collected / interaction messages.
// Design source: docs/02_设计文档/12_功能设计_我的笔记与互动数据.md
//
// Read-only live view. Nothing on this page is persisted, and every list is
// requested one page at a time — never a background full crawl (§12).
//
// Two facts drive the whole layout, both verified against the live platform
// (design doc §18) and easy to "fix" wrongly:
//
//   1. The list endpoints return a *thin* object — cover, display_title,
//      interact_info.liked_count, note_id, time, type, xsec_token — and nothing
//      else. No desc, no image_list, no collected/comment/share counts. So the
//      card shows only what exists, and the full picture is fetched on demand
//      when the detail modal opens (§11.4 / D7).
//   2. Which tabs work depends on the *cookie jar*, not on `sub_type`
//      (§17). Creator accounts have no www session of their own, and
//      liked/collected/interactions are only served over www.
//
//      **v1.27 (user decision, 2026-09-21)**: the page no longer *pre-judges*
//      availability by account type or capability — that pre-judgement greyed
//      out tabs whose endpoints were in fact reachable (a creator account only
//      stores an `ark` jar, which is a perfectly normal shape, yet the greyed
//      reason claimed the data was unreachable). Now every tab is clickable and
//      always issues its request; the *result* decides what is rendered:
//      data → render it; empty → empty state; failure → one quiet 25–30 char
//      line (no toast, no red block). Design doc §17.8.
//
//      The identity boundary is NOT relaxed by this: liked/collected/
//      interactions only ever use **this account's own** session, so a
//      failure here means "nothing to show", never "show someone else's data".
// ---------------------------------------------------------------------------

// 2026-10-06 需求「我的笔记页面重构」：新增「商业笔记」（goods）与「普通笔记」（normal）
// 两个离线页签。`XhsSelfContentScope` 是线上三分类（notes/liked/collected），
// 离线两个是**新的取值域**，故 TabKey 显式扩展，不并进 XhsSelfContentScope。
type TabKey = XhsSelfContentScope | "interactions" | "goods" | "normal";

type PanelState<T> = {
  items: T[];
  cursor: string;
  hasMore: boolean;
  loading: boolean;
  /**
   * 失败信息带**错误码**（v1.28 / DEF-058）：呈现方式取决于「这是不是预期内的取不到」，
   * 只留一句文案就只能一律渲染成同一个样子。
   */
  error: QuietError | null;
  loaded: boolean;
};

const emptyPanel = <T,>(): PanelState<T> => ({
  items: [],
  cursor: "",
  hasMore: false,
  loading: false,
  error: null,
  loaded: false,
});

/**
 * 失败信息：`text` 上屏（25–30 字），`full` 进 Tooltip 供排查，
 * `code` 决定**呈现方式**（见 `renderFailure` 的三分法）。
 */

const interactionKinds: { value: XhsInteractionKind; label: string }[] = [
  { value: "like_collect", label: "赞和收藏" },
  { value: "mention", label: "提到我的" },
  { value: "connection", label: "新增关注" },
];

/**
 * 互动分类的**合法取值**（用于本地缓存的取值域校验）。
 * 与上面的下拉**同一份**：新加一个分类时缓存校验自动跟上，不会出现「界面选得到、
 * 缓存校验不认」而把用户的选择静默退回默认。
 */
const INTERACTION_KIND_VALUES = interactionKinds.map((item) => item.value);

/**
 * 「无流量笔记清理」弹窗的**清空态**（阈值 + 查询条件）。
 *
 * 定义在模块级：既是状态初值、又是本地缓存的**校验模板**（`asFields` 按它的形状
 * 逐字段校验）—— 两份写开就会出现「缓存校验放行了状态初值不认的字段」。
 */
const EMPTY_NO_TRAFFIC_THRESHOLDS: NoTrafficThresholds = {
  max_browse: 0,
  max_pay_order: 0,
  max_pay_gmv: 0,
  // 2026-10-06 方案 A：可选互动指标，默认不参与判定。
  max_like: null,
  max_collect: null,
  max_comment: null,
  max_share: null,
};

const EMPTY_NO_TRAFFIC_QUERY: NoTrafficQuery = {
  keyword: "",
  note_type: "",
  published_before: null,
  category: "",
};

const TAB_LABELS: Record<TabKey, string> = {
  notes: "全部笔记",
  goods: "商业笔记",
  normal: "普通笔记",
  liked: "我赞过的",
  collected: "我收藏的",
  interactions: "互动消息",
};

/** Source labels for cross-scope search results (R8). Mirrors the backend's. */
const SCOPE_LABELS: Record<XhsSelfContentScope, string> = {
  notes: "我的笔记",
  liked: "我赞过的",
  collected: "我收藏的",
};

function formatMetric(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value >= 10000) return `${(value / 10000).toFixed(value >= 100000 ? 0 : 1)}w`;
  return value.toLocaleString();
}

function noteMediaUrls(note: XhsSearchNote): string[] {
  const urls = note.image_urls?.length ? note.image_urls : [note.cover_url];
  return urls.filter((url): url is string => Boolean(url));
}

function noteVideoUrl(note: XhsSearchNote): string {
  return note.video_url || note.video_addr || "";
}

function noteKindLabel(note: XhsSearchNote): "视频" | "图文" {
  return `${note.type || ""}`.toLowerCase().includes("video") || Boolean(noteVideoUrl(note)) ? "视频" : "图文";
}

function noteExternalUrl(note: XhsSearchNote): string {
  // 只认带 token 的 `note_url`：无 token 的 explore 链接会被平台 302 → 404
  // （error_code=300031），拼出来就是个必失效的「假链接」（DEF-053 同款）。
  // 离线笔记在 hydrate 回查后 `note_url` 会带上新鲜 token；回查失败（平台已不可见）
  // 则返回空串，前端不渲染「在小红书打开」。
  if (note.note_url && note.note_url.includes("xsec_token=")) return note.note_url;
  return "";
}

/**
 * Fold a 「继续搜索」 pass into the previous result.
 *
 * Items are de-duplicated by `scope + note_id` (the same note can be both
 * published and liked), and per-scope counters are summed so the header still
 * reports the total number of pages actually scanned across both passes.
 * Kept as a pure function so it can be reasoned about without the component.
 */
function mergeSearchResults(
  previous: XhsSelfSearchResponse,
  next: XhsSelfSearchResponse,
): XhsSelfSearchResponse {
  const seen = new Set(previous.items.map((item) => `${item.scope}:${item.note_id}`));
  const items = [...previous.items];
  for (const item of next.items) {
    const key = `${item.scope}:${item.note_id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push(item);
  }

  const scopes: Record<string, XhsSelfSearchScopeMeta> = { ...previous.scopes };
  for (const [scope, meta] of Object.entries(next.scopes) as [string, XhsSelfSearchScopeMeta][]) {
    const before = scopes[scope];
    scopes[scope] = before
      ? {
          ...meta,
          scanned_pages: before.scanned_pages + meta.scanned_pages,
          scanned_items: before.scanned_items + meta.scanned_items,
          matched: before.matched + meta.matched,
        }
      : meta;
  }

  return {
    ...next,
    items,
    total_matched: items.length,
    scopes,
  };
}

function Metric({ icon, value, title }: { icon: ReactNode; value: number; title: string }) {
  const p = useThemePalette();
  return (
    <Space size={4} title={title} style={{ fontSize: 12, color: p.textTertiary }}>
      {icon}
      <span>{formatMetric(value)}</span>
    </Space>
  );
}

/**
 * 表格数字单元格：**缺值渲染 `—`，不渲染 0**（硬约束⑤「读不到 ≠ 读到 0」）。
 *
 * 无流量候选的三个指标（阅读量 / 支付订单 / 支付金额）都来自千帆同步的
 * `entry_json`，未同步的笔记根本没有这个键 —— 显示 0 会让用户以为「真的没人看」，
 * 进而把一篇其实没数据的笔记删掉。
 */
function numberCell(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : String(value);
}

/**
 * 笔记维度的**处置结果** → 展示文案与配色（用户口径「主体应是笔记维度」）。
 *
 * ⚠️ 五值来自后端常量 `CLEANUP_ITEM_RESULTS`，**前端不另造一套名字**：
 * 认不出的取值**原样显示**（不翻译、不猜 —— 硬约束⑤「缺值不造假」的同款纪律）。
 * 特别是 `not_attempted`（本批被风控中止、这篇没轮到）与 `failed`（打了但失败）
 * 必须分得开 —— 用户看到「未执行」才知道该重试，看到「失败」才知道该查原因。
 */
function cleanupItemResult(result: string): { text: string; color: string } {
  switch (result) {
    case "deleted":
      return { text: "已删除", color: "green" };
    case "skipped":
      return { text: "已跳过", color: "orange" };
    case "failed":
      return { text: "失败", color: "red" };
    case "not_in_pool":
      return { text: "不在候选池", color: "default" };
    case "not_attempted":
      return { text: "未执行", color: "default" };
    default:
      return { text: result || "—", color: "default" };
  }
}

export function XhsMyNotesPage() {
  const p = useThemePalette();
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径：
  // 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）—— `App.useApp()`
  // 替代静态 `message` 导入，实例继承 ConfigProvider 的主题与 locale。
  const { message } = App.useApp();
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  // 2026-10-06：三平台登录态的**探活结果**（L3，覆盖列表自带的 L1/L2「未检测」）。
  // 进入页面 / 刷新时自动探一次（账号矩阵页同款），失败静默保持「未检测」。
  const [loginChannels, setLoginChannels] = useState<Record<number, XhsLoginChannels>>({});
  const loginProbeInFlight = useRef(false);
  // 账号选择 + 页签 + 互动分类**落本地缓存**（2026-09-26 用户口径「筛选条件和账号
  // 选择后放本地缓存，避免每次均需要重新选择」）：`usePersistedFilter` 与 `useState`
  // 同形，初值优先取缓存、变化即回写。账号经 `asFiniteNumber` 校验（只能是数字或
  // `null`），页签 / 互动分类走 `asEnum`（取值集合就是界面上那两组下拉的集合）。
  const [selectedAccountId, setSelectedAccountId] = usePersistedFilter<number | null>(
    FILTER_CACHE_KEYS.myNotes.accountId, null, asFiniteNumber
  );
  const [identity, setIdentity] = useState<XhsSelfIdentity | null>(null);
  const [identityError, setIdentityError] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<XhsSelfCapabilities | null>(null);
  const [qianfanBinding, setQianfanBinding] = useState<XhsQianfanBinding | null>(null);
  // 每次加载「我发布的」都会带一份**新鲜**的 `goods_source`（含按需补齐后的计数）。
  // 覆盖率显示必须用它 —— `capabilities` 只在绑/同步/解绑/巡检后刷新，
  // 拿它显示会把「浏览时补齐的行」漏掉（2026-09-19 用户实测报出的 bug）。
  const [goodsSource, setGoodsSource] = useState<XhsGoodsSource | null>(null);
  const [isLoadingAccounts, setIsLoadingAccounts] = useState(true);
  const [isLoadingIdentity, setIsLoadingIdentity] = useState(false);

  const [activeTab, setActiveTab] = usePersistedFilter<TabKey>(
    FILTER_CACHE_KEYS.myNotes.tab, "notes", asEnum(MY_NOTES_TAB_VALUES)
  );
  const [profileUrl, setProfileUrl] = useState("");
  const [needsToken, setNeedsToken] = useState(false);

  const [notePanels, setNotePanels] = useState<Record<XhsSelfContentScope, PanelState<XhsSearchNote>>>({
    notes: emptyPanel<XhsSearchNote>(),
    liked: emptyPanel<XhsSearchNote>(),
    collected: emptyPanel<XhsSearchNote>(),
  });
  // 2026-10-06 需求：商业/普通笔记两个**离线**页签（走本地库，零平台请求）。
  const [offlinePanels, setOfflinePanels] = useState<Record<"goods" | "normal", PanelState<XhsSearchNote>>>({
    goods: emptyPanel<XhsSearchNote>(),
    normal: emptyPanel<XhsSearchNote>(),
  });
  // 导入离线笔记数据文件（xlsx）的弹窗与上传状态。
  const [importOpen, setImportOpen] = useState(false);
  const [importCategory, setImportCategory] = useState<"goods" | "normal">("goods");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importUploading, setImportUploading] = useState(false);
  // 2026-10-06：导入改走任务日志入口后，「文件解析执行中」状态来自后台日志
  // （不是上传瞬间）。记录正在解析的 category，按钮据此显示执行中 + 轮询日志收口。
  const [importingCategory, setImportingCategory] = useState<"goods" | "normal" | null>(null);
  // 2026-10-06：复选框批量删除 —— 选中集（按 category 分开）、删除弹窗、搜索/商品ID。
  const [offlineSelected, setOfflineSelected] = useState<Record<"goods" | "normal", Set<string>>>({
    goods: new Set(),
    normal: new Set(),
  });
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [batchDeleteCategory, setBatchDeleteCategory] = useState<"goods" | "normal">("goods");
  const [batchDeleteMode, setBatchDeleteMode] = useState<"local" | "platform">("local");
  const [batchDeleting, setBatchDeleting] = useState(false);
  const [offlineKeyword, setOfflineKeyword] = useState<Record<"goods" | "normal", string>>({
    goods: "",
    normal: "",
  });
  const [offlineItemId, setOfflineItemId] = useState("");
  // 2026-10-06：手动同步日期范围（弹窗内 RangePicker，≤90天）+ 同步中状态（按 category）。
  const [syncDateRange, setSyncDateRange] = useState<[string, string] | null>(null);
  const [syncingCategory, setSyncingCategory] = useState<"goods" | "normal" | null>(null);
  const [syncOpen, setSyncOpen] = useState(false);
  const [syncCategory, setSyncCategory] = useState<"goods" | "normal">("goods");
  const [syncSubmitting, setSyncSubmitting] = useState(false);
  const [interactionKind, setInteractionKind] = usePersistedFilter<XhsInteractionKind>(
    FILTER_CACHE_KEYS.myNotes.interactionKind, "like_collect", asEnum(INTERACTION_KIND_VALUES)
  );
  const [interactionPanel, setInteractionPanel] = useState<PanelState<XhsInteractionMessage>>(
    emptyPanel<XhsInteractionMessage>(),
  );

  // Search state (R8 / design doc §22). Deliberately separate from the tab
  // panels: a result set spans all three scopes and carries its own metadata
  // (how many pages were scanned per scope), which a tab panel cannot express.
  const [searchKeyword, setSearchKeyword] = useState("");
  const [searchResult, setSearchResult] = useState<XhsSelfSearchResponse | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  // Guards for scroll-triggered paging (R7). Refs, not state: the observer
  // callback must read the *current* values without being re-created on every
  // render (a re-created observer is how duplicate page requests happen).
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const pagingRef = useRef({ loading: false, hasMore: false, blocked: false });

  const [selectedNote, setSelectedNote] = useState<XhsSearchNote | null>(null);
  const [detailNote, setDetailNote] = useState<XhsSearchNote | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  // 2026-10-06：离线笔记（商业/普通）详情弹窗的指标（`raw_json.metrics` 全面展示）。
  const [offlineMetrics, setOfflineMetrics] = useState<Record<string, number> | null>(null);
  // 详情/评论失败的承载：`text` 是给界面的**一行短文案**，`full` 是后端原话（Tooltip 里看）。
  // v1.27 之前详情失败只弹 toast、`detailError` 永远是 null —— 于是 `hasFullMetrics`
  // 的 `!detailError` 一直是真，作者当年写的那道闸其实是**死的**；现在它真正生效。
  const [detailError, setDetailError] = useState<QuietError | null>(null);
  const [comments, setComments] = useState<NoteComment[]>([]);
  const [commentError, setCommentError] = useState<QuietError | null>(null);
  const [isLoadingComments, setIsLoadingComments] = useState(false);

  // 千帆（ark）商品接线：同步中状态。
  // 千帆可用性由 `capabilities.goods` 承载（店铺名 / 总数 / 已同步数 / 同步时间都在那里），
  // 避免同一事实有两个来源。⚠️ 2026-09-30 ADR-021：绑店弹窗与 `bind*` 状态已随
  // `POST /xhs/qianfan/bind` 一并退场（千帆走账号矩阵扫码登录，不再粘贴 Cookie）。
  const [checkingHealth, setCheckingHealth] = useState(false);

  // Both account types are offered: a creator account can still show its own
  // published notes. Which tabs are enabled is decided by `capabilities`, not
  // by filtering the list (design doc §11.2).
  const xhsAccounts = useMemo(
    () => accounts.filter((account) => account.platform === "xhs"),
    [accounts],
  );
  const accountOptions = useMemo(
    () =>
      xhsAccounts.map((account) => ({
        value: account.id,
        // 2026-09-24（用户口径）：下拉**只展示账号名称** —— 去掉类型（创作者 / PC）
        // 与状态后缀。账号名本身就是用户在界面上认账号的唯一依据；类型是内部维度
        // （`sub_type` 只是协议链路），状态该由页面上的登录态 tag 回答，塞进下拉
        // 只会让一行里三个信息互相挤。
        label: account.nickname || `账号 ${account.id}`,
      })),
    [xhsAccounts],
  );

  // 2026-10-06：当前选中账号的三平台登录态。探活结果（L3）优先，回退列表自带（L1/L2）。
  // 「小红书号」后面那三个 tag 的数据源 —— 账号矩阵页同款（lib/login-state.ts 唯一实现）。
  const currentAccountLoginChannels = useMemo(
    () =>
      loginChannels[selectedAccountId ?? -1] ||
      accounts.find((a) => a.id === selectedAccountId)?.login_channels ||
      {},
    [loginChannels, accounts, selectedAccountId],
  );

  // 2026-10-06：进页面 / 刷新自动探一次三平台登录态（L3，账号矩阵页同款）。
  // 失败静默 —— 保持列表自带的「未检测」，不编「已失效」（§18.5 红线）。
  async function autoCheckLoginStates() {
    if (loginProbeInFlight.current) return;
    loginProbeInFlight.current = true;
    try {
      const payload = await fetchXhsAccountLoginStates({ probe: true });
      const next: Record<number, XhsLoginChannels> = {};
      for (const item of payload.accounts) {
        next[item.platform_account_id] = item.channels;
      }
      setLoginChannels(next);
    } catch {
      // 探活没结论就保持现状（「未检测」），刷新可再探一次。
    } finally {
      loginProbeInFlight.current = false;
    }
  }

  // Guards against a stale response from a previously selected account
  // overwriting the current account's data.
  const requestSeq = useRef(0);

  useEffect(() => {
    void (async () => {
      setIsLoadingAccounts(true);
      try {
        const loaded = await fetchAccounts("xhs");
        setAccounts(loaded);
        const first = loaded.find((account) => account.platform === "xhs");
        // 账号选择的**缓存自愈**（2026-09-26）：缓存里的账号可能已被删除 —— 它不在
        // 候选里时退回第一个账号（否则页面会带着一个下拉里根本找不到的 id 去请求，
        // 用户只看到「没数据」，看不出是缓存留下的旧账号）。
        setSelectedAccountId((current) => {
          if (current !== null && loaded.some((account) => account.id === current)) {
            return current;
          }
          return first?.id ?? null;
        });
        // 进页面自动探一次三平台登录态（L3）：不 await，探活要几秒，不能卡页面 loading。
        void autoCheckLoginStates();
      } catch {
        setIdentityError("账号列表加载失败。");
      } finally {
        setIsLoadingAccounts(false);
      }
    })();
  }, []);

  // Identity and capabilities are both prerequisites for the tabs, so they are
  // fetched once per account rather than per tab. Capabilities come from a
  // purely local endpoint, so they still answer when identity fails.
  useEffect(() => {
    if (!selectedAccountId) {
      setIdentity(null);
      setCapabilities(null);
      return;
    }
    const seq = ++requestSeq.current;
    setIdentity(null);
    setCapabilities(null);
    // 上一个账号的商品来源元信息必须一起清掉（否则新账号会短暂显示旧数字）。
    setGoodsSource(null);
    setIdentityError(null);
    setNeedsToken(false);
    setProfileUrl("");
    // Search results belong to the previous account — never carry them over.
    setSearchKeyword("");
    setSearchResult(null);
    setSearchError(null);
    setNotePanels({
      notes: emptyPanel<XhsSearchNote>(),
      liked: emptyPanel<XhsSearchNote>(),
      collected: emptyPanel<XhsSearchNote>(),
    });
    setOfflinePanels({
      goods: emptyPanel<XhsSearchNote>(),
      normal: emptyPanel<XhsSearchNote>(),
    });
    setInteractionPanel(emptyPanel<XhsInteractionMessage>());
    setIsLoadingIdentity(true);
    void (async () => {
      try {
        const loaded = await fetchXhsSelfCapabilities(selectedAccountId);
        if (seq === requestSeq.current) setCapabilities(loaded);
      } catch {
        // Not fatal: the backend stays authoritative for which link answers,
        // so leave the tabs enabled and let the real error surface in the tab
        // itself rather than duplicating the routing rule on the client.
        if (seq === requestSeq.current) setCapabilities(null);
      }
      try {
        const binding = await fetchXhsQianfanBinding(selectedAccountId);
        if (seq === requestSeq.current) setQianfanBinding(binding);
      } catch {
        // 千帆绑定详情只是补充（状态 + 失效原因）
        if (seq === requestSeq.current) setQianfanBinding(null);
      }
      try {
        const loadedIdentity = await fetchXhsSelfIdentity(selectedAccountId);
        if (seq !== requestSeq.current) return;
        setIdentity(loadedIdentity);
      } catch (error) {
        if (seq !== requestSeq.current) return;
        setIdentityError(readError(error).message);
      } finally {
        if (seq === requestSeq.current) setIsLoadingIdentity(false);
      }
    })();
  }, [selectedAccountId]);

  // ⚠️ **v1.27 起本页不再有「按能力预判页签可用性」的逻辑**（§17.8）。
  // 原 `capabilityFor(scope)` + `capabilities.capabilities[scope].available` 已被删除：
  // 三页签一律可点、一律发请求，由**取数结果**决定展示。`capabilities` 仍然要取，
  // 但它现在的用途只剩两个 —— 覆盖率行（`goods`）与会话来源标签（`session_source`）。

  /**
   * 商品来源元信息：**优先用最近一次加载笔记带回的那份**。
   *
   * `capabilities` 只在账号切换与绑/同步/解绑/巡检之后刷新，而「按需补齐」发生在
   * 每次加载笔记时 —— 若拿 capabilities 显示覆盖率，浏览时补齐的行就不体现在数字里
   * （2026-09-19 用户实测报出）。所以这里的先后顺序是有意的，不要调换。
   */
  const goodsInfo = goodsSource ?? capabilities?.capabilities.goods ?? null;
  const goodsInvalid = qianfanBinding?.status === "invalid" || goodsInfo?.status === "invalid";

  /** 绑定 / 同步千帆之后刷新能力声明（它同时是商品区的状态来源）。 */
  async function reloadCapabilities() {
    if (!selectedAccountId) return;
    // 动作之后 capabilities 就是最新的一份；丢掉旧的笔记快照，避免显示更旧的数字。
    setGoodsSource(null);
    try {
      const loaded = await fetchXhsSelfCapabilities(selectedAccountId);
      setCapabilities(loaded);
    } catch (error) {
      message.error(readError(error).message);
    }
    try {
      setQianfanBinding(await fetchXhsQianfanBinding(selectedAccountId));
    } catch {
      // 绑定详情是补充信息（状态 + 失效原因），拿不到不影响主体
      setQianfanBinding(null);
    }
  }

  /**
   * 绑定千帆店铺。
   *
   * 商品只存在于千帆（设计文档 §19），而千帆是**另一套登录态**
   * （`ark.xiaohongshu.com`，B 端统一登录）：创作者 Cookie 带过去会被判
   * 「缺失登录凭证」。所以这里要的是千帆自己的 Cookie，不是账号 Cookie。
   *
   * v1.32（设计 13 §30.2）：绑定后**不再自动全量同步挂链映射**（原
   * `handleSyncQianfanGoods(true)`，打的 `/sync-goods` 端点已移除）——
   * 映射由笔记同步 ark 源 + 本页加载时的**按需补齐**自动维护，刷新即可。
   */
  /**
   * ⚠️ **`handleBindQianfan()` 已删除**（ADR-021 / 设计 18，2026-09-30）：
   * `POST /xhs/qianfan/bind` 已退场（后端显式 `410 QIANFAN_BINDING_REMOVED`）。
   * 千帆能力现在由「**该账号自己的 `ark` 登录态**」决定 —— 要开通就去该账号的
   * **续期弹窗**（账号矩阵里，扫码为主、粘贴降级）导入千帆登录态，那是凭据操作。
   *
   * 被取代的原实现（保留备查）：
   *
   *   const binding = await bindXhsQianfanShop(selectedAccountId, cookieString);
   *   message.success(`已绑定千帆店铺：${binding.shop_name || binding.shop_user_id}`);
   */

  // ── 无流量笔记清理（写操作，全部走后台异步 + 限流 + 批次上限）──────────────
  const [noTrafficOpen, setNoTrafficOpen] = useState(false);
  const [noTrafficLoading, setNoTrafficLoading] = useState(false);
  // 阈值与查询条件也是**筛选项**（用户每次都要重填一遍的那个表单）⇒ 一起落缓存
  // （2026-09-26 口径）。`asFields` 按默认值的形状逐字段校验：类型对不上的字段回
  // 默认，多出来的键丢掉 —— 免得 `{"max_browse": "0"}` 这种脏值流进请求体。
  const [noTrafficThresholds, setNoTrafficThresholds] = usePersistedFilter<NoTrafficThresholds>(
    FILTER_CACHE_KEYS.myNotes.noTrafficThresholds,
    EMPTY_NO_TRAFFIC_THRESHOLDS,
    asFields(EMPTY_NO_TRAFFIC_THRESHOLDS)
  );
  const [noTrafficQuery, setNoTrafficQuery] = usePersistedFilter<NoTrafficQuery>(
    FILTER_CACHE_KEYS.myNotes.noTrafficQuery,
    EMPTY_NO_TRAFFIC_QUERY,
    asFields(EMPTY_NO_TRAFFIC_QUERY)
  );
  const [noTrafficCandidates, setNoTrafficCandidates] = useState<NoTrafficCandidate[]>([]);
  const [noTrafficTotal, setNoTrafficTotal] = useState(0);
  //: 候选列表当前页（从 1 起）。分页在后端做（`limit` = 每页条数）。
  const [noTrafficPage, setNoTrafficPage] = useState(1);
  //: 勾选待清理的候选笔记 id（「清理指定笔记」的入参）。
  const [noTrafficSelected, setNoTrafficSelected] = useState<string[]>([]);
  const [noTrafficPageSize, setNoTrafficPageSize] = useState(20);
  const [noTrafficCoverage, setNoTrafficCoverage] = useState<string>("");
  const [cleanupRunning, setCleanupRunning] = useState(false);
  const [cleanupStatus, setCleanupStatus] = useState<NoTrafficCleanupStatus | null>(null);
  //: 该账号**是否有清理在跑**（用户口径：「存在正在清理无流量笔记的任务时，
  //: 清理无流量按钮展示为『正在清理无流量笔记』且不可点击」）。它**独立于弹窗**：
  //: 弹窗点击后即关闭，任务态要留在页面上，所以不能靠 `noTrafficOpen` 派生。
  const [cleanupActiveTaskId, setCleanupActiveTaskId] = useState<number | null>(null);
  //: 清理日志弹窗（用户口径「新增一个查询清理无流量笔记日志详情按钮」）。
  const [cleanupLogOpen, setCleanupLogOpen] = useState(false);
  const [cleanupLogRows, setCleanupLogRows] = useState<NoTrafficCleanupLogItem[]>([]);
  const [cleanupLogTotal, setCleanupLogTotal] = useState(0);
  const [cleanupLogPage, setCleanupLogPage] = useState(1);
  const [cleanupLogLoading, setCleanupLogLoading] = useState(false);
  const cleanupTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * 预览无流量候选（纯本地查询，零平台请求）。
   *
   * ⚠️ **只换页码时不改筛选条件**：翻页、改每页条数、首次预览**共用本函数**，
   * `page` 由调用方给（改条件时必须显式回第 1 页 —— 否则在第 3 页改条件会
   * 请求新条件下的第 3 页，用户看到一张空表还以为没筛出来）。
   */
  async function handlePreviewNoTraffic(page = 1) {
    if (!selectedAccountId) return;
    setNoTrafficLoading(true);
    setNoTrafficCandidates([]);
    setCleanupStatus(null);
    try {
      const result = await fetchNoTrafficCandidates(
        selectedAccountId,
        noTrafficThresholds,
        noTrafficQuery,
        page,
        noTrafficPageSize
      );
      setNoTrafficCandidates(result.candidates);
      setNoTrafficTotal(result.total);
      // 页码以后端**回显**为准（越界请求它会回落成最后一页）：前端不自己夹一次，
      // 否则分页器显示的页与表格内容可能对不上。
      setNoTrafficPage(result.page);
      // ⚠️ 勾选**跨页保留**，这里**不清空、也不按当页过滤** —— 按当前页过滤会把上一页
      //    勾的 id 全丢掉（那是「跨页勾选」的反面）。真正「已不在候选池」的 id 由
      //    后端在清理时拦下并回 `not_in_pool`（不删、如实报数），前端不猜。
      setNoTrafficCoverage(
        `候选池 ${result.coverage.synced_notes} 条已同步映射 · ${result.coverage.reason}`
      );
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setNoTrafficLoading(false);
    }
  }

  /** 轮询后台清理任务状态，直到 done/failed。
   *
   * ⚠️ **弹窗关掉后仍继续轮询**（用户口径「点击后弹窗关闭」）—— 结算提示与
   * 「正在清理无流量笔记」按钮态都靠这条循环收敛，它不属于弹窗的生命周期。
   */
  async function pollCleanupStatus(taskId: number) {
    try {
      const status = await fetchNoTrafficCleanupStatus(taskId);
      setCleanupStatus(status);
      if (status.status === "pending" || status.status === "running") {
        setCleanupActiveTaskId(taskId);
        cleanupTimerRef.current = setTimeout(() => void pollCleanupStatus(taskId), 2000);
        return;
      }
      setCleanupRunning(false);
      setCleanupActiveTaskId(null);
      if (status.status === "done") {
        message.success(`清理完成：删除 ${status.deleted} 篇，跳过 ${status.skipped} 篇`);
        refreshActive();
        if (cleanupLogOpen) void loadCleanupLogs(1);
      } else {
        message.warning(
          `清理结束（有失败）：删除 ${status.deleted} · 跳过 ${status.skipped} · 失败 ${status.failed}`
        );
      }
    } catch (error) {
      setCleanupRunning(false);
      setCleanupActiveTaskId(null);
      message.error(readError(error).message);
    }
  }

  /** 查清理日志（日志弹窗列表用）。 */
  async function loadCleanupLogs(page = 1) {
    if (!selectedAccountId) return;
    setCleanupLogLoading(true);
    try {
      const data = await fetchNoTrafficCleanupLogs({
        accountId: selectedAccountId,
        page,
        limit: 20,
      });
      setCleanupLogRows(data.items);
      setCleanupLogTotal(data.total);
      setCleanupLogPage(data.page);
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setCleanupLogLoading(false);
    }
  }

  /** 进页面 / 切账号时探一次「有没有在跑的清理」——刷新后按钮态也能恢复。 */
  useEffect(() => {
    if (!selectedAccountId) {
      setCleanupActiveTaskId(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const active = await fetchNoTrafficCleanupActive(selectedAccountId);
        if (cancelled) return;
        setCleanupActiveTaskId(active.running ? active.task_id : null);
        // 在跑 ⇒ 接回轮询（页面刷新 / 换账号后不会丢掉那条任务）。
        if (active.running && active.task_id) void pollCleanupStatus(active.task_id);
      } catch {
        // 探测失败不打扰用户：按钮退回「清理无流量」，点了后端仍会挡住重复触发。
        if (!cancelled) setCleanupActiveTaskId(null);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountId]);

  /** 确认后开始清理：后端立即起后台线程，前端只轮询状态。 */
  async function handleStartCleanup(noteIds: string[] = []) {
    if (!selectedAccountId || !noTrafficTotal) return;
    setCleanupRunning(true);
    setCleanupStatus(null);
    try {
      // 两个入口共用本函数：`noteIds` 非空 = 清理指定笔记，空 = 清理全部候选。
      // ⚠️ 指定清理时 `batch_limit` 取**勾选数**（不是固定的 20）—— 否则用户勾了
      //    30 篇只会删掉前 20 篇，而界面写着「清理指定 30 篇」。
      const { task_id } = await startNoTrafficCleanup(
        selectedAccountId,
        noTrafficThresholds,
        noTrafficQuery,
        noteIds.length ? Math.min(noteIds.length, 50) : 20,
        noteIds
      );
      message.info(
        noteIds.length
          ? `清理任务已创建，本批 ${noteIds.length} 篇，后台执行中…`
          : "清理任务已创建，后台执行中…"
      );
      // ⚠️ 用户口径「点击后弹窗关闭」：任务已交后端，弹窗留着只会挡住页面。
      //    任务态改由**页面按钮**承担（见 `cleanupActiveTaskId` 的注释）。
      setCleanupActiveTaskId(task_id);
      setNoTrafficOpen(false);
      setNoTrafficSelected([]);
      void pollCleanupStatus(task_id);
    } catch (error) {
      setCleanupRunning(false);
      message.error(readError(error).message);
    }
  }

  useEffect(() => {
    return () => {
      if (cleanupTimerRef.current) clearTimeout(cleanupTimerRef.current);
    };
  }, []);

  /**
   * 立即巡检千帆登录态（T-31）。
   *
   * 与后台定时巡检逻辑同源；失效时后端会标 `invalid` + 发铃铛，这里只负责把结果
   * 讲给此刻正在看页面的人（成功/失效文案 + 刷新绑定与能力声明）。
   */
  async function handleCheckQianfanHealth() {
    if (!selectedAccountId) return;
    setCheckingHealth(true);
    try {
      const result = await checkXhsQianfanShopHealth(selectedAccountId);
      setQianfanBinding(result);
      if (result.status === "invalid") {
        message.warning(`千帆登录态已失效：${result.status_message || "请重新绑定千帆"}`);
      } else {
        message.success(`千帆登录态正常（巡检于 ${result.health_checked_at}）`);
      }
      await reloadCapabilities();
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setCheckingHealth(false);
    }
  }

  /**
   * ⚠️ **`handleUnbindQianfan()` 已删除**（ADR-021 / 设计 18，2026-09-30）：
   * `POST /xhs/qianfan/unbind` 随之退场（后端显式 `410`）—— 「绑定」不再存在，
   * 「解除绑定」自然也没有了。要断开千帆，就在账号矩阵把这个账号的 `ark`
   * 登录态清掉/替换（凭据操作），商品映射随账号隔离保留。
   *
   * 被取代的原实现（保留备查）：
   *
   *   const result = await unbindXhsQianfanShop(selectedAccountId);
   *   message.success(`已解除绑定，清理 ${result.removed} 条商品映射`);
   */

  /**
   * True when "我发布的" is served by the creator platform.
   *
   * Kept as a capability signal; it no longer gates the detail/comments calls.
   * The creator list carries a note-level `xsec_token`, and the backend pairs
   * it with a sibling www session of the same user, so the modal's body /
   * gallery / comments do load on this link too (2026-09-20, was believed
   * structurally unreachable — see design doc §17.2 / §18.5 revision note).
   */
  const isCreatorLink = capabilities?.notes_link === "creator";
  // `isCreatorLink` is intentionally still computed: it documents the link and
  // stays available for capability-driven UI (e.g. the search box gating).
  void isCreatorLink;

  function patchNotePanel(scope: XhsSelfContentScope, patch: Partial<PanelState<XhsSearchNote>>) {
    setNotePanels((current) => ({ ...current, [scope]: { ...current[scope], ...patch } }));
  }

  /**
   * Keep the paging guard in sync with the active panel.
   *
   * Written in an effect (not during render) so the observer callback always
   * reads committed values — this is the in-flight de-duplication that keeps one
   * scroll from firing the same page request twice (R7 / design doc §20).
   */
  useEffect(() => {
    const panel =
      activeTab === "interactions"
        ? interactionPanel
        : activeTab === "goods" || activeTab === "normal"
          ? offlinePanels[activeTab]
          : notePanels[activeTab as XhsSelfContentScope];
    pagingRef.current = {
      loading: Boolean(panel?.loading),
      hasMore: Boolean(panel?.hasMore),
      blocked: Boolean(panel?.error),
    };
  }, [activeTab, interactionPanel, notePanels, offlinePanels]);

  /**
   * Scroll-triggered paging (R7 / design doc §20).
   *
   * A sentinel element is observed instead of listening for `scroll` events:
   * `IntersectionObserver` is correct whether the window scrolls or an ancestor
   * with `overflow: auto` does (ancestor clipping is part of the intersection
   * rect), so the surrounding layout can change without touching this code.
   *
   * **One page per trigger, and no auto-retry.** Once a page fails the observer
   * stops and only the banner's 重试 button can resume — a sentinel that keeps
   * retrying is exactly how a platform hiccup becomes a request storm (§12).
   */
  useEffect(() => {
    // Search results page themselves with an explicit button, not by scrolling.
    if (searchResult) return;
    const node = sentinelRef.current;
    if (!node || typeof IntersectionObserver === "undefined") return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        const { loading, hasMore, blocked } = pagingRef.current;
        if (loading || !hasMore || blocked) return;
        if (activeTab === "interactions") {
          void loadInteractionPage(interactionPanel.cursor, true);
          return;
        }
        if (activeTab === "goods" || activeTab === "normal") {
          const panel = offlinePanels[activeTab];
          const page = Math.floor(panel.items.length / 20) + 1;
          void loadOfflinePage(activeTab, page, true);
          return;
        }
        const scope = activeTab as XhsSelfContentScope;
        void loadNotePage(scope, notePanels[scope].cursor, true);
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, searchResult, sentinelRef.current, notePanels, interactionPanel, capabilities, offlinePanels]);

  /** The only way forward after a failed page: an explicit user click (§20). */
  function retryActivePage() {
    if (activeTab === "interactions") {
      void loadInteractionPage(interactionPanel.cursor, true);
      return;
    }
    if (activeTab === "goods" || activeTab === "normal") {
      const panel = offlinePanels[activeTab];
      const page = Math.floor(panel.items.length / 20) + 1;
      void loadOfflinePage(activeTab, page, true);
      return;
    }
    const scope = activeTab as XhsSelfContentScope;
    void loadNotePage(scope, notePanels[scope].cursor, true);
  }

  /**
   * Keyword search across all three scopes (R8 / design doc §22).
   *
   * The server does a **bounded scan** (≤ `pages_per_scope` pages per scope) and
   * reports how many pages it actually read — the UI must show that, otherwise
   * 「没搜到」 reads as 「我没有这条笔记」, a conclusion nobody is entitled to.
   *
   * `append` powers 「继续搜索更早内容」: the previous cursor set is sent back so
   * the second pass continues where the first stopped instead of rescanning.
   */
  async function runSearch(keyword: string, cursors: Record<string, string> = {}, append = false) {
    const trimmed = keyword.trim();
    if (!selectedAccountId || !trimmed) return;
    const seq = requestSeq.current;
    const previous = searchResult;
    setSearchLoading(true);
    setSearchError(null);
    if (!append) setSearchKeyword(trimmed);
    try {
      const data = await fetchXhsSelfSearch({
        account_id: selectedAccountId,
        keyword: trimmed,
        cursors,
        profile_url: profileUrl.trim(),
      });
      if (seq !== requestSeq.current) return;
      setSearchResult(append && previous ? mergeSearchResults(previous, data) : data);
      if (data.identity) setIdentity(data.identity);
      setNeedsToken(false);
    } catch (error) {
      if (seq !== requestSeq.current) return;
      const { message: reason, code } = readError(error);
      setSearchError(reason);
      if (code === "XSEC_TOKEN_REQUIRED") setNeedsToken(true);
      if (!append) setSearchResult(null);
    } finally {
      if (seq === requestSeq.current) setSearchLoading(false);
    }
  }

  function clearSearch() {
    setSearchKeyword("");
    setSearchResult(null);
    setSearchError(null);
  }

  async function loadNotePage(scope: XhsSelfContentScope, cursor: string, append: boolean) {
    if (!selectedAccountId) return;
    const seq = requestSeq.current;
    patchNotePanel(scope, { loading: true, error: null });
    try {
      const payload = {
        account_id: selectedAccountId,
        cursor,
        profile_url: profileUrl.trim(),
      };
      const loader =
        scope === "notes" ? fetchXhsSelfNotes : scope === "liked" ? fetchXhsSelfLikedNotes : fetchXhsSelfCollectedNotes;
      const data = await loader(payload);
      if (seq !== requestSeq.current) return;
      setNotePanels((current) => ({
        ...current,
        [scope]: {
          items: append ? [...current[scope].items, ...data.items] : data.items,
          cursor: data.cursor,
          hasMore: data.has_more,
          loading: false,
          error: null,
          loaded: true,
        },
      }));
      setNeedsToken(false);
      if (data.identity) setIdentity(data.identity);
      // 商品来源元信息是**每次加载都最新**的（含按需补齐后的计数），
      // 覆盖率行优先用它，避免显示过期数字。
      if (scope === "notes" && data.goods_source) setGoodsSource(data.goods_source);
    } catch (error) {
      if (seq !== requestSeq.current) return;
      const { message: reason, code } = readError(error);
      // 失败信息连**错误码**一起存（v1.28）：`renderFailure` 据此三分法呈现 ——
      // 预期内的取不到走安静降级，真·接口异常走红块 + 真实原因。
      patchNotePanel(scope, { loading: false, error: quietError(reason, code), loaded: true });
      if (code === "XSEC_TOKEN_REQUIRED") setNeedsToken(true);
    }
  }

  async function loadInteractionPage(cursor: string, append: boolean) {
    if (!selectedAccountId) return;
    const seq = requestSeq.current;
    setInteractionPanel((current) => ({ ...current, loading: true, error: null }));
    try {
      const data = await fetchXhsInteractionMessages({
        account_id: selectedAccountId,
        kind: interactionKind,
        cursor,
      });
      if (seq !== requestSeq.current) return;
      setInteractionPanel((current) => ({
        items: append ? [...current.items, ...data.items] : data.items,
        cursor: data.cursor,
        hasMore: data.has_more,
        loading: false,
        error: null,
        loaded: true,
      }));
    } catch (error) {
      if (seq !== requestSeq.current) return;
      const { message: reason, code } = readError(error);
      setInteractionPanel((current) => ({
        ...current,
        loading: false,
        error: quietError(reason, code),
        loaded: true,
      }));
    }
  }

  /** 2026-10-06 需求：加载**离线**商业/普通笔记（走本地库，零平台请求）。 */
  async function loadOfflinePage(category: "goods" | "normal", page = 1, append = false) {
    if (!selectedAccountId) return;
    const seq = requestSeq.current;
    setOfflinePanels((current) => ({ ...current, [category]: { ...current[category], loading: true, error: null } }));
    try {
      const kw = offlineKeyword[category];
      const itemId = category === "goods" ? offlineItemId : "";
      const data = await fetchOfflineNotes(category, selectedAccountId, page, 20, kw, itemId);
      if (seq !== requestSeq.current) return;
      setOfflinePanels((current) => ({
        ...current,
        [category]: {
          items: (append
            ? [...current[category].items, ...(data.items as XhsSearchNote[])]
            : (data.items as XhsSearchNote[])
          ).map((item) => {
            // `_serialize_note` 的 `goods` 是**数组**（`note_goods` 多对多），与
            // XhsSearchNote 的 `goods`（NoteGoods 单数）语义不同 —— 列表行不带
            // 单数 goods，挂载商品统一由详情弹窗 hydrate 后走 `shop.goods`。
            // 2026-10-06：离线笔记的互动四数据在 `raw_json.metrics`（键 like_count/
            // collect_count/comment_count/share_count，来自千帆 xlsx）。提取到顶层
            // `likes/collects/comments/shares`，列表卡片即可渲染（缺值 0，不造假）。
            const rawJson = (item as XhsSearchNote & { raw_json?: Record<string, unknown> }).raw_json;
            const metrics = (rawJson?.metrics ?? {}) as Record<string, unknown>;
            return {
              ...item,
              goods: null,
              likes: Number(metrics.like_count ?? 0),
              collects: Number(metrics.collect_count ?? 0),
              comments: Number(metrics.comment_count ?? 0),
              shares: Number(metrics.share_count ?? 0),
            };
          }),
          cursor: "",
          hasMore: page * 20 < data.total,
          loading: false,
          error: null,
          loaded: true,
        },
      }));
    } catch (error) {
      if (seq !== requestSeq.current) return;
      const { message: reason, code } = readError(error);
      setOfflinePanels((current) => ({
        ...current,
        [category]: { ...current[category], loading: false, error: quietError(reason, code), loaded: true },
      }));
    }
  }

  /** 2026-10-06 需求：提交导入 xlsx（后端走任务日志入口异步解析，站内通知推送统计）。 */
  async function handleImportNoteData() {
    if (!selectedAccountId || !importFile) return;
    setImportUploading(true);
    try {
      await importNoteData(importFile, importCategory, selectedAccountId);
      message.success("导入任务已提交，解析完成后将通过站内消息通知");
      setImportOpen(false);
      setImportFile(null);
      // 标记「文件解析执行中」，按钮态据此显示，轮询日志收口后自动恢复。
      setImportingCategory(importCategory);
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setImportUploading(false);
    }
  }

  // 2026-10-06：轮询任务日志，收口「文件解析执行中」按钮态 —— 后台解析结束后
  // （`system.note_data_sync` 下没有 running 的「离线笔记数据导入」日志）清空标记。
  useEffect(() => {
    if (!importingCategory || !selectedAccountId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const logs = await fetchScheduleLogs({
          job_key: "system.note_data_sync",
          status: "running",
          page_size: 50,
        });
        const importing = (logs.items ?? []).some(
          (log) => (log.job_name || "") === "离线笔记数据导入",
        );
        if (!cancelled && !importing) setImportingCategory(null);
      } catch {
        // 查询失败不打断轮询，等下一轮再收口（不因一次查询失败就误判「已结束」）。
      }
    };
    const timer = setInterval(() => void poll(), 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [importingCategory, selectedAccountId]);

  // 2026-10-06：复选框批量删除（只删本地 / 删本地+平台），完成后清选中并刷新列表。
  async function handleBatchDelete() {
    const ids = Array.from(offlineSelected[batchDeleteCategory]);
    if (!ids.length) return;
    setBatchDeleting(true);
    try {
      const result = await batchDeleteNotes(ids, batchDeleteMode === "platform");
      if (result.failed.length) {
        message.warning(`已删除 ${result.deleted} 篇，${result.failed.length} 篇失败（详见原因）`);
      } else {
        message.success(`已删除 ${result.deleted} 篇笔记`);
      }
      setBatchDeleteOpen(false);
      // 清空该 category 的选中集，并刷新列表。
      setOfflineSelected((current) => ({ ...current, [batchDeleteCategory]: new Set() }));
      setOfflinePanels((current) => ({
        ...current,
        [batchDeleteCategory]: emptyPanel<XhsSearchNote>(),
      }));
      void loadOfflinePage(batchDeleteCategory, 1, false);
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setBatchDeleting(false);
    }
  }

  // 切换勾选（复选框）。
  function toggleOfflineSelect(category: "goods" | "normal", noteId: string) {
    setOfflineSelected((current) => {
      const next = new Set(current[category]);
      if (next.has(noteId)) next.delete(noteId);
      else next.add(noteId);
      return { ...current, [category]: next };
    });
  }

  // 2026-10-06：手动同步离线笔记数据（弹窗内选日期范围，≤90天），后台执行 + 轮询日志收口。
  async function handleSyncNoteData(category: "goods" | "normal") {
    if (!selectedAccountId) return;
    setSyncSubmitting(true);
    setSyncingCategory(category);
    try {
      await syncNoteData(
        selectedAccountId,
        category,
        syncDateRange?.[0],
        syncDateRange?.[1],
      );
      message.success("同步任务已提交，后台执行中…");
      // 关闭弹窗 + 清空日期选择，等下次同步重新选。
      setSyncOpen(false);
      setSyncDateRange(null);
      // 收口「同步中」按钮态：轮询 system.note_data_sync 下 running 的「离线笔记数据同步」日志。
      const poll = async () => {
        try {
          const logs = await fetchScheduleLogs({
            job_key: "system.note_data_sync",
            status: "running",
            page_size: 50,
          });
          const syncing = (logs.items ?? []).some(
            (log) => (log.job_name || "") === "离线笔记数据同步",
          );
          if (!syncing) {
            setSyncingCategory(null);
            // 同步完成，刷新列表。
            setOfflinePanels((current) => ({ ...current, [category]: emptyPanel<XhsSearchNote>() }));
            void loadOfflinePage(category, 1, false);
          } else {
            setTimeout(() => void poll(), 3000);
          }
        } catch {
          setTimeout(() => void poll(), 3000);
        }
      };
      setTimeout(() => void poll(), 3000);
    } catch (error) {
      setSyncingCategory(null);
      message.error(readError(error).message);
    } finally {
      setSyncSubmitting(false);
    }
  }

  // Lazy load: a tab only issues its first request once activated, and switching
  // back does not refetch. This is the main rate-limit guard (design doc §12).
  //
  // v1.27 (§17.8): **no availability pre-check here any more.** The old
  // `if (!capabilityFor(activeTab).available) return;` is what made a reachable
  // tab silently do nothing; now the request always goes out and the response
  // decides what is rendered.
  useEffect(() => {
    if (!selectedAccountId) return;
    if (activeTab === "interactions") {
      if (!interactionPanel.loaded && !interactionPanel.loading) void loadInteractionPage("", false);
      return;
    }
    if (activeTab === "goods" || activeTab === "normal") {
      const panel = offlinePanels[activeTab];
      if (!panel.loaded && !panel.loading) void loadOfflinePage(activeTab, 1, false);
      return;
    }
    const panel = notePanels[activeTab];
    if (!panel.loaded && !panel.loading) void loadNotePage(activeTab, "", false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, selectedAccountId, interactionKind, capabilities]);

  // Interaction kind is a sub-filter of the same tab, so changing it resets the list.
  useEffect(() => {
    if (activeTab !== "interactions") return;
    if (!selectedAccountId) return;
    setInteractionPanel(emptyPanel<XhsInteractionMessage>());
    void loadInteractionPage("", false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactionKind]);

  /**
   * Open the detail modal and pull the full note on demand.
   *
   * The list only carries a thin object, so the body text, the gallery and the
   * video can only come from the detail endpoint. One call per explicit open —
   * no prefetching, no per-card backfill (design doc §11.4 / D6 / D7).
   */
  /**
   * Re-fetch the detail of the note currently in the dialog, by note_id.
   *
   * Used by the title-bar 刷新 button and by the open flow. Keeps the
   * sequence guard so a stale fetch cannot overwrite a newer one.
   * On failure the dialog keeps its existing content and shows a short
   * message — never the long web_session explanation (that text was about
   * liked/collected/interactions and made no sense here).
   */
  async function refreshNoteDetail() {
    const note = selectedNote;
    if (!selectedAccountId || !note) return;

    // 2026-10-06：商业/普通（离线）笔记的「刷新」必须走 hydrate（落库详情），
    // 不能走线上 `fetchXhsNoteDetail`（www feed）—— 离线笔记没有 web_session，
    // 线上详情拿不到正文/媒体，`mergeNoteDetail` 会用空值覆盖已拉取的正文/媒体。
    if (activeTab === "goods" || activeTab === "normal") {
      const seq = requestSeq.current;
      setDetailError(null);
      setDetailLoading(true);
      try {
        const hydrated = await hydrateOfflineNoteDetail(note.note_id);
        if (seq !== requestSeq.current) return;
        applyHydratedOfflineNote(note, hydrated);
      } catch (error) {
        if (seq !== requestSeq.current) return;
        const { message: reason, code } = readError(error);
        setDetailError(quietError(reason, code));
      } finally {
        if (seq === requestSeq.current) setDetailLoading(false);
      }
      return;
    }

    const url = noteExternalUrl(note);
    if (!url) {
      message.warning("缺少笔记链接，无法加载详情。");
      return;
    }
    const seq = requestSeq.current;
    setDetailError(null);
    setDetailLoading(true);
    try {
      const detail = await fetchXhsNoteDetail({ account_id: selectedAccountId, url });
      if (seq !== requestSeq.current) return;
      setDetailNote(mergeNoteDetail(note, detail, url));
    } catch (error) {
      if (seq !== requestSeq.current) return;
      // 静默：详情拿不到就退回列表那份 thin 项 + 一行说明（v1.27）。
      // 原来在这里弹 `message.error`，把一句英文原文甩给用户（"Account has no cookies"）。
      const { message: reason, code } = readError(error);
      setDetailError(quietError(reason, code));
    } finally {
      if (seq === requestSeq.current) setDetailLoading(false);
    }
  }

  async function openNote(note: XhsSearchNote) {
    setSelectedNote(note);
    setDetailNote(note);
    setDetailError(null);
    setComments([]);
    setCommentError(null);

    const url = noteExternalUrl(note);
    if (!selectedAccountId || !url) {
      if (!url) message.warning("缺少笔记链接，无法加载详情与评论。");
      return;
    }

    // Creator link: the account itself has no web_session, but the list entry
    // carries a note-level xsec_token. The backend resolves detail/comments
    // through a sibling www session of the same platform identity, so these
    // calls do work — do NOT skip them (design doc §17.2 / §18.5, revised).
    const seq = requestSeq.current;
    setDetailLoading(true);
    setIsLoadingComments(true);
    try {
      const detail = await fetchXhsNoteDetail({ account_id: selectedAccountId, url });
      if (seq !== requestSeq.current) return;
      setDetailNote(mergeNoteDetail(note, detail, url));
    } catch (error) {
      if (seq !== requestSeq.current) return;
      // 静默（v1.27）：失败不弹 toast —— 弹窗保持列表数据，说明放正文区一行。
      const { message: reason, code } = readError(error);
      setDetailError(quietError(reason, code));
    } finally {
      if (seq === requestSeq.current) setDetailLoading(false);
    }

    try {
      const page = await fetchXhsNoteComments({ account_id: selectedAccountId, note_url: url });
      if (seq !== requestSeq.current) return;
      setComments(page.items);
    } catch (error) {
      if (seq !== requestSeq.current) return;
      const { message: reason, code } = readError(error);
      setCommentError(quietError(reason, code));
    } finally {
      if (seq === requestSeq.current) setIsLoadingComments(false);
    }
  }

  /**
   * 2026-10-06：商业/普通笔记（离线数据）点击卡片 —— 实时拉详情**并落库**。
   *
   * 与 `openNote`（全部笔记 tab，线上不落库）的关键区别：
   * 详情走 `POST /notes/{note_id}/hydrate`（后端匿名 SSR 拉平台详情 → 落库正文/
   * 媒体/话题拆分），返回落库后的完整笔记。评论仍复用「全部笔记」的线上端点
   * （`fetchXhsNoteComments`，实时展示不落库）。
   */
  /**
   * 把 hydrate 端点返回的离线笔记详情映射成 XhsSearchNote 并写入 detailNote。
   *
   * 2026-10-06：`openOfflineNote`（打开）与 `refreshNoteDetail`（刷新）共用的**唯一**
   * 映射实现 —— 挂载商品转 NoteGoods 契约、指标单独存、发布时间映射等只在这里写一遍
   * （DEF-044：一个语义一份实现）。
   */
  function applyHydratedOfflineNote(note: XhsSearchNote, hydrated: Awaited<ReturnType<typeof hydrateOfflineNoteDetail>>) {
    // 落库后的指标（`raw_json.metrics`）单独存，详情弹窗「离线指标」区块全面展示。
    const metrics = (hydrated.raw_json?.metrics ?? {}) as Record<string, number>;
    setOfflineMetrics(Object.keys(metrics).length ? metrics : null);
    // 挂载商品：离线笔记走 `note_goods` join `shop_products`，转成 `NoteGoods` 契约
    // （与「全部笔记」tab 的挂载商品**同一形状** —— `mountGoods` 的 goods 分支渲染，
    // 第三行含店铺名 + 商品 ID）。取第一件（离线数据一篇通常挂一件；多件时首件展示，
    // 与全部笔记 tab 的单数 NoteGoods 口径一致）。
    // price 是「分」字符串（如 "4852~7652" 区间 / "8660" 单值），换算成元（同后端 `_price_text`）。
    const firstGood = hydrated.goods?.[0];
    const priceInFen = firstGood?.price ?? "";
    const priceInYuan = (() => {
      const parts = priceInFen.split("~").map((s) => s.trim()).filter(Boolean);
      const nums = parts.map((p) => Number(p)).filter((n) => Number.isFinite(n));
      if (nums.length === 0) return "";
      const yuan = nums.map((n) => (n / 100).toFixed(2));
      return yuan.length === 1 ? yuan[0] : yuan.join("~");
    })();
    const offlineGoods: NoteGoods | null = firstGood?.item_id
      ? {
          goods_id: firstGood.item_id,
          title: firstGood.item_title || "",
          price: priceInYuan,
          image: firstGood.images?.[0] ?? "",
          url: "",
          shop_name: hydrated.account_name || "",
          variant: firstGood.variant || "",
        }
      : null;
    const merged: XhsSearchNote = {
      ...note,
      note_id: hydrated.note_id || note.note_id,
      title: hydrated.title || note.title,
      content: hydrated.content || note.content,
      author_name: hydrated.author_name || note.author_name,
      cover_url: hydrated.cover_url || note.cover_url,
      // 挂载商品走 `NoteGoods` 单数契约（与全部笔记 tab 一致）；离线列表行的 `goods`
      // 是 `_serialize_note` 的**数组**，透传会被 `mountGoods` 读成空壳卡片，故显式覆盖。
      goods: offlineGoods,
      image_urls: hydrated.asset_urls && hydrated.asset_urls.length ? hydrated.asset_urls : note.image_urls,
      video_url: hydrated.video_url || note.video_url,
      tags: hydrated.topics ? hydrated.topics.map((t) => t.name) : note.tags,
      likes: metrics.like_count ?? note.likes,
      collects: metrics.collect_count ?? note.collects,
      comments: metrics.comment_count ?? note.comments,
      shares: metrics.share_count ?? note.shares,
      // 「在小红书打开」链接：hydrate 回查后 `raw_json.note_url` 带**新鲜** xsec_token
      // （按 note_id 回查，见后端 hydrate 端点）。旧 token / 无 token 的 explore 链接
      // 会被平台 302 → 404（error_code=300031「当前笔记暂时无法浏览」）。
      note_url: (hydrated.raw_json?.note_url as string | undefined) || note.note_url,
      // 发布时间：离线笔记在 `raw_json.time`（字符串，如 "2026-08-13 09:22"），
      // 映射到 `timestamp` 让「发布时间」那一项能展示（`formatRemoteTime` 原样透传字符串）。
      timestamp: (hydrated.raw_json?.time as string | number | undefined) ?? note.timestamp,
    };
    setDetailNote(merged);
  }

  async function openOfflineNote(note: XhsSearchNote) {
    setSelectedNote(note);
    setDetailNote(note);
    setDetailError(null);
    setComments([]);
    setCommentError(null);
    setOfflineMetrics(null);

    const seq = requestSeq.current;
    setDetailLoading(true);
    try {
      const hydrated = await hydrateOfflineNoteDetail(note.note_id);
      if (seq !== requestSeq.current) return;
      applyHydratedOfflineNote(note, hydrated);
    } catch (error) {
      if (seq !== requestSeq.current) return;
      const { message: reason, code } = readError(error);
      setDetailError(quietError(reason, code));
    } finally {
      if (seq === requestSeq.current) setDetailLoading(false);
    }

    // 评论：离线笔记的 note_url 在 raw_json 里（带 xsec_token），复用线上评论端点。
    const url = noteExternalUrl(note);
    if (url && selectedAccountId) {
      setIsLoadingComments(true);
      try {
        const page = await fetchXhsNoteComments({ account_id: selectedAccountId, note_url: url });
        if (seq !== requestSeq.current) return;
        setComments(page.items);
      } catch (error) {
        if (seq !== requestSeq.current) return;
        const { message: reason, code } = readError(error);
        setCommentError(quietError(reason, code));
      } finally {
        if (seq === requestSeq.current) setIsLoadingComments(false);
      }
    }
  }

  function closeNote() {
    setSelectedNote(null);
    setDetailNote(null);
    setDetailError(null);
    setComments([]);
    setCommentError(null);
    setOfflineMetrics(null);
  }

  function refreshActive() {
    // 刷新同时重探三平台登录态（L3），登录态跟着页面一起刷新。
    void autoCheckLoginStates();
    if (activeTab === "interactions") {
      setInteractionPanel(emptyPanel<XhsInteractionMessage>());
      void loadInteractionPage("", false);
      return;
    }
    if (activeTab === "goods" || activeTab === "normal") {
      setOfflinePanels((current) => ({ ...current, [activeTab]: emptyPanel<XhsSearchNote>() }));
      void loadOfflinePage(activeTab, 1, false);
      return;
    }
    patchNotePanel(activeTab, emptyPanel<XhsSearchNote>());
    void loadNotePage(activeTab, "", false);
  }

  const renderNoteGrid = (
    panel: PanelState<XhsSearchNote>,
    onCardClick?: (note: XhsSearchNote) => void,
    selection?: {
      selectedIds: Set<string>;
      onToggle: (noteId: string) => void;
    },
    isOffline = false,
  ) => {
    if (panel.loading && panel.items.length === 0) {
      return (
        <div style={{ padding: "48px 0", textAlign: "center" }}>
          <Spin indicator={<LoadingOutlined spin />} /> <Text type="secondary">加载中…</Text>
        </div>
      );
    }
    if (panel.error && panel.items.length === 0) {
      // 安静降级（v1.27 §17.8）：`panel.error` 已经是 25–30 字的短文案，
      // 这里不再摆「加载失败」红块 —— 页签不置灰之后，失败的表现就是「这里没内容」。
      return renderFailure(panel.error);
    }
    if (panel.items.length === 0) {
      return <Empty description="没有数据（可能是该账号在此分类下确实为空）" />;
    }
    return (
      <>
        <Row gutter={[16, 16]}>
          {panel.items.map((note, index) => (
            <Col key={`${note.note_id || "note"}-${index}`} xs={24} sm={12} lg={8} xxl={6}>
              <Card
                hoverable
                size="small"
                onClick={() => void (onCardClick ?? openNote)(note)}
                cover={
                  note.cover_url ? (
                    <div style={{ position: "relative", height: 180, overflow: "hidden", background: p.mediaBg }}>
                      <img
                        src={note.cover_url}
                        alt={note.title || note.note_id}
                        style={{ width: "100%", height: "100%", objectFit: "cover" }}
                      />
                      {selection && note.note_id && (
                        <Checkbox
                          checked={selection.selectedIds.has(note.note_id)}
                          onClick={(e) => e.stopPropagation()}
                          onChange={() => selection.onToggle(note.note_id)}
                          style={{ position: "absolute", left: 8, top: 8 }}
                        />
                      )}
                      {noteKindLabel(note) === "视频" && (
                        <PlayCircleOutlined
                          style={{ position: "absolute", right: 8, bottom: 8, fontSize: 24, color: "#fff" }}
                        />
                      )}
                    </div>
                  ) : (
                    <div style={{ height: 180, display: "flex", alignItems: "center", justifyContent: "center", background: p.mediaBg, position: "relative" }}>
                      {selection && note.note_id && (
                        <Checkbox
                          checked={selection.selectedIds.has(note.note_id)}
                          onClick={(e) => e.stopPropagation()}
                          onChange={() => selection.onToggle(note.note_id)}
                          style={{ position: "absolute", left: 8, top: 8 }}
                        />
                      )}
                      <PictureOutlined style={{ fontSize: 28, color: p.fillIcon }} />
                    </div>
                  )
                }
              >
                <Card.Meta
                  title={<span style={{ fontSize: 13 }}>{note.title || "（无标题）"}</span>}
                  description={
                    <div>
                      {!isOffline && (
                        <Space size={12} wrap style={{ marginBottom: 6 }}>
                          {/*
                            Only the like count is rendered. The list payload's
                            interact_info carries {liked, liked_count, sticky} and
                            nothing else, so showing 收藏/评论/分享 here would
                            print a hard 0 for data that simply was not sent —
                            an outright lie to an operator. The full four appear
                            in the modal, where the detail response supplies them.
                            (design doc §18.2 / §18.3)
                          */}
                          <Metric icon={<HeartOutlined />} value={note.likes} title="点赞" />
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            其余互动数据点开后可见
                          </Text>
                        </Space>
                      )}
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                        <Space size={6} wrap>
                          {isOffline ? (
                            <>
                              {/* 2026-10-06：离线（商业/普通）笔记底部一行展示四互动数据 + 图文 tag。 */}
                              <Metric icon={<HeartOutlined style={{ color: "#ff4d4f" }} />} value={note.likes} title="点赞" />
                              <Metric icon={<StarOutlined style={{ color: "#faad14" }} />} value={note.collects} title="收藏" />
                              <Metric icon={<CommentOutlined style={{ color: "#1677ff" }} />} value={note.comments} title="评论" />
                              <Metric icon={<ShareAltOutlined style={{ color: "#52c41a" }} />} value={note.shares} title="分享" />
                            </>
                          ) : null}
                          <Tag color={noteKindLabel(note) === "视频" ? "purple" : "blue"} style={{ marginInlineEnd: 0 }}>
                            {noteKindLabel(note)}
                          </Tag>
                          {note.goods && <Tag color="gold" style={{ marginInlineEnd: 0 }}>挂商品</Tag>}
                        </Space>
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          {formatRemoteTime(note.timestamp)}
                        </Text>
                      </div>
                    </div>
                  }
                />
              </Card>
            </Col>
          ))}
        </Row>
        {renderPagingFooter(panel)}
      </>
    );
  };

  /**
   * The paging footer for every long list (R7 / design doc §20).
   *
   * There is no explicit paging control any more: the next page arrives when the
   * sentinel below scrolls into view (see the IntersectionObserver effect). The
   * observer stops on failure, so the banner's 重试 is the only way to resume —
   * never an automatic retry loop.
   */
  const renderPagingFooter = (panel: PanelState<unknown>) => {
    if (panel.error && panel.items.length > 0) {
      return (
        <div style={{ textAlign: "center", marginTop: 20 }}>
          <Alert
            type="warning"
            showIcon
            message="加载下一页失败（已停止自动加载）"
            description={panel.error.full}
            style={{ marginBottom: 12, textAlign: "left" }}
          />
          <Button onClick={retryActivePage} loading={panel.loading}>
            重试
          </Button>
        </div>
      );
    }
    return (
      <div style={{ textAlign: "center", marginTop: 20 }}>
        {panel.loading ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            <Spin size="small" indicator={<LoadingOutlined spin />} /> 正在加载下一页…
          </Text>
        ) : panel.hasMore ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            继续下滑加载后续笔记
          </Text>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>
            已全部加载
          </Text>
        )}
        {/* 滚动哨兵：进入视口（提前 240px）即拉下一页 */}
        <div ref={sentinelRef} style={{ height: 1 }} />
      </div>
    );
  };

  /** 2026-10-06 需求：商业/普通笔记离线页签的渲染（导入按钮 + 清理 + 刷新 + 卡片列表）。 */
  const renderOfflinePanel = (category: "goods" | "normal") => {
    const panel = offlinePanels[category];
    const catLabel = category === "goods" ? "商业笔记" : "普通笔记";
    return (
      <div>
        <Space wrap size={8} style={{ marginBottom: 16 }}>
          {/* 2026-10-06：手动同步 —— 点击弹窗内选日期范围（≤90 天）。 */}
          <Button
            icon={<SyncOutlined />}
            loading={syncingCategory === category}
            onClick={() => {
              setSyncCategory(category);
              setSyncOpen(true);
            }}
          >
            {syncingCategory === category ? "同步中" : "同步"}
          </Button>
          <Button
            icon={<UploadOutlined />}
            loading={importingCategory === category}
            onClick={() => {
              setImportCategory(category);
              setImportOpen(true);
              setImportFile(null);
            }}
          >
            {importingCategory === category ? "文件解析执行中" : "导入数据文件"}
          </Button>
          {/* 2026-10-06 需求 7：无流量清理迁移到商业/普通 tab，按 category 过滤。 */}
          <Tooltip title={cleanupActiveTaskId ? "正在清理无流量笔记，完成后可再次发起" : ""}>
            <Button
              icon={<DeleteOutlined />}
              disabled={cleanupActiveTaskId !== null}
              onClick={() => {
                setNoTrafficQuery((prev) => ({ ...prev, category }));
                setNoTrafficOpen(true);
                setNoTrafficCandidates([]);
                setNoTrafficTotal(0);
                setCleanupStatus(null);
              }}
            >
              {cleanupActiveTaskId ? "正在清理无流量笔记" : "清理无流量"}
            </Button>
          </Tooltip>
          {/* 2026-10-06：复选框批量删除（只删本地 / 删本地+平台）。 */}
          <Button
            danger
            icon={<DeleteOutlined />}
            disabled={offlineSelected[category].size === 0}
            onClick={() => {
              setBatchDeleteCategory(category);
              setBatchDeleteMode("local");
              setBatchDeleteOpen(true);
            }}
          >
            批量删除{offlineSelected[category].size > 0 ? `（${offlineSelected[category].size}）` : ""}
          </Button>
          <Button
            icon={<HistoryOutlined />}
            onClick={() => {
              setCleanupLogOpen(true);
              void loadCleanupLogs(1);
            }}
          >
            清理日志
          </Button>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => {
              setOfflineSelected((current) => ({ ...current, [category]: new Set() }));
              setOfflinePanels((current) => ({ ...current, [category]: emptyPanel<XhsSearchNote>() }));
              void loadOfflinePage(category, 1, false);
            }}
          >
            刷新
          </Button>
          {/* 2026-10-06：名称模糊查询 +（商业）商品 ID 查询。 */}
          <Input.Search
            allowClear
            placeholder="按标题模糊查询"
            style={{ width: 220 }}
            value={offlineKeyword[category]}
            onChange={(e) =>
              setOfflineKeyword((current) => ({ ...current, [category]: e.target.value }))
            }
            onSearch={() => {
              setOfflineSelected((current) => ({ ...current, [category]: new Set() }));
              setOfflinePanels((current) => ({ ...current, [category]: emptyPanel<XhsSearchNote>() }));
              void loadOfflinePage(category, 1, false);
            }}
          />
          {category === "goods" && (
            <Input.Search
              allowClear
              placeholder="按商品 ID 查询对应笔记"
              style={{ width: 220 }}
              value={offlineItemId}
              onChange={(e) => setOfflineItemId(e.target.value)}
              onSearch={() => {
                setOfflineSelected((current) => ({ ...current, [category]: new Set() }));
                setOfflinePanels((current) => ({ ...current, [category]: emptyPanel<XhsSearchNote>() }));
                void loadOfflinePage(category, 1, false);
              }}
            />
          )}
        </Space>
        {renderNoteGrid(
          panel,
          openOfflineNote,
          {
            selectedIds: offlineSelected[category],
            onToggle: (noteId) => toggleOfflineSelect(category, noteId),
          },
          true,
        )}
      </div>
    );
  };

  const renderInteractions = () => {
    const panel = interactionPanel;
    if (panel.loading && panel.items.length === 0) {
      return (
        <div style={{ padding: "48px 0", textAlign: "center" }}>
          <Spin indicator={<LoadingOutlined spin />} /> <Text type="secondary">加载中…</Text>
        </div>
      );
    }
    if (panel.error && panel.items.length === 0) {
      // 同 renderNoteGrid：安静降级，不摆红块（v1.27 §17.8）。
      return renderFailure(panel.error);
    }
    if (panel.items.length === 0) return <Empty description="没有互动消息" />;
    return (
      <>
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          {panel.items.map((item, index) => (
            <Card key={`${item.message_id || "msg"}-${index}`} size="small">
              <Space align="start" size={12} style={{ width: "100%" }}>
                <Avatar src={item.user_avatar || undefined} size={36}>
                  {(item.user_name || "?")[0]?.toUpperCase()}
                </Avatar>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Space size={8} wrap>
                    <Text strong style={{ fontSize: 13 }}>{item.user_name || "未知用户"}</Text>
                    <Text type="secondary" style={{ fontSize: 11 }}>{formatRemoteTime(item.time)}</Text>
                  </Space>
                  <div style={{ marginTop: 4 }}>
                    <Text style={{ fontSize: 13 }}>{item.content || item.title || "（无内容）"}</Text>
                  </div>
                  {item.note_url && (
                    <div style={{ marginTop: 6 }}>
                      <AntLink href={item.note_url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
                        <LinkOutlined /> 查看笔记
                      </AntLink>
                    </div>
                  )}
                </div>
                {item.cover_url && (
                  <img
                    src={item.cover_url}
                    alt=""
                    style={{ width: 56, height: 56, objectFit: "cover", borderRadius: 6, flexShrink: 0 }}
                  />
                )}
              </Space>
            </Card>
          ))}
        </Space>
        {renderPagingFooter(panel as PanelState<unknown>)}
      </>
    );
  };

  /**
   * Search results view (R8 / design doc §22).
   *
   * Every card carries its **source scope** and the header states how many pages
   * were actually scanned — without that, 「0 命中」 silently reads as 「我没有
   * 这条笔记」, which is a conclusion the scan budget does not entitle us to.
   */
  const renderSearchResults = (result: XhsSelfSearchResponse) => {
    const scopeEntries = Object.entries(result.scopes) as [XhsSelfContentScope, XhsSelfSearchScopeMeta][];
    const scanSummary = scopeEntries
      .map(([, meta]) =>
        meta.error
          ? `${meta.label} ${meta.scanned_pages} 页（失败：${meta.error}）`
          : `${meta.label} ${meta.scanned_pages} 页`,
      )
      .join(" · ");

    return (
      <div>
        <Space wrap size={12} style={{ marginBottom: 12 }}>
          <Text strong>「{result.keyword}」命中 {result.total_matched} 条</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            已扫描：{scanSummary}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            匹配范围为笔记标题（列表接口不返回正文）
          </Text>
        </Space>

        {result.items.length === 0 ? (
          <Empty
            description={
              <Space direction="vertical" size={4}>
                <Text>已扫描的范围内没有匹配</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {result.has_more
                    ? "还有更早的内容未扫描，可点「继续搜索更早内容」"
                    : "该关键词在三个分类的全部内容里都没有匹配"}
                </Text>
              </Space>
            }
          />
        ) : (
          <Row gutter={[16, 16]}>
            {result.items.map((note, index) => (
              <Col key={`${note.scope}-${note.note_id || index}`} xs={24} sm={12} lg={8} xxl={6}>
                <Card
                  hoverable
                  size="small"
                  onClick={() => void openNote(note)}
                  cover={
                    note.cover_url ? (
                      <div style={{ position: "relative", height: 160, overflow: "hidden", background: p.mediaBg }}>
                        <img
                          src={note.cover_url}
                          alt={note.title || note.note_id}
                          style={{ width: "100%", height: "100%", objectFit: "cover" }}
                        />
                      </div>
                    ) : (
                      <div style={{ height: 160, display: "flex", alignItems: "center", justifyContent: "center", background: p.mediaBg }}>
                        <PictureOutlined style={{ fontSize: 26, color: p.fillIcon }} />
                      </div>
                    )
                  }
                >
                  <Card.Meta
                    title={<span style={{ fontSize: 13 }}>{note.title || "（无标题）"}</span>}
                    description={
                      <Space size={6} wrap>
                        <Tag color="geekblue" style={{ marginInlineEnd: 0 }}>
                          {SCOPE_LABELS[note.scope] || note.scope}
                        </Tag>
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          <HeartOutlined /> {formatMetric(note.likes)}
                        </Text>
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          {formatRemoteTime(note.timestamp)}
                        </Text>
                      </Space>
                    }
                  />
                </Card>
              </Col>
            ))}
          </Row>
        )}

        {result.has_more && (
          <div style={{ textAlign: "center", marginTop: 20 }}>
            <Button
              loading={searchLoading}
              onClick={() => void runSearch(result.keyword, result.cursors as Record<string, string>, true)}
            >
              继续搜索更早内容
            </Button>
            <div style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 11 }}>
                每次最多再向下扫描 3 页/分类（平台接口有风控，不做全量翻页）
              </Text>
            </div>
          </div>
        )}
      </div>
    );
  };

  // The modal prefers the detail payload and falls back to the list object, so
  // the known fields render immediately while the rest is still in flight.
  const noteForDetail = detailNote ?? selectedNote;
  const goods: NoteGoods | null = noteForDetail?.goods ?? null;
  // 弹窗里「挂载商品」**只有一个区块**：两个来源合成一份（DEF-047），
  // 版式不随正文到达而改变。
  const mountedGoods = mountGoods(goods, noteForDetail?.shop ?? null);
  const mediaUrls = noteForDetail ? noteMediaUrls(noteForDetail) : [];
  const videoUrl = noteForDetail ? noteVideoUrl(noteForDetail) : "";
  const hasFullMetrics = Boolean(detailNote) && !detailError;

  /** Metric that admits when the list never carried the value. */
  const detailMetric = (icon: ReactNode, value: number, label: string) => (
    <span>
      {icon} {label} {hasFullMetrics ? value : "—"}
    </span>
  );

  // Search is offered wherever the www link can answer (design doc §22.4): all
  // three note scopes need `web_session`, so a creator-only account gets a
  // disabled box **plus the reason** instead of a box that always fails.
  const searchAvailable = (capabilities?.has_web_session ?? true) && activeTab !== "interactions";
  const searchDisabledReason = capabilities
    ? capabilities.capabilities.notes?.reason || capabilities.capabilities.liked?.reason || "该账号不可用搜索"
    : "";

  const renderSearchBar = () => (
    <Space direction="vertical" size={8} style={{ width: "100%", marginBottom: 16 }}>
      <Space wrap size={8}>
        <Input.Search
          allowClear
          style={{ width: 380 }}
          placeholder="搜索我的笔记 / 赞过的 / 收藏（按标题匹配）"
          value={searchKeyword}
          disabled={!searchAvailable}
          onChange={(event) => setSearchKeyword(event.target.value)}
          onSearch={(value) => {
            if (!value.trim()) {
              clearSearch();
              return;
            }
            void runSearch(value);
          }}
          enterButton="搜索"
        />
        {!searchAvailable && (
          <Tooltip title={searchDisabledReason}>
            <Text type="secondary" style={{ fontSize: 12, cursor: "not-allowed" }}>
              该账号不可用搜索
            </Text>
          </Tooltip>
        )}
        {searchResult && (
          <Button size="small" onClick={clearSearch}>
            退出搜索
          </Button>
        )}
      </Space>
      {searchError && <Alert type="warning" showIcon message="搜索失败" description={searchError} />}
    </Space>
  );

  const tabItems = (MY_NOTES_TAB_VALUES as readonly TabKey[]).map((key) => {
    // v1.27（用户拍板 / §17.8）：页签**不再按账号类型或能力置灰、不再禁用**。
    // 旧写法用 `capabilityFor(key).available` 把页签灰掉并换成一块说明 —— 但真机已证明
    // 那些接口是通的（creator 账号只有 ark 通道是**正常形态**），灰掉的恰恰是可用链路；
    // 而且它的「不可用」理由（「创作者平台没有这三类数据」）对「这个账号恰好没数据」
    // 这种情况是**假话**。现在一律可点、一律发请求，由**取数结果**决定展示什么。
    const label = TAB_LABELS[key];
    const children =
      key === "interactions" ? (
      <div>
        <Segmented
          style={{ marginBottom: 16 }}
          value={interactionKind}
          onChange={(value) => setInteractionKind(value as XhsInteractionKind)}
          options={interactionKinds}
        />
        {renderInteractions()}
      </div>
    ) : key === "goods" || key === "normal" ? (
      <div>
        {renderOfflinePanel(key)}
      </div>
    ) : (
      <div>
        {renderSearchBar()}
        {searchLoading && !searchResult ? (
          <div style={{ padding: "48px 0", textAlign: "center" }}>
            <Spin indicator={<LoadingOutlined spin />} />{" "}
            <Text type="secondary">搜索中…（每个分类最多扫描 3 页）</Text>
          </div>
        ) : searchResult ? (
          renderSearchResults(searchResult)
        ) : (
          renderNoteGrid(notePanels[key as XhsSelfContentScope])
        )}
      </div>
    );
    return { key, label, children };
  });

  return (
    <div>
      {/* 2026-10-05 用户口径「移除所有页面页面头」：原「小红书 · 账号自有内容 /
          我的笔记 / 实时读取说明」三行整块移除（用户明确点名这一块要删）。
          「刷新」收进下方账号与身份卡的右上角 —— 它需要的 `selectedAccountId`
          正是那张卡里的账号下拉选的。 */}

      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title="我的笔记"
        extra={
          <Button icon={<ReloadOutlined />} onClick={refreshActive} disabled={!selectedAccountId}>
            刷新
          </Button>
        }
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Space wrap size={12}>
            <Text type="secondary" style={{ fontSize: 12 }}>账号</Text>
            <Select
              style={{ minWidth: 320 }}
              placeholder={isLoadingAccounts ? "加载中…" : "选择一个小红书账号"}
              loading={isLoadingAccounts}
              value={selectedAccountId ?? undefined}
              options={accountOptions}
              onChange={(value) => setSelectedAccountId(value)}
            />
            {isLoadingIdentity && <Spin size="small" indicator={<LoadingOutlined spin />} />}
          </Space>

          {identity && (
            <Space align="center" size={12}>
              <Avatar src={identity.avatar || undefined} size={40}>
                {(identity.nickname || identity.user_id || "?")[0]?.toUpperCase()}
              </Avatar>
              <div>
                <Space size={8} wrap>
                  <Text strong>{identity.nickname || "（昵称未解析）"}</Text>
                  {identity.red_id && <Tag>小红书号 {identity.red_id}</Tag>}
                  {/* 2026-10-06：小红书号后面展示三个平台登录会话状态（千帆/创作者/网页版）。
                      顺序/短名/状态文案全来自 lib/login-state.ts（唯一实现，账号矩阵页同款）。 */}
                  {loginChannelRows(currentAccountLoginChannels).map((row) => (
                    <Tag
                      key={row.channel}
                      style={{
                        marginRight: 0,
                        flexShrink: 0,
                        fontSize: 11,
                        lineHeight: "18px",
                        color: row.view.color,
                        borderColor: row.view.color,
                        background: "transparent",
                        cursor: "default",
                      }}
                    >
                      {row.shortLabel} {row.view.label}
                    </Tag>
                  ))}
                  {identity.ip_location && <Tag>{identity.ip_location}</Tag>}
                </Space>
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>user_id: {identity.user_id}</Text>
                </div>
              </div>
            </Space>
          )}

          {identityError && (
            <Alert type="error" showIcon message="无法识别账号身份" description={identityError} />
          )}

          {capabilities && capabilities.capabilities.goods.available && goodsInfo && (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {goodsInvalid && (
                <Alert
                  type="warning"
                  showIcon
                  message="千帆登录态已失效，商品同步已停止"
                  description={
                    <Space size={10} wrap>
                      <Text style={{ fontSize: 12 }}>
                        {qianfanBinding?.status_message ||
                          goodsInfo.status_message ||
                          "请在该账号的千帆重新登录后再试。"}
                      </Text>
                      {/* ADR-021：绑定动作退场 ⇒ 出口改成「去账号矩阵补登本账号的千帆
                          登录态」（扫码为主、粘贴降级），与别处的千帆补齐入口同一去处。 */}
                      <Button
                        size="small"
                        type="primary"
                        onClick={() => {
                          window.location.assign("/platforms/xhs/accounts");
                        }}
                      >
                        去补登千帆
                      </Button>
                    </Space>
                  }
                />
              )}
              <Space wrap size={10} align="center">
                <Tag color="gold" icon={<ShoppingOutlined />} style={{ marginInlineEnd: 0 }}>
                  商品来源：千帆
                </Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {/* ADR-021：店铺不再是实体 ⇒ 展示的其实是「账号昵称（千帆卖家名）」，
                      文案改为「账号」以免读者误以为存在独立店铺对象。 */}
                  账号 {goodsInfo.shop_name || "（未命名）"}
                  {goodsInfo.synced_at ? ` · ${goodsInfo.synced_at}` : ""}
                </Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {goodsInfo.health_checked_at || qianfanBinding?.health_checked_at
                    ? `巡检 ${goodsInfo.health_checked_at || qianfanBinding?.health_checked_at}`
                    : "尚未巡检"}
                </Text>
                <Button
                  size="small"
                  icon={<SafetyCertificateOutlined />}
                  loading={checkingHealth}
                  onClick={() => void handleCheckQianfanHealth()}
                >
                  立即巡检
                </Button>
                {/* 2026-10-06 需求 7：无流量清理按钮 + 日志弹窗已迁移到「商业笔记 /
                    「普通笔记」两个离线页签（按 category 过滤），此处原入口移除。 */}
              </Space>
            </Space>
          )}

          {capabilities && !capabilities.capabilities.goods.available && (
            <Alert
              type="info"
              showIcon
              icon={<ShoppingOutlined />}
              message="笔记挂载的商品来自千帆商家后台"
              description={
                <Space direction="vertical" size={8} style={{ width: "100%" }}>
                  <Text style={{ fontSize: 12 }}>{capabilities.capabilities.goods.reason}</Text>
                  {/* ADR-021：`POST /qianfan/bind` 退场 ⇒ 不再有「绑定千帆」弹窗，
                      出口统一为「去账号矩阵补登本账号的千帆登录态」。 */}
                  <Button
                    size="small"
                    type="primary"
                    icon={<ShoppingOutlined />}
                    onClick={() => {
                      window.location.assign("/platforms/xhs/accounts");
                    }}
                  >
                    去补登千帆（读取挂载商品）
                  </Button>
                </Space>
              }
            />
          )}

          {needsToken && (
            <Alert
              type="warning"
              showIcon
              message="需要主页访问令牌（xsec_token）"
              description={
                <Space direction="vertical" size={8} style={{ width: "100%" }}>
                  <Text style={{ fontSize: 13 }}>
                    平台拒绝了列表请求。请打开小红书网页版进入自己的主页，从浏览器地址栏复制完整链接后粘贴到下方。
                  </Text>
                  <Space.Compact style={{ width: "100%", maxWidth: 640 }}>
                    <Input
                      placeholder="https://www.xiaohongshu.com/user/profile/...?xsec_token=..."
                      value={profileUrl}
                      onChange={(event) => setProfileUrl(event.target.value)}
                      onPressEnter={() => refreshActive()}
                      style={{ flex: 1 }}
                    />
                    <Button type="primary" onClick={refreshActive} disabled={!profileUrl.trim()}>
                      应用并重试
                    </Button>
                  </Space.Compact>
                </Space>
              }
            />
          )}
        </Space>
      </Card>

      <Tabs
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as TabKey)}
        items={tabItems}
      />

      {/* 2026-10-06 需求：导入离线笔记数据文件（xlsx）弹窗。 */}
      <Modal
        open={importOpen}
        centered
        title={`导入${importCategory === "goods" ? "商业" : "普通"}笔记数据文件`}
        okText="导入"
        confirmLoading={importUploading}
        onOk={() => void handleImportNoteData()}
        onCancel={() => {
          setImportOpen(false);
          setImportFile(null);
        }}
        okButtonProps={{ disabled: !importFile }}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            上传千帆数据中心导出的 .xlsx 笔记数据文件，后端异步解析落库，完成后通过站内消息通知统计结果。
          </Text>
          <Upload
            accept=".xlsx"
            maxCount={1}
            beforeUpload={(file) => {
              setImportFile(file);
              return false; // 不自动上传，手动提交
            }}
            onRemove={() => setImportFile(null)}
            fileList={importFile ? [{ uid: "1", name: importFile.name }] : []}
          >
            <Button icon={<UploadOutlined />}>选择 .xlsx 文件</Button>
          </Upload>
        </Space>
      </Modal>

      {/* 2026-10-06：复选框批量删除弹窗 —— 单选框，默认「只删本地」，可选「删本地+平台」。 */}
      <Modal
        open={batchDeleteOpen}
        centered
        title={`批量删除${batchDeleteCategory === "goods" ? "商业" : "普通"}笔记`}
        okText="删除"
        okButtonProps={{ danger: true }}
        confirmLoading={batchDeleting}
        onOk={() => void handleBatchDelete()}
        onCancel={() => setBatchDeleteOpen(false)}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Text>
            已选中 <Text strong>{offlineSelected[batchDeleteCategory].size}</Text> 篇笔记。
          </Text>
          <Radio.Group
            value={batchDeleteMode}
            onChange={(e) => setBatchDeleteMode(e.target.value as "local" | "platform")}
          >
            <Space direction="vertical">
              <Radio value="local">
                只删除本地数据
                <div style={{ fontSize: 12, color: "#888" }}>
                  仅移除本地记录，不影响平台上的笔记。
                </div>
              </Radio>
              <Radio value="platform">
                删除本地 + 平台数据
                <div style={{ fontSize: 12, color: "#888" }}>
                  先删除平台笔记，成功后同步删除本地记录（不可恢复）。
                </div>
              </Radio>
            </Space>
          </Radio.Group>
        </Space>
      </Modal>

      {/* 2026-10-06：手动同步弹窗 —— 弹窗内选日期范围（≤90 天）。 */}
      <Modal
        open={syncOpen}
        centered
        title={`同步${syncCategory === "goods" ? "商业" : "普通"}笔记数据`}
        okText="同步"
        confirmLoading={syncSubmitting}
        onOk={() => void handleSyncNoteData(syncCategory)}
        onCancel={() => setSyncOpen(false)}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            选择要同步的日期范围（最长 90 天）。留空则同步最近 90 天。
          </Text>
          <DatePicker.RangePicker
            value={
              syncDateRange
                ? [dayjs(syncDateRange[0]), dayjs(syncDateRange[1])]
                : null
            }
            onChange={(range) => {
              if (range && range[0] && range[1]) {
                setSyncDateRange([
                  range[0].format("YYYY-MM-DD"),
                  range[1].format("YYYY-MM-DD"),
                ]);
              } else {
                setSyncDateRange(null);
              }
            }}
            disabledDate={(current) =>
              current &&
              (current > dayjs().endOf("day") ||
                current < dayjs().subtract(90, "day").startOf("day"))
            }
            placeholder={["开始日期", "截止日期"]}
            style={{ width: "100%" }}
          />
        </Space>
      </Modal>

      {/*
        详情弹窗走**共用组件**（`components/platforms/note-detail-modal.tsx`）：
        版式（80vw / 72vh 双栏）、左栏媒体区、失败三分法、评论四态都在那一份实现里，
        内容库页用的是同一个 —— 不再两页各写一遍。
      */}
      <NoteDetailModal
        open={Boolean(selectedNote)}
        onClose={closeNote}
        title={noteForDetail?.title}
        onRefresh={() => refreshNoteDetail()}
        refreshing={detailLoading}
        loading={detailLoading}
        failure={detailError}
        mediaUrls={mediaUrls}
        videoUrl={videoUrl}
        mediaNamePrefix={noteForDetail?.note_id || "note"}
        commentsSlot={
          <NoteCommentList
            comments={comments}
            loading={isLoadingComments}
            error={commentError}
          />
        }
      >
        {noteForDetail && (
          <>
                {noteForDetail.content ? (
                  <Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 12, fontSize: 13 }}>
                    {noteForDetail.content}
                  </Paragraph>
                ) : (
                  !detailLoading && (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      列表接口不返回正文，未能取到笔记正文。
                    </Text>
                  )
                )}

                {noteForDetail.tags && noteForDetail.tags.length > 0 && (
                  <div style={{ marginTop: 8, marginBottom: 12 }}>
                    {noteForDetail.tags.map((tag) => (
                      <span
                        key={tag}
                        style={{ color: "#1677ff", fontSize: 13, marginRight: 10, whiteSpace: "nowrap" }}
                      >
                        #{tag}
                      </span>
                    ))}
                  </div>
                )}

                {/* 2026-10-06 重新布局：去掉 bordered 表格框，改「浅色信息块 + 分组」，
                    信息分块清晰（基础信息 / 互动 / 数据指标 / 千帆 / 链接）。 */}
                <div
                  style={{
                    background: p.mediaBg,
                    borderRadius: 8,
                    padding: "12px 16px",
                    fontSize: 13,
                    marginTop: 12,
                  }}
                >
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "8px 24px", alignItems: "center" }}>
                    <span>
                      <Text type="secondary">作者</Text>{" "}
                      <Text strong>{noteForDetail.author_name || "—"}</Text>
                    </span>
                    <span>
                      <Text type="secondary">类型</Text>{" "}
                      <Tag style={{ marginInlineEnd: 0 }}>{noteKindLabel(noteForDetail)}</Tag>
                    </span>
                    <span>
                      <Text type="secondary">发布时间</Text>{" "}
                      <Text>{formatRemoteTime(noteForDetail.timestamp) || "—"}</Text>
                    </span>
                    <span>
                      <Text type="secondary">笔记 ID</Text>{" "}
                      <Tag
                        icon={<CopyOutlined />}
                        style={{ cursor: "pointer", marginInlineEnd: 0 }}
                        onClick={() => {
                          if (!noteForDetail.note_id) return;
                          navigator.clipboard
                            .writeText(noteForDetail.note_id)
                            .then(() => message.success("笔记 ID 已复制"))
                            .catch(() => message.error("复制失败，请手动选中后复制"));
                        }}
                      >
                        {noteForDetail.note_id || "—"}
                      </Tag>
                      {noteExternalUrl(noteForDetail) && (
                        <AntLink
                          href={noteExternalUrl(noteForDetail)}
                          target="_blank"
                          rel="noreferrer"
                          style={{ marginLeft: 12, fontSize: 12 }}
                        >
                          <LinkOutlined /> 在小红书打开
                        </AntLink>
                      )}
                    </span>
                  </div>
                </div>

                {/* 互动数据（4 指标横排） */}
                <div style={{ marginTop: 16 }}>
                  <Text strong style={{ fontSize: 13, display: "block", marginBottom: 8 }}>
                    互动数据
                  </Text>
                  <Space size={20} wrap>
                    {detailMetric(<HeartOutlined style={{ color: "#ff4d4f" }} />, noteForDetail.likes, "点赞")}
                    {detailMetric(<StarOutlined style={{ color: "#faad14" }} />, noteForDetail.collects, "收藏")}
                    {detailMetric(<CommentOutlined style={{ color: "#1677ff" }} />, noteForDetail.comments, "评论")}
                    {detailMetric(<ShareAltOutlined style={{ color: "#52c41a" }} />, noteForDetail.shares, "分享")}
                  </Space>
                </div>

                {/* 2026-10-06：离线笔记（商业/普通）的指标全面展示 —— 数据来自
                    离线落库的 `raw_json.metrics`（24+ 个指标，按互动/观看/商品分组）。
                    缺值不渲染（硬约束⑤缺值口径）。 */}
                {offlineMetrics && (
                  <div style={{ marginTop: 16 }}>
                    <Text strong style={{ fontSize: 13, display: "block", marginBottom: 8 }}>
                      数据指标
                    </Text>
                    {/* 2026-10-06：一类一行（互动/观看/商品各起一行），类内 chips 超行允许
                        换行，类与类之间用**虚线**分割（去掉灰色小标题，分割用虚线表达）。 */}
                    {OFFLINE_METRICS_GROUPS.map((group, groupIndex) => {
                      const entries = group.metrics.filter(
                        ([key]) =>
                          offlineMetrics[key] !== undefined &&
                          offlineMetrics[key] !== null,
                      );
                      if (entries.length === 0) return null;
                      return (
                        <div
                          key={group.label}
                          style={{
                            display: "flex",
                            flexWrap: "wrap",
                            gap: 8,
                            padding: "4px 0",
                            // 最后一项不画虚线（避免区块底部多一条悬空的线）
                            borderBottom:
                              groupIndex < OFFLINE_METRICS_GROUPS.length - 1
                                ? "1px dashed #d9d9d9"
                                : "none",
                          }}
                        >
                          {entries.map(([key, label]) => (
                            <Tag
                              key={key}
                              style={{ marginInlineEnd: 0, fontSize: 12, padding: "2px 10px" }}
                            >
                              {label} <Text strong>{offlineMetrics[key]}</Text>
                            </Tag>
                          ))}
                        </div>
                      );
                    })}
                  </div>
                )}

                {/*
                  挂载商品（设计 12 v1.20 / DEF-047）：**只有一个区块、一套版式**。

                  两个来源（列表行带上来的 `goods` + 详情按 `note_ids` 查千帆的
                  `shop.goods`）在这里先合成一份（`mountGoods`）—— 早前它们各自渲染
                  一块、版式还互不相同，正文一到达就变样（用户 2026-09-20 报出）。
                  标题**单行** `…` + 悬浮看全；**规格与价格同一行**。
                */}
                {mountedGoods.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <Divider titlePlacement="start" style={{ margin: "8px 0" }}>
                      <Space size={6}>
                        <ShoppingOutlined />
                        <Text strong style={{ fontSize: 13 }}>
                          挂载商品
                        </Text>
                        <Tag>{mountedGoods.length}</Tag>
                      </Space>
                    </Divider>
                    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                      {mountedGoods.map((g) => (
                        <Card key={g.key} size="small">
                          <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
                            {g.image && (
                              <img
                                src={g.image}
                                alt={g.title || "商品"}
                                style={{
                                  width: 72,
                                  height: 72,
                                  objectFit: "cover",
                                  borderRadius: 6,
                                  flex: "0 0 auto",
                                }}
                              />
                            )}
                            <div style={{ flex: "1 1 0%", minWidth: 0 }}>
                              {/* 标题：**永远一行**，超出 `…` 收起 + 悬浮看全文 */}
                              <Text
                                strong
                                ellipsis={{ tooltip: g.title || "（无商品标题）" }}
                                style={{ fontSize: 13, display: "block" }}
                              >
                                {g.title || "（无商品标题）"}
                              </Text>
                              {/* 规格与价格：**同一行**（规格先让位 `…`+tip，价格不压缩） */}
                              {(g.variant || g.price) && (
                                <div className="goods-meta-row">
                                  {g.variant && (
                                    <Text
                                      type="secondary"
                                      ellipsis={{ tooltip: g.variant }}
                                      style={{ fontSize: 12, minWidth: 0, maxWidth: 300, display: "inline-block" }}
                                    >
                                      {g.variant}
                                    </Text>
                                  )}
                                  {g.price && (
                                    <Text
                                      strong
                                      style={{
                                        fontSize: 14,
                                        flex: "0 0 auto",
                                        whiteSpace: "nowrap",
                                        color: "#ff4d4f",
                                      }}
                                    >
                                      ¥ {g.price}
                                    </Text>
                                  )}
                                </div>
                              )}
                              <Space size={8} wrap style={{ marginTop: 4 }}>
                                {g.shopName && <Tag>{g.shopName}</Tag>}
                                {g.goodsId && (
                                  <Text type="secondary" style={{ fontSize: 11 }}>
                                    ID: {g.goodsId}
                                  </Text>
                                )}
                              </Space>
                            </div>
                          </div>
                        </Card>
                      ))}
                    </div>
                  </div>
                )}


          </>
        )}
      </NoteDetailModal>

      {/* 千帆「绑定」弹窗（粘贴 Cookie 那一套）已随 POST /xhs/qianfan/bind 于 2026-09-30 退场
          （ADR-021：店铺实体退场，千帆能力判据收敛为「本账号 ark 令牌」，走账号矩阵扫码登录）。
          历史实现见 git 提交与设计 18 §6。 */}

      {/* 无流量笔记清理：阈值可配 → 本地预览候选 → 确认后后台异步删除 */}
      <Modal
        centered
        open={noTrafficOpen}
        title="清理无流量笔记"
        // 尺寸（2026-10-05 用户口径「弹窗固定全屏的 70%，笔记列表表格内滚动」）：
        // 宽高都取视口的 70%，**不设固定像素** —— 与商品侧两个弹窗（75vw / 75vh）
        // 同一套「按视口比例」写法，只是这里略小一档（筛选区 + 列表同屏）。
        // **不设 maxWidth**：设了它宽屏上就不是 70% 了，口径与实际不符。
        width="70vw"
        // 高度 = 视口 70%，**不猜像素**：`content` 设成 flex 列，header / footer 由
        // antd 自己占位，`body` 吃剩下的一整块（`flex: 1 + minHeight: 0`）。
        // 先前用 `calc(70vh - 168px)` 猜页脚高度 —— 实测页脚换行后比预估矮，
        // body 反而溢出 40px 被裁（弹窗总高只剩 64.3vh，不是 70%）。
        styles={{
          // antd 6 的语义名是 `container`（≈ antd 5 的 `.ant-modal-content`），
          // **没有 `content`** —— 写错会 TS2353，不是静默忽略。
          container: { height: "70vh", display: "flex", flexDirection: "column" },
          body: {
            flex: 1,
            minHeight: 0,
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
          },
        }}
        onCancel={() => setNoTrafficOpen(false)}
        footer={
          // 页脚 = 报账 + 两个清理入口。**位置固定**（`container` 是 flex 列、
          // body 吃剩余高度）⇒ 表格再长也不会把按钮顶出视野。
          <Space wrap size={12}>
            {/* 「清理前 / 清理后多少条」：清理前 = 候选池总数（预览后即时可知）；
                清理后 = 后端**重算**的候选池（不是 before - deleted 的估算）。 */}
            <Text type="secondary" style={{ fontSize: 13 }}>
              {cleanupStatus?.before_total != null
                ? `清理前 ${cleanupStatus.before_total} 条`
                : noTrafficTotal
                  ? `候选 ${noTrafficTotal} 条`
                  : "尚未预览"}
              {cleanupStatus?.after_total != null
                ? ` → 清理后 ${cleanupStatus.after_total} 条`
                : ""}
              {noTrafficSelected.length ? ` · 已选 ${noTrafficSelected.length} 篇` : ""}
            </Text>
            <Button onClick={() => setNoTrafficOpen(false)}>关闭</Button>
            <Button
              icon={<SearchOutlined />}
              loading={noTrafficLoading}
              onClick={() => void handlePreviewNoTraffic()}
            >
              预览候选
            </Button>
            {/* 入口 ①：清理**指定**笔记（列表勾选）。没勾就禁用并说明，
                不是「点了没反应」。 */}
            <Popconfirm
              title={`确认删除勾选的 ${noTrafficSelected.length} 篇笔记？`}
              description="只删勾选的；推广中的自动跳过。"
              okText="清理选中"
              cancelText="取消"
              disabled={!noTrafficSelected.length || cleanupRunning}
              onConfirm={() => void handleStartCleanup(noTrafficSelected)}
            >
              <Tooltip title={noTrafficSelected.length ? "" : "先在列表左侧勾选要清理的笔记"}>
                <Button
                  danger
                  icon={<DeleteOutlined />}
                  loading={cleanupRunning}
                  disabled={!noTrafficSelected.length}
                >
                  清理指定笔记（{noTrafficSelected.length} 篇）
                </Button>
              </Tooltip>
            </Popconfirm>
            {/* 入口 ②：清理**全部**候选（按当前阈值 + 查询条件，单批上限 20）。 */}
            <Popconfirm
              title="确认按当前条件清理全部候选？"
              description="最多删 20 篇，推广中的自动跳过。"
              okText="清理全部"
              cancelText="取消"
              disabled={!noTrafficTotal || cleanupRunning}
              onConfirm={() => void handleStartCleanup()}
            >
              <Button
                type="primary"
                danger
                icon={<DeleteOutlined />}
                loading={cleanupRunning}
                disabled={!noTrafficTotal}
              >
                清理全部（{noTrafficTotal} 篇）
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            height: "100%",
            gap: 12,
            minHeight: 0,
          }}
        >
          {/* 筛选 + 阈值 + 覆盖说明 + 清理状态：一整块**不参与压缩**（flexShrink: 0）
              —— 被压扁的话输入框会被截掉半行，而它们才是这个弹窗的操作面板。 */}
          <div
            style={{
              flexShrink: 0,
              border: `1px solid ${p.border}`,
              borderRadius: 8,
              padding: "10px 12px",
              background: p.fillWeak,
            }}
          >
            <Text strong style={{ fontSize: 13 }}>
              筛选条件
            </Text>
            <div style={{ height: 8 }} />
            <Space wrap size={8}>
              <Input
                allowClear
                style={{ width: 220 }}
                placeholder="笔记标题关键词（千帆服务端搜索）"
                value={noTrafficQuery.keyword}
                onChange={(event) =>
                  setNoTrafficQuery((prev) => ({
                    ...prev,
                    keyword: event.target.value,
                  }))
                }
              />
              <Select
                style={{ width: 110 }}
                placeholder="笔记类型"
                allowClear
                value={noTrafficQuery.note_type || undefined}
                onChange={(value) =>
                  setNoTrafficQuery((prev) => ({
                    ...prev,
                    note_type: value || "",
                  }))
                }
                options={[
                  { value: "normal", label: "图文" },
                  { value: "video", label: "视频" },
                ]}
              />
            </Space>
          <Divider style={{ margin: "2px 0" }} />
          <Space wrap size={12}>
            <Text style={{ fontSize: 13 }}>阅读量 ≤</Text>
            <InputNumber
              min={0}
              max={100000}
              value={noTrafficThresholds.max_browse}
              onChange={(value) => {
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_browse: Number(value) || 0,
                }));
                setNoTrafficPage(1); // 改条件回第 1 页（同 note_type 的注释）
              }}
            />
            <Text style={{ fontSize: 13 }}>支付订单 ≤</Text>
            <InputNumber
              min={0}
              max={100000}
              value={noTrafficThresholds.max_pay_order}
              onChange={(value) => {
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_pay_order: Number(value) || 0,
                }));
                setNoTrafficPage(1); // 改条件回第 1 页（同 note_type 的注释）
              }}
            />
            <Text style={{ fontSize: 13 }}>支付金额 ≤</Text>
            <InputNumber
              min={0}
              max={10000000}
              step={0.01}
              value={noTrafficThresholds.max_pay_gmv}
              onChange={(value) => {
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_pay_gmv: Number(value) || 0,
                }));
                setNoTrafficPage(1); // 改条件回第 1 页（同 note_type 的注释）
              }}
            />
          </Space>
          <div style={{ height: 6 }} />
          <Space wrap size={12}>
            <Text style={{ fontSize: 13 }}>点赞 ≤</Text>
            <InputNumber
              min={0}
              max={1000000}
              placeholder="不限"
              value={noTrafficThresholds.max_like}
              onChange={(value) => {
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_like: value === null ? null : Number(value),
                }));
                setNoTrafficPage(1);
              }}
            />
            <Text style={{ fontSize: 13 }}>收藏 ≤</Text>
            <InputNumber
              min={0}
              max={1000000}
              placeholder="不限"
              value={noTrafficThresholds.max_collect}
              onChange={(value) => {
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_collect: value === null ? null : Number(value),
                }));
                setNoTrafficPage(1);
              }}
            />
            <Text style={{ fontSize: 13 }}>评论 ≤</Text>
            <InputNumber
              min={0}
              max={1000000}
              placeholder="不限"
              value={noTrafficThresholds.max_comment}
              onChange={(value) => {
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_comment: value === null ? null : Number(value),
                }));
                setNoTrafficPage(1);
              }}
            />
            <Text style={{ fontSize: 13 }}>分享 ≤</Text>
            <InputNumber
              min={0}
              max={1000000}
              placeholder="不限"
              value={noTrafficThresholds.max_share}
              onChange={(value) => {
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_share: value === null ? null : Number(value),
                }));
                setNoTrafficPage(1);
              }}
            />
          </Space>
          {noTrafficCoverage && (
            <Alert type="info" showIcon message={noTrafficCoverage} />
          )}
          {cleanupStatus && (
            <Alert
              type={cleanupStatus.status === "done" ? "success" : "info"}
              showIcon
              message={
                cleanupStatus.status === "running"
                  ? `后台执行中… 已删除 ${cleanupStatus.deleted} / 本批 ${cleanupStatus.total}`
                  : `删除 ${cleanupStatus.deleted} · 跳过 ${cleanupStatus.skipped} · 失败 ${cleanupStatus.failed}`
              }
            />
          )}
          </div>
          {/* 候选列表：表格 + **表内滚动**（用户口径「笔记列表表格内滚动」）。
              `flex: 1 + minHeight: 0` 是 flex 容器内出现滚动条的前提 ——
              `minHeight` 不给 0 的话，表格会把父容器撑高、滚到弹窗外层去。 */}
          <div
            style={{
              flex: 1,
              minHeight: 0,
              display: "flex",
              flexDirection: "column",
              border: `1px solid ${p.border}`,
              borderRadius: 8,
              overflow: "hidden",
            }}
          >
            <div
              style={{
                padding: "8px 12px",
                borderBottom: `1px solid ${p.border}`,
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <Text strong style={{ fontSize: 13 }}>
                候选笔记
              </Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {noTrafficTotal
                  ? `共 ${noTrafficTotal} 条 · 已选 ${noTrafficSelected.length} 条`
                  : "尚未预览"}
              </Text>
            </div>
            <div style={{ flex: 1, minHeight: 0 }}>
            {noTrafficLoading ? (
              <div style={{ textAlign: "center", padding: "32px 0" }}>
                <Spin indicator={<LoadingOutlined spin />} />{" "}
                <Text type="secondary">正在本地查询候选…</Text>
              </div>
            ) : (
              <Table<NoTrafficCandidate>
                size="small"
                rowKey="note_id"
                // 勾选（「清理指定笔记」）：跨页保留 —— 用户在第 1 页勾了两篇、
                // 翻到第 2 页再勾一篇，点清理应该删 3 篇，不是只删当页的。
                rowSelection={{
                  selectedRowKeys: noTrafficSelected,
                  onChange: (keys) => setNoTrafficSelected(keys as string[]),
                  // 不做「全选当前页」以外的全选（用户既有偏好：有上限的多选不设全选按钮）
                  columnWidth: 42,
                }}
                // 缺值一律 `—`，**不渲染 0**（「读不到」≠「读到 0」，硬约束⑤）
                dataSource={noTrafficCandidates}
                // 分页在后端（本地查询也要按页取，否则 3000+ 条候选会一次渲染完）。
                // 页码/页大小走受控值，改条件时由调用方显式回第 1 页。
                pagination={{
                  current: noTrafficPage,
                  pageSize: noTrafficPageSize,
                  total: noTrafficTotal,
                  showSizeChanger: true,
                  pageSizeOptions: [10, 20, 50, 100],
                  size: "small",
                  showTotal: (total: number) => `共 ${total} 篇候选`,
                  onChange: (nextPage: number, nextSize: number) => {
                    const sizeChanged = nextSize !== noTrafficPageSize;
                    setNoTrafficPageSize(nextSize);
                    void handlePreviewNoTraffic(sizeChanged ? 1 : nextPage);
                  },
                }}
                // `y` 交给 antd 需要一个具体长度，而外层 `.ant-table-body` 已经是
                // flex 撑满的 ⇒ 用 `max-content` 高度的容器配 `flex: 1` 会打架，
                // 所以这里仍给长度，但**按视口算**（不写死像素）：
                // 70vh 减去「弹窗标题 + 不压缩的筛选区 + 表头 + 分页器」的实测高度。
                scroll={{ y: "calc(70vh - 420px)" }}
                locale={{
                  emptyText: (
                    <Empty description="先点「预览候选」查看符合条件的笔记" />
                  ),
                }}
                columns={[
                  {
                    title: "封面",
                    dataIndex: "cover",
                    width: 76,
                    render: (cover: string) =>
                      cover ? (
                        <img
                          src={cover}
                          alt=""
                          style={{
                            width: 48,
                            height: 48,
                            objectFit: "cover",
                            borderRadius: 6,
                          }}
                        />
                      ) : (
                        <Text type="secondary">—</Text>
                      ),
                  },
                  {
                    title: "标题",
                    dataIndex: "title",
                    render: (title: string) => (
                      <Text ellipsis style={{ fontSize: 13 }}>
                        {title || "（无标题）"}
                      </Text>
                    ),
                  },
                  {
                    title: "阅读量",
                    dataIndex: "browse_num",
                    width: 100,
                    align: "right",
                    render: (value: number | null) => numberCell(value),
                  },
                  {
                    title: "支付订单",
                    dataIndex: "pay_order_cnt",
                    width: 100,
                    align: "right",
                    render: (value: number | null) => numberCell(value),
                  },
                  {
                    title: "支付金额",
                    dataIndex: "pay_gmv",
                    width: 110,
                    align: "right",
                    render: (value: number | null) => numberCell(value),
                  },
                ]}
              />
            )}
            </div>
          </div>
        </div>
      </Modal>

      {/* 清理日志详情（用户口径「查询清理无流量笔记日志详情按钮，弹窗列表展示该
          日志记录」）。数据源 = 独立表 `no_traffic_cleanup_logs`，不随队列记录
          清理消失（客户口径「日志表独立」）。

          ⚠️ 2026-10-06 用户口径「清理无流量的主要对象是笔记，**所有主体应是笔记
          维度**，记录操作日志时也应该是笔记维度」⇒ 列表改**父子两层**：
          批次行（这次整体怎么样）+ 展开行（**这一篇**被删了没有、为什么）。
          展开区是**一张独立的笔记表**（自带表头），不是把子行塞进父表 ——
          与网关页「父子表头各自独立」同款。 */}
      <Modal
        centered
        open={cleanupLogOpen}
        title="清理无流量笔记 · 操作日志"
        width="70vw"
        onCancel={() => setCleanupLogOpen(false)}
        footer={
          <Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              共 {cleanupLogTotal} 次
            </Text>
            <Button onClick={() => setCleanupLogOpen(false)}>关 闭</Button>
          </Space>
        }
      >
        <Table<NoTrafficCleanupLogItem>
          rowKey={(row) => String(row.id)}
          size="small"
          loading={cleanupLogLoading}
          dataSource={cleanupLogRows}
          pagination={{
            current: cleanupLogPage,
            pageSize: 20,
            total: cleanupLogTotal,
            showSizeChanger: false,
            onChange: (page) => void loadCleanupLogs(page),
          }}
          locale={{ emptyText: "还没有清理记录" }}
          scroll={{ x: 1100 }}
          // ⛔ **白屏修复同款一行**：`items` 是数据里的字段名（不是 antd 的
          //    `children`），但这里显式关掉「树数据自动探测」以防将来改名踩坑 ——
          //    父表只走 `expandedRowRender`（网关页的教训：两条渲染路径同时跑会崩）。
          childrenColumnName="__no_tree__"
          expandable={{
            rowExpandable: (row) => (row.items?.length ?? 0) > 0,
            expandedRowRender: (row) => (
              <Table<NoTrafficCleanupItemRow>
                rowKey={(item) => String(item.id)}
                size="small"
                dataSource={row.items}
                pagination={false}
                showHeader
                columns={[
                  {
                    title: "笔记",
                    key: "note",
                    render: (_, item) => (
                      <Space size={8} align="start">
                        {item.note_cover && (
                          <Image
                            src={item.note_cover}
                            alt=""
                            width={32}
                            height={32}
                            style={{ borderRadius: 4, objectFit: "cover" }}
                            preview={false}
                          />
                        )}
                        <Text style={{ fontSize: 13 }}>
                          {item.note_title || "（标题未知）"}
                        </Text>
                      </Space>
                    ),
                  },
                  {
                    title: "笔记 ID",
                    dataIndex: "note_id",
                    width: 220,
                    render: (value: string) => (
                      <Typography.Text code copyable={value ? { text: value } : false}>
                        {value || "—"}
                      </Typography.Text>
                    ),
                  },
                  {
                    title: "结果",
                    dataIndex: "result",
                    width: 110,
                    render: (value: string) => {
                      const hit = cleanupItemResult(value);
                      return <Tag color={hit.color}>{hit.text}</Tag>;
                    },
                  },
                  {
                    title: "原因",
                    dataIndex: "reason",
                    // 缺值渲染 `—`（读到空 ≠ 没有原因；删除成功本来就没有原因）。
                    render: (value: string) => value || "—",
                  },
                  {
                    title: "操作时间",
                    dataIndex: "operated_at",
                    width: 160,
                    render: (value: string | null) =>
                      value ? value.replace("T", " ").slice(0, 19) : "—",
                  },
                ]}
              />
            ),
          }}
          columns={[
            {
              title: "操作时间",
              dataIndex: "created_at",
              width: 160,
              render: (value: string | null) => (value ? value.replace("T", " ").slice(0, 19) : "—"),
            },
            {
              title: "账号",
              dataIndex: "account_name",
              width: 140,
              // ⚠️ 缺名兜底**不用 `账号 #${id}`** 那种形态：仓库有一条按文件生效的
              //    守卫（`test_account_dropdowns_show_only_the_account_name`）拦这个
              //    标记，它是为「下拉标签不许拼 id」立的；这里同样不该拼。
              render: (value: string, row) => value || (row.account_id ? `账号 ${row.account_id}` : "—"),
            },
            {
              title: "状态",
              dataIndex: "status",
              width: 90,
              render: (value: string) => {
                const map: Record<string, { text: string; color: string }> = {
                  running: { text: "执行中", color: "blue" },
                  done: { text: "完成", color: "green" },
                  failed: { text: "有失败", color: "red" },
                };
                const hit = map[value] ?? { text: value || "—", color: "default" };
                return <Tag color={hit.color}>{hit.text}</Tag>;
              },
            },
            {
              title: "笔记",
              key: "items",
              width: 120,
              // **笔记维度**的入口可见性：这一批动了几篇笔记（展开看逐篇）。
              render: (_, row) => (
                <Text type="secondary">
                  {row.items?.length ? `${row.items.length} 篇（展开查看）` : "—"}
                </Text>
              ),
            },
            {
              title: "清理前 / 后",
              key: "range",
              width: 130,
              render: (_, row) => `${row.before_total} → ${row.after_total}`,
            },
            { title: "删除", dataIndex: "deleted", width: 70, align: "right" },
            { title: "跳过", dataIndex: "skipped", width: 70, align: "right" },
            {
              title: "失败",
              dataIndex: "failed",
              width: 70,
              align: "right",
              render: (value: number) =>
                value ? <Text type="danger">{value}</Text> : <Text type="secondary">0</Text>,
            },
            {
              title: "失败原因",
              key: "reason",
              // ⚠️ DEF-095：这里只渲染**本次失败原因**（`reason`）。候选池覆盖度
              //    说明（`coverage_note`）**不显示在这一列** —— 它此前被错写进
              //    `reason`，于是平台风控失败时界面显示「候选池仅包含已同步千帆
              //    挂链映射的笔记…」，把用户指向完全错误的方向。
              render: (_, row) => {
                const parts: string[] = [];
                if (row.reason) parts.push(row.reason);
                // 中止（风控/凭据）加一句说明：本批没跑完，不是「只有这一篇失败」。
                if (row.aborted) {
                  parts.push(`本批已中止（未执行完）${row.abort_reason ? `：${row.abort_reason}` : ""}`);
                }
                const text = parts.join(" ");
                if (!text) return "—";
                return <Text type={row.failed ? "danger" : "secondary"}>{text}</Text>;
              },
            },
          ]}
        />
      </Modal>
    </div>
  );
}
