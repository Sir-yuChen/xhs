import axios from "axios";
import { message } from "antd";

import type { CleanResultShape } from "./task-records";
import type {
  ItemDraftCategory,
  ItemDraftCreatePayload,
  ItemDraftCreateResult,
  ItemDraftListResult,
  ItemDraftPackageParseResult,
  ItemPublishPayload,
  ItemPublishResult,
  ShopItemOperateAction,
  ShopItemOperateResult,
  ShopItemsSyncResult,
  SystemUpdateDownloadResult,
  SystemUpdateInfo,
  SystemUpdateInstallResult,
  SystemVersionInfo,
  XhsItemEditPayload,
  XhsItemEditResult,
  XhsItemPolishResult,
} from "../types";
import type {
  AuthPayload,
  AutoTask,
  AutoTaskCreatePayload,
  AutoTaskRunResult,
  AutoTaskUpdatePayload,
  AnalyticsCommentInsight,
  AnalyticsHotTopic,
  AnalyticsReportPayload,
  AnalyticsReportResponse,
  AnalyticsTopContent,
  AppNotification,
  BatchCreateDraftsPayload,
  BatchCreateDraftsResponse,
  CreateDraftPayload,
  DashboardOverview,
  Draft,
  DescribeImagePayload,
  GenerateNotePayload,
  GenerateImagePayload,
  GenerateImageResult,
  GeneratedImageAsset,
  GenerateTagsPayload,
  GenerateTitlePayload,
  KeywordGroup,
  KeywordGroupDetail,
  KeywordGroupPayload,
  LlmGatewayConfig,
  LlmGatewayConfigPayload,
  LlmGatewayModelRow,
  LlmGatewayPlatformOption,
  LlmGatewayStats,
  LlmGatewaySyncSummary,
  LlmGatewayAlert,
  LlmGatewayAlertPayload,
  LlmGatewayUsageOverview,
  LlmGatewayUsageDailyItem,
  LlmGatewayUsageBreakdownItem,
  BusinessKeyOption,
  BusinessKeysResponse,
  BusinessProviderListResponse,
  BusinessProviderRow,
  BusinessProviderUpsertPayload,
  ModelType,
  MonitoringNote,
  MonitoringRefreshResponse,
  MonitoringSnapshot,
  MonitoringTarget,
  MonitoringTargetPayload,
  NoteAsset,
  NoteComment,
  NotesExportPayload,
  NotesExportResponse,
  Paginated,
  PlatformAccount,
  PlatformMeta,
  PlatformUser,
  PublishAsset,
  PublishJob,
  PublishJobUpdatePayload,
  RewriteDraftPayload,
  SystemSetting,
  SystemSettingCreatePayload,
  SystemSettingDeleteResult,
  SystemSettingListResponse,
  SystemSettingUpdatePayload,
  RunDueTasksResponse,
  ScheduleIn,
  ScheduleTarget,
  ScheduleStatus,
  ScheduledJob,
  ScheduledJobLog,
  SavedNote,
  SavedNotesPage,
  SaveNotesResponse,
  SendDraftToPublishPayload,
  NoteTopic,
  UserImageFile,
  TaskRecord,
  XhsNoteSearchResponse,
  XhsDataCrawlItem,
  XhsDataCrawlPayload,
  XhsSearchOptions,
  XhsSearchNote,
  XhsSelfCapabilities,
  XhsSelfContentResponse,
  XhsSelfContentScope,
  XhsSelfIdentity,
  XhsSelfSearchResponse,
  XhsInteractionKind,
  XhsInteractionResponse,
  XhsQianfanBinding,
  GenerateDetailImageResult,
  XhsShopItemDetail,
  XhsShopItemsQuery,
  XhsShopItemsResponse,
  PictureSpaceItem,
  PictureSpaceItemsQuery,
  PictureSpaceItemsResponse,
  PictureSpaceResizeCheck,
  PictureSpaceUsage,
  PictureSpaceTreeNode,
  PictureSpaceWriteResult,
  PictureSpaceDeletePreview,
  PictureSpaceTask,
  CategoryPropertyResult,
  PictureSpaceUploadBatchResult,
  PictureSpaceUploadResult,
  RecommendCategoryResult,
  RecommendItemTitleResult,
  XhsShopItemsDeleteResult,
  XhsShopItemStatus,
  MaterialFolder,
  SaveDetailImagesResult,
  MaterialFile,
  PromptModule,
  PromptTemplate,
  LlmMediaSource,
  LlmMediaOptions,
  LlmGenerateQueueSnapshot,
  XhsItemNotesResponse,
  XhsAccountLoginState,
  XhsQrLoginSession,
} from "../types";

const http = axios.create({
  baseURL: "/api",
  timeout: 120000,
});

const REFRESH_TOKEN_KEY = "spider_xhs_refresh_token";
let accessToken: string | null = null;

export type AuthCredentials = {
  username: string;
  password: string;
};

export function getAccessToken(): string | null {
  return accessToken;
}

export function hasRefreshToken(): boolean {
  return Boolean(window.localStorage.getItem(REFRESH_TOKEN_KEY));
}

function getRefreshToken(): string | null {
  return window.localStorage.getItem(REFRESH_TOKEN_KEY);
}

function setAccessToken(token: string | null): void {
  accessToken = token;
}

function persistAuthPayload(payload: AuthPayload): AuthPayload {
  setAccessToken(payload.access_token);
  if (payload.refresh_token) {
    window.localStorage.setItem(REFRESH_TOKEN_KEY, payload.refresh_token);
  }
  return payload;
}

export function clearAuthTokens(): void {
  setAccessToken(null);
  window.localStorage.removeItem(REFRESH_TOKEN_KEY);
}

http.interceptors.request.use((config) => {
  if (accessToken) {
    config.headers.Authorization = `Bearer ${accessToken}`;
  }
  return config;
});

/**
 * 从后端的 `detail` 里取出**给人看的那句话**。
 *
 * 后端对「可自救的失败」一律返回**结构化** detail
 * （`{code, message, platform_message}`，例如 `WEB_SESSION_REQUIRED` /
 * `XSEC_TOKEN_REQUIRED`）—— 那是**唯一**能告诉用户该去做什么的东西。
 * 旧版拦截器只认 `typeof detail === "string"`，于是把它降级成一句
 * 「请求失败，请稍后重试」：**真实原因被丢掉、用户无从自救**，而且这句话在
 * 「账号只是没数据」时也是谎话（2026-09-21 三轮真机抓包证实这三类接口对
 * 创作者账号全部可达，问题只在凭据）。
 *
 * Pydantic 的 422 校验错误是**数组**（`[{loc, msg, type}]`）—— 它不是空话，而是
 * 「哪个字段没通过哪条约束」，正是排查接口异常最需要的信息，所以同样要还原。
 * 只还原**可读**的部分（字段路径 + 原因），丢掉 `type` / `input` 这类内部细节。
 *
 * 返回空串只有一种情况：`detail` 里确实没有任何可读内容 —— 由调用方兜底。
 */
function validationDetailMessage(items: unknown[]): string {
  const parts: string[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const entry = item as { loc?: unknown; msg?: unknown };
    const msg = typeof entry.msg === "string" ? entry.msg.trim() : "";
    if (!msg) continue;
    const loc = Array.isArray(entry.loc) ? entry.loc.map((p) => String(p)).join(".") : "";
    parts.push(loc ? `${loc}：${msg}` : msg);
  }
  return parts.join("；");
}

export function detailMessage(detail: unknown): string {
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) return validationDetailMessage(detail);
  if (detail && typeof detail === "object") {
    const shaped = detail as { message?: unknown; platform_message?: unknown };
    for (const candidate of [shaped.message, shaped.platform_message]) {
      if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
    }
  }
  return "";
}

/**
 * 把一次请求失败转成**可读文案**（页面内展示用，不弹全局 toast）。
 *
 * ⚠️ 单独抽出来是因为它此前在 `accounts-page.tsx` 里**私有实现了一份**
 * （只服务那一个页面），而「商品台账」页也要同一句话（需求 5：两个入口同一套逻辑）
 * ⇒ 若各写一份，两边对「后端给了什么原因」的解读迟早分叉（DEF-044）。
 * 现在这份是唯一实现，`accounts-page` 也已改为 import 它。
 *
 * 三级兜底：后端 detail（字符串 / 对象里的 message / platform_message）→
 * 原生 `Error.message` → `fallback`。
 */
export function readErrorDetail(error: unknown, fallback = "操作失败"): string {
  const detail = detailMessage(
    (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  );
  if (detail) return detail;
  const message = (error as Error)?.message;
  return typeof message === "string" && message.trim() ? message.trim() : fallback;
}

http.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config as typeof error.config & { _authRetry?: boolean; _silent?: boolean };
    if (error.response?.status !== 401 || originalRequest?._authRetry || !getRefreshToken()) {
      if (!originalRequest?._silent) {
        // 结构化 detail 同样是**真原因**（见 `detailMessage`），不能再退化成通用文案。
        message.error(detailMessage(error.response?.data?.detail) || "请求失败，请稍后重试");
      }
      return Promise.reject(error);
    }

    originalRequest._authRetry = true;
    try {
      const token = await refreshAccessToken();
      originalRequest.headers.Authorization = `Bearer ${token}`;
      return http(originalRequest);
    } catch (refreshError) {
      clearAuthTokens();
      message.error("登录已过期，请重新登录");
      return Promise.reject(refreshError);
    }
  }
);

export async function login(credentials: AuthCredentials): Promise<AuthPayload> {
  const response = await http.post<AuthPayload>("/auth/login", credentials);
  return persistAuthPayload(response.data);
}

export async function register(credentials: AuthCredentials): Promise<AuthPayload> {
  const response = await http.post<AuthPayload>("/auth/register", credentials);
  return persistAuthPayload(response.data);
}

export async function refreshAccessToken(): Promise<string> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) {
    throw new Error("Missing refresh token");
  }

  // ⚠️ 必须带 `_authRetry`（+ `_silent`）：**这个请求本身就是恢复手段**，
  // 它自己 401 时绝不能再去走拦截器的「401 ⇒ 刷新 ⇒ 重试」分支。
  //
  // 漏了这个标记的后果是**无限递归**（2026-09-27 打包版实测）：`http.post` 的 401
  // 重新进入同一分支时，`_authRetry` 是 undefined、而 `getRefreshToken()` 仍然非空
  // （旧令牌还在 localStorage 里），于是分支条件为假 ⇒ 又调 `refreshAccessToken()`
  // ⇒ 再 POST ⇒ 再 401 … 承诺**永不 settle**。上游 `bootstrapAuth()` 永远 await，
  // `use-auth` 的 status 卡在 "checking"，界面停在「正在验证登录状态…」；
  // 服务端 5 分钟内被打出 **8.8 万次** `POST /api/auth/refresh` 401。
  //
  // 触发条件不是边界情况：「登录已过期」是**每个用户都会走到**的正常路径
  // （refresh token 过期 / 库被重置 / 用户被删），所以这是 P1 而非边角。
  //
  // `_silent`：失败呈现权归调用点（`use-auth` 的 bootstrap，或拦截器自己的
  // catch 那句「登录已过期，请重新登录」）—— 不标就会在过期时连弹两次。
  const response = await http.post<{ access_token: string; token_type: "bearer" }>(
    "/auth/refresh",
    { refresh_token: refreshToken },
    { _authRetry: true, _silent: true } as never
  );
  setAccessToken(response.data.access_token);
  return response.data.access_token;
}

export async function fetchMe(): Promise<PlatformUser> {
  const response = await http.get<PlatformUser>("/auth/me");
  return response.data;
}

export async function bootstrapAuth(): Promise<PlatformUser | null> {
  if (!getAccessToken() && hasRefreshToken()) {
    await refreshAccessToken();
  }
  if (!getAccessToken()) {
    return null;
  }
  return fetchMe();
}

export async function logout(): Promise<void> {
  try {
    await http.post("/auth/logout");
  } finally {
    clearAuthTokens();
  }
}

/**
 * 平台注册表（后端 `core/platforms.py` 是唯一真相源）。
 *
 * ⚠️ **T-17 / DEF-023**：原实现失败时静默返回前端的 `fallbackPlatforms` 副本 ——
 * 那是**掩盖后端故障**（页面照常显示六个平台，而实际后端可能整个挂掉）。
 * 现在失败就抛，由页面渲染错误态；前端**不再维护第二份平台清单**。
 */
export async function fetchPlatforms(): Promise<PlatformMeta[]> {
  const response = await http.get<Paginated<PlatformMeta>>("/platforms");
  return response.data.items;
}

export async function fetchXhsOverview(): Promise<DashboardOverview> {
  const response = await http.get<DashboardOverview>("/xhs/analytics/overview");
  return response.data;
}

export async function fetchXhsTopContent(): Promise<{ items: AnalyticsTopContent[] }> {
  const response = await http.get<{ items: AnalyticsTopContent[] }>("/xhs/analytics/top-content");
  return response.data;
}

export async function fetchXhsHotTopics(): Promise<{ items: AnalyticsHotTopic[] }> {
  const response = await http.get<{ items: AnalyticsHotTopic[] }>("/xhs/analytics/hot-topics");
  return response.data;
}

export async function fetchXhsCommentInsights(): Promise<AnalyticsCommentInsight> {
  const response = await http.get<AnalyticsCommentInsight>("/xhs/analytics/comment-insights");
  return response.data;
}

// ---- 指标日快照（M2 数据仪表盘，设计 17 §13.5 / §13.9）----------------------
// ⚠️ 采集端点**纯本地零平台请求**（数据源是已落库的笔记/商品行），可随时调用。

/** 快照采集回执：`stat_date` 是被采集的统计日，`collected` 是落库行数。 */
export type MetricsCollectResult = {
  stat_date: string;
  collected: number;
};

/** 笔记指标趋势的按日聚合点（**只含有快照的天**，不补零）。 */
export type NoteMetricsTrendPoint = {
  stat_date: string;
  likes: number;
  collects: number;
  comments: number;
  shares: number;
  views: number;
  engagement: number;
};

/** 商品指标趋势的按日聚合点（价格是**区间最值**，不是求和）。 */
export type ShopMetricsTrendPoint = {
  stat_date: string;
  stock: number;
  sales: number;
  sales_30d: number;
  price_min_fen: number;
  price_max_fen: number;
  cart_count: number;
};

export async function collectNoteMetrics(): Promise<MetricsCollectResult> {
  const response = await http.post<MetricsCollectResult>("/xhs/analytics/metrics/collect");
  return response.data;
}

export async function fetchNoteMetricsTrend(params: {
  days?: number;
  note_pk?: number;
} = {}): Promise<NoteMetricsTrendPoint[]> {
  const response = await http.get<{ series: NoteMetricsTrendPoint[] }>(
    "/xhs/analytics/metrics/trend",
    { params }
  );
  return response.data.series;
}

export async function collectShopMetrics(): Promise<MetricsCollectResult> {
  const response = await http.post<MetricsCollectResult>("/xhs/analytics/shop-metrics/collect");
  return response.data;
}

export async function fetchShopMetricsTrend(params: {
  days?: number;
  product_pk?: number;
} = {}): Promise<ShopMetricsTrendPoint[]> {
  const response = await http.get<{ series: ShopMetricsTrendPoint[] }>(
    "/xhs/analytics/shop-metrics/trend",
    { params }
  );
  return response.data.series;
}

export async function createXhsAnalyticsReport(
  payload: AnalyticsReportPayload = { format: "json" }
): Promise<AnalyticsReportResponse> {
  const response = await http.post<AnalyticsReportResponse>("/xhs/analytics/reports", payload);
  return response.data;
}

export async function fetchAccounts(platform = "xhs"): Promise<PlatformAccount[]> {
  const response = await http.get<Paginated<PlatformAccount>>("/accounts", { params: { platform } });
  return response.data.items;
}

/**
 * 分页取账号（**无限下拉**用，2026-10-06 用户口径「账号下拉框应支持当前所有小红书账号，
 * 无限下拉未实现」）。
 *
 * 与 `fetchAccounts` 的区别：那个只取第一页（返回数组），本函数回**整页对象**
 * （含 `total`），供下拉滚动到底时判断「还有没有下一页」。
 */
export async function fetchAccountsPage(
  platform = "xhs",
  page = 1,
  pageSize = 20
): Promise<Paginated<PlatformAccount>> {
  const response = await http.get<Paginated<PlatformAccount>>("/accounts", {
    params: { platform, page, page_size: pageSize },
  });
  return response.data;
}

/**
 * 同步范围（设计 13 §23，2026-09-22 用户口径：弹窗自选「全量 / 时间区间」）。
 * 与后端 `sync_task_service.normalize_scope` 同口径。
 */
export type SyncScope = "full" | "range";

/** 同步笔记的可选参数（弹窗选择 + 调试用小批量）。 */
export interface SyncNotesOptions {
  /** 调试/测试用：显式限制页数。**不传 = 全量**（§21）。 */
  maxPages?: number;
  scope?: SyncScope;
  /**
   * **同步模式**（设计 13 §22 / §35.15，2026-09-27）。
   *
   * `full` = 真重扫整个列表（翻到平台没有下一页）；不传 = 后端 `auto`
   * （**有同步游标即增量**）。
   *
   * 为什么必须能显式传：`scope` 与 `mode` 是**正交**的两件事 —— `scope=full`
   * 只说「不设时间界」，模式仍由游标决定。用户点「全量同步」却不传 `mode=full`
   * 时，有同步历史的账号照样走增量、第 1 页就增量早停（§35.15.1 实测：同步
   * 「成功」但一条都没补），「全量」这个选项名不副实。
   */
  mode?: "full" | "incremental";
  /** `YYYY-MM-DD`；`scope=range` 时生效。 */
  startDate?: string;
  /** `YYYY-MM-DD`；`scope=range` 时生效（含当天一整天）。 */
  endDate?: string;
}

export type SavedNoteFilters = {
  platform?: string;
  q?: string;
  /**
   * **话题筛选**（设计 13 §34.4，2026-09-25）：原 `tag_id`（标签筛选）——
   * 标签表已并入话题表，走后端 `note_topics` 关系表子查询。取值 = 话题字典行
   * id（`topics.id`）。不传 = 不筛。
   */
  topic_id?: number;
  has_assets?: boolean;
  has_comments?: boolean;
  /**
   * 发布状态筛选（设计 13 §32.3.3，2026-09-23 收敛）。
   *
   * 四档：`undefined`/`"all"` = 不过滤，`"pending"` 待发布、`"published"` 已发布、
   * `"failed"` 发布失败。取值与后端查询参数**逐字同值**（旧 `published: boolean`
   * 两态参数随本次口径变更删除 —— 界面上的「发布失败」这一档旧参数表达不了）。
   */
  publish_status?: string;
  /**
   * **来源账号**筛选（2026-09-23，用户口径「内容库新增根据账号筛选的条件」）。
   *
   * 与「来源账号」列同一事实（`note.platform_account_id`）。不传 = 不筛。
   * 账号下拉候选**只列本平台的账号**（`selectableAccounts`），选项文案复用
   * `accountLabel`（昵称 · 店铺）—— 与商品台账的账号下拉同一套口径。
   */
  platform_account_id?: number;
  /**
   * **笔记类型**筛选（设计 13 §25，2026-09-23 新增）。
   *
   * 取值：`synced` 三方同步 / `local_generated` 本地生成 / `local` 本地采集。
   * 不传 = 不筛；未知取值后端 400 `NOTE_TYPE_UNSUPPORTED`（不猜枚举）。
   */
  note_type?: string;
  /**
   * **挂链商品**筛选（设计 13 §25.8，2026-09-23 新增）。
   *
   * 取值 = 平台商品 ID（`shop_products.item_id`，如商品列表 / 挂链弹窗里的那个）。
   * 走后端 `note_goods` 关系表子查询。不传 = 不筛；未知 ID = 空集（后端不 404）。
   * 挂链笔记弹窗的「在内容库中查看」跳转 `/platforms/xhs/library?item_id=…` 用它。
   */
  item_id?: string;
  page_size?: number;
  /** 页码（内容库「下拉自动加载更多」用；不传 = 第 1 页）。 */
  page?: number;
};

export async function fetchSavedNoteIds(platform = "xhs"): Promise<string[]> {
  const response = await http.get<{ items: string[] }>("/notes/ids", { params: { platform } });
  return response.data.items;
}

export async function fetchSavedNotes(
  platformOrFilters: string | SavedNoteFilters = "xhs"
): Promise<SavedNotesPage> {
  const params =
    typeof platformOrFilters === "string"
      ? { platform: platformOrFilters }
      : {
          platform: platformOrFilters.platform ?? "xhs",
          q: platformOrFilters.q || undefined,
          topic_id: platformOrFilters.topic_id,
          has_assets: platformOrFilters.has_assets,
          has_comments: platformOrFilters.has_comments,
          // `all` 不传（= 不筛）；其余四档原样透传（`"failed"` 是本轮新增的那一档）。
          publish_status:
            platformOrFilters.publish_status && platformOrFilters.publish_status !== "all"
              ? platformOrFilters.publish_status
              : undefined,
          // 账号 / 笔记类型筛选（2026-09-23）：**不传即不筛**，后端兜「不筛」。
          // 这里不做「空串转 undefined」以外的加工 —— 枚举校验是后端的事（400 带支持列表）。
          platform_account_id: platformOrFilters.platform_account_id,
          note_type: platformOrFilters.note_type || undefined,
          // 挂链商品（§25.8）：trim 后空串 = 不筛（与 note_type 同口径）。
          item_id: platformOrFilters.item_id?.trim() || undefined,
          page_size: platformOrFilters.page_size,
          page: platformOrFilters.page,
        };
  const response = await http.get<SavedNotesPage>("/notes", { params });
  return response.data;
}

/**
 * 手动切换一条笔记的发布状态（设计 13 §19.12 / §32.3.3）。
 *
 * 只允许 `pending` / `published` 两档（后端 `Literal` 白名单）。**本地生成笔记
 * 不接受手改**（后端 400 `NOTE_STATUS_MANAGED`）—— 它的状态由发布链路决定。
 *
 * 后端返回更新后的整行（含 `tags`），调用方**就地替换列表行**即可；但 `stats`
 * 会随之变化，所以还应重新拉一次列表刷新统计（本接口不返回 `stats` —— 它跟筛选
 * 条件无关，在这里返回会诱导调用方把它当「当前视图的统计」）。
 */
export async function updateSavedNotePublishStatus(
  noteId: number,
  publishStatus: "pending" | "published"
): Promise<SavedNote> {
  const response = await http.patch<SavedNote>(`/notes/${noteId}/publish-status`, {
    publish_status: publishStatus,
  });
  return response.data;
}

/** 单条同步（内容库「同步」按钮）的结果载荷（设计 13 §25.4）。 */
/**
 * 单条同步的**入队回执**（`POST /notes/{id}/sync`，2026-09-25 队列化后）。
 *
 * ⚠️ 2026-09-25 用户口径「单独点击同步按钮也需要放入同步队列中执行，与批量同步
 * 一致」⇒ 该端点从**同步阻塞执行**改为**入队即返回**，`sync` 字段随之从「同步的
 * 逐项结果」（`{status, content_updated, images_added, …}`）变成回执。
 *
 * 终态（同步成功 / 失败 + 真实原因）由 `GET /notes/sync-queue` 的快照 `finished`
 * 给一次 —— 与批量同步**同一个出口**（`SyncQueueFinishedEntry`），不另设一套。
 */
export interface SavedNoteSyncEnqueueResult {
  ok: true;
  /** 恒为 `queued`（入队成功；终态不走这个字段）。 */
  status: "queued";
  /** 本地笔记主键（进度卡逐篇条目用的就是它）。 */
  note_pk: number;
  /** 平台笔记 ID（展示用）。 */
  note_id: string;
  /** 该篇**已在队列里**（幂等命中，不重复请求）。 */
  already_queued?: boolean;
}

/**
 * 内容库「同步」按钮：把这篇**加入同步队列**（设计 13 §25.4 / §35.10）。
 *
 * 与批量同步**同一个队列、同一个 worker**（顺序执行 = www 限流的第一道保障），
 * 与定时任务同步笔记逻辑**同源**（后端 worker 复用 `hydrate_note_detail` 唯一实现）。
 * ⚠️ 2026-09-23 用户口径「已发布的笔记也支持同步笔记最新的三方数据」⇒ 判据
 * **`synced` 或 `publish_status == "published"`**（与前端 `canSyncNote` 同口径）。
 * 两者都不满足的笔记后端给结构化 400 **`NOTE_NOT_SYNCABLE`**。
 *
 * `_silent`：门禁失败的 400 要由调用点报（不弹全局 toast 双份）—— 调用点按
 * `message.error(readError(e).message)` 渲染真实原因。
 */
export async function syncSavedNote(
  noteId: number
): Promise<{ note: SavedNote; sync: SavedNoteSyncEnqueueResult }> {
  const response = await http.post<{ note: SavedNote; sync: SavedNoteSyncEnqueueResult }>(
    `/notes/${noteId}/sync`,
    {},
    { _silent: true } as never
  );
  return response.data;
}

/**
 * 话题增删的**落库入口**（设计 13 §29.3 / §34.4）：整列替换，返回更新后的整行。
 *
 * 内容库详情卡「+ 新增话题 / Tag 上 × 删话题」都走它 —— 每次增删一次写库（实时落库），
 * 调用方传增删后的**完整列表**（话题**名**），后端 find-or-create 话题典例行 +
 * 对齐 `note_topics` 关系行（归一化：trim / 去空 / 去重 / 限量，唯一实现）。
 */
export async function updateSavedNoteTopics(
  noteId: number,
  topics: string[]
): Promise<SavedNote> {
  const response = await http.put<SavedNote>(`/notes/${noteId}/topics`, { topics });
  return response.data;
}

// --------------------------------------------------------------------------- #
// 商品笔记智能生成与发布（设计 13 §31，2026-09-23；§31.10 起走队列）
// --------------------------------------------------------------------------- #

/**
 * 单商品「生成笔记」的**入队回执**（`POST /xhs/product-notes/generate-save`，
 * 2026-09-25 §31.10 起异步）。
 *
 * 异步口径（用户口径「单独生成商品笔记和批量生成商品笔记均需走队列」）：端点只同步
 * 做门禁（未绑店 / 无 ark 令牌 / 台账无此商品 ⇒ 结构化 400/404，由全局拦截器报），
 * 立即返回 `ok:true + status:"queued"`；三步生成 + 轮询 + 落库在后台 worker 跑。
 * **终态不在这里** —— 前端轮询 `fetchGenerateQueue` 看快照 `finished`（与批量同一个
 * 出口）；生成失败的真实原因只在那里给一次（生成失败不落库，没有 `publish_error`
 * 那样的持久位）。
 */
export interface ProductNoteGenerateResult {
  ok: boolean;
  platform_account_id: number;
  /** 入队成功恒为 `"queued"`。 */
  status: string;
  /** 商品 ID（入队时原样回显）。 */
  item_id: string;
  /** 重复入队（已在队列 / 执行中）⇒ `true`（幂等，不重复生成）。 */
  already_queued?: boolean;
}

/**
 * 单商品「生成笔记」（商品列表页操作列按钮；设计 13 §31.4 + §31.10）。
 *
 * 调千帆后台智能生成（attributes → sellerpoint/aigc → 轮询），落库为
 * **待发布**的本地生成笔记（内容库可见，之后在内容库发布）。
 */
export async function generateProductNote(
  accountId: number,
  itemId: string
): Promise<ProductNoteGenerateResult> {
  const response = await http.post<ProductNoteGenerateResult>("/xhs/product-notes/generate-save", {
    platform_account_id: accountId,
    item_id: itemId,
  });
  return response.data;
}

/** 批量生成的逐项回执。 */
export interface ProductNoteBatchItem {
  item_id: string;
  /** 入队成功（`status:"queued"`）或门禁失败。 */
  ok: boolean;
  /** 入队成功恒为 `"queued"`。 */
  status?: string;
  /** 重复入队（已在队列 / 执行中）⇒ `true`（幂等，不重复生成）。 */
  already_queued?: boolean;
  /** 门禁失败原因（平台原文 / 数据问题）。 */
  error?: string;
  /** 门禁失败码（`QIANFAN_ITEM_NOT_FOUND` 等）。 */
  code?: string;
}

/** 批量生成汇总（`POST /xhs/product-notes/batch-generate`）。 */
export interface ProductNoteBatchResult {
  total: number;
  /** 入队（后台将生成）的项数。 */
  started: number;
  /** 门禁失败（未入队）的项数。 */
  failed: number;
  results: ProductNoteBatchItem[];
}

/**
 * 批量「生成笔记」（商品列表页工具栏；设计 13 §31.4 步 6 + §31.10）。
 *
 * 逐项过门禁、通过项顺序入队，单项失败不阻断（与千帆页面 `Promise.allSettled`
 * 同语义）；≤20 项/请求。返回逐项回执，调用方汇总「已入队 X / 失败 Y」。
 * **逐项终态**（生成成功 / 平台失败）走 `fetchGenerateQueue` 快照 `finished`。
 */
export async function batchGenerateProductNotes(
  accountId: number,
  itemIds: string[]
): Promise<ProductNoteBatchResult> {
  const response = await http.post<ProductNoteBatchResult>("/xhs/product-notes/batch-generate", {
    platform_account_id: accountId,
    item_ids: itemIds,
  });
  return response.data;
}

/** 生成进度卡的单项条目（`GET /xhs/product-notes/generate-queue`）。 */
export interface GenerateQueueEntry {
  /** 恒 0（生成前笔记不存在）；详情抽屉据此不拉笔记标题。 */
  note_pk: number;
  /** 商品 ID（业务主键）。 */
  item_id: string;
  /** 商品名（入队时台账快照；终态后优先显示生成的笔记标题）。 */
  title: string;
  /** 生成的本地笔记 ID（终态后才有；排队中为空串）。 */
  note_id: string;
}

/** 生成进度卡的逐项结果（快照 `finished`；生成失败的原因只在这里给一次）。 */
export interface GenerateQueueFinishedEntry extends GenerateQueueEntry {
  ok: boolean;
  /** 与另两个队列同口径的结果态（`ok` / `failed`）。 */
  status: string;
  /** 失败的真实原因（平台原文；成功时为空串）。 */
  reason: string;
  /** 机器可读原因码（`AI_GENERATION_FAILED` 等；成功为空串）。 */
  code: string;
}

/** 生成进度卡数据（`GET /xhs/product-notes/generate-queue`，设计 13 §31.10）。

 * 2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：快照只描述**队列
 * 实时态 + 近期终态** —— `running` / `queued` / `total`（= 两者 + 近期终态数）+
 * `finished`（近期终态项，含 ok 与 reason）。**没有 `generated` / `failed` 计数**
 * ——「本轮成败」由页面按自己入队的项累计（后端不知道用户这一轮点了什么）。
 */
export interface GenerateQueueSnapshot {
  /** 正在生成的那条（null = 空闲）。 */
  running: GenerateQueueEntry | null;
  /** 排队中的（FIFO 序）。 */
  queued: GenerateQueueEntry[];
  /** 队列规模 = running + queued + 近期终态数（进度卡停止条件「>0 且待处理为 0」用）。 */
  total: number;
  /** 近期已出结果的逐项（含 ok 与 reason）。 */
  finished: GenerateQueueFinishedEntry[];
}

/** 生成进度快照的**空闲初值**（页面首屏 / 轮询停止后用）。 */
export const EMPTY_GENERATE_QUEUE_SNAPSHOT: GenerateQueueSnapshot = {
  running: null,
  queued: [],
  total: 0,
  finished: [],
};

/**
 * 生成进度快照（商品列表页「生成进度卡」轮询用；设计 13 §31.10）。
 *
 * 前端在「有生成中 / 排队」时每 5 秒拉一次，待处理为 0 即停（与另两张进度卡同一套
 * 骨架）。⚠️ 2026-09-26 起快照不带成败计数，页面自己按入队的项累计。
 */
export async function fetchGenerateQueue(silent = true): Promise<GenerateQueueSnapshot> {
  const response = await http.get<GenerateQueueSnapshot>("/xhs/product-notes/generate-queue", {
    _silent: silent,
  } as never);
  return response.data;
}

/**
 * 单篇发布**启动**结果（`POST /notes/{id}/publish`，2026-09-25 起异步）。
 *
 * 异步口径（用户「前端点击发布无需等待，后端异步发布」）：端点只同步做门禁，
 * 立即返回 `ok:true + status:"publishing"`；下载 / 上传 / 预审核 / 发布在后台
 * 线程跑。**终态不在这里** —— 前端轮询 `fetchSavedNote` 看 `publish_status`
 * （`published` / `failed`，失败原因在 `publish_error`），见 `note-publish-gates.ts`。
 */
export interface NotePublishResult {
  ok: boolean;
  /** 本地笔记主键（就地更新卡片用；与平台 ID 无关）。 */
  note_pk: number;
  /** 笔记 ID（异步启动时还是本地 `local_` ID；平台 ID 要等终态）。 */
  note_id: string;
  /** 恒为 `"publishing"`（启动成功的语义确认）。 */
  status?: string;
  /** 平台真实笔记 ID（异步启动时**不带**；终态轮询才有）。 */
  platform_note_id?: string;
  published_at?: string;
  /** 失败原因（平台原文 / 网络异常；启动成功为空）。 */
  error?: string;
  /** 机器可读失败码（`PUBLISH_AUDIT_REJECTED` 等；异步启动时不带）。 */
  code?: string;
}

/**
 * 单篇「发布」（内容库卡片按钮，前端先二次确认；设计 13 §31.5）。
 *
 * **异步启动**：毫秒级返回，不等发布链路。门禁失败（不含 / 已发布 / 发布中 /
 * 无媒体 / 无 ark 令牌）仍是 400 结构化错误，由调用方摆原因。
 */
export async function publishSavedNote(
  noteId: number
): Promise<{ note: SavedNote; publish: NotePublishResult }> {
  const response = await http.post<{ note: SavedNote; publish: NotePublishResult }>(
    `/notes/${noteId}/publish`,
    {},
    { _silent: true } as never
  );
  return response.data;
}

/** 批量发布**单篇**结果（`POST /notes/batch-publish`，2026-09-25 起异步）。 */
export interface NoteBatchPublishItem {
  note_pk: number;
  /** 入队成功（`status:"publishing"`）或门禁失败。 */
  ok: boolean;
  /** 笔记 ID（入队时是本地 `local_` ID）。 */
  note_id?: string;
  /** 入队成功恒为 `"publishing"`。 */
  status?: string;
  /** 门禁失败原因（平台原文 / 数据问题）。 */
  error?: string;
  /** 门禁失败码（`NOTE_ALREADY_PUBLISHED` 等）。 */
  code?: string;
}

/** 批量发布汇总（`POST /notes/batch-publish`）。 */
export interface NoteBatchPublishResult {
  total: number;
  /** 入队（后台将发布）的篇数。 */
  started: number;
  /** 门禁失败（未入队）的篇数。 */
  failed: number;
  results: NoteBatchPublishItem[];
}

/**
 * 批量「发布」（内容库批量操作区；设计 13 §31.5 步 8，2026-09-25 起异步）。
 *
 * 逐篇过门禁，通过篇入队由后台一个线程顺序跑完。**立即返回**：`results` 里
 * 入队篇是 `{ok:true, status:"publishing"}`，门禁失败篇带 `error`/`code`。
 * 终态由前端对入队篇轮询 `fetchSavedNote` 获取。
 */
export async function batchPublishSavedNotes(noteIds: number[]): Promise<NoteBatchPublishResult> {
  const response = await http.post<NoteBatchPublishResult>("/notes/batch-publish", {
    note_ids: noteIds,
  });
  return response.data;
}

/** 发布进度卡的单篇条目（`GET /notes/publish-queue`）。 */
export interface PublishQueueEntry {
  note_pk: number;
  title: string;
  /** 本地 `local_` ID（发布中还没换平台 ID）。 */
  note_id: string;
}

/**
 * 进度卡的**当日三数**（`GET /notes/publish-queue` / `GET /notes/sync-queue`
 * 的 `today` 键；2026-09-26 用户口径「只统计当日发布或同步的数据，在卡片右下角
 * 小字展示仅当日数据，统计队列记录表中的数据」）。
 *
 * 口径落在**队列记录表**（`tasks`）上，由后端现算（`today_item_counts`）：
 * 成功 / 失败按「**今天出结果的**」（`finished_at` 落在当日）收口，待处理是队列
 * **实时**未终态数（跨天滞留的续跑项照样算进来）。
 * ⚠️ 因此不再由页面按自己入队的篇累计 —— 那样刷新页面数字就归零，也看不见
 * 定时 / 恢复入口入队的项。
 */
export interface QueueTodayCounts {
  /** 统计口径的日期（`YYYY-MM-DD`，上海时区）——「仅当日数据」说的是哪一天。 */
  date: string;
  /** 当日成功数（`done`）。 */
  ok: number;
  /** 当日失败数（`failed` + `exhausted`）。 */
  failed: number;
  /** 待处理数（未终态：`pending` + `running`）。 */
  pending: number;
}

/** 进度卡当日三数的**空闲初值**（首屏 / 快照未到手时）。 */
export const EMPTY_QUEUE_TODAY_COUNTS: QueueTodayCounts = {
  date: "",
  ok: 0,
  failed: 0,
  pending: 0,
};

/** 发布进度卡数据（`GET /notes/publish-queue`，2026-09-25 异步 + 队列）。

 * 2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：`running` /
 * `queued` / `total`（= 两者 + 近期终态数）+ `counts`（四态原始计数）。
 *
 * ⚠️ 2026-09-26 再改口径（用户口径「只统计当日发布的数据……统计队列记录表中的
 * 数据」）：卡面三数改读 `today`（当日 `tasks` 行现算），**不再由页面按自己入队的
 * 篇累计**（原「本轮成败由页面累计」对卡面数字已不成立）。
 */
export interface PublishQueueSnapshot {
  /** 正在发布的那篇（null = 空闲）。 */
  running: PublishQueueEntry | null;
  /** 排队中的（FIFO 序）。 */
  queued: PublishQueueEntry[];
  /** 队列规模 = running + queued + 近期终态数（进度卡停止条件「>0 且待处理为 0」用）。 */
  total: number;
  /** 四态原始计数（publishing 不归并待发布，与筛选档统计是两件事）。 */
  counts: {
    pending: number;
    publishing: number;
    published: number;
    failed: number;
  };
  /** 当日三数（成功 / 失败 / 待处理）—— 卡面数字的来源（2026-09-26 口径）。 */
  today: QueueTodayCounts;
}

/**
 * 发布进度快照（内容库「发布进度卡」轮询用；设计 13 §31.5）。
 *
 * 前端在「有发布中 / 排队」时每几秒拉一次，队列空则停。
 */
export async function fetchPublishQueue(
  silent = true
): Promise<PublishQueueSnapshot> {
  const response = await http.get<PublishQueueSnapshot>("/notes/publish-queue", {
    _silent: silent,
  } as never);
  return response.data;
}

// --------------------------------------------------------------------------- #
// 批量同步 + 同步进度卡（设计 13 §35，2026-09-25）
// --------------------------------------------------------------------------- #

/** 批量同步**单篇**结果（`POST /notes/batch-sync`）。 */
export interface NoteBatchSyncItem {
  note_pk: number;
  /** 入队成功（`status:"queued"`）或门禁失败。 */
  ok: boolean;
  /** 笔记 ID（入队时原样回显）。 */
  note_id?: string;
  /** 入队成功恒为 `"queued"`。 */
  status?: string;
  /** 重复入队（已在队列 / 执行中）⇒ `true`（幂等，不重复请求）。 */
  already_queued?: boolean;
  /** 门禁失败原因（平台原文 / 数据问题）。 */
  error?: string;
  /** 门禁失败码（`NOTE_NOT_SYNCABLE` / `NOTE_NOT_FOUND`）。 */
  code?: string;
}

/** 批量同步汇总（`POST /notes/batch-sync`）。 */
export interface NoteBatchSyncResult {
  total: number;
  /** 入队（后台将同步）的篇数。 */
  started: number;
  /** 门禁失败（未入队）的篇数。 */
  failed: number;
  results: NoteBatchSyncItem[];
}

/**
 * 批量「同步」（内容库批量操作区；设计 13 §35.2）。
 *
 * 门禁比单条「同步」按钮严：只收**已发布**的选中篇（与后端
 * `note_supports_batch_sync` 同口径，不达标篇带 `error`/`code` 进 `results`）。
 * 逐篇入队由后台**一个**线程顺序跑完（顺序执行 = www 限流的第二道保障）。
 * **立即返回**；终态由前端轮询 `fetchSyncQueue`（快照 `finished` 带逐篇结果
 * 与原因 —— 同步失败不落库到笔记行，没有 `publish_error` 那样的持久位）。
 */
export async function batchSyncSavedNotes(noteIds: number[]): Promise<NoteBatchSyncResult> {
  const response = await http.post<NoteBatchSyncResult>("/notes/batch-sync", {
    note_ids: noteIds,
  });
  return response.data;
}

/** 同步进度卡的单篇条目（`GET /notes/sync-queue`）。 */
export interface SyncQueueEntry {
  note_pk: number;
  title: string;
  note_id: string;
}

/** 同步进度卡的逐篇结果（快照 `finished`；同步失败的原因只在这里给一次）。 */
export interface SyncQueueFinishedEntry extends SyncQueueEntry {
  ok: boolean;
  /** 与 `hydrate_note_detail` 同源的状态（`ok` / `no_token` / `no_url` / `no_session` / `failed`）。 */
  status: string;
  /** 失败/跳过的真实原因（成功时为空串）。 */
  reason: string;
  /** 机器可读原因码（`WEB_SESSION_REQUIRED` 等；成功为空串）。 */
  code: string;
}

/** 同步进度卡数据（`GET /notes/sync-queue`，2026-09-25 批量同步）。

 * 2026-09-26 常驻化（用户口径「不需要队列批次，队列是常驻」）：`running` /
 * `queued` / `total`（= 两者 + 近期终态数）+ `finished`（近期终态项，含 ok 与
 * reason —— 同步失败不落库到笔记行，原因只在这里给一次）。**没有 `synced` /
 * `failed` 计数**。
 *
 * ⚠️ 2026-09-26 再改口径（用户口径「只统计当日同步的数据……统计队列记录表中的
 * 数据」）：卡面三数改读 `today`（当日 `tasks` 行现算）。
 */
export interface SyncQueueSnapshot {
  /** 正在同步的那条（null = 空闲）。 */
  running: SyncQueueEntry | null;
  /** 排队中的（FIFO 序）。 */
  queued: SyncQueueEntry[];
  /** 队列规模 = running + queued + 近期终态数（进度卡停止条件「>0 且待处理为 0」用）。 */
  total: number;
  /** 近期已出结果的逐篇（含 ok 与 reason）。 */
  finished: SyncQueueFinishedEntry[];
  /** 当日三数（成功 / 失败 / 待处理）—— 卡面数字的来源（2026-09-26 口径）。 */
  today: QueueTodayCounts;
}

/** 同步进度快照的**空闲初值**（页面首屏 / 轮询停止后用）。 */
export const EMPTY_SYNC_QUEUE_SNAPSHOT: SyncQueueSnapshot = {
  running: null,
  queued: [],
  total: 0,
  finished: [],
  today: { ...EMPTY_QUEUE_TODAY_COUNTS },
};

/**
 * 同步进度快照（内容库「同步进度卡」轮询用；设计 13 §35.3）。
 *
 * 前端在「有同步中 / 排队」时每几秒拉一次，队列空则停（与发布进度卡同一套骨架）。
 */
export async function fetchSyncQueue(silent = true): Promise<SyncQueueSnapshot> {
  const response = await http.get<SyncQueueSnapshot>("/notes/sync-queue", {
    _silent: silent,
  } as never);
  return response.data;
}

/*
 * ⚠️ **详情补齐队列的前端接口已删除**（2026-09-27 用户口径：账号卡移除「补齐详情」，
 * 补齐能力并入「同步笔记」任务本身，设计 13 §35.18）。同时删掉的：
 * `fetchHydrateQueue` / `HydrateQueueSnapshot` / `EMPTY_HYDRATE_QUEUE_SNAPSHOT` /
 * `hydratePendingNotes` / `HydratePendingResult` —— 后端端点
 * `GET /notes/hydrate-queue` 与 `POST /accounts/{id}/notes/hydrate-pending` 一并移除。
 */

/**
 * 笔记详情（内容库）。**只读本地库，零平台请求**（2026-09-22 用户口径：
 * 「内容库应查询的均是本地数据」）。
 *
 * ⚠️ ~~`hydrate=true` 让后端当场打一次平台补齐正文~~ **已移除**：那条路径的
 * `db.commit()` 会与正在跑的同步任务抢 SQLite 写锁，`database is locked`
 * 未捕获 ⇒ 用户看到「内容库笔记详情 500」。一个只读页面不该有平台副作用。
 *
 * 正文与图集的补齐现在**只由同步链路**做（同步任务内联，含历史欠账，设计 13 §35.18）；
 * 本地暂时没有正文的行，界面按缺值口径说明，不假装有数据。
 */
export async function fetchSavedNote(
  noteId: number,
  silent = false
): Promise<SavedNote> {
  const response = await http.get<SavedNote>(`/notes/${noteId}`, {
    _silent: silent
  } as never);
  return response.data;
}

export async function fetchSavedNoteAssets(noteId: number): Promise<Paginated<NoteAsset>> {
  const response = await http.get<Paginated<NoteAsset>>(`/notes/${noteId}/assets`);
  return response.data;
}

export async function fetchSavedNoteComments(noteId: number, page = 1): Promise<Paginated<NoteComment>> {
  const response = await http.get<Paginated<NoteComment>>(`/notes/${noteId}/comments`, {
    params: { page, page_size: 50 }
  });
  return response.data;
}

export async function deleteSavedNote(noteId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/notes/${noteId}`);
  return response.data;
}

/**
 * 话题字典（设计 13 §34.4）。
 *
 * `keyword` 非空 ⇒ 后端 `name LIKE '%关键词%'` 模糊搜索（内容库筛选栏话题
 * 下拉的远程搜索用；调用方先过 `createTopicSearchDebouncer` 300ms 防抖，
 * 见 `lib/topic-search.ts`）。不传 = 全量（详情弹窗「选已有话题」仍走全量）。
 */
export async function fetchTopics(keyword?: string): Promise<Paginated<NoteTopic>> {
  const response = await http.get<Paginated<NoteTopic>>("/topics", {
    params: keyword && keyword.trim() ? { keyword: keyword.trim() } : undefined,
  });
  return response.data;
}

export async function batchCreateDraftsFromNotes(
  payload: BatchCreateDraftsPayload
): Promise<BatchCreateDraftsResponse> {
  const response = await http.post<BatchCreateDraftsResponse>("/notes/batch-create-drafts", payload);
  return response.data;
}

export async function exportSavedNotes(payload: NotesExportPayload): Promise<NotesExportResponse> {
  const response = await http.post<NotesExportResponse>("/notes/export", payload);
  return response.data;
}

export async function downloadExportFile(downloadUrl: string, fileName: string): Promise<void> {
  const endpoint = downloadUrl.startsWith("/api") ? downloadUrl.slice(4) : downloadUrl;
  const response = await http.get<Blob>(endpoint, { responseType: "blob" });
  const objectUrl = window.URL.createObjectURL(response.data);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(objectUrl);
}

export type UploadedFile = {
  file_name: string;
  file_path: string;
  download_url: string;
  asset_type: "image" | "video";
  size: number;
};

export async function uploadAssetFile(file: File): Promise<UploadedFile> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await http.post<UploadedFile>("/files/upload", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 120000,
  });
  return response.data;
}

export async function searchXhsNotes(payload: XhsSearchOptions): Promise<XhsNoteSearchResponse> {
  const response = await http.post<XhsNoteSearchResponse>("/xhs/pc/search/notes", payload);
  return response.data;
}

export async function crawlXhsDataStream(
  payload: XhsDataCrawlPayload,
  onItem: (index: number, item: XhsDataCrawlItem) => void,
  onProgress?: (message: string) => void,
  onError?: (message: string) => void,
): Promise<{ total: number; success_count: number; failed_count: number }> {
  const token = getAccessToken();
  const response = await fetch("/api/xhs/crawl/data", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail || `HTTP ${response.status}`);
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response stream");
  const decoder = new TextDecoder();
  let buffer = "";
  let result = { total: 0, success_count: 0, failed_count: 0 };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      try {
        const event = JSON.parse(line.slice(6));
        if (event.type === "item") onItem(event.index, event.item);
        else if (event.type === "progress") onProgress?.(event.message);
        else if (event.type === "error") onError?.(event.message);
        else if (event.type === "done") result = { total: event.total, success_count: event.success_count, failed_count: event.failed_count };
      } catch { /* skip malformed events */ }
    }
  }
  return result;
}

export async function fetchXhsNoteDetail(payload: { account_id: number; url: string }): Promise<XhsSearchNote> {
  const response = await http.post<XhsSearchNote>("/xhs/pc/notes/detail", payload);
  return response.data;
}

export async function fetchXhsNoteComments(payload: {
  account_id: number;
  note_url: string;
}): Promise<Paginated<NoteComment>> {
  const response = await http.post<{ total: number; items: NoteComment[] }>("/xhs/pc/notes/comments", payload);
  return {
    total: response.data.total,
    page: 1,
    page_size: response.data.items.length,
    items: response.data.items
  };
}

export async function saveXhsNotesToLibrary(payload: {
  account_id: number;
  notes: XhsSearchNote[];
}): Promise<SaveNotesResponse> {
  const response = await http.post<SaveNotesResponse>("/notes/batch-save", payload);
  return response.data;
}

// ---------------------------------------------------------------------------
// Self content: my notes / liked / collected / interaction messages.
// Design source: docs/02_设计文档/12_功能设计_我的笔记与互动数据.md
// Read-only and never persisted — this is a live view of the platform.
// ---------------------------------------------------------------------------

export async function fetchXhsSelfIdentity(accountId: number): Promise<XhsSelfIdentity> {
  const response = await http.post<XhsSelfIdentity>("/xhs/pc/users/me", { account_id: accountId });
  return response.data;
}

/**
 * Which self-content links this account can actually use.
 *
 * Purely local on the server (no platform request), so it is safe to call on
 * every account switch and it still answers when identity resolution fails.
 * This — not `sub_type` — decides which tabs are enabled (design doc §17.4).
 */
/**
 * Which self-content link an account can use — purely local on the backend.
 *
 * Marked `_silent`: the caller treats this probe as an optimisation and keeps
 * every tab live when it fails, so the global interceptor must not turn that
 * into a visible `message.error("Not Found")` toast. Without the flag the local
 * `catch` was defeated by the interceptor, which showed the raw HTTP status
 * text for a failure the page had already decided to tolerate.
 */
export async function fetchXhsSelfCapabilities(accountId: number): Promise<XhsSelfCapabilities> {
  const response = await http.post<XhsSelfCapabilities>(
    "/xhs/pc/users/capabilities",
    { account_id: accountId },
    { _silent: true } as never
  );
  return response.data;
}

export type XhsSelfContentPayload = {
  account_id: number;
  /** Opaque platform cursor; omit (or pass "") for the first page. */
  cursor?: string;
  /** Pasted profile URL — the primary source of xsec_token (design doc §7). */
  profile_url?: string;
  xsec_token?: string;
  xsec_source?: string;
};

/**
 * 自有内容列表的三个入口（我发布的 / 我赞过的 / 我收藏的）。
 *
 * 一律 `_silent`：**失败的展示权归调用点**（`my-notes-page` 三分法渲染）。
 * 页签不置灰之后，「取不到」不再是异常而是**一种正常结果** —— 若拦不住
 * 全局 toast，页面上那行安静说明旁边还会再弹一句「请求失败，请稍后重试」，
 * 等于把置灰换成了报错（v1.28 / DEF-058）。
 */
export async function fetchXhsSelfNotes(payload: XhsSelfContentPayload): Promise<XhsSelfContentResponse> {
  const response = await http.post<XhsSelfContentResponse>("/xhs/pc/users/notes", payload, {
    _silent: true,
  } as never);
  return response.data;
}

export async function fetchXhsSelfLikedNotes(payload: XhsSelfContentPayload): Promise<XhsSelfContentResponse> {
  const response = await http.post<XhsSelfContentResponse>("/xhs/pc/users/liked", payload, {
    _silent: true,
  } as never);
  return response.data;
}

export async function fetchXhsSelfCollectedNotes(payload: XhsSelfContentPayload): Promise<XhsSelfContentResponse> {
  const response = await http.post<XhsSelfContentResponse>("/xhs/pc/users/collected", payload, {
    _silent: true,
  } as never);
  return response.data;
}

export async function fetchXhsInteractionMessages(payload: {
  account_id: number;
  kind: XhsInteractionKind;
  cursor?: string;
}): Promise<XhsInteractionResponse> {
  const response = await http.post<XhsInteractionResponse>("/xhs/pc/users/interactions", payload, {
    _silent: true,
  } as never);
  return response.data;
}

// ---------------------------------------------------------------------------
// 千帆（ark）商家后台：笔记挂载商品的唯一真实来源
//
// 设计源：docs/02_设计文档/12_功能设计_我的笔记与互动数据.md §19
//
// 为什么单独一组函数：商品的来源与「我的笔记」的读链路**不是同一个后台**。
// 绑定 = 存一份千帆登录态；同步 = 把千帆商品笔记列表落到本地映射表。
// 浏览笔记本身不产生千帆请求。
// ---------------------------------------------------------------------------

/** 当前绑定状态（服务端纯本地查询，不打千帆）。 */
export async function fetchXhsQianfanBinding(accountId: number): Promise<XhsQianfanBinding> {
  const response = await http.post<XhsQianfanBinding>("/xhs/qianfan/binding", {
    platform_account_id: accountId,
  });
  return response.data;
}

/**
 * 绑定千帆店铺。
 *
 * `cookieString` 是**千帆自己的** Cookie（浏览器登录 ark.xiaohongshu.com 后复制），
 * 它必须含 `access-token-ark.xiaohongshu.com` —— 创作者 Cookie 在千帆上会被
 * 判成「缺失登录凭证」（实测 code=-13000）。
 */
export async function bindXhsQianfanShop(
  accountId: number,
  cookieString: string
): Promise<XhsQianfanBinding> {
  const response = await http.post<XhsQianfanBinding>("/xhs/qianfan/bind", {
    platform_account_id: accountId,
    cookie_string: cookieString,
  });
  return response.data;
}

/**
 * 触发**店铺商品本体**同步（M1.9 / §19，落 `shop_products` 表）。
 *
 * ⚠️ **v1.32 起它就是商品列表的唯一数据来源**（设计 13 §30.3）：`/items` 改读
 * 本地台账，本任务负责「翻页落库 + SKU 规格详情内联补齐 + 字段补足回填」。
 * （原 `syncXhsQianfanGoods` → `/sync-goods` 的「笔记↔商品映射」入口已移除，
 * 映射改由笔记同步 ark 源 + 我的笔记页按需补齐维护。）
 *
 * 账号未绑定千帆 → 400 `QIANFAN_BINDING_REQUIRED`（由卡片原因行展示，不弹全局提示）。
 * 完成语义：只有翻完才算完整，半途而废**一行旧数据都不删**。
 *
 * 2026-09-23 重构：每页列表商品的 SKU 规格详情在落库前拉取（批量 10），
 * 列表数据 + 规格详情一次性 upsert——列表翻完 = 详情也全了，不再有多轮补齐队列。
 *
 * **语义（§35.17）**：手动触发定时任务 `system.shop_goods_sync` 的「执行一次」——
 * 任务行给账号卡轮询，`scheduled_job_logs` 给任务中心「任务日志」；已有任务在跑时
 * `reused: true` 且 `log_id` 为 `null`。
 */
export async function syncShopGoods(payload: {
  platformAccountId: number;
  maxPages?: number;
  /** §23：同步范围（弹窗选择）。不传 = 后端默认 full。 */
  scope?: SyncScope;
  startDate?: string;
  endDate?: string;
  /**
   * 商品状态范围（设计 13 §30.3.2，2026-10-02）。
   *
   * 不传 = 后端默认 `all`（**同步全部状态**：售卖中 / 已下架 / 已冻结 / 整改 /
   * 审核中 / 审核驳回 / 审核通过）。传某个状态键则只同步那一种。
   */
  itemStatus?: XhsShopItemStatus;
}): Promise<XhsSyncTriggerResult> {
  const response = await http.post<XhsSyncTriggerResult>(
    "/xhs/qianfan/sync-shop-goods",
    {
      platform_account_id: payload.platformAccountId,
      // ⚠️ **不传 `max_pages` = 全量**（设计 13 §21）。旧的 `?? 3` 会让商品
      // 永远只同步前 150 件；不要再写回默认值。
      ...(payload.maxPages ? { max_pages: payload.maxPages } : {}),
      ...(payload.scope ? { scope: payload.scope } : {}),
      ...(payload.startDate ? { start_date: payload.startDate } : {}),
      ...(payload.endDate ? { end_date: payload.endDate } : {}),
      ...(payload.itemStatus ? { item_status: payload.itemStatus } : {}),
    },
    { _silent: true } as never
  );
  return response.data;
}

/**
 * ⚠️ **独立对账入口已移除**（2026-09-22，设计 13 §27，用户口径「数据对账功能融合到
 * 同步笔记和同步商品中」）：原 `reconcileAccountNotes` / `reconcileShopGoods` 与
 * `ReconcileReport`（§24 的报告/修正两段式）随端点一起拆除 —— 对账现在由每次同步
 * 在**本次同步范围内**自动完成（标记「三方不存在」+ 自愈），结果写在同步任务行的
 * `note` 里（账号卡片结果行可见）。此处仅留指针，不保留旧函数。
 */

/** 店铺商品同步的进度查询。 */
export async function syncShopGoodsStatus(platformAccountId: number): Promise<XhsSyncTaskStatus> {
  const response = await http.get<XhsSyncTaskStatus>("/xhs/qianfan/sync-shop-goods/status", {
    params: { platform_account_id: platformAccountId },
    _silent: true,
  } as never);
  return response.data;
}

// --------------------------------------------------------------------------- #
// 「我的笔记」离线数据（2026-10-06 需求）：商业/普通 Tab 查本地库 + 导入 xlsx。
// --------------------------------------------------------------------------- #

export interface OfflineNotesResult {
  total: number;
  page: number;
  page_size: number;
  items: unknown[];
}

export async function fetchOfflineNotes(
  category: string,
  platformAccountId?: number,
  page = 1,
  pageSize = 20,
  keyword?: string,
  itemId?: string,
  sortBy?: string,
  sortOrder?: "asc" | "desc",
  filters?: {
    publishedFrom?: number;
    publishedTo?: number;
    deleted?: "all" | "deleted" | "active";
    noteId?: string;
    readFrom?: number;
    readTo?: number;
    likeFrom?: number;
    likeTo?: number;
    collectFrom?: number;
    collectTo?: number;
    commentFrom?: number;
    commentTo?: number;
    shareFrom?: number;
    shareTo?: number;
    payGmvFrom?: number;
    payGmvTo?: number;
    payOrderFrom?: number;
    payOrderTo?: number;
  }
): Promise<OfflineNotesResult> {
  const response = await http.get<OfflineNotesResult>("/notes/offline", {
    params: {
      ...(category ? { category } : {}),
      ...(platformAccountId != null ? { platform_account_id: platformAccountId } : {}),
      ...(keyword && keyword.trim() ? { keyword: keyword.trim() } : {}),
      ...(itemId && itemId.trim() ? { item_id: itemId.trim() } : {}),
      ...(sortBy ? { sort_by: sortBy, sort_order: sortOrder || "desc" } : {}),
      ...(filters?.publishedFrom != null ? { published_from: filters.publishedFrom } : {}),
      ...(filters?.publishedTo != null ? { published_to: filters.publishedTo } : {}),
      ...(filters?.deleted ? { deleted: filters.deleted } : {}),
      ...(filters?.noteId && filters.noteId.trim() ? { note_id: filters.noteId.trim() } : {}),
      ...(filters?.readFrom != null ? { read_from: filters.readFrom } : {}),
      ...(filters?.readTo != null ? { read_to: filters.readTo } : {}),
      ...(filters?.likeFrom != null ? { like_from: filters.likeFrom } : {}),
      ...(filters?.likeTo != null ? { like_to: filters.likeTo } : {}),
      ...(filters?.collectFrom != null ? { collect_from: filters.collectFrom } : {}),
      ...(filters?.collectTo != null ? { collect_to: filters.collectTo } : {}),
      ...(filters?.commentFrom != null ? { comment_from: filters.commentFrom } : {}),
      ...(filters?.commentTo != null ? { comment_to: filters.commentTo } : {}),
      ...(filters?.shareFrom != null ? { share_from: filters.shareFrom } : {}),
      ...(filters?.shareTo != null ? { share_to: filters.shareTo } : {}),
      ...(filters?.payGmvFrom != null ? { pay_gmv_from: filters.payGmvFrom } : {}),
      ...(filters?.payGmvTo != null ? { pay_gmv_to: filters.payGmvTo } : {}),
      ...(filters?.payOrderFrom != null ? { pay_order_from: filters.payOrderFrom } : {}),
      ...(filters?.payOrderTo != null ? { pay_order_to: filters.payOrderTo } : {}),
      page,
      page_size: pageSize,
    },
  });
  return response.data;
}

/** 按商品 ID 查本地台账商品详情（数据中心「商品名称」点击弹窗用，2026-10-12）。 */
export type NoteGoodsDetail = {
  item_id: string;
  item_title: string;
  price: string;
  images: string[];
  stock: number | null;
  sales: number | null;
  variant: string;
};
export async function fetchNoteGoodsDetail(itemId: string): Promise<NoteGoodsDetail> {
  const response = await http.get<NoteGoodsDetail>(`/notes/goods/${encodeURIComponent(itemId)}`);
  return response.data;
}

/** 商业/普通 Tab 复选框批量删除（2026-10-06）：delete_platform=false 只删本地，true 删本地+平台。 */
export async function batchDeleteNotes(
  noteIds: string[],
  deletePlatform: boolean
): Promise<{ deleted: number; failed: { note_id: string; reason: string }[]; results: { note_id: string; ok: boolean; reason?: string }[] }> {
  const response = await http.post<{
    deleted: number;
    failed: { note_id: string; reason: string }[];
    results: { note_id: string; ok: boolean; reason?: string }[];
  }>("/notes/batch-delete", { note_ids: noteIds, delete_platform: deletePlatform }, { _silent: true } as never);
  return response.data;
}

/** 商业/普通 Tab 手动同步（2026-10-06）：按日期范围（≤90天）从千帆数据中心拉离线笔记数据。 */
export async function syncNoteData(
  accountId: number,
  category: string,
  startDate?: string,
  endDate?: string
): Promise<{ ok: boolean; status: string; category: string; log_id: number | null }> {
  const response = await http.post<{ ok: boolean; status: string; category: string; log_id: number | null }>(
    "/notes/sync-data",
    {
      account_id: accountId,
      category,
      ...(startDate ? { start_date: startDate } : {}),
      ...(endDate ? { end_date: endDate } : {}),
    },
    { _silent: true } as never
  );
  return response.data;
}

export async function importNoteData(
  file: File,
  category: string,
  accountId: number
): Promise<{ ok: boolean; status: string; category: string }> {
  const form = new FormData();
  form.append("file", file);
  form.append("category", category);
  form.append("account_id", String(accountId));
  const response = await http.post<{ ok: boolean; status: string; category: string }>(
    "/notes/import-data",
    form,
    { headers: { "Content-Type": "multipart/form-data" }, _silent: true } as never
  );
  return response.data;
}

/** 点击离线笔记卡片时实时拉详情并落库（正文/媒体/话题拆分，2026-10-06 需求）。 */
export async function hydrateOfflineNoteDetail(noteId: string): Promise<{
  note_id: string;
  title: string;
  content: string;
  author_name: string;
  account_name: string;
  cover_url: string;
  asset_urls: string[];
  video_url: string;
  topics: { id: number; name: string; color: string }[];
  raw_json: Record<string, unknown>;
  /** 详情补齐结果：ok / no_url（缺 token）/ failed（拉取失败）/ already_hydrated。失败时正文/媒体留空，前端据此刻安静提示。 */
  hydrate_status: string;
  /** 挂载商品（后端 `_serialize_note.goods` 形状：`{item_id, item_title, price, images, stock, sales, variant}`）。 */
  goods: {
    item_id: string;
    item_title: string;
    price: string;
    images: string[];
    stock: number | null;
    sales: number | null;
    variant: string;
  }[];
}> {
  const response = await http.post<{
    note_id: string;
    title: string;
    content: string;
    author_name: string;
    account_name: string;
    cover_url: string;
    asset_urls: string[];
    video_url: string;
    topics: { id: number; name: string; color: string }[];
    raw_json: Record<string, unknown>;
    hydrate_status: string;
    goods: {
      item_id: string;
      item_title: string;
      price: string;
      images: string[];
      stock: number | null;
      sales: number | null;
      variant: string;
    }[];
  }>(`/notes/${noteId}/hydrate`);
  return response.data;
}

/** 无流量判定阈值：可选核心指标，null = 不参与判定（2026-10-06 方案 A）。 */
export interface NoTrafficThresholds {
  max_browse: number | null;
  max_pay_order: number | null;
  max_pay_gmv: number | null;
  /** 2026-10-06 方案 A：可选互动指标（null = 不参与判定）。 */
  max_like: number | null;
  max_collect: number | null;
  max_comment: number | null;
  max_share: number | null;
}

export interface NoTrafficCandidate {
  note_id: string;
  title: string;
  cover: string;
  browse_num: number | null;
  browse_num_30d: number | null;
  likes: number | null;
  collects: number | null;
  comments: number | null;
  shares: number | null;
  pay_gmv: number | null;
  pay_order_cnt: number | null;
  publish_time: number | null;
  synced_at: string;
}

export interface NoTrafficQuery {
  keyword: string;
  note_type: string;
  published_before: number | null;
  /** 2026-10-06 方案 A：笔记分类（"" 不限 / goods / normal）。 */
  category: string;
  /** 2026-10-12：指标区间（min~max，null = 该侧不筛；交集 = 清理范围）。 */
  read_min: number | null;
  read_max: number | null;
  like_min: number | null;
  like_max: number | null;
  collect_min: number | null;
  collect_max: number | null;
  comment_min: number | null;
  comment_max: number | null;
  share_min: number | null;
  share_max: number | null;
  pay_gmv_min: number | null;
  pay_gmv_max: number | null;
  pay_order_min: number | null;
  pay_order_max: number | null;
}

export interface NoTrafficCandidatesResult {
  candidates: NoTrafficCandidate[];
  total: number;
  /** 实际采用的页码（越界请求会回落成最后一页）。 */
  page: number;
  /** 总页数（按 `total` / `page_size` 算，后端给）。 */
  page_count: number;
  /** 实际采用的每页条数。 */
  page_size: number;
  thresholds: NoTrafficThresholds;
  query: NoTrafficQuery;
  coverage: { synced_notes: number; pool_total: number; reason: string };
}

export async function fetchNoTrafficCandidates(
  accountId: number,
  thresholds: NoTrafficThresholds,
  query: NoTrafficQuery,
  page = 1,
  pageSize = 50
): Promise<NoTrafficCandidatesResult> {
  // 分页（2026-10-05）：`page` 从 1 起；后端回显实际页码（越界回落末页）
  // 与 `page_count`，前端按回显值对齐分页器 —— 不自己算第二份。
  const response = await http.post<NoTrafficCandidatesResult>(
    "/xhs/pc/users/no-traffic/candidates",
    { account_id: accountId, thresholds, query, limit: pageSize, page },
    { _silent: true } as never
  );
  return response.data;
}

export async function startNoTrafficCleanup(
  accountId: number,
  thresholds: NoTrafficThresholds,
  query: NoTrafficQuery,
  batchLimit = 20,
  noteIds: string[] = []
): Promise<{ task_id: number; status: string }> {
  // `noteIds` 非空 = **清理指定笔记**（列表勾选）；空 = **清理全部候选**
  // （上限 `batch_limit`）。两条入口后端共用同一条执行链，差别只在候选集。
  const response = await http.post<{ task_id: number; status: string }>(
    "/xhs/pc/users/no-traffic/cleanup",
    {
      account_id: accountId,
      thresholds,
      query,
      batch_limit: batchLimit,
      ...(noteIds.length ? { note_ids: noteIds } : {}),
    },
    { _silent: true } as never
  );
  return response.data;
}

export interface NoTrafficCleanupStatus {
  task_id: number;
  status: string;
  progress: number;
  deleted: number;
  skipped: number;
  failed: number;
  total: number;
  note: string;
  details: { note_id: string; ok: boolean; reason?: string }[];
  /** 清理**前**候选池总条数（未开跑时为 null）。 */
  before_total?: number | null;
  /** 清理**后**候选池总条数（重算，不是 `before - deleted` 估算）。 */
  after_total?: number | null;
  /** 勾选了但已不在候选池的笔记（改过阈值 / 已被删）—— 不删、如实报。 */
  not_in_pool?: string[];
}

export async function fetchNoTrafficCleanupStatus(
  taskId: number
): Promise<NoTrafficCleanupStatus> {
  const response = await http.get<NoTrafficCleanupStatus>(
    `/xhs/pc/users/no-traffic/cleanup/${taskId}`,
    { _silent: true } as never
  );
  return response.data;
}

/** 该账号此刻是否有清理在跑（「正在清理无流量笔记」按钮态的数据源）。 */
export interface NoTrafficCleanupActive {
  running: boolean;
  task_id: number | null;
  log_id: number | null;
  status: string;
}

export async function fetchNoTrafficCleanupActive(
  accountId: number
): Promise<NoTrafficCleanupActive> {
  const response = await http.get<NoTrafficCleanupActive>(
    "/xhs/pc/users/no-traffic/cleanup/active",
    { params: { account_id: accountId } }
  );
  return response.data;
}

/** 清理无流量笔记的**操作日志**一行（独立表 `no_traffic_cleanup_logs`）。
 *
 * ⚠️ 2026-10-06 用户口径「清理无流量的主要对象是笔记，**所有主体应是笔记维度**」：
 * 批次行内联 `items[]`（笔记维度明细），列表以**父子两层**渲染 ——
 * 批次行答「这次整体怎么样」，展开行答「**这一篇**被删了没有、为什么」。
 */
export interface NoTrafficCleanupItemRow {
  id: number;
  log_id: number | null;
  task_id: number | null;
  account_id: number | null;
  account_name: string;
  note_id: string;
  note_title: string;
  note_cover: string;
  /** `deleted` / `skipped` / `failed` / `not_in_pool` / `not_attempted`。 */
  result: string;
  reason: string;
  published_at: number | null;
  operated_at: string | null;
  created_at: string | null;
}

export interface NoTrafficCleanupLogItem {
  id: number;
  account_id: number | null;
  account_name: string;
  task_id: number | null;
  trigger_type: string;
  /** 批次号（2026-10-13）：同一次清理拆多批时 `{基号}-{序号}`，单批 = 基号。 */
  batch_no: string;
  thresholds: Record<string, unknown>;
  query: Record<string, unknown>;
  note_ids: string[];
  batch_limit: number;
  before_total: number;
  after_total: number;
  total: number;
  deleted: number;
  skipped: number;
  failed: number;
  not_in_pool: number;
  status: string;
  /** **本次失败原因**（首条失败笔记的平台原文）。⚠️ 不是候选池覆盖度说明
   * —— 那句在 `coverage_note`（DEF-095）。 */
  reason: string;
  /** 候选池覆盖度说明（信息性备注，不显示在「失败原因」列）。 */
  coverage_note: string;
  /** 本批是否被**中止**（平台风控 / 凭据类失败 ⇒ 剩余笔记不再发请求）。 */
  aborted: boolean;
  abort_reason: string;
  /** 兼容字段：`{note_id, ok, reason}` 形态的旧明细快照。 */
  details: { note_id: string; ok: boolean; reason?: string }[];
  /** **笔记维度明细**（一篇一行，用户口径「主体应是笔记维度」）。 */
  items: NoTrafficCleanupItemRow[];
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number;
}

export interface NoTrafficCleanupLogPage {
  total: number;
  page: number;
  limit: number;
  page_count: number;
  items: NoTrafficCleanupLogItem[];
}

/** 查清理日志（用户口径「查询清理无流量笔记日志详情按钮，弹窗列表展示」）。 */
export async function fetchNoTrafficCleanupLogs(params: {
  accountId?: number;
  page?: number;
  limit?: number;
}): Promise<NoTrafficCleanupLogPage> {
  const response = await http.get<NoTrafficCleanupLogPage>(
    "/xhs/pc/users/no-traffic/cleanup-logs",
    {
      params: {
        ...(params.accountId ? { account_id: params.accountId } : {}),
        page: params.page ?? 1,
        limit: params.limit ?? 20,
      },
      _silent: true,
    } as never
  );
  return response.data;
}

/**
 * 当前用户有哪些千帆店铺、每个店铺被哪几个账号绑着（**纯本地**，不打千帆）。
 *
 * T-32 起账号↔店铺是 N:M，所以店铺列表是「选账号」之外的一层信息：
 * 商品列表页用它给账号选项标注所属店铺，并在同一店铺多账号时提示。
 */
/** 解除绑定（连本地商品映射一起清掉）；`shop_remaining_accounts` = 店铺还有几个账号绑着。 */
export async function unbindXhsQianfanShop(
  accountId: number
): Promise<{
  platform_account_id: number;
  bound: boolean;
  removed: number;
  shop_remaining_accounts: number;
}> {
  const response = await http.post<{
    platform_account_id: number;
    bound: boolean;
    removed: number;
    shop_remaining_accounts: number;
  }>("/xhs/qianfan/unbind", { platform_account_id: accountId });
  return response.data;
}

/**
 * 立即巡检千帆登录态（T-31）：一次**只读**探活 `GET /api/edith/seller/info/v2`。
 *
 * 与后台定时巡检走同一条服务函数，差别只是「不判到期」——点一次就得到确定答案。
 * 失效时会把绑定标 `invalid` + 写可读原因 + 发一条铃铛通知，但**不清空**已同步的映射。
 */
export async function checkXhsQianfanShopHealth(
  accountId: number
): Promise<XhsQianfanBinding & { checked_now: boolean; flipped_to_invalid: boolean }> {
  const response = await http.post<XhsQianfanBinding & { checked_now: boolean; flipped_to_invalid: boolean }>(
    "/xhs/qianfan/health-check",
    { platform_account_id: accountId },
    { _silent: true } as never
  );
  return response.data;
}

/**
 * 千帆商品列表（**v1.32 起走本地台账**，设计 13 §30.3）。
 *
 * 数据来源 = 「同步商品」任务落库的 `shop_products`（请求 / 响应契约与改造前一致）；
 * `queried_at` 是**本地同步时间**（不是查询时间）。`products_total` /
 * `products_synced_at` 同步任务照写，但**页面顶部已不再展示**（2026-09-23 用户口径，
 * 时效出口在账号矩阵页同步任务结果行）。零平台请求。
 *
 * 排序：`sort_field` / `order` 由**表头箭头**驱动（v1.32 起不在搜索区），
 * 服务端分页 ⇒ 必须回服务端排。金额入参是**元**、出参是**分**（展示层 /100）。
 * 筛选参数名对应千帆商品列表页自己发的请求，见设计文档 §25。
 */
export async function fetchXhsShopItems(query: XhsShopItemsQuery): Promise<XhsShopItemsResponse> {
  const response = await http.post<XhsShopItemsResponse>("/xhs/qianfan/items", query);
  return response.data;
}

/**
 * 图片空间列表（设计 22 §1.3）——**千帆云只读代理，本地零落库**。
 *
 * ⚠️ 与「素材库」（`fetchMaterialFiles`，本地磁盘）不是一回事：本函数拿的是
 * 千帆云上的图片资产（15G 配额）。列表端点平台**强制签名**（后端 `_signed_post`），
 * 但本调用本身是只读查询。
 */
export async function fetchPictureSpaceItems(
  query: PictureSpaceItemsQuery
): Promise<PictureSpaceItemsResponse> {
  const response = await http.post<PictureSpaceItemsResponse>(
    "/xhs/qianfan/picture-space/items",
    query
  );
  return response.data;
}

/** 图片空间配额（只读；取不到 ⇒ `used_space`/`total_space` 为 `null`，页面不渲染配额条）。 */
export async function fetchPictureSpaceUsage(accountId: number): Promise<PictureSpaceUsage> {
  const response = await http.get<PictureSpaceUsage>("/xhs/qianfan/picture-space/usage", {
    params: { platform_account_id: accountId },
  });
  return response.data;
}

/** 图片空间**扩容条件**（只读；弹窗打开时调，2026-10-07）。 */
export async function fetchPictureSpaceResizeConditions(
  accountId: number
): Promise<PictureSpaceResizeCheck> {
  const response = await http.get<PictureSpaceResizeCheck>(
    "/xhs/qianfan/picture-space/resize/check",
    { params: { platform_account_id: accountId } }
  );
  return response.data;
}

/** **申请扩容**（写；成功后重取配额）。 */
export async function resizePictureSpace(
  accountId: number
): Promise<PictureSpaceWriteResult> {
  const response = await http.post<PictureSpaceWriteResult>("/xhs/qianfan/picture-space/resize", {
    platform_account_id: accountId,
  });
  return response.data;
}

/** 图片空间目录树（只读；本页只用根节点）。 */
export async function fetchPictureSpaceTree(accountId: number): Promise<PictureSpaceTreeNode> {
  const response = await http.get<PictureSpaceTreeNode>("/xhs/qianfan/picture-space/tree", {
    params: { platform_account_id: accountId },
  });
  return response.data;
}

/**
 * 图片空间写操作（设计 22 §1.5）—— **用户显式操作**，非后台自动写。
 *
 * ⚠️ 全部需要 ark 签名（平台强制）。`items` 传列表里已有的归一化条目
 * （后端反推成平台形状，前端不必回传原始 JSON）。
 */

/** 新建文件夹（文件夹名 ≤20 字）。 */
export async function createPictureSpaceFolder(
  accountId: number,
  name: string
): Promise<PictureSpaceWriteResult> {
  const response = await http.post<PictureSpaceWriteResult>("/xhs/qianfan/picture-space/folders", {
    platform_account_id: accountId,
    name,
  });
  return response.data;
}

/** 移动到目录（平台侧可逆；`fatherDirectoryId` = 目标目录 id，`"0"` = 根）。 */
export async function movePictureSpaceItems(
  accountId: number,
  items: PictureSpaceItem[],
  fatherDirectoryId: string
): Promise<PictureSpaceWriteResult> {
  const response = await http.post<PictureSpaceWriteResult>("/xhs/qianfan/picture-space/move", {
    platform_account_id: accountId,
    items,
    father_directory_id: fatherDirectoryId,
  });
  return response.data;
}

/** 删除 / 彻底删除前预览（二次确认凭据，纯本地）。`action`：`delete` | `permanent-delete`。 */
export async function previewPictureSpaceDelete(
  accountId: number,
  items: PictureSpaceItem[],
  action: "delete" | "permanent-delete" = "delete"
): Promise<PictureSpaceDeletePreview> {
  const response = await http.post<PictureSpaceDeletePreview>("/xhs/qianfan/picture-space/preview", {
    platform_account_id: accountId,
    items,
    action,
  });
  return response.data;
}

/** 批量删除（**进平台回收站**，需 `confirmed` + `confirm_token`）。 */
export async function deletePictureSpaceItems(
  accountId: number,
  items: PictureSpaceItem[],
  confirmToken: string
): Promise<PictureSpaceWriteResult> {
  const response = await http.post<PictureSpaceWriteResult>("/xhs/qianfan/picture-space/delete", {
    platform_account_id: accountId,
    items,
    confirmed: true,
    confirm_token: confirmToken,
  });
  return response.data;
}

/** **从回收站还原**（平台侧可逆，不需要两层确认）。 */
export async function restorePictureSpaceItems(
  accountId: number,
  items: PictureSpaceItem[]
): Promise<PictureSpaceWriteResult> {
  const response = await http.post<PictureSpaceWriteResult>("/xhs/qianfan/picture-space/restore", {
    platform_account_id: accountId,
    items,
  });
  return response.data;
}

/** **彻底删除**（回收站，不可还原，需 `confirmed` + `confirm_token`）。 */
export async function permanentDeletePictureSpaceItems(
  accountId: number,
  items: PictureSpaceItem[],
  confirmToken: string
): Promise<PictureSpaceWriteResult> {
  const response = await http.post<PictureSpaceWriteResult>(
    "/xhs/qianfan/picture-space/permanent-delete",
    {
      platform_account_id: accountId,
      items,
      confirmed: true,
      confirm_token: confirmToken,
    }
  );
  return response.data;
}

/** 批量下载（平台打包成异步任务，返回 task_id）。 */
export async function downloadPictureSpaceItems(
  accountId: number,
  items: PictureSpaceItem[]
): Promise<PictureSpaceWriteResult> {
  const response = await http.post<PictureSpaceWriteResult>("/xhs/qianfan/picture-space/download", {
    platform_account_id: accountId,
    items,
  });
  return response.data;
}

/** 查图片空间异步任务（下载打包进度 / 结果）。 */
export async function fetchPictureSpaceTask(
  accountId: number,
  taskId: string
): Promise<PictureSpaceTask> {
  const response = await http.get<PictureSpaceTask>(`/xhs/qianfan/picture-space/task/${taskId}`, {
    params: { platform_account_id: accountId },
  });
  return response.data;
}

/**
 * **上传本地图片**到图片空间（设计 22 §1.5）。
 *
 * 前端把文件读成 base64 传上来；后端三步（申请凭证 → PUT 到 ROS → 登记条目）。
 */
export async function uploadPictureSpaceFile(
  accountId: number,
  file: File,
  options: { fatherDirectoryId?: string } = {}
): Promise<PictureSpaceUploadResult> {
  const contentBase64 = await fileToBase64(file);
  const response = await http.post<PictureSpaceUploadResult>("/xhs/qianfan/picture-space/upload", {
    platform_account_id: accountId,
    //: ⚠️ 刻意**不传** file.name（用户口径：文件名由后端按内容 MD5 生成，避免重名）。
    //: 传空串 ⇒ 后端走 MD5 分支。
    file_name: "",
    content_base64: contentBase64,
    father_directory_id: options.fatherDirectoryId ?? "0",
  });
  return response.data;
}


/** **批量并发**上传本地图片（方案 B：一次 permit + 服务端并发 PUT）。
 *
 * 用户口径（2026-10-11）：自动加载参数的主图/详情图、以及「从预检 SKU 导入」的规格图
 * 都走并发上传；**并发数默认 5、可在 config 表配置**（`xhs_image_upload_parallelism`）。
 *
 * ⚠️ `results[]` **与入参 `files[]` 逐位对齐**（按下标回填），单项失败不整批失败。
 * ⚠️ 为什么并发不触发风控：PUT 打的是 ROS/COS 对象存储（**不是小红书域名**），
 *   打小红书的只有**整批一次**的 `upload/web/permit`。
 */
export async function uploadPictureSpaceFiles(
  accountId: number,
  files: File[],
  options: { fatherDirectoryId?: string; concurrency?: number } = {}
): Promise<PictureSpaceUploadBatchResult> {
  const payload = await Promise.all(
    files.map(async (file) => ({
      //: 空名 ⇒ 后端按内容 MD5 生成（与单文件端点同口径，避免重名）。
      file_name: "",
      content_base64: await fileToBase64(file),
    }))
  );
  const response = await http.post<PictureSpaceUploadBatchResult>(
    "/xhs/qianfan/picture-space/upload-batch",
    {
      platform_account_id: accountId,
      files: payload,
      father_directory_id: options.fatherDirectoryId ?? "0",
      concurrency: options.concurrency ?? 0,
    }
  );
  return response.data;
}

/** 按**主图**生成商品标题（千帆 `getRecommendItemTitle`）。
 *
 * ⚠️ 千帆发布页上传主图后**自动**触发的就是这一条 —— 平台把**图（带签名链接）**
 * 喂给模型，所以标题与图相符。只传文件名会让模型看不到图、生成无关标题。
 *
 * ⚠️ 平台侧该 action **要签名**（只带 Cookie 重放返回 406）。
 */
export async function recommendItemTitle(
  accountId: number,
  images: {
    link: string;
    name?: string;
    width?: number | null;
    height?: number | null;
    size?: number;
    extension?: string;
    file_id?: string;
  }[],
): Promise<RecommendItemTitleResult> {
  const response = await http.post<RecommendItemTitleResult>(
    "/xhs/qianfan/product/recommend-title",
    { platform_account_id: accountId, main_images: images },
  );
  return response.data;
}

/** 属性**智能生成**（千帆 `predictProductProperty`，只读预测）。
 *
 * ⚠️ **必须带主图**（`image_urls`）—— 真机复测（2026-10-09）：无图平台恒回
 * 空数组 ⇒ 页面提示「平台未给出可填充的属性值」。图对象与平台 `imageUrls` 同形。
 */
export async function predictProperties(
  accountId: number,
  payload: { category_id: string; title?: string; image_urls?: Record<string, unknown>[] },
): Promise<{ properties: Record<string, unknown> }> {
  const response = await http.post<{ properties: Record<string, unknown> }>(
    "/xhs/qianfan/product/predict-properties",
    {
      account_id: accountId,
      ...payload,
    },
  );
  return response.data;
}

/** 取**物流模板候选**（千帆 `getSellerCarriageTemplate`，只读）。 */
export type ShippingTemplate = { template_id: string; name: string };

export async function fetchShippingTemplates(
  accountId: number,
): Promise<{ templates: ShippingTemplate[]; default_template_id: string | null }> {
  const response = await http.post<{ templates: ShippingTemplate[]; default_template_id: string | null }>(
    "/xhs/qianfan/product/shipping-templates",
    { account_id: accountId },
  );
  return response.data;
}

/** 取**物流计划（发货仓）候选**（千帆 `publish_render` 空白表单，只读）。 */
export type LogisticsTemplate = { plan_id: string; name: string; logistics_mode: string };

export async function fetchLogisticsTemplates(
  accountId: number,
): Promise<{ templates: LogisticsTemplate[]; default_plan_id: string | null }> {
  const response = await http.post<{ templates: LogisticsTemplate[]; default_plan_id: string | null }>(
    "/xhs/qianfan/product/logistics-templates",
    { account_id: accountId },
  );
  return response.data;
}

/** 生成**导购短标题**（千帆 `generateItemShortTitle`）。
 *
 * 与商品标题同款口径：能自动生成的就走平台，不拿本地文本冒充。
 *
 * ⚠️ 入参是 `{title, categoryId}`（真机抓包 2026-10-09）—— **必须给类目 id**，
 * 且**没有** `mainImages`。缺类目平台会返回空短标题。
 */
export async function generateShortTitle(
  accountId: number,
  name: string,
  categoryId = "",
): Promise<{ short_title: string }> {
  const response = await http.post<{ short_title: string }>("/xhs/qianfan/product/short-title", {
    platform_account_id: accountId,
    name,
    category_id: categoryId,
  });
  return response.data;
}

/** 上传**商品视频**（直传，不走图片空间 —— 视频登记会被图片库拒）。
 *
 * 真机 bundle（`9515.*.js`）：视频走 `scene=common_file` 独立直传，
 * 成功直接给 `{url, fileId}`。⚠️ 误走图片空间会在登记一步答
 * 「获取上传文件[v]信息失败」（2026-10-09 实测根因）。
 */
export async function uploadProductVideo(
  accountId: number,
  file: File,
): Promise<{
  file_name: string;
  file_id: string;
  url: string;
  extension: string;
  size: number;
  materialId: string;
  path: string;
  link: string;
  duration: number | null;
  width: number | null;
  height: number | null;
}> {
  const contentBase64 = await fileToBase64(file);
  const response = await http.post("/xhs/qianfan/video/upload", {
    platform_account_id: accountId,
    file_name: file.name,
    content_base64: contentBase64,
  });
  return response.data;
}

/** 售后服务规则（按类目；`must_support` = 平台强制支持 ⇒ 勾选不可改）。 */
export type AfterSaleRule = {
  name: string;
  type: string;
  default_support: boolean;
  must_support: boolean;
};

export async function fetchAfterSaleRules(
  accountId: number,
  categoryId: string,
): Promise<{ rules: AfterSaleRule[] }> {
  const response = await http.post<{ rules: AfterSaleRule[] }>(
    "/xhs/qianfan/product/after-sale-rule",
    { platform_account_id: accountId, category_id: categoryId },
  );
  return response.data;
}

/** 取**类目的属性定义**（千帆 `getCategoryProperty`）。
 *
 * ⚠️ 属性定义**不在** `getProductPropertyConfig`（那只回两个开关）——
 * 真机实测（2026-10-08，133KB 响应）落在 `category.categoryPropertyInfo`，
 * 含 `descProperties[]`（其他属性）与 `saleProperties[]`（销售属性=规格类型）。
 */
export async function fetchCategoryProperties(
  accountId: number,
  categoryId: string,
): Promise<CategoryPropertyResult> {
  const response = await http.post<CategoryPropertyResult>(
    "/xhs/qianfan/product/property-config",
    { platform_account_id: accountId, category_id: categoryId },
  );
  return response.data;
}

/** 按**标题 + 主图**推荐商品类目（千帆 `getRecommendCategory`）。
 *
 * ⚠️ 这就是千帆发布页在**标题采用之后**自动带出类目的那条 action
 * （真机 2026-10-08：点「立即使用」后「已选类目」立刻出现）。
 * 平台侧要签名；返回按 score 降序。
 */
export async function recommendProductCategory(
  accountId: number,
  payload: {
    name: string;
    image_url?: string;
    uploader_info_model?: Record<string, unknown>;
  },
): Promise<RecommendCategoryResult> {
  const response = await http.post<RecommendCategoryResult>(
    "/xhs/qianfan/product/recommend-category",
    { platform_account_id: accountId, ...payload },
  );
  return response.data;
}

/** `File` → base64（不含 `data:` 前缀）。 */
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result ?? "");
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/**
 * 批量删除本地台账行（v1.36，设计 13 §33.10；**零平台请求**）。
 *
 * 删的只有本店 `shop_products` 的选中行 —— 平台商品不动（千帆 SDK 无商品删除
 * 接口），`note_goods` 关系行也不动。两个连带后果写在
 * `lib/shop-items-view.ts::batchDeleteHint()` 里，确认框负责讲给用户听。
 *
 * 键用 `item_id`（表格 `rowKey`）：跨页勾选时前端手里就是它；店铺维度由后端用
 * `shop_id_fk` 限定，传别人的 item_id 只会删到 0 行。
 */
export async function deleteXhsShopItems(
  accountId: number,
  itemIds: readonly string[]
): Promise<XhsShopItemsDeleteResult> {
  const response = await http.post<XhsShopItemsDeleteResult>("/xhs/qianfan/items/delete", {
    platform_account_id: accountId,
    item_ids: [...itemIds],
  });
  return response.data;
}

/**
 * 商品详情弹窗的数据源（v1.32 新增，设计 13 §30.3.6；**纯本地**，零平台请求）。
 *
 * 返回基本信息（与列表 `items[]` 元素同构）+ 图集 + SKU 规格明细。
 * `detail_synced_at` 为空串 = 还没补过详情（同步商品任务会多轮补足）——
 * 页面渲染空态 + 说明，**不渲染成 0 行**（缺值口径）。
 */
export async function fetchXhsShopItemDetail(
  accountId: number,
  itemId: string
): Promise<XhsShopItemDetail> {
  const response = await http.post<XhsShopItemDetail>("/xhs/qianfan/item-detail", {
    platform_account_id: accountId,
    item_id: itemId,
  });
  return response.data;
}

// --------------------------------------------------------------------------- //
// M4「商品编辑优化」（设计 20）：分组 / 本地编辑 / AI 润色 / CSV 导出
//
// ⚠️ 边界：**改价 / 上下架不可交付**（千帆 SDK 零商品写接口，设计 20 §0）——
// 这里**不写**它们的方法（写了就是「看起来能改」的假能力）。
// --------------------------------------------------------------------------- //

// ⚠️ 商品分组 4 个请求函数（`fetchProductGroups` / `createProductGroup` /
// `renameProductGroup` / `deleteProductGroup`）已随「彻底移除商品分组」退场
// （2026-10-05）。后端对应路径保留为 **410 墓碑**（`PRODUCT_GROUPS_REMOVED`），
// 前端不再有任何调用方 —— 留住这些函数就是留住一条会 410 的死路径。

// --------------------------------------------------------------------------- //
// 商品操作（2026-10-05 需求 7/8，设计 20 §11）
//
// ⚠️ 与下面 `editShopItem`（改本地台账）**不是一回事**：
// 这三个函数打的是**平台**（上下架 / 删除真商品），不可撤回。
// --------------------------------------------------------------------------- //

/**
 * 同步**指定商品**到本地台账（需求 6）。
 *
 * ⚠️ 平台侧**没有**「按 item_id 拉商品」的读接口（真机调研：`search_item_v2` 只支持
 * 整页 + 筛选，`get_skus_by_item_ids` 只回规格）。⇒ 后端实现是「把指定 id 作为过滤条件、
 * 整店翻页扫描后**只落命中 id**」，所以**慢**（要翻完才能确认没命中）。
 * 弹窗里如实标注这个代价，不假装是「精准单查」。
 */
export async function syncSpecifiedShopItems(
  platformAccountId: number,
  itemIds: string[],
  /**
   * 期间条件（`{scope:"full"}` 或 `{scope:"range", startDate, endDate}`）。
   *
   * ⚠️ 2026-10-06 合并「批量同步商品信息」后，同一个条件弹窗的区间对**按 ID 同步**
   * 这条路也要生效（原来只认 ID，区间无处可传）。不传 = 后端按 `full` 处理。
   */
  options?: SyncNotesOptions,
): Promise<ShopItemsSyncResult> {
  const response = await http.post<ShopItemsSyncResult>("/xhs/qianfan/items/sync", {
    platform_account_id: platformAccountId,
    item_ids: itemIds,
    ...(options?.scope ? { scope: options.scope } : {}),
    ...(options?.startDate ? { start_date: options.startDate } : {}),
    ...(options?.endDate ? { end_date: options.endDate } : {}),
  });
  return response.data;
}

/**
 * 批量上架 / 下架 / 删除**平台商品**（需求 7；**真实写操作**）。
 *
 * 返回**任务回执**（`task_id`），不是执行结果 —— 逐条结果要拿 `task_id` 轮询
 * {@link fetchShopItemOperateResult}（手动触发式任务，与「清理无流量笔记」同款）。
 */
export async function operateShopItems(
  platformAccountId: number,
  action: ShopItemOperateAction,
  /**
   * ⚠️ **必须显式传 id 清单**（不给默认值 = 不给「顺手操作整页」的机会）：
   * 批量入口传勾选集、单行入口传 `[item.item_id]`。
   * 曾把它写成「由调用方自行 memo 出 id 集」⇒ 单行按钮误走了批量处理器
   * （见 `handleItemOperate` / `operateOneItem` 的注释）。
   */
  itemIds: string[],
): Promise<ShopItemOperateResult> {
  const response = await http.post<ShopItemOperateResult>("/xhs/qianfan/items/operate", {
    platform_account_id: platformAccountId,
    action,
    item_ids: itemIds,
  });
  return response.data;
}

/** 商品操作任务的**逐条结果**（弹窗轮询；`items[]` 里每条的 `result` 三态）。 */
export async function fetchShopItemOperateResult(taskId: number): Promise<ShopItemOperateResult> {
  const response = await http.get<ShopItemOperateResult>(`/xhs/qianfan/items/operate/${taskId}`);
  return response.data;
}

/** `PATCH /items/edit` 的执行体（**只带改过的键**，见 `lib/product-edit.ts`）。 */
/** 商品本地编辑（**纯本地写**，零平台请求；平台标题 / 价格不在这里改）。 */
export async function editShopItem(payload: XhsItemEditPayload): Promise<XhsItemEditResult> {
  const response = await http.patch<XhsItemEditResult>("/xhs/qianfan/items/edit", payload);
  return response.data;
}

/** 商品标题 / 卖点 AI 润色（走两个业务点；未配服务商 ⇒ 400 结构化）。 */
export async function polishShopItem(payload: {
  platform_account_id: number;
  item_id: string;
  template_id?: number | null;
  instruction?: string;
}): Promise<XhsItemPolishResult> {
  const response = await http.post<XhsItemPolishResult>("/xhs/qianfan/items/polish", payload);
  return response.data;
}

/* ------------------------------------------------------------------ *
 * 违规词库（2026-10-08 用户口径；设计 25 §2）
 *
 * ⚠️ **为什么独立于千帆**：平台只有 `check_forbidden_keyword`（校验器：拿文本
 * 问平台有没有违规），没有词表的增删改查，也没有「停用/启用」语义。用户要的
 * 四个动作只能落在本地表 `forbidden_words` 上。
 * ------------------------------------------------------------------ */

/** 一条违规词（`match_mode`：1=精确 / 2=包含）。 */
export interface ForbiddenWord {
  id: number;
  word: string;
  match_mode: number;
  enabled: boolean;
  remark: string;
  /** 分组名；空串 = 未分组。 */
  group_name: string;
  created_at: string | null;
  updated_at: string | null;
}

/** 违规词分组（`name` 为空串表示「未分组」，前端负责显示成「未分组」）。 */
export interface ForbiddenWordGroup {
  name: string;
  count: number;
  /** 整组启用态（停用 ⇒ 组内词全部退出匹配）。 */
  enabled: boolean;
}

/**
 * **整组启用 / 停用**（用户口径：「组新增启用停用功能」）。
 *
 * ⚠️ 「未分组」用占位符 `__ungrouped__` 传（空串当路径参数不合法），
 * 后端翻译回空串。
 */
export async function setForbiddenWordGroupEnabled(
  name: string,
  enabled: boolean,
): Promise<{ name: string; enabled: boolean; count: number }> {
  const key = name === "" ? "__ungrouped__" : name;
  const response = await http.post<{ name: string; enabled: boolean; count: number }>(
    `/xhs/forbidden-words/groups/${encodeURIComponent(key)}/enabled`,
    { enabled },
  );
  return response.data;
}

/** 已有分组 + 各自词条数（预检的「指定分组」下拉用）。 */
export async function fetchForbiddenWordGroups(): Promise<ForbiddenWordGroup[]> {
  const response = await http.get<ForbiddenWordGroup[]>("/xhs/forbidden-words/groups");
  return response.data;
}

/** 违规词列表（`enabledOnly` 只列启用中的 —— 那是 OCR 匹配真正用的集合）。 */
export async function fetchForbiddenWords(
  enabledOnly = false,
  groupName?: string,
): Promise<ForbiddenWord[]> {
  const response = await http.get<ForbiddenWord[]>("/xhs/forbidden-words", {
    params: { enabled_only: enabledOnly, ...(groupName ? { group_name: groupName } : {}) },
  });
  return response.data;
}

/**
 * 新增违规词。**重复 ⇒ 后端 409**（`FORBIDDEN_WORD_DUPLICATE`）。
 *
 * ⚠️ 后端把「Best」与「best」、「ＢＥＳＴ」判为同一个词（归一化后比较），
 * 所以前端**不要**自己做去重提示 —— 那样会与服务端口径不一致。
 */
export async function createForbiddenWord(payload: {
  group_name?: string;
  word: string;
  match_mode?: number;
  remark?: string;
  enabled?: boolean;
}): Promise<ForbiddenWord> {
  const response = await http.post<ForbiddenWord>("/xhs/forbidden-words", payload);
  return response.data;
}

/** 修改违规词（词 / 匹配模式 / 备注 / 启用态）。重复 ⇒ 409。 */
export async function updateForbiddenWord(
  wordId: number,
  payload: {
    word?: string;
    match_mode?: number;
    remark?: string;
    enabled?: boolean;
    group_name?: string;
  },
): Promise<ForbiddenWord> {
  const response = await http.patch<ForbiddenWord>(`/xhs/forbidden-words/${wordId}`, payload);
  return response.data;
}

/**
 * 停用 / 启用违规词（软开关；停用后**不参与** OCR 匹配，行不删）。
 *
 * ⚠️ 独立端点而非 PATCH 的一种取值 —— 用户口径把「停用」「启用」与
 * 「新增」「修改」并列为四个动作，前端是四个按钮，且**不读当前态**就能
 * 把按钮摆对。
 */
export async function setForbiddenWordEnabled(wordId: number, enabled: boolean): Promise<ForbiddenWord> {
  const response = await http.post<ForbiddenWord>(`/xhs/forbidden-words/${wordId}/enabled`, { enabled });
  return response.data;
}

/** 删除违规词（**不可恢复**）。 */
export async function deleteForbiddenWord(wordId: number): Promise<{ deleted: number }> {
  const response = await http.delete<{ deleted: number }>(`/xhs/forbidden-words/${wordId}`);
  return response.data;
}

/** 导出商品 CSV（按**当前筛选**；超限后端整批 400，前端先用 `exportBlockReason` 拦）。 */
export async function exportShopItems(query: XhsShopItemsQuery): Promise<Blob> {
  const response = await http.post("/xhs/qianfan/items/export", query, {
    responseType: "blob",
  });
  return response.data as Blob;
}

/**
 * **解析商品数据包**（`POST /item-drafts/parse`；**纯本地，零平台请求**）。
 *
 * 读 xlsx（SKU 表）+ 列目录下的图片 / 视频名，回给弹窗预览。
 * ⚠️ 返回里的 `warnings[]` **必须全渲染** —— 静默过滤掉的东西，
 * 用户到千帆页面才会发现少了什么。
 */
export async function parseItemDraftPackage(
  platformAccountId: number,
  packagePath: string,
): Promise<ItemDraftPackageParseResult> {
  const response = await http.post<ItemDraftPackageParseResult>("/xhs/qianfan/item-drafts/parse", {
    platform_account_id: platformAccountId,
    package_path: packagePath,
  });
  return response.data;
}

/**
 * **按关键词搜商品类目**（`GET /item-drafts/categories`；只回叶子类目）。
 *
 * ⚠️ 只回 `isLeaf=true` —— 商品必须挂在叶子上，回非叶子的选项只会让人
 * 选到一个建不出商品的类目。
 */
export async function searchItemDraftCategories(
  platformAccountId: number,
  keyword: string,
): Promise<ItemDraftCategory[]> {
  const response = await http.get<{ categories: ItemDraftCategory[] }>(
    "/xhs/qianfan/item-drafts/categories",
    { params: { platform_account_id: platformAccountId, keyword } },
  );
  return response.data.categories;
}

/**
 * **新增商品 → 保存到草稿箱**（`POST /item-drafts`）。
 *
 * ⚠️ **只到草稿箱，不会上架** —— 接口的请求体里**没有**任何发布 / 上架字段
 * （后端 `extra="forbid"`，多传直接 422）。红线落在契约层，不是文案承诺。
 *
 * 耗时较长（逐张上传图片 + 建草稿 + 回读校验），超时给足 300s。
 */
export async function createItemDraft(
  payload: ItemDraftCreatePayload,
): Promise<ItemDraftCreateResult> {
  const response = await http.post<ItemDraftCreateResult>("/xhs/qianfan/item-drafts", payload, {
    timeout: 300000,
    //: ⚠️ 同 `publishShopItem`：保存草稿失败由调用方弹完整错误，全局拦截器闭嘴。
    _silent: true,
  } as object);
  return response.data;
}

/**
 * **草稿箱列表**（`GET /item-drafts`；**实时查平台**，不落本地表）。
 *
 * 草稿的唯一真相源在平台（用户在千帆页面继续填、平台审核都在那边），
 * 本地存一份镜像必然漂移成「本地有、平台没有」。
 */
export async function fetchItemDrafts(
  platformAccountId: number,
  pageNo = 1,
  pageSize = 20,
  keyword = "",
  sortField = "create_time",
  order = "desc",
): Promise<ItemDraftListResult> {
  const response = await http.get<ItemDraftListResult>("/xhs/qianfan/item-drafts", {
    params: {
      platform_account_id: platformAccountId,
      page_no: pageNo,
      page_size: pageSize,
      keyword,
      sort_field: sortField,
      order,
    },
  });
  return response.data;
}

/**
 * **批量删除草稿**（`POST /item-drafts/delete`，设计 24 §9.4）。
 *
 * ⚠️ **不可恢复**（草稿删了没有撤销入口）—— 调用前必须显式确认。
 */
export async function deleteItemDrafts(
  platformAccountId: number,
  draftIds: string[],
): Promise<{ platform_account_id: number; deleted: number }> {
  const response = await http.post<{ platform_account_id: number; deleted: number }>(
    "/xhs/qianfan/item-drafts/delete",
    { platform_account_id: platformAccountId, draft_ids: draftIds },
  );
  return response.data;
}

/**
 * **提交商品 → 发布到线上**（`POST /items/publish`，设计 24 §4.2）。
 *
 * ⚠️⚠️ **真实上架写操作**：成功即产生一个在售商品。
 * 「发布成功后是否下架」由调用方决定（2026-10-11 用户**取消红线**）：
 * `off_shelf_after: true` = 发布并下架（保守默认）；`false` = 发布后保持在售。
 * ⚠️ 原句「前端**不得**传 false（那是测试钩子）」**已被取代**。
 *
 * 耗时较长（逐张上传图片 + 提交 + 轮询 + 下架），超时给足 300s。
 */
/* ------------------------------------------------------------------ *
 * 数据包预检（设计 25 §4）
 * ------------------------------------------------------------------ */

export type PrecheckGroup = "main" | "video" | "sku" | "detail";

export interface PrecheckHit {
  word_id: number;
  word: string;
  match_mode: number;
  /** 归一化到 0–1 的红框坐标，可多个（同一词在一张图里出现多次）。 */
  boxes: { x: number; y: number; w: number; h: number }[];
}

export interface PrecheckMediaItem {
  group: PrecheckGroup;
  index: number;
  rel_path: string;
  file_name: string;
  size_bytes: number;
  oversized: boolean;
  unsupported: boolean;
  ocr_ok?: boolean | null;
  /** `ocr_ok === null` = 没检过（抽帧失败 / 引擎缺失），前端默认不勾选 */
  unverified_reason?: string | null;
  error?: string;
  error_message?: string;
  width?: number;
  height?: number;
  hits?: PrecheckHit[];
  has_forbidden?: boolean;
  /** 视频专属：抽了几帧（图片为 `null`）。 */
  frame_count?: number | null;
  /** 视频专属：其中几帧没识别出来。 */
  frames_failed?: number | null;
  /** 视频专属：逐帧明细（第几秒、识别到什么、命中哪些词）。 */
  frames?: { at: number; ocr_ok: boolean; text: string; hits: string[] }[];
}

export interface PrecheckTable {
  title: string;
  sheet_name: string;
  columns: string[];
  rows: (string | null)[][];
  /** 来源 xlsx 的文件名（表格顶部展示）。 */
  source_file?: string;
}

export interface PrecheckResult {
  /** 是否已跑过违规词检测（`false` = 只读取展示）。 */
  inspected?: boolean;
  package_name: string;
  media_groups: Record<PrecheckGroup, number>;
  media: PrecheckMediaItem[];
  tables: PrecheckTable[];
  warnings: string[];
}

/** 上传数据包目录 → 预检（解析 + OCR 违规词），一趟返回。 */
export async function precheckPackage(params: {
  packageName: string;
  files: { relPath: string; blob: Blob }[];
  onProgress?: (percent: number) => void;
}): Promise<{ token: string; precheck: PrecheckResult }> {
  const form = new FormData();
  form.append("package_name", params.packageName);
  for (const file of params.files) {
    // 第三个参数必须是**相对路径**（含目录名），后端据此分区。
    form.append("files", file.blob, file.relPath);
  }
  const response = await http.post<{
    token: string;
    precheck: PrecheckResult;
  }>("/xhs/package-precheck", form, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 600000,
    onUploadProgress: (event) => {
      if (event.total && params.onProgress) {
        params.onProgress(Math.round((event.loaded / event.total) * 100));
      }
    },
  });
  return response.data;
}

/**
 * 按**本地目录绝对路径**预检（主路径，用户 2026-10-08 口径「不要上传」）。
 *
 * 零字节离开本机：只把目录**路径**发给后端，媒体与表格都由服务端在本地读。
 * ⚠️ 慢在 OCR（真包 69 张 ≈ 52s），超时给足。
 */
export async function precheckLocalPackage(params: {
  packagePath: string;
  packageName?: string;
  /** ⚠️ `false`（默认）= 只读取展示，不跑 OCR；`true` = 连违规词检测一起跑。 */
  inspect?: boolean;
}): Promise<{ token: string; package_path: string; precheck: PrecheckResult }> {
  const response = await http.post<{
    token: string;
    package_path: string;
    precheck: PrecheckResult;
  }>(
    "/xhs/package-precheck/local",
    {
      package_path: params.packagePath,
      package_name: params.packageName ?? "",
      inspect: params.inspect ?? false,
    },
    { timeout: 900000 },
  );
  return response.data;
}

/**
 * **弹出系统「选择文件夹」对话框**，返回用户选中的绝对路径。
 *
 * 用户 2026-10-08 口径：「点击加载数据包选择本地文件夹，获得本地夹地址后，
 * 直接读取该数据包内的商品数据」。
 *
 * ⚠️ 由**后端**弹框：浏览器拿不到 `File` 的真实路径（`<input webkitdirectory>`
 * 只给文件名），所以只有与用户同机的后端进程能拿到 `D:\\...`。
 * ⚠️ 用户取消 ⇒ `cancelled=true`（不是错误，前端别弹红条）。
 */
export async function pickPackageFolder(): Promise<{ package_path: string; cancelled: boolean }> {
  const response = await http.post<{ package_path: string; cancelled: boolean }>(
    "/xhs/package-precheck/pick-folder",
    undefined,
    //: 用户挑目录可以很久（在资源管理器里翻），超时给足 5 分钟。
    { timeout: 300000 },
  );
  return response.data;
}

/** 改了违规词库后，对已加载的包重跑一次预检。 */
export async function rescanPackagePrecheck(
  token: string,
  inspect = true,
  /** 只用这些违规词分组（空 = 全部分组，用户口径的默认）。 */
  groups: string[] = [],
): Promise<PrecheckResult> {
  //: ⚠️ 入参走 **body**（不是 query）—— 分组是列表，塞 query 会退化成
  //: `?groups=a&groups=b` 的重复键，两边都容易写错。
  const response = await http.post<{ precheck: PrecheckResult }>(
    `/xhs/package-precheck/${token}/scan`,
    { inspect, groups },
    //: `inspect=true` 会跑 69 张图 + 视频抽帧，超时给足。
    { timeout: 900000 },
  );
  return response.data.precheck;
}

/** 预检**执行进度**（用户口径 2026-10-09）：跑 OCR/抽帧期间轮询。 */
export type PrecheckProgress = {
  phase: string | null;
  done: number;
  total: number;
  percent: number;
};

export async function fetchPrecheckProgress(token: string): Promise<PrecheckProgress> {
  const response = await http.get<PrecheckProgress>(`/xhs/package-precheck/${token}/progress`);
  return response.data;
}

/** 取暂存包里的一张图（红框 overlay 的底图；需带鉴权 ⇒ 走 blob URL）。 */
export async function fetchPrecheckImage(token: string, relPath: string): Promise<string> {
  const response = await http.get(`/xhs/package-precheck/${token}/image`, {
    params: { path: relPath },
    responseType: "blob",
  });
  return URL.createObjectURL(response.data as Blob);
}

/** 丢弃暂存包（关弹窗 / 发布完成后调用）。 */
export async function discardPackagePrecheck(token: string): Promise<void> {
  await http.delete(`/xhs/package-precheck/${token}`);
}

/**
 * 真实上架写操作。
 *
 * ⚠️⚠️ **真实上架写操作**：成功即产生一个在售商品。红线（用户 2026-10-08）
 * 「发布成功后是否下架」由调用方决定（2026-10-11 用户取消红线）：
 * `off_shelf_after: true` = 发布并下架（保守默认）；`false` = 发布后保持在售。
 * ⚠️ 原句「前端**不得**传 false（那是测试钩子）」**已被取代**。
 *
 * 耗时较长（逐张上传图片 + 提交 + 轮询 + 下架），超时给足 300s。
 */
export async function publishShopItem(
  payload: ItemPublishPayload,
): Promise<ItemPublishResult> {
  const response = await http.post<ItemPublishResult>("/xhs/qianfan/items/publish", payload, {
    timeout: 300000,
    //: ⚠️ 用户口径（2026-10-10）：发布失败**由调用方弹一条完整错误**，全局拦截器
    //: 不许再叠一句「请求失败，请稍后重试」盖掉它（两个 message 后弹的覆盖前弹的）。
    //: `_silent` 让拦截器对 4xx/5xx 闭嘴，错误仍会 reject 到调用方 catch。
    _silent: true,
  } as object);
  return response.data;
}

/**
 * **生成商品详情图**（商品列表**操作列**独立弹窗内触发，设计 17 §13.3.3；v2.12 从详情弹窗迁出）。
 *
 * 走 `generate_product_detail_image` 业务点（与「生成配图」是两个业务点：配图服务笔记正文，
 * 详情图服务商品）。⚠️ **不传图片 URL** —— 商品图由服务端按 `item_id` 从本地台账取：
 * 让前端传 URL 等于让后端去下载任意地址（SSRF 面），且台账里就有真值。
 *
 * 出图较慢（服务商生图），超时给足 180s（同 `generateImageWithAi`）。
 */
export async function generateProductDetailImage(
  accountId: number,
  itemId: string,
  prompt = "",
  referenceImageUrls: string[] = [],
  count = 3,
  regenerate?: { group: string; index: number },
  groupSettings?: Record<string, { prompt?: string; count?: number }>,
  //: v2.24：**公共提示词模板**（用户口径「生成图片时应选择公共提示词模板，保证图片商品的
  //: 一致性」）。`templateId` 是顶层默认；`groupTemplates` 按组覆盖（主图/详情图各选一个）。
  //: ⚠️ 优先级：组级 > 顶层 > 手输 prompt > 内置默认（判据只能有一个赢家）。
  templateId?: number,
  groupTemplates?: Record<string, { template_id?: number }>
): Promise<GenerateDetailImageResult> {
  const response = await http.post<GenerateDetailImageResult>(
    "/xhs/qianfan/generate-detail-image",
    {
      platform_account_id: accountId,
      item_id: itemId,
      prompt,
      // v2.15：勾选的参考图（主图 / 原商品详情图）。空数组 ⇒ 后端沿用商品首图。
      // ⚠️ 这里只传**从台账勾选的** URL —— 后端会按台账白名单校验，
      // 传别的会 400（保留 SSRF 防线）。
      ...(referenceImageUrls.length ? { reference_image_urls: referenceImageUrls } : {}),
      // v2.18/v2.20：生成张数（3~10）。默认 3 ⇒ 总是显式传（后端默认值也随之一致）。
      count,
      // v2.21：单张重做（只重跑该组第 N 张）。不传 = 整组生成。
      ...(regenerate ? { regenerate } : {}),
      // v2.22：每组各自配置（提示词 / 张数）。不传 = 用顶层兜底。
      ...(groupSettings ? { group_settings: groupSettings } : {}),
      // v2.24：公共提示词模板（选了才传 —— 不传时后端走「手输 > 内置默认」的旧链路）。
      ...(templateId ? { template_id: templateId } : {}),
      ...(groupTemplates && Object.keys(groupTemplates).length
        ? { group_templates: groupTemplates }
        : {}),
    },
    // ⚠️ v2.18：张数越多耗时越长（**逐张串行请求**，每张上游 timeout 180s）
    // ⇒ 超时按张数放大，否则多图必然在前端先超时（后端还在跑，用户看到假失败）。
    { timeout: 180000 * Math.max(1, count) }
  );
  return response.data;
}

/**
 * 某个商品**挂了哪些笔记**（商品列表页点「N 篇」）。
 *
 * 服务端是**纯本地 join**（`shop_note_goods` 按 `item_id` 取行，标题/指标取自同步时存的
 * `entry_json`），**零平台请求**；因此响应带 `synced_notes` / `goods_note_total` 覆盖率，
 * 页面必须把「未同步的可能不在其中」讲清楚。
 */
export async function fetchXhsItemNotes(
  accountId: number,
  itemId: string
): Promise<XhsItemNotesResponse> {
  const response = await http.post<XhsItemNotesResponse>("/xhs/qianfan/item-notes", {
    platform_account_id: accountId,
    item_id: itemId,
  });
  return response.data;
}

/**
 * 在自有内容范围内按关键词搜索（跨 我发布的 / 我赞过的 / 我收藏的）。
 *
 * 服务端是**有界扫描**：每个分类最多 `pages_per_scope` 页（默认 3、上限 5），
 * 响应里带每个分类的扫描元数据 —— 页面**必须**把「已扫描多少页」显示出来，
 * 否则用户会把「没搜到」误读成「我没有这条笔记」（设计文档 §22）。
 *
 * 注意匹配域：自有内容的列表项**只有标题没有正文**，所以这是标题搜索。
 */
export async function fetchXhsSelfSearch(payload: {
  account_id: number;
  keyword: string;
  pages_per_scope?: number;
  cursors?: Partial<Record<XhsSelfContentScope, string>>;
  profile_url?: string;
}): Promise<XhsSelfSearchResponse> {
  const response = await http.post<XhsSelfSearchResponse>("/xhs/pc/users/search", payload);
  return response.data;
}

export async function createDraftFromNote(payload: CreateDraftPayload): Promise<Draft> {
  const response = await http.post<Draft>("/drafts", payload);
  return response.data;
}

export async function fetchDrafts(platform = "xhs"): Promise<Paginated<Draft>> {
  const response = await http.get<Paginated<Draft>>("/drafts", { params: { platform } });
  return response.data;
}

export async function updateDraft(draftId: number, payload: { title?: string; body?: string; tags?: { id?: string; name: string }[] }): Promise<Draft> {
  const response = await http.patch<Draft>(`/drafts/${draftId}`, payload);
  return response.data;
}

export async function deleteDraft(draftId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/drafts/${draftId}`);
  return response.data;
}

export type DraftAsset = {
  id: number;
  draft_id: number;
  asset_type: "image" | "video" | string;
  url: string;
  local_path: string;
  sort_order: number;
};

export async function fetchDraftAssets(draftId: number): Promise<{ items: DraftAsset[] }> {
  const response = await http.get<{ items: DraftAsset[] }>(`/drafts/${draftId}/assets`);
  return response.data;
}

export async function addDraftAsset(draftId: number, payload: { asset_type: string; url?: string; local_path?: string }): Promise<DraftAsset> {
  const response = await http.post<DraftAsset>(`/drafts/${draftId}/assets`, payload);
  return response.data;
}

export async function deleteDraftAsset(draftId: number, assetId: number): Promise<void> {
  await http.delete(`/drafts/${draftId}/assets/${assetId}`);
}

export async function updateDraftAsset(draftId: number, assetId: number, payload: { url?: string; local_path?: string }): Promise<DraftAsset> {
  const response = await http.patch<DraftAsset>(`/drafts/${draftId}/assets/${assetId}`, payload);
  return response.data;
}

export async function reorderDraftAssets(draftId: number, assetIds: number[]): Promise<void> {
  await http.put(`/drafts/${draftId}/assets/reorder`, { asset_ids: assetIds });
}

export async function sendDraftToPublish(draftId: number, payload: SendDraftToPublishPayload): Promise<PublishJob> {
  const response = await http.post<PublishJob>(`/drafts/${draftId}/send-to-publish`, payload);
  return response.data;
}

export async function rewriteDraftWithAi(payload: RewriteDraftPayload): Promise<Draft> {
  const response = await http.post<Draft>("/ai/rewrite-note", payload);
  return response.data;
}

export async function generateNoteWithAi(payload: GenerateNotePayload): Promise<Draft> {
  const response = await http.post<Draft>("/ai/generate-note", payload);
  return response.data;
}

export async function generateTitleOptions(payload: GenerateTitlePayload): Promise<{ items: string[] }> {
  const response = await http.post<{ items: string[] }>("/ai/generate-title", payload);
  return response.data;
}

export async function generateTagOptions(payload: GenerateTagsPayload): Promise<{ items: string[] }> {
  const response = await http.post<{ items: string[] }>("/ai/generate-tags", payload);
  return response.data;
}

export async function fetchGeneratedImageAssets(): Promise<Paginated<GeneratedImageAsset>> {
  const response = await http.get<Paginated<GeneratedImageAsset>>("/ai/images/assets");
  return response.data;
}

export async function deleteGeneratedImageAsset(assetId: number): Promise<void> {
  await http.delete(`/ai/images/assets/${assetId}`);
}

export async function deleteUserImage(fileName: string): Promise<void> {
  await http.delete(`/files/images/${fileName}`);
}

export async function generateImageWithAi(payload: GenerateImagePayload): Promise<GenerateImageResult> {
  const response = await http.post<GenerateImageResult>("/ai/images/generate", payload, { timeout: 180000 });
  return response.data;
}

export async function fetchUserImages(): Promise<{ items: UserImageFile[] }> {
  const response = await http.get<{ items: UserImageFile[] }>("/files/images");
  return response.data;
}

export async function describeImageWithAi(payload: DescribeImagePayload): Promise<{ text: string }> {
  const response = await http.post<{ text: string }>("/ai/images/describe", payload);
  return response.data;
}

// —— 「平台 × 业务点 → 服务商」映射（设计 17 §13.2，v2.0）——
//
// ⚠️ 一期 6 个 `*ModelConfig` 请求函数已随 v2.0 退场（服务商单独配置 + 业务点映射）。
// 同 DEF-044：端点退场则请求函数同批移除，不留影子实现。

/** 业务点清单（**后端下发**，前端零硬编码 —— 新增业务点前端零改动） */
export async function fetchBusinessProviderKeys(): Promise<BusinessKeysResponse> {
  const response = await http.get<BusinessKeysResponse>("/llm/business-provider-keys");
  return response.data;
}

/** 映射列表（含未配置的业务点，用于渲染空态行） */
export async function fetchBusinessProviders(platform?: string): Promise<BusinessProviderListResponse> {
  const response = await http.get<BusinessProviderListResponse>("/llm/business-providers", {
    params: platform ? { platform } : undefined,
  });
  return response.data;
}

/** **实时切换服务商 + 具体模型**（v2.3）：upsert (platform, business_key) → (provider_key, model_name)。改一行即生效。 */
export async function upsertBusinessProvider(
  payload: BusinessProviderUpsertPayload,
): Promise<{
  platform: string;
  business_key: string;
  provider_key: string;
  model_name: string;
  configured: boolean;
}> {
  const response = await http.put("/llm/business-providers", payload);
  return response.data;
}

// —— LLM 网关配置（设计 17 §7.1，M1/T1）——
export async function fetchGatewayConfigs(): Promise<Paginated<LlmGatewayConfig>> {
  // 工作平台公共维度（设计 17 §7.1 v1.4）：网关不挂 /xhs/ 平台前缀
  const response = await http.get<Paginated<LlmGatewayConfig>>("/llm/gateway-configs", { params: { page: 1, page_size: 100 } });
  return response.data;
}

/**
 * 服务商 × 模型**两级列表**（v2.9，2026-10-02 用户口径「服务商使用父子列表展示」）。
 *
 * 一个端点拿到「服务商 + 其全部模型行」—— 前端服务商页据此渲染父子两张独立表
 * （父表列 = 服务商维度；子表列 = 模型维度，各用各的表头）。
 *
 * ⚠️ 与 `fetchGatewayConfigs()` 的区别只在 `models`：那个端点的每项 `models` 恒为 `[]`
 * （后端 `_serialize_config` 的形参默认值），只够渲染父表；本端点才真的装了模型行。
 * **别用 `fetchGatewayConfigs` 渲染子表** —— 子表会永远空着（且不报错，最难查的那种）。
 */
export async function fetchGatewayModelsTree(): Promise<Paginated<LlmGatewayConfig>> {
  const response = await http.get<Paginated<LlmGatewayConfig>>(
    "/llm/gateway-configs/models-tree",
    { params: { page: 1, page_size: 100 } }
  );
  return response.data;
}

export async function fetchGatewayStats(configId: number): Promise<LlmGatewayStats> {
  const response = await http.get<LlmGatewayStats>(`/llm/gateway-configs/${configId}/stats`);
  return response.data;
}

export async function createGatewayConfig(payload: LlmGatewayConfigPayload): Promise<LlmGatewayConfig> {
  const response = await http.post<LlmGatewayConfig>("/llm/gateway-configs", payload);
  return response.data;
}

export async function updateGatewayConfig(
  configId: number,
  payload: Partial<LlmGatewayConfigPayload> & { enabled?: boolean }
): Promise<LlmGatewayConfig> {
  const response = await http.patch<LlmGatewayConfig>(`/llm/gateway-configs/${configId}`, payload);
  return response.data;
}

/**
 * **同步服务商模型**（v2.9，`POST /llm/gateway-configs/{id}/sync-models`）。
 *
 * 动作 = 拉上游 `/models` 目录 + 调一次 LLM 归纳整批模型类型 + upsert 模型行。
 * 「创建服务商时自动同步一次」走的是**同一份后端实现**（DEF-044），前端这里只供
 * 操作列的「同步服务商模型」按钮重试。
 *
 * ⚠️ **同步失败不是 HTTP 错误**：上游不可达 / 令牌过期时接口仍 200，
 * 看 `summary.ok` 与 `summary.reason`（服务商本身没问题，只是这次没同步成）。
 */
export async function syncGatewayModels(
  configId: number,
  payload: { platform?: string } = {}
): Promise<{ id: number; sync: LlmGatewaySyncSummary; catalog: string[]; models: LlmGatewayModelRow[] }> {
  const response = await http.post<{
    id: number;
    sync: LlmGatewaySyncSummary;
    catalog: string[];
    models: LlmGatewayModelRow[];
  }>(`/llm/gateway-configs/${configId}/sync-models`, payload);
  return response.data;
}

/**
 * 改一条**模型行**的类型（v2.9「类型」列就地可编辑下拉）。
 *
 * 走既有 `PATCH /llm/gateway-configs/{id}/models/{model_id}`（`update_gateway_model`
 * 早就支持 `model_type`）—— 不为「改类型」新开端点（同一语义不留两份实现）。
 */
export async function updateGatewayModel(
  configId: number,
  modelId: number,
  payload: Partial<Pick<LlmGatewayModelRow, "model_type" | "enabled" | "platform" | "remark">>
): Promise<LlmGatewayModelRow> {
  const response = await http.patch<LlmGatewayModelRow>(
    `/llm/gateway-configs/${configId}/models/${modelId}`,
    payload
  );
  return response.data;
}

// ⚠️ ~~`deleteGatewayConfig`~~ **v1.9 移除**（用户口径「移除删除功能」）：
// 后端 DELETE 端点为 **410 墓碑**（设计 17 §7.1），前端不再有删除入口。
// 与 `LlmGatewayToken` 的 4 个请求函数一起删（DEF-044：不留死代码）。

export async function testGatewayConfig(
  configId: number
): Promise<{ id: number; healthy: boolean; message: string; models: string[] }> {
  const response = await http.post<{ id: number; healthy: boolean; message: string; models: string[] }>(
    `/llm/gateway-configs/${configId}/test`
  );
  return response.data;
}

/**
 * 队列记录页的**父子树**（`GET /tasks?tree=1`，2026-09-26 用户口径
 * 「批次+详情父子树风格展示」）。
 *
 * 为什么用同一个 `GET /tasks` 的 `tree` 参数而不是新端点：这一页要的是「`tasks`
 * 的另一种视图」，不是另一份资源 —— 新开端点等于把「任务归属收口 / 权限」
 * 多维护一处（DEF-044 同型）。
 *
 * 返回的顶级行**只有批次父行**（一次入队动作 = 一个唯一批次号），每行带
 * `children`（明细，FIFO 序）+ `summary`。**不做存量兼容**（2026-09-26 用户口径
 * 「不做兼容逻辑，数据已清空」）：不再有「无父的队列项行」这种形态。
 * 批次行的 `status` / `progress` / `duration_ms` 由后端**按子行现算**覆盖 ——
 * 前端不再自己汇总（判据只有一份）；批次**自己的 `created_at` 不被覆盖**
 * （「批次创建时间」列取它）。
 *
 * `batchNo` 给定时按**唯一批次号**查（同日用户口径「新增列表支持使用唯一批次号
 * 查询」）：大小写不敏感、支持只给尾段（用户手抄常只记得后四位）。
 */

export async function fetchQueueRecords(
  limit?: number,
  batchNo?: string
): Promise<Paginated<TaskRecord>> {
  const response = await http.get<Paginated<TaskRecord>>("/tasks", {
    params: {
      tree: 1,
      ...(limit ? { page_size: limit } : {}),
      ...(batchNo ? { batch_no: batchNo } : {}),
    },
  });
  return response.data;
}

/**
 * 任务记录详情（含 `children` —— 一个批次 = 父行 + N 个子行，设计 13 §35.9）。
 *
 * 与列表的差别只在 `children`：列表一次拉一页看不出「这一批里有哪几篇、各自什么
 * 结果」。同步 / 发布失败的真实原因只在子行 `payload.reason` 里给一次（不落库到
 * 笔记行）⇒ 详情抽屉必须把它显示出来，吞了用户就失去自救能力（DEF-058 同型）。
 */
/**
 * 队列清单 + 每个队列的实时计数（`GET /tasks/queues`，设计 13 §35.11）。
 *
 * ⚠️ **为什么由后端下发**：队列不止同步 / 发布两种（后端服务可能有很多不同的队列）。
 * 前端写死类型常量 ⇒ 新队列上线时界面不显示（「按钮在、功能不在」的老问题）。
 * 队列记录页按这份清单渲染：新增队列 = 后端加一行注册，前端零改动。
 *
 * 2026-09-26 批次层：清单同时下发 `batch_task_type`（批次父行类型）—— 前端把
 * 批次行也归到它所属队列做筛选，**依然不写死类型清单**。
 * ⚠️ 它是**注册表常量**（每个队列恒定），**不上屏**：卡片只显示队列类型
 * （2026-09-26 用户口径「直接使用队列类型不就可以了，唯一 key 肯定是唯一的，
 * 不需要多一个批次」）；真实批次号是任务行上的 `batch_no`（`NB…`）。
 */
export interface QueueSummary {
  key: string;
  label: string;
  item_task_type: string;
  /**
   * 批次父行类型（一次入队动作 = 一个批次号；批次行永不被执行）。
   * **只给 `queueKeyForTask` 反查归队用，不渲染**（它恒定，会被误读成批次号）。
   */
  batch_task_type: string;
  note_bound: boolean;
  /** 待跑 + 在跑。 */
  open: number;
  /** 近 15 天成功（与清理任务同口径，不是全库累计）。 */
  done: number;
  failed: number;
  cancelled: number;
}

export async function fetchQueues(): Promise<QueueSummary[]> {
  const response = await http.get<{ items: QueueSummary[] }>("/tasks/queues");
  return response.data.items ?? [];
}

export async function fetchTaskDetail(taskId: number): Promise<TaskRecord> {
  const response = await http.get<TaskRecord>(`/tasks/${taskId}`);
  return response.data;
}

/**
 * 手动清理**指定**队列记录（单条 / 批量选中；2026-09-26 用户口径「新增手动批量
 * 清理」；同日再一轮「支持按照批次清理记录……清理功能分为两个维度，批次维度和批次内
 * 的操作」）。
 *
 * 返回**分两个维度报账**：`batches`（批次维度 = 点名批次行 / 一键清除）、`items`
 * （批次内明细维度 = 点名单条）、`blocked`（被挡下的批次 + 原因）。⚠️ 批内有
 * **正在执行 / 待执行**或**待重试**明细的批次**整批不清理**（原子动作，不半清）；
 * 前端禁用按钮只是体验，这里才是边界。
 */
export async function cleanTasks(taskIds: number[]): Promise<CleanResultShape> {
  const response = await http.post<CleanResultShape>("/tasks/clean", { task_ids: taskIds });
  return response.data;
}

/**
 * 一键清除**全部可清**队列记录：只清**整批可清**的批次（连明细）；批内有活 /
 * 待重试明细的，**连它可清的明细也不动**（不把还在使用的批次清成残的）。
 */
export async function cleanAllTasks(): Promise<CleanResultShape> {
  const response = await http.post<CleanResultShape>("/tasks/clean-all");
  return response.data;
}

export async function cancelTask(taskId: number): Promise<TaskRecord> {
  const response = await http.post<TaskRecord>(`/tasks/${taskId}/cancel`);
  return response.data;
}

export async function retryTask(taskId: number): Promise<TaskRecord> {
  const response = await http.post<TaskRecord>(`/tasks/${taskId}/retry`);
  return response.data;
}

export async function runDueTasks(platform = "xhs"): Promise<RunDueTasksResponse> {
  const response = await http.post<RunDueTasksResponse>("/tasks/run-due", null, { params: { platform } });
  return response.data;
}

// Schedule Task Center (设计 15：若依式任务管理 + 调度日志)

export type ScheduleJobQuery = {
  status?: string;
  trigger_type?: string;
  keyword?: string;
  page?: number;
  page_size?: number;
};

export type ScheduleLogQuery = {
  job_id?: number;
  job_key?: string;
  /** 按任务类型筛选（cron / interval / at；空 = 全部） */
  trigger_type?: string;
  status?: string;
  date_from?: string;
  date_to?: string;
  page?: number;
  page_size?: number;
};

export async function fetchScheduleJobs(query: ScheduleJobQuery = {}): Promise<Paginated<ScheduledJob>> {
  const response = await http.get<Paginated<ScheduledJob>>("/schedule/jobs", { params: query });
  return response.data;
}

export async function fetchScheduleTargets(): Promise<ScheduleTarget[]> {
  const response = await http.get<{ items: ScheduleTarget[] }>("/schedule/targets");
  return response.data.items;
}

export async function createScheduleJob(payload: {
  name: string;
  job_key: string;
  schedule?: ScheduleIn;
  params?: Record<string, unknown>;
  remark?: string;
}): Promise<ScheduledJob> {
  const response = await http.post<ScheduledJob>("/schedule/jobs", payload);
  return response.data;
}

export async function updateScheduleJob(
  jobId: number,
  payload: {
    name?: string;
    schedule?: ScheduleIn;
    params?: Record<string, unknown>;
    remark?: string;
  }
): Promise<ScheduledJob> {
  const response = await http.put<ScheduledJob>(`/schedule/jobs/${jobId}`, payload);
  return response.data;
}

export async function deleteScheduleJob(jobId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/schedule/jobs/${jobId}`);
  return response.data;
}

export async function changeScheduleJobStatus(
  jobId: number,
  status: "active" | "paused"
): Promise<ScheduledJob> {
  const response = await http.patch<ScheduledJob>(`/schedule/jobs/${jobId}/status`, { status });
  return response.data;
}

/**
 * 「执行一次」（设计 22 §5）。
 *
 * `params` 传了 ⇒ **只对这一次**生效（不写回任务行配置）：用户改的是「这次用什么
 * 参数跑」，不是「把定时配置改掉」。不传 ⇒ 按任务行配置执行 —— 无参数任务走这条。
 */
export async function runScheduleJobOnce(
  jobId: number,
  params?: Record<string, unknown>
): Promise<{ log_id: number; status: string }> {
  const response = await http.post<{ log_id: number; status: string }>(
    `/schedule/jobs/${jobId}/run`,
    params ? { params } : {}
  );
  return response.data;
}

/**
 * 调度日志列表（任务中心「任务日志」页签 / 队列记录页）。
 *
 * 一律 `_silent`：**失败的展示权归调用点**（`task-center-page` 的日志页签自己
 * 给「任务日志加载失败，请稍后重试。」，队列记录页各自给本页上下文）。不拦的话
 * 页面上会**同时**弹两条 —— 调用点那条 + 拦截器兜底的通用
 * 「请求失败，请稍后重试」，后者既没有上下文也不是真原因（2026-09-30 用户口径：
 * 「你先提示任务日志加载失败，请稍后重试，又提示请求失败，请稍后重试，
 * 这 2 个移除后一个」）。
 */
export async function fetchScheduleLogs(query: ScheduleLogQuery = {}): Promise<Paginated<ScheduledJobLog>> {
  const response = await http.get<Paginated<ScheduledJobLog>>("/schedule/logs", {
    params: query,
    _silent: true,
  } as never);
  return response.data;
}

export async function deleteScheduleLogs(ids: number[]): Promise<{ requested: number; deleted: number }> {
  const response = await http.delete<{ requested: number; deleted: number }>("/schedule/logs", {
    data: { ids }
  });
  return response.data;
}

export async function cleanScheduleLogs(jobId?: number): Promise<{ deleted: number }> {
  const response = await http.delete<{ deleted: number }>("/schedule/logs/clean", {
    params: jobId ? { job_id: jobId } : undefined
  });
  return response.data;
}

export async function validateCron(cron: string): Promise<{ valid: boolean; next_fire_times: string[] }> {
  const response = await http.post<{ valid: boolean; next_fire_times: string[] }>("/schedule/cron/validate", {
    cron_expression: cron
  });
  return response.data;
}

export async function fetchScheduleStatus(): Promise<ScheduleStatus> {
  const response = await http.get<ScheduleStatus>("/schedule/status");
  return response.data;
}

export async function fetchMonitoringTargets(): Promise<Paginated<MonitoringTarget>> {
  const response = await http.get<Paginated<MonitoringTarget>>("/xhs/monitoring/targets");
  return response.data;
}

export async function createMonitoringTarget(payload: MonitoringTargetPayload): Promise<MonitoringTarget> {
  const response = await http.post<MonitoringTarget>("/xhs/monitoring/targets", payload);
  return response.data;
}

/**
 * 暂停 / 恢复监控目标（DEF-012 / T-112）。
 *
 * ⚠️ 复用既有的 `PATCH /xhs/monitoring/targets/{id}`（`status: active|paused`）——
 * 后端从 2026-09-18 起就支持，缺的只是前端入口（**不给后端加重复端点**，DEF-044）。
 * 暂停 = 巡检跳过它（`monitoring_crawl_service` 只取 `status == active`）。
 */
export async function setMonitoringTargetStatus(
  targetId: number,
  status: "active" | "paused"
): Promise<MonitoringTarget> {
  const response = await http.patch<MonitoringTarget>(`/xhs/monitoring/targets/${targetId}`, {
    status,
  });
  return response.data;
}

export async function deleteMonitoringTarget(targetId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/xhs/monitoring/targets/${targetId}`);
  return response.data;
}

export async function refreshMonitoringTarget(targetId: number): Promise<MonitoringRefreshResponse> {
  const response = await http.post<MonitoringRefreshResponse>(`/xhs/monitoring/targets/${targetId}/refresh`);
  return response.data;
}

export async function fetchMonitoringSnapshots(targetId: number): Promise<{ target_id: number; items: MonitoringSnapshot[] }> {
  const response = await http.get<{ target_id: number; items: MonitoringSnapshot[] }>(
    `/xhs/monitoring/targets/${targetId}/snapshots`
  );
  return response.data;
}

export async function fetchMonitoringTargetNotes(targetId: number): Promise<{ target_id: number; items: MonitoringNote[] }> {
  const response = await http.get<{ target_id: number; items: MonitoringNote[] }>(
    `/xhs/monitoring/targets/${targetId}/notes`
  );
  return response.data;
}

export async function fetchKeywordGroups(platform = "xhs"): Promise<Paginated<KeywordGroup>> {
  const response = await http.get<Paginated<KeywordGroup>>("/keyword-groups", { params: { platform } });
  return response.data;
}

export async function createKeywordGroup(payload: KeywordGroupPayload): Promise<KeywordGroup> {
  const response = await http.post<KeywordGroup>("/keyword-groups", payload);
  return response.data;
}

export async function fetchKeywordGroup(groupId: number): Promise<KeywordGroupDetail> {
  const response = await http.get<KeywordGroupDetail>(`/keyword-groups/${groupId}`);
  return response.data;
}

export async function updateKeywordGroup(
  groupId: number,
  payload: Partial<KeywordGroupPayload>
): Promise<KeywordGroup> {
  const response = await http.patch<KeywordGroup>(`/keyword-groups/${groupId}`, payload);
  return response.data;
}

export async function deleteKeywordGroup(groupId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/keyword-groups/${groupId}`);
  return response.data;
}

export async function fetchPublishJobs(platform = "xhs"): Promise<Paginated<PublishJob>> {
  const response = await http.get<Paginated<PublishJob>>("/publish/jobs", { params: { platform } });
  return response.data;
}

export async function fetchPublishJob(jobId: number): Promise<PublishJob> {
  const response = await http.get<PublishJob>(`/publish/jobs/${jobId}`);
  return response.data;
}

export async function updatePublishJob(jobId: number, payload: PublishJobUpdatePayload): Promise<PublishJob> {
  const response = await http.patch<PublishJob>(`/publish/jobs/${jobId}`, payload);
  return response.data;
}

export async function publishJobToCreator(jobId: number): Promise<PublishJob> {
  const response = await http.post<PublishJob>(`/publish/jobs/${jobId}/publish`);
  return response.data;
}

export async function deletePublishJob(jobId: number): Promise<void> {
  await http.delete(`/publish/jobs/${jobId}`);
}

export async function fetchPublishAssets(jobId: number): Promise<Paginated<PublishAsset>> {
  const response = await http.get<Paginated<PublishAsset>>(`/publish/jobs/${jobId}/assets`);
  return response.data;
}

export async function importXhsCookieAccount(payload: {
  sub_type: "pc" | "creator";
  cookie_string: string;
  sync_creator?: boolean;
  //: **第二条令牌**（§11.6 / v1.24）：可选的千帆/创作者平台 Cookie。
  //: www 与 ark 是两套互不通用的令牌，一次导入两份 ⇒ 库里只留**一个账号**。
  ark_cookie_string?: string;
}): Promise<PlatformAccount> {
  const response = await http.post<PlatformAccount>("/accounts/import-cookie", {
    platform: "xhs",
    ...payload
  });
  return response.data;
}

/**
 * 健康检查（会改账号 `status`，并顺带刷新粉丝/关注/获赞三项计数）。
 * 响应里的 `metrics_reason` 非空表示互动数据不可得（如创作者链路），文案可直接展示。
 */
export async function checkAccount(
  accountId: number,
): Promise<PlatformAccount & { metrics_reason?: string }> {
  const response = await http.post<PlatformAccount & { metrics_reason?: string }>(
    `/accounts/${accountId}/check`,
  );
  return response.data;
}

/**
 * 同步账号级互动数据（粉丝 / 关注 / 获赞）。
 *
 * `_silent` 的语义是「这个失败的展示权归调用点」（先例 `fetchXhsSelfCapabilities`）：
 * 失败原因要显示在卡片里，而不是弹一个全局 toast。
 * 响应对象与 `/check` 同构（`serialize_account()` + `metrics_reason`），可整对象替换。
 */
export async function syncAccountMetrics(
  accountId: number,
): Promise<PlatformAccount & { metrics_reason?: string }> {
  const response = await http.post<PlatformAccount & { metrics_reason?: string }>(
    `/accounts/${accountId}/metrics`,
    {},
    { _silent: true } as never,
  );
  return response.data;
}

export async function deleteAccount(accountId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/accounts/${accountId}`);
  return response.data;
}

// ---------------------------------------------------------------------------
// M1.9 账号矩阵同步：笔记同步 / 店铺商品同步（设计源：设计 13 §4 + §6.3）
//
// 两个同步都是**异步任务**：触发端点立即返回任务摄要，真正干活在后台线程里
// （有界翻页 + 按账号限流 + 页间停顿）。所以每个动作都是「触发 + 轮询状态」一对。
//
// 字段名一律照抄后端 `services/sync_task_service.py` 的
// `task_summary()` / `status_payload()`，前端**不另起一套命名** ——
// 这套形状是触发端点与状态端点共用的，抄错一个字段不会报错，只会静默少显示。
// ---------------------------------------------------------------------------

/** 同步进度。`total` 是平台总数，**取不到时为 `null`**（渲染 `-`，不是 `0`）。 */
export interface XhsSyncProgress {
  stored: number;
  /**
   * 进度分母（设计 13 §35.18.6）：**只在「无界全量」时才有值** —— 增量 / 时间区间
   * 同步会早停，`stored` 追不上账号总量，后端**不写这个键** ⇒ `null` 表示
   * 「比例未知」，**不是**「0%」（缺值口径）。
   */
  total: number | null;
  /** §35.18：收尾「账号级详情补齐」阶段的分子 / 分母（只在进入该阶段后才有值）。 */
  detail_done?: number | null;
  detail_total?: number | null;
}

/** 触发端点与状态端点共用的部分（后端 `task_summary`）。 */
export interface XhsSyncTaskSummary {
  /** 无进行中任务时为 `null`（`status: "idle"`）—— 前端据此停轮询。 */
  task_id: number | null;
  task_type: string | null;
  status: string;
  progress: XhsSyncProgress | null;
  note: string;
  /**
   * §23：**实际生效的同步范围**（如「按时间区间同步：2026-09-01 ~ 2026-09-10」）。
   * 只在用户选了时间区间时出现 —— 区间没解析出来时这里是「已按全量同步」，
   * 用户必须知道，否则会以为只同步了一个时间段。
   */
  scope_note?: string;
  /**
   * §24.8：起止时间与耗时（后端算好，前端只格式化）。无任务时三个都是 `null`。
   * `duration_ms` = finished_at - started_at；任一端缺值 ⇒ `null`（不造 0）。
   */
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
}

/** 单个笔记源的同步结果（§19.3：后端任务 payload 的 `sources[]`）。 */
export interface XhsSyncSourceResult {
  /** `www` / `creator` / `ark`。 */
  source: string;
  stored: number;
  total: number | null;
  /** 该源的失败原因（成功为空串）。 */
  error: string;
  /**
   * 该源是否**被限流中断**（§19.11 真机教训：千帆显示「0 条」实际第 1 页前
   * 就被挡了）。`stored: 0` + `limited: true` = 没跑完，**不是**没数据。
   */
  limited?: boolean;
}

/** 最近一次已完成任务的结果（后端 `last_result`）。 */
export interface XhsSyncLastResult {
  /** 这一条对应的任务号（§35.17.4：账号卡「同步任务」区块要显示 `任务 #N`）。 */
  task_id?: number | null;
  status: string;
  progress: XhsSyncProgress | null;
  /** 本次顺带落库的挂载商品数（只有笔记同步会填）。 */
  goods_stored: number | null;
  note: string;
  error: string;
  finished_at: string | null;
  /** §24.8：起止时间与耗时（`latest_finished_task` 含 `cancelled` 终态）。 */
  started_at?: string | null;
  duration_ms?: number | null;
  /** 笔记同步的分源明细（§19；商品同步为 `[]`）。 */
  sources?: XhsSyncSourceResult[];
  /** 商品同步本轮补详情的商品数（内联补齐，不再有多轮队列）。 */
  detail_done?: number | null;
  /** 商品同步全店还剩多少商品没详情（增量同步可能只覆盖新商品；`null` = 不知道，显示 `-`）。 */
  detail_total?: number | null;
}

/** 状态端点响应（后端 `status_payload`）。 */
export interface XhsSyncTaskStatus extends XhsSyncTaskSummary {
  last_result: XhsSyncLastResult | null;
}

/**
 * 账号卡同步按钮的触发响应（设计 13 §35.17，2026-09-27 用户口径）。
 *
 * = **任务摄要**（与状态端点同一形状，前端不用记两套字段）+ 本次点击在
 * `scheduled_job_logs` 留下的行号。
 *
 * `log_id` 为 `null` = **本次没有记日志**：该账号已有任务在跑（`reused`），
 * 没有新执行就没有新日志 —— 同一个任务行记两次账会让任务中心的执行次数翻倍。
 */
export interface XhsSyncTriggerResult extends XhsSyncTaskSummary {
  log_id: number | null;
  /** `true` = 该账号已有任务在跑，返回的是**已有任务**的摄要，本次未触发新执行。 */
  reused?: boolean;
}

/**
 * 触发笔记同步（**所有账号**都用这个）。
 *
 * `profileUrl` 只有 **PC 账号**需要：`user_posted` 要的是 URL 级 `xsec_token`，
 * 而账号 Cookie 拿不到它（设计 13 §4.1 与 §6.1.1）—— PC 账号不传就必定
 * `XSEC_TOKEN_REQUIRED`。Creator 账号传空串即可。
 *
 * **语义（§35.17）**：手动触发定时任务 `system.note_sync` 的「执行一次」——
 * 任务行给账号卡轮询，`scheduled_job_logs` 给任务中心「任务日志」。
 *
 * **防重**：该账号已有未完成的同步任务 → 后端返回**同一个 `task_id`** 且 `reused: true`，
 * 不新建任务、不重复打平台，`log_id` 为 `null`。
 *
 * `_silent`：失败原因要显示在卡片里（`readErrorDetail`），不弹全局 toast。
 */
export async function syncAccountNotes(
  accountId: number,
  options: SyncNotesOptions = {}
): Promise<XhsSyncTriggerResult> {
  return postNoteSync(`${accountId}/notes/sync`, options);
}

/**
 * 触发**商品笔记**同步（**§44**，2026-09-30 用户拍板拆分）。
 *
 * **语义**：手动触发定时任务 `system.goods_note_sync` 的「执行一次」——
 * 走千帆 `goods-note/list` 的挂链笔记（落 `notes` + `note_goods`），
 * 与「同步笔记」（creator 列表）/「同步商品」（店铺商品台账）**都不是同一条链路**。
 *
 * 请求体与响应形状与 `syncAccountNotes` **逐字一致**（后端两条端点共用同一个
 * 建行 / 记日志实现）⇒ 请求体拼装走同一个 `postNoteSync`，不写第二份（DEF-044）。
 *
 * **防重**：该账号已有未完成的**商品笔记**任务 → 返回同一个 `task_id` 且
 * `reused: true`。⚠️ 与普通笔记**各自成立** —— 普通笔记在跑不阻塞本按钮。
 */
export async function syncGoodsNotes(
  accountId: number,
  options: SyncNotesOptions = {}
): Promise<XhsSyncTriggerResult> {
  return postNoteSync(`${accountId}/goods-notes/sync`, options);
}

/**
 * 笔记同步触发请求体的**唯一实现**（普通笔记 / 商品笔记共用，§44）。
 *
 * 两条端点（`…/notes/sync` 与 `…/goods-notes/sync`）的入参语义完全相同，差别只在路径
 * ⇒ 请求体必须只拼一次：写两份必然在其中一份上漂移（§35.15 就是漏了 `mode`）。
 */
async function postNoteSync(
  path: string,
  options: SyncNotesOptions
): Promise<XhsSyncTriggerResult> {
  const response = await http.post<XhsSyncTriggerResult>(
    `/accounts/${path}`,
    {
      // ⚠️ **不传 `max_pages` = 全量**（设计 13 §21，2026-09-22 用户口径：
      // 「不要设置翻页上限，应为全量同步」）。旧的 `?? 3` 正是「1961 条的账号
      // 永远只同步 180 条」的直接入口 —— 不要再写回默认值。
      // `maxPages` 只留给调试/测试显式小批量。
      ...(options.maxPages ? { max_pages: options.maxPages } : {}),
      // §23：用户在弹窗里选的同步范围。不传 = 后端默认 full（不设时间界）。
      ...(options.scope ? { scope: options.scope } : {}),
      // §35.15：**模式**要单独传 —— 弹窗选「全量同步」时必须带 `mode: "full"`，
      // 否则后端按游标判 auto=增量，第 1 页就早停（scope=full 管不到模式）。
      ...(options.mode ? { mode: options.mode } : {}),
      ...(options.startDate ? { start_date: options.startDate } : {}),
      ...(options.endDate ? { end_date: options.endDate } : {}),
    },
    { _silent: true } as never
  );
  return response.data;
}

/** 笔记同步的进度查询（前端每 5 秒轮询，设计 13 §6.2）。 */
export async function syncAccountNotesStatus(accountId: number): Promise<XhsSyncTaskStatus> {
  const response = await http.get<XhsSyncTaskStatus>(`/accounts/${accountId}/notes/sync/status`, {
    _silent: true,
  } as never);
  return response.data;
}

/**
 * **商品笔记**同步的进度查询（§44，前端每 5 秒轮询）。
 *
 * 与 `syncAccountNotesStatus` 逐字同构，只换路径。⚠️ 两条链路的状态**互相独立**
 * （后端各有一条任务类型与一条定时任务日志流）⇒ 普通笔记在跑不会影响本按钮的
 * 「同步中」态，反之亦然。
 */
export async function syncGoodsNotesStatus(accountId: number): Promise<XhsSyncTaskStatus> {
  const response = await http.get<XhsSyncTaskStatus>(
    `/accounts/${accountId}/goods-notes/sync/status`,
    { _silent: true } as never
  );
  return response.data;
}

export async function createXhsPcQrLoginSession(payload?: {
  sync_creator?: boolean;
  /** 续期目标账号（设计 13 §18）：把新令牌写进**这一个**账号，而不是按身份 upsert。 */
  target_account_id?: number;
}): Promise<XhsQrLoginSession> {
  const response = await http.post<XhsQrLoginSession>("/xhs/login-sessions/pc/qrcode", payload ?? {});
  return response.data;
}

export async function createXhsCreatorQrLoginSession(payload?: {
  target_account_id?: number;
}): Promise<XhsQrLoginSession> {
  const response = await http.post<XhsQrLoginSession>(
    "/xhs/login-sessions/creator/qrcode",
    payload ?? {}
  );
  return response.data;
}

/**
 * 千帆扫码续期 / 建号（2026-09-21；真机抓包验证可行）。
 *
 * 与创作者扫码同一套 customer 单点登录流程，service 指向
 * `https://ark.xiaohongshu.com` —— 扫码成功后种下的就是千帆域令牌。
 * `target_account_id` **可选**（2026-09-23，设计 13 §18.15.3）：带 = 续期
 * （写进这一个账号）；不带 = 绑定新账号（confirmed 时建 `sub_type=qianfan` 行）。
 */
export async function createXhsQianfanQrLoginSession(payload?: {
  target_account_id?: number;
}): Promise<XhsQrLoginSession> {
  const response = await http.post<XhsQrLoginSession>(
    "/xhs/login-sessions/qianfan/qrcode",
    payload ?? {}
  );
  return response.data;
}

/**
 * 千帆邮箱密码登录（2026-09-23，设计 13 §18.15.3；真机抓包 + 服务端实证）。
 *
 * **同步端点**：一次请求跑完登录全链（emailPwd → 换票 → ssologin → 选店），
 * 不需要轮询。**邮箱与密码不保留** —— 后端只用它们换令牌，不落库、不回显。
 *
 * `target_account_id`：带 = 续期（卡片上千帆 tag 点开）；不带 = 绑定新账号
 * （绑定账号弹窗的千帆 tab）。响应与扫码 confirmed 同构（`account` /
 * `renewed_channel` / `renewed_shop`），两种入口的后续处理一致。
 */
export async function loginXhsQianfanByEmail(payload: {
  email: string;
  pwd: string;
  target_account_id?: number;
}): Promise<XhsQrLoginSession> {
  const response = await http.post<XhsQrLoginSession>(
    "/xhs/login-sessions/qianfan/email-login",
    payload
  );
  return response.data;
}

/**
 * 账号维度的登录态（设计 13 §18.5）。
 *
 * `probe=false` 只回 L1/L2（**零平台请求**）；`probe=true` 会对已确认登录态的通道
 * 各发一次**只读探针**回答「还能不能用」。`_silent` 让失败呈现权归调用点 ——
 * 卡片上的状态行自己会说明情况，全局 toast 再弹一次只会把「可自救」说成「报错」。
 */
export async function fetchXhsAccountLoginState(
  accountId: number,
  options?: { probe?: boolean }
): Promise<XhsAccountLoginState> {
  const response = await http.post<XhsAccountLoginState>(
    `/accounts/${accountId}/login-state`,
    { probe: Boolean(options?.probe) },
    { _silent: true } as never
  );
  return response.data;
}

/**
 * **一批账号**的登录态（账号矩阵页「进入/刷新自动检查」用，v1.30）。
 *
 * 为什么要有批量版：自动检查要对每张卡片的各平台探活，逐账号调单条端点会发 N 次
 * 请求，而答案本来就在同一次查询里。一个入口一次问完，前端也只发一个请求。
 * 同样 `_silent`：没拿到结论就保持列表自带的 L1/L2，**不编造状态**
 * （§18.5 红线：`unknown` 不得渲染成「已失效」）。
 */
export async function fetchXhsAccountLoginStates(options?: {
  probe?: boolean;
}): Promise<{ probe: boolean; accounts: XhsAccountLoginState[] }> {
  const response = await http.post<{ probe: boolean; accounts: XhsAccountLoginState[] }>(
    "/accounts/login-states",
    { probe: Boolean(options?.probe) },
    { _silent: true } as never
  );
  return response.data;
}

/**
 * 续期千帆（ark 平台）令牌：粘贴 → 后端**只读校验** → 落库（v1.30）。
 *
 * ⚠️ 千帆没有扫码口（`ssologin` 要 `X-s`/`X-t` 签名，复现 = 登录逆向，违反只读
 * 边界 / C4）—— 所以这是「一步粘贴」而不是二维码。校验失败时后端把平台原话放回
 * `detail`，这里**原样抛出**让弹窗显示，不吞掉（吞掉用户就不知道错在哪）。
 */
export async function renewXhsQianfanCookie(
  accountId: number,
  cookieString: string
): Promise<PlatformAccount> {
  const response = await http.post<PlatformAccount>(
    `/accounts/${accountId}/qianfan-cookie`,
    { cookie_string: cookieString }
  );
  return response.data;
}

export async function pollXhsLoginSession(
  sessionId: number,
  options?: { sync_creator?: boolean }
): Promise<XhsQrLoginSession> {
  const response = await http.get<XhsQrLoginSession>(`/xhs/login-sessions/${sessionId}`, {
    params:
      options?.sync_creator === undefined ? undefined : { sync_creator: options.sync_creator }
  });
  return response.data;
}

export async function sendXhsPhoneCode(payload: {
  sub_type: "pc" | "creator";
  phone: string;
  sync_creator?: boolean;
  /** 续期目标账号（设计 13 §18.16）：把验证码登录拿到的新令牌写进**这一个**账号。 */
  target_account_id?: number;
}): Promise<{ session_id: number; status: string; message: string }> {
  const response = await http.post<{ session_id: number; status: string; message: string }>(
    `/xhs/login-sessions/${payload.sub_type}/phone/send-code`,
    { phone: payload.phone, sync_creator: payload.sync_creator, target_account_id: payload.target_account_id }
  );
  return response.data;
}

export async function confirmXhsPhoneLogin(payload: {
  sub_type: "pc" | "creator";
  session_id: number;
  phone: string;
  code: string;
  sync_creator?: boolean;
  /** 续期目标账号（§18.16）；缺省时后端以发码时记下的目标兜底。 */
  target_account_id?: number;
}): Promise<XhsQrLoginSession> {
  const response = await http.post<XhsQrLoginSession>(`/xhs/login-sessions/${payload.sub_type}/phone/confirm`, {
    session_id: payload.session_id,
    phone: payload.phone,
    code: payload.code,
    sync_creator: payload.sync_creator,
    target_account_id: payload.target_account_id
  });
  return response.data;
}

export async function fetchNotifications(params?: { unread?: boolean; page?: number; page_size?: number }): Promise<Paginated<AppNotification>> {
  const response = await http.get<Paginated<AppNotification>>("/notifications", { params });
  return response.data;
}

// ── 系统更新（设计 36，2026-10-13）───────────────────────────────────────
export async function fetchAppVersion(): Promise<SystemVersionInfo> {
  const response = await http.get<SystemVersionInfo>("/system/version");
  return response.data;
}

export async function checkSystemUpdate(): Promise<SystemUpdateInfo> {
  const response = await http.get<SystemUpdateInfo>("/system/update/check");
  return response.data;
}

export async function downloadSystemUpdate(payload: { url: string; version: string }): Promise<SystemUpdateDownloadResult> {
  const response = await http.post<SystemUpdateDownloadResult>("/system/update/download", payload);
  return response.data;
}

export async function installSystemUpdate(path: string): Promise<SystemUpdateInstallResult> {
  const response = await http.post<SystemUpdateInstallResult>("/system/update/install", { path });
  return response.data;
}

export async function fetchNotificationUnreadCount(): Promise<{ unread: number }> {
  const response = await http.get<{ unread: number }>("/notifications/unread-count");
  return response.data;
}

export async function markNotificationRead(id: number): Promise<AppNotification> {
  const response = await http.post<AppNotification>(`/notifications/${id}/read`);
  return response.data;
}

export async function markAllNotificationsRead(): Promise<{ marked: number }> {
  const response = await http.post<{ marked: number }>("/notifications/read-all");
  return response.data;
}

// Auto Tasks (Auto Operations)

export async function fetchAutoTasks(): Promise<Paginated<AutoTask>> {
  const response = await http.get<Paginated<AutoTask>>("/auto-tasks");
  return response.data;
}

export async function createAutoTask(payload: AutoTaskCreatePayload): Promise<AutoTask> {
  const response = await http.post<AutoTask>("/auto-tasks", payload);
  return response.data;
}

export async function updateAutoTask(taskId: number, payload: AutoTaskUpdatePayload): Promise<AutoTask> {
  const response = await http.patch<AutoTask>(`/auto-tasks/${taskId}`, payload);
  return response.data;
}

export async function deleteAutoTask(taskId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/auto-tasks/${taskId}`);
  return response.data;
}

export async function runAutoTask(taskId: number): Promise<AutoTaskRunResult> {
  const response = await http.post<AutoTaskRunResult>(`/auto-tasks/${taskId}/run`);
  return response.data;
}

// ---------------------------------------------------------------------------
// 运行时配置中心（设计 10 §7，2026-09-27）：设置页的列表 / 增删改查
// ---------------------------------------------------------------------------
// ⚠️ key 一律 `encodeURIComponent`：配置键可能含 `.` / `:` 等字符，
// 不编码会拼出会被路径解析吃掉的 URL（`/settings/a/b` 变成两段路径）。
export async function fetchSystemSettings(category?: string): Promise<SystemSettingListResponse> {
  const response = await http.get<SystemSettingListResponse>("/settings", {
    params: category ? { category } : {},
  });
  return response.data;
}

export async function createSystemSetting(
  payload: SystemSettingCreatePayload
): Promise<SystemSetting> {
  const response = await http.post<SystemSetting>("/settings", payload);
  return response.data;
}

export async function updateSystemSetting(
  key: string,
  payload: SystemSettingUpdatePayload
): Promise<SystemSetting> {
  const response = await http.put<SystemSetting>(`/settings/${encodeURIComponent(key)}`, payload);
  return response.data;
}

export async function deleteSystemSetting(key: string): Promise<SystemSettingDeleteResult> {
  const response = await http.delete<SystemSettingDeleteResult>(
    `/settings/${encodeURIComponent(key)}`
  );
  return response.data;
}

// ─────────────────────────────────────────────────────────────
// LLM 网关令牌映射 + 预警规则 + 用量展示（设计 17 §7.1a/§7.1b/§7.5，v1.6）
// ─────────────────────────────────────────────────────────────

// ⚠️ ~~令牌映射 4 个请求函数~~ **v1.9 整组移除**（用户口径「令牌映射页面移除」）：
// `fetchGatewayTokens` / `createGatewayToken` / `updateGatewayToken` / `deleteGatewayToken`
// 随 `llm_gateway_tokens` 表与 4 个端点一起退场（设计 17 §7.1a）。

/** 「使用平台」枚举（v1.9 D3：**后端下发**，前端不许就地写死选项）
 *  ⚠️ 路径是 `/llm/providers/platforms`（**独立前缀**，不在 `gateway-configs` 下）——
 *  枚举是「服务商」维度的公共字典，与某条具体配置无关。 */
export async function fetchGatewayPlatforms(): Promise<{ items: LlmGatewayPlatformOption[] }> {
  const response = await http.get<{ items: LlmGatewayPlatformOption[] }>("/llm/providers/platforms");
  return response.data;
}

/**
 * **未保存**时拉取服务商模型目录（v1.9）—— 供弹窗里的「默认模型」下拉。
 *
 * ⚠️ 传**地址 + 令牌**而不是 `id`：新增时还没有 id；编辑时用户可能改了地址或重填令牌，
 * 必须按表单当前值拉。编辑态令牌留空（= 不修改）时传 `id` 让后端用库里令牌兜底。
 * ⚠️ 后端**不落库**（预览语义），目录落库发生在保存 / 测试。
 */
export async function previewGatewayModels(payload: {
  base_url: string;
  access_token?: string;
  id?: number;
}): Promise<{ models: string[]; message: string }> {
  const response = await http.post<{ models: string[]; message: string }>(
    "/llm/gateway-configs/models-preview",
    payload
  );
  return response.data;
}

/**
 * 预警规则列表。
 *
 * ⚠️ `gateway_id` 是**可选过滤**：不传 ⇒ 返回全部规则（含 `gateway_id=null` 的**全网关**规则）。
 * 传了 ⇒ 后端只回 `gateway_id == X` 的行，**全网关规则会被滤掉**。
 * 预警页因此**不按网关过滤**，改为全量拉取后在前端分组展示（否则全网关规则永远看不见）。
 */
export async function fetchGatewayAlerts(params?: {
  gateway_id?: number;
  enabled?: boolean;
}): Promise<Paginated<LlmGatewayAlert>> {
  const response = await http.get<Paginated<LlmGatewayAlert>>("/llm/gateway-alerts", {
    params: { page: 1, page_size: 100, ...params },
  });
  return response.data;
}

export async function createGatewayAlert(payload: LlmGatewayAlertPayload): Promise<LlmGatewayAlert> {
  const response = await http.post<LlmGatewayAlert>("/llm/gateway-alerts", payload);
  return response.data;
}

export async function updateGatewayAlert(
  alertId: number,
  payload: Partial<Pick<LlmGatewayAlert, "threshold" | "operator" | "window_seconds" | "level" | "enabled">>
): Promise<LlmGatewayAlert> {
  const response = await http.patch<LlmGatewayAlert>(`/llm/gateway-alerts/${alertId}`, payload);
  return response.data;
}

export async function deleteGatewayAlert(alertId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/llm/gateway-alerts/${alertId}`);
  return response.data;
}

export async function fetchGatewayUsageOverview(params?: {
  gateway_id?: number;
  platform?: string;
  function?: string;
  model_name?: string;
}): Promise<LlmGatewayUsageOverview> {
  const response = await http.get<LlmGatewayUsageOverview>("/llm/usage/overview", { params });
  return response.data;
}

export async function fetchGatewayUsageDaily(params?: {
  gateway_id?: number;
  platform?: string;
  function?: string;
  model_name?: string;
  start_date?: string;
  end_date?: string;
}): Promise<LlmGatewayUsageDailyItem[]> {
  const response = await http.get<LlmGatewayUsageDailyItem[]>("/llm/usage/daily", { params });
  return response.data;
}

export async function fetchGatewayUsageBreakdown(params?: {
  gateway_id?: number;
  model_name?: string;
  dimension: string;
  start_date?: string;
  end_date?: string;
}): Promise<LlmGatewayUsageBreakdownItem[]> {
  const response = await http.get<LlmGatewayUsageBreakdownItem[]>("/llm/usage/breakdown", { params });
  return response.data;
}

// ─────────────────────── T9：提示词 / 素材库 / LLM 生成（设计 17 §7.2–§7.4） ───────────────────────

/** 提示词模块列表（`GET /api/xhs/llm/prompt-modules`；**分页形状**，非裸数组）。 */
export async function fetchPromptModules(params: {
  category?: string;
  page?: number;
  pageSize?: number;
} = {}): Promise<Paginated<PromptModule>> {
  const response = await http.get<Paginated<PromptModule>>("/xhs/llm/prompt-modules", {
    params: {
      ...(params.category ? { category: params.category } : {}),
      ...(params.page ? { page: params.page } : {}),
      ...(params.pageSize ? { page_size: params.pageSize } : {}),
    },
  });
  return response.data;
}

/** 新建提示词模块。 */
export async function createPromptModule(payload: {
  name: string;
  category?: string;
  content: string;
  sort_order?: number;
  enabled?: boolean;
}): Promise<PromptModule> {
  const response = await http.post<PromptModule>("/xhs/llm/prompt-modules", payload);
  return response.data;
}

/** 修改提示词模块（局部更新）。 */
export async function updatePromptModule(
  moduleId: number,
  payload: Partial<{
    name: string;
    category: string;
    content: string;
    sort_order: number;
    enabled: boolean;
  }>
): Promise<PromptModule> {
  const response = await http.patch<PromptModule>(`/xhs/llm/prompt-modules/${moduleId}`, payload);
  return response.data;
}

/** 删除提示词模块。 */
export async function deletePromptModule(moduleId: number): Promise<void> {
  await http.delete(`/xhs/llm/prompt-modules/${moduleId}`);
}

/** 提示词模板列表（`GET /api/xhs/llm/prompt-templates`；**分页形状**，`modules` 已展开）。 */
export async function fetchPromptTemplates(params: {
  taskType?: string;
  page?: number;
  pageSize?: number;
} = {}): Promise<Paginated<PromptTemplate>> {
  const response = await http.get<Paginated<PromptTemplate>>("/xhs/llm/prompt-templates", {
    params: {
      ...(params.taskType ? { task_type: params.taskType } : {}),
      ...(params.page ? { page: params.page } : {}),
      ...(params.pageSize ? { page_size: params.pageSize } : {}),
    },
  });
  return response.data;
}

/** 新建提示词模板。 */
export async function createPromptTemplate(payload: {
  name: string;
  task_type: string;
  prompt_skeleton: string;
  module_ids?: number[];
  is_default?: boolean;
}): Promise<PromptTemplate> {
  const response = await http.post<PromptTemplate>("/xhs/llm/prompt-templates", payload);
  return response.data;
}

/** 修改提示词模板（局部更新）。 */
export async function updatePromptTemplate(
  templateId: number,
  payload: Partial<{
    name: string;
    task_type: string;
    prompt_skeleton: string;
    module_ids: number[];
    is_default: boolean;
  }>
): Promise<PromptTemplate> {
  const response = await http.patch<PromptTemplate>(
    `/xhs/llm/prompt-templates/${templateId}`,
    payload
  );
  return response.data;
}

/** 删除提示词模板。 */
export async function deletePromptTemplate(templateId: number): Promise<void> {
  await http.delete(`/xhs/llm/prompt-templates/${templateId}`);
}

/**
 * 渲染预览（`POST /api/xhs/llm/prompt-templates/render-preview`）。
 *
 * 拿一个真实商品跑一遍插槽填充，让用户在保存模板前就看到最终提示词 ——
 * 而不是等生成失败了才知道插槽写错了。`shopProductId` 可为空（只渲骨架）。
 */
export async function previewPromptTemplate(
  templateId: number,
  shopProductId?: number
): Promise<{ rendered: string; missing_slots?: string[] }> {
  const response = await http.post<{ rendered: string; missing_slots?: string[] }>(
    "/xhs/llm/prompt-templates/render-preview",
    { template_id: templateId, ...(shopProductId ? { shop_product_id: shopProductId } : {}) }
  );
  return response.data;
}

/** 素材文件夹列表（`GET /api/xhs/materials/folders`）。 */
export async function fetchMaterialFolders(): Promise<MaterialFolder[]> {
  const response = await http.get<MaterialFolder[]>("/xhs/materials/folders");
  return response.data;
}

/** 新建素材文件夹（后端 202：扫描是后台线程，返回时文件数可能还是 0）。 */
export async function createMaterialFolder(payload: {
  name: string;
  path: string;
}): Promise<MaterialFolder> {
  const response = await http.post<MaterialFolder>("/xhs/materials/folders", payload);
  return response.data;
}

/** 重命名 / 启停素材文件夹。 */
export async function updateMaterialFolder(
  folderId: number,
  payload: Partial<{ name: string; enabled: boolean }>
): Promise<MaterialFolder> {
  const response = await http.patch<MaterialFolder>(`/xhs/materials/folders/${folderId}`, payload);
  return response.data;
}

/** 删除素材文件夹（**只删索引行，不物理删磁盘文件** —— 只读边界）。 */
export async function deleteMaterialFolder(folderId: number): Promise<{ removed_files: number }> {
  const response = await http.delete<{ removed_files: number }>(
    `/xhs/materials/folders/${folderId}`
  );
  return response.data;
}

/** 触发重扫（后端 202：扫描在后台跑，需轮询文件夹列表看 `last_scan_at`）。 */
export async function scanMaterialFolder(folderId: number): Promise<{ first_scan: boolean }> {
  const response = await http.post<{ first_scan: boolean }>(
    `/xhs/materials/folders/${folderId}/scan`
  );
  return response.data;
}

/** 素材文件列表 + 搜索（`GET /api/xhs/materials/files`）。 */
export async function fetchMaterialFiles(
  params: {
    folderId?: number;
    keyword?: string;
    fileType?: "image" | "video";
    sort?: "mtime" | "filename" | "size";
    order?: "asc" | "desc";
    page?: number;
    pageSize?: number;
  } = {}
): Promise<Paginated<MaterialFile>> {
  const response = await http.get<Paginated<MaterialFile>>("/xhs/materials/files", {
    params: {
      ...(params.folderId ? { folder_id: params.folderId } : {}),
      ...(params.keyword ? { keyword: params.keyword } : {}),
      ...(params.fileType ? { file_type: params.fileType } : {}),
      ...(params.sort ? { sort: params.sort } : {}),
      ...(params.order ? { order: params.order } : {}),
      ...(params.page ? { page: params.page } : {}),
      ...(params.pageSize ? { page_size: params.pageSize } : {}),
    },
  });
  return response.data;
}

/**
 * 素材文件的原始内容 URL（`<img src>` / 预览用，**不经 axios**）。
 *
 * ⚠️ 故意返回 URL 字符串而不是发请求：浏览器要直接 `<img src>` 加载，
 * 走 axios 会把二进制读进内存再转 blob URL（还要管回收）。
 * 鉴权由 cookie 承担（与页面同源）。
 */
export function materialFileUrl(fileId: number): string {
  return `/api/xhs/materials/files/${fileId}/file`;
}

/**
 * 删除一个素材文件（**真删源文件，但进回收站**，v2.31）。
 *
 * ⚠️ 与「删除文件夹」语义**不同**：那个只删索引（源文件不动），这个会动用户磁盘。
 * 所以前端**必须二次确认**，且确认文案要写清「移入回收站」（可捞回）。
 */
export async function deleteMaterialFile(fileId: number): Promise<void> {
  await http.delete(`/xhs/materials/files/${fileId}`);
}

/**
 * 批量删除素材文件（v2.31，**一次上限 50**）。
 *
 * ⚠️ 后端超限**整体拒绝**（不部分执行）⇒ 这里如实把后端的错误码透出去，
 * 让调用方显示原因（不吞成「删除失败」这种查不下去的话）。
 */
export async function deleteMaterialFilesBatch(fileIds: number[]): Promise<{
  deleted: number;
  skipped: number[];
}> {
  const response = await http.post<{ deleted: number; skipped: number[] }>(
    "/xhs/materials/files/delete-batch",
    { file_ids: fileIds }
  );
  return response.data;
}

/**
 * 上传一个文件进素材文件夹（v2.31）。
 *
 * ⚠️ 走 `FormData`（**不手动设 Content-Type**）：`multipart/form-data` 的
 * `boundary` 必须由浏览器生成，手设会让后端解析失败。
 */
export async function uploadMaterialFile(
  folderId: number,
  file: File
): Promise<MaterialFile & { path: string }> {
  const form = new FormData();
  form.append("folder_id", String(folderId));
  form.append("file", file);
  const response = await http.post<MaterialFile & { path: string }>(
    "/xhs/materials/files/upload",
    form
  );
  return response.data;
}

/**
 * 素材**缩略图** URL（v2.29 —— 修「素材库图片不显示」）。
 *
 * ## 为什么要单独一个函数
 *
 * 后端 `material_scan_service._thumb_rel_url()` 已经算好并落库了 `thumb_url`
 * （形如 `/api/xhs/materials/folders/1/thumbs/1`），但**前端此前根本没用它** ——
 * 两个分支都拿 `materialFileUrl(file.id)`（**原图端点**）当 `src`。
 * 原图实测 1776×2368 / **6.5 MB** ⇒ 加载慢、易被掐 ⇒ 页面显示破图
 * （用户现象「图没有在页面正常展示，无法加载该图片」）。
 *
 * 这里直接吃后端下发的 `thumb_url`（**不自己拼**：拼装规则在后端一处，
 * 前端再拼一份就是两个实现，改一处必漏一处）。
 */
export function materialThumbUrl(thumbUrl: string): string {
  return thumbUrl;
}

/**
 * **带鉴权取素材图**，返回 `blob:` URL（v2.29.2 —— 修「素材库图片不显示」的**真根因**）。
 *
 * ## 为什么不能用 `<img src="/api/...">`
 *
 * 后端 `get_current_user` 只认 **`Authorization: Bearer …`**
 * （`core/deps.py` 的 `OAuth2PasswordBearer`，**没有 cookie 鉴权**）。
 * 而浏览器加载 `<img src>` **发不出自定义请求头** ⇒ 那个请求必然 **401** ⇒ 破图。
 *
 * ⚠️ 这与「缩略图端点不存在」是**两个独立的 bug**：补了端点之后，
 * `<img src>` 仍然取不到图 —— 用户反馈「素材库中图依旧未展示」正是这一层。
 *
 * 范式与 `downloadExportFile` 同源：走 axios（**拦截器自动带 token**）
 * → `responseType: "blob"` → `createObjectURL`。
 *
 * ⚠️ **调用方负责 `revokeObjectURL`**（否则 blob 常驻内存；列表翻页会累积）。
 * 见 `material-library-panel.tsx` 的 `useEffect` 清理。
 */
export async function fetchMaterialImageObjectUrl(url: string): Promise<string> {
  const endpoint = url.startsWith("/api") ? url.slice(4) : url;
  const response = await http.get<Blob>(endpoint, { responseType: "blob" });
  return window.URL.createObjectURL(response.data);
}

/** 单条 LLM 生成（入队即返回，设计 17 §7.4）。 */
export async function generateLlmNote(payload: {
  platformAccountId: number;
  itemId: string;
  templateId: number;
  gatewayConfigId?: number;
  modelName?: string;
  mediaSource?: LlmMediaSource;
  mediaOptions?: LlmMediaOptions;
}): Promise<{ ok: boolean; status: string; item_id: string; already_queued?: boolean }> {
  const response = await http.post<{
    ok: boolean;
    status: string;
    item_id: string;
    already_queued?: boolean;
  }>(
    "/xhs/product-notes/generate-llm",
    {
      platform_account_id: payload.platformAccountId,
      item_id: payload.itemId,
      template_id: payload.templateId,
      ...(payload.gatewayConfigId ? { gateway_config_id: payload.gatewayConfigId } : {}),
      ...(payload.modelName ? { model_name: payload.modelName } : {}),
      ...(payload.mediaSource ? { media_source: payload.mediaSource } : {}),
      ...(payload.mediaOptions ? { media_options: payload.mediaOptions } : {}),
    },
    { _silent: true } as never
  );
  return response.data;
}

/** 批量 LLM 生成（入队即返回；单项失败不阻断）。 */
export async function batchGenerateLlmNotes(payload: {
  platformAccountId: number;
  itemIds: readonly string[];
  templateId: number;
  gatewayConfigId?: number;
  modelName?: string;
  mediaSource?: LlmMediaSource;
  mediaOptions?: LlmMediaOptions;
  maxItems?: number;
}): Promise<{ ok: boolean; queued: number; skipped?: number }> {
  const response = await http.post<{ ok: boolean; queued: number; skipped?: number }>(
    "/xhs/product-notes/batch-generate-llm",
    {
      platform_account_id: payload.platformAccountId,
      item_ids: [...payload.itemIds],
      template_id: payload.templateId,
      ...(payload.gatewayConfigId ? { gateway_config_id: payload.gatewayConfigId } : {}),
      ...(payload.modelName ? { model_name: payload.modelName } : {}),
      ...(payload.mediaSource ? { media_source: payload.mediaSource } : {}),
      ...(payload.mediaOptions ? { media_options: payload.mediaOptions } : {}),
      ...(payload.maxItems ? { max_items: payload.maxItems } : {}),
    },
    { _silent: true } as never
  );
  return response.data;
}

/** LLM 生成队列快照（5s 轮询；键名 `generated`）。 */
export async function fetchLlmGenerateQueue(): Promise<LlmGenerateQueueSnapshot> {
  const response = await http.get<LlmGenerateQueueSnapshot>(
    "/xhs/product-notes/generate-llm-queue",
    { _silent: true } as never
  );
  return response.data;
}

/** 重试一条失败的生成记录。 */
export async function retryLlmGenerate(recordId: number): Promise<{ ok: boolean }> {
  const response = await http.post<{ ok: boolean }>(
    `/xhs/product-notes/generate-llm/${recordId}/retry`
  );
  return response.data;
}

/**
 * 把生成结果保存到素材库（v2.29 §13.3.10）。
 *
 * 用户口径：「**支持选择性保存**到指定素材库，支持**全部保存**到指定素材库」
 * ⇒ 一个端点两种语义（`asset_ids` 给几张就存几张），不另开 `save-all`
 * （那是同一件事的两份实现，且「全部」的定义在前端手里）。
 *
 * `asset_groups` = `{"<asset_id>": "product"|"detail"}` —— **必须传**：
 * 分组事实只在生成响应里，后端反查不到（不传就只能写「图-NN」）。
 */
export async function saveDetailImages(payload: {
  folder_id: number;
  asset_ids: number[];
  asset_groups?: Record<string, string>;
}): Promise<SaveDetailImagesResult> {
  const response = await http.post<SaveDetailImagesResult>(
    "/xhs/qianfan/save-detail-images",
    payload
  );
  return response.data;
}

/**
 * 某商品的 **AI 生成记录**（v2.30 §13.3.11）。
 *
 * 用户口径：「进入素材库后无法再区分与产品的关系」⇒ 给生成结果建立**商品关联**，
 * 在商品列表操作列提供查看入口。
 *
 * ⚠️ 只返回**有关联**的（老图 `item_id` 为空，不出现）—— 不猜、不回溯。
 */
export async function fetchItemGenerations(payload: {
  platform_account_id: number;
  item_id: string;
  page?: number;
  page_size?: number;
}): Promise<{
  items: {
    id: number;
    asset_type: string;
    url: string;
    local_path: string;
    prompt: string;
    model_name: string;
    created_at: string;
  }[];
  total: number;
  page: number;
  page_size: number;
}> {
  const response = await http.post("/xhs/qianfan/item-generations", payload);
  return response.data;
}

// ---------------------------------------------------------------------------
// 订单二期写操作（设计 16 §7.4，M7）—— **真实平台写、不可撤回**
//
// ⚠️ 两层确认：`confirmed` + `confirm_token`（预览接口下发的内容指纹）。
// 前端**必须先调 preview** 拿 token，用户确认后原样回传 —— 不自己算 token。
// ---------------------------------------------------------------------------

export interface OrderWritePreviewParams {
  platformAccountId: number;
  kind: "ship" | "ship_batch" | "after_sale";
  orderNo?: string;
  items?: Array<{ order_no: string; company_code?: string; tracking_no?: string }>;
  afterSaleId?: string;
  action?: "agree" | "reject";
  rejectReason?: string;
  companyCode?: string;
  trackingNo?: string;
}

export interface OrderWritePreviewResult {
  platform_account_id: number;
  shop_name: string;
  kind: string;
  count: number;
  /** 二次确认凭据 —— 原样回传给写端点。 */
  confirm_token: string;
  items: Array<Record<string, unknown>>;
}

/** 写操作预览（**纯本地、零平台请求**）：拿影响面 + 二次确认凭据。 */
export async function previewOrderWrite(
  params: OrderWritePreviewParams,
): Promise<OrderWritePreviewResult> {
  const response = await http.post<OrderWritePreviewResult>("/xhs/qianfan/orders/ship/preview", {
    platform_account_id: params.platformAccountId,
    kind: params.kind,
    order_no: params.orderNo ?? "",
    items: params.items ?? [],
    after_sale_id: params.afterSaleId ?? "",
    action: params.action ?? "",
    reject_reason: params.rejectReason ?? "",
    company_code: params.companyCode ?? "",
    tracking_no: params.trackingNo ?? "",
  });
  return response.data;
}

/** **单笔发货**（真实写、不可撤回）。 */
export async function shipOrder(payload: {
  platformAccountId: number;
  orderNo: string;
  companyCode: string;
  trackingNo: string;
  confirmToken: string;
}): Promise<Record<string, unknown>> {
  const response = await http.post<{ result: Record<string, unknown> }>(
    "/xhs/qianfan/orders/ship",
    {
      platform_account_id: payload.platformAccountId,
      order_no: payload.orderNo,
      company_code: payload.companyCode,
      tracking_no: payload.trackingNo,
      confirmed: true,
      confirm_token: payload.confirmToken,
    },
  );
  return response.data.result;
}

export interface OrderBatchShipResult {
  requested: number;
  succeeded: Array<{ order_no: string; tracking_no?: string }>;
  failed: Array<{ order_no: string; code: string; message: string; current_status?: string }>;
  succeeded_count: number;
  failed_count: number;
}

/** **批量发货**（真实写、不可撤回；回执分层）。 */
export async function shipOrdersBatch(payload: {
  platformAccountId: number;
  items: Array<{ order_no: string; company_code: string; tracking_no: string }>;
  confirmToken: string;
}): Promise<OrderBatchShipResult> {
  const response = await http.post<OrderBatchShipResult>("/xhs/qianfan/orders/ship-batch", {
    platform_account_id: payload.platformAccountId,
    items: payload.items,
    confirmed: true,
    confirm_token: payload.confirmToken,
  });
  return response.data;
}

/** **售后审核**（同意 / 拒绝；真实写、不可撤回）。 */
export async function handleAfterSale(payload: {
  platformAccountId: number;
  afterSaleId: string;
  action: "agree" | "reject";
  rejectReason?: string;
  confirmToken: string;
}): Promise<Record<string, unknown>> {
  const response = await http.post<{ result: Record<string, unknown> }>(
    "/xhs/qianfan/after-sales/handle",
    {
      platform_account_id: payload.platformAccountId,
      after_sale_id: payload.afterSaleId,
      action: payload.action,
      reject_reason: payload.rejectReason ?? "",
      confirmed: true,
      confirm_token: payload.confirmToken,
    },
  );
  return response.data.result;
}

// ---------------------------------------------------------------------------
// 订单模块（设计 16 §7，M6 · 一期只读）
//
// ⚠️ **一期只读**：这里没有任何「发货 / 改价 / 售后审核」的写接口 ——
// 平台的写接口未授权（硬约束②）。**备注是本地写**（`order_remarks` 表），
// 不同步到平台，页面必须如实告知用户。
// ---------------------------------------------------------------------------

/** 订单状态（与后端 `models/order.py` 的 ORDER_STATUSES 逐值一致）。
 *
 * ⚠️ 是 `待收货` 不是 `已发货` —— 平台语义是「已发货待收货」。
 */
export type OrderStatus =
  | "待付款"
  | "待发货"
  | "待收货"
  | "已完成"
  | "售后中"
  | "已关闭"
  | "";

/** 全部订单状态（筛选器用它；**顺序即页面展示顺序**）。 */
export const ORDER_STATUSES: OrderStatus[] = [
  "待付款",
  "待发货",
  "待收货",
  "已完成",
  "售后中",
  "已关闭",
];

export interface OrderItemRow {
  id: number;
  item_id?: string | null;
  /** 已匹配到的本地商品 id（千帆商品不在台账时为 null）。 */
  shop_product_id?: number | null;
  item_name: string;
  item_image?: string | null;
  sku?: string | null;
  quantity: number;
  unit_price?: number | null;
  amount?: number | null;
  refund_status?: string | null;
}

export interface OrderShipmentRow {
  id: number;
  package_id?: string | null;
  logistics_company?: string | null;
  tracking_no?: string | null;
  ship_time?: string | null;
  sign_time?: string | null;
  warehouse_desc?: string | null;
}

export interface OrderAfterSaleRow {
  id: number;
  after_sale_id?: string | null;
  type?: string | null;
  reason?: string | null;
  status?: string | null;
  amount?: number | null;
  apply_time?: string | null;
  handle_time?: string | null;
  handle_result?: string | null;
}

export interface OrderRemarkRow {
  id: number;
  kind: string;
  content: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface OrderRow {
  id: number;
  order_no: string;
  package_id?: string | null;
  platform_account_id: number;
  type_desc?: string | null;
  buyer_nick: string;
  /** ⚠️ 默认**脱敏**（`张*` / `189****2857`）；`includeFull` 才给库里原值。 */
  receiver_name?: string | null;
  receiver_phone?: string | null;
  receiver_address?: string | null;
  receiver_province?: string | null;
  receiver_city?: string | null;
  status: OrderStatus | string;
  /** 平台原始状态码（**表外值原样显示、不造名**）。 */
  platform_status?: string | null;
  total_amount?: number | null;
  pay_amount?: number | null;
  freight?: number | null;
  discount?: number | null;
  pay_method?: string | null;
  order_source?: string | null;
  logistics_company?: string | null;
  tracking_no?: string | null;
  created_at?: string | null;
  pay_time?: string | null;
  ship_time?: string | null;
  finish_time?: string | null;
  promise_ship_time?: string | null;
  synced_at?: string | null;
  seller_mark?: string | null;
  items?: OrderItemRow[];
  shipments?: OrderShipmentRow[];
  after_sales?: OrderAfterSaleRow[];
  remarks?: OrderRemarkRow[];
}

export interface OrderListParams {
  platformAccountId: number;
  status?: OrderStatus | string;
  keyword?: string;
  dateFrom?: string;
  dateTo?: string;
  page?: number;
  pageSize?: number;
  /** true 才回完整收件人信息；**默认脱敏**。 */
  includeFull?: boolean;
}

export interface OrderListResult {
  platform_account_id: number;
  source: string;
  shop_name?: string | null;
  status: string;
  page: number;
  page_size: number;
  total: number;
  count: number;
  items: OrderRow[];
}

export async function fetchOrders(params: OrderListParams): Promise<OrderListResult> {
  const response = await http.post<OrderListResult>("/xhs/qianfan/orders", {
    platform_account_id: params.platformAccountId,
    status: params.status ?? "",
    keyword: params.keyword ?? "",
    date_from: params.dateFrom ?? null,
    date_to: params.dateTo ?? null,
    page: params.page ?? 1,
    page_size: params.pageSize ?? 20,
    include_full: params.includeFull ?? false,
  });
  return response.data;
}

export async function fetchOrderDetail(
  platformAccountId: number,
  orderNo: string,
  includeFull = false,
): Promise<OrderRow> {
  const response = await http.post<{ order: OrderRow }>("/xhs/qianfan/orders/detail", {
    platform_account_id: platformAccountId,
    order_no: orderNo,
    include_full: includeFull,
  });
  return response.data.order;
}

export async function syncOrders(platformAccountId: number): Promise<Record<string, unknown>> {
  const response = await http.post<Record<string, unknown>>("/xhs/qianfan/orders/sync", {
    platform_account_id: platformAccountId,
  });
  return response.data;
}

export interface OrderMetrics {
  order_count: number;
  gmv: number;
  avg_order_value?: number | null;
  paid_count: number;
  finished_count: number;
  refund_count: number;
  refund_rate?: number | null;
  avg_ship_hours?: number | null;
  /** ⚠️ 每个状态都有键（缺值 0），前端不用补样板。 */
  by_status: Record<string, number>;
}

export async function fetchOrderMetrics(platformAccountId: number): Promise<OrderMetrics> {
  const response = await http.get<{ metrics: OrderMetrics }>("/xhs/qianfan/orders/metrics", {
    params: { platform_account_id: platformAccountId },
  });
  return response.data.metrics;
}

/** ⚠️ **本地**备注（不碰平台）—— 空 `content` = 删除该备注。 */
export async function saveOrderRemark(
  platformAccountId: number,
  orderNo: string,
  content: string,
  kind = "订单",
): Promise<{ ok: boolean; deleted: boolean; remark: OrderRemarkRow | null }> {
  const response = await http.post<{
    ok: boolean;
    deleted: boolean;
    remark: OrderRemarkRow | null;
  }>("/xhs/qianfan/orders/remark", {
    platform_account_id: platformAccountId,
    order_no: orderNo,
    content,
    kind,
  });
  return response.data;
}

export interface OrderExportResult {
  platform_account_id: number;
  count: number;
  truncated: boolean;
  filename: string;
  /** ⚠️ 含**完整**收件人信息（履约口径）—— 下载前必须显式提示用户。 */
  csv: string;
}

export async function exportOrders(
  platformAccountId: number,
  params: Omit<OrderListParams, "platformAccountId" | "page" | "pageSize" | "includeFull"> = {},
  limit = 1000,
): Promise<OrderExportResult> {
  const response = await http.post<OrderExportResult>("/xhs/qianfan/orders/export", {
    platform_account_id: platformAccountId,
    status: params.status ?? "",
    keyword: params.keyword ?? "",
    date_from: params.dateFrom ?? null,
    date_to: params.dateTo ?? null,
    limit,
  });
  return response.data;
}
