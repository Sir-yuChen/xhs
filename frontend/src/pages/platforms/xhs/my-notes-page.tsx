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
import { useAccountScope } from "../../../lib/account-scope";
import type { ReactNode } from "react";

import { useThemePalette } from "../../../app/providers";
import {
  FILTER_CACHE_KEYS,
  MY_NOTES_TAB_VALUES,
  asEnum,
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
  fetchXhsAccountLoginStates,
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
import { dictLabel, useDictOptions } from "../../../lib/system-dicts";

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
type TabKey = XhsSelfContentScope | "interactions";

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

const TAB_LABELS: Record<TabKey, string> = {
  notes: "全部笔记",
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
  // 同形，初值优先取缓存、变化即回写。页签 / 互动分类走 `asEnum`（取值集合就是界面上那两组下拉的集合）。
  //
  // ⚠️ 账号**不走**页面级旧键了（设计 32 §5.4 / A5，2026-10-11）：改用**业务线级**
  // 的 `useAccountScope("content")` —— 与「内容库」共用同一个账号，
  // 用户切页不再看到「账号悄悄换了一个」。旧键由 `readAccountScope` 一次性迁移。
  const [selectedAccountId, setSelectedAccountId] = useAccountScope("content");
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
  const [interactionKind, setInteractionKind] = usePersistedFilter<XhsInteractionKind>(
    FILTER_CACHE_KEYS.myNotes.interactionKind, "like_collect", asEnum(INTERACTION_KIND_VALUES)
  );
  // 设计 63：互动分类选项走字典 `xhs_interaction_kind`（镜像 interactionKinds 兜底）。
  const interactionKindOptions = useDictOptions(
    "xhs_interaction_kind",
    interactionKinds.map((item) => ({ value: item.value, label: item.label }))
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
        const scope = activeTab as XhsSelfContentScope;
        void loadNotePage(scope, notePanels[scope].cursor, true);
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, searchResult, sentinelRef.current, notePanels, interactionPanel, capabilities]);

  /** The only way forward after a failed page: an explicit user click (§20). */
  function retryActivePage() {
    if (activeTab === "interactions") {
      void loadInteractionPage(interactionPanel.cursor, true);
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

  function closeNote() {
    setSelectedNote(null);
    setDetailNote(null);
    setDetailError(null);
    setComments([]);
    setCommentError(null);
  }

  function refreshActive() {
    // 刷新同时重探三平台登录态（L3），登录态跟着页面一起刷新。
    void autoCheckLoginStates();
    if (activeTab === "interactions") {
      setInteractionPanel(emptyPanel<XhsInteractionMessage>());
      void loadInteractionPage("", false);
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
                          {dictLabel("sys_own_content_scope", note.scope, SCOPE_LABELS) || note.scope}
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
          options={interactionKindOptions}
        />
        {renderInteractions()}
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


    </div>
  );
}
