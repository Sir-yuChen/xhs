import dayjs from "dayjs";
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  HistoryOutlined,
  ReloadOutlined,
  SearchOutlined,
  SettingOutlined,
  SyncOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  App,
  Alert,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Empty,
  Flex,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Popover,
  Radio,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ColumnsType } from "antd/es/table";
import { useAccountScope } from "../../../lib/account-scope";
import { useThemePalette } from "../../../app/providers";
import {
  fetchAccounts,
  fetchOfflineNotes,
  fetchNoteGoodsDetail,
  importNoteData,
  syncNoteData,
  hydrateOfflineNoteDetail,
  batchDeleteNotes,
  fetchNoTrafficCandidates,
  fetchNoTrafficCleanupActive,
  fetchNoTrafficCleanupLogs,
  startNoTrafficCleanup,
  fetchScheduleLogs,
  type NoTrafficThresholds,
  type NoTrafficCandidate,
  type NoTrafficCleanupLogItem,
  type NoTrafficCleanupItemRow,
  type NoTrafficQuery,
  type NoteGoodsDetail,
} from "../../../lib/api";
import type { PlatformAccount } from "../../../types";
import { WaitMask } from "../../../components/ui/wait-mask";
import {
  NoteDetailModal,
  quietError,
  readError,
  renderFailure,
} from "../../../components/platforms/note-detail-modal";

const { Text, Paragraph } = Typography;

/** `/notes/offline` 返回的统计表条目（2026-10-12 设计 35）。 */
export type DatacenterStatItem = {
  note_id: string;
  title: string;
  note_flag: string;
  item_id: string;
  item_title: string;
  publish_time: number | null;
  read_count: number | null;
  like_count: number | null;
  collect_count: number | null;
  comment_count: number | null;
  share_count: number | null;
  pay_gmv: number | null;
  pay_order_cnt: number | null;
  stat_date: string;
  window: string;
  platform_deleted_at: string | null;
  in_library: boolean;
};

type PanelState<T> = {
  items: T[];
  loading: boolean;
  error: ReturnType<typeof quietError> | null;
  loaded: boolean;
  hasMore: boolean;
  total: number;
};

function emptyPanel<T>(): PanelState<T> {
  return { items: [], loading: false, error: null, loaded: false, hasMore: false, total: 0 };
}

const CATEGORY_LABELS: Record<"goods" | "normal", string> = {
  goods: "商业笔记",
  normal: "普通笔记",
};

// 2026-10-12：清理候选完全由「指标区间」决定，不再传固定无流量阈值（三项 max=0）。
// 全 null = 不参与判定，候选 = 指标区间（含分类/关键词）筛选结果。
const EMPTY_NO_TRAFFIC_THRESHOLDS: NoTrafficThresholds = {
  max_browse: null,
  max_pay_order: null,
  max_pay_gmv: null,
  max_like: null,
  max_collect: null,
  max_comment: null,
  max_share: null,
};

function noTrafficQuery(
  category: "goods" | "normal",
  range: Record<string, number | null>,
): NoTrafficQuery {
  return {
    keyword: "",
    note_type: "",
    published_before: null,
    category,
    read_min: range.read_min ?? null,
    read_max: range.read_max ?? null,
    like_min: range.like_min ?? null,
    like_max: range.like_max ?? null,
    collect_min: range.collect_min ?? null,
    collect_max: range.collect_max ?? null,
    comment_min: range.comment_min ?? null,
    comment_max: range.comment_max ?? null,
    share_min: range.share_min ?? null,
    share_max: range.share_max ?? null,
    pay_gmv_min: range.pay_gmv_min ?? null,
    pay_gmv_max: range.pay_gmv_max ?? null,
    pay_order_min: range.pay_order_min ?? null,
    pay_order_max: range.pay_order_max ?? null,
  };
}

function num(value: number | null | undefined): number {
  return Number.isFinite(value) && value != null ? (value as number) : 0;
}

/** 清理结果 → 展示标签（颜色 + 文案）。 */
const CLEANUP_RESULT_LABEL: Record<string, { text: string; color: string }> = {
  deleted: { text: "已删除", color: "green" },
  skipped: { text: "跳过", color: "orange" },
  failed: { text: "失败", color: "red" },
  not_in_pool: { text: "不在候选池", color: "default" },
  not_attempted: { text: "未执行", color: "default" },
};

function fmtMetric(value: number | null | undefined): string {
  const v = num(value);
  if (v <= 0) return "0";
  if (v >= 10000) return `${(v / 10000).toFixed(v >= 100000 ? 0 : 1)}w`;
  return v.toLocaleString();
}

function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "";
  //: 平台 publish_time 实测为**毫秒**（秒级值 ×1000 会得到年份 5.8 万的烂日期）。
  //: 按数量级自适应：< 1e11 视为秒（×1000），否则已是毫秒直接用。
  const ms = ts < 1e11 ? ts * 1000 : ts;
  return new Date(ms).toLocaleString("zh-CN", { hour12: false });
}

/** ISO 时间字符串 → 本地可读时间（`finished_at`/`operated_at` 等落库字段）。 */
function fmtTimeStr(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("zh-CN", { hour12: false });
}

/** 全部可展示字段（列 key → 中文名）。核心字段默认可见，其余勾选后才显示。
    注意：`Object.keys` 保持插入顺序 = 表格列顺序，`note_id` 放第一位即第一列。 */
const ALL_COLUMNS: Record<string, string> = {
  note_id: "笔记ID",
  title: "笔记标题",
  read_count: "阅读",
  like_count: "点赞",
  collect_count: "收藏",
  comment_count: "评论",
  share_count: "分享",
  item_title: "商品名",
  pay_gmv: "支付GMV",
  pay_order_cnt: "支付订单数",
  publish_time: "发布时间",
  note_flag: "分类",
  stat_date: "同步日",
};

/** 默认展示的字段（截图口径 2026-10-12：笔记ID/标题/阅读/点赞/收藏/评论/分享/
    支付GMV/支付订单数/发布时间/分类 默认可见；商品名、同步日默认隐藏）。 */
const DEFAULT_VISIBLE_COLUMNS = [
  "note_id",
  "title",
  "read_count",
  "like_count",
  "collect_count",
  "comment_count",
  "share_count",
  "pay_gmv",
  "pay_order_cnt",
  "publish_time",
  "note_flag",
];

/** 指标区间搜索条件（「更多筛选」折叠区）。`fromKey`/`toKey` 是 `searchFilters` 里的
    camelCase 字段名；`minKey`/`maxKey` 是清理接口 `NoTrafficQuery` 的 snake_case 字段名。 */
type MetricRangeKey = "readFrom" | "readTo" | "likeFrom" | "likeTo" | "collectFrom" | "collectTo" | "commentFrom" | "commentTo" | "shareFrom" | "shareTo" | "payGmvFrom" | "payGmvTo" | "payOrderFrom" | "payOrderTo";
const METRIC_RANGE_FIELDS: { key: string; label: string; fromKey: MetricRangeKey; toKey: MetricRangeKey; minKey: string; maxKey: string }[] = [
  { key: "read", label: "阅读", fromKey: "readFrom", toKey: "readTo", minKey: "read_min", maxKey: "read_max" },
  { key: "like", label: "点赞", fromKey: "likeFrom", toKey: "likeTo", minKey: "like_min", maxKey: "like_max" },
  { key: "collect", label: "收藏", fromKey: "collectFrom", toKey: "collectTo", minKey: "collect_min", maxKey: "collect_max" },
  { key: "comment", label: "评论", fromKey: "commentFrom", toKey: "commentTo", minKey: "comment_min", maxKey: "comment_max" },
  { key: "share", label: "分享", fromKey: "shareFrom", toKey: "shareTo", minKey: "share_min", maxKey: "share_max" },
  { key: "payGmv", label: "支付GMV", fromKey: "payGmvFrom", toKey: "payGmvTo", minKey: "pay_gmv_min", maxKey: "pay_gmv_max" },
  { key: "payOrder", label: "支付订单", fromKey: "payOrderFrom", toKey: "payOrderTo", minKey: "pay_order_min", maxKey: "pay_order_max" },
];

function renderCell(record: DatacenterStatItem, key: string): ReactNode {
  switch (key) {
    case "note_id":
      return <span style={{ fontSize: 12 }}>{record.note_id || ""}</span>;
    case "title":
      return <span style={{ fontSize: 13 }}>{record.title || record.item_title || "（无标题）"}</span>;
    case "read_count":
      return fmtMetric(record.read_count);
    case "like_count":
      return fmtMetric(record.like_count);
    case "collect_count":
      return fmtMetric(record.collect_count);
    case "comment_count":
      return fmtMetric(record.comment_count);
    case "share_count":
      return fmtMetric(record.share_count);
    case "item_title":
      return record.item_title || "";
    case "pay_gmv":
      return record.pay_gmv != null ? `¥${fmtMetric(record.pay_gmv)}` : "";
    case "pay_order_cnt":
      return fmtMetric(record.pay_order_cnt);
    case "publish_time":
      return fmtTime(record.publish_time);
    case "note_flag":
      return record.note_flag === "goods_note" ? "商业" : record.note_flag ? record.note_flag : "";
    case "stat_date":
      return record.stat_date || "";
    default:
      return "";
  }
}

export function XhsDatacenterPage() {
  const { message } = App.useApp();
  const p = useThemePalette();
  // 数据中心归**店铺线**（千帆数据中心，ark 令牌），与「我的笔记」的 `content` 分开。
  const [selectedAccountId, setSelectedAccountId] = useAccountScope("shop");
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);

  const [activeTab, setActiveTab] = useState<"goods" | "normal">("goods");
  const [panels, setPanels] = useState<Record<"goods" | "normal", PanelState<DatacenterStatItem>>>({
    goods: emptyPanel(),
    normal: emptyPanel(),
  });
  const [keyword, setKeyword] = useState<Record<"goods" | "normal", string>>({ goods: "", normal: "" });
  // 笔记 ID 精确查询（2026-10-12）。
  const [noteIdQuery, setNoteIdQuery] = useState("");
  const [page, setPage] = useState<Record<"goods" | "normal", number>>({ goods: 1, normal: 1 });
  // 当前排序列 + 方向（指标列排序，服务端做）。
  const [sort, setSort] = useState<Record<"goods" | "normal", { field: string; order: "asc" | "desc" } | null>>({
    goods: null,
    normal: null,
  });
  const [selected, setSelected] = useState<Record<"goods" | "normal", Set<string>>>({
    goods: new Set(),
    normal: new Set(),
  });
  // 列设置：核心 6 字段默认可见，其余勾选后展示。
  const [visibleColumns, setVisibleColumns] = useState<string[]>([...DEFAULT_VISIBLE_COLUMNS]);
  // 搜索条件（核心 + 指标区间）。`searchExpanded` 控制「更多筛选」折叠（指标区间类折叠）。
  const [searchExpanded, setSearchExpanded] = useState(false);
  const [searchFilters, setSearchFilters] = useState<{
    publishedRange: [dayjs.Dayjs, dayjs.Dayjs] | null;
    deleted: "all" | "deleted" | "active";
    readFrom: number | null;
    readTo: number | null;
    likeFrom: number | null;
    likeTo: number | null;
    collectFrom: number | null;
    collectTo: number | null;
    commentFrom: number | null;
    commentTo: number | null;
    shareFrom: number | null;
    shareTo: number | null;
    payGmvFrom: number | null;
    payGmvTo: number | null;
    payOrderFrom: number | null;
    payOrderTo: number | null;
  }>({
    publishedRange: null,
    deleted: "all",
    readFrom: null, readTo: null,
    likeFrom: null, likeTo: null,
    collectFrom: null, collectTo: null,
    commentFrom: null, commentTo: null,
    shareFrom: null, shareTo: null,
    payGmvFrom: null, payGmvTo: null,
    payOrderFrom: null, payOrderTo: null,
  });

  // 导入 / 同步弹窗态。
  const [importOpen, setImportOpen] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importCategory, setImportCategory] = useState<"goods" | "normal">("goods");
  const [importUploading, setImportUploading] = useState(false);
  const [importingCategory, setImportingCategory] = useState<"goods" | "normal" | null>(null);
  const [syncOpen, setSyncOpen] = useState(false);
  const [syncCategory, setSyncCategory] = useState<"goods" | "normal">("goods");
  const [syncRange, setSyncRange] = useState<[dayjs.Dayjs, dayjs.Dayjs] | null>(null);
  const [syncingCategory, setSyncingCategory] = useState<"goods" | "normal" | null>(null);

  // 批量删除。
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [batchDeleteCategory, setBatchDeleteCategory] = useState<"goods" | "normal">("goods");
  const [batchDeleteMode, setBatchDeleteMode] = useState<"local" | "platform">("local");
  const [batchDeleting, setBatchDeleting] = useState(false);
  // 操作列单篇删除中（转圈用）。
  const [deletingNoteId, setDeletingNoteId] = useState<string | null>(null);

  // 无流量清理。
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [cleanupCategory, setCleanupCategory] = useState<"goods" | "normal">("goods");
  const [cleanupCandidates, setCleanupCandidates] = useState<NoTrafficCandidate[]>([]);
  const [cleanupTotal, setCleanupTotal] = useState(0);
  const [cleanupActiveTaskId, setCleanupActiveTaskId] = useState<number | null>(null);
  // 是否有清理任务在执行中（进页面/切账号时查一次，按钮据此置灰「清理无流量中」）。
  const [cleanupRunning, setCleanupRunning] = useState(false);
  const [cleanupLogOpen, setCleanupLogOpen] = useState(false);
  const [cleanupLogs, setCleanupLogs] = useState<NoTrafficCleanupLogItem[]>([]);
  const [cleanupLogTotal, setCleanupLogTotal] = useState(0);
  // 清理日志分页（按批次分页，20/50/100）。
  const [cleanupLogPage, setCleanupLogPage] = useState(1);
  const [cleanupLogPageSize, setCleanupLogPageSize] = useState(20);
  const [cleanupSelected, setCleanupSelected] = useState<Set<string>>(new Set());
  // 清理候选分页（50/100/500，用户口径）。
  const [cleanupPage, setCleanupPage] = useState(1);
  const [cleanupPageSize, setCleanupPageSize] = useState(50);
  // 清理指标区间筛选（2026-10-12）：7 指标 min~max，null = 该侧不筛。
  const [cleanupRange, setCleanupRange] = useState<Record<string, number | null>>({
    read_min: null, read_max: null,
    like_min: null, like_max: null,
    collect_min: null, collect_max: null,
    comment_min: null, comment_max: null,
    share_min: null, share_max: null,
    pay_gmv_min: null, pay_gmv_max: null,
    pay_order_min: null, pay_order_max: null,
  });

  // 详情弹窗。
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailNote, setDetailNote] = useState<DatacenterStatItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<ReturnType<typeof quietError> | null>(null);
  const [hydrated, setHydrated] = useState<Awaited<ReturnType<typeof hydrateOfflineNoteDetail>> | null>(null);

  // 商品详情弹窗（点「商品名称」列触发）。
  const [productDetailOpen, setProductDetailOpen] = useState(false);
  const [productDetail, setProductDetail] = useState<NoteGoodsDetail | null>(null);
  const [productDetailLoading, setProductDetailLoading] = useState(false);

  // 已复制的笔记 ID 集合（点击复制成功后变蓝，视觉反馈）。
  const [copiedNoteIds, setCopiedNoteIds] = useState<Set<string>>(new Set());

  const requestSeq = useRef(0);

  // 进页面拉一次账号列表（账号矩阵页同款；失败静默保持空）。
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const loaded = await fetchAccounts("xhs");
        if (!cancelled) setAccounts(loaded);
      } catch {
        // 忽略。
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const accountOptions = useMemo(
    () =>
      accounts.map((account) => ({
        value: account.id,
        label: account.nickname || `账号 ${account.id}`,
      })),
    [accounts],
  );

  async function loadPage(
    category: "goods" | "normal",
    page = 1,
    sortOverride?: { field: string; order: "asc" | "desc" } | null,
  ) {
    if (!selectedAccountId) return;
    const seq = ++requestSeq.current;
    setPanels((cur) => ({ ...cur, [category]: { ...cur[category], loading: true, error: null } }));
    try {
      const kw = keyword[category];
      const s = sortOverride !== undefined ? sortOverride : sort[category];
      const filters = {
        ...(searchFilters.publishedRange
          ? {
              //: publish_time 列存的是**毫秒**（见 fmtTime 注），筛选端点也用毫秒对齐。
              publishedFrom: searchFilters.publishedRange[0].startOf("day").valueOf(),
              publishedTo: searchFilters.publishedRange[1].endOf("day").valueOf(),
            }
          : {}),
        ...(searchFilters.deleted !== "all" ? { deleted: searchFilters.deleted } : {}),
        noteId: noteIdQuery.trim() || undefined,
        readFrom: searchFilters.readFrom ?? undefined,
        readTo: searchFilters.readTo ?? undefined,
        likeFrom: searchFilters.likeFrom ?? undefined,
        likeTo: searchFilters.likeTo ?? undefined,
        collectFrom: searchFilters.collectFrom ?? undefined,
        collectTo: searchFilters.collectTo ?? undefined,
        commentFrom: searchFilters.commentFrom ?? undefined,
        commentTo: searchFilters.commentTo ?? undefined,
        shareFrom: searchFilters.shareFrom ?? undefined,
        shareTo: searchFilters.shareTo ?? undefined,
        payGmvFrom: searchFilters.payGmvFrom ?? undefined,
        payGmvTo: searchFilters.payGmvTo ?? undefined,
        payOrderFrom: searchFilters.payOrderFrom ?? undefined,
        payOrderTo: searchFilters.payOrderTo ?? undefined,
      };
      const data = await fetchOfflineNotes(category, selectedAccountId, page, 20, kw, undefined, s?.field, s?.order, filters);
      if (seq !== requestSeq.current) return;
      const items = (data.items as unknown as DatacenterStatItem[]);
      setPanels((cur) => ({
        ...cur,
        [category]: {
          items,
          loading: false,
          error: null,
          loaded: true,
          hasMore: page * 20 < data.total,
          total: data.total,
        },
      }));
    } catch (error) {
      if (seq !== requestSeq.current) return;
      const { message: reason, code } = readError(error);
      setPanels((cur) => ({
        ...cur,
        [category]: { ...cur[category], loading: false, error: quietError(reason, code), loaded: true },
      }));
    }
  }

  useEffect(() => {
    if (selectedAccountId) void loadPage(activeTab, 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, selectedAccountId]);

  /** 点「查询」：清勾选 + 回第 1 页 + 重拉（带当前搜索条件）。 */
  function runSearch() {
    if (!selectedAccountId) return;
    setSelected((cur) => ({ ...cur, [activeTab]: new Set() }));
    setPage((cur) => ({ ...cur, [activeTab]: 1 }));
    void loadPage(activeTab, 1);
  }

  /** 点「重置」：清空全部搜索条件 + 回第 1 页 + 重拉。 */
  function resetSearch() {
    setKeyword((cur) => ({ ...cur, [activeTab]: "" }));
    setNoteIdQuery("");
    setSearchFilters({
      publishedRange: null,
      deleted: "all",
      readFrom: null, readTo: null,
      likeFrom: null, likeTo: null,
      collectFrom: null, collectTo: null,
      commentFrom: null, commentTo: null,
      shareFrom: null, shareTo: null,
      payGmvFrom: null, payGmvTo: null,
      payOrderFrom: null, payOrderTo: null,
    });
    setSelected((cur) => ({ ...cur, [activeTab]: new Set() }));
    setPage((cur) => ({ ...cur, [activeTab]: 1 }));
    //: 重置后立刻重拉（用清空后的条件）。但 state 尚未刷新，这里直接传空 filters 重拉。
    void (async () => {
      if (!selectedAccountId) return;
      const seq = ++requestSeq.current;
      setPanels((cur) => ({ ...cur, [activeTab]: { ...cur[activeTab], loading: true, error: null } }));
      try {
        const data = await fetchOfflineNotes(activeTab, selectedAccountId, 1, 20, "", undefined, undefined, undefined, {});
        if (seq !== requestSeq.current) return;
        setPanels((cur) => ({
          ...cur,
          [activeTab]: {
            items: data.items as unknown as DatacenterStatItem[],
            loading: false, error: null, loaded: true,
            hasMore: 20 < data.total, total: data.total,
          },
        }));
      } catch (error) {
        if (seq !== requestSeq.current) return;
        const { message: reason, code } = readError(error);
        setPanels((cur) => ({
          ...cur,
          [activeTab]: { ...cur[activeTab], loading: false, error: quietError(reason, code), loaded: true },
        }));
      }
    })();
  }

  async function handleImport() {
    if (!selectedAccountId || !importFile) return;
    setImportUploading(true);
    try {
      await importNoteData(importFile, importCategory, selectedAccountId);
      message.success("导入任务已提交，解析完成后将通过站内消息通知");
      setImportOpen(false);
      setImportFile(null);
      setImportingCategory(importCategory);
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setImportUploading(false);
    }
  }

  async function handleSync() {
    if (!selectedAccountId) return;
    setSyncingCategory(syncCategory);
    try {
      await syncNoteData(
        selectedAccountId,
        syncCategory,
        syncRange?.[0]?.format("YYYY-MM-DD"),
        syncRange?.[1]?.format("YYYY-MM-DD"),
      );
      message.success("同步任务已提交，完成后将通过站内消息通知");
      setSyncOpen(false);
      void loadPage(syncCategory, 1);
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setSyncingCategory(null);
    }
  }

  function toggleSelect(category: "goods" | "normal", noteId: string) {
    setSelected((cur) => {
      const next = new Set(cur[category]);
      if (next.has(noteId)) next.delete(noteId);
      else next.add(noteId);
      return { ...cur, [category]: next };
    });
  }

  async function handleBatchDelete() {
    const ids = Array.from(selected[batchDeleteCategory]);
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
      setSelected((cur) => ({ ...cur, [batchDeleteCategory]: new Set() }));
      setPanels((cur) => ({ ...cur, [batchDeleteCategory]: emptyPanel() }));
      void loadPage(batchDeleteCategory, 1);
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setBatchDeleting(false);
    }
  }

  /** 操作列单篇删除：`deletePlatform` 决定「只删本地」还是「删本地 + 平台」。 */
  async function deleteNote(note: DatacenterStatItem, category: "goods" | "normal", deletePlatform: boolean) {
    setDeletingNoteId(note.note_id);
    try {
      const result = await batchDeleteNotes([note.note_id], deletePlatform);
      if (result.failed.length) {
        message.warning(`已删除 ${result.deleted} 篇，${result.failed.length} 篇失败：${result.failed[0]?.reason || ""}`);
      } else {
        message.success(deletePlatform ? "已删除本地数据与平台笔记" : "已删除本地笔记数据");
      }
      setSelected((cur) => ({ ...cur, [category]: new Set() }));
      setPanels((cur) => ({ ...cur, [category]: emptyPanel() }));
      void loadPage(category, 1);
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setDeletingNoteId(null);
    }
  }

  async function loadCleanupCandidates(category: "goods" | "normal", pageNo = 1, pageSize = 50) {
    if (!selectedAccountId) return;
    try {
      const data = await fetchNoTrafficCandidates(
        selectedAccountId,
        EMPTY_NO_TRAFFIC_THRESHOLDS,
        noTrafficQuery(category, cleanupRange),
        pageNo,
        pageSize,
      );
      setCleanupCandidates(data.candidates);
      setCleanupTotal(data.total);
      setCleanupPage(data.page);
      setCleanupPageSize(data.page_size);
    } catch (error) {
      message.error(readError(error).message);
    }
  }

  async function handleStartCleanup() {
    if (!selectedAccountId) return;
    const noteIds = Array.from(cleanupSelected);
    if (!noteIds.length) {
      message.warning("请先勾选要清理的笔记");
      return;
    }
    await doStartCleanup(noteIds);
  }

  /** 清理当前筛选下的**全部候选**（不传 note_ids = 后端按指标区间全量清）。 */
  async function handleCleanupAll() {
    if (!selectedAccountId) return;
    await doStartCleanup([]);
  }

  async function doStartCleanup(noteIds: string[]) {
    if (!selectedAccountId) return;
    try {
      const res = await startNoTrafficCleanup(
        selectedAccountId,
        EMPTY_NO_TRAFFIC_THRESHOLDS,
        noTrafficQuery(cleanupCategory, cleanupRange),
        50,
        noteIds,
      );
      message.success("清理任务已发起，完成后可查看日志");
      setCleanupOpen(false);
      setCleanupSelected(new Set());
      setCleanupRunning(true);
      setCleanupActiveTaskId(res.task_id); // 用真实 task_id 驱动轮询收口。
    } catch (error) {
      message.error(readError(error).message);
    }
  }

  async function loadCleanupLogs(page = 1, pageSize = 20) {
    if (!selectedAccountId) return;
    try {
      const data = await fetchNoTrafficCleanupLogs({ accountId: selectedAccountId, page, limit: pageSize });
      setCleanupLogs(data.items);
      setCleanupLogTotal(data.total);
    } catch (error) {
      message.error(readError(error).message);
    }
  }

  // 进页面 / 切账号时查一次「是否有清理任务在执行」，据此置灰「清理无流量」按钮。
  useEffect(() => {
    if (!selectedAccountId) return;
    let cancelled = false;
    (async () => {
      try {
        const active = await fetchNoTrafficCleanupActive(selectedAccountId);
        if (!cancelled) {
          setCleanupRunning(Boolean(active?.running));
          if (active?.task_id != null) setCleanupActiveTaskId(active.task_id);
        }
      } catch {
        // 查询失败静默，按钮保持可点。
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountId]);

  // 轮询清理任务状态，收口「正在清理」按钮态（点击清理后或进页面发现执行中时启动）。
  useEffect(() => {
    if (!cleanupActiveTaskId || !selectedAccountId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const active = await fetchNoTrafficCleanupActive(selectedAccountId);
        if (!cancelled && active && !active.running) {
          setCleanupActiveTaskId(null);
          setCleanupRunning(false);
          void loadPage(activeTab, 1);
        }
      } catch {
        // 查询失败不打断轮询。
      }
    };
    const timer = setInterval(() => void poll(), 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cleanupActiveTaskId, selectedAccountId]);

  // 导入解析中按钮态收口：轮询任务日志。
  useEffect(() => {
    if (!importingCategory || !selectedAccountId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const logs = await fetchScheduleLogs({ job_key: "system.note_data_sync", status: "running", page_size: 50 });
        const importing = (logs?.items ?? []).some((l: { job_name?: string }) => l.job_name === "离线笔记数据导入");
        if (!cancelled && !importing) {
          const cat = importingCategory;
          setImportingCategory(null);
          void loadPage(cat, 1);
        }
      } catch {
        // 忽略。
      }
    };
    const timer = setInterval(() => void poll(), 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [importingCategory, selectedAccountId]);

  async function openDetail(note: DatacenterStatItem) {
    setDetailNote(note);
    setDetailOpen(true);
    setDetailLoading(true);
    setDetailError(null);
    setHydrated(null);
    try {
      const result = await hydrateOfflineNoteDetail(note.note_id);
      setHydrated(result);
    } catch (error) {
      const { message: reason, code } = readError(error);
      setDetailError(quietError(reason, code));
    } finally {
      setDetailLoading(false);
    }
  }

  /** 点「商品名称」列 → 弹商品详情（按 item_id 查本地台账商品，零平台请求）。 */
  async function openProductDetail(note: DatacenterStatItem) {
    if (!note.item_id) {
      message.warning("该笔记未挂载商品");
      return;
    }
    setProductDetailOpen(true);
    setProductDetailLoading(true);
    setProductDetail(null);
    try {
      const detail = await fetchNoteGoodsDetail(note.item_id);
      setProductDetail(detail);
    } catch (error) {
      message.error(readError(error).message);
    } finally {
      setProductDetailLoading(false);
    }
  }

  function renderTable(category: "goods" | "normal") {
    const panel = panels[category];
    if (panel.loading && panel.items.length === 0) {
      //: 2026-10-20 统一：等待一律走 `<WaitMask>`。
      return <WaitMask open title="加载中" />;
    }
    if (panel.error && panel.items.length === 0) {
      return renderFailure(panel.error);
    }
    if (panel.items.length === 0) {
      return <Empty description="没有数据（可能是该账号在此分类下确实为空）" />;
    }
    // 列按「全部字段顺序」过滤出当前可见的。
    const SORTABLE_KEYS = new Set([
      "read_count", "like_count", "collect_count", "comment_count",
      "share_count", "pay_gmv", "pay_order_cnt", "publish_time", "stat_date",
    ]);
    const columns: ColumnsType<DatacenterStatItem> = Object.keys(ALL_COLUMNS)
      .filter((key) => visibleColumns.includes(key))
      .map((key) => ({
        key,
        dataIndex: key,
        title: ALL_COLUMNS[key],
        ellipsis: true,
        //: 指标列可排序（服务端排序，`sorter: true` 让表头出箭头 + 进 onChange）。
        sorter: SORTABLE_KEYS.has(key) ? true : undefined,
        render: (_: unknown, record: DatacenterStatItem) => {
          // 笔记 ID：点击立即复制到剪贴板；**默认不蓝**，复制成功后变蓝。
          if (key === "note_id") {
            const copied = copiedNoteIds.has(record.note_id);
            return (
              <span
                style={{
                  fontSize: 12,
                  cursor: "pointer",
                  color: copied ? "#1677ff" : "inherit",
                }}
                title="点击复制笔记 ID"
                onClick={(e) => {
                  e.stopPropagation();
                  if (record.note_id) {
                    void navigator.clipboard.writeText(record.note_id).then(
                      () => {
                        message.success("已复制笔记 ID");
                        setCopiedNoteIds((cur) => new Set(cur).add(record.note_id));
                      },
                      () => message.error("复制失败"),
                    );
                  }
                }}
              >
                {record.note_id || ""}
              </span>
            );
          }
          // 笔记标题：可点击链接（悬停蓝色），点击弹笔记详情。
          if (key === "title") {
            return (
              <a
                style={{ fontSize: 13 }}
                onClick={(e) => {
                  e.stopPropagation();
                  void openDetail(record);
                }}
              >
                {record.title || record.item_title || "（无标题）"}
              </a>
            );
          }
          // 商品名称：可点击链接（悬停蓝色），点击弹商品详情。
          if (key === "item_title") {
            return (
              <a
                style={{ fontSize: 13 }}
                onClick={(e) => {
                  e.stopPropagation();
                  openProductDetail(record);
                }}
              >
                {record.item_title || "（无）"}
              </a>
            );
          }
          return renderCell(record, key);
        },
      }));
    // 固定操作列（右侧）：删除（tip 弹窗选「只删本地」/「删本地+平台」）。
    columns.push({
      key: "action",
      title: "操作",
      width: 90,
      fixed: "right" as const,
      render: (_: unknown, record: DatacenterStatItem) => (
        <Popover
          trigger="click"
          placement="left"
          content={
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <Button
                type="link"
                size="small"
                danger
                loading={deletingNoteId === record.note_id}
                onClick={() => void deleteNote(record, category, false)}
              >
                删除本地笔记数据
              </Button>
              <Button
                type="link"
                size="small"
                danger
                loading={deletingNoteId === record.note_id}
                onClick={() => void deleteNote(record, category, true)}
              >
                删除本地 + 平台笔记
              </Button>
            </div>
          }
        >
          <Button
            type="link"
            size="small"
            danger
            icon={<DeleteOutlined />}
            onClick={(e) => e.stopPropagation()}
          >
            删除
          </Button>
        </Popover>
      ),
    });
    return (
      //: 复用商品页 `.shop-items-table` 的 flex 高度链 CSS（分页钉卡底、表体独立滚动）。
      <div className="shop-items-table" style={{ flex: 1, minHeight: 0 }}>
      <Table<DatacenterStatItem>
        rowKey={(r) => r.note_id}
        size="small"
          //: ⚠️ 2026-10-20 统一：Table 自带的 loading 会渲染一层 antd Spin 覆盖，与公共 WaitMask 叠成两层 ⇒ 移除，等待交给 WaitMask。
        columns={columns}
        dataSource={panel.items}
        rowSelection={{
          selectedRowKeys: Array.from(selected[category]),
          onChange: (keys) => {
            const next = new Set(keys.map(String));
            setSelected((cur) => ({ ...cur, [category]: next }));
          },
        }}
        onRow={(record) => ({
          //: 不再整行点击弹详情（2026-10-12 用户口径）：只有「笔记标题」列点击弹笔记详情、
          //: 「商品名称」列点击弹商品详情。整行 cursor 保持默认。
          style: { cursor: "default" },
        })}
        rootClassName="shop-items-table"
        onChange={(_pagination, _filters, sorter) => {
          // 排序变化 → 记到 state 并回第 1 页重拉（服务端排序，走 loadPage 带全部筛选）。
          // ⚠️ antd 的 order 是 `ascend`/`descend`，后端契约是 `asc`/`desc`，必须转换
          //（直接强转会发出 `ascend` ⇒ 422 `sort_order should match ^(asc|desc)$`）。
          const s = Array.isArray(sorter) ? sorter[0] : sorter;
          const nextSort = s && s.order
            ? {
                field: String(s.field),
                order: (s.order === "ascend" ? "asc" : s.order === "descend" ? "desc" : null) as "asc" | "desc" | null,
              }
            : null;
          const normalized = nextSort && nextSort.order
            ? { field: nextSort.field, order: nextSort.order }
            : null;
          setSort((cur) => ({ ...cur, [category]: normalized }));
          setPage((cur) => ({ ...cur, [category]: 1 }));
          void loadPage(category, 1, normalized);
        }}
        pagination={{
          current: page[category],
          pageSize: 20,
          total: panel.total,
          showTotal: (total) => `共 ${total} 篇`,
          onChange: (p) => {
            setPage((cur) => ({ ...cur, [category]: p }));
            void loadPage(category, p);
          },
        }}
        scroll={{ x: 1300, y: "100%" }}
      />
      </div>
    );
  }

  return (
    <Flex
      vertical
      gap={10}
      //: **上中下三段式布局**（参考商品页 `shop-items-page.tsx` 同款口径）：
      //:   上 = 搜索条件卡（`flexShrink:0`，固定）
      //:   中 = 数据中心台账卡（`flex:1; minHeight:0`，卡内「卡头按钮 / 表格 / 分页」）
      //:   下 = 分页由 Table 自带（贴在卡体底部，不随表格体滚动）。
      //: 高度 = 视口 - 顶栏(48) - 内容上下内边距(15+24) = `calc(100vh - 87px)`。
      style={{ height: "calc(100vh - 87px)" }}
    >
      {/* **搜索区**：只有一层卡，标题「搜索条件」。常驻一行 = 账号 + 分类 Tab +
          关键词 + 发布时间区间 + 已删除 + 查询/重置/更多筛选 ⌄；点「更多筛选」
          追加指标区间条件（阅读/点赞/收藏/评论/分享/GMV/订单），末位按钮变「收起 ^」。 */}
      <Card
        size="small"
        style={{ marginTop: -5, flexShrink: 0 }}
        title="搜索条件"
      >
        {accounts.length === 0 && (
          <Alert
            style={{ marginBottom: 12 }}
            type="warning"
            showIcon
            message="没有小红书账号可选"
            description="数据中心按账号隔离；请先添加账号并导入千帆登录态，才能同步笔记数据。"
          />
        )}

        <div style={{ rowGap: 8, display: "flex", flexWrap: "wrap", alignItems: "center", columnGap: 16 }}>
          {/* 账号（每次查询都要看的一项，台账按账号隔离）。 */}
          <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <Text type="secondary">账号：</Text>
            <Select
              style={{ width: 180 }}
              value={selectedAccountId ?? undefined}
              placeholder="选择账号"
              options={accountOptions}
              notFoundContent="没有小红书账号"
              onChange={(v) => {
                setSelectedAccountId(v ?? null);
                setPanels({ goods: emptyPanel(), normal: emptyPanel() });
                setPage({ goods: 1, normal: 1 });
              }}
            />
          </div>

          {/* 分类 Tab（商业 / 普通）并进搜索区一行。 */}
          <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <Text type="secondary">分类：</Text>
            <Select
              style={{ width: 140 }}
              value={activeTab}
              options={[
                { value: "goods", label: "商业笔记" },
                { value: "normal", label: "普通笔记" },
              ]}
              onChange={(v) => setActiveTab(v as "goods" | "normal")}
            />
          </div>

          {/* 关键词（标题模糊）。 */}
          <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <Text type="secondary">关键词：</Text>
            <Input
              allowClear
              style={{ width: 180 }}
              placeholder="标题模糊查询"
              value={keyword[activeTab]}
              onChange={(e) => setKeyword((cur) => ({ ...cur, [activeTab]: e.target.value }))}
              onPressEnter={() => runSearch()}
            />
          </div>

          {/* 笔记 ID（精确查询，所有分类通用）。 */}
          <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <Text type="secondary">笔记 ID：</Text>
            <Input
              allowClear
              style={{ width: 180 }}
              placeholder="按笔记 ID 精确查询"
              value={noteIdQuery}
              onChange={(e) => setNoteIdQuery(e.target.value)}
              onPressEnter={() => runSearch()}
            />
          </div>

          {/* 发布时间区间（常驻）。 */}
          <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <Text type="secondary">发布时间：</Text>
            <DatePicker.RangePicker
              style={{ width: 220 }}
              placeholder={["开始日期", "截止日期"]}
              allowClear
              value={searchFilters.publishedRange}
              onChange={(v) =>
                setSearchFilters((cur) => ({
                  ...cur,
                  publishedRange: v as [dayjs.Dayjs, dayjs.Dayjs] | null,
                }))
              }
            />
          </div>

          {/* 已删除开关（已隐藏，2026-10-12 用户口径「状态隐藏」）：删除态默认不展示，筛选恒为 all。 */}

          {/* 指标区间（折叠，点「更多筛选」展开）。 */}
          {searchExpanded &&
            METRIC_RANGE_FIELDS.map((m) => (
              <div key={m.key} style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                <Text type="secondary">{m.label}：</Text>
                <InputNumber
                  style={{ width: 90 }}
                  min={0}
                  placeholder="最低"
                  value={searchFilters[m.fromKey]}
                  onChange={(v) => setSearchFilters((cur) => ({ ...cur, [m.fromKey]: v as number | null }))}
                />
                <Text type="secondary">~</Text>
                <InputNumber
                  style={{ width: 90 }}
                  min={0}
                  placeholder="最高"
                  value={searchFilters[m.toKey]}
                  onChange={(v) => setSearchFilters((cur) => ({ ...cur, [m.toKey]: v as number | null }))}
                />
              </div>
            ))}

          {/* 动作组（末位）：查询 / 重置 / 更多筛选（展开态变「收起」）。与条件同行，不换行。 */}
          <Space>
            <Button type="primary" icon={<SearchOutlined />} loading={panels[activeTab].loading} onClick={() => runSearch()}>
              查询
            </Button>
            <Button onClick={() => resetSearch()}>重置</Button>
            <Button
              type="link"
              icon={searchExpanded ? <ArrowUpOutlined /> : <ArrowDownOutlined />}
              iconPosition="end"
              onClick={() => setSearchExpanded((prev) => !prev)}
            >
              {searchExpanded ? "收起" : "更多筛选"}
            </Button>
          </Space>
        </div>
      </Card>

      {/* **台账卡**：`flex:1 minHeight:0` 吃掉剩余高度，卡内切「卡头按钮 / 表格体」。 */}
      <Card
        size="small"
        style={{ marginTop: 0, flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
        styles={{ body: { padding: 12, flex: 1, minHeight: 0, display: "flex", flexDirection: "column" } }}
        title="数据中心台账"
        extra={
          <Space size={8}>
            <Tooltip title="同步离线笔记数据（最长 90 天）">
              <Button
                icon={<SyncOutlined />}
                loading={syncingCategory === activeTab}
                onClick={() => {
                  setSyncCategory(activeTab);
                  setSyncRange(null);
                  setSyncOpen(true);
                }}
              >
                {syncingCategory === activeTab ? "同步中" : "同步"}
              </Button>
            </Tooltip>
            <Tooltip title="导入千帆数据中心导出的 xlsx 数据包">
              <Button
                icon={<UploadOutlined />}
                loading={importingCategory === activeTab}
                onClick={() => {
                  setImportCategory(activeTab);
                  setImportOpen(true);
                  setImportFile(null);
                }}
              >
                {importingCategory === activeTab ? "解析中" : "导入数据文件"}
              </Button>
            </Tooltip>
            <Tooltip title={cleanupRunning ? "清理任务执行中" : "清理无流量笔记"}>
              <Button
                icon={<DeleteOutlined />}
                disabled={cleanupRunning}
                onClick={() => {
                  setCleanupCategory(activeTab);
                  setCleanupOpen(true);
                  setCleanupCandidates([]);
                  setCleanupSelected(new Set());
                  void loadCleanupCandidates(activeTab);
                }}
              >
                {cleanupRunning ? "清理无流量中" : "清理无流量"}
              </Button>
            </Tooltip>
            <Tooltip title={selected[activeTab].size > 0 ? `删除选中的 ${selected[activeTab].size} 篇` : "勾选后批量删除"}>
              <Button
                danger
                icon={<DeleteOutlined />}
                disabled={selected[activeTab].size === 0}
                onClick={() => {
                  setBatchDeleteCategory(activeTab);
                  setBatchDeleteMode("local");
                  setBatchDeleteOpen(true);
                }}
              >
                批量删除{selected[activeTab].size > 0 ? `（${selected[activeTab].size}）` : ""}
              </Button>
            </Tooltip>
            <Tooltip title="清理日志">
              <Button
                icon={<HistoryOutlined />}
                onClick={() => {
                  setCleanupLogOpen(true);
                  setCleanupLogPage(1);
                  void loadCleanupLogs(1, cleanupLogPageSize);
                }}
              >
                清理日志
              </Button>
            </Tooltip>
            <Tooltip title="刷新列表">
              <Button
                icon={<ReloadOutlined />}
                onClick={() => {
                  setSelected((cur) => ({ ...cur, [activeTab]: new Set() }));
                  setPanels((cur) => ({ ...cur, [activeTab]: emptyPanel() }));
                  setPage((cur) => ({ ...cur, [activeTab]: 1 }));
                  void loadPage(activeTab, 1);
                }}
              />
            </Tooltip>
            {/* **列设置图标放右侧**（用户口径「列设置放右侧，使用图标」）：纯图标按钮，无文字。 */}
            <Tooltip title="列设置">
              <Popover
                trigger="click"
                placement="bottomRight"
                content={
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {Object.keys(ALL_COLUMNS).map((key) => (
                      <Checkbox
                        key={key}
                        checked={visibleColumns.includes(key)}
                        onChange={(e) => {
                          const checked = e.target.checked;
                          setVisibleColumns((cur) =>
                            checked ? [...cur, key] : cur.filter((k) => k !== key),
                          );
                        }}
                      >
                        {ALL_COLUMNS[key]}
                      </Checkbox>
                    ))}
                  </div>
                }
              >
                <Button type="text" icon={<SettingOutlined />} aria-label="列设置" />
              </Popover>
            </Tooltip>
          </Space>
        }
      >
        {renderTable(activeTab)}
      </Card>

      {/* 导入 xlsx 弹窗 */}
      <Modal
        centered
        open={importOpen}
        title={`导入${CATEGORY_LABELS[importCategory]}数据`}
        onCancel={() => setImportOpen(false)}
        onOk={() => void handleImport()}
        confirmLoading={importUploading}
        okText="提交导入"
        okButtonProps={{ disabled: !importFile }}
      >
        <Upload.Dragger
          accept=".xlsx"
          maxCount={1}
          beforeUpload={(file) => {
            setImportFile(file);
            return false;
          }}
          onRemove={() => setImportFile(null)}
        >
          <p>点击或拖拽上传 .xlsx 文件（千帆数据中心导出的笔记数据）</p>
        </Upload.Dragger>
      </Modal>

      {/* 同步弹窗 */}
      <Modal
        centered
        open={syncOpen}
        title={`同步${CATEGORY_LABELS[syncCategory]}数据`}
        onCancel={() => setSyncOpen(false)}
        onOk={() => void handleSync()}
        confirmLoading={syncingCategory === syncCategory}
        okText="开始同步"
      >
        <Paragraph type="secondary">从千帆数据中心拉取离线笔记数据（最长 90 天）。</Paragraph>
        <DatePicker.RangePicker
          style={{ width: "100%" }}
          value={syncRange}
          onChange={(v) => setSyncRange(v as [dayjs.Dayjs, dayjs.Dayjs] | null)}
        />
      </Modal>

      {/* 批量删除弹窗 */}
      <Modal
        centered
        open={batchDeleteOpen}
        title="批量删除"
        onCancel={() => setBatchDeleteOpen(false)}
        onOk={() => void handleBatchDelete()}
        confirmLoading={batchDeleting}
        okText="删除"
        okButtonProps={{ danger: true }}
      >
        <Paragraph>已选 {selected[batchDeleteCategory].size} 篇笔记，选择删除方式：</Paragraph>
        <Radio.Group
          value={batchDeleteMode}
          onChange={(e) => setBatchDeleteMode(e.target.value)}
        >
          <Radio value="local">只删本地</Radio>
          <Radio value="platform">删本地 + 平台</Radio>
        </Radio.Group>
      </Modal>

      {/* 无流量清理弹窗：指标区间筛选 + 候选列表（选指定/全选）。 */}
      <Modal
        centered
        open={cleanupOpen}
        title="清理无流量笔记"
        onCancel={() => setCleanupOpen(false)}
        width="75vw"
        footer={
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <Popconfirm
              title={`确认清理全部候选笔记？`}
              description={`将清理当前筛选条件下的全部 ${cleanupTotal} 篇笔记（含平台笔记 + 本地软删除）。`}
              okText="确认清理"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => void handleCleanupAll()}
            >
              <Button danger disabled={cleanupTotal === 0}>
                清理全部候选笔记（{cleanupTotal}）
              </Button>
            </Popconfirm>
            <Space>
              <Button onClick={() => setCleanupOpen(false)}>取消</Button>
              <Button type="primary" danger disabled={cleanupSelected.size === 0} onClick={() => void handleStartCleanup()}>
                清理勾选的（{cleanupSelected.size}）
              </Button>
            </Space>
          </div>
        }
      >
        {/* 指标区间筛选（2026-10-12）：每指标 min~max，空 = 该侧不筛，交集 = 清理范围。 */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12, alignItems: "center" }}>
          {METRIC_RANGE_FIELDS.map((m) => (
            <div key={m.key} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>{m.label}</Text>
              <InputNumber
                size="small"
                style={{ width: 70 }}
                min={0}
                placeholder="最小"
                value={cleanupRange[m.minKey] ?? null}
                onChange={(v) => setCleanupRange((cur) => ({ ...cur, [m.minKey]: v as number | null }))}
              />
              <Text type="secondary">~</Text>
              <InputNumber
                size="small"
                style={{ width: 70 }}
                min={0}
                placeholder="最大"
                value={cleanupRange[m.maxKey] ?? null}
                onChange={(v) => setCleanupRange((cur) => ({ ...cur, [m.maxKey]: v as number | null }))}
              />
            </div>
          ))}
          <Button type="primary" size="small" icon={<SearchOutlined />} onClick={() => { setCleanupPage(1); void loadCleanupCandidates(cleanupCategory, 1, cleanupPageSize); }}>
            查询
          </Button>
        </div>
        <Table<NoTrafficCandidate>
          rowKey={(r) => r.note_id}
          size="small"
          dataSource={cleanupCandidates}
          rowSelection={{
            selectedRowKeys: Array.from(cleanupSelected),
            onChange: (keys) => setCleanupSelected(new Set(keys.map(String))),
            preserveSelectedRowKeys: true,
          }}
          pagination={{
            current: cleanupPage,
            pageSize: cleanupPageSize,
            total: cleanupTotal,
            showSizeChanger: true,
            pageSizeOptions: ["50", "100", "500"],
            showTotal: (t) => `共 ${t} 篇`,
            onChange: (p, size) => void loadCleanupCandidates(cleanupCategory, p, size),
          }}
          scroll={{ x: 900, y: 380 }}
          columns={[
            { title: "笔记标题", dataIndex: "title", ellipsis: true, render: (v: string, r) => v || r.note_id },
            { title: "阅读", dataIndex: "browse_num", width: 80, align: "right", render: (v: number | null) => fmtMetric(v) },
            { title: "点赞", dataIndex: "likes", width: 70, align: "right", render: (v: number | null) => fmtMetric(v) },
            { title: "收藏", dataIndex: "collects", width: 70, align: "right", render: (v: number | null) => fmtMetric(v) },
            { title: "评论", dataIndex: "comments", width: 70, align: "right", render: (v: number | null) => fmtMetric(v) },
            { title: "分享", dataIndex: "shares", width: 70, align: "right", render: (v: number | null) => fmtMetric(v) },
            { title: "支付订单", dataIndex: "pay_order_cnt", width: 90, align: "right", render: (v: number | null) => fmtMetric(v) },
            { title: "支付GMV", dataIndex: "pay_gmv", width: 90, align: "right", render: (v: number | null) => fmtMetric(v) },
            { title: "发布时间", dataIndex: "publish_time", width: 150, render: (v: number | null) => fmtTime(v) },
          ]}
        />
      </Modal>

      {/* 清理日志弹窗：树状展示（批次为父行，笔记明细为子行）+ 按批次分页 20/50/100。 */}
      <Modal
        centered
        open={cleanupLogOpen}
        title="清理日志"
        onCancel={() => setCleanupLogOpen(false)}
        footer={null}
        width="80vw"
      >
        <Table<NoTrafficCleanupLogItem>
          rowKey={(r) => r.id}
          size="small"
          dataSource={cleanupLogs}
          pagination={{
            current: cleanupLogPage,
            pageSize: cleanupLogPageSize,
            total: cleanupLogTotal,
            showSizeChanger: true,
            pageSizeOptions: ["20", "50", "100"],
            showTotal: (t) => `共 ${t} 批`,
            onChange: (p, size) => {
              setCleanupLogPage(p);
              setCleanupLogPageSize(size);
              void loadCleanupLogs(p, size);
            },
          }}
          scroll={{ x: 1000, y: 480 }}
          columns={[
            {
              title: "批次号",
              dataIndex: "batch_no",
              width: 160,
              render: (v: string, r) => v || `#${r.id}`,
            },
            { title: "账号", dataIndex: "account_name", width: 120, ellipsis: true },
            {
              title: "清理前/后",
              key: "total",
              width: 100,
              render: (_, r) => `${r.before_total} → ${r.after_total}`,
            },
            { title: "删除", dataIndex: "deleted", width: 60, align: "right" },
            { title: "跳过", dataIndex: "skipped", width: 60, align: "right" },
            { title: "失败", dataIndex: "failed", width: 60, align: "right" },
            {
              title: "状态",
              dataIndex: "status",
              width: 80,
              render: (v: string, r) => (
                <Tag color={r.status === "done" ? "green" : r.status === "failed" ? "red" : "blue"}>
                  {r.status}
                </Tag>
              ),
            },
            { title: "失败原因", dataIndex: "reason", ellipsis: true, render: (v: string) => v || "—" },
            {
              title: "操作时间",
              dataIndex: "finished_at",
              width: 160,
              render: (v: string | null) => (v ? fmtTimeStr(v) : "—"),
            },
          ]}
          expandable={{
            expandedRowRender: (record) => (
              <Table<NoTrafficCleanupItemRow>
                rowKey={(r) => r.id}
                size="small"
                dataSource={record.items ?? []}
                pagination={false}
                columns={[
                  { title: "笔记ID", dataIndex: "note_id", width: 180, ellipsis: true },
                  { title: "标题", dataIndex: "note_title", ellipsis: true },
                  {
                    title: "结果",
                    dataIndex: "result",
                    width: 100,
                    render: (v: string) => (
                      <Tag color={CLEANUP_RESULT_LABEL[v]?.color ?? "default"}>
                        {CLEANUP_RESULT_LABEL[v]?.text ?? v}
                      </Tag>
                    ),
                  },
                  { title: "原因", dataIndex: "reason", ellipsis: true, render: (v: string) => v || "—" },
                  {
                    title: "操作时间",
                    dataIndex: "operated_at",
                    width: 160,
                    render: (v: string | null) => (v ? fmtTimeStr(v) : "—"),
                  },
                ]}
              />
            ),
          }}
        />
      </Modal>

      {/* 商品详情弹窗（点「商品名称」列触发，参考我的笔记页挂载商品区块）。
          ⚠️ 宽度是**视口百分比**（用户口径 2026-10-18：*「商品详情弹窗最大 全屏的85%」*）——
          原为固定 480px，图多/属性多时挤成一列滚不完。
          `maxWidth` 兜住超宽屏：85vw 在 2560 屏上是 2176px，全摊开反而难读。 */}
      <Modal
        centered
        open={productDetailOpen}
        title="商品详情"
        onCancel={() => setProductDetailOpen(false)}
        footer={null}
        width="85vw"
        style={{ maxWidth: 1600 }}
      >
        {productDetailLoading ? (
          /* 2026-10-20 统一：等待一律走 `<WaitMask>`。 */
          <WaitMask open title="加载中" />
        ) : productDetail ? (
          <Card size="small">
            <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
              {productDetail.images && productDetail.images.length > 0 && (
                <img
                  src={productDetail.images[0]}
                  alt={productDetail.item_title || "商品"}
                  style={{ width: 72, height: 72, objectFit: "cover", borderRadius: 6, flex: "0 0 auto" }}
                />
              )}
              <div style={{ flex: "1 1 0%", minWidth: 0 }}>
                <Text strong ellipsis={{ tooltip: productDetail.item_title || "（无商品标题）" }} style={{ fontSize: 13, display: "block" }}>
                  {productDetail.item_title || "（无商品标题）"}
                </Text>
                {(productDetail.variant || productDetail.price) && (
                  <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 4 }}>
                    {productDetail.variant && (
                      <Text type="secondary" ellipsis={{ tooltip: productDetail.variant }} style={{ fontSize: 12, minWidth: 0, maxWidth: 300, display: "inline-block" }}>
                        {productDetail.variant}
                      </Text>
                    )}
                    {productDetail.price && (
                      <Text strong style={{ fontSize: 14, flex: "0 0 auto", whiteSpace: "nowrap", color: "#ff4d4f" }}>
                        ¥ {(Number(productDetail.price) / 100).toFixed(2)}
                      </Text>
                    )}
                  </div>
                )}
                <div style={{ marginTop: 4 }}>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    ID: {productDetail.item_id}
                  </Text>
                </div>
              </div>
            </div>
          </Card>
        ) : (
          <Empty description="商品详情暂缺" />
        )}
      </Modal>

      {/* 笔记详情弹窗 */}
      <NoteDetailModal
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        title={detailNote?.title || ""}
        loading={detailLoading}
        failure={detailError}
        mediaUrls={hydrated?.asset_urls ?? []}
        videoUrl={hydrated?.video_url ?? ""}
        mediaNamePrefix={detailNote?.note_id}
      >
        {hydrated && hydrated.hydrate_status !== "ok" && (
          <Paragraph type="secondary">详情暂不可用（{hydrated.hydrate_status}）。</Paragraph>
        )}
        {hydrated?.content ? (
          <Paragraph>{hydrated.content}</Paragraph>
        ) : (
          <Paragraph type="secondary">正文暂缺。</Paragraph>
        )}
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            阅读 {fmtMetric(detailNote?.read_count)} · 赞 {fmtMetric(detailNote?.like_count)} ·
            藏 {fmtMetric(detailNote?.collect_count)} · 评 {fmtMetric(detailNote?.comment_count)}
          </Text>
        </div>
        {detailNote?.item_title && (
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>商品：{detailNote.item_title}</Text>
          </div>
        )}
        {hydrated?.topics && hydrated.topics.length > 0 && (
          <div style={{ marginTop: 8 }}>
            {hydrated.topics.map((t) => (
              <span key={t.id} style={{ marginRight: 8, color: p.text }}>#{t.name}</span>
            ))}
          </div>
        )}
      </NoteDetailModal>
    </Flex>
  );
}

export default XhsDatacenterPage;
