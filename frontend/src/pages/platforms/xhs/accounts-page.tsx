import {
  Alert,
  App,
  Avatar,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Modal,
  Row,
  Space,
  Spin,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  CloudDownloadOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  ShopOutlined,
  SyncOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { useEffect, useRef, useState } from "react";
import { hasPermission } from "../../../hooks/use-permission";

import { AddAccountDrawer } from "../../../components/account/add-account-drawer";
import { RenewLoginModal, type RenewTarget } from "../../../components/account/renew-login-modal";
import { SyncOptionsModal, type SyncOptions } from "../../../components/platforms/sync-options-modal";
import { useThemePalette } from "../../../app/providers";
import {
  deleteAccount,
  fetchAccounts,
  fetchXhsAccountLoginState,
  fetchXhsAccountLoginStates,
  syncAccountMetrics,
  syncAccountNotes,
  syncAccountNotesStatus,
  syncGoodsNotes,
  syncGoodsNotesStatus,
  readErrorDetail,
  syncShopGoods,
  syncShopGoodsStatus,
} from "../../../lib/api";
import type {
  SyncNotesOptions,
  XhsSyncLastResult,
  XhsSyncTaskStatus,
} from "../../../lib/api";
import { loginChannelLabel, loginChannelRows, loginTagTip } from "../../../lib/login-state";
import {
  channelState,
  channelUnavailable,
  goodsGate,
  goodsNoteSyncPlan,
  goodsResultText,
  noteSyncPlan,
  syncResultText,
  syncTaskPhase,
  syncTerminalNotice,
} from "../../../lib/sync-gates";
import { formatShanghaiTime } from "../../../lib/time";
import type { PlatformAccount, XhsLoginChannels } from "../../../types";
import { WaitMask } from "../../../components/ui/wait-mask";
import { dictLabel, dictListClass } from "../../../lib/system-dicts";

const { Text } = Typography;

/**
 * 同步状态轮询间隔（设计 13 §6.2）：5 秒，与既有任务状态查询同阶。
 * **不引入 SSE** —— 这里不是长连接场景，多一个通道就多一个会静默失效的地方。
 */
const SYNC_POLL_INTERVAL_MS = 5000;

function formatDate(value?: string): string {
  return formatShanghaiTime(value);
}

function profileValue(account: PlatformAccount, key: string): string | null {
  const value = account.profile?.[key];
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return String(value);
}

const statusColorMap: Record<string, string> = {
  active: "green",
  healthy: "green",
  expired: "red",
  unknown: "default",
};

const statusLabelMap: Record<string, string> = {
  active: "正常",
  healthy: "正常",
  expired: "过期",
  unknown: "未知",
};

// 设计 63：状态文案/颜色走字典 `xhs_account_status`（上表为镜像兜底）。
function accountStatusLabel(status: string): string {
  return dictLabel("xhs_account_status", status, statusLabelMap);
}

function accountStatusColor(status: string): string {
  return dictListClass("xhs_account_status", status, statusColorMap);
}

/**
 * 平台门禁判据 / 三态决策 / 结果行文案都在 `lib/sync-gates.ts`（§19）——
 * 纯函数、可被行为脚本直接驱动；本页面只留接线。
 */

/**
 * 一条**账号级同步链路**的接线（**§44**，2026-09-30 拆成三条）。
 *
 * 为什么做成数据而不是三份 `if`：首查、5s 轮询、按钮态三处都要「按链路 × 账号」遍历，
 * 写三遍必然在其中一遍上漂移（漏掉商品笔记 = 它的按钮永远切不回「同步笔记」，
 * 正是 H9 那类「看着像在跑，其实再也没更新过」）。链路的**差异**只有这五项，
 * 其余纪律（判据 / 终态提示 / 竞态防护）三条完全共用。
 */
interface SyncChannel {
  kind: "notes" | "goods_notes" | "goods";
  fetchStatus: (accountId: number) => Promise<XhsSyncTaskStatus>;
  setIds: (updater: (current: Set<number>) => Set<number>) => void;
  /** 当前在跑的账号集合（按钮态由它驱动）。 */
  ids: Set<number>;
  /** 结果行的量词（笔记「条」/ 商品「件」）。 */
  unit: string;
  formatResult: (result: XhsSyncLastResult | null, unit: string) => string;
}

/*
 * ⚠️ 私有实现 `readErrorDetail` **已移入 `lib/api.ts`**（2026-10-05）：商品台账页也要
 * 同一句话（需求 5：两个入口一套逻辑）⇒ 一份实现、两处 import（DEF-044）。
 * 本文件改为从 `lib/api` import 它，行为逐字不变（默认兜底仍传「同步失败」）。
 */

export function XhsAccountsPage() {
  const p = useThemePalette();
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [syncingAccountIds, setSyncingAccountIds] = useState<Set<number>>(() => new Set());
  /** 每个账号最近一次「互动数据」的结果说明（不可得原因 / 同步失败原因），只在卡片内展示。 */
  const [metricsNotes, setMetricsNotes] = useState<Record<number, string>>({});
  /**
   * 正在跑「同步笔记」/「同步商品笔记」/「同步店铺商品」的账号。
   *
   * **唯一用途 = 按钮文案在「同步笔记 ↔ 同步中」之间切换**（2026-09-27 用户口径：
   * 「点击后展示的同步任务进度卡移除，只需要将同步笔记按钮改成同步中即可」）——
   * 卡片里不再有进度区块，进度 / 结果 / 失败原因一律去任务中心的「任务日志」看。
   *
   * ⚠️ **§44（2026-09-30）起「同步笔记」与「同步商品笔记」各自一个集合**：它们是两个
   * 独立任务（`xhs_note_sync` / `xhs_goods_note_sync`）与两条独立日志流 ⇒ 各自独立
   * 轮询、各自独立按钮态。合并回一个集合会让「商品笔记在跑」把普通笔记按钮也点亮
   * （正是拆分要修的语义）。
   */
  const [noteSyncIds, setNoteSyncIds] = useState<Set<number>>(() => new Set());
  const [goodsNoteSyncIds, setGoodsNoteSyncIds] = useState<Set<number>>(() => new Set());
  const [goodsSyncIds, setGoodsSyncIds] = useState<Set<number>>(() => new Set());
  /**
   * 轮询节拍。每轮结束 +1 —— 用来让下面的 effect **重新排下一次 timeout**。
   * 为什么不靠 Set 的引用变化：一轮轮询若什么都没变（状态仍是 running），
   * Set 内容相同、effect 不会重跑，轮询会**静默停在第 2 次**。
   */
  const [syncPollTick, setSyncPollTick] = useState(0);
  /**
   * **本页刚刚触发过**同步的账号（§35.17.4：首查与「刚点击」的竞态防护）。
   *
   * 首查是「进入页面时的第一个信息」，但它是**异步**的：用户在首查返回之前点了
   * 「同步笔记」，那条首查响应（问的是点击**之前**的库状态 ⇒ idle）就会把刚刚入集的
   * 账号摘出轮询集 —— 症状正是本项目最忌讳的那一类「看着像在跑，其实再也没更新过」。
   *
   * 所以本页触发过的账号记在这里，首查**不许摘**它们；等轮询真的看到 `active`
   * （或走到终态）再删除标记，交还给常规判据。刷新页面时 ref 自然重置，
   * 那时库里的任务已经被首查看见，不需要这个兜底。
   */
  const locallyTriggeredRef = useRef<Set<number>>(new Set());
  /** PC 账号点「同步笔记」时先要主页链接（设计 13 §6.1.1）。 */
  const [linkAccount, setLinkAccount] = useState<PlatformAccount | null>(null);
  const [linkInput, setLinkInput] = useState("");
  /**
   * 千帆会话不可用时要提示的账号（v1.31，用户 2026-09-21 口径）。
   *
   * 点「同步商品」时若 ark 状态是「确定不可用」，**不发起同步**，
   * 先弹这个窗提示去补登千帆；弹窗内可一键打开对应账号的 ark 续期弹窗。
   */
  const [arkPrompt, setArkPrompt] = useState<PlatformAccount | null>(null);
  /**
   * 「同步**商品笔记**」时千帆会话不可用要提示的账号（**§44**，2026-09-30 用户拍板拆分）。
   *
   * 与上面的 `arkPrompt` 是**同一个门禁平台（ark）但两条不同链路**：`arkPrompt` 属于
   * 「同步商品」（店铺商品台账，`shop_products`），本状态属于「同步商品笔记」
   * （千帆 `goods-note/list` 的挂链笔记，`notes` + `note_goods`）。文案必须分开写 ——
   * 用户看到「补登千帆」却不知道是为了哪件事，就等于没说清。
   */
  const [goodsNotePrompt, setGoodsNotePrompt] = useState<PlatformAccount | null>(null);
  /**
   * 「同步笔记」（普通笔记）创作者会话不可用时要提示的账号（§21.4 + **§44**）。
   *
   * **创作者平台会话是单一硬门禁**（用户 2026-09-22 口径：「同步笔记按钮该账号必须存在
   * 有效的创作者登录会话，不存在有效创作者平台登录会话的提示补齐后再触发」）；
   * `unknown` 一律放行让同步真发一次（DEF-057：没问到 ≠ 不可用）。
   *
   * ⚠️ §44 起商品笔记（ark 门禁）**不再**走这里 —— 它有自己的 `goodsNotePrompt`。
   */
  const [notePrompt, setNotePrompt] = useState<PlatformAccount | null>(null);
  // §23 同步选项弹窗：门禁通过后让用户选「全量 / 时间区间」再触发。
  // §44：`kind` 三值 —— `notes`（普通笔记）/ `goods_notes`（**商品笔记**）/ `goods`（店铺商品台账）。
  const [syncOptions, setSyncOptions] = useState<
    { kind: "notes" | "goods_notes" | "goods"; accountId: number; nickname: string } | null
  >(null);
  /**
   * 网页版（PC）会话不可用时要提示登录的账号（v1.31，用户 2026-09-21 口径：
   * 「[同步数据]走pc获取粉丝关注获赞数，如果pc会话不可用则提示登录」）。
   *
   * 点「同步数据」时若 www 状态是「确定不可用」，不发起同步，先弹窗提示登录；
   * 弹窗内可一键打开该账号的网页版续期弹窗（PC 扫码）。
   */
  const [webPrompt, setWebPrompt] = useState<PlatformAccount | null>(null);
  /**
   * 每个账号三个平台的登录态（设计 13 §18.5；2026-09-21 拆分：千帆 / 创作者 / 网页版）。
   *
   * **列表自带一份 L1/L2**（零平台请求）；进入页面 / 点刷新时再用带探针的结果
   * **覆盖**它 —— 覆盖而不是合并：探针轮的结果更全，合并会让
   * 「上一轮说已失效、这一轮说可用」这种新旧混在一张卡片上。
   * （v1.31 起「检查」按钮已移除，探活只剩这一个入口。）
   */
  const [loginChannels, setLoginChannels] = useState<Record<number, XhsLoginChannels>>({});
  /** 正在「重新登录 / 补齐令牌」的目标（打开续期弹窗的依据）。 */
  const [renewTarget, setRenewTarget] = useState<RenewTarget | null>(null);
  const [error, setError] = useState<string | null>(null);
  /**
   * 消息提醒统一走 antd `message`（设计 04 §3.6）：卡片里已经没有「同步任务」进度块
   * 了，同步触发失败 / 轮询到非正常终态这类**必须让用户知道**的事只能由轻提示承载
   * （失败三分法：真异常要给真原因，不能静默吞掉）。
   */
  const { message } = App.useApp();
  /**
   * 探活**在飞标记**（设计 13 §18.18.10）：同一时刻只允许一轮探活。
   *
   * 有两个来源会让同一批探活被重复触发：① 开发模式下 React StrictMode 的 effect
   * 双跑；② 用户连点「刷新」。重复那一轮**不会更快出结论**（后端要按账号逐个探），
   * 却会让平台请求数翻倍（风控面）—— 所以直接跳过，等已经在飞的那一轮回来。
   */
  const loginProbeInFlight = useRef(false);

  async function loadAccounts() {
    setIsLoading(true);
    setError(null);
    try {
      const loadedAccounts = await fetchAccounts("xhs");
      setAccounts(loadedAccounts);
      // 登录态随列表一起到（后端一次算完 L1/L2），前端不再逐卡片补问一次。
      setLoginChannels(
        Object.fromEntries(
          loadedAccounts.map((account) => [account.id, account.login_channels || {}])
        )
      );
      // 进入页面 / 点刷新 ⇒ **自动检查各平台令牌**（v1.30，用户 2026-09-21 要求）。
      // 列表自带的那份是 L1/L2（零平台请求），这里再补一次 L3 探活，让卡片一进来
      // 就是「现在还能不能用」而不是「还没检测」。不 await：探活要几秒钟，
      // 不能让整个页面卡在 loading 上等它。
      void autoCheckLoginStates();
      // 进入页面 / 点刷新 ⇒ **首查一次同步任务状态**（§35.17.4，2026-09-27 用户口径）。
      // 这是「根据第一个获取的信息决定是否需要轮询」里的那个第一个信息：命中未结束
      // ⇒ 入轮询集，命中已结束 / 没有任务 ⇒ 不入集。不 await：它每卡两次只读查询，
      // 不该把页面卡在 loading 上。
      void firstQuerySyncStatus(loadedAccounts);
    } catch {
      setError("账号列表加载失败。");
    } finally {
      setIsLoading(false);
    }
  }

  /**
   * 批量探活：一次请求问完所有账号的各平台令牌（≤3 次只读探针/账号）。
   *
   * **失败也安静**：探活没结论（网络 / 平台结构变了）就保持列表自带的 L1/L2，
   * 不编一个 `expired` 出来（§18.5 红线：`unknown` 不得渲染成「已失效」）。
   */
  async function autoCheckLoginStates() {
    // 已有一轮在飞 ⇒ 跳过（§18.18.10）：重复发不会更快，只会把平台请求数翻倍。
    if (loginProbeInFlight.current) {
      return;
    }
    loginProbeInFlight.current = true;
    try {
      const payload = await fetchXhsAccountLoginStates({ probe: true });
      const next: Record<number, XhsLoginChannels> = {};
      for (const item of payload.accounts) {
        next[item.platform_account_id] = item.channels;
      }
      setLoginChannels(next);
    } catch {
      // 见上：没结论就不改，卡片继续说「未检测」（刷新页面可再探一次）。
    } finally {
      loginProbeInFlight.current = false;
    }
  }

  /**
   * 同步账号级互动数据（粉丝 / 关注 / 获赞）。
   *
   * **只由用户显式点击触发**：不在页面加载时自动拉取平台数据（既有守卫见
   * `tests/backend/test_api.py::test_accounts_page_does_not_auto_check_accounts_on_load`）。
   * 失败原因就地展示在卡片里（`_silent`），不弹全局提示。
   *
   * v1.31（2026-09-21 用户口径）：
   * ① 取数一律走**网页版（www）通道**、打 PC 端接口（后端
   *    `/accounts/{id}/metrics` 已按此实现，设计 12 §21.4）—— 不再按账号
   *    类型区分，创作者账号带上网页会话同样取得到；
   * ② **PC 会话不可用时弹窗提示登录**（与千帆门禁同一条判据、同一个弹窗形态），
   *    不再只显示「创作者平台不返回互动数据，请改用网页版登录的账号查看」——
   *    那句话的归因已被真机抓包推翻（错归因让人永远不去补登录）。
   */
  async function handleSyncMetrics(account: PlatformAccount) {
    if (syncingAccountIds.has(account.id)) {
      return;
    }
    // 网页版会话「确定不可用」⇒ 不发起同步，先弹窗提示登录（unknown 放行，
    // 让同步真发一次；失败原因由后端给到卡片）。
    if (channelUnavailable(loginChannels[account.id], "www")) {
      setWebPrompt(account);
      return;
    }
    setSyncingAccountIds((current) => new Set(current).add(account.id));
    try {
      const { metrics_reason: reason, ...synced } = await syncAccountMetrics(account.id);
      setAccounts((current) => current.map((item) => (item.id === synced.id ? synced : item)));
      setMetricsNotes((current) => ({ ...current, [account.id]: reason || "" }));
    } catch (syncError) {
      setMetricsNotes((current) => ({ ...current, [account.id]: readErrorDetail(syncError) }));
    } finally {
      setSyncingAccountIds((current) => {
        const next = new Set(current);
        next.delete(account.id);
        return next;
      });
    }
  }

  /**
   * 点「同步笔记」：**创作者平台会话是硬门禁**（设计 13 §21.4，2026-09-22 用户口径：
   * 「同步笔记按钮该账号必须存在有效的创作者登录会话，不存在有效创作者平台登录
   * 会话的提示补齐后再触发」）。
   *
   * 决策本身在 `lib/sync-gates.ts::noteSyncPlan`（纯函数、有行为脚本守着）；
   * 这里只照做：创作者会话不可用 ⇒ 弹窗提示补齐；其余直接触发。
   *
   * `plan.needsLink` 现已**恒为 false**（www 源退场后主页链接录入窗没有用武之地），
   * 分支保留是为了恢复 www 源时不必重写 UI。
   */
  function handleNoteSyncClick(account: PlatformAccount) {
    const plan = noteSyncPlan(loginChannels[account.id], account.sub_type);
    if (plan.blocked) {
      setNotePrompt(account);
      return;
    }
    if (plan.needsLink) {
      setLinkInput("");
      setLinkAccount(account);
      return;
    }
    // §23：门禁通过后先让用户选「全量 / 时间区间」，不再直接触发。
    setSyncOptions({ kind: "notes", accountId: account.id, nickname: account.nickname });
  }

  /**
   * 点「同步商品」：千帆门禁（v1.31 / §19.4）→ 通过才触发。
   *
   * v1.31 起该按钮**对所有账号展示**（不再仅 Creator）—— 用户口径「底部按钮
   * 不再按照账号类型进行区分」。没有千帆会话的账号点了会被告知先补登，
   * 不会再像旧版那样把按钮藏起来（藏起来 = 用户不知道这个能力存在）。
   *
   * §19.4：文案按 ark 状态区分「失效」与「未登录」—— 用户原话
   * 「不可用失效或未登录则提示重新登录千帆后再次触发」。
   */
  function handleGoodsSyncClick(account: PlatformAccount) {
    if (goodsGate(loginChannels[account.id]).blocked) {
      setArkPrompt(account);
      return;
    }
    // §23：同笔记侧，先弹选项让用户决定同步范围。
    setSyncOptions({ kind: "goods", accountId: account.id, nickname: account.nickname });
  }

  /**
   * 点「同步**商品笔记**」：千帆门禁（**§44**，2026-09-30 用户拍板拆分）。
   *
   * 硬门禁 = **ark 令牌**（商品笔记源走 `qianfan_cookies_for_account` + `has_ark_token`，
   * 见设计 13 §44.2）—— 与「同步笔记」（creator 门禁）**不是同一个平台**，
   * 因此两条按钮各自拦各自的：creator 不可用**不该**拦住商品笔记，反之亦然。
   *
   * 决策本身在 `lib/sync-gates.ts::goodsNoteSyncPlan`（纯函数、有行为脚本守着）；
   * 这里只照做：ark 不可用 ⇒ 弹补登千帆提示；其余直接进选项弹窗。
   * `unknown` 一律放行（DEF-057：没问到 ≠ 不可用）。
   */
  function handleGoodsNoteSyncClick(account: PlatformAccount) {
    const plan = goodsNoteSyncPlan(loginChannels[account.id]);
    if (plan.blocked) {
      setGoodsNotePrompt(account);
      return;
    }
    // §23：门禁通过后先让用户选「全量 / 时间区间」，不再直接触发。
    setSyncOptions({ kind: "goods_notes", accountId: account.id, nickname: account.nickname });
  }

  /**
   * 触发笔记同步（§35.17：**手动触发定时任务 `system.note_sync` 的执行一次**）。
   *
   * ⚠️ **不再传 `profile_url` / `max_pages`**（设计 13 §21，2026-09-22 用户口径）：
   * www 源已不参与（creator 源强制）、翻页不再设上限。`profileUrl` 曾用于 www 链路的
   * URL 级 `xsec_token` —— 那个令牌**不是登录令牌**（是网页版主页 URL 上的查询参数），
   * 随 www 源一并退场。
   *
   * 触发响应本身就是「第一个获取的信息」：它带回任务摄要 ⇒ 卡片立刻把按钮切成
   * 「同步中」，不用等 5 秒后的第一轮轮询；入轮询集是为了**在任务结束时把按钮切回来**。
   *
   * ⚠️ **卡片不再展示任何进度 / 结果行**（2026-09-27 用户口径「点击后展示的同步任务
   * 进度卡移除，只需要将同步笔记按钮改成同步中即可」）⇒ 触发失败必须走轻提示报出真原因
   * （否则点了没反应）；成功不打扰，进度与结果去任务中心「任务日志」看。
   */
  async function startNoteSync(accountId: number, options?: SyncNotesOptions) {
    if (noteSyncIds.has(accountId)) {
      return;
    }
    try {
      const summary = await syncAccountNotes(accountId, options);
      if (summary.reused) {
        message.info("该账号已有同步任务在跑，本次未触发新执行。");
      }
      setNoteSyncIds((current) => new Set(current).add(accountId));
      // 记下这次是本页触发的：首查（它问的是点击之前的库状态）不许把刚入集的我摘掉。
      locallyTriggeredRef.current.add(accountId);
    } catch (syncError) {
      message.error(readErrorDetail(syncError));
    }
  }

  /** 触发店铺商品同步（**仅 Creator**；未绑定千帆时后端 400，原因由轻提示报出）。 */
  async function startGoodsSync(accountId: number, options?: SyncNotesOptions) {
    if (goodsSyncIds.has(accountId)) {
      return;
    }
    try {
      if (!hasPermission("xhs:account:syncShopGoods")) {
        message.warning("没有同步店铺商品的权限");
        return;
      }
      const summary = await syncShopGoods({ platformAccountId: accountId, ...(options ?? {}) });
      if (summary.reused) {
        message.info("该账号已有同步任务在跑，本次未触发新执行。");
      }
      setGoodsSyncIds((current) => new Set(current).add(accountId));
      // 同上：本页刚触发 ⇒ 首查不许摘（笔记 / 商品共用同一份竞态防护）。
      locallyTriggeredRef.current.add(accountId);
    } catch (syncError) {
      message.error(readErrorDetail(syncError));
    }
  }

  /**
   * 触发**商品笔记**同步（**§44**）：与 `startNoteSync` **逐字同构**，只换请求函数与集合。
   *
   * 同构是刻意的（DEF-044）：两条链路的差异只有后端任务类型（⇒ 源清单与硬门禁），
   * 防重 / 入集 / 竞态防护 / 失败报真原因的纪律必须一致，否则又会长出两套各自漂移的实现。
   *
   * ⚠️ 入的是**独立集合** `goodsNoteSyncIds`：与「同步笔记」各自 5s 轮询、各自按钮态
   * （合并回一个集合会让一个任务把另一个按钮也点亮）。
   */
  async function startGoodsNoteSync(accountId: number, options?: SyncNotesOptions) {
    if (goodsNoteSyncIds.has(accountId)) {
      return;
    }
    try {
      const summary = await syncGoodsNotes(accountId, options);
      if (summary.reused) {
        message.info("该账号已有商品笔记同步任务在跑，本次未触发新执行。");
      }
      setGoodsNoteSyncIds((current) => new Set(current).add(accountId));
      // 同上：本页刚触发 ⇒ 首查不许摘（三条链路共用同一份竞态防护）。
      locallyTriggeredRef.current.add(accountId);
    } catch (syncError) {
      message.error(readErrorDetail(syncError));
    }
  }

  /**
   * **首查**（§35.17.4，2026-09-27 用户口径）：进页面 / 点刷新时，对每张卡各查一次
   * 两个状态端点，**由第一个拿到的信息决定要不要轮询**。
   *
   * 三种结果对应三种处置（判据在 `lib/sync-gates.ts` 的 `syncTaskPhase`）：
   *
   * * `active`（未结束）⇒ **入轮询集**（后续 5s 一轮，按钮显示「同步中」）；
   * * `finished`（正常 / 非正常结束都算）⇒ **不入集**（不会再变，轮询纯浪费）；
   * * `idle`（没有对应任务）⇒ 不入集。
   *
   * 为什么必须有这一步：账号卡的「同步中」此前只由**本页点击**点亮 ⇒ 换台设备、
   * 刷新页面、或定时任务正在跑，卡片都显示成「空闲」—— 而任务其实在跑。
   */
  /**
   * 三条同步链路的**唯一接线表**（§44：`notes` 普通笔记 / `goods_notes` 商品笔记 /
   * `goods` 店铺商品）。首查、轮询、按钮态全部由它驱动 —— 新链路只在这里加一行。
   *
   * ⚠️ 商品笔记（`goods_notes`）与店铺商品（`goods`）**不是一回事**：前者落
   * `notes` + `note_goods`（千帆挂链笔记），后者落 `shop_products`（SKU / 价格）。
   */
  const syncChannels: SyncChannel[] = [
    {
      kind: "notes",
      fetchStatus: syncAccountNotesStatus,
      setIds: setNoteSyncIds,
      ids: noteSyncIds,
      unit: "条",
      formatResult: syncResultText,
    },
    {
      kind: "goods_notes",
      fetchStatus: syncGoodsNotesStatus,
      setIds: setGoodsNoteSyncIds,
      ids: goodsNoteSyncIds,
      unit: "条",
      formatResult: syncResultText,
    },
    {
      kind: "goods",
      fetchStatus: syncShopGoodsStatus,
      setIds: setGoodsSyncIds,
      ids: goodsSyncIds,
      unit: "件",
      formatResult: goodsResultText,
    },
  ];

  async function firstQuerySyncStatus(accountList: PlatformAccount[]) {
    await Promise.all(
      accountList.flatMap((account) =>
        syncChannels.map((channel) => querySyncStatus(channel.kind, account.id))
      )
    );
  }

  /**
   * 首查的单个账号 × 单条链路（三条链路同构，只差状态端点与集合）。
   *
   * 卡片上已经没有进度区块了，首查现在只回答**一件事**：这个账号此刻要不要继续轮询
   * （判据 `syncTaskPhase`：`active` ⇒ 入集；其余 ⇒ 不入集）。按钮文案由集合驱动，
   * 所以首查同时负责「刷新页面 / 换台设备 / 定时任务在跑」这几种情况下的按钮还原。
   */
  async function querySyncStatus(kind: SyncChannel["kind"], accountId: number) {
    const channel = syncChannels.find((item) => item.kind === kind);
    if (!channel) {
      // 防御性兜底：接线表里没有这条链路 ⇒ 不猜、不轮询（新链路忘了加行时不至于报错）。
      return;
    }
    const { setIds, fetchStatus } = channel;
    try {
      const status = await fetchStatus(accountId);
      if (syncTaskPhase(status) === "active") {
        // 它真的在跑 ⇒ 竞态窗口已过，交还给常规判据。
        locallyTriggeredRef.current.delete(accountId);
        setIds((current) => new Set(current).add(accountId));
      } else if (locallyTriggeredRef.current.has(accountId)) {
        // ⚠️ 本页刚触发过、首查却回 idle —— 这是**首查响应比点击更早**造成的旧快照
        // （它问的是点击之前的库状态）。此时摘除会让轮询当场停摆，正是 H9 那类
        // 「看着像在跑，其实再也没更新过」的 bug。留着，下一轮轮询会看到真状态。
      } else {
        // 结束 / 没有任务 ⇒ **摘出去**（首查就是那个「第一个获取的信息」）。
        setIds((current) => {
          if (!current.has(accountId)) return current;
          const next = new Set(current);
          next.delete(accountId);
          return next;
        });
      }
    } catch {
      // 首查失败**不入集也不报错**：它是进入页面的附带动作，问不到就让按钮停在
      // 「同步笔记」，用户点按钮时那条路会给出真原因（不在这里刷一条与当前操作无关的红字）。
    }
  }

  /**
   * 一轮轮询：把每个在跑账号的状态拉回来，**只为把按钮从「同步中」切回来**。
   *
   * **结束判据是「状态端点回 `idle`」，不是某个 `done`**：状态端点只返回
   * *进行中* 的任务，任务一结束它就变 `idle`，结果挪到 `last_result` 里
   * （设计 13 §4.2）。所以「看到 idle」= 「这一轮干完了」。
   *
   * ⚠️ **终态只在「没成功」时打扰用户**：成功不弹（进度 / 结果去任务中心「任务日志」
   * 看），`failed` / `cancelled` 用轻提示报一次真原因 —— 卡片里已经没有进度块了，
   * 静默吞掉失败等于用户点了半天不知道发生了什么（失败三分法）。
   */
  async function pollSyncTasks() {
    const pollOne = async (
      accountId: number,
      fetchStatus: (id: number) => Promise<XhsSyncTaskStatus>,
      unit: string,
      formatResult: (result: XhsSyncLastResult | null, unit: string) => string,
      setIds: (updater: (current: Set<number>) => Set<number>) => void,
    ) => {
      let status: XhsSyncTaskStatus | null = null;
      try {
        status = await fetchStatus(accountId);
        if (syncTaskPhase(status) === "active") {
          // 真的在跑 ⇒ 首查那个竞态窗口过去了，撤掉本页触发的保护标记。
          locallyTriggeredRef.current.delete(accountId);
          return;
        }
      } catch {
        // 轮询失败：问不到就摘出去停刷（不在这里刷红字 —— 与当前操作无关的报错是噪声）。
      }
      const notice = syncTerminalNotice(status?.last_result ?? null, unit, formatResult);
      if (notice) {
        message.error(notice);
      }
      // ⚠️ **摘除账号必须写在这里，不能写在 `finally` 里**：上面「仍在跑」的分支是
      // `return`，而 `finally` 对 `return` 一样生效 —— 写进 `finally` 就会在**第一轮**
      // 把账号移出集合，轮询当场停摆（症状是「看着像在跑，其实再也没更新过」）。
      // 只有真的到终态或这次查询失败才摘除。
      locallyTriggeredRef.current.delete(accountId);
      setIds((current) => {
        const next = new Set(current);
        next.delete(accountId);
        return next;
      });
    };

    await Promise.all(
      syncChannels.flatMap((channel) =>
        Array.from(channel.ids).map((accountId) =>
          pollOne(accountId, channel.fetchStatus, channel.unit, channel.formatResult, channel.setIds),
        ),
      ),
    );
    setSyncPollTick((tick) => tick + 1);
  }

  async function handleDelete(account: PlatformAccount) {
    // ⚠️ `centered: true` 必须**逐点显式传**：`Modal.confirm` 走的是静态方法分支
    //（`modal/confirm.js` 自建 ConfirmDialog），`ConfigProvider modal={{centered:true}}`
    //（`providers.tsx` 里给的是 ConfigContext）**到不了它** ⇒ 不传就还是顶部对齐。
    Modal.confirm({
      centered: true,
      title: "删除账号",
      content: `删除账号「${account.nickname || account.external_user_id || account.id}」？本地保存的账号 Cookie 也会移除。`,
      okText: "确认删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        setError(null);
        try {
          await deleteAccount(account.id);
          setAccounts((current) => current.filter((item) => item.id !== account.id));
        } catch {
          setError("账号删除失败。");
        }
      },
    });
  }

  useEffect(() => {
    void loadAccounts();
  }, []);

  /**
   * 同步任务轮询（设计 13 §6.2）：只要有账号在同步，就每 5 秒拉一次状态。
   *
   * 依赖里同时挂 `syncPollTick`：一轮轮询结束就 +1，让 effect 重新排下一次
   * timeout。**只依赖两个 Set 是不够的** —— 状态一直停在 `running` 时 Set
   * 内容不变，effect 不会再跑，轮询会静默停在第 2 次。
   * cleanup 负责卸载时真的停掉（否则离开页面后还在打后端）。
   */
  useEffect(() => {
    if (noteSyncIds.size === 0 && goodsNoteSyncIds.size === 0 && goodsSyncIds.size === 0) {
      return;
    }
    const timer = setTimeout(() => {
      void pollSyncTasks();
    }, SYNC_POLL_INTERVAL_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [noteSyncIds, goodsNoteSyncIds, goodsSyncIds, syncPollTick]);

  return (
    <div style={{ padding: "0 0 32px" }}>
      {/*
        ⚠️ **页面头部整块移除**（2026-09-28 用户口径：「将头部的 XHS Accounts / 账号矩阵 /
        管理小红书账号、各平台登录态与数据同步（笔记 / 商品 / 互动数据）。移除，
        绑定账号按钮放到右上角与刷新一起」）——设计 13 §40。

        原来那三行（eyebrow `XHS Accounts` + `账号矩阵` 标题 + 一句描述）是**重复信息**：
        「账号矩阵」这个标题与侧边菜单项**逐字相同**，描述那句说的正是下面卡片里
        已经摆着的东西（已绑定账号列表 + 三个同步按钮）。删掉之后页面从卡片开始，
        一屏能多看见一行账号卡。

        「绑定账号」不再是页面级的独立按钮，**收进卡片右上角与「刷新」同行** ——
        与设置页（`刷新` + `新增`）、任务中心（刷新从顶部标题区挪进卡片）同一套排布。
      */}

      {/* Section card */}
      <Card
        title={
          <span style={{ color: p.text, fontWeight: 600 }}>已绑定账号</span>
        }
        extra={
          <Space size={8}>
            <Button icon={<ReloadOutlined />} onClick={loadAccounts} loading={isLoading}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setDrawerOpen(true)}>
              绑定账号
            </Button>
          </Space>
        }
        style={{ background: p.cardBg, borderColor: p.border }}
        styles={{ body: { padding: 24 } }}
      >
        {error ? <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} /> : null}

        {isLoading ? (
          //: 2026-10-20 统一：等待一律走 `<WaitMask>`。
          <WaitMask open title="正在加载账号列表" />
        ) : accounts.length === 0 ? (
          <Empty
            image={<SafetyCertificateOutlined style={{ fontSize: 48, color: p.fillIcon }} />}
            imageStyle={{ height: 64 }}
            description={
              <Space direction="vertical" size={4}>
                <Text strong style={{ color: p.textSecondary }}>
                  还没有绑定小红书账号
                </Text>
                <Text style={{ color: p.textMuted, fontSize: 13 }}>
                  先绑定一个账号，即可同步笔记、店铺商品与互动数据。
                </Text>
              </Space>
            }
          >
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setDrawerOpen(true)}>
              添加账号
            </Button>
          </Empty>
        ) : (
          <Row gutter={[16, 16]}>
            {accounts.map((account) => {
              const isSyncing = syncingAccountIds.has(account.id);
              const metricsNote = metricsNotes[account.id] || "";
              const isCreator = account.sub_type === "creator";
              const isNoteSyncing = noteSyncIds.has(account.id);
              const isGoodsNoteSyncing = goodsNoteSyncIds.has(account.id);
              const isGoodsSyncing = goodsSyncIds.has(account.id);
              /** 时间行文案（同一份文本既用于显示、也用于 tip，避免两处各拼一次而漂移）。 */
              const updatedText = `更新时间：${formatDate(account.updated_at || account.created_at)}`;
              const statusColor = accountStatusColor(account.status) || "default";
              /**
               * 三个平台的登录态标签（设计 13 §18 / §18.14；2026-09-21 拆分），
               * 挂在昵称后面。
               *
               * 顺序 / 短名 / 文案 / 动作**全部来自 `lib/login-state.ts`**（唯一实现）——
               * 卡片这里只负责摆放。那条「`unknown` 不得渲染成已失效」的规则住在
               * 那个模块里，并被 `tests/frontend/login_state.cjs` 真实驱动过。
               */
              const loginRows = loginChannelRows(loginChannels[account.id]);

              return (
                <Col xs={24} sm={24} md={12} lg={8} key={account.id}>
                  <Card
                    size="small"
                    style={{
                      background: p.cardBg,
                      borderColor: isCreator ? "#303050" : p.border,
                      borderLeft: `3px solid ${isCreator ? "#722ed1" : "#1668dc"}`,
                    }}
                    styles={{ body: { padding: 20 } }}
                  >
                    {/*
                      Head：头像 + 名称（**名称后面挂三个平台的登录态标签**）+ 账号状态。
                      登录态标签放在这里而不是单开行（设计 13 §18.14，用户 2026-09-21 口径）：
                      登录态是**账号的属性**，贴着昵称读起来才像属性；单开行会占掉一张卡片
                      近三成的高度，却只说了两句话。
                    */}
                    <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 16 }}>
                      <Avatar
                        size={44}
                        src={account.avatar_url || undefined}
                        icon={!account.avatar_url ? <UserOutlined /> : undefined}
                        style={{ background: p.cardBgElevated, flexShrink: 0 }}
                      >
                        {!account.avatar_url ? (account.nickname?.slice(0, 1).toUpperCase() || "X") : undefined}
                      </Avatar>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        {/*
                          标签窄列里放不下时**整块换行**（`flexWrap`），而不是把昵称压没 ——
                          昵称是这张卡片的主标识，先保它；标签排到第二行仍然读得通，
                          昵称被截成「张」就彻底没用了。
                        */}                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 6,
                            flexWrap: "wrap",
                            rowGap: 4,
                          }}
                        >
                          <Text
                            strong
                            ellipsis
                            style={{
                              minWidth: 0,
                              maxWidth: "100%",
                              color: p.text,
                              fontSize: 15,
                            }}
                          >
                            {account.nickname || "未命名账号"}
                          </Text>
                          {/*
                            三个平台的标签：顺序 / 短名 / 状态文案 / 动作**全部来自
                            `lib/login-state.ts`**（唯一实现），卡片只负责摆放。
                            「`unknown` 不得渲染成已失效」那条规则住在那个模块里，
                            由 `tests/frontend/login_state.cjs` 真实驱动过。

                          可点击性由 `view.action` 决定，**不由状态名硬编码**：
                          只有「失效 / 未登录 / 非登录态」可点（用户 2026-09-21 口径），
                          点开就是对应平台的续期弹窗（§18.16 起与绑定账号弹窗同一套
                          登录方式矩阵：网页版/创作者 = 扫码 + 手机验证码，千帆 =
                          扫码 + 邮箱密码，默认扫码、可切换）。
                          */}
                          {loginRows.map((row) => {
                            const clickable = row.view.action === "login";
                            return (
                              <Tooltip key={row.channel} title={loginTagTip(row)}>
                                <Tag
                                  onClick={
                                    clickable
                                      ? () =>
                                          setRenewTarget({
                                            account,
                                            channel: row.channel,
                                            state: row.view.state,
                                          })
                                      : undefined
                                  }
                                  style={{
                                    marginRight: 0,
                                    flexShrink: 0,
                                    fontSize: 11,
                                    lineHeight: "18px",
                                    color: row.view.color,
                                    borderColor: row.view.color,
                                    background: "transparent",
                                    cursor: clickable ? "pointer" : "default",
                                  }}
                                >
                                  {row.shortLabel} {row.view.label}
                                </Tag>
                              </Tooltip>
                            );
                          })}
                        </div>
                        {/*
                          **小红书号跟在名称下面**（用户 2026-09-21 口径）。

                          原来这里放的是 `external_user_id`（一串 24 位 hex）—— 已撤掉，
                          换成 `red_id`：这才是使用者在小红书 App 里看到的「账号」，
                          有辨识度。信息块里不再重复放它（同一字段两处展示 = 两份会漂移的实现）。
                          缺值渲染 `-`（非计数字段的口径，硬约束 5）。
                        */}
                        <Text
                          type="secondary"
                          ellipsis
                          style={{ display: "block", fontSize: 12, color: p.textMuted }}
                        >
                          {profileValue(account, "red_id") || "-"}
                        </Text>
                      </div>
                      <Tag color={statusColor} style={{ marginRight: 0, flexShrink: 0 }}>
                        {accountStatusLabel(account.status) || account.status}
                      </Tag>
                    </div>

                    {/*
                      Info block — **一段 JSX 同时服务 PC 与 Creator**（R4）：
                      粉丝 / 关注 / 获赞数。
                      缺值渲染 "-" 而不是隐藏（项目原则「前端置灰而非隐藏」）：
                      隐藏会被当成 bug，"-" 明确表达「平台没有返回这个值」。
                      v1.4 起 Creator 的互动指标行不再被整行隐藏——两类卡片结构必须一致。

                      v1.30（用户 2026-09-21 口径）：**去掉「类型」**（PC / Creator）与
                      小红书号 —— 协议链路对使用者没有意义（他关心的是「这个号能干什么」，
                      而那由三个登录态 tag 与按钮集回答）；小红书号移到名称下面，
                      这里再放一遍就是同一字段两处展示。

                      v1.31（用户 2026-09-21 口径）：三项计数**均来自网页端**——
                      取数一律走 www 通道、打 PC 端接口（后端 `/accounts/{id}/metrics`，
                      设计 12 §21.4），不再按账号类型区分；创作者账号带上网页会话
                      同样取得到。「同步数据」按钮即该取数动作。
                    */}
                    <Row gutter={12} style={{ marginBottom: 8 }}>
                      <Col span={8}>
                        <Statistic
                          title={<span style={{ color: p.textTertiary, fontSize: 12 }}>粉丝</span>}
                          value={profileValue(account, "followers") ?? "0"}
                          valueStyle={{ color: p.text, fontSize: 14 }}
                        />
                      </Col>
                      <Col span={8}>
                        <Statistic
                          title={<span style={{ color: p.textTertiary, fontSize: 12 }}>关注</span>}
                          value={profileValue(account, "following") ?? "0"}
                          valueStyle={{ color: p.text, fontSize: 14 }}
                        />
                      </Col>
                      <Col span={8}>
                        <Statistic
                          title={<span style={{ color: p.textTertiary, fontSize: 12 }}>获赞数</span>}
                          value={profileValue(account, "likes") ?? "0"}
                          valueStyle={{ color: p.text, fontSize: 14 }}
                        />
                      </Col>
                    </Row>

                    {/* 三项计数缺值渲染 `0`（R10 / D16）：`0` 是**展示层默认值**，
                        「真 0」与「没取到」由下面这行原因区分（原因 25–30 字，来自后端
                        `metrics_reason`，前端不硬编码业务判断）。小红书号仍用 `-`——它不是计数。
                        **一行放不下就用 `…` 收起、悬停看全文**：原因文案是后端给的，
                        长度不由前端决定；换成换行会把卡片撑高、把按钮挤出可视区。
                        用 antd 的 `ellipsis={{ tooltip }}` —— 它**只在真被截断时**才出 tip，
                        没截断时不会平白多一个悬停气泡。 */}
                    {metricsNote ? (
                      <Text
                        type="secondary"
                        ellipsis={{ tooltip: metricsNote }}
                        style={{
                          display: "block",
                          fontSize: 12,
                          marginBottom: 12,
                          color: p.textMuted,
                        }}
                      >
                        {metricsNote}
                      </Text>
                    ) : null}

                    {/* Status message（平台原文，可能很长 —— 同样 `…` 收起 + tip 看全） */}
                    {account.status_message ? (
                      <Text
                        type="secondary"
                        ellipsis={{ tooltip: account.status_message }}
                        style={{ display: "block", fontSize: 12, marginBottom: 12, color: p.textMuted }}
                      >
                        {account.status_message}
                      </Text>
                    ) : null}

                    {/*
                      ⚠️ **「同步任务」进度区块整块移除**（2026-09-27 用户口径：
                      「点击后展示的同步任务进度卡移除，只需要将同步笔记按钮改成同步中即可」）。

                      原区块（设计 13 §35.17.4，标题 + 笔记 / 商品两行 + 行内取消）连同
                      驱动它的 `noteSyncNotes` / `goodsSyncNotes` / `*TaskIds` 一起删掉：
                      任务态**只有一处入口**——任务中心「任务日志」（账号卡两个按钮现在都
                      落一条 `trigger_type=manual` 的日志，§35.17）；中断入口在队列记录页。
                      卡片这一层只保留「同步中」这个按钮态，它由首查 + 5s 轮询维持。
                    */}

                    {/*
                      Footer：更新时间一行、按钮一行（设计 13 §6.1 的卡片草图就是这个排布）。
                      改成上下两行是因为 M1.9 把按钮从 3 个加到 5 个 —— 挤在一行时
                      窄列（lg=8）会把「同步店铺商品」压到换行位置，看起来像布局坏了。

                      v1.31（2026-09-21 用户口径）：按钮**不再按账号类型区分**，
                      全部账号统一展示「同步数据 / 同步笔记 / 同步商品笔记 / 同步商品 / 删除」；
                      「检查」按钮移除（登录态探活已由进入页面/刷新自动完成）。
                      （§44 于 2026-09-30 插入「同步商品笔记」— 原「同步笔记」是混合双源。）
                    */}
                    <div
                      style={{
                        paddingTop: 12,
                        borderTop: `1px solid ${p.border}`,
                      }}
                    >
                      {/* 时间行同样 `…` 收起 + tip 看全（窄列里日期常被挤成半截）。 */}
                      <Text
                        ellipsis={{ tooltip: updatedText }}
                        style={{
                          fontSize: 11,
                          color: p.textMuted,
                          display: "block",
                          marginBottom: 8,
                        }}
                      >
                        {updatedText}
                      </Text>
                      <Space size={4} wrap>
                        <Button
                          size="small"
                          icon={<SyncOutlined />}
                          onClick={() => void handleSyncMetrics(account)}
                          disabled={isSyncing}
                          title="同步粉丝 / 关注 / 获赞数（网页版数据，走 PC 端接口）"
                        >
                          {isSyncing ? "同步中" : "同步数据"}
                        </Button>
                        <Button
                          size="small"
                          icon={isNoteSyncing ? <SyncOutlined spin /> : <CloudDownloadOutlined />}
                          onClick={() => handleNoteSyncClick(account)}
                          disabled={isNoteSyncing}
                          title={`把该账号的普通笔记同步到本地库（需${loginChannelLabel("creator")}会话）`}
                        >
                          {isNoteSyncing ? "同步中" : "同步笔记"}
                        </Button>
                        {/*
                          「同步**商品笔记**」—— **§44**（2026-09-30 用户拍板拆分）新增。
                          千帆 `goods-note/list` 的**挂链笔记**（落 `notes` + `note_goods`），
                          与普通笔记是两条**独立任务 + 独立日志流 + 独立防重**。

                          ⚠️ **与右边那颗「同步商品」不是一回事**（文案必须带「笔记」二字）：
                          那颗同步的是**店铺商品台账**（SKU / 价格，落 `shop_products`）。
                          两件事的硬门禁同为千帆，但链路、落库表、任务类型全都不同。
                        */}
                        <Button
                          size="small"
                          icon={isGoodsNoteSyncing ? <SyncOutlined spin /> : <ShopOutlined />}
                          onClick={() => handleGoodsNoteSyncClick(account)}
                          disabled={isGoodsNoteSyncing}
                          title="把该账号的商品笔记同步到本地库（需千帆会话）"
                        >
                          {isGoodsNoteSyncing ? "同步中" : "同步商品笔记"}
                        </Button>
                        {/*
                          「同步商品」v1.31 起**所有账号都渲染**（旧版仅 Creator）。
                          千帆会话不可用时点击会弹窗提示先补登（见 handleGoodsSyncClick）——
                          按钮摆上去不再是「点了必错」：它给的是「缺什么」的可操作提示。
                        */}
                        <Button
                          size="small"
                          icon={isGoodsSyncing ? <SyncOutlined spin /> : <ShopOutlined />}
                          onClick={() => handleGoodsSyncClick(account)}
                          disabled={isGoodsSyncing}
                          title="把该店铺的商品同步到本地库（需千帆商家后台会话）"
                        >
                          {isGoodsSyncing ? "同步中" : "同步商品"}
                        </Button>
                        {/*
                          ⚠️ 「补齐详情」按钮**已移除**（2026-09-27 用户口径：
                          「账号信息卡中移除补齐详情逻辑，点击同步笔记时，应获取到当前笔记的
                          所有信息后再落库，在一个任务中完成，不需要再点击补齐详情」）。

                          原按钮 + 它背后的详情补齐队列（`POST /accounts/{id}/notes/hydrate-pending`、
                          `GET /notes/hydrate-queue`、`note_hydrate` 队列）整条链路一并删除 ——
                          补齐能力已并入「同步笔记」任务本身（设计 13 §35.18：任务收尾对该账号
                          全部待补笔记内联补齐，`_hydrate_page_notes` 那把尺子，含历史欠账）。
                        */}
                        <Button
                          size="small"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={() => void handleDelete(account)}
                          title="删除账号"
                        />
                      </Space>
                    </div>
                  </Card>
                </Col>
              );
            })}
          </Row>
        )}
      </Card>

      {/*
        PC 账号「同步笔记」的链接录入弹窗（设计 13 §6.1.1）。
        链接**只随本次请求走**：不落库、不进 localStorage —— 令牌会过期，
        存下来就是第二套真相源，还会让「上一次能用」误导下一次同步。
        取消 = 什么都不发（不打平台）；空链接时确定按钮 disabled。
      */}
      <Modal
                centered
                open={linkAccount !== null}
        title="同步笔记需要主页链接"
        okText="开始同步"
        cancelText="取消"
        okButtonProps={{ disabled: !linkInput.trim() }}
        onCancel={() => setLinkAccount(null)}
        onOk={() => {
          const account = linkAccount;
          if (!account) {
            return;
          }
          setLinkAccount(null);
          setLinkInput("");
          void startNoteSync(account.id);
        }}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            需要主页访问令牌（xsec_token）。请打开小红书网页版进入自己的主页，从浏览器地址栏复制完整链接后粘贴到下方。
          </Text>
          <Input
            placeholder="https://www.xiaohongshu.com/user/profile/...?xsec_token=..."
            value={linkInput}
            onChange={(event) => setLinkInput(event.target.value)}
            allowClear
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            链接只用于本次同步，不会保存。
          </Text>
        </Space>
      </Modal>

      {/*
        千帆会话不可用提示（v1.31 / §19.4，用户 2026-09-21 口径：
        「千帆登录令牌必须可用，不可用失效或未登录则提示重新登录千帆后再次触发」）。

        点「同步商品」时若 ark 状态是**确定不可用**
        （missing / guest / expired），不发起同步，先弹这个窗。
        「去补登千帆」直接打开该账号的 ark 续期弹窗（扫码为主、粘贴降级）；
        续期成功后登录态刷新，用户再次点击即可同步 —— **不自动开始同步**：
        扫码成功与同步意图是两件事，自动发起会在用户没预期的时候打平台。

        文案按状态区分（用户原话「失效或未登录」）：expired = 登录态失效；
        missing / guest = 还没登录过 / 只是游客态。

        `unknown`（未检测 / 探活没得到结论）**不弹这个窗**：那是「没问到」而不是
        「不可用」，放行让同步真发一次，失败原因由后端给到卡片（DEF-057 红线）。
      */}
      <Modal
                centered
                open={arkPrompt !== null}
        title="需要千帆商家后台会话"
        okText="去补登千帆"
        cancelText="取消"
        onCancel={() => setArkPrompt(null)}
        onOk={() => {
          const account = arkPrompt;
          setArkPrompt(null);
          if (account) {
            setRenewTarget({
              account,
              channel: "ark",
              state: channelState(loginChannels[account.id], "ark"),
            });
          }
        }}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Text style={{ fontSize: 13 }}>
            {arkPrompt && channelState(loginChannels[arkPrompt.id], "ark") === "expired"
              ? "千帆登录态已失效，同步商品需要可用的千帆令牌。请重新登录千帆后，再次触发同步商品。"
              : "尚未登录千帆商家后台，同步商品需要千帆登录令牌。请登录千帆后，再次触发同步商品。"}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            补登方式：扫码登录千帆，或粘贴千帆后台 Cookie。只更新本账号的千帆令牌，不影响其它平台。
          </Text>
        </Space>
      </Modal>

      {/*
        「同步**商品笔记**」的千帆会话不可用提示（**§44**，2026-09-30 用户拍板拆分）。

        与上面那个 `arkPrompt` 是**同一个门禁平台、两条不同链路**：这里说的是商品笔记
        （千帆 `goods-note/list` 的挂链笔记），上面说的是店铺商品台账。文案里的动作名
        必须写全「同步商品笔记」—— 只写「同步商品」会让用户去补登之后发现补错地方。

        `unknown` 一律不弹（DEF-057）：没问到 ≠ 不可用，放行让同步真发一次。
      */}
      <Modal
                centered
                open={goodsNotePrompt !== null}
        title={`需要${loginChannelLabel("ark")}会话`}
        okText="去补登千帆"
        cancelText="取消"
        onCancel={() => setGoodsNotePrompt(null)}
        onOk={() => {
          const account = goodsNotePrompt;
          setGoodsNotePrompt(null);
          if (account) {
            setRenewTarget({
              account,
              channel: "ark",
              state: channelState(loginChannels[account.id], "ark"),
            });
          }
        }}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Text style={{ fontSize: 13 }}>
            {goodsNotePrompt && channelState(loginChannels[goodsNotePrompt.id], "ark") === "expired"
              ? `千帆登录态已失效，同步商品笔记需要可用的${loginChannelLabel("ark")}令牌。请重新登录后，再次触发同步商品笔记。`
              : `尚未登录${loginChannelLabel("ark")}，同步商品笔记需要登录令牌。请登录后，再次触发同步商品笔记。`}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            补登方式：扫码登录千帆，或粘贴千帆后台 Cookie。只更新本账号的千帆令牌，不影响其它平台。
          </Text>
        </Space>
      </Modal>

      {/*
        「同步笔记」（普通笔记）的登录态缺失提示（§21.4 + **§44**）。

        ⚠️ **§44 起这条门禁只认创作者平台会话**：~~PC 网页版或千帆商家后台任何一个可用即可~~
        已被取代（www 源 §21.4 退场；商品笔记 §44 拆成独立任务）。所以这里不再提「两者都不可用」，
        也不再引导补千帆 —— 补千帆对普通笔记**没有用**，会把用户引到一次无效操作上。

        `unknown` 不弹（DEF-057）：没问到 ≠ 不可用，放行让同步真发一次，
        后端给的结构化原因由轻提示展示。
      */}
      <Modal
                centered
                open={notePrompt !== null}
        title={`同步笔记需要${loginChannelLabel("creator")}登录态`}
        okText="去登录"
        cancelText="取消"
        onCancel={() => setNotePrompt(null)}
        onOk={() => {
          const account = notePrompt;
          setNotePrompt(null);
          if (account) {
            // 直接开创作者平台的续期弹窗（硬门禁就是它；开别的平台等于让用户白跑一趟）。
            setRenewTarget({
              account,
              channel: "creator",
              state: channelState(loginChannels[account.id], "creator"),
            });
          }
        }}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Text style={{ fontSize: 13 }}>
            同步普通笔记需要{loginChannelLabel("creator")}登录会话，当前不可用。请先登录，再回到这里触发。
          </Text>
          <Text style={{ fontSize: 12 }} type="secondary">
            商品笔记（挂链笔记）走另一条链路，需先补登{loginChannelLabel("ark")} ——
            点「同步商品笔记」按钮单独补登。
          </Text>
        </Space>
      </Modal>

      {/*
        网页版（PC）会话不可用提示（v1.31，用户 2026-09-21 口径：
        「[同步数据]走pc获取粉丝关注获赞数，如果pc会话不可用则提示登录」）。

        粉丝 / 关注 / 获赞只存在于网页版接口，PC 会话不可用时点击「同步数据」
        **不发起同步**，先弹这个窗提示登录。「去登录」打开该账号的网页版续期
        弹窗（PC 扫码）；登录成功后登录态刷新，再次点击即可同步。

        与千帆门禁同一条判据（`channelUnavailable`）、同一个弹窗形态 ——
        `unknown`（未检测）放行：那是「没问到」而不是「不可用」（DEF-057 红线）。
      */}
      <Modal
                centered
                open={webPrompt !== null}
        title="需要网页版会话"
        okText="去登录"
        cancelText="取消"
        onCancel={() => setWebPrompt(null)}
        onOk={() => {
          const account = webPrompt;
          setWebPrompt(null);
          if (account) {
            setRenewTarget({
              account,
              channel: "www",
              state: channelState(loginChannels[account.id], "www"),
            });
          }
        }}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Text style={{ fontSize: 13 }}>
            粉丝 / 关注 / 获赞数来自网页版接口，当前网页版会话不可用。
            请先登录网页版，再回来同步。
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            登录方式：小红书网页版扫码。只更新本账号的网页版令牌，不影响其它平台。
          </Text>
        </Space>
      </Modal>

      {/*
        续期 / 补齐令牌弹窗（设计 13 §18）。扫码结果由后端写库，这里只做两件事：
        刷新这一个账号的登录态；**不动列表其它部分**（整页重载会让用户正在看的
        同步进度一起闪一下）。
      */}
      <RenewLoginModal
        target={renewTarget}
        onClose={() => setRenewTarget(null)}
        onRenewed={(accountId) => {
          void (async () => {
            try {
              // 不带探针：新令牌「存好了」是事实，「还能用」要再检测一次才算数
              // （§18.5：L2 过关不等于 available）。
              const state = await fetchXhsAccountLoginState(accountId, { probe: false });
              setLoginChannels((current) => ({ ...current, [accountId]: state.channels }));
            } catch {
              // 连本地状态都取不到（极少见）→ 退化成整页重载，至少不会让卡片停在旧值。
              await loadAccounts();
            }
          })();
        }}
      />

      <AddAccountDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} onBound={loadAccounts} />

      {/*
        §23 同步选项弹窗：门禁通过后由用户决定「全量 / 时间区间」。
        `destroyOnClose` 让每次打开都是默认态（不残留上一次的选择）。
      */}
      <SyncOptionsModal
        open={syncOptions !== null}
        title={
          syncOptions?.kind === "goods"
            ? `同步店铺商品 · ${syncOptions?.nickname ?? ""}`
            : syncOptions?.kind === "goods_notes"
              ? `同步商品笔记 · ${syncOptions?.nickname ?? ""}`
              : `同步笔记 · ${syncOptions?.nickname ?? ""}`
        }
        hint={
          syncOptions?.kind === "goods"
            ? "商品按上架/创建时间过滤；不选区间则翻完整个商品列表。"
            : syncOptions?.kind === "goods_notes"
              ? "商品笔记按发布时间过滤；不选区间则翻完整个商品笔记列表。"
              : "笔记按发布时间过滤；不选区间则翻完整个笔记列表。"
        }
        onCancel={() => setSyncOptions(null)}
        onConfirm={(options: SyncOptions) => {
          const target = syncOptions;
          setSyncOptions(null);
          if (!target) return;
          const payload: SyncNotesOptions =
            options.scope === "range"
              ? { scope: "range", startDate: options.startDate, endDate: options.endDate }
              : { scope: "full" };
          if (target.kind === "notes") {
            void startNoteSync(target.accountId, payload);
          } else if (target.kind === "goods_notes") {
            // §44：商品笔记走独立端点（`POST /accounts/{id}/goods-notes/sync`）。
            void startGoodsNoteSync(target.accountId, payload);
          } else {
            void startGoodsSync(target.accountId, payload);
          }
        }}
      />
    </div>
  );
}
