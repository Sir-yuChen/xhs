import {
  Alert,
  App,
  Avatar,
  Button,
  Card,
  Empty,
  Flex,
  Modal,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  DeleteOutlined,
  PlusOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SyncOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { useEffect, useRef, useState } from "react";

import { AddAccountDrawer } from "../../../components/account/add-account-drawer";
import { RenewLoginModal, type RenewTarget } from "../../../components/account/renew-login-modal";
import { useThemePalette } from "../../../app/providers";
import {
  deleteAccount,
  fetchAccountsPage,
  fetchXhsAccountLoginState,
  fetchXhsAccountLoginStates,
  syncAccountMetrics,
  readErrorDetail,
} from "../../../lib/api";
import { loginChannelRows, loginTagTip } from "../../../lib/login-state";
import {
  channelState,
  channelUnavailable,
} from "../../../lib/sync-gates";
import { formatShanghaiTime } from "../../../lib/time";
import type { PlatformAccount, XhsLoginChannels } from "../../../types";
import { WaitMask } from "../../../components/ui/wait-mask";

const { Text } = Typography;

/** 每页条数（无分页器，滚到底再拉下一页；后端 `page_size` 上限 100）。 */
const ACCOUNT_PAGE_SIZE = 50;

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

/**
 * 「说明」列的原因 —— **只在用户能做点什么时给一句，否则留空**。
 *
 * 判据：
 * - 有任一计数 ⇒ 空（没有要解释的事）
 * - 网页版会话无效 ⇒ 空（结构上就取不到这个数，不是异常）
 * - 会话有效但库里没数 ⇒ 提示点「同步数据」
 */
function accountDataHint(
  account: PlatformAccount,
  channels: XhsLoginChannels | undefined
): string {
  const hasAnyMetric =
    profileValue(account, "followers") !== null ||
    profileValue(account, "following") !== null ||
    profileValue(account, "likes") !== null;
  if (hasAnyMetric) {
    return "";
  }
  //: 网页版会话无效 ⇒ 这三个数**结构上取不到**（后端 `fetch_account_metrics` 在没有
  //: `web_session` 时零平台请求）。不是错误，不解释。
  if (channelState(channels, "www") !== "valid") {
    return "";
  }
  return "尚未同步，点「同步数据」获取";
}

function accountNumber(account: PlatformAccount, key: string): number {
  const value = profileValue(account, key);
  if (value === null) {
    return 0;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function metricCell(
  account: PlatformAccount,
  channels: XhsLoginChannels | undefined,
  key: string
): number | string {
  //: 只有网页版会话**确定可用**才显示数字（2026-10-27 用户口径「网页版登录态有效则才展示」）。
  //: `unknown`（没探过）按「无数据」处理 —— 没确认能取到，就不摆一个可能过期的数字。
  if (channelState(channels, "www") !== "valid") {
    return "-";
  }
  return accountNumber(account, key);
}

/**
 * 表头 + 说明图标（`Tooltip` 包 `QuestionCircleOutlined`）。
 *
 * 登录态列必须自解释：三个平台各自独立判定、`未检测` ≠ `已失效`，
 * 不写清就会让人去做一次不需要的重登。
 */
function HeaderWithHint({ title, hint }: { title: string; hint: string }) {
  return (
    <Space size={4}>
      <span>{title}</span>
      <Tooltip title={hint} styles={{ root: { maxWidth: 420 } }}>
        <QuestionCircleOutlined
          style={{ cursor: "help", opacity: 0.55, fontSize: 12 }}
          //: 表头文字不可选中是 antd 默认行为，这里只保证图标本身可聚焦（可键盘触达）。
          tabIndex={0}
        />
      </Tooltip>
    </Space>
  );
}

/**
 * 平台门禁判据 / 三态决策 / 结果行文案都在 `lib/sync-gates.ts`（§19）——
 * 纯函数、可被行为脚本直接驱动；本页面只留接线。
 */

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
  /** 刷新等待态。与 `isLoading`（首次进入的整页占位）分开：刷新时表格已有内容，只盖蒙版。 */
  const [refreshing, setRefreshing] = useState(false);
  /** 刷新耗时（秒），喂给 `WaitMask` 的 `elapsed`。 */
  const [refreshElapsed, setRefreshElapsed] = useState(0);
  /** 行内「同步数据」的等待耗时（秒）—— 与整表刷新**分开**，两者可能先后发生。 */
  const [metricsWaitElapsed, setMetricsWaitElapsed] = useState(0);
  const [syncingAccountIds, setSyncingAccountIds] = useState<Set<number>>(() => new Set());
  /** 每个账号最近一次「互动数据」的结果说明（不可得原因 / 同步失败原因），只在卡片内展示。 */
  const [metricsNotes, setMetricsNotes] = useState<Record<number, string>>({});
  /**
   * 网页版会话不可用时提示登录的目标账号。
   *
   * 点「同步数据」时若 www 状态是「确定不可用」，不发起同步，先弹窗提示登录；
   * 弹窗内可一键打开该账号的网页版续期弹窗（PC 扫码）。
   */
  const [webPrompt, setWebPrompt] = useState<PlatformAccount | null>(null);
  /** 每个账号三个平台的登录态（千帆 / 创作者 / 网页版，各自独立判定）。
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
  /** 轻提示统一走 antd `message`；失败必须给出真原因，不能静默吞掉。 */
  const { message } = App.useApp();
  /** 探活在飞标记：同一时刻只允许一轮。
   *
   * 有两个来源会让同一批探活被重复触发：① 开发模式下 React StrictMode 的 effect
   * 双跑；② 用户连点「刷新」。重复那一轮**不会更快出结论**（后端要按账号逐个探），
   * 却会让平台请求数翻倍（风控面）—— 所以直接跳过，等已经在飞的那一轮回来。
   */
  const loginProbeInFlight = useRef(false);
  /** 已加载到第几页（无限下拉游标）+ 后端总条数。 */
  const [total, setTotal] = useState(0);
  const [loadedPage, setLoadedPage] = useState(1);
  /** 追加下一页进行中（三重刹车之一，防滚到底重复请求）。 */
  const [loadingMore, setLoadingMore] = useState(false);
  /** 已勾选的账号 id（`rowSelection`）。刷新时清空 —— 列表换内容后旧 id 会指向看不见的行。 */
  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set());

  /**
   * 拉第一页列表。
   *
   * :param skipProbe: 刷新路径传 `True` —— 探活由 `refreshAccounts()` 自己跑两轮并
   *     判定「是否无效」，这里再探一次就变成三轮、且判定逻辑分两处。
   */
  async function loadAccounts(skipProbe = false) {
    //: 刷新时表格已有内容 ⇒ 不显示首次进入的整页占位（改由 `WaitMask` 盖住）。
    if (!skipProbe) {
      setIsLoading(true);
    }
    setError(null);
    //: 重新加载 ⇒ 勾选清空（列表内容换了，旧选中会指向看不见甚至已不存在的行）。
    setSelectedIds(new Set());
    try {
      const res = await fetchAccountsPage("xhs", 1, ACCOUNT_PAGE_SIZE);
      setAccounts(res.items);
      setTotal(res.total);
      setLoadedPage(1);
      // 登录态随列表一起到（后端一次算完 L1/L2），前端不再逐卡片补问一次。
      setLoginChannels(
        Object.fromEntries(
          res.items.map((account) => [account.id, account.login_channels || {}])
        )
      );
      // 进入页面 / 点刷新 ⇒ **自动检查各平台令牌**（v1.30，用户 2026-09-21 要求）。
      // 列表自带的那份是 L1/L2（零平台请求），这里再补一次 L3 探活，让列表一进来
      // 就是「现在还能不能用」而不是「还没检测」。不 await：探活要几秒钟，
      // 不能让整个页面卡在 loading 上等它。
      if (!skipProbe) {
        void autoCheckLoginStates();
      }
    } catch {
      setError("账号列表加载失败。");
    } finally {
      setIsLoading(false);
    }
  }

  /**
   * 追加下一页。三重刹车：`hasMore` + `loadingMore` + 哨兵存在。
   * 去重按 `id`（翻页期间删除会让下一页与前页重叠）。
   */
  async function loadMore() {
    if (loadingMore || accounts.length >= total) {
      return;
    }
    setLoadingMore(true);
    try {
      const next = loadedPage + 1;
      const res = await fetchAccountsPage("xhs", next, ACCOUNT_PAGE_SIZE);
      setAccounts((current) => {
        const seen = new Set(current.map((item) => item.id));
        return [...current, ...res.items.filter((item) => !seen.has(item.id))];
      });
      // 登录态按 id 合并（同样只补缺，不覆盖已有探活结果 —— 探活比 L1/L2 更新）。
      setLoginChannels((current) => {
        const next: Record<number, XhsLoginChannels> = { ...current };
        for (const item of res.items) {
          if (!next[item.id]) next[item.id] = item.login_channels || {};
        }
        return next;
      });
      setTotal(res.total);
      setLoadedPage(next);
    } catch {
      // 静默：滚动加载失败不打断页面，用户再滚一次会重试。
    } finally {
      setLoadingMore(false);
    }
  }

  /**
   * 批量探活：一次请求问完所有账号的各平台令牌（≤3 次只读探针/账号）。
   *
   * **失败也安静**：探活没结论（网络 / 平台结构变了）就保持列表自带的 L1/L2，
   * 不编一个 `expired` 出来（§18.5 红线：`unknown` 不得渲染成「已失效」）。
   */
  async function autoCheckLoginStates(): Promise<"ok" | "failed"> {
    // 已有一轮在飞 ⇒ 跳过（§18.18.10）：重复发不会更快，只会把平台请求数翻倍。
    if (loginProbeInFlight.current) {
      return "ok";
    }
    loginProbeInFlight.current = true;
    try {
      const payload = await fetchXhsAccountLoginStates({ probe: true });
      const next: Record<number, XhsLoginChannels> = {};
      for (const item of payload.accounts) {
        next[item.platform_account_id] = item.channels;
      }
      setLoginChannels(next);
      return "ok";
    } catch {
      // 见上：没结论就不改，卡片继续说「未检测」（刷新页面可再探一次）。
      return "failed";
    } finally {
      loginProbeInFlight.current = false;
    }
  }

  /**
   * 刷新 = 列表 + 两次探活，全程由 `WaitMask` 盖住。
   *
   * ## 为什么重试一次而不是直接报失败
   *
   * 探活要真打平台（每账号 ≤3 次只读），**第一次失败很可能只是网络抖动**（后端本身
   * 已有 3 次重试 × 10s 超时，能到前端失败说明那一轮整体没结论）。直接说「无效」
   * 会把一次瞬时故障说成账号问题，用户会去做一次根本不需要的重登（DEF-057 同型）。
   * 所以再探一次：
   *
   * * 任一次成功 ⇒ 用成功那次的结果；
   * * **两次都失败** ⇒ 判定「探活无效」，提示**刷新重试**（不是「登录失效」，
   *   因为我们并没有拿到平台「拒绝」的答复，只是没探出结论）。
   *
   * ⚠️ 不把失败写成 `expired`：`unknown` 不得渲染成「已失效」是既有红线
   * （设计 13 §18.5）。这里的失败只走**顶部提示**，不动每行的登录态列。
   */
  async function refreshAccounts() {
    if (refreshing) {
      return;
    }
    setRefreshing(true);
    setRefreshElapsed(0);
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setRefreshElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    try {
      await loadAccounts(true);
      //: 两次探活：第一次失败才做第二次（成功就不多打一轮平台请求）。
      let first: "ok" | "failed" = await autoCheckLoginStates();
      let second: "ok" | "failed" | null = null;
      if (first === "failed") {
        second = await autoCheckLoginStates();
      }
      if (first === "failed" && second === "failed") {
        //: 两次都没有结论 —— 说清是「探活没结果」，并给出唯一可执行动作（再刷新）。
        message.warning("登录态探活两次均未得到结论，请刷新重试");
      }
    } finally {
      window.clearInterval(timer);
      setRefreshing(false);
    }
  }

  /**
   * 同步粉丝 / 关注 / 获赞（只由用户点击触发，不在加载时打平台）。
   * 取数一律走网页版通道；网页版会话不可用时弹窗提示登录。
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
    //: 行内刷新也要有**公共等待组件**（2026-10-27 用户反馈：只转图标、
    //: 既没有蒙版也没有结果提示，用户不知道点没点上、成没成）。
    //: 与整表「刷新」共用同一个 `WaitMask`，文案按动作区分。
    setMetricsWaitElapsed(0);
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setMetricsWaitElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    try {
      const { metrics_reason: reason, ...synced } = await syncAccountMetrics(account.id);
      setAccounts((current) => current.map((item) => (item.id === synced.id ? synced : item)));
      setMetricsNotes((current) => ({ ...current, [account.id]: reason || "" }));
      //: 结果必须**说出来**（三种结局分别给话，不许都变成「成功」）：
      //: 取到数 / 平台没给数（带后端可读原因）/ 请求本身失败。
      if (reason) {
        message.warning(`同步完成，但未取到数据：${reason}`);
      } else {
        const noun = account.nickname || `#${account.id}`;
        message.success(`「${noun}」粉丝 / 关注 / 获赞已更新`);
      }
    } catch (syncError) {
      const detail = readErrorDetail(syncError);
      setMetricsNotes((current) => ({ ...current, [account.id]: detail }));
      message.error(`同步失败：${detail}`);
    } finally {
      window.clearInterval(timer);
      setSyncingAccountIds((current) => {
        const next = new Set(current);
        next.delete(account.id);
        return next;
      });
    }
  }


  /**
   * 删除账号 —— 点击即删，不弹二次确认。
   *
   * ⚠️ 语义没变：后端是**软删**（`deleted_at` 打时间戳，`notes` 等内容库数据不丢，
   * Cookie 版本物理删除）。重新导入同一账号会**复活**同一行（`action="restored"`），
   * 所以「误点删除」是可挽回的 —— 这是敢去掉二次确认的前提。
   * 失败仍报出来（`setError` 走卡片顶部 Alert），不静默吞掉。
   */
  async function handleDelete(account: PlatformAccount) {
    setError(null);
    try {
      await deleteAccount(account.id);
      setAccounts((current) => current.filter((item) => item.id !== account.id));
      setTotal((current) => Math.max(0, current - 1));
      //: 勾选集里同步摘掉（否则批量条的「已选 N」会把已删的行算进去）。
      setSelectedIds((current) => {
        if (!current.has(account.id)) return current;
        const next = new Set(current);
        next.delete(account.id);
        return next;
      });
    } catch {
      setError("账号删除失败。");
    }
  }

  useEffect(() => {
    void loadAccounts();
  }, []);

  /** 勾选 / 取消一个账号。 */
  function toggleSelected(accountId: number, checked: boolean) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (checked) {
        next.add(accountId);
      } else {
        next.delete(accountId);
      }
      return next;
    });
  }

  /** 全选 / 取消全选（只作用于**已加载**的行 —— 未加载的看不见，勾它等于暗箱操作）。 */
  function toggleSelectAll(checked: boolean) {
    setSelectedIds(checked ? new Set(accounts.map((account) => account.id)) : new Set());
  }

  /**
   * 批量删除勾选的账号。
   *
   * 逐个调用 `deleteAccount`（后端只有单条端点，**没有**批量删除入口 —— 不为这个按钮
   * 新开一条端点）。回执按四态报账（本仓硬约束 6 的口径）：成功几个、失败几个，
   * **失败必须报出真原因**，不许静默吞掉。
   */
  async function handleBatchDelete() {
    const targets = accounts.filter((account) => selectedIds.has(account.id));
    if (targets.length === 0) {
      return;
    }
    //: **不弹二次确认**（与单条删除同一口径，2026-10-27 用户口径）。
    setError(null);
    let ok = 0;
    //: 失败项按 **id** 记（名称可能重复 / 为空，用它回查会认错行）。
    const failedIds = new Set<number>();
    const failedNames: string[] = [];
    for (const account of targets) {
      try {
        await deleteAccount(account.id);
        ok += 1;
      } catch {
        failedIds.add(account.id);
        failedNames.push(account.nickname || `#${account.id}`);
      }
    }
    //: 本地列表立刻收口（成功的移除、失败的原样留着）—— 不整页重载，免闪。
    setAccounts((current) =>
      current.filter((item) => !selectedIds.has(item.id) || failedIds.has(item.id))
    );
    setTotal((current) => Math.max(0, current - ok));
    setSelectedIds(new Set());
    //: 回执必须报出真原因（不许静默吞掉失败）。
    if (failedNames.length > 0) {
      message.error(`删除失败 ${failedNames.length} 个：${failedNames.join("、")}`);
    } else {
      message.success(`已删除 ${ok} 个账号`);
    }
  }



  return (
    <Flex vertical style={{ height: "calc(100vh - 87px)" }}>
      {/*
        页面形态：antd `Table` + `pagination={false}`（表体滚到底自动追加下一页）
        + 原生 `rowSelection`（不自研 Checkbox）；高度链用共享类 `.fill-table`。
      */}

      <Card
        title={
          //: 与「字典管理」页同一写法（`标题（N）`）：标题即计数，不在别处重复。
          <span style={{ color: p.text, fontWeight: 600 }}>
            账号管理（{total}）
          </span>
        }
        extra={
          <Space size={8}>
            {/* 勾选后出现批量操作（与 `rowSelection` 配套）。 */}
            {selectedIds.size > 0 ? (
              <>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  已选 {selectedIds.size} 个
                </Text>
                <Button size="small" danger icon={<DeleteOutlined />} onClick={() => void handleBatchDelete()}>
                  批量删除
                </Button>
                <Button size="small" type="link" onClick={() => setSelectedIds(new Set())}>
                  取消选择
                </Button>
              </>
            ) : null}
            {/* 刷新走 `refreshAccounts()`：列表 + **两次探活**，全程被 `WaitMask` 盖住。
                用户 2026-10-27：「点击刷新添加公共等待组件，等待刷新结果」+
                「做两次探查，全部失败判断无效提示刷新」。 */}
            <Button
              icon={<ReloadOutlined spin={refreshing} />}
              onClick={() => void refreshAccounts()}
              loading={isLoading}
              disabled={refreshing}
            >
              {refreshing ? "刷新中…" : "刷新"}
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setDrawerOpen(true)}>
              绑定账号
            </Button>
          </Space>
        }
        style={{
          background: p.cardBg,
          borderColor: p.border,
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
        }}
        //: body 必须是撑满的 flex 项，否则 `.fill-table` 拿不到确定高度（高度链断）。
        styles={{
          //: `position: relative` 是 `WaitMask scope="panel"` 的前提 —— 没有它，蒙版会往上
          //: 找到 body 的定位祖先，变成整页蒙版（组件自己的注释里也标了这条）。
          body: { position: "relative", padding: 24, flex: 1, minHeight: 0, display: "flex", flexDirection: "column" },
        }}
      >
        {error ? <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} /> : null}

        {/*: 刷新等待（2026-10-27）：公共组件 `WaitMask`，带耗时 + 「请勿重复点击」。 */}
        <WaitMask
          open={refreshing}
          scope="panel"
          title="正在刷新账号与登录态…"
          elapsed={refreshElapsed}
          sub="正在对上每个账号的令牌做探活，请勿重复点击"
        />
        {/*: 行内「同步数据」的等待 —— 同一个公共组件，文案按动作区分。 */}
        <WaitMask
          open={syncingAccountIds.size > 0}
          scope="panel"
          title="正在同步粉丝 / 关注 / 获赞…"
          elapsed={metricsWaitElapsed}
          sub="正在向网页版取数，请勿重复点击"
        />

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
                  先绑定一个账号，即可查看各平台登录态与粉丝互动数据。
                </Text>
              </Space>
            }
          >
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setDrawerOpen(true)}>
              添加账号
            </Button>
          </Empty>
        ) : (
          //: `.fill-table` = 共享三区布局类（表体内部滚动 / 底部钉住），不要手写拷贝。
          <div className="fill-table">
            {/*
              **无限下拉的滚动宿主**：`.fill-table` 只负责高度链，真正的滚动发生在
              `.ant-table-body` 里（`scroll={{ y: "100%" }}` 让 antd 自己建那个滚动容器）。

              ⚠️ 滚动事件必须挂在**表体**上，不能挂外层 div：外层没有 `overflow`，
              它永远不会产生 `scroll` 事件 ⇒ 无限下拉静默失效。
              所以用 antd `Table` 官方的 `onScroll`（它把事件从 `.ant-table-body` 透出来）。
            */}
            <Table<PlatformAccount>
              rowKey="id"
              size="small"
              dataSource={accounts}
              //: **不渲染分页器**（用户口径「tabe默认无限不使用分页」）。
              pagination={false}
              //: `scroll.y="100%"` 让表体吃满高度并**由 antd 建立滚动容器**。
              scroll={{ x: 1240, y: "100%" }}
              /**
               * 滚到底 ⇒ 追加下一页。
               *
               * ⚠️ 为什么只认 `table-body` 那一个目标：表头 `table-header` 与固定列容器
               * （`table-sticky-*`）也会冒泡 `scroll`，但它们是**同步滚动**的镜像层 ——
               * 对它们触发的加载会让同一次滚动请求两三次（实测本仓商品列表踩过）。
               */
              onScroll={(event) => {
                const target = event.target as HTMLElement;
                if (!target.classList?.contains("ant-table-body")) return;
                if (target.scrollTop + target.clientHeight >= target.scrollHeight - 24) {
                  void loadMore();
                }
              }}
              //: **原生复选框**（antd `Table` 的 rowSelection）—— 不自研 Checkbox。
              rowSelection={{
                selectedRowKeys: Array.from(selectedIds),
                onChange: (keys) => setSelectedIds(new Set(keys.map(Number))),
                //: 只允许勾**已加载**的行（未加载的看不见，勾它等于暗箱操作）。
                columnWidth: 48,
              }}
              columns={[
                {
                  title: "账号",
                  dataIndex: "nickname",
                  width: 260,
                  fixed: "left",
                  render: (_: unknown, account: PlatformAccount) => (
                    <Flex align="center" gap={10} style={{ minWidth: 0 }}>
                      <Avatar
                        size={32}
                        src={account.avatar_url || undefined}
                        icon={!account.avatar_url ? <UserOutlined /> : undefined}
                        style={{ background: p.cardBgElevated, flexShrink: 0 }}
                      >
                        {!account.avatar_url
                          ? account.nickname?.slice(0, 1).toUpperCase() || "X"
                          : undefined}
                      </Avatar>
                      <Flex vertical style={{ minWidth: 0 }}>
                        <Text strong ellipsis style={{ color: p.text, fontSize: 13 }}>
                          {account.nickname || "未命名账号"}
                        </Text>
                        {/* 小红书号用 red_id（用户可见的账号）；缺值 `-`，不是计数。 */}
                        <Text type="secondary" ellipsis style={{ fontSize: 11, color: p.textMuted }}>
                          {profileValue(account, "red_id") || "-"}
                        </Text>
                      </Flex>
                    </Flex>
                  ),
                },
                {
                  title: (
                    <HeaderWithHint
                      title="登录态"
                      hint={
                        `① 三个平台各自一条令牌，各自判定、各自续期
② 判定：存没存过 → 是不是登录态 → 还能不能用
③ 可用 / 已失效 = 本次刷新页面探活的结论
④ 未检测 = 没探过，或没拿到平台答复（网络问题不算失效）
⑤ 点彩色标签可单独续期该平台`
                      }
                    />
                  ),
                  dataIndex: "login_channels",
                  width: 330,
                  render: (_: unknown, account: PlatformAccount) => (
                    //: 顺序 / 短名 / 文案 / 动作全部来自 `lib/login-state.ts`（唯一实现）；
                    //: 可点击性由 `view.action` 决定，**不由状态名硬编码**（2026-09-21 口径）。
                    <Space size={4} wrap>
                      {loginChannelRows(loginChannels[account.id]).map((row) => {
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
                    </Space>
                  ),
                },
                {
                  title: "粉丝",
                  dataIndex: "followers",
                  width: 88,
                  align: "right",
                  render: (_: unknown, account: PlatformAccount) =>
                    metricCell(account, loginChannels[account.id], "followers"),
                },
                {
                  title: "关注",
                  dataIndex: "following",
                  width: 88,
                  align: "right",
                  render: (_: unknown, account: PlatformAccount) =>
                    metricCell(account, loginChannels[account.id], "following"),
                },
                {
                  title: "获赞数",
                  dataIndex: "likes",
                  width: 96,
                  align: "right",
                  render: (_: unknown, account: PlatformAccount) =>
                    metricCell(account, loginChannels[account.id], "likes"),
                },
                {
                  title: "更新时间",
                  dataIndex: "updated_at",
                  width: 150,
                  render: (_: unknown, account: PlatformAccount) => (
                    <Text
                      type="secondary"
                      style={{ fontSize: 12, color: p.textMuted, whiteSpace: "nowrap" }}
                    >
                      {formatDate(account.updated_at || account.created_at)}
                    </Text>
                  ),
                },
                {
                  title: "说明",
                  dataIndex: "metrics_note",
                  //: 只放能指导用户动作的原因，逐级回落：
                  //: ① 本次同步结果（后端 `metrics_reason`）；② `accountDataHint`；③ 空 ⇒ `-`。
                  ellipsis: true,
                  render: (_: unknown, account: PlatformAccount) => {
                    const note =
                      metricsNotes[account.id] ||
                      accountDataHint(account, loginChannels[account.id]);
                    return note ? (
                      <Text type="secondary" ellipsis={{ tooltip: note }} style={{ fontSize: 12 }}>
                        {note}
                      </Text>
                    ) : (
                      <Text type="secondary" style={{ fontSize: 12, color: p.textMuted }}>
                        -
                      </Text>
                    );
                  },
                },
                {
                  title: "操作",
                  key: "actions",
                  width: 160,
                  fixed: "right",
                  render: (_: unknown, account: PlatformAccount) => {
                    const isSyncing = syncingAccountIds.has(account.id);
                    return (
                      <Space size={4}>
                        {/*
                          两个操作都做成**纯图标**（用户 2026-10-27 口径「撤回同步数据按钮的
                          逻辑，把文本改成刷新图标，删除也改成刷新图标，都已图标形式展示」）。

                          ⚠️ 撤回的是「按网页版会话显隐」那条逻辑 —— 按钮恢复**常显**。
                          图标按钮没有可见文字，`title` 就是唯一的可读说明（悬停提示），
                          所以两处 `title` 必须写全动作名，不能只写「同步」「删除」。
                        */}
                        {/*
                          **没有网页版登录态 ⇒ 不显示刷新图标**（2026-10-27 用户口径
                          「没有登录网页版，刷新数据的按钮图标不展示」）。

                          为什么「隐藏」而不是「置灰」：这三个数**只来自网页版接口**
                          （后端 `fetch_account_metrics` 没有 `web_session` 时**零平台请求**，
                          直接返回原因）—— 没有会话时这个按钮**结构上不可能成功**。
                          摆一个永远失败的按钮，用户只会反复点、反复失望；
                          不显示则直接对应「这个号没有网页版数据」这个事实。

                          判据是**正向**的：只有网页版会话**确定可用**（`valid`）才显示 ——
                          没确认有效就不给入口，避免用户在一个状态未知的账号上反复点。
                        */}
                        {channelState(loginChannels[account.id], "www") === "valid" && (
                          <Button
                            size="small"
                            type="text"
                            icon={<SyncOutlined spin={isSyncing} />}
                            onClick={() => void handleSyncMetrics(account)}
                            disabled={isSyncing}
                            title={isSyncing ? "正在同步粉丝 / 关注 / 获赞数…" : "同步数据（粉丝 / 关注 / 获赞数，走网页版接口）"}
                          />
                        )}
                        <Button
                          size="small"
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={() => void handleDelete(account)}
                          title="删除账号"
                        />
                      </Space>
                    );
                  },
                },
              ]}
            />
            {/*
              **无限下拉的加载提示**（表体滚到底由 `onScroll` 触发 `loadMore`）。
              为什么不用 antd 的哨兵组件：`Table` 没有 `loadMore` 插槽（那是 `List` 的），
              官方表格的无限滚动就是「监听表体 `onScroll` + 自己渲染底部提示」这一种写法。
            */}
            <div
              style={{
                flexShrink: 0,
                padding: "8px 0 0",
                textAlign: "center",
                color: p.textMuted,
                fontSize: 12,
              }}
            >
              {loadingMore ? (
                <Space size={6}>
                  <Spin size="small" />
                  <span>正在加载更多…</span>
                </Space>
              ) : accounts.length < total ? (
                <span>已加载 {accounts.length} / {total}，继续下拉加载</span>
              ) : null}
            </div>
          </div>
        )}
      </Card>

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
    </Flex>
  );
}
