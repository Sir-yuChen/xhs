import {
  BookOutlined,
  CheckSquareOutlined,
  CloudSyncOutlined,
  CopyOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
  FileAddOutlined,
  FileTextOutlined,
  HeartOutlined,
  LinkOutlined,
  MessageOutlined,
  PictureOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SendOutlined,
  ShareAltOutlined,
  ShoppingOutlined,
  StarOutlined,
  SyncOutlined,
  TagsOutlined,
} from "@ant-design/icons";
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Divider,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Popover,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import {
  NoteDetailModal,
  formatRemoteTime,
  quietError,
  readError,
  renderFailure,
} from "../../../components/platforms/note-detail-modal";
import type { QuietError } from "../../../components/platforms/note-detail-modal";
import {
  batchCreateDraftsFromNotes,
  batchPublishSavedNotes,
  batchTagNotes,
  createDraftFromNote,
  createTag,
  deleteSavedNote,
  downloadExportFile,
  exportSavedNotes,
  fetchAccounts,
  fetchSavedNote,
  fetchSavedNoteAssets,
  fetchSavedNotes,
  fetchTags,
  publishSavedNote,
  syncSavedNote,
  updateSavedNotePublishStatus,
  updateSavedNoteTopics,
} from "../../../lib/api";
import { fenPriceText } from "../../../lib/format";
import {
  PUBLISH_STATUS_FILTER_OPTIONS,
  canPublish,
  nextManualStatus,
  publishStatusTag,
  splitPublishTargets,
  type PublishStatusFilter,
} from "../../../lib/note-publish-gates";
import { formatShanghaiTime } from "../../../lib/time";
import {
  NOTE_TYPE_FILTER_OPTIONS,
  accountFilterLabel,
  canSyncNote,
  noteTypeLabel,
  noteTypeTagColor,
  type NoteTypeFilter,
} from "../../../lib/note-filters";
import type { NoteAsset, NotesExportResponse, PlatformAccount, SavedNote, Tag as TagType } from "../../../types";

const { Title, Text, Paragraph } = Typography;

/**
 * 正文缺口的**安静说明**（失败三分法的第三态，设计 12 §17.9）。
 *
 * ⚠️ 2026-09-22 起详情端点**不再打平台**（`hydrate=true` 已移除，用户口径：
 * 「内容库应查询的均是本地数据」）⇒ 这里再也拿不到 `hydrate.status` / `reason`，
 * 「取不到」与「真没数据」**在内容库里不再可区分**。按缺值口径照实说：
 * 列表源不带正文，补齐由同步链路异步做，不伪装成「平台没有这篇正文」。
 *
 * 原实现（读 `note.hydrate` 区分 `ok`/`reason`）保留在版本历史里 —— 那条区分
 * 需要一次平台请求才成立，而这次请求正是 500 的来源。
 */
function hydrateHint(_note: SavedNote): string | null {
  return "列表源不带正文，待同步链路补齐。";
}

/**
 * 「下拉自动加载更多」的底部条（2026-09-22 用户口径）。
 *
 * 两种入口都保留：**滚动**（哨兵 + IntersectionObserver，`rootMargin` 240px 提前触发）
 * 和**按钮**（观察器不可用 / 用户就是想点一下时的退路）。只有按钮没有哨兵 = 老用户体验不变；
 * 只有哨兵没有按钮 = 观察器失效时彻底加载不到更多。
 *
 * 失败**不静默**：取下一页失败时摆一行安静说明 + 「重试」按钮（三分法）。
 */
function LoadMoreBar(props: {
  hasMore: boolean;
  loading: boolean;
  loaded: number;
  total: number;
  failure: QuietError | null;
  onLoadMore: () => void;
}) {
  const { hasMore, loading, loaded, total, failure, onLoadMore } = props;
  if (!hasMore && !failure) {
    return (
      <div style={{ textAlign: "center", padding: "16px 0", color: "rgba(255,255,255,.35)", fontSize: 12 }}>
        {loaded > 0 ? `已全部加载（共 ${loaded} 条）` : ""}
      </div>
    );
  }
  return (
    <div style={{ textAlign: "center", padding: "16px 0" }}>
      {failure && <div style={{ marginBottom: 8 }}>{renderFailure(failure, "加载更多失败")}</div>}
      <Button size="small" loading={loading} onClick={onLoadMore} disabled={!hasMore && !failure}>
        {loading ? "加载中…" : failure ? "重试" : `加载更多（已加载 ${loaded} / ${total}）`}
      </Button>
    </div>
  );
}

function formatSavedTime(v: string): string { return formatShanghaiTime(v); }
function getRawNoteType(note: SavedNote): string { const t = note.raw_json?.model_type ?? note.raw_json?.type; return typeof t === "string" ? t : "note"; }

/** 从 `raw_json` 里挖发布时间（**原值**：秒 / 毫秒 / 字符串都可能）。 */
function getNotePublishTimestamp(note: SavedNote): unknown {
  const raw = note.raw_json ?? {};
  const data = (raw.data && typeof raw.data === "object") ? raw.data as Record<string, unknown> : {};
  const items = Array.isArray(data.items) ? data.items : [];
  const item = (items[0] && typeof items[0] === "object") ? items[0] as Record<string, unknown> : {};
  const card = (item.note_card && typeof item.note_card === "object") ? item.note_card as Record<string, unknown> : {};
  return card.time ?? card.create_time ?? card.last_update_time ?? raw.time ?? raw.create_time;
}
/**
 * 发布时间（展示用）。**格式化走 `formatRemoteTime`（唯一实现）** ——
 * 这里只负责「从 raw_json 里找出那个时间戳」，不再自己写一遍日期转换。
 */
function getNotePublishTime(note: SavedNote): string { return formatRemoteTime(getNotePublishTimestamp(note)); }

function getNoteUrl(note: SavedNote): string {
  const raw = note.raw_json ?? {};
  for (const key of ["note_url", "url", "share_url"]) {
    const v = raw[key];
    if (typeof v === "string" && v.startsWith("http")) return v;
  }
  const data = (raw.data && typeof raw.data === "object") ? raw.data as Record<string, unknown> : {};
  const items = Array.isArray(data.items) ? data.items : [];
  const item = (items[0] && typeof items[0] === "object") ? items[0] as Record<string, unknown> : {};
  const card = (item.note_card && typeof item.note_card === "object") ? item.note_card as Record<string, unknown> : {};
  for (const obj of [card, item]) {
    const xsec = obj.xsec_token;
    if (typeof xsec === "string" && xsec) {
      const src = (typeof obj.xsec_source === "string" ? obj.xsec_source : "") || "pc_feed";
      return `https://www.xiaohongshu.com/explore/${note.note_id}?xsec_token=${xsec}&xsec_source=${src}`;
    }
    for (const k of ["note_url", "url", "share_url"]) {
      const v = obj[k];
      if (typeof v === "string" && v.startsWith("http")) return v;
    }
  }
  return `https://www.xiaohongshu.com/explore/${note.note_id}`;
}
function getAuthorProfileUrl(note: SavedNote): string {
  const raw = note.raw_json ?? {};
  const directId = raw.author_id;
  if (typeof directId === "string" && directId) return `https://www.xiaohongshu.com/user/profile/${directId}`;
  const data = (raw.data && typeof raw.data === "object") ? raw.data as Record<string, unknown> : {};
  const items = Array.isArray(data.items) ? data.items : [];
  const item = (items[0] && typeof items[0] === "object") ? items[0] as Record<string, unknown> : {};
  const card = (item.note_card && typeof item.note_card === "object") ? item.note_card as Record<string, unknown> : {};
  const user = (card.user && typeof card.user === "object") ? card.user as Record<string, unknown> : {};
  const uid = user.user_id ?? user.id;
  if (typeof uid === "string" && uid) return `https://www.xiaohongshu.com/user/profile/${uid}`;
  return "";
}
function getNoteEngagement(note: SavedNote): { likes: number; collects: number; comments: number; shares: number } {
  const raw = note.raw_json ?? {};
  // Direct fields (from search results)
  const likes = Number(raw.liked_count ?? raw.likes ?? 0);
  const collects = Number(raw.collected_count ?? raw.collects ?? 0);
  const comments = Number(raw.comment_count ?? raw.comments ?? 0);
  const shares = Number(raw.share_count ?? raw.shares ?? 0);
  if (likes || collects || comments || shares) return { likes, collects, comments, shares };
  // Nested in data.items[0].note_card.interact_info
  const data = (raw.data && typeof raw.data === "object") ? raw.data as Record<string, unknown> : {};
  const items = Array.isArray(data.items) ? data.items : [];
  const item = (items[0] && typeof items[0] === "object") ? items[0] as Record<string, unknown> : {};
  const card = (item.note_card && typeof item.note_card === "object") ? item.note_card as Record<string, unknown> : {};
  const info = (card.interact_info && typeof card.interact_info === "object") ? card.interact_info as Record<string, unknown> : {};
  return {
    likes: Number(info.liked_count ?? 0),
    collects: Number(info.collected_count ?? 0),
    comments: Number(info.comment_count ?? 0),
    shares: Number(info.share_count ?? 0),
  };
}
function rawString(note: SavedNote, keys: string[]): string { for (const k of keys) { const v = note.raw_json?.[k]; if (typeof v === "string" && v) return v; } return ""; }
function getSavedNoteCoverUrl(note: SavedNote): string { return note.cover_url || note.asset_urls?.[0] || rawString(note, ["cover_url", "image_url"]); }

/**
 * **唯一的发布状态标签**（设计 13 §32.3.4：卡片、详情两处只展示一个状态）。
 * ⚠️ 2026-09-23 表格视图已移除（用户口径「不再支持表格展示笔记」）⇒ 由三处变两处，
 * 口径本身不变：**同一字段同一组件**，不在第二个地方另写一份呈现。
 *
 * 改之前这里并排两个状态（「已发布/待发布」开关 + 独立的「发布失败」tag）——
 * 既重复又可能互相矛盾（用户口径：「笔记卡片页中只展示一个发布状态」）。
 *
 * * 本地生成笔记：状态由发布链路管理 ⇒ **只读**，`failed` 时挂 Tooltip 摆
 *   `publish_error` 原文（长文案 `…` 收起 + Tooltip、不换行，ADR-018）；
 * * 其余笔记：可点切换「待发布 / 已发布」（§19.12 的既有能力保留）。
 */
function PublishStatusTag({
  note,
  onToggle,
  loading,
}: {
  note: SavedNote;
  onToggle?: (next: "pending" | "published") => void;
  loading?: boolean;
}) {
  const view = publishStatusTag(note.note_type, note.publish_status);
  // 只有「非本地生成」的笔记才可点切换（本地生成的状态归发布链路管）。
  const clickable = view.toggleable && Boolean(onToggle);
  const tag = (
    <Tag
      color={view.color}
      style={clickable ? { cursor: "pointer", margin: 0 } : { margin: 0 }}
      onClick={
        clickable
          ? (event) => {
              event.stopPropagation();
              onToggle?.(nextManualStatus(note.publish_status));
            }
          : undefined
      }
    >
      {loading ? <Spin size="small" /> : view.text}
    </Tag>
  );
  // 失败态：Tooltip 摆**完整失败原因**（取不到原因时不编文案）；失败态优先于切换提示。
  if (view.failed) {
    return <Tooltip title={note.publish_error || "发布失败（无原因记录）"}>{tag}</Tooltip>;
  }
  return clickable ? (
    <Tooltip title="点击切换发布状态（待发布 ⇄ 已发布）">{tag}</Tooltip>
  ) : (
    tag
  );
}

export function XhsLibraryPage() {
  const navigate = useNavigate();
  const [notes, setNotes] = useState<SavedNote[]>([]);
  const [total, setTotal] = useState(0);
  // 全库口径的发布状态统计（**不随筛选变化**，设计 05 §2.5 / §32.3.3 三档）。
  const [stats, setStats] = useState<{ pending: number; published: number; failed: number }>({
    pending: 0,
    published: 0,
    failed: 0,
  });
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedNote, setSelectedNote] = useState<SavedNote | null>(null);
  const [selectedAssets, setSelectedAssets] = useState<NoteAsset[]>([]);
  const [isDetailOpen, setIsDetailOpen] = useState(false);
  const [isDetailLoading, setIsDetailLoading] = useState(false);
  // 失败承载：`QuietError` 让**三分法**能区分「预期内取不到」与「真接口异常」
  // （设计 12 §17.9）—— 早前这里是一句 `Alert type="warning"` 的泛化文案，
  // 用户既看不出原因、也没有自救线索。
  const [detailFailure, setDetailFailure] = useState<QuietError | null>(null);
  const [detailActionMessage, setDetailActionMessage] = useState<string | null>(null);
  // 「同步」按钮（§25）：**笔记卡片与详情卡都渲染**（仅三方同步笔记）。
  // `syncingNoteId` 记「哪一条在同步中」—— 卡片与详情共用同一个 loading 语义。
  // `detailSyncFailure` 承载详情卡内的失败三分法：`WEB_SESSION_REQUIRED` 安静一行，
  // 其余红块摆后端原话（DEF-058）。
  const [syncingNoteId, setSyncingNoteId] = useState<number | null>(null);
  const [detailSyncFailure, setDetailSyncFailure] = useState<QuietError | null>(null);
  // 「发布」按钮（设计 13 §31）：**笔记卡片与详情卡都渲染**（仅本地生成且
  // 待发布/发布失败的笔记）。`publishingNoteId` 记「哪一篇在发布中」——
  // 发布是几次平台请求量级的链路，loading 语义与同步分开（互不打断）。
  const [publishingNoteId, setPublishingNoteId] = useState<number | null>(null);
  const [isBatchPublishing, setIsBatchPublishing] = useState(false);
  const [isCreatingDraft, setIsCreatingDraft] = useState(false);
  const [availableTags, setAvailableTags] = useState<TagType[]>([]);
  // 标签增删（§29.7）：详情弹窗标题后展示 + 「+ 添加标签」Popover（下拉选已有 + 输入自定义）。
  // `tagPopoverOpen` = Popover 开合；`customTagName` = 自定义输入框草稿；
  // `tagSaving` = 正在写库（防连点）；`tagFailure` = 失败三分法呈现。
  const [tagPopoverOpen, setTagPopoverOpen] = useState(false);
  const [customTagName, setCustomTagName] = useState("");
  const [tagSaving, setTagSaving] = useState(false);
  const [tagFailure, setTagFailure] = useState<QuietError | null>(null);
  // 话题增删（§29.3）：Tag 展示，增删都实时落库。`topicDraft` = 「+」弹出输入框里的草稿；
  // `topicSaving` = 正在写库（防连点），`topicFailure` = 写库失败的三分法呈现。
  const [topicPopoverOpen, setTopicPopoverOpen] = useState(false);
  const [topicDraft, setTopicDraft] = useState("");
  const [topicSaving, setTopicSaving] = useState(false);
  const [topicFailure, setTopicFailure] = useState<QuietError | null>(null);
  const [keywordFilter, setKeywordFilter] = useState("");
  const [selectedTagFilter, setSelectedTagFilter] = useState("");
  // 发布状态筛选（设计 13 §32.3.3 四档：全部 / 待发布 / 已发布 / 发布失败）。
  const [publishStatusFilter, setPublishStatusFilter] = useState<PublishStatusFilter>("all");
  // 来源账号 / 笔记类型筛选（2026-09-23，用户口径「内容库新增根据账号筛选的条件」+「笔记类型」）。
  // 两个都是**枚举筛选**，取值集合与后端查询参数逐字对齐（见 `lib/note-filters.ts`）；
  // `""` / `"all"` 一律表示**不筛**（不传参数），不复用成「查 id 为空的账号」。
  const [accountFilter, setAccountFilter] = useState<string>("");
  const [noteTypeFilter, setNoteTypeFilter] = useState<NoteTypeFilter>("");
  // 挂链商品筛选（设计 13 §25.8）：平台商品 ID。**惰性从 URL 读初值** ——
  // 挂链笔记弹窗的「在内容库中查看」跳 `/platforms/xhs/library?item_id=…`，
  // 首屏渲染时就要带上它（放 effect 里 setState 会晚一拍，首条请求已发出）。
  // `""` = 不筛；位置在筛选区**第二行**（第一行 span 之和已满 24，§25.7.5）。
  const [searchParams, setSearchParams] = useSearchParams();
  const [itemIdFilter, setItemIdFilter] = useState<string>(
    () => new URLSearchParams(window.location.search).get("item_id") ?? ""
  );
  // 账号下拉候选：**只列本平台的账号**（`fetchAccounts("xhs")`）。
  // 取不到时留空数组 + 不动筛选值 —— 下拉空是「列表没取到」，不是「筛成 0 条」。
  const [filterAccounts, setFilterAccounts] = useState<PlatformAccount[]>([]);
  const [hasAssetsFilter, setHasAssetsFilter] = useState(false);
  const [hasCommentsFilter, setHasCommentsFilter] = useState(false);
  // ⚠️ 2026-09-23 用户口径「内容库默认使用卡片，不再支持表格展示笔记」：
  // `viewMode` / `setViewMode` **整条移除**（不是默认改成 card 留着切换器）——
  // 视图只剩一种，「当前视图」就不再是状态，留着它就是一个永远不变的死状态。
  const [selectedNoteIds, setSelectedNoteIds] = useState<number[]>([]);
  // ⚠️ `batchTagId` / `setBatchTagId` 已随批量打标签的界面入口一起移除
  // （2026-09-23 用户口径「把选择当前页中的标签下拉框移除」）——
  // 那个 state 只服务于被撤掉的下拉，留着就是一个永远不被写入的死状态。
  const [batchActionMessage, setBatchActionMessage] = useState<string | null>(null);
  const [batchFailure, setBatchFailure] = useState<QuietError | null>(null);
  const [isBatchWorking, setIsBatchWorking] = useState(false);
  const [latestExport, setLatestExport] = useState<NotesExportResponse | null>(null);
  const [togglingStatusId, setTogglingStatusId] = useState<number | null>(null);
  // 「下拉自动加载更多」（2026-09-22 用户口径）：列表按页累加，滚到底部自动取下一页。
  // `hasMore` 由「已加载条数 < total」推，不额外问接口（少一次往返，也少一个能漂移的口径）。
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [loadMoreFailure, setLoadMoreFailure] = useState<QuietError | null>(null);

  const selectedNoteIdSet = new Set(selectedNoteIds);

  /**
   * 当前筛选条件（唯一的组装点：加载与「重置」都用它，避免两处口径漂移）。
   *
   * 两个新筛选用**显式判断**而不是 `x || undefined`：账号 id 是**数字**，
   * `0 || undefined` 会把「筛 id 为 0 的账号」静默变成「不筛」（虽然 ids 从 1 起，
   * 但那是数据库的巧合，不该当作逻辑前提）；笔记类型走 `|| undefined` 是因为
   * 它的「不筛」就是空串本身，成立。
   */
  function currentFilters() {
    return {
      q: keywordFilter.trim() || undefined,
      tag_id: selectedTagFilter ? Number(selectedTagFilter) : undefined,
      has_assets: hasAssetsFilter || undefined,
      has_comments: hasCommentsFilter || undefined,
      publish_status: publishStatusFilter === "all" ? undefined : publishStatusFilter,
      platform_account_id: accountFilter ? Number(accountFilter) : undefined,
      note_type: noteTypeFilter || undefined,
      item_id: itemIdFilter.trim() || undefined,
    };
  }

  /**
   * 改挂链商品筛选时**同步 URL query**（§25.8）：清空 = 从 URL 删掉 `item_id`。
   *
   * 为什么必须同步：这个筛选的初值来自 URL（弹窗跳转），只改 state 不改 URL 的话，
   * 用户手动清空后再刷新页面，跳转带来的 `?item_id=` 会把筛选**加回来** ——
   * 「我明明清过了」的经典复现。用 `replace` 不留历史戳。
   */
  function changeItemIdFilter(value: string) {
    setItemIdFilter(value);
    const next = new URLSearchParams(searchParams);
    if (value.trim()) {
      next.set("item_id", value.trim());
    } else {
      next.delete("item_id");
    }
    setSearchParams(next, { replace: true });
  }

  async function loadNotes(overrideFilters?: Partial<ReturnType<typeof currentFilters>>) {
    setIsLoading(true); setError(null);
    const f = overrideFilters ?? currentFilters();
    try {
      // 每次都从第 1 页重新取（筛选变了就是新的一次查询），并重置分页状态。
      const r = await fetchSavedNotes({ platform: "xhs", page: 1, ...f });
      setNotes(r.items); setTotal(r.total); setStats(r.stats);
      setPage(1);
      setHasMore(r.items.length < r.total);
      setLoadMoreFailure(null);
      const ids = new Set(r.items.map((n) => n.id));
      setSelectedNoteIds((c) => c.filter((id) => ids.has(id)));
    } catch { setError("内容库加载失败。"); } finally { setIsLoading(false); }
  }

  /** 取下一页并**追加**到列表末尾（下拉自动加载 / 点「加载更多」都走这里）。 */
  async function loadMoreNotes() {
    // 三重闸：没有更多 / 正在加载 / 没有筛选条件时不允许重复触发。
    // 没有 `isLoadingMore` 这道闸，快速滚动会并发发多个同页请求 ⇒ 列表出现重复行。
    if (!hasMore || isLoadingMore || isLoading) return;
    const next = page + 1;
    setIsLoadingMore(true); setLoadMoreFailure(null);
    try {
      const r = await fetchSavedNotes({ platform: "xhs", page: next, ...currentFilters() });
      setNotes((c) => {
        const seen = new Set(c.map((n) => n.id));
        // 按 id 去重：平台侧若在两页之间插入新行，翻页会重叠（缺值口径：不造假、不重复）。
        const fresh = r.items.filter((n) => !seen.has(n.id));
        return [...c, ...fresh];
      });
      setPage(next);
      setHasMore(notes.length + r.items.length < r.total);
    } catch (e) {
      const { message, code } = readError(e);
      setLoadMoreFailure(quietError(message, code));
    } finally { setIsLoadingMore(false); }
  }

  /** 滚到底部自动取下一页。用 `rootMargin` 提前 240px 触发，避免用户到底了才转圈。 */
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node || !hasMore) return;
    if (typeof IntersectionObserver === "undefined") return;  // 老浏览器降级：手动点「加载更多」
    const observer = new IntersectionObserver(
      (entries) => { if (entries.some((en) => en.isIntersecting)) void loadMoreNotes(); },
      { rootMargin: "240px" }
    );
    observer.observe(node);
    return () => observer.disconnect();
    // `notes.length` 进依赖：追加后 sentinel 位置变化，需要重新观察
  }, [hasMore, page, notes.length, isLoading, isLoadingMore]);

  useEffect(() => { void loadNotes(); void loadTags(); void loadFilterAccounts(); }, []);

  /**
   * 「重置」= **回默认视图**：清空全部筛选**状态**，再按一份**手写**的全空条件重新拉。
   *
   * 为什么不 `loadNotes(currentFilters())`：`setState` 是异步的，刚调的四个 `set*`
   * 在这一刻还没生效，`currentFilters()` 读到的仍是**旧状态** ⇒ 重置后面板空白、
   * 列表却还是筛过的（口径漂移的经典形态）。所以这里显式传一份全空条件，
   * 与 `currentFilters()` 的回默认结果**逐字对应**（新增筛选项时必须同时改这两处）。
   */
  function clearFilters() {
    setKeywordFilter(""); setSelectedTagFilter(""); setPublishStatusFilter("all");
    setAccountFilter(""); setNoteTypeFilter(""); setItemIdFilter("");
    setHasAssetsFilter(false); setHasCommentsFilter(false);
    // URL 里的 `item_id` 一并删（§25.8）：否则刷新会把跳转来的筛选加回来。
    const next = new URLSearchParams(searchParams);
    next.delete("item_id");
    setSearchParams(next, { replace: true });
    void loadNotes({
      q: undefined,
      tag_id: undefined,
      has_assets: undefined,
      has_comments: undefined,
      publish_status: undefined,
      platform_account_id: undefined,
      note_type: undefined,
    });
  }
  function toggleNoteSelection(id: number) { setSelectedNoteIds((c) => c.includes(id) ? c.filter((i) => i !== id) : [...c, id]); }
  function toggleVisibleSelection() { if (!notes.length) return; const vis = notes.map((n) => n.id); const allSel = vis.every((id) => selectedNoteIdSet.has(id)); setSelectedNoteIds((c) => allSel ? c.filter((id) => !vis.includes(id)) : Array.from(new Set([...c, ...vis]))); }
  function clearSelection() { setSelectedNoteIds([]); setBatchActionMessage(null); setBatchFailure(null); }

  async function loadTags() { try { const r = await fetchTags(); setAvailableTags(r.items); } catch { setAvailableTags([]); } }

  /**
   * 账号下拉候选（`GET /accounts?platform=xhs`）—— **只读本平台**。
   *
   * 取不到就留空数组、**不弹错误**：账号列表挂了不该让整个内容库变成错误页，
   * 而账号下拉空着本身已经说明问题（用户仍可按其它条件筛选）。
   * 与 `loadTags` 同一策略：筛选辅助数据失败是**可容忍失败**，不抢主列表的错。
   */
  async function loadFilterAccounts() {
    try { setFilterAccounts(await fetchAccounts("xhs")); } catch { setFilterAccounts([]); }
  }

  async function openDetail(note: SavedNote) {
    setIsDetailOpen(true); setSelectedNote(note); setDetailFailure(null); setDetailActionMessage(null); setIsDetailLoading(true);
    try {
      // **只读本地库**（2026-09-22 用户口径）：详情与资产都不打平台。
      // 曾在这里按 `!note.content` 走 `?hydrate=true` 让后端当场补正文，
      // 但那条路径的 commit 会与同步任务抢 SQLite 写锁 ⇒ `database is locked`
      // ⇒ 未捕获 ⇒ 用户看到「内容库笔记详情 500」。补齐只由同步链路做。
      const [d, a] = await Promise.all([fetchSavedNote(note.id), fetchSavedNoteAssets(note.id)]);
      setSelectedNote(d); setSelectedAssets(a.items);
    } catch (e) {
      // 详情取不到**不吞原因**：三分法里「预期内」安静一行、「真异常」红块摆后端原话。
      const { message, code } = readError(e);
      setDetailFailure(quietError(message, code));
    } finally { setIsDetailLoading(false); }
  }
  function closeDetail() { setIsDetailOpen(false); setDetailFailure(null); setDetailActionMessage(null); setSelectedAssets([]); setDetailSyncFailure(null); setTopicPopoverOpen(false); setTopicDraft(""); setTopicFailure(null); }

  /** 重新读一次本地详情（弹窗右上角「刷新」）。**不打平台** —— 只重读本地行。 */
  async function refreshDetail() {
    if (!selectedNote) return;
    setIsDetailLoading(true); setDetailFailure(null);
    try {
      const [d, a] = await Promise.all([fetchSavedNote(selectedNote.id), fetchSavedNoteAssets(selectedNote.id)]);
      setSelectedNote(d); setSelectedAssets(a.items);
    } catch (e) {
      const { message, code } = readError(e);
      setDetailFailure(quietError(message, code));
    } finally { setIsDetailLoading(false); }
  }

  /**
   * 切换发布状态「待发布 ⇄ 已发布」（设计 13 §19.12 / §32.3.3）。
   *
   * 写的是**唯一发布状态字段** `publish_status`（后端 `published` 是它的投影，
   * 由后端同一次写入维护）。**本地生成笔记不走这里**（后端 400
   * `NOTE_STATUS_MANAGED`）：它的状态由发布链路决定，界面也不给切换入口。
   *
   * 顺手重取列表：`stats` 会随这一列变化，只用响应里那一行更新会把统计卡留在旧值
   * （「改完之后卡片数字不动」= 用户会以为没生效）。
   */
  async function togglePublishStatus(note: SavedNote, next: "pending" | "published") {
    setTogglingStatusId(note.id); setBatchActionMessage(null); setBatchFailure(null);
    try {
      const updated = await updateSavedNotePublishStatus(note.id, next);
      setNotes((c) => c.map((n) => (n.id === updated.id ? { ...n, ...updated, tags: updated.tags ?? n.tags } : n)));
      if (selectedNote?.id === updated.id) setSelectedNote((current) => current ? { ...current, ...updated, tags: updated.tags ?? current.tags } : current);
      setBatchActionMessage(`已标记为「${updated.publish_status === "published" ? "已发布" : "待发布"}」。`);
      await loadNotes();
    } catch (e) {
      const { message, code } = readError(e);
      setBatchFailure(quietError(message, code));
    } finally { setTogglingStatusId(null); }
  }

  async function copySelectedNote() {
    if (!selectedNote) return;
    try { await navigator.clipboard.writeText(`${selectedNote.title}\n\n${selectedNote.content}`.trim()); setDetailActionMessage("已复制标题和正文。"); }
    catch { setDetailActionMessage("复制失败。"); }
  }

  /**
   * 「同步」按钮（设计 13 §25）：用笔记 id 拉平台最新内容，更新落库。
   * **笔记卡片与详情卡共用**这一个实现（同一语义不留两份）。
   *
   * 与定时任务同步笔记逻辑同源（后端同一个 `hydrate_note_detail` 实现）。
   * 成功 ⇒ 就地替换列表行（详情开着的连详情行一起换）+ 一行说明（补到什么
   * 说什么；什么都没变明说「平台未返回新内容」，不伪装成已更新）；
   * 失败 ⇒ 失败三分法（详情开着的在弹窗里摆，否则用页面级提示）。
   */
  async function syncNote(note: SavedNote) {
    setSyncingNoteId(note.id);
    setDetailActionMessage(null); setDetailSyncFailure(null);
    setBatchActionMessage(null); setBatchFailure(null);
    try {
      const r = await syncSavedNote(note.id);
      replaceNoteInList(r.note);
      if (selectedNote?.id === r.note.id) setSelectedNote(r.note);
      const s = r.sync;
      const name = `「${r.note.title || "未命名"}」`;
      const parts: string[] = [];
      if (s.content_updated) parts.push("正文");
      if (s.images_added) parts.push(`图片 +${s.images_added}`);
      if (s.video_added) parts.push("视频地址");
      const inDetail = isDetailOpen && selectedNote?.id === r.note.id;
      if (s.status === "ok") {
        const text = parts.length
          ? `已同步${name}最新内容（${parts.join("、")}）。`
          : `${name}已同步，平台未返回新内容。`;
        if (inDetail) setDetailActionMessage(text); else setBatchActionMessage(text);
      } else {
        const failure = quietError(s.reason, s.code);
        if (inDetail) setDetailSyncFailure(failure); else setBatchFailure(failure);
      }
    } catch (e) {
      // 真异常（含非 synced 的结构化 400）：摆真实原因，不吞。
      const { message, code } = readError(e);
      const failure = quietError(message, code);
      if (isDetailOpen && selectedNote?.id === note.id) setDetailSyncFailure(failure);
      else setBatchFailure(failure);
    } finally { setSyncingNoteId(null); }
  }

  /**
   * 某篇笔记**能不能发布**（设计 13 §31）：本地生成 + 待发布/发布失败。
   * 判据只此一处（`lib/note-publish-gates.ts`，与后端常量对齐）——
   * 卡片 / 详情 / 批量共用，不留第二份。
   */
  function canPublishNote(note: SavedNote): boolean {
    return canPublish(note.note_type, note.publish_status);
  }

  /**
   * 「发布」按钮（设计 13 §31.5）：**先二次确认**再发请求（用户口径）。
   *
   * 链路（后端做）：下载媒体到本地临时文件夹 → 上传平台 → 平台预审核 → 发布 →
   * 回写平台真实 note_id → 删临时目录。失败也回 200：`publish.ok=false` +
   * 平台原文原因，卡片改「发布失败」态、详情弹窗摆原因，支持重发。
   */
  function publishNoteWithConfirm(note: SavedNote) {
    Modal.confirm({
      title: "发布这篇笔记？",
      content: `将把《${note.title || "未命名"}》发布到小红书并挂上对应商品链接。发布成功后状态更新为已发布，本地临时下载的媒体会自动清理。`,
      okText: "确认发布",
      cancelText: "取消",
      onOk: async () => {
        await publishNote(note);
      },
    });
  }

  async function publishNote(note: SavedNote) {
    setPublishingNoteId(note.id);
    setDetailActionMessage(null);
    setBatchActionMessage(null);
    setBatchFailure(null);
    try {
      const r = await publishSavedNote(note.id);
      replaceNoteInList(r.note);
      if (selectedNote?.id === r.note.id) setSelectedNote(r.note);
      const name = `「${r.note.title || "未命名"}」`;
      const inDetail = isDetailOpen && selectedNote?.id === r.note.id;
      if (r.publish.ok) {
        const text = `${name}已发布（平台笔记 ID ${r.publish.platform_note_id}）。`;
        if (inDetail) setDetailActionMessage(text); else setBatchActionMessage(text);
        await loadNotes();
      } else {
        const text = `${name}发布失败：${r.publish.error || "平台未给原因"}（可在详情里重发）`;
        if (inDetail) setDetailActionMessage(text); else setBatchActionMessage(text);
      }
    } catch (e) {
      // 真异常（含门禁类结构化 400）：摆真实原因，不吞。
      const { message } = readError(e);
      if (isDetailOpen && selectedNote?.id === note.id) setDetailActionMessage(`发布失败：${message}`);
      else setBatchActionMessage(`发布失败：${message}`);
    } finally {
      setPublishingNoteId(null);
    }
  }

  /** 批量发布（设计 13 §31.5 步 8）：只发选中里**可发布**的，跳过的明示。 */
  function batchPublishSelected() {
    const { targets, skipped } = splitPublishTargets(notes, selectedNoteIds);
    if (!targets.length) {
      setBatchActionMessage("选中的笔记里没有可发布的（待发布/发布失败的本地生成笔记才可发布）。");
      return;
    }
    Modal.confirm({
      title: `批量发布 ${targets.length} 篇笔记？`,
      content: `逐篇发布到小红书（下载媒体 → 上传 → 预审核 → 发布），单项失败不阻断。${
        skipped > 0 ? `另有 ${skipped} 篇选中项不可发布，将跳过。` : ""
      }`,
      okText: "确认发布",
      cancelText: "取消",
      onOk: async () => {
        setIsBatchPublishing(true);
        setBatchActionMessage(null);
        setBatchFailure(null);
        try {
          const r = await batchPublishSavedNotes(targets.map((n) => n.id));
          // 逐篇结果回填（按本地主键 note_pk 匹配）：成功的换平台 ID、
          // 失败的标发布失败（临时目录保留可重发）。
          for (const item of r.results) {
            if (item.ok) {
              setNotes((c) =>
                c.map((n) =>
                  n.id === item.note_pk
                    ? {
                        ...n,
                        note_id: item.platform_note_id || n.note_id,
                        // 状态是唯一源；`published` 只是它的投影，这里一起更新免得
                        // 就地替换的行与后端投影短暂不一致（下次刷新即覆盖为后端值）。
                        publish_status: "published",
                        published: true,
                        publish_error: "",
                        published_at: item.published_at || null,
                      }
                    : n
                )
              );
            } else {
              setNotes((c) =>
                c.map((n) =>
                  n.id === item.note_pk
                    ? { ...n, publish_status: "failed", publish_error: item.error || "" }
                    : n
                )
              );
            }
          }
          setBatchActionMessage(
            `批量发布完成：成功 ${r.succeeded} 篇，失败 ${r.failed} 篇。失败的可在卡片上重发。`
          );
          await loadNotes();
        } catch (e) {
          const { message } = readError(e);
          setBatchFailure(quietError(message, ""));
        } finally {
          setIsBatchPublishing(false);
        }
      },
    });
  }

  async function createDraft(intent: "rewrite" | "publish") {
    if (!selectedNote) return; setIsCreatingDraft(true); setDetailActionMessage(null);
    try {
      const d = await createDraftFromNote({ platform: "xhs", source_note_id: selectedNote.id, intent });
      setDetailActionMessage(`已创建草稿 #${d.id}，正在跳转...`);
      setTimeout(() => navigate("/platforms/xhs/drafts"), 600);
    }
    catch { setDetailActionMessage("草稿创建失败。"); } finally { setIsCreatingDraft(false); }
  }

  async function addToDrafts() {
    if (!selectedNote) return; setIsCreatingDraft(true); setDetailActionMessage(null);
    try {
      const d = await createDraftFromNote({ platform: "xhs", source_note_id: selectedNote.id, intent: "rewrite" });
      setDetailActionMessage(`已加入草稿工坊，草稿 #${d.id}。`);
    }
    catch { setDetailActionMessage("加入草稿工坊失败。"); } finally { setIsCreatingDraft(false); }
  }

  async function handleDeleteNote(note: SavedNote) {
    Modal.confirm({
      title: "确定删除？", content: "相关素材、评论和标签关系也会一起删除。",
      onOk: async () => {
        try {
          await deleteSavedNote(note.id);
          setNotes((c) => c.filter((n) => n.id !== note.id));
          setSelectedNoteIds((c) => c.filter((id) => id !== note.id));
          setTotal((c) => Math.max(0, c - 1));
          if (selectedNote?.id === note.id) closeDetail();
          setBatchActionMessage("已删除笔记。");
          // 统计卡跟着删掉的那一条走（已发布/待发布都可能是它）。
          await loadNotes();
        } catch { setBatchActionMessage("删除失败。"); }
      },
    });
  }

  function replaceNoteInList(u: SavedNote) { setNotes((c) => c.map((n) => n.id === u.id ? u : n)); }
  function replaceNotesInList(us: SavedNote[]) { const m = new Map(us.map((n) => [n.id, n])); setNotes((c) => c.map((n) => m.get(n.id) ?? n)); if (selectedNote) { const u = m.get(selectedNote.id); if (u) setSelectedNote(u); } }

  // ⚠️ `applyBatchTag`（批量打标签）已随界面入口一起移除（2026-09-23 用户口径
  // 「把选择当前页中的标签下拉框移除」）：它依赖被撤掉的下拉值 `batchTagId`，
  // 没有入口就不可能被调用。**`batchTagNotes` 与后端端点保留** —— 详情弹窗里
  // 给**单条**笔记增删标签走的正是它（见 `addTagToDetail` / `removeTagFromDetail`）。
  // 原实现（保留备查）：
  //   async function applyBatchTag(mode: "add" | "remove") {
  //     if (!selectedNoteIds.length) { setBatchActionMessage("请先选择笔记。"); return; }
  //     if (!batchTagId) { setBatchActionMessage("请选择一个标签。"); return; }
  //     setIsBatchWorking(true); setBatchActionMessage(null); setBatchFailure(null);
  //     try { const r = await batchTagNotes({ note_ids: selectedNoteIds, tag_ids: [Number(batchTagId)], mode }); replaceNotesInList(r.items); setBatchActionMessage(mode === "add" ? `已添加标签 (${r.updated_count})` : `已移除标签 (${r.updated_count})`); }
  //     catch (e) { const { message, code } = readError(e); setBatchFailure(quietError(message, code)); } finally { setIsBatchWorking(false); }
  //   }

  async function createBatchRewriteDrafts() {
    if (!selectedNoteIds.length) { setBatchActionMessage("请先选择笔记。"); return; }
    setIsBatchWorking(true); setBatchActionMessage(null); setBatchFailure(null);
    try { const r = await batchCreateDraftsFromNotes({ note_ids: selectedNoteIds, intent: "rewrite" }); setBatchActionMessage(`已创建 ${r.created_count} 个改写草稿。`); }
    catch (e) { const { message, code } = readError(e); setBatchFailure(quietError(message, code)); } finally { setIsBatchWorking(false); }
  }

  async function exportSelectedNotes(format: "json" | "csv") {
    if (!selectedNoteIds.length) { setBatchActionMessage("请先选择笔记。"); return; }
    setIsBatchWorking(true); setBatchActionMessage(null); setBatchFailure(null);
    try { const r = await exportSavedNotes({ note_ids: selectedNoteIds, format }); setLatestExport(r); setBatchActionMessage(`已导出 ${r.exported_count} 条笔记。`); }
    catch (e) { const { message, code } = readError(e); setBatchFailure(quietError(message, code)); } finally { setIsBatchWorking(false); }
  }

  async function batchDeleteNotes() {
    if (!selectedNoteIds.length) return;
    setIsBatchWorking(true); setBatchActionMessage(null); setBatchFailure(null);
    try {
      for (const id of selectedNoteIds) {
        await deleteSavedNote(id);
      }
      setNotes((c) => c.filter((n) => !selectedNoteIds.includes(n.id)));
      setTotal((c) => Math.max(0, c - selectedNoteIds.length));
      setBatchActionMessage(`已删除 ${selectedNoteIds.length} 条笔记。`);
      setSelectedNoteIds([]);
      await loadNotes();
    } catch (e) { const { message, code } = readError(e); setBatchFailure(quietError(message, code)); }
    finally { setIsBatchWorking(false); }
  }

  async function downloadLatestExport() {
    if (!latestExport) return; setIsBatchWorking(true); setBatchActionMessage(null);
    try { await downloadExportFile(latestExport.download_url, latestExport.file_name); setBatchActionMessage(`已下载：${latestExport.file_name}`); }
    catch { setBatchActionMessage("下载失败。"); } finally { setIsBatchWorking(false); }
  }

  // --------------------------------------------------------------------- //
  // 标签增删（设计 13 §29.7）：标题后展示已有标签，「+ 添加标签」Popover 两条路
  // （下拉选已创建的 / 输入自定义新建）。全部走 `note_tags` 关联表 + `tags` 实体表，
  // 每次操作一次写库（实时落库），返回更新后的整行就地替换。
  // --------------------------------------------------------------------- //
  /** 打一枚已有标签（下拉选中的那条）。 */
  async function addTagToNote(tagId: number) {
    if (!selectedNote) return;
    setTagSaving(true); setTagFailure(null);
    try {
      const r = await batchTagNotes({ note_ids: [selectedNote.id], tag_ids: [tagId], mode: "add" });
      const u = r.items[0]; setSelectedNote(u); replaceNoteInList(u);
      setTagPopoverOpen(false);
    } catch (e) {
      // 失败三分法：预期内安静一行、真异常红块摆后端原话（DEF-058）。
      const { message, code } = readError(e);
      setTagFailure(quietError(message, code));
    } finally { setTagSaving(false); }
  }

  /** 去掉一枚标签（Tag 上的 ×）。删成功才消失，失败保持原样。 */
  async function removeTagFromNote(tagId: number) {
    if (!selectedNote) return;
    setTagSaving(true); setTagFailure(null);
    try {
      const r = await batchTagNotes({ note_ids: [selectedNote.id], tag_ids: [tagId], mode: "remove" });
      const u = r.items[0]; setSelectedNote(u); replaceNoteInList(u);
    } catch (e) {
      const { message, code } = readError(e);
      setTagFailure(quietError(message, code));
    } finally { setTagSaving(false); }
  }

  /** 新建一枚自定义标签并打上（下拉+输入合一组件里回车走这条，§29.10）。 */
  async function createAndAddCustomTag(name: string) {
    const trimmed = name.trim();
    if (!trimmed || !selectedNote) return;
    setTagSaving(true); setTagFailure(null);
    try {
      const created = await createTag({ name: trimmed, color: "#111111" });
      setAvailableTags((t) => [...t, created]);
      const r = await batchTagNotes({ note_ids: [selectedNote.id], tag_ids: [created.id], mode: "add" });
      const u = r.items[0]; setSelectedNote(u); replaceNoteInList(u);
      setCustomTagName(""); setTagPopoverOpen(false);
    } catch (e) {
      const { message, code } = readError(e);
      setTagFailure(quietError(message, code));
    } finally { setTagSaving(false); }
  }

  /**
   * 下拉+输入**合一**组件的提交（§29.10，用户口径「添加标签弹窗中得组件使用一个下拉
   * 支持输入得组件」）：输入框里的名字——
   * ① 命中**已打在这条笔记上**的标签 ⇒ 无事可做（关掉即可，不重复请求）；
   * ② 命中**可添加**的已有标签 ⇒ 直接打上（`addTagToNote`，后端 add 幂等）；
   * ③ 都不命中 ⇒ 新建后打上（`createAndAddCustomTag`）。
   * 显式收 name 参数：`onSelect` 触发时 state 还没更新，读闭包会拿到旧值。
   */
  async function submitTagDraft(name: string) {
    const trimmed = name.trim();
    if (!trimmed || !selectedNote) return;
    if ((selectedNote.tags ?? []).some((t) => t.name === trimmed)) {
      setCustomTagName(""); setTagPopoverOpen(false); return;
    }
    const existing = addableTags.find((t) => t.name === trimmed);
    if (existing) { await addTagToNote(existing.id); setCustomTagName(""); return; }
    await createAndAddCustomTag(trimmed);
  }

  // 下拉里只列**还没打在这条笔记上**的标签（打了的再列一次没有意义）。
  const addableTags = availableTags.filter(
    (t) => !(selectedNote?.tags ?? []).some((n) => n.id === t.id)
  );

  // --------------------------------------------------------------------- //
  // 话题增删（设计 13 §29.3）：Tag 展示，增删都**实时落库**。
  // 整列替换语义 —— 传增删后的完整列表给后端，不猜加了哪个删了哪个。
  // --------------------------------------------------------------------- //
  async function saveTopics(next: string[]) {
    if (!selectedNote) return;
    setTopicSaving(true); setTopicFailure(null);
    try {
      const updated = await updateSavedNoteTopics(selectedNote.id, next);
      setSelectedNote(updated);
      replaceNoteInList(updated);
    } catch (e) {
      // 失败三分法：预期内安静一行、真异常红块摆后端原话（DEF-058）。
      const { message, code } = readError(e);
      setTopicFailure(quietError(message, code));
    } finally { setTopicSaving(false); }
  }

  /** 删一个话题：Tag 上的 ×。其余原样传回（顺序保持）。 */
  function removeTopic(topic: string) {
    if (!selectedNote) return;
    void saveTopics((selectedNote.topics ?? []).filter((t) => t !== topic));
  }

  /** 加一个话题：「+」弹出框里回车/点确定。空值忽略；已存在则不重复请求。 */
  function submitTopicDraft() {
    const name = topicDraft.trim();
    if (!name || !selectedNote) return;
    const current = selectedNote.topics ?? [];
    if (current.includes(name)) { setTopicPopoverOpen(false); setTopicDraft(""); return; }
    setTopicPopoverOpen(false); setTopicDraft("");
    void saveTopics([...current, name]);
  }

  // --------------------------------------------------------------------- //
  // 弹窗媒体：**优先本地已下载素材**（内容库的东西已经躺在磁盘上，
  // 没有理由再去打平台 CDN），空时回退封面 / 直链。
  // --------------------------------------------------------------------- //
  const selectedImageAssets = selectedAssets.filter((a) => a.asset_type !== "video");
  const selectedVideoAsset = selectedAssets.find((a) => a.asset_type === "video");
  const detailMediaUrls = selectedImageAssets.length
    ? selectedImageAssets.map((a) => a.url || a.download_url || "")
    : (selectedNote?.asset_urls ?? []).filter(Boolean);
  const detailVideoUrl = selectedVideoAsset?.url || selectedNote?.video_url || selectedNote?.video_addr || "";
  // 是不是视频笔记：平台 `type` 原值在 `raw_json` 里（列表与详情同一口径）。
  const selectedNoteIsVideo = getRawNoteType(selectedNote ?? ({} as SavedNote)).includes("video");
  // 对账标记（§24）：平台列表里已经没有这条笔记。**不删行**，标记可自愈。
  const platformMissingHint = selectedNote?.platform_missing_at
    ? `平台已无此笔记（对账标记于 ${formatShanghaiTime(selectedNote.platform_missing_at)}）`
    : null;
  const hasLocalAssets = selectedAssets.some((a) => Boolean(a.local_path));

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 24 }}>
        <Col><Title level={4} style={{ margin: 0 }}>内容库</Title><Text type="secondary">保存的笔记素材，支持标签、筛选、批量操作和导出</Text></Col>
        <Col><Button icon={<ReloadOutlined />} onClick={() => void loadNotes()} loading={isLoading}>刷新</Button></Col>
      </Row>

      {/*
        统计卡：**已发布 / 待发布 / 发布失败**（内容池结构这一眼）+ 已保存 + 已选择。
        口径是**全库**（`stats` 来自后端，只按 user+platform 统计）—— 不随筛选变化，
        也不由 `items.length` 反推。设计 13 §32.3.3：三档与筛选同一份映射。
      */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}><Card size="small"><Statistic title="已发布" value={stats.published} valueStyle={{ color: "#52c41a" }} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="待发布" value={stats.pending} valueStyle={{ color: "#fa8c16" }} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="发布失败" value={stats.failed} valueStyle={{ color: "#ff4d4f" }} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="已保存笔记" value={total} suffix={`/ 已选 ${selectedNoteIds.length}`} /></Card></Col>
      </Row>

      {/*
        搜索条件：**恰好两行**（2026-09-23 用户口径「内容库的搜索区域重新布局，
        布局为两行展示」，与商品列表页 §33.8 的同一口径对齐）。

        改之前是**一行硬排 9 项**（5+3+3+4+3+2+2+2+2=26 格 > 24）：`span` 之和溢出，
        `Col` 挤到第二行去，而按钮又被 `flex` 挤到半行空位 —— 观感是「一行没排完、
        第二行只有两个按钮」，最该显眼的「筛选」按钮反而不在视线落点上。

        现在按**语义**切两行（不是按数量塞满）：

        * 第一行 = **定位条件**：关键词（宽）+ 来源账号 + 笔记类型 + 标签 + 发布状态
          ⇒ 这五项决定「我要看哪一批」；
        * 第二行 = **附加开关 + 动作**：有素材 / 有评论｜按钮组**右对齐收尾**
          （与 §33.8 商品列表页同款：按钮永远在最后）。
          ⚠️ 原「卡片表格切换」已随表格视图一起移除（2026-09-23 用户口径
          「内容库默认使用卡片，不再支持表格展示笔记」）。
      */}
      <Card size="small" style={{ marginBottom: 16 }}>
        {/* 第一行：关键词(6) + 账号(4) + 类型(4) + 标签(5) + 发布状态(5) = 24 */}
        <Row gutter={12} align="middle" style={{ marginBottom: 12 }}>
          <Col span={6}><Input placeholder="标题、正文、作者" value={keywordFilter} onChange={(e) => setKeywordFilter(e.target.value)} allowClear /></Col>
          {/*
            来源账号 + 笔记类型（2026-09-23，用户口径「内容库新增根据账号筛选的条件」+「笔记类型」）。
            两个下拉都是**枚举筛选**：`undefined` / `""` = 不筛（不传参数），
            不能兜成「筛 id 为空的账号」—— 那是个永远查不到东西的假筛选。
            账号候选来自 `GET /accounts?platform=xhs`；取不到时下拉空着（不阻断其它筛选）。
          */}
          <Col span={4}>
            <Select
              value={accountFilter || undefined}
              onChange={(v) => setAccountFilter(v ?? "")}
              placeholder="全部账号"
              allowClear
              style={{ width: "100%" }}
              options={filterAccounts.map((account) => ({ value: String(account.id), label: accountFilterLabel(account) }))}
            />
          </Col>
          <Col span={4}>
            <Select
              value={noteTypeFilter}
              onChange={(v) => setNoteTypeFilter((v ?? "") as NoteTypeFilter)}
              style={{ width: "100%" }}
              options={NOTE_TYPE_FILTER_OPTIONS}
            />
          </Col>
          <Col span={5}><Select value={selectedTagFilter || undefined} onChange={(v) => setSelectedTagFilter(v ?? "")} placeholder="全部标签" allowClear style={{ width: "100%" }} options={availableTags.map((t) => ({ value: String(t.id), label: t.name }))} /></Col>
          {/* 发布状态四档（含「发布失败」，设计 13 §32.3.3） */}
          <Col span={5}><Select value={publishStatusFilter} onChange={(v) => setPublishStatusFilter(v as PublishStatusFilter)} style={{ width: "100%" }} options={PUBLISH_STATUS_FILTER_OPTIONS} /></Col>
        </Row>
        {/* 第二行：开关（左）｜按钮组（右对齐收尾）。

            ⚠️ 2026-09-23 用户口径「内容库默认使用卡片，不再支持表格展示笔记」：
            「卡片 / 表格」`Segmented` **整块移除** —— 视图只剩一种，
            切换器就没有可切换的东西（留着一个永远停在「卡片」的开关 = 死控件）。 */}
        <Row gutter={12} align="middle" justify="space-between">
          <Col>
            <Space size={16} wrap>
              <Checkbox checked={hasAssetsFilter} onChange={(e) => setHasAssetsFilter(e.target.checked)}>有素材</Checkbox>
              <Checkbox checked={hasCommentsFilter} onChange={(e) => setHasCommentsFilter(e.target.checked)}>有评论</Checkbox>
              {/*
                挂链商品筛选（设计 13 §25.8，2026-09-23）：按平台商品 ID 反查
                「挂了这件商品的笔记」。放在**第二行**而不是第一行 —— 第一行
                span 之和已满 24（关键词6+账号4+类型4+标签5+状态5），加一项
                就三行（§25.7.5 两行纪律）。普通输入框而不是下拉：商品有上千个，
                下拉没有意义；主入口是挂链弹窗的「在内容库中查看」跳转
                （带 `?item_id=` 直达），手工输入是兜底。
              */}
              <Input
                value={itemIdFilter}
                onChange={(e) => changeItemIdFilter(e.target.value)}
                placeholder="挂链商品 ID"
                allowClear
                onClear={() => changeItemIdFilter("")}
                style={{ width: 220 }}
              />
            </Space>
          </Col>
          <Col>
            <Space size={8}>
              <Button onClick={clearFilters}>重置</Button>
              <Button type="primary" onClick={() => void loadNotes()} loading={isLoading}>筛选</Button>
            </Space>
          </Col>
        </Row>
      </Card>

      {notes.length > 0 && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Col span={24}>
            <Space wrap>
              <Checkbox checked={notes.length > 0 && notes.every((n) => selectedNoteIdSet.has(n.id))} onChange={toggleVisibleSelection}>选择当前页</Checkbox>
              <Text strong>{selectedNoteIds.length} 条已选</Text>
              <Button icon={<CheckSquareOutlined />} disabled={isBatchWorking || !selectedNoteIds.length} onClick={createBatchRewriteDrafts} size="small">批量加入草稿工坊</Button>
              {/* 批量发布（设计 13 §31.5 步 8）：只发选中里可发布的（本地生成待发布/发布失败），
                  跳过的在确认框里明示；单项失败不阻断。 */}
              <Tooltip title="把选中里待发布 / 发布失败的本地生成笔记发布到小红书（逐篇处理，单项失败不阻断）">
                <Button
                  type="primary"
                  icon={<SendOutlined />}
                  disabled={isBatchWorking || isBatchPublishing || !selectedNoteIds.length}
                  loading={isBatchPublishing}
                  onClick={() => void batchPublishSelected()}
                  size="small"
                >
                  批量发布
                </Button>
              </Tooltip>
              {/* 批量打标签的**界面入口已移除**（2026-09-23 用户口径
                  「把选择当前页中的标签下拉框移除」）。

                  原控件 = 「选择标签」下拉 + 「加标签」/「去标签」两枚按钮，三者是
                  **一个功能的三件套**：没有下拉就选不到标签，两枚按钮也就永远 disabled。
                  所以只删下拉、留下两枚死按钮是不行的 —— 三件套一起撤。

                  ⚠️ **后端与单条能力都没动**：`POST /notes/batch-tag` 端点、
                  `batchTagNotes()`（api.ts）、以及**详情弹窗里给单条笔记增删标签**的
                  入口全部保留 —— 撤的只是「批量操作卡里那个下拉」，不是打标签能力本身。
                  （用户要批量打标签时，走详情弹窗逐条加，或以后用别的形态接回来。）

                  原注释（保留备查）：「批量打标签：`batchTagNotes` 早就有了、
                  `applyBatchTag` 也早就写好了，但一直没有界面入口 —— 属于
                  『写好了没接线』，本轮补上。」⇒ **那一轮补的入口，本轮按用户口径撤掉。** */}
              <Button type="primary" icon={<DownloadOutlined />} disabled={isBatchWorking || !selectedNoteIds.length} onClick={() => exportSelectedNotes("json")} size="small">JSON</Button>
              <Button icon={<DownloadOutlined />} disabled={isBatchWorking || !selectedNoteIds.length} onClick={() => exportSelectedNotes("csv")} size="small">CSV</Button>
              {latestExport && <Button icon={<DownloadOutlined />} disabled={isBatchWorking} onClick={downloadLatestExport} size="small">下载</Button>}
              <Popconfirm title={`确定删除选中的 ${selectedNoteIds.length} 条笔记？`} onConfirm={batchDeleteNotes}>
                <Button danger icon={<DeleteOutlined />} disabled={isBatchWorking || !selectedNoteIds.length} size="small">批量删除</Button>
              </Popconfirm>
              <Button disabled={!selectedNoteIds.length} onClick={clearSelection} size="small">清空选择</Button>
            </Space>
          </Col>
          {batchActionMessage && <Alert message={batchActionMessage} type="info" showIcon style={{ marginTop: 8 }} closable onClose={() => setBatchActionMessage(null)} />}
          {batchFailure && <div style={{ marginTop: 8 }}>{renderFailure(batchFailure, "批量操作失败")}</div>}
        </Card>
      )}

      {error && <Alert message={error} type="error" showIcon style={{ marginBottom: 16 }} />}

      {/*
        挂链商品筛选生效时的**提示条**（设计 13 §25.8）：输入框里只有一个原始
        商品 ID，不说明「当前列表为什么是这批」。这条把三件事一次讲清 ——
        在按哪个商品筛、命中多少篇（与商品列表「N 篇」/ 挂链弹窗同一口径，
        都是 note_goods 关系表）、怎么退出。没有它，用户看到空列表时无法
        区分「这个商品真没挂笔记」和「筛选条件写错了」。
      */}
      {itemIdFilter.trim() && (
        <div style={{ marginBottom: 16 }}>
          <Tag color="blue" style={{ marginRight: 8 }}>挂链商品 {itemIdFilter.trim()}</Tag>
          <Text type="secondary" style={{ marginRight: 8 }}>共 {total} 篇</Text>
          <Button type="link" size="small" style={{ padding: 0 }} onClick={() => changeItemIdFilter("")}>
            清除该筛选
          </Button>
        </div>
      )}

      {/*
        ⚠️ 2026-09-23 用户口径「内容库默认使用卡片，不再支持表格展示笔记」：
        **表格分支整条移除**（原先这里是 `viewMode === "table" ? <Table…/> : <Row 卡片…>`）。
        原表格视图的说明（`total` 报已加载条数、分页交给 antd）随分支一起删，
        需要时从版本历史取回 —— 这里不保留「注释掉的死代码」。
        卡片视图本身不变：`Row gutter` 网格 + `Col` 卡片 + 哨兵 + LoadMoreBar。
      */}
      {isLoading ? <Spin size="large" style={{ display: "block", textAlign: "center", margin: "48px 0" }} /> : notes.length === 0 ? (
        <Empty description={itemIdFilter.trim() ? "这个商品在内容库里还没有挂链笔记（可能是未同步，也可能确实没挂）" : "内容库还是空的"}>
          {/* 「去发现笔记」只对**真的空库**有意义；筛选无结果时它是误导入口 */}
          {!itemIdFilter.trim() && <Link to="/platforms/xhs/discovery"><Button type="primary" icon={<BookOutlined />}>去发现笔记</Button></Link>}
        </Empty>
      ) : (
        <Row gutter={[16, 16]}>
          {notes.map((note) => {
            const cover = getSavedNoteCoverUrl(note);
            const kind = getRawNoteType(note);
            return (
              <Col xs={12} sm={8} md={6} lg={4} xl={4} key={note.id}>
                {/*
                  类型呈现的三轮演变（2026-09-23，用户口径，**全部保留**）：

                  ① 「卡片上不展示了，鼠标放到卡片上显示标签即可」⇒ 类型只在
                     卡片级 Tooltip 里，封面左下角那枚 Tag 移除；
                  ② 「笔记卡片上应展示本地生成或三方同步」⇒ 类型改成**正文文本**；
                  ③ 「三方同步/本地生成使用 tag 放图片右下角」+ 两句长文案移除
                     ⇒ 类型改成**封面右下角的 Tag**，**直接可见**。

                  ⚠️ ③ 之后**卡片级 Tooltip 整条移除**：类型已经直接可见，
                  悬停提示只剩「复读」提示文字和 Tag 一字不差的内容，属于纯噪声。
                  原两句长文案（「…账号同步链路落库，可拉取平台最新内容」/
                  「…由商品智能生成，待发布 / 发布失败可重发」）随 Tooltip 一起删，
                  `noteTypeTooltip` 函数已从 `lib/note-filters.ts` 移除。

                  ⚠️ ③ 也**不是**把 ① 推翻回去：① 取消的是「封面**左下角**那枚 Tag」
                  （与「图文/视频」同排易混、且和左下角的按钮挤在一起）；
                  ③ 要的是「封面**右下角**」—— 那个位置本来是空的，不与任何元素争。
                */}
                <Card hoverable size="small" style={{ overflow: "hidden" }} onClick={() => void openDetail(note)}
                  cover={
                    <div style={{ position: "relative", background: "#262626" }}>
                      <Checkbox checked={selectedNoteIdSet.has(note.id)} onClick={(e) => { e.stopPropagation(); toggleNoteSelection(note.id); }} style={{ position: "absolute", top: 8, left: 8, zIndex: 2 }} />
                      {cover ? <img src={cover} alt={note.title} referrerPolicy="no-referrer" style={{ width: "100%", aspectRatio: "1/1", objectFit: "cover", display: "block" }} /> : <div style={{ width: "100%", aspectRatio: "1/1", display: "flex", alignItems: "center", justifyContent: "center", color: "rgba(255,255,255,.2)", fontSize: 28 }}><PictureOutlined /></div>}
                      <Tag color={kind.includes("video") ? "purple" : "blue"} style={{ position: "absolute", top: 8, right: 8 }} icon={kind.includes("video") ? <PlayCircleOutlined /> : <PictureOutlined />}>{kind.includes("video") ? "视频" : "图文"}</Tag>
                      {/*
                        封面左下角一排（§29.5）：发布状态（可点切换）→ 同步按钮
                        （仅 synced / 已发布）→ 发布按钮（仅本地生成待发布/发布失败，
                        设计 13 §31）。

                        2026-09-23 三轮演变后这里的**最终形态**：
                        * 类型 Tag **不在这里**（① 把它移出去了，③ 放到了右下角）；
                        * `flexWrap` 兜窄卡；两枚按钮都带 stopPropagation ——
                          点同步 / 发不顺带打开详情；
                        * ⚠️ `maxWidth` 从 `calc(100% - 16px)` 收成
                          `calc(100% - 84px)`：右下角多了类型 Tag，左下这排再占满宽
                          就会**压到它**（两块都是 absolute + zIndex 2，谁后渲染谁在上）。
                      */}
                      <div style={{ position: "absolute", left: 8, bottom: 8, zIndex: 2, display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap", maxWidth: "calc(100% - 84px)" }}>
                        <PublishStatusTag
                          note={note}
                          loading={togglingStatusId === note.id}
                          onToggle={(next) => void togglePublishStatus(note, next)}
                        />
                        {canSyncNote(note) && (
                          <Button
                            size="small"
                            // 红色主按钮（用户口径）：红底白字在深色封面上正是可读组合。
                            // 与详情弹窗的同步按钮同款 —— 同一动作不留两种呈现。
                            danger
                            type="primary"
                            icon={<SyncOutlined spin={syncingNoteId === note.id} />}
                            loading={syncingNoteId === note.id}
                            disabled={syncingNoteId !== null && syncingNoteId !== note.id}
                            onClick={(event) => { event.stopPropagation(); void syncNote(note); }}
                          >
                            同步
                          </Button>
                        )}
                        {canPublishNote(note) && (
                          <Button
                            size="small"
                            type="primary"
                            icon={<SendOutlined />}
                            loading={publishingNoteId === note.id}
                            disabled={publishingNoteId !== null && publishingNoteId !== note.id}
                            onClick={(event) => { event.stopPropagation(); void publishNoteWithConfirm(note); }}
                          >
                            发布
                          </Button>
                        )}
                        {/* 发布失败**不再单独挂一个 tag**（设计 13 §32.3.4）：状态标签
                            自己会变成红色「发布失败」并带完整原因的 Tooltip —— 卡片上
                            只留一个状态，避免两个标签并排说同一件事。 */}
                      </div>
                      {/*
                        封面**右下角**：笔记类型 Tag（2026-09-23 用户口径
                        「三方同步/本地生成使用 tag 放图片右下角」）。

                        * 走 `noteTypeLabel` + `noteTypeTagColor`（**唯一文案 / 唯一配色**，
                          与筛选下拉、详情弹窗同一份）—— 不在这里重抄字面量（DEF-061）；
                        * **只渲染已知类型**：`noteTypeLabel` 对空串回空串 ⇒ 缺值整项不渲染
                          （硬约束⑤：不写「未知」充数，也不给未知类型硬安一个颜色）；
                        * 不带 icon：封面右下角只有约 84px 宽，「图标 + 四个汉字」在窄卡
                          （xl 下一行 6 张）会挤到换行。颜色已经足够区分三档。
                        * ⚠️ 与左下角那排的间距靠左下角 `maxWidth` 让位（见上），
                          不在这里加 margin —— 两块都是 absolute，加 margin 只会在
                          某一侧生效、另一侧照旧压。
                      */}
                      {noteTypeLabel(note.note_type) && (
                        <Tag color={noteTypeTagColor(note.note_type)} style={{ position: "absolute", right: 8, bottom: 8, zIndex: 2, margin: 0 }}>
                          {noteTypeLabel(note.note_type)}
                        </Tag>
                      )}
                    </div>
                  }>
                  {/*
                    卡片正文只有**一行**（用户口径 2026-09-23）：
                    「只展示笔记标题和来源加时间即可，账号名和时间之间添加空格」。

                    * **互动数据从卡片上移除**（原点赞 / 收藏 / 评论 / 分享那一排）——
                      这四个数在**详情弹窗**里完整可见（那才是需要看数据的地方），
                      卡片上既挤又抢焦点。`getNoteEngagement` 仍被详情弹窗使用，**保留**。
                    * 来源与时间的间隔：原先靠 `marginLeft` 拼，只在「有作者名」时加；
                      现在**统一用显式分隔**，不依赖前一个元素是否存在（账号名缺失时
                      时间也不会贴上去）。
                    * **笔记类型从正文移回封面右下角**（2026-09-23 用户口径第三轮
                      「三方同步/本地生成使用 tag 放图片右下角」）：第二轮曾把类型
                      做成正文里的一行纯文本，第三轮要求改成**封面右下角的 Tag**
                      ⇒ 正文这一段随之移除，正文回到「标题 + 来源 + 时间」三项。
                      两轮口径不矛盾（都要「看得见」），变的是**承载位置**：
                      封面右下角比正文更早被看到，且不与来源/时间抢那一行。
                  */}
                  <Card.Meta title={<Text ellipsis style={{ fontSize: 13 }}>{note.title || "未命名"}</Text>} description={
                    <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 6 }}>
                      {note.author_name && <Text type="secondary" style={{ fontSize: 12 }}>{note.author_name}</Text>}
                      {/* 来源账号只在**与作者不同**时另起一段：同步笔记两者是同一个人，
                          重复显示只是噪声；采集别人的笔记时这一条才是关键信息。 */}
                      {note.account_name && note.account_name !== note.author_name && (
                        <Text type="secondary" style={{ fontSize: 11 }}>来源：{note.account_name}</Text>
                      )}
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {getNotePublishTime(note) || formatSavedTime(note.created_at)}
                      </Text>
                    </div>
                  } />
                  {/*
                    标签（§29.9，用户口径「内容页笔记卡片中也增加标签，鼠标悬停展示全部，
                    卡片上只展示4个」）：最多展示 4 枚，超出给「+N」；整行挂 Tooltip，
                    悬停展示**全部**标签（含被收起的）。§29.4「标签不上卡片」的口径被本条
                    取代 —— 原句保留在设计文档 §29.4 并标注。
                  */}
                  {note.tags && note.tags.length > 0 && (
                    <Tooltip
                      title={
                        <Space size={[4, 4]} wrap>
                          {note.tags.map((t) => (
                            <Tag key={t.id} color="blue" style={{ fontSize: 11, margin: 0 }}>{t.name}</Tag>
                          ))}
                        </Space>
                      }
                    >
                      <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 4 }}>
                        {note.tags.slice(0, 4).map((t) => (
                          <Tag key={t.id} color="blue" style={{ fontSize: 11, margin: 0 }}>{t.name}</Tag>
                        ))}
                        {note.tags.length > 4 && (
                          <Tag style={{ fontSize: 11, margin: 0 }}>+{note.tags.length - 4}</Tag>
                        )}
                      </div>
                    </Tooltip>
                  )}
                </Card>
              </Col>
            );
          })}
          {/*
            「下拉自动加载更多」的哨兵：滚到它附近就取下一页（rootMargin 240px 提前触发）。
            没有 IntersectionObserver 的环境也能点下面的按钮加载 —— 哨兵不是唯一入口。
          */}
          <Col span={24}>
            <div ref={sentinelRef} style={{ height: 1 }} />
            <LoadMoreBar
              hasMore={hasMore}
              loading={isLoadingMore}
              loaded={notes.length}
              total={total}
              failure={loadMoreFailure}
              onLoadMore={() => void loadMoreNotes()}
            />
          </Col>
        </Row>
      )}

      {/*
        详情：**共用弹窗**（与「我的笔记」同一份版式，`components/platforms/note-detail-modal.tsx`）。
        左侧媒体取本地已下载素材，右侧保留内容库原有的能力（正文 / 标签管理 /
        加入草稿 / AI 改写 / 复制 / 删除）。

        ⚠️ 2026-09-22 用户口径（设计 13 §29.1）：内容库**不展示笔记评论与原始 JSON** ——
        评论区（`commentsSlot`）与 `raw_json` 原文块已移除；`NoteCommentList` 组件与
        `/notes/{id}/comments` 端点保留（「我的笔记」页仍在用）。列表页「有评论」
        筛选与互动**计数**保留：筛的是数据事实、显示的是指标，都不是评论内容。
      */}
      <NoteDetailModal
        open={isDetailOpen}
        onClose={closeDetail}
        title={
          // 自定义弹窗标题（§29.12，用户口径「使用自定义弹窗标题来实现展示标签tag和
          // 添加标签tag」）：整行 = 笔记名称 + 加号 + 标签 chips，元素按 DOM 顺序紧邻
          // 排列 —— 加号因此紧跟名称文本。⚠️ 标题栏容器是 space-between，任何「标题
          // 右侧插槽」都会被推到弹窗远端（§29.7 的 titleExtra 正因此做不到「紧跟」）。
          <Space size={[4, 4]} wrap style={{ minWidth: 0 }}>
            {/* 标题文本：加粗 + 超长省略号截断（flex 行里可收窄） */}
            <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>
              {selectedNote?.title || "笔记详情"}
            </span>
            {/* 加号：**紧跟标题文本后**（用户口径 §29.12）—— 自定义标题行里按 DOM
                顺序紧邻，标题文本之后第一个元素就是它，标签 chips 排在其后 */}
            <Tooltip title="添加标签">
              <Popover
                open={tagPopoverOpen}
                onOpenChange={(open) => { setTagPopoverOpen(open); if (!open) setCustomTagName(""); }}
                trigger="click"
                content={
                  <Space.Compact style={{ width: 260 }}>
                    {/*
                      **一个**组件同时支持「下拉选已有」与「输入新建」（§29.10，用户口径
                      「添加标签弹窗中得组件使用一个下拉支持输入得组件」）—— AutoComplete：
                      点下拉里的已有标签 ⇒ 立即打上；输入新名字 ⇒ 点「添加」新建后打上。
                      不用 Enter 提交：AutoComplete 的 Enter 会被选项选择消费，自处理会双触发。
                    */}
                    <AutoComplete
                      autoFocus
                      style={{ width: "100%" }}
                      placeholder="选择已有标签，或输入新标签名"
                      value={customTagName}
                      options={addableTags.map((t) => ({ value: t.name, label: t.name }))}
                      filterOption={(input, option) =>
                        String(option?.label ?? "").toLowerCase().includes(input.toLowerCase())
                      }
                      notFoundContent={null}
                      onSelect={(name) => { void submitTagDraft(String(name)); }}
                      onChange={(value) => setCustomTagName(value)}
                    />
                    <Button
                      type="primary"
                      loading={tagSaving}
                      disabled={!customTagName.trim()}
                      onClick={() => void submitTagDraft(customTagName)}
                    >
                      添加
                    </Button>
                  </Space.Compact>
                }
              >
                {/* 只展示加号图标（无文字）—— 用户口径 */}
                <Tag
                  icon={<PlusOutlined />}
                  style={{ cursor: "pointer", borderStyle: "dashed" }}
                  onClick={() => setTagPopoverOpen(true)}
                />
              </Popover>
            </Tooltip>
            {(selectedNote?.tags ?? []).map((t) => (
              <Tag
                key={t.id}
                color="blue"
                closable
                // 先阻止默认关闭：列表由服务端状态驱动，删成功才消失，
                // 失败保持原样 + 下方红块/安静行说明（不假装删掉了）。
                onClose={(e) => { e.preventDefault(); void removeTagFromNote(t.id); }}
              >
                {t.name}
              </Tag>
            ))}
          </Space>
        }
        loading={isDetailLoading}
        failure={detailFailure}
        mediaUrls={detailMediaUrls}
        videoUrl={detailVideoUrl}
        isVideo={selectedNoteIsVideo}
        mediaNamePrefix={selectedNote?.note_id || "note"}
        mediaFootnote={hasLocalAssets ? "素材已存本地，可直接下载，链接长期有效" : "素材为平台 CDN 直链，链接有时效性，久置后可能失效"}
      >
        {selectedNote && (
          <>
            {detailActionMessage && <Alert message={detailActionMessage} type="success" showIcon style={{ marginBottom: 12 }} closable onClose={() => setDetailActionMessage(null)} />}

            {/*
              ⚠️ 标签不在右栏单开一块 —— 已整体进**弹窗标题栏**（自定义标题行：名称 +
              可删标签 chips + 纯加号图标「添加标签」按钮，用户 2026-09-22 口径 §29.12）。
              右栏从这里直接进入正文。
            */}

            {selectedNote.content ? (
              <Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 12, fontSize: 13 }}>
                {selectedNote.content}
              </Paragraph>
            ) : (
              !isDetailLoading && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {hydrateHint(selectedNote) ?? "这条笔记在本地还没有正文。"}
                </Text>
              )
            )}

            {/*
              话题（§28 / §29.3）：专有列，**Tag 展示 + 可增删**。
              - 每个 Tag 带 × 删除；末尾「+」弹出输入框新增；
              - 增删都**实时落库**（`PUT /notes/{id}/topics` 整列替换），不等保存；
              - 空列表也渲染这一行 —— 「+」是加第一个话题的入口；删到空不是错误态。
            */}
            <div style={{ marginTop: 8, marginBottom: 12 }}>
              <Space size={[4, 8]} wrap>
                {(selectedNote.topics ?? []).map((t) => (
                  <Tag
                    key={t}
                    color="blue"
                    closable
                    // 先阻止默认关闭：列表由服务端状态驱动，删成功才消失，
                    // 失败时保持原样 + 下方红块/安静行说明（不假装删掉了）。
                    onClose={(e) => { e.preventDefault(); removeTopic(t); }}
                  >
                    #{t}
                  </Tag>
                ))}
                <Popover
                  open={topicPopoverOpen}
                  onOpenChange={(open) => { setTopicPopoverOpen(open); if (open) setTopicDraft(""); }}
                  trigger="click"
                  content={
                    <Space.Compact style={{ width: 240 }}>
                      <Input
                        size="small"
                        autoFocus
                        placeholder="话题名（不用输 #）"
                        maxLength={50}
                        value={topicDraft}
                        onChange={(e) => setTopicDraft(e.target.value)}
                        onPressEnter={submitTopicDraft}
                      />
                      <Button size="small" type="primary" loading={topicSaving} onClick={submitTopicDraft}>确定</Button>
                    </Space.Compact>
                  }
                >
                  <Tag
                    icon={<PlusOutlined />}
                    style={{ cursor: "pointer", borderStyle: "dashed" }}
                    onClick={() => setTopicPopoverOpen(true)}
                  >
                    添加话题
                  </Tag>
                </Popover>
              </Space>
              {topicFailure && <div style={{ marginTop: 6 }}>{renderFailure(topicFailure, "话题保存失败")}</div>}
            </div>

            {/*
              挂载商品（§28.3 / §29.2）：走关系表查商品表当下的真值；空则整块不渲染。
              版式对齐「我的笔记」详情弹窗（用户 2026-09-22 口径）—— Divider 标题 +
              计数 Tag + 每商品一张 Card（72px 封面 + 标题单行省略 + 规格/价格同行）。
              内容库没有 variant / url / shopName（那是「我的笔记」从平台实时响应拿的），
              按缺值口径不渲染对应行，不造假。
            */}
            {selectedNote.goods && selectedNote.goods.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <Divider titlePlacement="start" style={{ margin: "8px 0" }}>
                  <Space size={6}>
                    <ShoppingOutlined />
                    <Text strong style={{ fontSize: 13 }}>挂载商品</Text>
                    <Tag>{selectedNote.goods.length}</Tag>
                  </Space>
                </Divider>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {selectedNote.goods.map((g) => (
                    <Card key={g.item_id} size="small">
                      <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
                        {g.images?.[0] && (
                          <img
                            src={g.images[0]}
                            alt={g.item_title || "商品"}
                            referrerPolicy="no-referrer"
                            style={{ width: 72, height: 72, objectFit: "cover", borderRadius: 6, flex: "0 0 auto" }}
                          />
                        )}
                        <div style={{ flex: "1 1 0%", minWidth: 0 }}>
                          {/* 标题：**永远一行**，超出 `…` 收起 + 悬浮看全文（ADR-018） */}
                          <Text strong ellipsis={{ tooltip: g.item_title || "（无商品标题）" }} style={{ fontSize: 13, display: "block" }}>
                            {g.item_title || "（无商品标题）"}
                          </Text>
                          {/* 价格与库存/销量：价格不压缩（`flex: 0 0 auto`） */}
                          <div className="goods-meta-row">
                            <Text strong style={{ fontSize: 14, flex: "0 0 auto", whiteSpace: "nowrap", color: "#ff4d4f" }}>
                              {/* 分字符串（可能区间 "1999~2999"）⇒ 元：走唯一实现
                                  `fenPriceText`。直接 Number() 会把区间算成 NaN。 */}
                              {g.price ? `¥ ${fenPriceText(g.price) || g.price}` : "价格待同步"}
                            </Text>
                            {g.sales != null && <Text type="secondary" style={{ fontSize: 12 }}>已售 {g.sales}</Text>}
                            {g.stock != null && <Text type="secondary" style={{ fontSize: 12 }}>库存 {g.stock}</Text>}
                          </div>
                          <Space size={8} wrap style={{ marginTop: 4 }}>
                            <Text type="secondary" style={{ fontSize: 11 }}>ID: {g.item_id}</Text>
                          </Space>
                        </div>
                      </div>
                    </Card>
                  ))}
                </div>
              </div>
            )}

            <Descriptions column={1} size="small" bordered>
              <Descriptions.Item label="基础信息">
                <Space size={6} wrap>
                  <Tag icon={<FileTextOutlined />}>{getRawNoteType(selectedNote).includes("video") ? "视频" : "图文"}</Tag>
                  {/* 笔记类型（§25）：解释「为什么这条有同步按钮、那条没有」。
                      ⚠️ 旧实现把 `local_generated` 也显示成「本地」（两值 else 分支）——
                      那与内容库新增的类型筛选下拉（三档）口径不一致，用户筛「本地生成」
                      进详情却看到「本地」。改走 `noteTypeLabel`（与下拉同一份文案）。
                      ⚠️ 2026-09-23：**配色也收敛到 `noteTypeTagColor`** —— 原先这里是
                      内联三元（synced→blue / local_generated→purple / 其它→default），
                      卡片封面右下角也要同一套颜色，再抄一遍就是两份实现（DEF-061 同型）。 */}
                  <Tag
                    icon={selectedNote.note_type === "synced" ? <CloudSyncOutlined /> : <DatabaseOutlined />}
                    color={noteTypeTagColor(selectedNote.note_type)}
                  >
                    {noteTypeLabel(selectedNote.note_type)}
                  </Tag>
                  {platformMissingHint && (
                    <Tag color="orange" icon={<ExclamationCircleOutlined />}>{platformMissingHint}</Tag>
                  )}
                  <Tag
                    icon={<CopyOutlined />}
                    style={{ cursor: "pointer" }}
                    onClick={() => {
                      void navigator.clipboard.writeText(selectedNote.note_id)
                        .then(() => setDetailActionMessage("笔记 ID 已复制。"))
                        .catch(() => setDetailActionMessage("复制失败，请手动选中后复制。"));
                    }}
                  >
                    {selectedNote.note_id || "—"}
                  </Tag>
                  {selectedNote.author_name ? (
                    getAuthorProfileUrl(selectedNote) ? (
                      <Typography.Link href={getAuthorProfileUrl(selectedNote)} target="_blank" rel="noreferrer">
                        {selectedNote.author_name}
                      </Typography.Link>
                    ) : (
                      <Tag>{selectedNote.author_name}</Tag>
                    )
                  ) : null}
                  {/*
                    来源账号并入基础信息行（用户口径「来源账号放基础信息行」）。
                    **同步落库的笔记来源账号就是作者本人**（用户 2026-09-22 口径）——
                    两者同一人时重复显示只是噪声，只在**不同**时渲染（与笔记卡片同规则）；
                    采集来的别人笔记，这一枚才是关键信息。
                  */}
                  {selectedNote.account_name && selectedNote.account_name !== selectedNote.author_name && (
                    <Tag>来源：{selectedNote.account_name}</Tag>
                  )}
                </Space>
              </Descriptions.Item>
              {/*
                **发布状态：只此一块**（设计 13 §32.3.4）。
                改之前这里是两块（「发布状态」开关 + 「发布链路」状态机），说的是同一件事
                的两个字段 —— 用户口径「只展示一个发布状态」。现在两列合一：
                状态标签来自 `publish_status`，失败原因在同一块里摆平台原文（DEF-058）。
                本地生成笔记不给手动开关（状态归发布链路，后端会 400）。
              */}
              <Descriptions.Item label="发布状态">
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  <Space size={8} wrap>
                    <PublishStatusTag
                      note={selectedNote}
                      loading={togglingStatusId === selectedNote.id}
                      onToggle={(next) => void togglePublishStatus(selectedNote, next)}
                    />
                    {selectedNote.note_type !== "local_generated" && (
                      <Switch
                        size="small"
                        checked={selectedNote.publish_status === "published"}
                        loading={togglingStatusId === selectedNote.id}
                        onChange={(checked) =>
                          void togglePublishStatus(selectedNote, checked ? "published" : "pending")
                        }
                      />
                    )}
                    {selectedNote.note_type === "local_generated" && selectedNote.publish_attempts ? (
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        已尝试 {selectedNote.publish_attempts} 次
                      </Text>
                    ) : null}
                    {selectedNote.published_at && (
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        发布于 {formatSavedTime(selectedNote.published_at)}
                      </Text>
                    )}
                    {canPublishNote(selectedNote) && (
                      <Button
                        size="small"
                        type="primary"
                        icon={<SendOutlined />}
                        loading={publishingNoteId === selectedNote.id}
                        disabled={publishingNoteId !== null && publishingNoteId !== selectedNote.id}
                        onClick={() => void publishNoteWithConfirm(selectedNote)}
                      >
                        {selectedNote.publish_status === "failed" ? "重新发布" : "发布"}
                      </Button>
                    )}
                  </Space>
                  {selectedNote.publish_status === "failed" && selectedNote.publish_error && (
                    <Alert
                      type="error"
                      showIcon
                      style={{ marginTop: 2 }}
                      message="发布失败原因"
                      description={selectedNote.publish_error}
                    />
                  )}
                </Space>
              </Descriptions.Item>
              {/*
                发布时间与保存时间**同一行**（用户口径「发布时间和保持时间放一行」）：
                `Descriptions column={1}` 下一行一个 Item ⇒ 合并为一个 Item，
                两个时间用 Space 横排。发布时间取不到时只渲染保存时间（缺值口径：
                非计数不占位，但也不把「取不到」画成有时间）。
              */}
              <Descriptions.Item label="发布 / 保存">
                <Space size={12} wrap>
                  {getNotePublishTime(selectedNote) && <span>发布 {getNotePublishTime(selectedNote)}</span>}
                  <span>保存 {formatSavedTime(selectedNote.created_at)}</span>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="互动数据">
                <Space size={12} wrap>
                  <Text style={{ fontSize: 12 }}><HeartOutlined /> {getNoteEngagement(selectedNote).likes}</Text>
                  <Text style={{ fontSize: 12 }}><StarOutlined /> {getNoteEngagement(selectedNote).collects}</Text>
                  <Text style={{ fontSize: 12 }}><MessageOutlined /> {getNoteEngagement(selectedNote).comments}</Text>
                  <Text style={{ fontSize: 12 }}><ShareAltOutlined /> {getNoteEngagement(selectedNote).shares}</Text>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="作品链接">
                <Typography.Link href={getNoteUrl(selectedNote)} target="_blank" rel="noreferrer" style={{ fontSize: 12, wordBreak: "break-all" }}>
                  <LinkOutlined /> 在小红书打开
                </Typography.Link>
              </Descriptions.Item>
            </Descriptions>

            {/*
              功能按钮行（§29.8，用户口径「把内容页笔记弹窗中的功能按钮与刷新按钮放一行」）：
              刷新从弹窗标题栏移到这里 —— 只重读本地行，零平台请求。「我的笔记」页的
              标题栏刷新不动（口径限定内容页）。
            */}
            <Space wrap style={{ marginTop: 12, marginBottom: 12 }}>
              <Button
                icon={<ReloadOutlined spin={isDetailLoading} />}
                onClick={() => void refreshDetail()}
                loading={isDetailLoading}
                size="small"
              >
                刷新
              </Button>
              {/* 「同步」（§25）：判定走 `canSyncNote`（唯一实现，与卡片同一函数）——
                  用笔记 id 拉平台最新内容更新落库，逻辑与同步任务一致。 */}
              {canSyncNote(selectedNote) && (
                <Tooltip title="用这条笔记的 ID 拉取平台最新内容并更新本地（与同步任务同一套逻辑）">
                  <Button
                    icon={<SyncOutlined spin={syncingNoteId === selectedNote.id} />}
                    onClick={() => void syncNote(selectedNote)}
                    loading={syncingNoteId === selectedNote.id}
                    size="small"
                    // 红色主按钮：与笔记卡片上的同步按钮同款（§29.5）——
                    // 同一个动作不留两种呈现。
                    danger
                    type="primary"
                  >
                    同步
                  </Button>
                </Tooltip>
              )}
              <Button icon={<CopyOutlined />} onClick={copySelectedNote} size="small">复制内容</Button>
              <Button icon={<FileAddOutlined />} onClick={addToDrafts} loading={isCreatingDraft} size="small">加入草稿工坊</Button>
              <Button icon={<EditOutlined />} onClick={() => createDraft("rewrite")} loading={isCreatingDraft} size="small">AI 改写</Button>
              <Popconfirm title="确定删除？" onConfirm={() => void handleDeleteNote(selectedNote)}><Button danger icon={<DeleteOutlined />} size="small">删除</Button></Popconfirm>
            </Space>
            {detailSyncFailure && (
              <div style={{ marginBottom: 12 }}>{renderFailure(detailSyncFailure, "同步失败")}</div>
            )}

            {/*
              标签增删失败的三分法呈现（§29.7）：预期内安静一行、真异常红块摆后端原话。
              位置跟在功能按钮行后（标签块在右栏顶部，失败提示靠近操作区）。
            */}
            {tagFailure && (
              <div style={{ marginBottom: 12 }}>{renderFailure(tagFailure, "标签保存失败")}</div>
            )}
          </>
        )}
      </NoteDetailModal>
    </div>
  );
}
