import {
  DeleteOutlined,
  CommentOutlined,
  CopyOutlined,
  FileTextOutlined,
  HeartOutlined,
  LinkOutlined,
  LoadingOutlined,
  PictureOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  ShareAltOutlined,
  ShoppingOutlined,
  SearchOutlined,
  SafetyCertificateOutlined,
  StarOutlined,
  UserOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Avatar,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Empty,
  InputNumber,
  Input,
  message,
  Modal,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { PageHeader } from "../../../components/layout/app-shell";
import {
  bindXhsQianfanShop,
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
  fetchNoTrafficCandidates,
  startNoTrafficCleanup,
  fetchNoTrafficCleanupStatus,
  type NoTrafficThresholds,
  type NoTrafficCandidate,
  type NoTrafficCleanupStatus,
  type NoTrafficQuery,
  unbindXhsQianfanShop,
} from "../../../lib/api";
import type {
  NoteComment,
  NoteGoods,
  PlatformAccount,
  XhsGoodsSource,
  XhsInteractionKind,
  XhsInteractionMessage,
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

/** 复制到剪贴板：优先 Clipboard API，非安全上下文（如局域网 IP 访问）退回 execCommand。 */
async function copyToClipboard(value: string, okText: string): Promise<void> {
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
    message.success(okText);
  } catch {
    message.error("复制失败，请手动选中后复制");
  }
}

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
  return {
    ...note,
    ...detail,
    goods: detail.goods ?? note.goods ?? null,
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

const TAB_LABELS: Record<TabKey, string> = {
  notes: "我发布的",
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
  if (note.note_url) return note.note_url;
  return note.note_id ? `https://www.xiaohongshu.com/explore/${note.note_id}` : "";
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
  return (
    <Space size={4} title={title} style={{ fontSize: 12, color: "rgba(255,255,255,0.45)" }}>
      {icon}
      <span>{formatMetric(value)}</span>
    </Space>
  );
}

export function XhsMyNotesPage() {
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null);
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

  const [activeTab, setActiveTab] = useState<TabKey>("notes");
  const [profileUrl, setProfileUrl] = useState("");
  const [needsToken, setNeedsToken] = useState(false);

  const [notePanels, setNotePanels] = useState<Record<XhsSelfContentScope, PanelState<XhsSearchNote>>>({
    notes: emptyPanel<XhsSearchNote>(),
    liked: emptyPanel<XhsSearchNote>(),
    collected: emptyPanel<XhsSearchNote>(),
  });
  const [interactionKind, setInteractionKind] = useState<XhsInteractionKind>("like_collect");
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
  // 详情/评论失败的承载：`text` 是给界面的**一行短文案**，`full` 是后端原话（Tooltip 里看）。
  // v1.27 之前详情失败只弹 toast、`detailError` 永远是 null —— 于是 `hasFullMetrics`
  // 的 `!detailError` 一直是真，作者当年写的那道闸其实是**死的**；现在它真正生效。
  const [detailError, setDetailError] = useState<QuietError | null>(null);
  const [comments, setComments] = useState<NoteComment[]>([]);
  const [commentError, setCommentError] = useState<QuietError | null>(null);
  const [isLoadingComments, setIsLoadingComments] = useState(false);

  // 千帆（ark）商品接线：绑定弹窗 + 同步中状态。
  // 绑定结果本身不单独存一份 state —— 它由 `capabilities.goods` 承载
  // （店铺名 / 总数 / 已同步数 / 同步时间都在那里），避免同一事实有两个来源。
  const [bindModalOpen, setBindModalOpen] = useState(false);
  const [bindCookieText, setBindCookieText] = useState("");
  const [bindSubmitting, setBindSubmitting] = useState(false);
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
        setSelectedAccountId((current) => current ?? first?.id ?? null);
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
  async function handleBindQianfan() {
    if (!selectedAccountId) return;
    const cookieString = bindCookieText.trim();
    if (!cookieString) {
      message.warning("请先粘贴千帆的 Cookie");
      return;
    }
    setBindSubmitting(true);
    try {
      const binding = await bindXhsQianfanShop(selectedAccountId, cookieString);
      message.success(`已绑定千帆店铺：${binding.shop_name || binding.shop_user_id}`);
      setBindModalOpen(false);
      setBindCookieText("");
      await reloadCapabilities();
      refreshActive();
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setBindSubmitting(false);
    }
  }

  // ── 无流量笔记清理（写操作，全部走后台异步 + 限流 + 批次上限）──────────────
  const [noTrafficOpen, setNoTrafficOpen] = useState(false);
  const [noTrafficLoading, setNoTrafficLoading] = useState(false);
  const [noTrafficThresholds, setNoTrafficThresholds] = useState<NoTrafficThresholds>({
    max_browse: 0,
    max_pay_order: 0,
    max_pay_gmv: 0,
  });
  const [noTrafficQuery, setNoTrafficQuery] = useState<NoTrafficQuery>({
    keyword: "",
    note_type: "",
    published_before: null,
  });
  const [noTrafficCandidates, setNoTrafficCandidates] = useState<NoTrafficCandidate[]>([]);
  const [noTrafficTotal, setNoTrafficTotal] = useState(0);
  const [noTrafficCoverage, setNoTrafficCoverage] = useState<string>("");
  const [cleanupRunning, setCleanupRunning] = useState(false);
  const [cleanupStatus, setCleanupStatus] = useState<NoTrafficCleanupStatus | null>(null);
  const cleanupTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 预览无流量候选（纯本地查询，零平台请求）。 */
  async function handlePreviewNoTraffic() {
    if (!selectedAccountId) return;
    setNoTrafficLoading(true);
    setNoTrafficCandidates([]);
    setCleanupStatus(null);
    try {
      const result = await fetchNoTrafficCandidates(
        selectedAccountId,
        noTrafficThresholds,
        noTrafficQuery,
        50
      );
      setNoTrafficCandidates(result.candidates);
      setNoTrafficTotal(result.total);
      setNoTrafficCoverage(
        `候选池 ${result.coverage.synced_notes} 条已同步映射 · ${result.coverage.reason}`
      );
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setNoTrafficLoading(false);
    }
  }

  /** 轮询后台清理任务状态，直到 done/failed。 */
  async function pollCleanupStatus(taskId: number) {
    try {
      const status = await fetchNoTrafficCleanupStatus(taskId);
      setCleanupStatus(status);
      if (status.status === "pending" || status.status === "running") {
        cleanupTimerRef.current = setTimeout(() => void pollCleanupStatus(taskId), 2000);
        return;
      }
      setCleanupRunning(false);
      if (status.status === "done") {
        message.success(`清理完成：删除 ${status.deleted} 篇，跳过 ${status.skipped} 篇`);
        refreshActive();
      } else {
        message.warning(
          `清理结束（有失败）：删除 ${status.deleted} · 跳过 ${status.skipped} · 失败 ${status.failed}`
        );
      }
    } catch (error) {
      setCleanupRunning(false);
      message.error(readError(error).message);
    }
  }

  /** 确认后开始清理：后端立即起后台线程，前端只轮询状态。 */
  async function handleStartCleanup() {
    if (!selectedAccountId || !noTrafficTotal) return;
    setCleanupRunning(true);
    setCleanupStatus(null);
    try {
      const { task_id } = await startNoTrafficCleanup(
        selectedAccountId,
        noTrafficThresholds,
        noTrafficQuery,
        20
      );
      message.info(`清理任务已创建（#${task_id}），后台执行中…`);
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

  async function handleUnbindQianfan() {
    if (!selectedAccountId) return;
    try {
      const result = await unbindXhsQianfanShop(selectedAccountId);
      message.success(`已解除绑定，清理 ${result.removed} 条商品映射`);
      await reloadCapabilities();
      refreshActive();
    } catch (error) {
      message.error(readError(error).message);
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
   * Keep the paging guard in sync with the active panel.
   *
   * Written in an effect (not during render) so the observer callback always
   * reads committed values — this is the in-flight de-duplication that keeps one
   * scroll from firing the same page request twice (R7 / design doc §20).
   */
  useEffect(() => {
    const panel =
      activeTab === "interactions" ? interactionPanel : notePanels[activeTab as XhsSelfContentScope];
    pagingRef.current = {
      loading: Boolean(panel?.loading),
      hasMore: Boolean(panel?.hasMore),
      blocked: Boolean(panel?.error),
    };
  }, [activeTab, interactionPanel, notePanels]);

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
      const { message, code } = readError(error);
      setSearchError(message);
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
    if (activeTab === "interactions") {
      setInteractionPanel(emptyPanel<XhsInteractionMessage>());
      void loadInteractionPage("", false);
      return;
    }
    patchNotePanel(activeTab, emptyPanel<XhsSearchNote>());
    void loadNotePage(activeTab, "", false);
  }

  const renderNoteGrid = (panel: PanelState<XhsSearchNote>) => {
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
                onClick={() => void openNote(note)}
                cover={
                  note.cover_url ? (
                    <div style={{ position: "relative", height: 180, overflow: "hidden", background: "#141414" }}>
                      <img
                        src={note.cover_url}
                        alt={note.title || note.note_id}
                        style={{ width: "100%", height: "100%", objectFit: "cover" }}
                      />
                      {noteKindLabel(note) === "视频" && (
                        <PlayCircleOutlined
                          style={{ position: "absolute", right: 8, bottom: 8, fontSize: 24, color: "#fff" }}
                        />
                      )}
                    </div>
                  ) : (
                    <div style={{ height: 180, display: "flex", alignItems: "center", justifyContent: "center", background: "#141414" }}>
                      <PictureOutlined style={{ fontSize: 28, color: "rgba(255,255,255,0.25)" }} />
                    </div>
                  )
                }
              >
                <Card.Meta
                  title={<span style={{ fontSize: 13 }}>{note.title || "（无标题）"}</span>}
                  description={
                    <div>
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
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                        <Space size={6}>
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
                      <div style={{ position: "relative", height: 160, overflow: "hidden", background: "#141414" }}>
                        <img
                          src={note.cover_url}
                          alt={note.title || note.note_id}
                          style={{ width: "100%", height: "100%", objectFit: "cover" }}
                        />
                      </div>
                    ) : (
                      <div style={{ height: 160, display: "flex", alignItems: "center", justifyContent: "center", background: "#141414" }}>
                        <PictureOutlined style={{ fontSize: 26, color: "rgba(255,255,255,0.25)" }} />
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

  const tabItems = (["notes", "liked", "collected", "interactions"] as TabKey[]).map((key) => {
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
          renderNoteGrid(notePanels[key])
        )}
      </div>
    );
    return { key, label, children };
  });

  return (
    <div>
      <PageHeader
        eyebrow="小红书 · 账号自有内容"
        title="我的笔记"
        description="实时读取当前账号在小红书平台侧的笔记与互动数据；本页数据不落库，仅引用平台 CDN 直链。"
        action={
          <Button icon={<ReloadOutlined />} onClick={refreshActive} disabled={!selectedAccountId}>
            刷新
          </Button>
        }
      />

      <Card size="small" style={{ marginBottom: 16 }}>
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
            {capabilities && (
              <Tag color={capabilities.session_source ? "green" : "orange"}>
                {capabilities.session_source === "own" ? "网页会话可用" : "无网页会话"}
              </Tag>
            )}
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
                  {identity.ip_location && <Tag>{identity.ip_location}</Tag>}
                  {identity.link && <Tag color="geekblue">{identity.link === "creator" ? "创作者平台" : "网页版"}</Tag>}
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
                          "请在千帆重新登录后重新绑定。"}
                      </Text>
                      <Button size="small" type="primary" onClick={() => setBindModalOpen(true)}>
                        重新绑定千帆
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
                  店铺 {goodsInfo.shop_name || "（未命名）"}
                  {/* 2026-09-22：原「同店 N 个账号」提示已移除 —— 店铺与账号是 1:1
                      （用户口径：不允许「同店铺、多账号」），那句话描述的场景不再存在。 */}
                  {goodsInfo.shop_total > 0
                    ? ` · 已按需补齐 ${goodsInfo.synced_notes}/${goodsInfo.shop_total} 条商品笔记`
                    : ` · 已按需补齐 ${goodsInfo.synced_notes} 条商品笔记`}
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
                <Popconfirm
                  title="解除千帆绑定？"
                  description="将同时清掉本地已同步的商品映射，笔记详情不再显示商品。"
                  okText="解除"
                  cancelText="取消"
                  onConfirm={() => void handleUnbindQianfan()}
                >
                  <Button size="small" type="text">
                    解除绑定
                  </Button>
                </Popconfirm>
                <Button
                  size="small"
                  icon={<DeleteOutlined />}
                  onClick={() => {
                    setNoTrafficOpen(true);
                    setNoTrafficCandidates([]);
                    setNoTrafficTotal(0);
                    setCleanupStatus(null);
                  }}
                >
                  清理无流量
                </Button>
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
                  <Button
                    size="small"
                    type="primary"
                    icon={<ShoppingOutlined />}
                    onClick={() => setBindModalOpen(true)}
                  >
                    绑定千帆（读取挂载商品）
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

                <Descriptions column={1} size="small" bordered>
                  <Descriptions.Item label="基础信息">
                    <Space size={6} wrap>
                      <Tag icon={<FileTextOutlined />}>{noteKindLabel(noteForDetail)}</Tag>
                      <Tag
                        icon={<CopyOutlined />}
                        style={{ cursor: "pointer" }}
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
                      <Tag icon={<UserOutlined />}>{noteForDetail.author_name || "—"}</Tag>
                    </Space>
                  </Descriptions.Item>
                  <Descriptions.Item label="发布时间">{formatRemoteTime(noteForDetail.timestamp) || "—"}</Descriptions.Item>
                  <Descriptions.Item label="互动数据">
                    <Space size={12} wrap>
                      {detailMetric(<HeartOutlined />, noteForDetail.likes, "点赞")}
                      {detailMetric(<StarOutlined />, noteForDetail.collects, "收藏")}
                      {detailMetric(<CommentOutlined />, noteForDetail.comments, "评论")}
                      {detailMetric(<ShareAltOutlined />, noteForDetail.shares, "分享")}
                    </Space>
                  </Descriptions.Item>
                  {/*
                    千帆（店铺侧）指标：创作者账号的数据来自千帆商品笔记列表。
                    只渲染千帆真的返回了的项 —— 缺值不填 0（设计文档 §18.2 口径）。
                  */}
                  {noteForDetail.shop && (
                    <Descriptions.Item label="千帆数据">
                      <Space size={12} wrap>
                        {noteForDetail.shop.browse_num !== null &&
                          noteForDetail.shop.browse_num !== undefined && (
                            <Text style={{ fontSize: 12 }}>
                              阅读 {noteForDetail.shop.browse_num}
                            </Text>
                          )}
                        {noteForDetail.shop.browse_num_30d !== null &&
                          noteForDetail.shop.browse_num_30d !== undefined && (
                            <Text style={{ fontSize: 12 }}>
                              近 30 日阅读 {noteForDetail.shop.browse_num_30d}
                            </Text>
                          )}
                        {noteForDetail.shop.pay_gmv !== null &&
                          noteForDetail.shop.pay_gmv !== undefined && (
                            <Text style={{ fontSize: 12 }}>
                              支付金额 ¥{noteForDetail.shop.pay_gmv}
                            </Text>
                          )}
                        {noteForDetail.shop.pay_order_cnt !== null &&
                          noteForDetail.shop.pay_order_cnt !== undefined && (
                            <Text style={{ fontSize: 12 }}>
                              支付订单 {noteForDetail.shop.pay_order_cnt}
                            </Text>
                          )}
                      </Space>
                    </Descriptions.Item>
                  )}
                  <Descriptions.Item label="原始链接">
                    {noteExternalUrl(noteForDetail) ? (
                      <AntLink href={noteExternalUrl(noteForDetail)} target="_blank" rel="noreferrer">
                        <LinkOutlined /> 在小红书打开
                      </AntLink>
                    ) : (
                      "—"
                    )}
                  </Descriptions.Item>
                </Descriptions>

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
                                      style={{ fontSize: 12, minWidth: 0 }}
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
                              {g.url && (
                                <div style={{ marginTop: 6 }}>
                                  <Space size={6} align="center">
                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                      商品链接
                                    </Text>
                                    {/* 文本展示 + 点击复制（用户 2026-09-19 定：不再跳转，
                                        要的是把链接拷去别处用）。长链接省略显示，复制的是完整值。 */}
                                    <Tooltip title="点击复制">
                                      <Text
                                        style={{
                                          fontSize: 12,
                                          maxWidth: 300,
                                          cursor: "pointer",
                                          color: "#1677ff",
                                        }}
                                        ellipsis
                                        onClick={() => void copyToClipboard(g.url, "商品链接已复制")}
                                      >
                                        {g.url}
                                      </Text>
                                    </Tooltip>
                                    <Tooltip title="点击复制">
                                      <Button
                                        type="text"
                                        size="small"
                                        icon={<CopyOutlined />}
                                        onClick={() => void copyToClipboard(g.url, "商品链接已复制")}
                                      />
                                    </Tooltip>
                                  </Space>
                                </div>
                              )}
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

      {/* 千帆绑定弹窗：商品数据的唯一入口（设计文档 §19）。
          要的是**千帆自己的**登录态，不是本账号的小红书 Cookie。 */}
      <Modal
        open={bindModalOpen}
        title="绑定千帆商家后台（读取笔记挂载商品）"
        okText="绑定并同步"
        cancelText="取消"
        confirmLoading={bindSubmitting}
        onOk={() => void handleBindQianfan()}
        onCancel={() => setBindModalOpen(false)}
        width={640}
      >
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          <Text style={{ fontSize: 13 }}>
            笔记挂载的商品只存在于千帆商家后台，且千帆使用它自己的登录态
            ——账号的小红书 Cookie 在这里会被判为「缺失登录凭证」。请按下面三步取一份千帆
            Cookie：
          </Text>
          <ol style={{ margin: 0, paddingInlineStart: 20, fontSize: 12, lineHeight: 1.9 }}>
            <li>
              用 Chrome 打开并登录{" "}
              <AntLink href="https://ark.xiaohongshu.com/" target="_blank" rel="noreferrer">
                ark.xiaohongshu.com
              </AntLink>
              （千帆商家后台）；
            </li>
            <li>F12 → Network，刷新页面，任选一条向 ark.xiaohongshu.com 发起的请求；</li>
            <li>
              复制请求头里的 Cookie 整串，粘贴到下面。必须包含{" "}
              <Text code>access-token-ark.xiaohongshu.com</Text>。
            </li>
          </ol>
          <Input.TextArea
            rows={4}
            value={bindCookieText}
            onChange={(event) => setBindCookieText(event.target.value)}
            placeholder="a1=...; access-token-ark.xiaohongshu.com=...; x-user-id-ark.xiaohongshu.com=..."
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            Cookie 以 Fernet 加密落库，仅用于只读同步商品笔记；接口不回显它，日志也不打印。
          </Text>
        </Space>
      </Modal>

      {/* 无流量笔记清理：阈值可配 → 本地预览候选 → 确认后后台异步删除 */}
      <Modal
        open={noTrafficOpen}
        title="清理无流量笔记"
        width={680}
        onCancel={() => setNoTrafficOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setNoTrafficOpen(false)}>关闭</Button>
            <Button
              icon={<SearchOutlined />}
              loading={noTrafficLoading}
              onClick={() => void handlePreviewNoTraffic()}
            >
              预览候选
            </Button>
            <Popconfirm
              title={`确认删除符合条件的笔记？`}
              description={`本次最多删除 20 篇（批次上限），推广中的笔记会自动跳过。后台异步执行，可随时关闭本窗口。`}
              okText="开始清理"
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
                开始清理（{noTrafficTotal} 篇）
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <div>
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
          </div>
          <Divider style={{ margin: "2px 0" }} />
          <Space wrap size={12}>
            <Text style={{ fontSize: 13 }}>阅读量 ≤</Text>
            <InputNumber
              min={0}
              max={100000}
              value={noTrafficThresholds.max_browse}
              onChange={(value) =>
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_browse: Number(value) || 0,
                }))
              }
            />
            <Text style={{ fontSize: 13 }}>支付订单 ≤</Text>
            <InputNumber
              min={0}
              max={100000}
              value={noTrafficThresholds.max_pay_order}
              onChange={(value) =>
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_pay_order: Number(value) || 0,
                }))
              }
            />
            <Text style={{ fontSize: 13 }}>支付金额 ≤</Text>
            <InputNumber
              min={0}
              max={10000000}
              step={0.01}
              value={noTrafficThresholds.max_pay_gmv}
              onChange={(value) =>
                setNoTrafficThresholds((prev) => ({
                  ...prev,
                  max_pay_gmv: Number(value) || 0,
                }))
              }
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
          {noTrafficLoading ? (
            <div style={{ textAlign: "center", padding: "32px 0" }}>
              <Spin indicator={<LoadingOutlined spin />} />{" "}
              <Text type="secondary">正在本地查询候选…</Text>
            </div>
          ) : noTrafficCandidates.length ? (
            <Space direction="vertical" size={6} style={{ width: "100%" }}>
              {noTrafficCandidates.map((candidate) => (
                <Card key={candidate.note_id} size="small">
                  <Space align="start" style={{ width: "100%" }}>
                    {candidate.cover && (
                      <img
                        src={candidate.cover}
                        alt=""
                        style={{
                          width: 48,
                          height: 48,
                          objectFit: "cover",
                          borderRadius: 6,
                        }}
                      />
                    )}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <Text ellipsis style={{ fontSize: 13 }}>
                        {candidate.title || "（无标题）"}
                      </Text>
                      <div>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          阅读 {candidate.browse_num ?? "—"} · 支付订单{" "}
                          {candidate.pay_order_cnt ?? "—"} · 支付金额{" "}
                          {candidate.pay_gmv ?? "—"}
                        </Text>
                      </div>
                    </div>
                  </Space>
                </Card>
              ))}
            </Space>
          ) : (
            !noTrafficTotal && (
              <Empty description="先点「预览候选」查看符合条件的笔记" />
            )
          )}
        </Space>
      </Modal>
    </div>
  );
}
