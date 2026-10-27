/**
 * 笔记详情弹窗（**共用版式，唯一实现**）。
 *
 * ## 为什么抽出来
 *
 * 「我的笔记」与「内容库」都需要「左媒体 / 右信息」的详情弹窗。早前两页各写一份：
 * 内容库是 `Drawer` + 单栏 + `Alert type="warning"` 的泛化文案，我的笔记是 `Modal`
 * + 双栏 + 失败三分法 —— 同一个「看一条笔记」的动作，在两页长得不一样、失败时说
 * 的话也不一样。硬约束是**同一语义不留两份实现**（DEF-044 / DEF-047 / DEF-058）：
 * 一处对一处错 = 必现漂移。
 *
 * 现在版式在这里**只有一份**：
 *
 * * 弹窗尺寸与「主体不滚、两栏各自滚」（R9 / 设计 12 §23）；
 * * 左栏媒体区（视频 / 多图轮播 / 缩略图 / 下载）；
 * * 失败呈现的**三分法**（设计 12 §17.9）；
 * * 评论区四态渲染（加载 / 空 / 取不到 / 有数据，且**四态占同一高度**，版式不跳）。
 *
 * 调用方只提供**数据**与**右栏扩展内容**（正文、指标、商品、标签管理…）——
 * 这些正是两页真正不同的部分。
 */
import {
  Alert,
  Avatar,
  Button,
  Divider,
  Empty,
  Modal,
  Space,
  Spin,
  Tooltip,
  Typography,
} from "antd";
import {
  CommentOutlined,
  DownloadOutlined,
  HeartOutlined,
  LeftOutlined,
  LoadingOutlined,
  ReloadOutlined,
  RightOutlined,
} from "@ant-design/icons";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";

import { detailMessage } from "../../lib/api";
import { useThemePalette } from "../../app/providers";

const { Text } = Typography;

// --------------------------------------------------------------------------- #
// 失败呈现（三分法）——**唯一实现**
// --------------------------------------------------------------------------- #

/**
 * 取数失败的**安静降级文案**（v1.27 / 设计 12 §17.8）。
 *
 * 页签不再按账号类型置灰，改成「以取数结果为准」—— 那么失败时也不能把它渲染成
 * 一块红色错误条或弹一句 toast：那只是**把置灰的位置换成了报错**。所以这里给一行
 * 25–30 字的说明（沿用 §21.2 缺值口径的长度约束），讲清「为什么这里是空的」；
 * 完整原因放进 Tooltip，需要排查时看得到。
 */
const QUIET_REASONS: Record<string, string> = {
  // 27 字（§21.2 的 25–30 窗口）。**不写「请稍后再试」**：等一会儿不会变好，
  // 这件事只能靠重新登录解决 —— 一句不可执行的建议等于让人白等（v1.28 / DEF-058）。
  // v1.40（2026-09-24 用户口径）：引导语与后端 `_web_session_required_detail()`
  // **同一口径**（「创作者平台或 PC 端平台」）。同一个 code 在两处给两种引导，
  // 用户按哪一句做都可能对不上界面上的按钮名。「重新登录」这个词必须留着 ——
  // 它是这条文案**唯一可执行的动作**，守卫 `test_quiet_reason_is_actionable_
  // not_a_wait_and_see` 钉的就是它。
  WEB_SESSION_REQUIRED: "该账号缺少登录态，需重新登录创作者平台或 PC 端平台",
  // 28 字
  XSEC_TOKEN_REQUIRED: "缺少主页访问令牌，粘贴你自己的主页链接后即可读取完整列表",
  // 28 字（§19.13.9，2026-09-28）：缺详情令牌是**用户可自救**的（补齐网页版登录后
  // 重新同步），所以给安静说明而不是红块 —— 与「平台没返回内容」那种用户无法行动的
  // 归因刻意区分（DEF-058）。「重新登录」是唯一可执行的动作，必须留着。
  NOTE_DETAIL_TOKEN_REQUIRED: "该账号的这条笔记缺少详情令牌，需重新登录网页版后重新同步",
};

/** 一行安静说明的**形状**：`text` 给界面，`full` 是后端原话（Tooltip 里看）。 */
export type QuietError = { text: string; full: string; code: string };

export function quietText(message: string, code: string): string {
  const mapped = QUIET_REASONS[code];
  if (mapped) return mapped;
  const trimmed = (message || "").trim();
  if (!trimmed) return "这一类数据暂时取不到";
  return trimmed.length > 30 ? `${trimmed.slice(0, 29)}…` : trimmed;
}

export function quietError(message: string, code: string): QuietError {
  const full = (message || "").trim();
  const text = quietText(message, code);
  return { text, full: full || text, code: code || "" };
}

/**
 * Normalise the several error shapes the backend can return.
 *
 * 文案解析走 `detailMessage`（**唯一实现**，与全局拦截器同一份）——
 * 这里只额外把机器可读的 `code` 取出来。同一件事留两份解析，迟早会出现
 * 「toast 说的是真原因、页面说的是通用文案」这种自相矛盾。
 */
export function readError(error: unknown): { message: string; code: string } {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  const message = detailMessage(detail);
  if (message) {
    const code =
      detail && typeof detail === "object" && !Array.isArray(detail)
        ? ((detail as { code?: string }).code ?? "")
        : "";
    return { message, code };
  }
  return { message: (error as Error)?.message || "请求失败", code: "" };
}

/**
 * **预期内的取不到** —— 不是接口异常。
 *
 * 这类原因的共同点：**原因在本地、用户可自救**，所以它的正确呈现是
 * 「这里为什么是空的」的一句说明，而不是一块错误。
 */
const EXPECTED_FAILURE_CODES = new Set([
  "WEB_SESSION_REQUIRED",
  "XSEC_TOKEN_REQUIRED",
  "NOTE_DETAIL_TOKEN_REQUIRED",
]);

/** 一行安静提示：不占红块、不弹 toast；完整原因挂 Tooltip 供排查。 */
export function quietHint(fullReason: string, text: string) {
  return (
    <Tooltip title={fullReason}>
      <div style={{ padding: "32px 0", textAlign: "center" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {text}
        </Text>
      </div>
    </Tooltip>
  );
}

/**
 * 失败呈现的**三分法**（v1.28 / DEF-058、设计 12 §17.9）。
 *
 * | 情形 | 呈现 |
 * |---|---|
 * | 有数据 | 正常渲染（错误与数据可并存：翻页失败不该抹掉前一页） |
 * | **没数据** | 空态 —— 那**不是错误**（接口 200、列表为空） |
 * | **取不到**（预期内 code） | 一行 `text` 安静说明 + Tooltip |
 * | **真·接口异常** | 红块 + **真实原因**，绝不替换成「请稍后重试」 |
 *
 * 前两行对应用户口径「如果没有数据则无需展示」；最后一行对应「只有接口异常才真实
 * 抛出异常信息」。**不能**把所有失败渲染成同一句话 —— 否则「可自救的取不到」和
 * 「真坏了」在界面上长得一模一样。
 */
export function renderFailure(error: QuietError, title = "这一类数据加载失败") {
  if (EXPECTED_FAILURE_CODES.has(error.code)) return quietHint(error.full, error.text);
  return (
    <div style={{ padding: "16px 0" }}>
      <Alert showIcon type="error" message={title} description={error.full} />
    </div>
  );
}

// --------------------------------------------------------------------------- #
// 素材下载
// --------------------------------------------------------------------------- #

/**
 * 远程素材「取回本地」（`fetch → blob → 下载`），失败回退新窗口打开。
 *
 * **不做服务端转发** —— 不给后端加带宽（设计 04 §2.20）。
 * 本地素材（内容库的 `/api/files/media/...`）走同一个函数：同源 fetch 一样成立，
 * 免得同一个动作留两份实现。
 */
export async function saveRemoteAsset(url: string, fileName: string): Promise<void> {
  if (!url) return;
  try {
    const response = await fetch(url, { mode: "cors" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const objectUrl = window.URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = fileName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.URL.revokeObjectURL(objectUrl);
  } catch {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

// --------------------------------------------------------------------------- #
// 评论区（四态，渲染唯一实现）
// --------------------------------------------------------------------------- #

/** 一条评论的**渲染契约**：两页的数据源不同，但落到界面上的字段是这几个。 */
export type NoteDetailComment = {
  comment_id: string;
  user_name: string;
  content: string;
  like_count: number;
  parent_comment_id?: string | null;
  created_at_remote?: unknown;
};

/**
 * 平台返回的发布时间 → `YYYY/M/D`（**唯一实现**）。
 *
 * 平台给的时间戳不统一：秒（`1e12` 以下）与毫秒都有，creator 链路甚至直接给
 * 字符串（`"2026-08-13 09:30"`）—— 三种形态都在这里一次归一，别处不再各写一份。
 */
export function formatRemoteTime(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  const num = typeof value === "number" ? value : Number(value);
  if (Number.isFinite(num) && num > 0) {
    return new Date(num > 1e12 ? num : num * 1000).toLocaleDateString("zh-CN");
  }
  return typeof value === "string" ? value : "";
}

/**
 * 评论列表（**四态占同一高度**，所以评论到达时弹窗版式不跳动 —— R9 / §23）。
 *
 * | 状态 | 呈现 |
 * |---|---|
 * | 加载中 | `Spin` + 「评论加载中…」 |
 * | 取不到（预期内） | 一行安静说明（`renderFailure` 的三分法） |
 * | 真·接口异常 | 红块 + 真实原因 |
 * | 空 | `Empty`「没有评论」——**不是错误** |
 *
 * `footer` 用于「加载更多」这类由调用方驱动的动作（内容库是分页追加）。
 */
export function NoteCommentList({
  comments,
  loading,
  error,
  emptyText = "没有评论",
  footer,
}: {
  comments: NoteDetailComment[];
  loading?: boolean;
  error?: QuietError | null;
  emptyText?: string;
  footer?: ReactNode;
}) {
  return (
    <div className="note-detail-comments" style={{ marginTop: 12 }}>
      <Divider titlePlacement="start" style={{ margin: "8px 0" }}>
        <Space size={6}>
          <CommentOutlined />
          评论 {comments.length > 0 && <Text type="secondary">({comments.length})</Text>}
        </Space>
      </Divider>
      {loading ? (
        <div style={{ padding: "16px 0" }}>
          <Spin indicator={<LoadingOutlined spin />} />{" "}
          <Text type="secondary">评论加载中…</Text>
        </div>
      ) : error ? (
        renderFailure(error)
      ) : comments.length === 0 ? (
        <Empty description={emptyText} />
      ) : (
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          {comments.map((comment, index) => (
            <div
              key={`${comment.comment_id || "c"}-${index}`}
              style={{ paddingLeft: comment.parent_comment_id ? 20 : 0 }}
            >
              <Space size={8} align="start">
                <Avatar size={24}>{(comment.user_name || "?")[0]?.toUpperCase()}</Avatar>
                <div>
                  <Space size={8}>
                    <Text strong style={{ fontSize: 12 }}>
                      {comment.user_name || "匿名"}
                    </Text>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      <HeartOutlined /> {comment.like_count}
                    </Text>
                    {Boolean(comment.created_at_remote) && (
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {formatRemoteTime(comment.created_at_remote)}
                      </Text>
                    )}
                  </Space>
                  <div>
                    <Text style={{ fontSize: 13 }}>{comment.content}</Text>
                  </div>
                </div>
              </Space>
            </div>
          ))}
        </Space>
      )}
      {footer}
    </div>
  );
}

// --------------------------------------------------------------------------- #
// 两栏主体（弹窗与挂链笔记 tabs 共用，唯一实现）
// --------------------------------------------------------------------------- #

/**
 * 「左媒体 / 右信息」的两栏主体（从 `NoteDetailModal` 抽出，设计 13 §31.6）。
 *
 * 为什么抽出来：商品列表页的挂链笔记弹窗是 **Modal + Tabs**（一篇笔记一个 tab），
 * 每个 tab 的版式要与内容库的笔记详情弹窗**完全一致** —— 同一个「看一条笔记」的
 * 动作留两份实现 = 必现漂移（DEF-044/047/058）。现在版式只有这一份：弹窗是
 * 「Modal + NoteDetailBody」，tab 弹窗是「Modal + Tabs + NoteDetailBody」。
 *
 * 媒体状态由本组件自管（索引 / 下载中）；换了一篇笔记（mediaUrls 变）自动回到第一张。
 */

/**
 * 缩略图边长（v1.55 用户口径「缩小图片一行展示，超出一行的支持左右滑动」）：
 * 48 → 40。收窄让不放滑也能多容几张；真正放不下时由 `.note-detail-thumbs`
 * 的 `overflow-x: auto` 左右滑（触板横滑 / shift+滚轮 / 拖滚动条均可）。
 * **一个常量**：真图与选中描边共用同一尺寸口径（选中态只换 border 颜色）。
 */
const MEDIA_THUMB_SIZE = 40;

export function NoteDetailBody({
  mediaUrls = [],
  videoUrl = "",
  isVideo = false,
  mediaNamePrefix = "note",
  mediaFootnote = "素材为平台 CDN 直链，链接有时效性，久置后可能失效",
  mediaEmpty,
  children,
}: {
  mediaUrls?: string[];
  videoUrl?: string;
  isVideo?: boolean;
  mediaNamePrefix?: string;
  mediaFootnote?: string;
  mediaEmpty?: ReactNode;
  children?: ReactNode;
}) {
  const p = useThemePalette();
  const [mediaIndex, setMediaIndex] = useState(0);
  const [downloadingUrl, setDownloadingUrl] = useState("");

  const mediaKey = mediaUrls.join("|");
  useEffect(() => {
    setMediaIndex(0);
  }, [mediaKey, videoUrl]);

  // 媒体换了但索引还指着旧的 ⇒ 取模兜底（负索引也回到合法范围）。
  const safeIndex = mediaUrls.length
    ? ((mediaIndex % mediaUrls.length) + mediaUrls.length) % mediaUrls.length
    : 0;

  const currentImage = mediaUrls.length ? mediaUrls[safeIndex] : "";

  function download(url: string, fileName: string) {
    setDownloadingUrl(url);
    void saveRemoteAsset(url, fileName).finally(() => setDownloadingUrl(""));
  }

  return (
    <>
      {/*
        两栏布局的 CSS 在 `global.css`（`.note-detail-*`）：低于 900px 时两栏堆叠，
        固定高度会裁掉内容，因此那时改成主体滚动（媒体查询在同一处）。
      */}
      <div className="note-detail-body">
        {/* ---------------- LEFT · media ---------------- */}
        <div className="note-detail-col" style={{ flex: "1 1 360px", minWidth: 300, maxWidth: 640 }}>
          {isVideo && !videoUrl && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="这条视频笔记还没有可播放的地址"
              description="同步笔记补齐后即可播放。"
            />
          )}
          {videoUrl ? (
            <div>
              <video
                src={videoUrl}
                controls
                style={{ width: "100%", maxHeight: "58vh", background: "#000", borderRadius: 8 }}
              />
              <Button
                style={{ marginTop: 8 }}
                icon={<DownloadOutlined />}
                loading={downloadingUrl === videoUrl}
                onClick={() => download(videoUrl, `${mediaNamePrefix}.mp4`)}
              >
                下载视频
              </Button>
            </div>
          ) : mediaUrls.length > 0 ? (
            <div>
              {/* ⚠️ 预览窗口**尺寸固定**（2026-10-27 用户口径「图片的预览窗口大小固定不要变化」）：
                  原先是 `maxHeight: 58vh` + `objectFit: contain` —— 那是「随图片自适应」，
                  于是竖图和方图之间切换时容器高度来回变（点下一张，右边正文跟着上下跳）。
                  ⇒ 改成**固定的 58vh 高盒子**，图片在里面 `contain` 居中留白，盒子不动。 */}
              <div
                style={{
                  position: "relative",
                  background: p.mediaBg,
                  borderRadius: 8,
                  overflow: "hidden",
                  height: "58vh",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <img
                  src={currentImage}
                  alt=""
                  style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: "block" }}
                />
                {mediaUrls.length > 1 && (
                  <>
                    <Button
                      shape="circle"
                      icon={<LeftOutlined />}
                      style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)" }}
                      onClick={() => setMediaIndex((current) => current - 1)}
                    />
                    <Button
                      shape="circle"
                      icon={<RightOutlined />}
                      style={{ position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)" }}
                      onClick={() => setMediaIndex((current) => current + 1)}
                    />
                    <div
                      style={{
                        position: "absolute",
                        right: 10,
                        bottom: 10,
                        padding: "2px 8px",
                        borderRadius: 10,
                        background: "rgba(0,0,0,0.55)",
                        color: "#fff",
                        fontSize: 12,
                      }}
                    >
                      {safeIndex + 1} / {mediaUrls.length}
                    </div>
                  </>
                )}
              </div>
              {mediaUrls.length > 1 && (
                /* 缩略图条（v1.55 用户口径）：**一行展示，超出一行左右滑**。
                   旧实现是 `Space wrap` —— 图一多就堆成好几行，把下载按钮和素材
                   说明顶出首屏，左栏被迫多滚一屏。现在永不分行，放不下的部分由
                   `.note-detail-thumbs` 的 `overflow-x: auto` 接住。 */
                <div className="note-detail-thumbs" style={{ marginTop: 8 }}>
                  {mediaUrls.map((url, index) => (
                    <img
                      key={`${url}-${index}`}
                      src={url}
                      alt=""
                      onClick={() => setMediaIndex(index)}
                      style={{
                        width: MEDIA_THUMB_SIZE,
                        height: MEDIA_THUMB_SIZE,
                        flex: "0 0 auto",
                        objectFit: "cover",
                        borderRadius: 4,
                        cursor: "pointer",
                        border: index === safeIndex ? "2px solid #1677ff" : "2px solid transparent",
                      }}
                    />
                  ))}
                </div>
              )}
              {/* 单个按钮不需要 `Space wrap`（v1.55：wrap 语义只属于会溢出的行） */}
              <div style={{ marginTop: 8 }}>
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  loading={downloadingUrl === currentImage}
                  onClick={() => download(currentImage, `${mediaNamePrefix}-${safeIndex + 1}.jpg`)}
                >
                  下载当前图片
                </Button>
              </div>
            </div>
          ) : (
            mediaEmpty ?? <Alert type="info" showIcon message="未解析到图片或视频素材" />
          )}

          {mediaFootnote && (
            <div style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 11 }}>
                {mediaFootnote}
              </Text>
            </div>
          )}
        </div>

        {/* ---------------- RIGHT · content ---------------- */}
        <div
          className="note-detail-col note-detail-col--content"
          style={{ flex: "1 1 420px", minWidth: 320 }}
        >
          {/* 信息区自身可滚（`min-height: 0` 是 flex 子项能滚的必要条件），
              因此它不会把下方的评论区顶出弹窗。 */}
          <div className="note-detail-main">{children}</div>
        </div>
      </div>
    </>
  );
}

// --------------------------------------------------------------------------- #
// 弹窗外壳
// --------------------------------------------------------------------------- #

export type NoteDetailModalProps = {
  open: boolean;
  onClose: () => void;
  /**
   * 标题：**字符串**（组件渲染标题文本 + 省略号截断，右侧留刷新按钮）或
   * **自定义 ReactNode**（调用方自己排整行标题 —— 内容库：笔记名称 + 加号 + 标签
   * chips，设计 13 §29.12）。
   *
   * 为什么需要自定义：标题栏容器是 `justify-content: space-between`，任何「标题右侧
   * 的插槽」都会被推到弹窗远端 —— §29.7 的 `titleExtra` 插槽正因此做不到「加号紧跟
   * 标题文本后」（用户 2026-09-22 报出）。让调用方直接给一整行，元素按 DOM 顺序紧邻
   * 排列，组件不再猜。两种形态由 `typeof title` 判别，互斥。
   */
  title?: ReactNode;
  /** 标题栏右侧「刷新」按钮；不传则不渲染该按钮。 */
  onRefresh?: () => void;
  refreshing?: boolean;
  /** 顶部一行「正在获取…」提示（详情加载中）。 */
  loading?: boolean;
  loadingHint?: string;
  /** 详情取不到的**就地**说明（三分法）；有数据时也可以同时有它。 */
  failure?: QuietError | null;
  /** 左栏媒体：图片 URL 列表与视频 URL（二选一，视频优先）。 */
  mediaUrls?: string[];
  videoUrl?: string;
  /**
   * 这条笔记**是不是视频笔记**（由调用方按平台 `type` 判，组件不猜）。
   *
   * 与 `videoUrl` 为空组合出的第三种状态要分开说（三分法，设计 12 §17.9）：
   * 视频笔记 + 没有地址 = **取不到**（平台列表不带流地址，要补一次详情），
   * 不是「这条笔记没有媒体」。没有这个 prop 就只能显示空态，用户会以为
   * 笔记本身没内容。
   */
  isVideo?: boolean;
  /** 下载文件名的前缀（一般传 note_id），默认 `note`。 */
  mediaNamePrefix?: string;
  /** 左栏底部的补充说明（如「素材为平台 CDN 直链…」）；不传则不渲染。 */
  mediaFootnote?: string;
  /** 左栏占位（既无图也无视频时）；默认提示「未解析到图片或视频素材」。 */
  mediaEmpty?: ReactNode;
  /** 右栏内容（信息区，可滚动）。 */
  children?: ReactNode;
  /** 右栏底部（评论区），固定高度内部滚动。 */
  commentsSlot?: ReactNode;
  /** 标题栏下方、两栏之上的一行（如只读提示）。 */
  banner?: ReactNode;
};

/**
 * 「左媒体 / 右信息」的笔记详情弹窗。
 *
 * 布局与交互约束（设计 04 §2.20 / 设计 12 §23）：
 *
 * * `80vw` / `72vh`，**弹窗主体本身不滚**（`overflow: hidden`）—— 信息区与评论区
 *   各自内部滚动。用固定 `height` 而不是 `maxHeight`，短笔记与长笔记之间**不跳版**；
 * * 左栏：视频优先（`<video controls>`），否则多图轮播（左右按钮 + 计数角标 +
 *   缩略图条），再否则空态；
 * * 三栏以上的界面由调用方在 `children` 里组织 —— 组件不假设右栏长什么样。
 */
export function NoteDetailModal({
  open,
  onClose,
  title,
  onRefresh,
  refreshing = false,
  loading = false,
  loadingHint = "正在获取当前笔记的详情（正文 / 全部图片 / 视频）…",
  failure,
  mediaUrls = [],
  videoUrl = "",
  isVideo = false,
  mediaNamePrefix = "note",
  mediaFootnote = "素材为平台 CDN 直链，链接有时效性，久置后可能失效",
  mediaEmpty,
  children,
  commentsSlot,
  banner,
}: NoteDetailModalProps) {
  // 自定义标题（ReactNode）= 调用方自己排的一行；字符串 = 组件默认排版。
  // ⚠️ 不要退回「标题右侧插槽」的思路（§29.7 的 titleExtra）：标题栏容器是
  // space-between，插槽元素永远被推到弹窗远端，做不到「紧跟标题文本后」（§29.12）。
  const customTitle = title !== undefined && title !== null && typeof title !== "string";

  return (
    <Modal
            centered
            open={open}
      onCancel={onClose}
      width="80vw"
      style={{ maxWidth: 1600, paddingBottom: 0 }}
      styles={{
        body: {
          // 固定 `height`（不是 `maxHeight`）⇒ 短笔记与长笔记之间不跳版；
          // 主体不滚，两栏各自滚（R9 / §23）。
          height: "72vh",
          overflow: "hidden",
          paddingRight: 12,
        },
      }}
      title={
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: customTitle ? 6 : 0,
            paddingRight: 28,
            minWidth: 0,
            // 自定义行按 DOM 顺序**紧邻**排列（加号才能紧跟标题文本，§29.12）；
            // 字符串标题保持旧版式：文本在左、刷新按钮被 space-between 推到右。
            justifyContent: customTitle ? "flex-start" : "space-between",
          }}
        >
          {customTitle ? (
            title
          ) : (
            /* `minWidth: 0` 是 flex 子项能被省略号收窄的必要条件 —— 标题长时仍正确
               截断，不会把刷新按钮挤出可视区。 */
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>
              {title || "笔记详情"}
            </span>
          )}
          {onRefresh && (
            <Button
              size="small"
              icon={<ReloadOutlined spin={refreshing} />}
              onClick={onRefresh}
              loading={refreshing}
              // 自定义行里刷新按钮仍靠右，不挤调用方排的左侧内容。
              style={customTitle ? { marginLeft: "auto" } : undefined}
            >
              刷新
            </Button>
          )}
        </div>
      }
      footer={null}
      destroyOnClose
    >
      <div>
        {banner}
        {loading && (
          <div style={{ padding: "4px 0 12px" }}>
            <Spin size="small" indicator={<LoadingOutlined spin />} />{" "}
            <Text type="secondary" style={{ fontSize: 12 }}>
              {loadingHint}
            </Text>
          </div>
        )}
        {failure && renderFailure(failure)}
        <NoteDetailBody
          mediaUrls={mediaUrls}
          videoUrl={videoUrl}
          isVideo={isVideo}
          mediaNamePrefix={mediaNamePrefix}
          mediaFootnote={mediaFootnote}
          mediaEmpty={mediaEmpty}
        >
          {children}
        </NoteDetailBody>
        {commentsSlot}
      </div>
    </Modal>
  );
}

