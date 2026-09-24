import axios from "axios";
import { message } from "antd";

import { fallbackPlatforms } from "./platforms";
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
  BenchmarkCreateDraftsResponse,
  BenchmarkOverview,
  BatchCreateDraftsPayload,
  BatchCreateDraftsResponse,
  BatchTagNotesPayload,
  BatchTagNotesResponse,
  ComposeImagePayload,
  CreateDraftPayload,
  DashboardOverview,
  Draft,
  DescribeImagePayload,
  GenerateNotePayload,
  GenerateCoverPayload,
  GenerateImagePayload,
  GenerateImageResult,
  GeneratedImageAsset,
  GenerateTagsPayload,
  GenerateTitlePayload,
  ImageUtilityFile,
  KeywordGroup,
  KeywordGroupDetail,
  KeywordGroupPayload,
  ModelConfig,
  ModelConfigPayload,
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
  PublishAssetPayload,
  PublishJob,
  PublishJobUpdatePayload,
  PolishTextPayload,
  ResizeImagePayload,
  RewriteDraftPayload,
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
  Tag,
  UserImageFile,
  TagPayload,
  TaskRecord,
  XhsNoteSearchResponse,
  XhsDataCrawlItem,
  XhsDataCrawlPayload,
  XhsDataCrawlResponse,
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
  XhsQianfanShop,
  XhsShopItemDetail,
  XhsShopItemsQuery,
  XhsShopItemsResponse,
  XhsShopItemsDeleteResult,
  XhsItemNotesResponse,
  XhsAccountLoginState,
  XhsQrLoginSession
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

  const response = await http.post<{ access_token: string; token_type: "bearer" }>("/auth/refresh", {
    refresh_token: refreshToken
  });
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

export async function fetchPlatforms(): Promise<PlatformMeta[]> {
  try {
    const response = await http.get<Paginated<PlatformMeta>>("/platforms");
    return response.data.items;
  } catch {
    return fallbackPlatforms;
  }
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

export async function fetchXhsBenchmarks(): Promise<BenchmarkOverview> {
  const response = await http.get<BenchmarkOverview>("/xhs/analytics/benchmarks");
  return response.data;
}

export async function createBenchmarkDrafts(targetId: number, limit = 5): Promise<BenchmarkCreateDraftsResponse> {
  const response = await http.post<BenchmarkCreateDraftsResponse>(
    `/xhs/analytics/benchmarks/${targetId}/create-drafts`,
    null,
    { params: { limit } }
  );
  return response.data;
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
 * 同步范围（设计 13 §23，2026-09-22 用户口径：弹窗自选「全量 / 时间区间」）。
 * 与后端 `sync_task_service.normalize_scope` 同口径。
 */
export type SyncScope = "full" | "range";

/** 同步笔记的可选参数（弹窗选择 + 调试用小批量）。 */
export interface SyncNotesOptions {
  /** 调试/测试用：显式限制页数。**不传 = 全量**（§21）。 */
  maxPages?: number;
  scope?: SyncScope;
  /** `YYYY-MM-DD`；`scope=range` 时生效。 */
  startDate?: string;
  /** `YYYY-MM-DD`；`scope=range` 时生效（含当天一整天）。 */
  endDate?: string;
}

export type SavedNoteFilters = {
  platform?: string;
  q?: string;
  tag_id?: number;
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
          tag_id: platformOrFilters.tag_id,
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
export interface SavedNoteSyncResult {
  /** 与后端 `hydrate_note_detail` 同源的状态机。 */
  status: "ok" | "no_url" | "no_session" | "failed";
  /** 正文被详情内容覆盖/补上了（平台改过正文时本地跟上）。 */
  content_updated: boolean;
  title_updated: boolean;
  /** 本轮补进本地的图片条数（只增不删）。 */
  images_added: number;
  /** 是否补到视频流地址（列表端点不带，只有详情有）。 */
  video_added: boolean;
  /** 失败/跳过的真实原因（成功时为空串）。 */
  reason: string;
  /** 机器可读原因码（`WEB_SESSION_REQUIRED` 等；成功为空串）。 */
  code: string;
}

/**
 * 内容库「同步」按钮：用笔记 id 拉平台最新内容，更新落库（设计 13 §25）。
 *
 * 与定时任务同步笔记逻辑**同源**（后端复用 `hydrate_note_detail` 唯一实现）。
 * ⚠️ 2026-09-23 用户口径「已发布的笔记也支持同步笔记最新的三方数据」⇒ 判据放宽为
 * **`synced` 或 `publish_status == "published"`**（与前端 `canSyncNote` 同口径）。
 * 两者都不满足的笔记后端给结构化 400 **`NOTE_NOT_SYNCABLE`**（原 `NOTE_NOT_SYNCED`
 * 已随口径一起改名 —— 名字里不再预设「只有 synced 才行」）。
 *
 * `_silent`：结果（含失败原因）要显示在详情卡里，不弹全局 toast —— 失败三分法
 * 由调用方按 `sync.status` / `sync.code` 渲染（预期内安静一行、真异常红块）。
 */
export async function syncSavedNote(
  noteId: number
): Promise<{ note: SavedNote; sync: SavedNoteSyncResult }> {
  const response = await http.post<{ note: SavedNote; sync: SavedNoteSyncResult }>(
    `/notes/${noteId}/sync`,
    {},
    { _silent: true } as never
  );
  return response.data;
}

/**
 * 话题增删的**落库入口**（设计 13 §29.3）：整列替换，返回更新后的整行。
 *
 * 内容库详情卡「+ 新增 / Tag 上 × 删话题」都走它 —— 每次增删一次写库（实时落库），
 * 调用方传增删后的**完整列表**，后端负责归一化（trim / 去空 / 去重 / 限量）。
 */
export async function updateSavedNoteTopics(
  noteId: number,
  topics: string[]
): Promise<SavedNote> {
  const response = await http.put<SavedNote>(`/notes/${noteId}/topics`, { topics });
  return response.data;
}

// --------------------------------------------------------------------------- #
// 商品笔记智能生成与发布（设计 13 §31，2026-09-23）
// --------------------------------------------------------------------------- #

/** 单商品智能生成 + 落库的结果（`POST /xhs/product-notes/generate-save`）。 */
export interface ProductNoteGenerateResult {
  ok: boolean;
  platform_account_id: number;
  item_id: string;
  /** 本地笔记 ID（`local_` 前缀；发布成功后换成平台真实 ID）。 */
  note_id: string;
  title: string;
  /** 平台 AI 带的话题名（发布时用平台话题 ID，见 `raw_json.aigc.topics`）。 */
  topics: string[];
  /** 落库的媒体张数（商品图，**最多 6 张**，设计 13 §32.2.1）。 */
  image_count: number;
  /** 落库的视频个数（**最多 1 个**；视频优先于图片。当前商品无视频源 ⇒ 恒 0）。 */
  video_count: number;
  /** 本地护栏结果（记录不拦截；内容出自平台官方 AI）。 */
  guardrails: { passed: boolean; blocked_terms: string[]; price_note: string };
}

/**
 * 单商品「生成笔记」（商品列表页操作列按钮；设计 13 §31.4）。
 *
 * 调千帆后台智能生成（attributes → sellerpoint/aigc → 轮询），落库为
 * **待发布**的本地生成笔记。门禁失败（未绑店 / 无令牌 / 无此商品）是结构化
 * 400/404；平台侧失败 502 + 平台原文。
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

/** 批量生成的逐项结果。 */
export interface ProductNoteBatchItem {
  item_id: string;
  ok: boolean;
  note_id?: string;
  title?: string;
  error?: string;
  code?: string;
}

/** 批量生成汇总（`POST /xhs/product-notes/batch-generate`）。 */
export interface ProductNoteBatchResult {
  total: number;
  succeeded: number;
  failed: number;
  results: ProductNoteBatchItem[];
}

/**
 * 批量「生成笔记」（商品列表页工具栏；设计 13 §31.4 步 6）。
 *
 * 顺序处理、单项失败不阻断（与千帆页面 `Promise.allSettled` 同语义）；
 * ≤20 项/请求。返回逐项结果，调用方汇总「成功 X / 失败 Y」。
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

/** 单篇发布结果（`POST /notes/{id}/publish`）。 */
export interface NotePublishResult {
  ok: boolean;
  /** 本地笔记主键（就地更新卡片用；与平台 ID 无关）。 */
  note_pk: number;
  /** 笔记 ID（成功时是平台真实 ID，失败时是本地 `local_` ID）。 */
  note_id: string;
  /** 平台真实笔记 ID（成功时）。 */
  platform_note_id?: string;
  published_at?: string;
  /** 失败原因（平台原文 / 网络异常；成功为空串）。 */
  error?: string;
  /** 机器可读失败码（`PUBLISH_AUDIT_REJECTED` 等）。 */
  code?: string;
}

/**
 * 单篇「发布」（内容库卡片按钮，前端先二次确认；设计 13 §31.5）。
 *
 * 链路：下载媒体到本地临时文件夹 → 上传平台 → 预审核 → 发布 → 回写平台
 * 真实 note_id → 删临时目录。**失败也回 200**：门禁之外的一切失败
 * （下载 / 上传 / 平台拒发）是「试一次的结果」，落在 `publish.ok=false` +
 * `publish.error`，前端把笔记状态改成「发布失败」并展示原因、支持重发。
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

/** 批量发布汇总（`POST /notes/batch-publish`）。 */
export interface NoteBatchPublishResult {
  total: number;
  succeeded: number;
  failed: number;
  results: NotePublishResult[];
}

/**
 * 批量「发布」（内容库批量操作区；设计 13 §31.5 步 8）。
 *
 * 顺序处理、单项失败不阻断；≤20 篇/请求。返回逐篇结果，
 * 前端按结果逐条更新卡片状态（成功的换平台 ID、失败的标「发布失败」）。
 */
export async function batchPublishSavedNotes(noteIds: number[]): Promise<NoteBatchPublishResult> {
  const response = await http.post<NoteBatchPublishResult>("/notes/batch-publish", {
    note_ids: noteIds,
  });
  return response.data;
}

/**
 * 笔记详情（内容库）。**只读本地库，零平台请求**（2026-09-22 用户口径：
 * 「内容库应查询的均是本地数据」）。
 *
 * ⚠️ ~~`hydrate=true` 让后端当场打一次平台补齐正文~~ **已移除**：那条路径的
 * `db.commit()` 会与正在跑的同步任务抢 SQLite 写锁，`database is locked`
 * 未捕获 ⇒ 用户看到「内容库笔记详情 500」。一个只读页面不该有平台副作用。
 *
 * 正文与图集的补齐现在**只由同步链路**做（有界 + 跨轮累积）；本地暂时没有正文
 * 的行，界面按缺值口径说明，不假装有数据。
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

export async function addNoteAsset(noteId: number, payload: { asset_type: string; url?: string; local_path?: string }): Promise<NoteAsset> {
  const response = await http.post<NoteAsset>(`/notes/${noteId}/assets`, payload);
  return response.data;
}

export async function deleteNoteAsset(noteId: number, assetId: number): Promise<void> {
  await http.delete(`/notes/${noteId}/assets/${assetId}`);
}

export async function reorderNoteAssets(noteId: number, assetIds: number[]): Promise<void> {
  await http.put(`/notes/${noteId}/assets/reorder`, { asset_ids: assetIds });
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

export async function fetchTags(): Promise<Paginated<Tag>> {
  const response = await http.get<Paginated<Tag>>("/tags");
  return response.data;
}

export async function createTag(payload: TagPayload): Promise<Tag> {
  const response = await http.post<Tag>("/tags", payload);
  return response.data;
}

export async function batchTagNotes(payload: BatchTagNotesPayload): Promise<BatchTagNotesResponse> {
  const response = await http.post<BatchTagNotesResponse>("/notes/batch-tag", payload);
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

export async function downloadMediaFile(downloadUrl: string, fileName: string): Promise<void> {
  return downloadExportFile(downloadUrl, fileName);
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

export async function createMediaObjectUrl(downloadUrl: string): Promise<string> {
  const endpoint = downloadUrl.startsWith("/api") ? downloadUrl.slice(4) : downloadUrl;
  const response = await http.get<Blob>(endpoint, { responseType: "blob" });
  return window.URL.createObjectURL(response.data);
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
 */
export async function syncShopGoods(payload: {
  platformAccountId: number;
  maxPages?: number;
  /** §23：同步范围（弹窗选择）。不传 = 后端默认 full。 */
  scope?: SyncScope;
  startDate?: string;
  endDate?: string;
}): Promise<XhsSyncTaskSummary> {
  const response = await http.post<XhsSyncTaskSummary>(
    "/xhs/qianfan/sync-shop-goods",
    {
      platform_account_id: payload.platformAccountId,
      // ⚠️ **不传 `max_pages` = 全量**（设计 13 §21）。旧的 `?? 3` 会让商品
      // 永远只同步前 150 件；不要再写回默认值。
      ...(payload.maxPages ? { max_pages: payload.maxPages } : {}),
      ...(payload.scope ? { scope: payload.scope } : {}),
      ...(payload.startDate ? { start_date: payload.startDate } : {}),
      ...(payload.endDate ? { end_date: payload.endDate } : {}),
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

/** 无流量判定阈值：三项全部 <= 阈值才算无流量（后端不写死，由用户配置）。 */
export interface NoTrafficThresholds {
  max_browse: number;
  max_pay_order: number;
  max_pay_gmv: number;
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
}

export interface NoTrafficCandidatesResult {
  candidates: NoTrafficCandidate[];
  total: number;
  thresholds: NoTrafficThresholds;
  query: NoTrafficQuery;
  coverage: { synced_notes: number; pool_total: number; reason: string };
}

export async function fetchNoTrafficCandidates(
  accountId: number,
  thresholds: NoTrafficThresholds,
  query: NoTrafficQuery,
  limit = 50
): Promise<NoTrafficCandidatesResult> {
  const response = await http.post<NoTrafficCandidatesResult>(
    "/xhs/pc/users/no-traffic/candidates",
    { account_id: accountId, thresholds, query, limit }
  );
  return response.data;
}

export async function startNoTrafficCleanup(
  accountId: number,
  thresholds: NoTrafficThresholds,
  query: NoTrafficQuery,
  batchLimit = 20
): Promise<{ task_id: number; status: string }> {
  const response = await http.post<{ task_id: number; status: string }>(
    "/xhs/pc/users/no-traffic/cleanup",
    { account_id: accountId, thresholds, query, batch_limit: batchLimit }
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
}

export async function fetchNoTrafficCleanupStatus(
  taskId: number
): Promise<NoTrafficCleanupStatus> {
  const response = await http.get<NoTrafficCleanupStatus>(
    `/xhs/pc/users/no-traffic/cleanup/${taskId}`
  );
  return response.data;
}

/**
 * 当前用户有哪些千帆店铺、每个店铺被哪几个账号绑着（**纯本地**，不打千帆）。
 *
 * T-32 起账号↔店铺是 N:M，所以店铺列表是「选账号」之外的一层信息：
 * 商品列表页用它给账号选项标注所属店铺，并在同一店铺多账号时提示。
 */
export async function fetchXhsQianfanShops(
  accountId?: number
): Promise<{ count: number; shops: XhsQianfanShop[] }> {
  const response = await http.post<{ count: number; shops: XhsQianfanShop[] }>(
    "/xhs/qianfan/shops",
    accountId ? { platform_account_id: accountId } : {}
  );
  return response.data;
}

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
    { platform_account_id: accountId }
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

export async function polishTextWithAi(payload: PolishTextPayload): Promise<{ text: string }> {
  const response = await http.post<{ text: string }>("/ai/polish-text", payload);
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

export async function generateCoverWithAi(payload: GenerateCoverPayload): Promise<GeneratedImageAsset> {
  const response = await http.post<GeneratedImageAsset>("/ai/images/generate-cover", payload);
  return response.data;
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

export async function composeImageUtility(payload: ComposeImagePayload): Promise<ImageUtilityFile> {
  const response = await http.post<ImageUtilityFile>("/files/images/compose", payload);
  return response.data;
}

export async function resizeImageUtility(payload: ResizeImagePayload): Promise<ImageUtilityFile> {
  const response = await http.post<ImageUtilityFile>("/files/images/resize", payload);
  return response.data;
}

export async function fetchModelConfigs(modelType?: ModelType): Promise<Paginated<ModelConfig>> {
  const response = await http.get<Paginated<ModelConfig>>("/model-configs", {
    params: modelType ? { model_type: modelType } : undefined
  });
  return response.data;
}

export async function createModelConfig(payload: ModelConfigPayload): Promise<ModelConfig> {
  const response = await http.post<ModelConfig>("/model-configs", payload);
  return response.data;
}

export async function setDefaultModelConfig(configId: number): Promise<ModelConfig> {
  const response = await http.post<ModelConfig>(`/model-configs/${configId}/set-default`);
  return response.data;
}

export async function updateModelConfig(configId: number, payload: Partial<ModelConfigPayload>): Promise<ModelConfig> {
  const response = await http.patch<ModelConfig>(`/model-configs/${configId}`, payload);
  return response.data;
}

export async function testModelConfig(configId: number): Promise<{ id: number; status: string; message: string }> {
  const response = await http.post<{ id: number; status: string; message: string }>(`/model-configs/${configId}/test`);
  return response.data;
}

export async function deleteModelConfig(configId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/model-configs/${configId}`);
  return response.data;
}

export async function fetchTasks(platform?: string): Promise<Paginated<TaskRecord>> {
  const response = await http.get<Paginated<TaskRecord>>("/tasks", {
    params: platform ? { platform } : undefined
  });
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

export async function runScheduleJobOnce(jobId: number): Promise<{ log_id: number; status: string }> {
  const response = await http.post<{ log_id: number; status: string }>(`/schedule/jobs/${jobId}/run`);
  return response.data;
}

export async function fetchScheduleLogs(query: ScheduleLogQuery = {}): Promise<Paginated<ScheduledJobLog>> {
  const response = await http.get<Paginated<ScheduledJobLog>>("/schedule/logs", { params: query });
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

export async function updateMonitoringTarget(
  targetId: number,
  payload: Partial<MonitoringTargetPayload>
): Promise<MonitoringTarget> {
  const response = await http.patch<MonitoringTarget>(`/xhs/monitoring/targets/${targetId}`, payload);
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

export async function retryPublishJob(jobId: number): Promise<PublishJob> {
  const response = await http.post<PublishJob>(`/publish/jobs/${jobId}/retry`);
  return response.data;
}

export async function cancelPublishJob(jobId: number): Promise<PublishJob> {
  const response = await http.post<PublishJob>(`/publish/jobs/${jobId}/cancel`);
  return response.data;
}

export async function deletePublishJob(jobId: number): Promise<void> {
  await http.delete(`/publish/jobs/${jobId}`);
}

export async function fetchPublishAssets(jobId: number): Promise<Paginated<PublishAsset>> {
  const response = await http.get<Paginated<PublishAsset>>(`/publish/jobs/${jobId}/assets`);
  return response.data;
}

export async function addPublishAsset(jobId: number, payload: PublishAssetPayload): Promise<PublishAsset> {
  const response = await http.post<PublishAsset>(`/publish/jobs/${jobId}/assets`, payload);
  return response.data;
}

export async function deletePublishAsset(assetId: number): Promise<{ id: number; status: string }> {
  const response = await http.delete<{ id: number; status: string }>(`/publish/assets/${assetId}`);
  return response.data;
}

export async function uploadPublishAsset(assetId: number): Promise<PublishAsset> {
  const response = await http.post<PublishAsset>(`/publish/assets/${assetId}/upload`);
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
  total: number | null;
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
 * 触发笔记同步（**所有账号**都用这个）。
 *
 * `profileUrl` 只有 **PC 账号**需要：`user_posted` 要的是 URL 级 `xsec_token`，
 * 而账号 Cookie 拿不到它（设计 13 §4.1 与 §6.1.1）—— PC 账号不传就必定
 * `XSEC_TOKEN_REQUIRED`。Creator 账号传空串即可。
 *
 * **防重**：该账号已有未完成的同步任务 → 后端返回**同一个 `task_id`**，
 * 不新建任务、不重复打平台。
 *
 * `_silent`：失败原因要显示在卡片里（`readErrorDetail`），不弹全局 toast。
 */
export async function syncAccountNotes(
  accountId: number,
  options: SyncNotesOptions = {}
): Promise<XhsSyncTaskSummary> {
  const response = await http.post<XhsSyncTaskSummary>(
    `/accounts/${accountId}/notes/sync`,
    {
      // ⚠️ **不传 `max_pages` = 全量**（设计 13 §21，2026-09-22 用户口径：
      // 「不要设置翻页上限，应为全量同步」）。旧的 `?? 3` 正是「1961 条的账号
      // 永远只同步 180 条」的直接入口 —— 不要再写回默认值。
      // `maxPages` 只留给调试/测试显式小批量。
      ...(options.maxPages ? { max_pages: options.maxPages } : {}),
      // §23：用户在弹窗里选的同步范围。不传 = 后端默认 full（不设时间界）。
      ...(options.scope ? { scope: options.scope } : {}),
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
