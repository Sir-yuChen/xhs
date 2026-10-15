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
} from "@ant-design/icons";
import {
  Alert,
  App,
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
  theme,
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import {
  NoteDetailModal,
  formatRemoteTime,
  quietError,
  readError,
} from "../../../components/platforms/note-detail-modal";
import type { QuietError } from "../../../components/platforms/note-detail-modal";
import { useThemePalette } from "../../../app/providers";
import {
  batchCreateDraftsFromNotes,
  batchPublishSavedNotes,
  batchSyncSavedNotes,
  createDraftFromNote,
  deleteSavedNote,
  downloadExportFile,
  exportSavedNotes,
  fetchAccounts,
  fetchPublishQueue,
  fetchSavedNote,
  fetchSavedNoteAssets,
  fetchSavedNotes,
  fetchSyncQueue,
  fetchTopics,
  publishSavedNote,
  syncSavedNote,
  syncSpecifiedShopItems,
  updateSavedNotePublishStatus,
  updateSavedNoteTopics,
  EMPTY_QUEUE_TODAY_COUNTS,
  EMPTY_SYNC_QUEUE_SNAPSHOT,
} from "../../../lib/api";
import { fenPriceText } from "../../../lib/format";
import { useAccountScope } from "../../../lib/account-scope";
import {
  FILTER_CACHE_KEYS,
  NOTE_TYPE_FILTER_ALL_VALUES,
  PUBLISH_STATUS_FILTER_VALUES,
  asBoolean,
  asEnum,
  asString,
  usePersistedFilter,
} from "../../../lib/filter-cache";
import {
  PUBLISH_STATUS_FILTER_OPTIONS,
  canPublish,
  nextManualStatus,
  publishStatusTag,
  splitPublishTargets,
  type PublishStatusFilter,
} from "../../../lib/note-publish-gates";
import {
  createTopicSearchDebouncer,
  type TopicSearchDebouncer,
} from "../../../lib/topic-search";
import { formatShanghaiTime } from "../../../lib/time";
import {
  NOTE_TYPE_FILTER_OPTIONS,
  accountFilterLabel,
  canSyncNote,
  noteTypeLabel,
  noteTypeTagColor,
  splitSyncTargets,
  type NoteTypeFilter,
} from "../../../lib/note-filters";
import type { NoteAsset, NotesExportResponse, NoteTopic, PlatformAccount, SavedNote } from "../../../types";
import type { PublishQueueSnapshot, SyncQueueFinishedEntry, SyncQueueSnapshot } from "../../../lib/api";

const { Title, Text, Paragraph } = Typography;

/** 进度卡轮询间隔（ms）。2026-09-25 用户口径「数据 5 秒更新一次」。 */
const QUEUE_POLL_INTERVAL_MS = 5000;

/**
 * 「本轮没有任何任务」连续多少轮才认定真的没跑起来（6 × 5s ≈ 30 秒）。
 *
 * 为什么需要它：2026-09-25 用户口径把停止条件定成「**待同步数为 0 时，不再刷新
 * 统计数据**」。那个条件成立的前提是快照里能看到本轮任务（`total > 0`）；若入队
 * 与快照之间有时差（或这轮压根没入队成功），待处理数一上来就是 0，直接停就再也
 * 不刷新（用户报的「同步进度未更新」）。连续空转若干轮再停：不误停，也不泄漏
 * 一个永不停止的定时器。
 */
const QUEUE_IDLE_ROUNDS_LIMIT = 6;

/**
 * 统计行六张卡**统一的内容高度**（px）—— 2026-09-26 用户口径「另外两个进度卡与
 * 其它卡片大小不一致，应与其它卡片保持一致」。
 *
 * 为什么需要钉一个高度：四张普通卡的内容 = 小标题（14px ≈ 22px）+ 值（24px ≈ 38px）
 * ≈ 64px；两张进度卡 = 三个 16px 小统计 ≈ 51px + 右下角「仅当日数据」小字 ≈ 19px
 * ≈ 70px。两条内容**自然高度天然不同**（进度卡还多一行小字），靠内容撑高就会
 * 一张高一张矮、同一行上下沿对不齐。
 *
 * 取较高者（70）作为六张卡共同的内容高度，普通卡用垂直居中吸收多出的几像素；
 * ⚠️ 不要改成逐卡调 padding —— 任一处文案变长就又会错位（`flex="1 1 0%"` 只保证
 * **等宽**，管不了高度）。
 */
const STAT_CARD_CONTENT_HEIGHT = 70;

/** 普通统计卡的内容盒（固定高度 + 垂直居中）。 */
const STAT_CARD_CONTENT: CSSProperties = {
  height: STAT_CARD_CONTENT_HEIGHT,
  display: "flex",
  flexDirection: "column",
  justifyContent: "center",
};

/** 进度卡的内容盒：三数在上、当日口径小字在下（两行撑满同一高度）。 */
const STAT_CARD_PROGRESS_CONTENT: CSSProperties = {
  ...STAT_CARD_CONTENT,
  justifyContent: "space-between",
};

/** 进度卡**右下角**的当日口径小字（2026-09-26 用户口径「在卡片右下角小字展示仅当日数据」）。
 *
 * ⚠️ **颜色交给 antd 主题令牌**（渲染处再补 `color: token.colorTextSecondary`
 * = 亮色 `rgba(0,0,0,.65)` / 暗色 `rgba(255,255,255,.65)`），**这里不许再写死颜色**。
 * ⚠️ 别用 `<Text type="secondary">` 顶替令牌 —— 它取的是 `colorTextTertiary`（0.45），
 *    暗色下 12px 小字只有 ≈4.5:1，勉强；亮色下则和写死的 0.45 黑一模一样。
 * 2026-09-26 用户报「仅当日数据黑色字体看不清」：本应用**默认就是暗色主题**
 * （`app/providers.tsx` 里 `saved === "light" ? "light" : "dark"`，卡片底 `#1a1a1a`），
 * 写死的黑字叠在近黑底上对比度约 1.2:1 —— 亮色下同样偏淡（0.45 黑 ≈ #8C8C8C，12px 未达 4.5:1）。
 * 同一页里其它统计值用的是高饱和语义色（绿 / 橙 / 红），两种主题都成立，不受此坑影响。
 */
const STAT_CARD_TODAY_NOTE: CSSProperties = {
  display: "block",
  textAlign: "right",
  fontSize: 12,
  lineHeight: "16px",
};

/** 「仅当日数据」小字（固定文案 —— 后端把窗口定在当日，前端只负责说明）。 */
const TODAY_ONLY_NOTE = "仅当日数据";

/**
 * 正文缺口的**安静说明**（失败三分法的第三态，设计 12 §17.9）。
 *
 * ⚠️ 2026-09-22 起详情端点**不再打平台**（`hydrate=true` 已移除，用户口径：
 * 「内容库应查询的均是本地数据」）⇒ 这里拿不到 `hydrate.status` / `reason`，
 * 「取不到」与「真没数据」**在内容库里不再可区分**。按缺值口径照实说，
 * 不伪装成「平台没有这篇正文」。
 *
 * ⚠️ 2026-09-27 改成**可执行指引**：原句「列表源不带正文，待同步链路补齐。」
 * 是被动等待语气，用户无从下手 —— 而当时后端**确实补不上**（详情令牌取自
 * 创作者 / 千帆列表，打 www 详情恒返回空 data，见设计 13 §19.13.5）。
 * 修复后「点同步」是**真的**能补齐的路径，文案必须指向它。
 * **被取代的原句保留在版本历史里**（不静默删除）。
 */
function hydrateHint(_note: SavedNote): string | null {
  return "列表源不带正文。点上方「同步」触发同步任务，会在同一次任务里补齐正文与挂载商品。";
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
  const p = useThemePalette();
  const { hasMore, loading, loaded, total, failure, onLoadMore } = props;
  if (!hasMore && !failure) {
    return (
      <div style={{ textAlign: "center", padding: "16px 0", color: p.textMuted, fontSize: 12 }}>
        {loaded > 0 ? `已全部加载（共 ${loaded} 条）` : ""}
      </div>
    );
  }
  return (
    <div style={{ textAlign: "center", padding: "16px 0" }}>
      {/*
        ⚠️ 2026-09-25 用户口径「页面所有提示包括异常提示、正常提示、成功提示等
        均实现消息提示组件，不要占用页面布局」：原「加载更多失败」inline 红块 /
        安静行**已移除**。失败原因由全局拦截器的 `message` 承担（`fetchSavedNotes`
        不是 `_silent`，展示权在拦截器）；这里只留「重试」按钮 —— 它是**控件**
        （用户不必滚回去再滚下来重试），不是提示条。
      */}
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
    <Tooltip title="切换发布状态（待发布 ⇄ 已发布）">{tag}</Tooltip>
  ) : (
    tag
  );
}

export function XhsLibraryPage() {
  const p = useThemePalette();
  const navigate = useNavigate();
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径：
  // 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）—— 批量 / 详情的
  // 操作结果反馈不再渲染 inline Alert 占版面。`error` 红块与各 `*Failure`
  // 三分法呈现是结构块，**保留**（设计 12 §17.9）。
  const { message } = App.useApp();
  // 主题令牌（2026-09-26 用户口径「仅当日数据黑色字体看不清」）：**凡是要读的小字，
  // 颜色一律取 `token.colorTextSecondary`**（亮 `rgba(0,0,0,.65)` / 暗
  // `rgba(255,255,255,.65)`），不许写死 `rgba(0,0,0,…)` —— 本应用默认是暗色主题
  // （`app/providers.tsx`），写死的黑字叠 `#1a1a1a` 卡底基本看不见。
  // ⚠️ 也**不用** `<Text type="secondary">`：它取的是 `colorTextTertiary`（0.45），
  // 暗色下对比度只有 ≈4.5:1，12px 小字仍然勉强。
  const { token } = theme.useToken();
  const [notes, setNotes] = useState<SavedNote[]>([]);
  const [total, setTotal] = useState(0);
  // 全库口径的发布状态统计（**不随筛选变化**，设计 05 §2.5 / §32.3.3 三档）。
  const [stats, setStats] = useState<{ pending: number; published: number; failed: number }>({
    pending: 0,
    published: 0,
    failed: 0,
  });
  const [isLoading, setIsLoading] = useState(true);
  const [selectedNote, setSelectedNote] = useState<SavedNote | null>(null);
  const [selectedAssets, setSelectedAssets] = useState<NoteAsset[]>([]);
  const [isDetailOpen, setIsDetailOpen] = useState(false);
  const [isDetailLoading, setIsDetailLoading] = useState(false);
  // 失败承载：`QuietError` 让**三分法**能区分「预期内取不到」与「真接口异常」
  // （设计 12 §17.9）—— 早前这里是一句 `Alert type="warning"` 的泛化文案，
  // 用户既看不出原因、也没有自救线索。
  const [detailFailure, setDetailFailure] = useState<QuietError | null>(null);
  // 「同步」按钮（§25）：**笔记卡片与详情卡都渲染**（仅三方同步笔记）。
  // `syncingNoteId` 记「哪一条在同步中」—— 卡片与详情共用同一个 loading 语义。
  // ⚠️ 2026-09-25 用户口径「页面所有提示……均实现消息提示组件，不要占用页面布局」：
  // 原 `detailSyncFailure` 承载（预期内安静一行 / 真异常红块）**已移除** —— 同步失败
  // 统一由 `message.error` 报一次（`syncSavedNote` 是 `_silent`，展示权本在调用点）。
  const [syncingNoteId, setSyncingNoteId] = useState<number | null>(null);
  // 「发布」按钮（设计 13 §31）：**笔记卡片与详情卡都渲染**（仅本地生成且
  // 待发布/发布失败的笔记）。`publishingNoteId` 记「哪一篇在发布中」——
  // 发布是几次平台请求量级的链路，loading 语义与同步分开（互不打断）。
  const [publishingNoteId, setPublishingNoteId] = useState<number | null>(null);
  const [isBatchPublishing, setIsBatchPublishing] = useState(false);
  // 发布进度卡（2026-09-25 异步 + 发布队列；2026-09-26 常驻化 + 当日口径）。
  // `ref` 与 state 同步，供「入队后算前面还有几篇」这类**即时**读取（state 在闭包
  // 里是旧值）。卡面三数 = 快照 `today`（**当日队列记录**，2026-09-26 用户口径
  // 「只统计当日发布的数据……统计队列记录表中的数据」）—— 本页**不再自己累计**
  // （原 `publishRound` 已删：刷新页面就归零，也看不见别的入口入队的项）。
  const [publishQueue, setPublishQueue] = useState<PublishQueueSnapshot>({
    running: null,
    queued: [],
    total: 0,
    counts: { pending: 0, publishing: 0, published: 0, failed: 0 },
    today: { ...EMPTY_QUEUE_TODAY_COUNTS },
  });
  const publishQueueRef = useRef(publishQueue);
  const publishPollRef = useRef<number | null>(null);
  const trackedPublishIdsRef = useRef<Set<number>>(new Set());
  // 连续「本轮无任务」的轮数（见 `QUEUE_IDLE_ROUNDS_LIMIT`）。
  const publishIdleRoundsRef = useRef(0);
  // 同步进度卡（2026-09-25 批量同步 + 队列，设计 13 §35；2026-09-26 常驻化）。
  // 与发布进度卡**同一套骨架**（ref 与 state 同步、tracked 集合、离开队列即拉
  // 终态回填）—— 两张卡是两件事，但轮询纪律只有一份。卡面三数同发布卡走
  // 快照 `today`（原 `syncRound` 已删，理由同上）。
  const [syncQueue, setSyncQueue] = useState<SyncQueueSnapshot>(EMPTY_SYNC_QUEUE_SNAPSHOT);
  const syncQueueRef = useRef(syncQueue);
  const syncPollRef = useRef<number | null>(null);
  const trackedSyncIdsRef = useRef<Set<number>>(new Set());
  const syncIdleRoundsRef = useRef(0);
  // 已处理过的逐篇结果（按 note_pk 累积）：快照 `finished` 只回**近期**终态项，
  // 累积一份才能把每条的结果都报一次、且不重复报。
  const processedSyncResultsRef = useRef<Map<number, SyncQueueFinishedEntry>>(new Map());
  const [isBatchSyncing, setIsBatchSyncing] = useState(false);
  const [isCreatingDraft, setIsCreatingDraft] = useState(false);
  // 话题字典候选（§34.4）：详情弹窗话题区「+ 添加话题」的 AutoComplete 下拉选项
  // （选已有话题 / 输入新名字 —— 后端 find-or-create，不再需要先建标签再关联）。
  // 原「标签」state（availableTags / tagPopoverOpen / customTagName / tagSaving /
  // tagFailure）随标题栏标签 UI 一起移除（标签表已并入话题表）。
  const [availableTopics, setAvailableTopics] = useState<NoteTopic[]>([]);
  // 话题增删（§29.3 / §34.4）：Tag 展示，增删都实时落库。`topicDraft` = 「+」弹出
  // 输入框里的草稿（AutoComplete：下拉选已有 + 输入新名）；`topicSaving` = 正在写库
  // （防连点）。写库失败的提示走 message —— 2026-09-25 原 `topicFailure` inline 红块已移除。
  const [topicPopoverOpen, setTopicPopoverOpen] = useState(false);
  const [topicDraft, setTopicDraft] = useState("");
  const [topicSaving, setTopicSaving] = useState(false);
  // 以下 8 个筛选状态**都落本地缓存**（2026-09-26 用户口径「筛选条件和账号选择后放
  // 本地缓存，避免每次均需要重新选择」）：`usePersistedFilter` 与 `useState` 同形，
  // 初值优先取缓存、变化即回写 —— 调用点（onChange / clearFilters / currentFilters）
  // 一个字都不用改。⚠️ 缓存要过**取值域校验**（`asEnum`）：后端枚举改了之后，
  // 旧缓存不会变成一个界面选得出、后端不认的值。
  const [keywordFilter, setKeywordFilter] = usePersistedFilter(
    FILTER_CACHE_KEYS.library.keyword, "", asString
  );
  // 话题筛选（§34.4）：原「标签筛选」—— 标签表并入话题表后走 `topic_id` 参数。
  const [selectedTopicFilter, setSelectedTopicFilter] = usePersistedFilter(
    FILTER_CACHE_KEYS.library.topicId, "", asString
  );
  // 话题筛选下拉的**远程模糊搜索**（§34.4，2026-09-25 用户口径「支持输入模糊查询，
  // 控制查询频率」）：`topicSearchResults = null` 表示没输入过（回退全量
  // `availableTopics`），非 null = 后端按 keyword 过滤后的结果；`topicSearching`
  // 是下拉 loading；`topicSearchSeq` 是竞态序号（后发的请求覆盖先发的，过期
  // 响应直接丢弃 —— 慢先到不渲染）。
  const [topicSearchResults, setTopicSearchResults] = useState<NoteTopic[] | null>(null);
  const [topicSearching, setTopicSearching] = useState(false);
  const topicSearchSeq = useRef(0);
  const topicDebouncerRef = useRef<TopicSearchDebouncer | null>(null);
  // 发布状态筛选（设计 13 §32.3.3 四档：全部 / 待发布 / 已发布 / 发布失败）。
  const [publishStatusFilter, setPublishStatusFilter] = usePersistedFilter<PublishStatusFilter>(
    FILTER_CACHE_KEYS.library.publishStatus, "all", asEnum(PUBLISH_STATUS_FILTER_VALUES)
  );
  // 来源账号 / 笔记类型筛选（2026-09-23，用户口径「内容库新增根据账号筛选的条件」+「笔记类型」）。
  // 两个都是**枚举筛选**，取值集合与后端查询参数逐字对齐（见 `lib/note-filters.ts`）；
  // `""` / `"all"` 一律表示**不筛**（不传参数），不复用成「查 id 为空的账号」。
  const [accountFilter, setAccountFilter] = usePersistedFilter(
    FILTER_CACHE_KEYS.library.accountId, "", asString
  );
  // ⚠️ 内容库的账号是**筛选条件**（`""` = 不筛，见上面两行），语义与「当前账号」不同，
  // 所以**不替换**成 `useAccountScope`。但两者要**保持同步**（设计 32 §5.4 / A5）：
  // 用户在内容库选了账号 A，切到「我的笔记」也该是 A —— 否则同一业务线内各页各记一份，
  // 用户看不出自己换过账号（本设计要消除的正是这个）。
  //
  // 同步是**双向**的：
  //  * 本页选账号 → 写进业务线范围（供「我的笔记」等页读取）；
  //  * 本页「重置」成不筛 → **不清**业务线范围（不筛 ≠ 把当前账号改成「无」）。
  // ⚠️ 空串（不筛）**不许**写进范围：`sanitizeAccountId("")` ⇒ `null`，
  //    那会把用户刚在别的页选的账号清掉。
  const [contentAccountScope, setContentAccountScope] = useAccountScope("content");
  useEffect(() => {
    if (accountFilter === "") return;
    const parsed = Number(accountFilter);
    if (Number.isFinite(parsed) && parsed > 0 && parsed !== contentAccountScope) {
      setContentAccountScope(parsed);
    }
  }, [accountFilter, contentAccountScope, setContentAccountScope]);
  const [noteTypeFilter, setNoteTypeFilter] = usePersistedFilter<NoteTypeFilter>(
    FILTER_CACHE_KEYS.library.noteType, "", asEnum(NOTE_TYPE_FILTER_ALL_VALUES)
  );
  // 挂链商品筛选（设计 13 §25.8）：平台商品 ID。**惰性从 URL 读初值** ——
  // 挂链笔记弹窗的「在内容库中查看」跳 `/platforms/xhs/library?item_id=…`，
  // 首屏渲染时就要带上它（放 effect 里 setState 会晚一拍，首条请求已发出）。
  // `""` = 不筛；位置在筛选区**第二行**（第一行 span 之和已满 24，§25.7.5）。
  const [searchParams, setSearchParams] = useSearchParams();
  const [itemIdFilter, setItemIdFilter] = usePersistedFilter(FILTER_CACHE_KEYS.library.itemId, () =>
    new URLSearchParams(window.location.search).get("item_id") ?? ""
  );
  // 账号下拉候选：**只列本平台的账号**（`fetchAccounts("xhs")`）。
  // 取不到时留空数组 + 不动筛选值 —— 下拉空是「列表没取到」，不是「筛成 0 条」。
  const [filterAccounts, setFilterAccounts] = useState<PlatformAccount[]>([]);
  // 账号候选**成功取到**过没有（失败 / 还没回来时不能拿空数组去「校验」缓存里的账号：
  // 那会把用户存的账号当成已删除而清掉）。见下面的缓存自愈 effect。
  const [filterAccountsLoaded, setFilterAccountsLoaded] = useState(false);
  const [hasAssetsFilter, setHasAssetsFilter] = usePersistedFilter(
    FILTER_CACHE_KEYS.library.hasAssets, false, asBoolean
  );
  const [hasCommentsFilter, setHasCommentsFilter] = usePersistedFilter(
    FILTER_CACHE_KEYS.library.hasComments, false, asBoolean
  );
  // ⚠️ 2026-09-23 用户口径「内容库默认使用卡片，不再支持表格展示笔记」：
  // `viewMode` / `setViewMode` **整条移除**（不是默认改成 card 留着切换器）——
  // 视图只剩一种，「当前视图」就不再是状态，留着它就是一个永远不变的死状态。
  const [selectedNoteIds, setSelectedNoteIds] = useState<number[]>([]);
  // ⚠️ `batchTagId` / `setBatchTagId` 已随批量打标签的界面入口一起移除
  // （2026-09-23 用户口径「把选择当前页中的标签下拉框移除」）——
  // 那个 state 只服务于被撤掉的下拉，留着就是一个永远不被写入的死状态。
  // ⚠️ 2026-09-25：原 `batchFailure`（批量操作失败 inline 红块）**已移除** —— 用户口径
  // 「页面所有提示包括异常提示……均实现消息提示组件，不要占用页面布局」。批量链路里
  // 非 `_silent` 的接口失败由全局拦截器弹 message；`syncSavedNote` 这类 `_silent` 的
  // 接口由调用点自己 `message.error`（展示权归调用点，§3.1.1）。
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
      topic_id: selectedTopicFilter ? Number(selectedTopicFilter) : undefined,
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
    setIsLoading(true);
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
    // 失败提示归全局拦截器（`fetchSavedNotes` 非 `_silent` ⇒ 弹后端原话）；
    // 2026-09-25 用户口径「提示不占页面布局」⇒ 原页面级 inline `Alert` 已移除。
    } catch { /* 见上：提示走 message */ } finally { setIsLoading(false); }
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

  useEffect(() => {
    void loadNotes();
    void loadTopics();
    void loadFilterAccounts();
    void refreshPublishQueue(); // 发布进度卡：进页面先拉一次（队列可能还有在跑的生产任务）
    void refreshSyncQueue(); // 同步进度卡：同上（批量同步队列可能还有在跑的任务）
  }, []);

  // 发布轮询随页面卸载停（daemon 线程在后台照跑，但前端不再拉）。
  useEffect(
    () => () => {
      if (publishPollRef.current !== null) window.clearInterval(publishPollRef.current);
      if (syncPollRef.current !== null) window.clearInterval(syncPollRef.current);
    },
    []
  );

  /**
   * 账号筛选的**缓存自愈**（2026-09-26 随「筛选条件落缓存」一起加）。
   *
   * 账号可能已经被删除（换机 / 账号矩阵里删过），而缓存里还留着它的 id：此时下拉
   * 里没有这个值 ⇒ 界面显示一个**裸数字**、列表恒空，用户看不出是缓存干的。
   *
   * 候选账号到手后校验一次：不在候选里 ⇒ 退回「不筛」并按全空条件重拉一次
   * （`setState` 是异步的，所以显式把 `platform_account_id` 传成 `undefined`，
   * 与 `clearFilters` 同一手法）。校验只在**候选成功取到**之后做 —— 账号列表
   * 取失败时拿空数组去校验，会把用户存的账号当成已删除而清掉。
   */
  useEffect(() => {
    if (!filterAccountsLoaded || !accountFilter) return;
    if (filterAccounts.some((account) => String(account.id) === accountFilter)) return;
    setAccountFilter("");
    void loadNotes({ ...currentFilters(), platform_account_id: undefined });
    // 触发时机 = 「候选账号到手」；`accountFilter` 故意不进依赖（它不是触发条件，
    // 是校验对象；进去会让每次改账号都白跑一遍校验）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterAccountsLoaded, filterAccounts]);

  /**
   * 「重置」= **回默认视图**：清空全部筛选**状态**，再按一份**手写**的全空条件重新拉。
   *
   * 为什么不 `loadNotes(currentFilters())`：`setState` 是异步的，刚调的四个 `set*`
   * 在这一刻还没生效，`currentFilters()` 读到的仍是**旧状态** ⇒ 重置后面板空白、
   * 列表却还是筛过的（口径漂移的经典形态）。所以这里显式传一份全空条件，
   * 与 `currentFilters()` 的回默认结果**逐字对应**（新增筛选项时必须同时改这两处）。
   */
  function clearFilters() {
    setKeywordFilter(""); setSelectedTopicFilter(""); setPublishStatusFilter("all");
    setAccountFilter(""); setNoteTypeFilter(""); setItemIdFilter("");
    setHasAssetsFilter(false); setHasCommentsFilter(false);
    // URL 里的 `item_id` 一并删（§25.8）：否则刷新会把跳转来的筛选加回来。
    const next = new URLSearchParams(searchParams);
    next.delete("item_id");
    setSearchParams(next, { replace: true });
    void loadNotes({
      q: undefined,
      topic_id: undefined,
      has_assets: undefined,
      has_comments: undefined,
      publish_status: undefined,
      platform_account_id: undefined,
      note_type: undefined,
    });
  }
  function toggleNoteSelection(id: number) { setSelectedNoteIds((c) => c.includes(id) ? c.filter((i) => i !== id) : [...c, id]); }
  function toggleVisibleSelection() { if (!notes.length) return; const vis = notes.map((n) => n.id); const allSel = vis.every((id) => selectedNoteIdSet.has(id)); setSelectedNoteIds((c) => allSel ? c.filter((id) => !vis.includes(id)) : Array.from(new Set([...c, ...vis]))); }
  function clearSelection() { setSelectedNoteIds([]); }

  async function loadTopics() { try { const r = await fetchTopics(); setAvailableTopics(r.items); } catch { setAvailableTopics([]); } }

  /**
   * 话题筛选下拉的远程模糊搜索（§34.4）：后端 `GET /topics?keyword=` LIKE 过滤。
   *
   * 三分法：搜索失败**不清空**当前 options（保留上一次结果，用户可继续选）——
   * 搜索挂了只是这一次没结果，不是「话题没了」。竞态防护：序号只有最新
   * 那次能落 state / 关 loading（慢先到的响应直接丢弃）。
   */
  async function searchTopicsByKeyword(keyword: string) {
    const seq = ++topicSearchSeq.current;
    setTopicSearching(true);
    try {
      const r = await fetchTopics(keyword);
      if (seq !== topicSearchSeq.current) return; // 过期响应：丢弃
      setTopicSearchResults(keyword.trim() ? r.items : null); // 清空输入 ⇒ 回退全量
    } catch {
      // 可容忍失败：保留当前 options（见上方三分法说明）
    } finally {
      if (seq === topicSearchSeq.current) setTopicSearching(false);
    }
  }

  // 防抖搜索器（300ms，§34.4）：整个生命周期只建一个；卸载时 cancel ——
  // 防「组件没了定时器还在，回调打回来 setState」。`onSearch` 每次输入都调
  // `search`（内部合并），只有停下来那一次会真正打到后端（控制查询频率）。
  useEffect(() => {
    topicDebouncerRef.current = createTopicSearchDebouncer((kw) => { void searchTopicsByKeyword(kw); });
    return () => topicDebouncerRef.current?.cancel();
  }, []);

  /**
   * 筛选下拉的最终 options：没搜索过 ⇒ 全量 `availableTopics`；搜索过 ⇒ 后端结果。
   * **选中项常驻**：远程结果可能不含当前选中话题（先选再清空输入的常见路径），
   * 不并进来的话 antd 只能渲染出 value 数字（用户看到一串 id，不知道是谁）。
   */
  const topicFilterOptions: NoteTopic[] = useMemo(() => {
    const base = topicSearchResults ?? availableTopics;
    if (!selectedTopicFilter) return base;
    const picked = availableTopics.find((t) => String(t.id) === selectedTopicFilter);
    return picked && !base.some((t) => t.id === picked.id) ? [picked, ...base] : base;
  }, [topicSearchResults, availableTopics, selectedTopicFilter]);

  /**
   * 账号下拉候选（`GET /accounts?platform=xhs`）—— **只读本平台**。
   *
   * 取不到就留空数组、**不弹错误**：账号列表挂了不该让整个内容库变成错误页，
   * 而账号下拉空着本身已经说明问题（用户仍可按其它条件筛选）。
   * 与 `loadTopics` 同一策略：筛选辅助数据失败是**可容忍失败**，不抢主列表的错。
   */
  async function loadFilterAccounts() {
    try {
      setFilterAccounts(await fetchAccounts("xhs"));
      // 只有**成功取到**才算「候选已知」（失败留空数组 ⇒ 不能拿去校验缓存里的账号）
      setFilterAccountsLoaded(true);
    } catch { setFilterAccounts([]); }
  }

  async function openDetail(note: SavedNote) {
    setIsDetailOpen(true); setSelectedNote(note); setDetailFailure(null); setIsDetailLoading(true);
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
  function closeDetail() { setIsDetailOpen(false); setDetailFailure(null); setSelectedAssets([]); setTopicPopoverOpen(false); setTopicDraft(""); }

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
    setTogglingStatusId(note.id);
    try {
      const updated = await updateSavedNotePublishStatus(note.id, next);
      setNotes((c) => c.map((n) => (n.id === updated.id ? { ...n, ...updated } : n)));
      if (selectedNote?.id === updated.id) setSelectedNote((current) => current ? { ...current, ...updated } : current);
      message.success(`已标记为「${updated.publish_status === "published" ? "已发布" : "待发布"}」。`);
      await loadNotes();
    } catch { /* 提示归全局拦截器（`updateSavedNotePublishStatus` 非 `_silent`） */ }
    finally { setTogglingStatusId(null); }
  }

  async function copySelectedNote() {
    if (!selectedNote) return;
    try { await navigator.clipboard.writeText(`${selectedNote.title}\n\n${selectedNote.content}`.trim()); message.success("已复制标题和正文。"); }
    catch { message.error("复制失败。"); }
  }

  /**
   * 「同步」按钮（设计 13 §25.4 / §35.10）：把这**一篇**加入同步队列，
   * 由队列 worker 拉平台最新内容并落库。**笔记卡片与详情卡共用**这一个实现
   * （同一语义不留两份）。
   *
   * ⚠️ 2026-09-25 用户口径「单独点击同步按钮也需要放入同步队列中执行，与批量同步
   * 一致」：原实现是**同步阻塞**（点下去等平台请求回来再报结果），现在与批量走
   * **同一个队列、同一个 worker**（顺序执行 = www 限流的第一道保障，两条链路不再
   * 各打各的请求）。前端随之只报**入队回执**，终态（同步成功 / 失败 + 真实原因）
   * 由轮询 `GET /notes/sync-queue` 的快照 `finished` 报一次 —— 与批量同一出口、
   * 同一个轮询器（`trackSyncNotes` / `pollSyncQueue`）。
   *
   * 按钮的 loading 也顺势交给队列：入队后 `syncQueueNoteIds`（快照的 running +
   * queued）即包含这一篇，卡片与详情的 `SyncOutlined spin` 直接跟着转，直到它
   * 离开队列。
   */
  /**
   * 同步笔记时，把**本地台账查不到信息**的挂载商品放进商品信息同步队列
   * （2026-10-12 用户口径）。
   *
   * 用户原话：「项目中商业笔记同步笔记时存在商品在本地查询不到，则需要将商品 id
   * 放入商品同步队列执行同步商品信息的逻辑」—— **单篇与批量同一个动作**，
   * 故这里收一个列表（单篇 = 长度为 1），**不留两份实现**（DEF-044）。
   *
   * 判据：挂载商品**有 `item_id`、但 `item_title` 与 `price` 都空** —— 后端
   * `_goods_entry` 的缺值口径（关系行在、`shop_products` 无行）。
   * 失败静默：用户点的是「同步笔记」，补商品是附带动作，不该盖住主流程反馈。
   */
  async function enqueueMissingGoodsInfo(notes: SavedNote[]) {
    // 同一个商品可能挂在多篇笔记上 ⇒ 按商品 ID 去重；账号按商品所属笔记取。
    const byItemId = new Map<string, number>();
    for (const note of notes) {
      const accountId = note.platform_account_id;
      if (!accountId) continue;
      for (const g of note.goods ?? []) {
        if (g.item_id && !g.item_title && !g.price && !byItemId.has(g.item_id)) {
          byItemId.set(g.item_id, accountId);
        }
      }
    }
    if (byItemId.size === 0) return;
    // 按账号分组（`syncSpecifiedShopItems` 的签名只有一个 platform_account_id）。
    const byAccount = new Map<number, string[]>();
    for (const [itemId, accountId] of byItemId) {
      const list = byAccount.get(accountId) ?? [];
      list.push(itemId);
      byAccount.set(accountId, list);
    }
    for (const [accountId, itemIds] of byAccount) {
      try {
        const res = await syncSpecifiedShopItems(accountId, itemIds);
        if (res?.note) message.info(res.note);
      } catch {
        // 静默（同上）。
      }
    }
  }

  async function syncNote(note: SavedNote) {
    setSyncingNoteId(note.id);
    try {
      const r = await syncSavedNote(note.id);
      replaceNoteInList(r.note);
      if (selectedNote?.id === r.note.id) setSelectedNote(r.note);
      const name = `「${r.note.title || "未命名"}」`;
      await refreshSyncQueue(); // 入队后的最新位置（ref 已是新值）
      if (r.sync.already_queued) {
        message.info(`${name}已在同步队列中，进度见上方进度卡。`);
      } else {
        message.success(`${name}已加入同步队列，按顺序执行中，进度见上方进度卡。`);
      }
      trackSyncNotes([note.id]); // 终态由轮询报（与批量同一条路径）
      // 2026-10-12 用户口径：**点这篇笔记的「同步」时**，若挂载商品在本地台账查不到信息
      // （有 item_id、但标题与价格都空 = 后端 `_goods_entry` 缺值口径），把商品 ID
      // 放进商品信息同步队列并触发同步。⚠️ **只在这条路做** —— 批量同步笔记不做这件事。
      void enqueueMissingGoodsInfo([note]);
    } catch (e) {
      // 真异常（含门禁不过的结构化 400 `NOTE_NOT_SYNCABLE`）：摆真实原因，不吞。
      // `syncSavedNote` 是 `_silent` ⇒ 展示权在调用点，这里必须自己报。
      const { message: reason } = readError(e);
      message.error(`同步失败：${reason}`);
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
   * 「发布」按钮（设计 13 §31.5，2026-09-25 异步 + 发布队列）：
   * **先二次确认**再发请求（用户口径）。点击即入发布队列、毫秒级返回 ——
   * 发布链路（下载媒体 → 上传 → 预审核 → 发布）由后端队列 worker 逐篇顺序
   * 执行，前端不等待；进度看「发布进度卡」，终态（已发布 / 发布失败 + 平台
   * 原文原因）由轮询落库后回填卡片。
   */
  function publishNoteWithConfirm(note: SavedNote) {
    // ⚠️ `centered: true` 必须**逐点显式传**：`Modal.confirm` 走的是静态方法分支
    //（`modal/confirm.js` 自建 ConfirmDialog），`ConfigProvider modal={{centered:true}}`
    //（`providers.tsx` 里给的是 ConfigContext）**到不了它** ⇒ 不传就还是顶部对齐。
    Modal.confirm({
      centered: true,
      title: "发布这篇笔记？",
      content: `将把《${note.title || "未命名"}》加入发布队列并发布到小红书、挂上对应商品链接。发布成功后状态更新为已发布，本地临时下载的媒体会自动清理。`,
      okText: "确认发布",
      cancelText: "取消",
      onOk: () => {
        void publishNote(note);
      },
    });
  }

  /** 拉一次发布进度快照（失败安静略过 —— 下一轮轮询会重试）。 */
  async function refreshPublishQueue() {
    try {
      const snapshot = await fetchPublishQueue();
      publishQueueRef.current = snapshot;
      setPublishQueue(snapshot);
    } catch {
      // 预期内失败：安静（失败三分法），不打断发布
    }
  }

  /** 起轮询（5s 一轮，2026-09-25 用户口径「数据 5 秒更新一次」）：直到**待发布为 0**。
   *
   * 2026-09-26 当日口径后本页**不再有「轮次记账」**（卡面三数是快照 `today`，
   * 后端按当日 `tasks` 行现算）—— 这里只管轮询起停与逐篇结果消息。
   * ⚠️ 起轮询前先 `refreshPublishQueue()` 拉一次（见 :func:`publishNote`）：卡上数字
   * 是**入库后**的当日数，早于入队拉会看到少一篇的旧值。
   */
  function trackPublishNotes(ids: number[]) {
    for (const id of ids) trackedPublishIdsRef.current.add(id);
    if (publishPollRef.current === null) {
      publishPollRef.current = window.setInterval(() => {
        void pollPublishQueue();
      }, QUEUE_POLL_INTERVAL_MS);
    }
  }

  function stopPublishPolling() {
    if (publishPollRef.current !== null) {
      window.clearInterval(publishPollRef.current);
      publishPollRef.current = null;
    }
    trackedPublishIdsRef.current.clear();
    publishIdleRoundsRef.current = 0;
    setPublishingNoteId(null);
    setIsBatchPublishing(false);
  }

  /** 轮询一轮：刷新快照 → 把离开队列的笔记拉终态回填 → 待发布为 0 则收尾。 */
  async function pollPublishQueue() {
    let snapshot: PublishQueueSnapshot;
    try {
      snapshot = await fetchPublishQueue();
    } catch {
      return; // 安静重试（下一轮）
    }
    publishQueueRef.current = snapshot;
    setPublishQueue(snapshot);
    const stillActive = new Set<number>([
      ...(snapshot.running ? [snapshot.running.note_pk] : []),
      ...snapshot.queued.map((q) => q.note_pk),
    ]);
    const finished = [...trackedPublishIdsRef.current].filter((id) => !stillActive.has(id));
    if (finished.length) {
      const updated = await Promise.all(
        finished.map((id) => fetchSavedNote(id).catch(() => null))
      );
      const okNotes: SavedNote[] = [];
      const failNotes: SavedNote[] = [];
      for (const u of updated) {
        if (!u) continue;
        trackedPublishIdsRef.current.delete(u.id);
        replaceNoteInList(u);
        if (selectedNote?.id === u.id) setSelectedNote(u);
        if (u.publish_status === "published") okNotes.push(u);
        else if (u.publish_status === "failed") failNotes.push(u);
      }
      // 逐篇结果消息（卡面三数不在这里算 —— 是快照 `today`，见 `refreshPublishQueue`）。
      const parts: string[] = [];
      if (okNotes.length) parts.push(`已发布 ${okNotes.length} 篇`);
      if (failNotes.length) {
        const reasons = failNotes
          .map((u) => `《${u.title || "未命名"}》：${u.publish_error || "平台未给原因"}`)
          .join("；");
        parts.push(`发布失败 ${failNotes.length} 篇（${reasons}）`);
      }
      if (parts.length) {
        const text = `${parts.join("；")}。失败的可在卡片上重发。`;
        if (failNotes.length) message.warning(text); else message.success(text);
      }
    }
    // 2026-09-25 用户口径「待同步数为 0 时，不再刷新统计数据」：停止条件从
    // 「被跟踪的笔记全部离开队列」改成**待处理数为 0**。`total > 0` 是「本轮确实
    // 跑过」的判据（否则待处理数一上来就是 0，会立刻停、永远不刷新）；空转兜底
    // 防定时器泄漏。
    publishIdleRoundsRef.current = snapshot.total > 0 ? 0 : publishIdleRoundsRef.current + 1;
    const pending = (snapshot.running ? 1 : 0) + snapshot.queued.length;
    if ((snapshot.total > 0 && pending === 0) || publishIdleRoundsRef.current >= QUEUE_IDLE_ROUNDS_LIMIT) {
      stopPublishPolling();
      await loadNotes(); // 刷新列表统计（stats 与进度卡三数同源）
    }
  }

  async function publishNote(note: SavedNote) {
    setPublishingNoteId(note.id);
    try {
      const r = await publishSavedNote(note.id);
      // 入队成功：本地先置 publishing（卡片转「发布中」、发布按钮收起），
      // 之后进度走队列轮询 —— 不等链路（用户口径「点击发布无需等待」）。
      replaceNoteInList(r.note);
      if (selectedNote?.id === r.note.id) setSelectedNote(r.note);
      await refreshPublishQueue(); // 入队后的最新位置（ref 已是新值）
      trackPublishNotes([note.id]);
      const ahead =
        (publishQueueRef.current.running ? 1 : 0) + publishQueueRef.current.queued.length;
      const name = `「${r.note.title || "未命名"}」`;
      const text =
        ahead > 0
          ? `${name}已加入发布队列（前面还有 ${ahead} 篇），进度见上方进度卡。`
          : `${name}发布中，进度见上方进度卡。`;
      message.success(text);
    } catch (e) {
      // 真异常（含门禁类结构化 400）：摆真实原因，不吞。
      const { message: reason } = readError(e);
      message.error(`发布失败：${reason}`);
      setPublishingNoteId(null);
    }
  }

  /** 批量发布（设计 13 §31.5 步 8）：只发选中里**可发布**的，跳过的明示；入队后异步执行。 */
  function batchPublishSelected() {
    const { targets, skipped } = splitPublishTargets(notes, selectedNoteIds);
    if (!targets.length) {
      message.warning("选中的笔记里没有可发布的");
      return;
    }
    // ⚠️ `centered: true` 必须**逐点显式传**：`Modal.confirm` 走的是静态方法分支
    //（`modal/confirm.js` 自建 ConfirmDialog），`ConfigProvider modal={{centered:true}}`
    //（`providers.tsx` 里给的是 ConfigContext）**到不了它** ⇒ 不传就还是顶部对齐。
    Modal.confirm({
      centered: true,
      title: `批量发布 ${targets.length} 篇笔记？`,
      content: `逐篇加入发布队列，由后端按顺序执行（下载媒体 → 上传 → 预审核 → 发布），单项失败不阻断。${
        skipped > 0 ? `另有 ${skipped} 篇选中项不可发布，将跳过。` : ""
      }`,
      okText: "确认发布",
      cancelText: "取消",
      onOk: () => {
        void batchPublishNotes(targets);
      },
    });
  }

  async function batchPublishNotes(targets: SavedNote[]) {
    setIsBatchPublishing(true);
    try {
      const r = await batchPublishSavedNotes(targets.map((n) => n.id));
      const enqueuedIds: number[] = [];
      for (const item of r.results) {
        if (item.ok) {
          enqueuedIds.push(item.note_pk);
          // 入队即 publishing（状态唯一源；`published` 投影一起翻，下次刷新由后端覆盖）
          setNotes((c) =>
            c.map((n) =>
              n.id === item.note_pk
                ? { ...n, publish_status: "publishing", published: false, publish_error: "" }
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
      const rejected = r.results.filter((item) => !item.ok);
      const parts = [`已加入发布队列 ${r.started} 篇，按顺序执行中，进度见上方进度卡。`];
      if (rejected.length) {
        parts.push(
          `${rejected.length} 篇不满足发布条件已跳过（${rejected.map((i) => i.error || "原因未知").join("；")}）。`
        );
      }
      if (rejected.length) message.warning(parts.join("")); else message.success(parts.join(""));
      if (enqueuedIds.length) trackPublishNotes(enqueuedIds);
      else {
        setIsBatchPublishing(false);
        await loadNotes();
      }
    } catch {
      // 提示归全局拦截器（`batchPublishSavedNotes` 非 `_silent`）。
      setIsBatchPublishing(false);
    }
  }

  // --------------------------------------------------------------------- //
  // 批量同步（设计 13 §35）：门禁 → 入队 → 轮询收尾。与发布同一套骨架，
  // 不同的是终态不在笔记行上（同步失败不落库）⇒ 结果从快照 `finished` 取。
  // --------------------------------------------------------------------- //

  /**
   * 批量同步（设计 13 §35.2）：只同步选中里**已发布**的，跳过的明示。
   *
   * 门禁判据是 `splitSyncTargets`（`canBatchSyncNote`：仅 `published`）——
   * 比单条「同步」按钮（`canSyncNote`：synced 或 published）严，用户口径
   * 「只有已发布的支持被选中」。跳过数在确认框里明示，不静默丢。
   */
  function batchSyncSelected() {
    const { targets, skipped } = splitSyncTargets(notes, selectedNoteIds);
    if (!targets.length) {
      message.warning("选中的笔记里没有已发布的");
      return;
    }
    // ⚠️ `centered: true` 必须**逐点显式传**：`Modal.confirm` 走的是静态方法分支
    //（`modal/confirm.js` 自建 ConfirmDialog），`ConfigProvider modal={{centered:true}}`
    //（`providers.tsx` 里给的是 ConfigContext）**到不了它** ⇒ 不传就还是顶部对齐。
    Modal.confirm({
      centered: true,
      title: `批量同步 ${targets.length} 篇笔记？`,
      content: `逐篇加入同步队列，由后端按顺序拉取平台最新内容（正文 / 图集 / 视频地址，单项失败不阻断）。${
        skipped > 0 ? `另有 ${skipped} 篇选中项未发布或发布失败，将跳过。` : ""
      }`,
      okText: "确认同步",
      cancelText: "取消",
      onOk: () => {
        void batchSyncNotes(targets);
      },
    });
  }

  async function batchSyncNotes(targets: SavedNote[]) {
    setIsBatchSyncing(true);
    try {
      const r = await batchSyncSavedNotes(targets.map((n) => n.id));
      const enqueuedIds: number[] = [];
      for (const item of r.results) {
        if (item.ok) enqueuedIds.push(item.note_pk);
      }
      await refreshSyncQueue(); // 入队后的最新位置（ref 已是新值）
      const rejected = r.results.filter((item) => !item.ok);
      const parts = [`已加入同步队列 ${r.started} 篇，按顺序执行中，进度见上方进度卡。`];
      if (rejected.length) {
        parts.push(
          `${rejected.length} 篇不满足同步条件已跳过（${rejected.map((i) => i.error || "原因未知").join("；")}）。`
        );
      }
      if (rejected.length) message.warning(parts.join("")); else message.success(parts.join(""));
      if (enqueuedIds.length) trackSyncNotes(enqueuedIds);
      else setIsBatchSyncing(false);
      // 2026-10-12 用户口径：**批量同步同样要补** —— 商业笔记挂的商品本地查不到时，
      // 把商品 ID 放进商品信息同步队列（与单篇同一个 `enqueueMissingGoodsInfo`）。
      void enqueueMissingGoodsInfo(targets);
    } catch {
      // 提示归全局拦截器（`batchSyncSavedNotes` 非 `_silent`）—— 2026-09-25 修：
      // 原来这里把原因摆成页面 inline 红块（用户看到「批量操作失败 Method Not
      // Allowed」占版面那条），现按用户口径只留 message 一条。
      setIsBatchSyncing(false);
    }
  }

  /** 拉一次同步进度快照（失败安静略过 —— 下一轮轮询会重试）。 */
  async function refreshSyncQueue() {
    try {
      const snapshot = await fetchSyncQueue();
      syncQueueRef.current = snapshot;
      setSyncQueue(snapshot);
    } catch {
      // 预期内失败：安静（失败三分法），不打断同步
    }
  }

  /** 起轮询（5s 一轮，2026-09-25 用户口径「数据 5 秒更新一次」）：直到**待同步为 0**。
   *
   * 2026-09-26 当日口径后本页**不再有「轮次记账」**（卡面三数是快照 `today`，后端
   * 按当日 `tasks` 行现算）—— 这里只管轮询起停与逐篇结果消息。
   */
  function trackSyncNotes(ids: number[]) {
    for (const id of ids) trackedSyncIdsRef.current.add(id);
    if (syncPollRef.current === null) {
      syncPollRef.current = window.setInterval(() => {
        void pollSyncQueue();
      }, QUEUE_POLL_INTERVAL_MS);
    }
  }

  function stopSyncPolling() {
    if (syncPollRef.current !== null) {
      window.clearInterval(syncPollRef.current);
      syncPollRef.current = null;
    }
    trackedSyncIdsRef.current.clear();
    processedSyncResultsRef.current.clear();
    syncIdleRoundsRef.current = 0;
    setIsBatchSyncing(false);
  }

  /** 轮询一轮：刷新快照 → 累积逐篇结果 → 把离开队列的笔记拉新行回填 → 待同步为 0 则收尾。 */
  async function pollSyncQueue() {
    let snapshot: SyncQueueSnapshot;
    try {
      snapshot = await fetchSyncQueue();
    } catch {
      return; // 安静重试（下一轮）
    }
    syncQueueRef.current = snapshot;
    setSyncQueue(snapshot);
    // 累积逐篇结果：快照 `finished` 可能被新批次清空重开而变短，累积才能每条都报一次。
    for (const entry of snapshot.finished) {
      processedSyncResultsRef.current.set(entry.note_pk, entry);
    }
    const stillActive = new Set<number>([
      ...(snapshot.running ? [snapshot.running.note_pk] : []),
      ...snapshot.queued.map((q) => q.note_pk),
    ]);
    const finishedNow = [...trackedSyncIdsRef.current].filter((id) => !stillActive.has(id));
    if (finishedNow.length) {
      const updated = await Promise.all(
        finishedNow.map((id) => fetchSavedNote(id).catch(() => null))
      );
      const okNotes: SavedNote[] = [];
      const failNotes: SavedNote[] = [];
      for (const u of updated) {
        if (!u) continue;
        trackedSyncIdsRef.current.delete(u.id);
        replaceNoteInList(u);
        if (selectedNote?.id === u.id) setSelectedNote(u);
        const result = processedSyncResultsRef.current.get(u.id);
        if (result && !result.ok) failNotes.push(u);
        else if (result) okNotes.push(u);
        // 第三种：离开了队列但拿不到结果（快照被新批次清空 / 进程重启）——
        // 只刷新行，不宣称成功也不宣称失败（缺值口径：不造假）。
      }
      const parts: string[] = [];
      if (okNotes.length) parts.push(`已同步 ${okNotes.length} 篇`);
      if (failNotes.length) {
        const reasons = failNotes
          .map((u) => `《${u.title || "未命名"}》：${processedSyncResultsRef.current.get(u.id)?.reason || "平台未给原因"}`)
          .join("；");
        parts.push(`同步失败 ${failNotes.length} 篇（${reasons}）`);
      }
      // 逐篇结果消息（卡面三数不在这里算 —— 是快照 `today`）。
      if (parts.length) {
        const text = `${parts.join("；")}。`;
        if (failNotes.length) message.warning(text); else message.success(text);
      }
    }
    // 2026-09-25 用户口径「待同步数为 0 时，不再刷新统计数据」：停止条件从
    // 「被跟踪的笔记全部离开队列」改成**待同步数为 0** —— 这同时修掉用户报的
    // 「同步进度未更新」（旧形态首个快照里 tracked 若无活跃项就立刻停，数字再也
    // 不动）。`total > 0` = 本轮确实跑过；空转兜底防定时器泄漏。
    syncIdleRoundsRef.current = snapshot.total > 0 ? 0 : syncIdleRoundsRef.current + 1;
    const pending = (snapshot.running ? 1 : 0) + snapshot.queued.length;
    if ((snapshot.total > 0 && pending === 0) || syncIdleRoundsRef.current >= QUEUE_IDLE_ROUNDS_LIMIT) {
      stopSyncPolling();
      await loadNotes(); // 刷新列表统计（stats 与进度卡三数同源）
    }
  }

  async function createDraft(intent: "rewrite" | "publish") {
    if (!selectedNote) return; setIsCreatingDraft(true);
    try {
      const d = await createDraftFromNote({ platform: "xhs", source_note_id: selectedNote.id, intent });
      message.success(`已创建草稿 #${d.id}，正在跳转...`);
      // 2026-09-25 §36：草稿工坊并入「笔记工坊」页（tab=drafts）。旧路径
      // /platforms/xhs/drafts 仍有重定向兜底，这里直接指规范 URL。
      setTimeout(() => navigate("/platforms/xhs/note-workshop?tab=drafts"), 600);
    }
    catch { message.error("草稿创建失败。"); } finally { setIsCreatingDraft(false); }
  }

  async function addToDrafts() {
    if (!selectedNote) return; setIsCreatingDraft(true);
    try {
      const d = await createDraftFromNote({ platform: "xhs", source_note_id: selectedNote.id, intent: "rewrite" });
      message.success(`已加入草稿工坊，草稿 #${d.id}。`);
    }
    catch { message.error("加入草稿工坊失败。"); } finally { setIsCreatingDraft(false); }
  }

  async function handleDeleteNote(note: SavedNote) {
    // ⚠️ `centered: true` 必须**逐点显式传**：`Modal.confirm` 走的是静态方法分支
    //（`modal/confirm.js` 自建 ConfirmDialog），`ConfigProvider modal={{centered:true}}`
    //（`providers.tsx` 里给的是 ConfigContext）**到不了它** ⇒ 不传就还是顶部对齐。
    Modal.confirm({
      centered: true,
      // 连带后果说全（2026-09-27 用户口径「话题只删关系表」+「删除笔记时需删除
      // 笔记与商品之间的关系」）：**关系行**一并删，**话题字典行不删**（那是用户资产，
      // 别处还在用）—— 原文写「标签关系」是标签表并入话题表之前的旧词，已改。
      title: "确定删除？",
      content: "相关素材、评论、话题关系与挂载商品关系也会一起删除（话题本身保留）。",
      onOk: async () => {
        try {
          await deleteSavedNote(note.id);
          setNotes((c) => c.filter((n) => n.id !== note.id));
          setSelectedNoteIds((c) => c.filter((id) => id !== note.id));
          setTotal((c) => Math.max(0, c - 1));
          if (selectedNote?.id === note.id) closeDetail();
          message.success("已删除笔记。");
          // 统计卡跟着删掉的那一条走（已发布/待发布都可能是它）。
          await loadNotes();
        } catch { message.error("删除失败。"); }
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
    if (!selectedNoteIds.length) { message.warning("请先选择笔记。"); return; }
    setIsBatchWorking(true);
    try { const r = await batchCreateDraftsFromNotes({ note_ids: selectedNoteIds, intent: "rewrite" }); message.success(`已创建 ${r.created_count} 个改写草稿。`); }
    catch { /* 提示归全局拦截器（非 `_silent`） */ } finally { setIsBatchWorking(false); }
  }

  async function exportSelectedNotes(format: "json" | "csv") {
    if (!selectedNoteIds.length) { message.warning("请先选择笔记。"); return; }
    setIsBatchWorking(true);
    try { const r = await exportSavedNotes({ note_ids: selectedNoteIds, format }); setLatestExport(r); message.success(`已导出 ${r.exported_count} 条笔记。`); }
    catch { /* 提示归全局拦截器（非 `_silent`） */ } finally { setIsBatchWorking(false); }
  }

  async function batchDeleteNotes() {
    if (!selectedNoteIds.length) return;
    setIsBatchWorking(true);
    try {
      for (const id of selectedNoteIds) {
        await deleteSavedNote(id);
      }
      setNotes((c) => c.filter((n) => !selectedNoteIds.includes(n.id)));
      setTotal((c) => Math.max(0, c - selectedNoteIds.length));
      message.success(`已删除 ${selectedNoteIds.length} 条笔记。`);
      setSelectedNoteIds([]);
      await loadNotes();
    } catch { /* 提示归全局拦截器（非 `_silent`） */ }
    finally { setIsBatchWorking(false); }
  }

  async function downloadLatestExport() {
    if (!latestExport) return; setIsBatchWorking(true);
    try { await downloadExportFile(latestExport.download_url, latestExport.file_name); message.success(`已下载：${latestExport.file_name}`); }
    catch { message.error("下载失败。"); } finally { setIsBatchWorking(false); }
  }

  // --------------------------------------------------------------------- //
  // 话题增删（设计 13 §29.3 / §34.4）：正文下方话题区展示，「+ 添加话题」
  // AutoComplete（下拉选已有 / 输入新名）。**标签功能已合并进来**（2026-09-25，
  // 标签表并入话题表）—— 原标题栏的标签 chips + 「+ 添加标签」弹层整块移除，
  // 这里一个入口同时干两份事。增删都**实时落库**（PUT 整列替换，后端
  // find-or-create 话题典例行 + 对齐关系行），返回更新后的整行就地替换。
  // --------------------------------------------------------------------- //
  async function saveTopics(next: string[]) {
    if (!selectedNote) return;
    setTopicSaving(true);
    try {
      const updated = await updateSavedNoteTopics(selectedNote.id, next);
      setSelectedNote(updated);
      replaceNoteInList(updated);
    } catch {
      // 提示归全局拦截器（`updateSavedNoteTopics` 非 `_silent`）。
      // ⚠️ 2026-09-25：原「话题保存失败」inline 红块按用户口径移除（提示不占版面）。
    } finally { setTopicSaving(false); }
  }

  /** 删一个话题：Tag 上的 ×。其余原样传回（顺序保持）。 */
  function removeTopic(topic: NoteTopic) {
    if (!selectedNote) return;
    void saveTopics((selectedNote.topics ?? []).filter((t) => t.id !== topic.id).map((t) => t.name));
  }

  /**
   * 加一个话题：AutoComplete 里回车/点「添加」。输入框里的名字——
   * ① 命中**已打在这条笔记上**的话题 ⇒ 无事可做（关掉即可，不重复请求）；
   * ② 命中**字典里已有**的话题（按名字，大小写不敏感）⇒ 直接打上；
   * ③ 都不命中 ⇒ 新名字原样传（后端 find-or-create 建典例行再关联）。
   * 显式收 name 参数：`onSelect` 触发时 state 还没更新，读闭包会拿到旧值。
   */
  function submitTopicDraft(name: string) {
    const trimmed = name.trim();
    if (!trimmed || !selectedNote) return;
    const current = selectedNote.topics ?? [];
    if (current.some((t) => t.name.toLowerCase() === trimmed.toLowerCase())) {
      setTopicPopoverOpen(false); setTopicDraft(""); return;
    }
    setTopicPopoverOpen(false); setTopicDraft("");
    void saveTopics([...current.map((t) => t.name), trimmed]);
  }

  // AutoComplete 下拉里只列**还没打在这条笔记上**的话题（打了的再列一次没有意义）。
  const addableTopics = availableTopics.filter(
    (t) => !(selectedNote?.topics ?? []).some((n) => n.id === t.id)
  );

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
  // 发布队列的**实时**待处理数（发布中 + 排队）—— **只喂轮询停止条件**，不上屏
  // （卡面「待发布」是快照 `today.pending`，2026-09-26 当日口径）。
  // ⚠️ 2026-09-25 用户口径「发布、同步进度卡做类似的调整」+「去掉 tip」⇒
  // 原 Tooltip（悬停看「正在发布哪篇 + 后面排几篇」）**已撤**，卡上只留三个数。
  const publishQueuePending = (publishQueue.running ? 1 : 0) + publishQueue.queued.length;

  // --------------------------------------------------------------------- //
  // 同步进度卡派生值（设计 13 §35.4）
  // * `syncQueueNoteIds`：running + queued 的 note_pk 集合 —— 卡片 / 详情的
  //   单条「同步」按钮对队列中的笔记同样转圈（同一动作不留两种呈现）；
  // * `syncQueuePending`：同步队列的**实时**待处理数 —— **只喂轮询停止条件**
  //   （卡面「待同步」是快照 `today.pending`）。
  //   2026-09-25 用户口径「同步进度统计调整为，成功同步数 / 失败同步数 / 待同步数」
  //   +「去掉 tip」⇒ 原 `syncPercent` 与 Tooltip **已撤**，卡上只留三个数。
  // --------------------------------------------------------------------- //
  const syncQueueNoteIds = useMemo(
    () =>
      new Set<number>([
        ...(syncQueue.running ? [syncQueue.running.note_pk] : []),
        ...syncQueue.queued.map((q) => q.note_pk),
      ]),
    [syncQueue]
  );
  const syncQueuePending = (syncQueue.running ? 1 : 0) + syncQueue.queued.length;

  // 两张进度卡的**卡面三数**（当日口径，2026-09-26 用户口径）。
  // `?? EMPTY_QUEUE_TODAY_COUNTS` 是形状兜底：`today` 是 2026-09-26 才加进快照的键，
  // 前端热更新撞上**还没重启的旧后端**时读到 `undefined` —— 直接取 `.ok` 会整页白屏。
  // 缺值按缺值口径渲染 0（后端漏发该键由 `test_progress_card_snapshots_carry_today_counts`
  // 在测试里钉死，不会静默漏）。
  const publishToday = publishQueue.today ?? EMPTY_QUEUE_TODAY_COUNTS;
  const syncToday = syncQueue.today ?? EMPTY_QUEUE_TODAY_COUNTS;

  return (
    <div>
      {/* 2026-10-05 用户口径「移除所有页面页面头」：原「内容库 + 一句描述 + 刷新」
          整块移除，「刷新」收进下方搜索卡右上角（与账号矩阵卡片同一排布）。 */}

      {/*
        统计卡：**已发布 / 待发布 / 发布失败**（内容池结构这一眼）+ 已保存 + 已选择
        + **发布进度**（2026-09-25 用户口径「发布进度卡做成小卡，与已发、待发布、
        发布失败、等卡片一行样式一致，并去掉相同的统计数据」）
        + **同步进度**（2026-09-25 用户口径「内容库新增一个同步进度卡……统计数据到
        进度卡，已同步数/同步进度比/总数」）。
        口径是**全库**（`stats` 来自后端，只按 user+platform 统计）—— 不随筛选变化，
        也不由 `items.length` 反推。设计 13 §32.3.3：三档与筛选同一份映射。

        ⚠️ 两张进度卡（§31.5 / §35.4）都是**当日三数**：成功 / 失败 / 待处理。
        2026-09-25 用户口径「同步进度统计调整为，成功同步数 / 失败同步数 /
        待同步数」+「发布、同步进度卡做类似的调整」+「去掉 tip」+「数据 5 秒
        更新一次」+「待同步数为 0 时，不再刷新统计数据」。
        2026-09-26 用户口径（**现行**）：「只统计当日发布或同步的数据，在卡片右下角
        小字展示仅当日数据，**统计队列记录表中的数据**」⇒ 三数改读快照 `today`
        （后端 `today_item_counts` 从 `tasks` 表按当日现算），本页**不再自己累计**
        （刷新页面就归零、也看不见定时 / 恢复入口入队的项）；卡右下角加
        「仅当日数据」小字。**被取代的原句**（保留备查）：「成功 / 失败由**本页按
        自己入队的篇累计**（`publishRound` / `syncRound`，空闲起轮归零、忙时并入）」。
        原「已同步/总数 + Progress 细条 + Tooltip」与「发布中 0/1 + 排队 N +
        Tooltip」两种形态**都已撤**（原始口径保留在用例库 / 设计文档，不静默删除）。
        `counts` 快照后端仍全量返回（TC-PPUB-13 守它的形状），前端不再渲染它。

        六张卡用 `flex="1 1 0%"` **严格等宽**填满一行（原来四张 `span={6}` 正好
        24 格，加第五张必须换布局 —— `span={4}` 会剩 4 列尾空；`flex={1}` 在 antd
        里解析成 `1 1 auto`，列宽被内容撑着走，「已保存笔记」带后缀会更宽，
        五张不一致；`1 1 0%` 才是一行等宽）。
        ⚠️ **等宽管不了高度**（2026-09-26 用户口径「另外两个进度卡与其它卡片大小
        不一致，应与其它卡片保持一致」）：六张卡的内容盒统一用 `STAT_CARD_CONTENT`
        （固定高度；普通卡居中、进度卡两行撑满）—— 见文件顶部常量处的推导。
      */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col flex="1 1 0%" style={{ minWidth: 0 }}><Card size="small"><div style={STAT_CARD_CONTENT}><Statistic title="已发布" value={stats.published} valueStyle={{ color: "#52c41a" }} /></div></Card></Col>
        <Col flex="1 1 0%" style={{ minWidth: 0 }}><Card size="small"><div style={STAT_CARD_CONTENT}><Statistic title="待发布" value={stats.pending} valueStyle={{ color: "#fa8c16" }} /></div></Card></Col>
        <Col flex="1 1 0%" style={{ minWidth: 0 }}><Card size="small"><div style={STAT_CARD_CONTENT}><Statistic title="发布失败" value={stats.failed} valueStyle={{ color: "#ff4d4f" }} /></div></Card></Col>
        <Col flex="1 1 0%" style={{ minWidth: 0 }}><Card size="small"><div style={STAT_CARD_CONTENT}><Statistic title="已保存笔记" value={total} suffix={`/ 已选 ${selectedNoteIds.length}`} /></div></Card></Col>
        {/*
          发布进度（§31.5）：**当日三数** —— 发布成功 / 失败 / 待发布
          （卡面取 `publishToday` = 快照 `today`，后端按当日 `tasks` 行现算）。
          轮询 5 秒一轮，**待发布（实时队列）为 0 即停**（`trackPublishNotes` 起、
          `stopPublishPolling` 停）—— 停止条件用的是队列实时态 `publishQueuePending`，
          与卡面数字（当日窗口）是两件事：一个问「还有没有活」，一个问「今天跑成几篇」。
          待发布 > 0 着蓝色（进行时；另两个是终态计数，用语义色）。
          2026-09-26 用户口径：卡片**不再带「发布进度」小标题** —— 与同行其余
          卡片（已发布 / 待发布 / 发布失败 / 已保存笔记）一样只有统计项；
          「当日」口径由右下角小字说明，而「已发布」那张是**全库四态**。
        */}
        <Col flex="1 1 0%" style={{ minWidth: 0 }}>
          <Card size="small">
            <div style={STAT_CARD_PROGRESS_CONTENT}>
              <Space size={8}>
                <Statistic title="发布成功" value={publishToday.ok} valueStyle={{ fontSize: 16, color: "#52c41a" }} />
                <Statistic title="失败" value={publishToday.failed} valueStyle={{ fontSize: 16, color: "#ff4d4f" }} />
                <Statistic
                  title="待发布"
                  value={publishToday.pending}
                  valueStyle={{ fontSize: 16, color: publishToday.pending > 0 ? "#1677ff" : undefined }}
                />
              </Space>
              <Text style={{ ...STAT_CARD_TODAY_NOTE, color: token.colorTextSecondary }}>{TODAY_ONLY_NOTE}</Text>
            </div>
          </Card>
        </Col>
        {/*
          同步进度（§35.4）：**当日三数** —— 同步成功 / 失败 / 待同步
          （卡面取 `syncToday` = 快照 `today`，与发布卡同源同口径）。
          轮询 5 秒一轮，**待同步（实时队列）为 0 即停**（`trackSyncNotes` 起、
          `stopSyncPolling` 停）；要重新看进度就再发起一次同步。
          2026-09-26 用户口径：卡片**不再带「同步进度」小标题**（与发布进度卡
          同样处理，两张卡都只剩统计项 + 右下角当日说明）。
        */}
        <Col flex="1 1 0%" style={{ minWidth: 0 }}>
          <Card size="small">
            <div style={STAT_CARD_PROGRESS_CONTENT}>
              <Space size={8}>
                <Statistic title="同步成功" value={syncToday.ok} valueStyle={{ fontSize: 16, color: "#52c41a" }} />
                <Statistic title="失败" value={syncToday.failed} valueStyle={{ fontSize: 16, color: "#ff4d4f" }} />
                <Statistic
                  title="待同步"
                  value={syncToday.pending}
                  valueStyle={{ fontSize: 16, color: syncToday.pending > 0 ? "#1677ff" : undefined }}
                />
              </Space>
              <Text style={{ ...STAT_CARD_TODAY_NOTE, color: token.colorTextSecondary }}>{TODAY_ONLY_NOTE}</Text>
            </div>
          </Card>
        </Col>
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
      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title="内容库"
        extra={<Button icon={<ReloadOutlined />} onClick={() => void loadNotes()} loading={isLoading}>刷新</Button>}
      >
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
          <Col span={5}>
            <Select
              value={selectedTopicFilter || undefined}
              onChange={(v) => setSelectedTopicFilter(v ?? "")}
              placeholder="全部话题"
              allowClear
              showSearch
              filterOption={false}
              loading={topicSearching}
              onSearch={(kw) => topicDebouncerRef.current?.search(kw)}
              style={{ width: "100%" }}
              options={topicFilterOptions.map((t) => ({ value: String(t.id), label: t.name }))}
            />
          </Col>
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
              <Tooltip title="把选中里待发布 / 发布失败的本地生成笔记发布到小红书">
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
              {/* 批量同步（设计 13 §35）：只同步选中里**已发布**的（用户口径「只有已发布的
                  支持被选中」），跳过的在确认框里明示；入队后按顺序执行，进度见上方进度卡。 */}
              <Tooltip title="把选中里已发布的笔记加入同步队列，拉取平台最新内容">
                <Button
                  icon={<CloudSyncOutlined />}
                  disabled={isBatchWorking || isBatchSyncing || !selectedNoteIds.length}
                  loading={isBatchSyncing}
                  onClick={() => void batchSyncSelected()}
                  size="small"
                >
                  批量同步
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
          {/*
            ⚠️ 2026-09-25 用户口径「页面所有提示包括异常提示、正常提示、成功提示等
            均实现消息提示组件，不要占用页面布局」：原「批量操作失败」inline 红块
            **已移除**（它正是用户看到「批量操作失败 Method Not Allowed」占版面那条）。
            非 `_silent` 接口的失败由全局拦截器弹 message；`_silent` 的同步接口由
            `syncNote` 自己 `message.error`。
          */}
        </Card>
      )}

      {/* 列表加载失败不再占版面：`fetchSavedNotes` 非 `_silent`，提示归全局拦截器。 */}

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
                    <div style={{ position: "relative", background: p.mediaBg }}>
                      <Checkbox checked={selectedNoteIdSet.has(note.id)} onClick={(e) => { e.stopPropagation(); toggleNoteSelection(note.id); }} style={{ position: "absolute", top: 8, left: 8, zIndex: 2 }} />
                      {cover ? <img src={cover} alt={note.title} referrerPolicy="no-referrer" style={{ width: "100%", aspectRatio: "1/1", objectFit: "cover", display: "block" }} /> : <div style={{ width: "100%", aspectRatio: "1/1", display: "flex", alignItems: "center", justifyContent: "center", color: p.fillIcon, fontSize: 28 }}><PictureOutlined /></div>}
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
                            icon={<SyncOutlined spin={syncingNoteId === note.id || syncQueueNoteIds.has(note.id)} />}
                            loading={syncingNoteId === note.id || syncQueueNoteIds.has(note.id)}
                            disabled={
                              syncingNoteId === note.id
                              || syncQueueNoteIds.has(note.id)
                              || (syncingNoteId !== null && syncingNoteId !== note.id)
                            }
                            onClick={(event) => { event.stopPropagation(); void syncNote(note); }}
                          >
                            同步
                          </Button>
                        )}
                        {canPublishNote(note) && (
                          // 异步 + 队列后**去掉**「别的笔记在发布就禁用」：点击只是入队
                          // （后端 worker 顺序执行），多发几篇会排队而不是互相打断。
                          // 发布中这篇的按钮本来就会被 `canPublishNote` 收起（publishing
                          // 不可再发），双发由后端门禁兜（400 NOTE_NOT_PUBLISHABLE）。
                          <Button
                            size="small"
                            type="primary"
                            icon={<SendOutlined />}
                            loading={publishingNoteId === note.id}
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
                    话题**不在卡片上展示**（2026-09-25 用户口径「拆分的话题不要放在
                    卡片上，放在笔记详情弹窗内就可以了」）：§29.9「卡片也展示话题、
                    悬停看全部」的口径被本条**取代**（原句保留在设计文档 §29.9 并标注），
                    §34.4 标签并入话题后短暂落到卡片上的那版也一并退回来。
                    话题的展示与增删**只在详情弹窗**（正文下方话题区：Tag 列表 +
                    AutoComplete 选已有 / 输新名 + × 实时删）—— 卡片保持
                    「封面 + 标题 + 来源/时间」的干净形态。
                  */}
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
        // ⚠️ 2026-09-25（§34.4）：原标题栏的「标签 chips + 加号添加标签」整块移除
        // —— 标签展示 / 添加已合并进正文下方的话题区（一个入口管全部话题）。
        // 标题栏只留标题文本（加粗 + 超长省略号截断）。
        title={
          <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>
            {selectedNote?.title || "笔记详情"}
          </span>
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
              话题（§28 / §29.3 / §34.4）：**Tag 展示 + 可增删**；2026-09-25 起
              标题栏的「标签展示 / 添加」已合并进来（标签表并入话题表）——
              一个入口管全部话题。
              - 每个 Tag 带 × 删除；末尾「+」弹出 **AutoComplete** 新增
                （下拉选字典里已有的话题 / 输入新名字，后端 find-or-create）；
              - 增删都**实时落库**（`PUT /notes/{id}/topics` 整列替换），不等保存；
              - 空列表也渲染这一行 —— 「+」是加第一个话题的入口；删到空不是错误态。
            */}
            <div style={{ marginTop: 8, marginBottom: 12 }}>
              <Space size={[4, 8]} wrap>
                {(selectedNote.topics ?? []).map((t) => (
                  <Tag
                    key={t.id}
                    color="blue"
                    closable
                    // 先阻止默认关闭：列表由服务端状态驱动，删成功才消失，
                    // 失败时保持原样 + 下方红块/安静行说明（不假装删掉了）。
                    onClose={(e) => { e.preventDefault(); removeTopic(t); }}
                  >
                    #{t.name}
                  </Tag>
                ))}
                <Popover
                  open={topicPopoverOpen}
                  onOpenChange={(open) => { setTopicPopoverOpen(open); if (open) setTopicDraft(""); }}
                  trigger="click"
                  content={
                    <Space.Compact style={{ width: 260 }}>
                      {/*
                        **一个**组件同时支持「下拉选已有」与「输入新建」（§29.10 组件形态
                        沿用，2026-09-25 从标签搬到话题）—— AutoComplete：点下拉里的已有
                        话题 ⇒ 立即打上；输入新名字 ⇒ 点「添加」新建后打上（后端
                        find-or-create，不再需要先建典再关联）。
                      */}
                      <AutoComplete
                        size="small"
                        autoFocus
                        style={{ width: "100%" }}
                        placeholder="选择已有话题，或输入新话题名"
                        value={topicDraft}
                        options={addableTopics.map((t) => ({ value: t.name, label: t.name }))}
                        filterOption={(input, option) =>
                          String(option?.label ?? "").toLowerCase().includes(input.toLowerCase())
                        }
                        notFoundContent={null}
                        onSelect={(name) => { submitTopicDraft(String(name)); }}
                        onChange={(value) => setTopicDraft(value)}
                      />
                      <Button
                        size="small"
                        type="primary"
                        loading={topicSaving}
                        disabled={!topicDraft.trim()}
                        onClick={() => submitTopicDraft(topicDraft)}
                      >
                        添加
                      </Button>
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
                          {/* 本地查不到这件商品 ⇒ 不干放「价格待同步」，如实说明点「同步」会自动入队补 */}
                          {!g.item_title && !g.price && g.item_id && (
                            <Text type="warning" style={{ fontSize: 12, display: "block", marginTop: 4 }}>
                              本地查询不到该商品信息，点「同步」会自动加入商品信息同步队列
                            </Text>
                          )}
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
                        .then(() => message.success("笔记 ID 已复制。"))
                        .catch(() => message.error("复制失败，请手动选中后复制。"));
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
                        onClick={() => void publishNoteWithConfirm(selectedNote)}
                      >
                        {selectedNote.publish_status === "failed" ? "重新发布" : "发布"}
                      </Button>
                    )}
                  </Space>
                  {/*
                    ⚠️ 「发布失败原因」**保留 inline**（不随 message 化搬走）：它不是一次性
                    操作反馈，而是这条笔记的**持久状态说明** —— `publish_error` 是落库字段，
                    用户重开弹窗还要看（设计 04 §3.6 表格第 2 行「静态说明」类）。搬成 toast
                    只会让原因一条条飘走、无法回看。
                  */}
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
                <Tooltip title="用这条笔记的 ID 拉取平台最新内容并更新本地">
                  <Button
                    icon={<SyncOutlined spin={syncingNoteId === selectedNote.id || syncQueueNoteIds.has(selectedNote.id)} />}
                    onClick={() => void syncNote(selectedNote)}
                    loading={syncingNoteId === selectedNote.id || syncQueueNoteIds.has(selectedNote.id)}
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

            {/*
              ⚠️ 2026-09-25（§34.4）：原「标签增删失败」呈现块随标题栏标签 UI 一并
              移除；同轮（用户口径「提示不占页面布局」）把话题增删失败的 inline 红块
              也一并撤掉 —— 现在失败只弹一次 message，这里不留任何第二块。
            */}
          </>
        )}
      </NoteDetailModal>
    </div>
  );
}
