/**
 * 店铺违规管理（设计 43，2026-10-18）—— 千帆后台 `app-violation/violation-manage/list` 的一比一复刻。
 *
 * ## 三条硬约定（改本页前先读）
 *
 * 1. **零落库**：平台数据现取现渲染，本地不建表、不存副本（用户口径「数据均不落库」）。
 * 2. **只读边界**：不代提交申诉、不代标已读、不做任何批量删除 / 批量操作
 *    （用户口径「关于删除，只允许删除一条数据…不要触发批量删除、批量操作」）。
 *    批量入口**功能在**（按千帆渲染、可点），点击只提示、**不发请求**。
 * 3. **缺值 `-`**：平台没给的字段渲染 `-`，不渲染 `0`；但 `0` 是真实值时不改成 `-`。
 *
 * ## 布局（对照千帆）
 *
 * ```
 * 账号选择 + 刷新
 * ├ 状态卡：严重违规 N 个严重影响经营的违规 | 高危预警 N 个即将影响经营的违规预警
 * ├ 子 Tab：预警 / 违规 / 赔付 / 仲裁 / 抽检 / 免罚（?tab=）
 * └ 各 Tab：快捷筛选胶囊 → 筛选表单 → 表格 → 分页
 * ```
 */
import {
  MoreOutlined,
  ReloadOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Divider,
  Drawer,
  Dropdown,
  Empty,
  Flex,
  Form,
  Input,
  Modal,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMatch, useNavigate, useSearchParams } from "react-router-dom";

import { useThemePalette } from "../../../app/providers";
import {
  fetchAccountsPage,
  fetchViolationAppealRecord,
  fetchViolationArbitrations,
  fetchViolationArbitrationTags,
  fetchViolationCheckupAdvice,
  fetchViolationCheckupRisks,
  fetchViolationCompensations,
  fetchViolationCounts,
  fetchViolationDetail,
  fetchViolationEvidence,
  fetchViolationExemptionItems,
  fetchViolationExemptionOverview,
  fetchViolationList,
  fetchViolationMeta,
  fetchViolationNegativeFeedback,
  fetchViolationSpotChecks,
  deleteViolationRelated,
} from "../../../lib/api";
import { useAccountScope } from "../../../lib/account-scope";
import { channelUnavailable } from "../../../lib/sync-gates";
import type { PlatformAccount, XhsLoginChannels } from "../../../types";
import type {
  CheckupAdviceResponse,
  ViolationActionDetail,
  ViolationDetail,
  ViolationListQuery,
  ViolationMeta,
  ViolationPagedResponse,
  ViolationDeleteTarget,
  ViolationRow,
} from "../../../types/violation";
import {
  APPEAL_NOTE_MAX,
  BATCH_ACTION_LABELS,
  BATCH_CONFIRM_ACTIONS,
  CHECKUP_TABS,
  EXPORT_HEADERS,
  LOCAL_READ_MESSAGE,
  LOCAL_READ_STORAGE_KEY,
  PAGE_SIZE_OPTIONS,
  SPOT_CHECK_APPEAL_FILTERS,
  SPOT_CHECK_PUNISH_STATUSES,
  VIOLATION_QUICK_FILTERS,
  VIOLATION_TABS,
  WARNING_QUICK_FILTERS,
  appealTagColor,
  batchOutcomeText,
  canDeleteRelated,
  deleteActionLabel,
  deleteBlockedReason,
  deleteConfirmText,
  deleteOutcomeText,
  isLocallyRead,
  keepVisibleKeys,
  rowsForExport,
  setReadMarks,
  toCsv,
  normalizeTab,
  quickFilterLabel,
  reasonOptions,
  renderActions,
  renderEntity,
  renderPeriodEnd,
  renderText,
  renderTime,
  renderTotalText,
  statusTagColor,
  parseReadMarks,
  toggleReadMark,
} from "../../../lib/violation-manage";
import type { BatchActionKey, CheckupTabKey, QuickFilterKey, ReadMarkMap, ViolationTabKey } from "../../../lib/violation-manage";

import { WaitMask } from "../../../components/ui/wait-mask";
const { Text, Link } = Typography;

/** 表格行宽（固定布局：表格内滚，分页与统计固定在卡底）。 */
/**
 * 表格最小宽度 = **各列 width 之和**（不是随手写一个大数）。
 *
 * ⚠️ 2026-10-18 实测踩过：原先写 `1400`，而 8 列 width 合计只有 ~930 —— antd 会按
 * `scroll.x` 把表格**强行拉到 1400px**，于是右侧 4 列（违规单号 / 违规时间 /
 * 处罚处理状态 / 处罚状态）被推到可视区外，看起来像「页面少了四列」。
 *
 * 正确做法：让宽度由列定义决定；只在容器比它更窄时才出现横向滚动（窄屏兜底）。
 */
/**
 * ⚠️ 这个值是**列宽之和**，不是随便写的大数。
 *
 * 8 列（含勾选）要在一屏内放下：`scroll.x` 小于列宽和 ⇒ antd 仍按列宽渲染；
 * 大于列宽和 ⇒ 表格被强行拉宽、右侧列推出视口（2026-10-18 实测踩过两次：
 * 写 1400 时右侧 4 列不可见；写 744 时状态列被固定列盖住）。
 */
const TABLE_SCROLL_X = 874;

export function ViolationManagePage() {
  const p = useThemePalette();
  const { message, modal } = App.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  void p; // 主题调色板已按页面全局注入，本页沿用 antd 默认色值

  // 店铺线的账号口径与其他页共用（用户切换后回到本页仍是同一账号）。
  //
  // ⚠️ 这里**只加载一次**（`[]` 依赖）—— 挂到 `accountId` 上会：① 每次切账号重拉一遍
  // 全量账号；② 清空选择后又被自动选回第一个，用户永远选不到「未选」。
  const [accountId, setAccountId] = useAccountScope("shop");
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  //: 全量账号加载失败 ⇒ 给一句**可执行**的话（不静默空表，D6）。
  const [accountsError, setAccountsError] = useState("");
  //: 账号列表里的原始登录态：用来在**没打平台之前**就判断「该账号有没有千帆令牌」。
  const [accountChannels, setAccountChannels] = useState<Record<number, XhsLoginChannels>>({});
  //: **上游**报错原文（如「登录凭证已过期, 请重新登录」）。
  //: 与 `arkBlocked` 分工：`arkBlocked` 判「本地有没有令牌」，这个判「平台认不认令牌」。
  //: 空表 + 常驻说明 ≠ 静默空表 —— 用户能分清「没有违规」和「没读到」。
  const [upstreamError, setUpstreamError] = useState("");
  //: 点「重试」重新触发主加载（凭证补登后不用整页刷新）。
  const [reloadTick, setReloadTick] = useState(0);
  //: 删除进行中 ⇒ 按钮 loading（删除要打平台，慢于本地动作，必须给反馈）。
  const [deleting, setDeleting] = useState(false);

  // 每 Tab 各自的查询态（切 Tab 不互相污染）。
  //
  // 表格实际渲染的 Tab 是「当前激活的那个子 Tab」（`?tab=`），**不是**内层 Tabs 的
  // `activeKey` 变量 —— 代码里没有第二个 tab state，两者是同一个值。
  const tab = normalizeTab(searchParams.get("tab"));
  const [quick, setQuick] = useState<QuickFilterKey>("all");
  const [pageNo, setPageNo] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [keyword, setKeyword] = useState("");
  const [entityType, setEntityType] = useState<number | undefined>(undefined);
  const [reasonName, setReasonName] = useState<string | undefined>(undefined);
  const [status, setStatus] = useState<number | undefined>(undefined);
  const [severeOnly, setSevereOnly] = useState(false);
  const [appealFilter, setAppealFilter] = useState<boolean | undefined>(undefined);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(null);
  const [spotPunishStatus, setSpotPunishStatus] = useState<string | undefined>(undefined);

  const [rows, setRows] = useState<ViolationRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [counts, setCounts] = useState<Record<string, number | null>>({
    unread: null,
    pending: null,
  });
  const [highRisk, setHighRisk] = useState(0);
  const [severeCount, setSevereCount] = useState(0);
  const [meta, setMeta] = useState<ViolationMeta | null>(null);
  const [paged, setPaged] = useState<ViolationPagedResponse | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  //: **本地**已读标记（`?tab=violations` 行的「标为已读」）。平台 `read_mark` 写接口不接，
  //: 故只存在 localStorage 里，且每条标记后 `message` 说明「本地标记，不同步平台」。
  const [readMarks, setReadMarksState] = useState<ReadMarkMap>({});

  // 详情抽屉
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<ViolationDetail | null>(null);
  const [appealRecords, setAppealRecords] = useState<Record<string, unknown>[]>([]);
  const [evidence, setEvidence] = useState<unknown>(null);
  const [evidenceLoaded, setEvidenceLoaded] = useState(false);
  const [appealOpen, setAppealOpen] = useState(false);

  // 店铺体检（独立路由）
  const [checkupTab, setCheckupTab] = useState<CheckupTabKey>("all");
  const [checkupRisks, setCheckupRisks] = useState<ViolationRow[]>([]);
  const [checkupRiskTotal, setCheckupRiskTotal] = useState(0);
  const [checkupAdvice, setCheckupAdvice] = useState<CheckupAdviceResponse | null>(null);
  const [negativeFeedback, setNegativeFeedback] = useState<ViolationPagedResponse | null>(null);
  const [checkupLoading, setCheckupLoading] = useState(false);

  // 免罚机会 · 现货爆单豁免概览（`hasExplosiveSpotOrders` / 已累计豁免件数）。
  // 与列表分开存：上一版把它塞进列表响应里又原样丢弃 ⇒ 概览卡永远显示不出来。
  const [exemptionOverview, setExemptionOverview] = useState<Record<string, unknown> | null>(null);

  const isCheckupPath = searchParams.get("view") === "checkup";
  //: `/violation-manage/checkup` 与 `?view=checkup` 两条入口等价（设计 43 §3.1）。
  const isCheckup = isCheckupPath || Boolean(useMatch("/platforms/xhs/violation-manage/checkup"));
  //: 列表请求序号（并发保护：只认最后一次请求的结果）。
  const requestSeq = useRef(0);

  // ---- 本地已读标记：进页读一次（坏数据退空表，不白屏）----
  useEffect(() => {
    try {
      setReadMarksState(parseReadMarks(window.localStorage.getItem(LOCAL_READ_STORAGE_KEY)));
    } catch {
      setReadMarksState({});
    }
  }, []);

  /**
   * 标 / 取消标已读：**只改本地**，立刻告诉用户「不同步平台」。
   *
   * 平台对这个域有 `read_mark` 写接口，本系统刻意不接（设计 43 §1.3）——
   * 接了就等于「顺手改平台状态」，而用户口径是只读。
   */
  const toggleLocalRead = useCallback(
    (violationId: string | null | undefined) => {
      if (!(violationId || "").trim()) return;
      setReadMarksState((prev) => {
        const next = toggleReadMark(prev, accountId, violationId);
        try {
          window.localStorage.setItem(LOCAL_READ_STORAGE_KEY, JSON.stringify(next));
        } catch {
          // 存不下（隐私模式 / 配额满）⇒ 本次会话内仍生效，不阻断操作
        }
        return next;
      });
      message.info(LOCAL_READ_MESSAGE);
    },
    [accountId, message],
  );

  // ---- 账号列表：只留店铺线（ark 通道有令牌）能用的那批由后端门禁判定，这里不过滤 ----
  useEffect(() => {
    let alive = true;
    void fetchAccountsPage("xhs", 1, 100)
      .then((page) => {
        if (!alive) return;
        const items = page.items || [];
        setAccounts(items);
        setAccountsError("");
        setAccountChannels(
          Object.fromEntries(
            items
              .filter((item) => item.login_channels)
              .map((item) => [item.id, item.login_channels as XhsLoginChannels]),
          ),
        );
        if (accountId === null && items.length) {
          // 默认选第一个账号（与店铺线其他页一致：选了就记住）。
          setAccountId(items[0].id);
        }
      })
      .catch((error: unknown) => {
        if (!alive) return;
        setAccounts([]);
        // 账号列表都拿不到 ⇒ 必须说出来（空表会让用户以为「我一个账号都没有」）。
        setAccountsError(describeError(error, "账号列表加载失败"));
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  //: 当前账号的 ark 门禁（纯本地判定，零平台请求）。
  //: `blocked` 只对「**确定**不可用」（missing/guest/expired）成立，`unknown` 放行 ——
  //: 「没检测到」不等于「不可用」（DEF-057 红线）。
  const arkBlocked = useMemo(() => {
    if (accountId === null) return false;
    const channels = accountChannels[accountId];
    return channels ? channelUnavailable(channels, "ark") : false;
  }, [accountId, accountChannels]);

  // ---- 元数据（枚举）----
  useEffect(() => {
    if (accountId === null) return;
    let alive = true;
    void fetchViolationMeta({ platform_account_id: accountId })
      .then((data) => {
        if (alive) setMeta(data);
      })
      .catch(() => {
        if (alive) setMeta(null);
      });
    return () => {
      alive = false;
    };
  }, [accountId]);

  // ---- 切换 Tab 时重置查询态 ----
  useEffect(() => {
    setQuick("all");
    setPageNo(1);
    setKeyword("");
    setEntityType(undefined);
    setReasonName(undefined);
    setStatus(undefined);
    setSevereOnly(false);
    setAppealFilter(undefined);
    setUnreadOnly(false);
    setDateRange(null);
    setSpotPunishStatus(undefined);
    setSelectedKeys([]);
    setRows([]);
    setPaged(null);
  }, [tab, accountId]);

  const setTab = useCallback(
    (next: ViolationTabKey) => {
      const nextParams = new URLSearchParams(searchParams);
      nextParams.set("tab", next);
      nextParams.delete("view");
      // 从 `/checkup` 回到列表：必须切回父路径，否则页面仍按 `useMatch` 判成体检态。
      navigate(`/platforms/xhs/violation-manage?${nextParams.toString()}`, { replace: true });
    },
    [searchParams, navigate],
  );

  /** 去店铺体检（`?view=checkup` 与 `/violation-manage/checkup` 等价，这里走后者）。 */
  const openCheckup = useCallback(() => {
    navigate("/platforms/xhs/violation-manage/checkup", { replace: true });
  }, [navigate]);

  const dateParams = useMemo(() => {
    const start = dateRange?.[0] ? dateRange[0].format("YYYY-MM-DD") : "";
    const end = dateRange?.[1] ? dateRange[1].format("YYYY-MM-DD") : "";
    return { date_start: start, date_end: end };
  }, [dateRange]);

  // ---- 拉列表 ----
  const loadList = useCallback(async () => {
    // 确定没有千帆令牌 ⇒ 不发请求（后端会 400，这里提前给可执行提示，见下方渲染）。
    if (accountId === null || arkBlocked) return;
    const seq = ++requestSeq.current;
    setLoading(true);
    try {
      setUpstreamError(""); // 上一轮的错误不该挂在新一次的加载上
      if (tab === "violations" || tab === "warnings") {
        const query: ViolationListQuery = {
          platform_account_id: accountId,
          scene: tab === "warnings" ? "warning" : "violation",
          page_no: pageNo,
          page_size: pageSize,
          ...(quick !== "all" ? { quick } : {}),
          ...(quick === "all"
            ? {
                keyword,
                ...(entityType !== undefined ? { entity_types: [entityType] } : {}),
                ...(reasonName ? { reason_name: reasonName } : {}),
                ...(status !== undefined ? { statuses: [status] } : {}),
                severe_only: severeOnly,
                ...(appealFilter !== undefined ? { allow_appeal: appealFilter } : {}),
                unread_only: unreadOnly,
                date_start: dateParams.date_start,
                date_end: dateParams.date_end,
              }
            : {}),
        };
        const data = await fetchViolationList(query);
        if (seq !== requestSeq.current) return;
        setRows(data.items || []);
        setTotal(data.total || 0);
      } else if (tab === "compensations") {
        const data = await fetchViolationCompensations({
          platform_account_id: accountId,
          page_no: pageNo,
          page_size: pageSize,
        });
        if (seq !== requestSeq.current) return;
        setPaged(data);
        setTotal(data.total || 0);
      } else if (tab === "arbitrations") {
        const data = await fetchViolationArbitrations({
          platform_account_id: accountId,
          page_no: pageNo,
          page_size: pageSize,
        });
        if (seq !== requestSeq.current) return;
        setPaged(data);
        setTotal(data.total || 0);
        // 仲裁原因下拉：千帆进本 Tab 会拉一次 taglist。
        void fetchViolationArbitrationTags({ platform_account_id: accountId }).catch(() => undefined);
      } else if (tab === "spot-checks") {
        const data = await fetchViolationSpotChecks({
          platform_account_id: accountId,
          page_no: pageNo,
          page_size: pageSize,
          keyword,
          reason_name: reasonName || "",
          allow_appeal: appealFilter,
          punish_status: spotPunishStatus || "",
        });
        if (seq !== requestSeq.current) return;
        setRows(data.items || []);
        setTotal(data.total || 0);
      } else if (tab === "exemptions") {
        const data = await fetchViolationExemptionItems({
          platform_account_id: accountId,
          page_no: pageNo,
          page_size: pageSize,
          date_start: dateParams.date_start,
          date_end: dateParams.date_end,
        });
        if (seq !== requestSeq.current) return;
        setPaged(data);
        setTotal(data.total || 0);
        // 概览（已累计豁免 / 是否有爆单）是**另一个接口**；它失败只缺那张卡，不拖垮列表。
        void fetchViolationExemptionOverview({ platform_account_id: accountId })
          .then((res) => {
            if (seq === requestSeq.current) setExemptionOverview(res.overview);
          })
          .catch(() => {
            if (seq === requestSeq.current) setExemptionOverview(null);
          });
      }
    } catch (error) {
      if (seq === requestSeq.current) {
        const text = describeError(error, "违规数据加载失败");
        message.error(text);
        // 常驻一条（message 会消失，而「为什么是空表」必须一直看得见）
        setUpstreamError(text);
        setRows([]);
        setTotal(0);
      }
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [
    accountId,
    tab,
    pageNo,
    pageSize,
    quick,
    keyword,
    entityType,
    reasonName,
    status,
    severeOnly,
    appealFilter,
    unreadOnly,
    dateParams.date_start,
    dateParams.date_end,
    spotPunishStatus,
    arkBlocked,
    reloadTick,
    message,
  ]);

  // ---- 角标与状态卡计数（各查一次 counts）----
  const loadCounts = useCallback(async () => {
    if (accountId === null || arkBlocked) return;
    try {
      const [unread, pending, severe, warning] = await Promise.all([
        fetchViolationCounts({ platform_account_id: accountId, quick: "unread" }),
        fetchViolationCounts({ platform_account_id: accountId, quick: "pending" }),
        fetchViolationCounts({ platform_account_id: accountId, quick: "severe" }),
        fetchViolationCounts({
          platform_account_id: accountId,
          scene: "warning",
          quick: "high_risk",
        }),
      ]);
      setCounts({ unread: unread.count, pending: pending.count });
      setSevereCount(severe.count);
      setHighRisk(warning.count);
    } catch {
      // 计数失败不影响列表：角标保持「未知」而不是显示 0。
      setCounts({ unread: null, pending: null });
    }
  }, [accountId, arkBlocked]);

  useEffect(() => {
    if (isCheckup) return;
    void loadList();
  }, [loadList, isCheckup]);

  useEffect(() => {
    if (isCheckup) return;
    // `?tab=` 直接变 ⇒ 三角标（未读 / 待处理）跟着刷新（千帆角标随页面数据变）。
    void loadCounts();
  }, [loadCounts, isCheckup, tab]);

  // ---- 店铺体检 ----
  const loadCheckup = useCallback(async () => {
    if (accountId === null || arkBlocked) return;
    setCheckupLoading(true);
    try {
      const [risks, advice, quality, logistics] = await Promise.all([
        fetchViolationCheckupRisks({ platform_account_id: accountId, page_size: 100 }),
        fetchViolationCheckupAdvice({ platform_account_id: accountId }),
        fetchViolationNegativeFeedback({ platform_account_id: accountId, kind: "quality" }),
        fetchViolationNegativeFeedback({ platform_account_id: accountId, kind: "logistics" }),
      ]);
      setCheckupRisks((risks.items || []) as unknown as ViolationRow[]);
      setCheckupRiskTotal(risks.total || 0);
      setCheckupAdvice(advice);
      setNegativeFeedback({
        total: (quality.total || 0) + (logistics.total || 0),
        items: [
          ...((quality.items || []) as Record<string, unknown>[]),
          ...((logistics.items || []) as Record<string, unknown>[]),
        ],
      });
    } catch (error) {
      message.error(describeError(error, "店铺体检数据加载失败"));
    } finally {
      setCheckupLoading(false);
    }
  }, [accountId, arkBlocked, message]);

  useEffect(() => {
    if (!isCheckup) return;
    void loadCheckup();
  }, [isCheckup, loadCheckup]);

  // ---- 详情 ----
  const openDetail = useCallback(
    async (row: ViolationRow) => {
      if (!accountId || !row.violation_id) return;
      setDetailOpen(true);
      setDetailLoading(true);
      setDetail(null);
      setAppealRecords([]);
      setEvidence(null);
      setEvidenceLoaded(false);
      try {
        const payload = {
          platform_account_id: accountId,
          violation_id: row.violation_id,
          tenant: row.tenant || "",
          tag_id: row.tag_id || "",
        };
        const [info, records, ev] = await Promise.all([
          fetchViolationDetail(payload),
          fetchViolationAppealRecord(payload).catch(() => ({ items: [] })),
          fetchViolationEvidence({
            platform_account_id: accountId,
            violation_id: row.violation_id,
            entity_id: row.entity_id || "",
            entity_type: row.entity_type,
            tag_id: row.tag_id || "",
            tenant: row.tenant || "",
          }).catch(() => ({ evidence: null })),
        ]);
        setDetail(info);
        setAppealRecords(records.items || []);
        setEvidence(ev.evidence);
        setEvidenceLoaded(true);
      } catch (error) {
        message.error(describeError(error, "违规详情加载失败"));
        setDetailOpen(false);
      } finally {
        setDetailLoading(false);
      }
    },
    [accountId, message],
  );

  /**
   * 批量操作：**真的执行**（2026-10-18 用户口径修正，推翻首版「一律拦截」）。
   *
   * | 动作 | 写平台？ |
   * |---|---|
   * | 标记已读 / 未读 | 否（本地标记，与单条同源） |
   * | 导出选中记录 | 否（只读） |
   * | **删除关联内容** | **是** —— 先删平台商品/笔记，再删本地（用户口径） |
   *
   * ⚠️ 开发与验收阶段**只用一条数据**试删除；功能本身不做数量上限。
   */
  const runBatchAction = useCallback(
    async (action: BatchActionKey) => {
      const ids = keepVisibleKeys(
        selectedKeys,
        rows.map((row) => row.violation_id).filter((id): id is string => Boolean(id))
      );
      if (ids.length === 0) {
        message.warning("请先勾选要处理的行");
        return;
      }

      if (action === "mark_read" || action === "mark_unread") {
        // ⚠️ 用 `setReadMarks`（幂等设定）而不是循环 `toggleLocalRead`：
        //    勾 5 条里若已有 3 条是已读，「全标已读」用切换会把这 3 条翻成未读。
        const read = action === "mark_read";
        setReadMarksState((prev) => {
          const next = setReadMarks(prev, accountId, ids, read);
          try {
            window.localStorage.setItem(LOCAL_READ_STORAGE_KEY, JSON.stringify(next));
          } catch {
            // 存不下也不阻断（与单条一致）
          }
          return next;
        });
        message.success(batchOutcomeText(action, ids.length));
        return;
      }

      // export：只读导出 CSV（BOM 让 Excel 正确识别中文）
      const exported = rowsForExport(rows as unknown as Array<Record<string, unknown>>, ids);
      const csv = toCsv(exported, EXPORT_HEADERS);
      const blob = new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `违规记录-${accountId ?? "unknown"}-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      URL.revokeObjectURL(url);
      message.success(batchOutcomeText("export", exported.length));
    },
    [accountId, message, rows, selectedKeys, toggleLocalRead]
  );

  /**
   * 删除违规关联的商品 / 笔记 —— **单条与批量共用本函数**（只有入参条数不同）。
   *
   * 口径（用户 2026-10-18）：**先删平台，再核对本地，有则删、没有则跳过**。
   * 后端已按这个顺序实现（`violation_delete_service.execute_delete`），
   * 前端只负责：过滤掉不可删的行 → 二次确认 → 如实显示四态结果 → 刷新列表。
   */
  const deleteRelated = useCallback(
    async (picked: ViolationRow[]) => {
      if (accountId === null) return;

      // 只保留有可删实体的行（店铺/账号/直播间没有），并入参去重
      const targets: ViolationDeleteTarget[] = [];
      const seen = new Set<string>();
      let blocked = 0;
      for (const row of picked) {
        if (!canDeleteRelated(row.entity_type)) {
          blocked += 1;
          continue;
        }
        const id = (row.entity_id || "").trim();
        if (!id) {
          blocked += 1;
          continue;
        }
        const key = `${row.entity_type}:${id}`;
        if (seen.has(key)) continue;
        seen.add(key);
        targets.push({ entity_type: row.entity_type as number, entity_id: id });
      }

      if (targets.length === 0) {
        message.warning(
          blocked > 0 ? "选中行没有可删除的商品或笔记" : "没有可删除的内容"
        );
        return;
      }

      const confirmed = await new Promise<boolean>((resolve) => {
        modal.confirm({
          title: `删除关联内容（${targets.length} 条）`,
          // 说明里带换行 ⇒ 用 pre-line 的 div 承载（antd 的纯文本 content 不换行）
          content: (
            <div style={{ whiteSpace: "pre-line" }}>{deleteConfirmText(targets.length)}</div>
          ),
          okText: "确认删除",
          okButtonProps: { danger: true },
          cancelText: "取消",
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        });
      });
      if (!confirmed) return;

      setDeleting(true);
      try {
        const res = await deleteViolationRelated({
          platform_account_id: accountId,
          targets,
        });
        const summary = {
          deleted: res.deleted,
          platform_only: res.platform_only,
          failed: res.failed,
          skipped: res.skipped,
          absent: res.absent ?? 0,
        };
        const text = deleteOutcomeText(summary);
        // 「数据已经被删除，不要重复删除」不是成功也不该当失败 —— 用 info 单独告知
        if (summary.failed > 0) message.warning(text);
        else if (summary.deleted === 0 && summary.absent > 0) message.info(text);
        else message.success(text);

        // 失败原因要看得见（哪几条没删掉、为什么）
        const failures = res.results.filter((r) => r.status === "failed");
        if (failures.length > 0) {
          modal.warning({
            title: "部分内容未能删除",
            content: (
              <div style={{ maxHeight: 240, overflow: "auto" }}>
                {failures.map((r) => (
                  <div key={`${r.entity_type}:${r.entity_id}`} style={{ marginBottom: 6 }}>
                    <Text code>{r.entity_id}</Text>
                    <Text type="secondary"> — {r.reason || "未知原因"}</Text>
                  </div>
                ))}
              </div>
            ),
          });
        }

        // 列表刷新（平台数据已变，本地缓存也可能变了）
        setReloadTick((n) => n + 1);
      } catch (error) {
        message.error(describeError(error, "删除失败"));
      } finally {
        setDeleting(false);
      }
    },
    [accountId, message, modal]
  );


  // ---- 列定义 ----
  const violationColumns: ColumnsType<ViolationRow> = useMemo(
    () => [
      // ⚠️ **列宽是「够用」不是「舒服」**：千帆原页 8 列在 ~740px 内放下、不横向滚动，
      //    首列原因列夹住时换 2 行属正常。宽了就会把「违规单号 / 违规时间 / 处罚状态」
      //    挤出视口 —— 2026-10-18 实测 260+260+280 三列就把 739px 容器撑到 1572px。
      {
        title: "违规原因",
        dataIndex: "violation_reason",
        width: 160,
        ellipsis: { showTitle: true },
        render: (_: unknown, row) => (
          <Space direction="vertical" size={2} style={{ maxWidth: "100%" }}>
            <Text ellipsis={{ tooltip: renderText(row.violation_reason) }} style={{ maxWidth: "100%" }}>
              {renderText(row.violation_reason)}
            </Text>
            {row.severe ? (
              <Tag color="red" style={{ marginInlineEnd: 0 }}>
                严重违规
              </Tag>
            ) : null}
          </Space>
        ),
      },
      {
        title: "违规对象",
        dataIndex: "entity_name",
        width: 140,
        render: (_: unknown, row) => {
          const entity = renderEntity(row);
          return (
            <Space direction="vertical" size={0} style={{ maxWidth: "100%" }}>
              <Text ellipsis={{ tooltip: entity.name }} style={{ maxWidth: "100%" }}>
                {entity.name}
              </Text>
              {/* ID 单行截断，tooltip 给全文 —— 列宽只有 134px，完整 ID 放不下 */}
              <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: entity.idText }}>
                {entity.idText}
              </Text>
            </Space>
          );
        },
      },
      {
        title: "处罚方式",
        dataIndex: "actions",
        width: 140,
        render: (_: unknown, row) => (
          <Space direction="vertical" size={0} style={{ maxWidth: "100%" }}>
            <Text ellipsis={{ tooltip: renderActions(row.actions) }} style={{ maxWidth: "100%" }}>
              {renderActions(row.actions)}
            </Text>
            {renderPeriodEnd(row.period_end_time) ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {renderPeriodEnd(row.period_end_time)}
              </Text>
            ) : null}
          </Space>
        ),
      },
      {
        title: "违规单号",
        dataIndex: "violation_id",
        width: 128,
        // 19 位单号在 112px 里放不下，`copyable` 的图标占一行空间 ⇒ 单行截断 + tooltip + 可复制
        render: (_: unknown, row) => {
          const id = renderText(row.violation_id);
          return (
            <Text copyable={{ text: id, tooltips: ["复制违规单号", "已复制"] }} ellipsis={{ tooltip: id }}>
              {id}
            </Text>
          );
        },
      },
      {
        title: <span className="violation-th-nowrap">违规时间</span>,
        dataIndex: "violation_time",
        width: 108,
        render: (_: unknown, row) => (
          <span className="violation-td-nowrap">{renderTime(row.violation_time)}</span>
        ),
      },
      {
        title: <span className="violation-th-nowrap">处罚处理状态</span>,
        dataIndex: "appeal_status_name",
        width: 90,
        // ⚠️ 这里**不用 `Tag`**：列宽 ~60px，Tag 自带 4px 边框 + 8px 内边距 + 8px 外边距，
        //    会把「可申诉」挤成竖排（2026-10-18 实测）。纯色文字既省宽度也更好读。
        render: (_: unknown, row) => (
          <Text style={{ color: appealTagColor(row.appeal_status), whiteSpace: "nowrap" }}>
            {renderText(row.appeal_status_name)}
          </Text>
        ),
      },
      {
        title: <span className="violation-th-nowrap">处罚状态</span>,
        dataIndex: "status_name",
        width: 76,
        render: (_: unknown, row) => (
          <Text style={{ color: statusTagColor(row.status), whiteSpace: "nowrap" }}>
            {renderText(row.status_name)}
          </Text>
        ),
      },
      {
        title: "操作",
        key: "action",
        width: 48,
        fixed: "right",
        render: (_: unknown, row) => {
          const read = isLocallyRead(readMarks, accountId, row.violation_id);
          const deletable = canDeleteRelated(row.entity_type);
          // 8 列要挤进 ~740px：操作列只给 48px ⇒ 一个图标 + 下拉菜单，
          // 不再并排放两个图标按钮（2026-10-18 实测：两个图标把「处罚状态」挤成竖排）。
          return (
            <Dropdown
              trigger={["click"]}
              menu={{
                items: [
                  { key: "detail", label: "查看详情" },
                  {
                    key: "read",
                    // 已读标记：**只在前端本地切换**（平台 read_mark 写接口不接）。
                    label: read ? "标为未读" : "标为已读",
                  },
                  { type: "divider" as const },
                  {
                    key: "delete",
                    // 删的是违规**关联的本地内容**（平台违规记录删不掉）。
                    // 不可删的对象类型直接禁用 + 说明原因，不让用户点了才看到失败。
                    label: deletable ? deleteActionLabel(row.entity_type) : deleteBlockedReason(row.entity_type),
                    danger: deletable,
                    disabled: !deletable,
                  },
                ],
                onClick: ({ key }) => {
                  if (key === "detail") void openDetail(row);
                  else if (key === "read") toggleLocalRead(row.violation_id);
                  else if (key === "delete") void deleteRelated([row]);
                },
              }}
            >
              <Button type="link" size="small" aria-label="更多操作" icon={<MoreOutlined />} />
            </Dropdown>
          );
        },
      },
    ],
    [openDetail, readMarks, accountId, toggleLocalRead, deleteRelated],
  );

  const warningColumns: ColumnsType<ViolationRow> = useMemo(
    () => [
      {
        title: "预警原因",
        dataIndex: "violation_reason",
        width: 260,
        fixed: "left",
        render: (_: unknown, row) => renderText(row.violation_reason),
      },
      {
        title: "预警对象",
        dataIndex: "entity_name",
        width: 260,
        render: (_: unknown, row) => {
          const entity = renderEntity(row);
          return (
            <Space direction="vertical" size={0}>
              <Text>{entity.name}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {entity.idText}
              </Text>
            </Space>
          );
        },
      },
      {
        title: "预警单号",
        dataIndex: "violation_id",
        width: 200,
        render: (_: unknown, row) => <Text copyable>{renderText(row.violation_id)}</Text>,
      },
      {
        title: "预警时间",
        dataIndex: "violation_time",
        width: 180,
        render: (_: unknown, row) => renderTime(row.violation_time),
      },
      {
        title: "预警处理状态",
        dataIndex: "appeal_status_name",
        width: 130,
        render: (_: unknown, row) => renderText(row.appeal_status_name),
      },
      {
        title: "预警状态",
        dataIndex: "status_name",
        width: 120,
        render: (_: unknown, row) => renderText(row.status_name),
      },
      {
        title: "操作",
        key: "action",
        width: 110,
        fixed: "right",
        render: (_: unknown, row) => (
          <Button type="link" size="small" onClick={() => void openDetail(row)}>
            查看详情
          </Button>
        ),
      },
    ],
    [openDetail],
  );

  /** 赔付 / 仲裁：平台字段因账号而异（本店为空），列按千帆表头渲染、缺值 `-`。 */
  const genericColumns = useCallback(
    (headers: string[]): ColumnsType<Record<string, unknown>> =>
      headers.map((title) => ({
        title,
        key: title,
        width: 180,
        render: (_: unknown, row: Record<string, unknown>) =>
          renderText(pickByTitle(row, title)),
      })),
    [],
  );

  const compensationColumns = useMemo(
    () =>
      genericColumns([
        "赔付原因",
        "赔付对象",
        "处罚方式",
        "赔付时间",
        "赔付单编号",
        "申诉状态",
        "处罚状态",
        "操作",
      ]),
    [genericColumns],
  );
  const arbitrationColumns = useMemo(
    () =>
      genericColumns([
        "仲裁原因",
        "仲裁对象",
        "售后仲裁处理",
        "仲裁时间",
        "仲裁ID",
        "申诉状态",
        "仲裁处理状态",
        "操作",
      ]),
    [genericColumns],
  );
  const exemptionColumns = useMemo(
    () =>
      genericColumns([
        "爆单日期(支付日期)",
        "宽限期内爆单订单量",
        "违规考核宽限期",
        "宽限期发货超时率",
        "宽限期后72小时发货率",
        "是否满足履约好标准",
        "操作",
      ]),
    [genericColumns],
  );

  const spotCheckColumns: ColumnsType<ViolationRow> = useMemo(
    () => [
      {
        title: "违规原因",
        dataIndex: "violation_reason",
        width: 300,
        render: (_: unknown, row) => renderText(row.violation_reason),
      },
      {
        title: "违规单号",
        dataIndex: "violation_id",
        width: 220,
        render: (_: unknown, row) => <Text copyable>{renderText(row.violation_id)}</Text>,
      },
      {
        title: "违规时间",
        dataIndex: "violation_time",
        width: 180,
        render: (_: unknown, row) => renderTime(row.violation_time),
      },
      {
        title: "申诉状态",
        dataIndex: "appeal_status_name",
        width: 130,
        render: (_: unknown, row) => renderText(row.appeal_status_name),
      },
      {
        title: "操作",
        key: "action",
        width: 110,
        render: (_: unknown, row) => (
          <Button type="link" size="small" onClick={() => void openDetail(row)}>
            查看详情
          </Button>
        ),
      },
    ],
    [openDetail],
  );

  // ---- 渲染 ----
  if (accountsError) {
    return (
      <Card>
        <Empty description={`账号列表加载失败：${accountsError}`} />
      </Card>
    );
  }
  if (accounts.length === 0 && accountId === null) {
    return (
      <Card>
        <Empty description="还没有可用的小红书账号，请先在「账号矩阵」导入账号与千帆登录态。" />
      </Card>
    );
  }

  const quickFilters =
    tab === "warnings" ? WARNING_QUICK_FILTERS : tab === "violations" ? VIOLATION_QUICK_FILTERS : [];
  const currentColumns =
    tab === "warnings"
      ? warningColumns
      : tab === "compensations"
        ? compensationColumns
        : tab === "arbitrations"
          ? arbitrationColumns
          : tab === "spot-checks"
            ? spotCheckColumns
            : tab === "exemptions"
              ? exemptionColumns
              : violationColumns;
  const currentRows =
    tab === "violations" || tab === "warnings" || tab === "spot-checks"
      ? rows
      : (paged?.items as unknown as Record<string, unknown>[]) || [];

  return (
    <Flex vertical gap={10} style={{ height: "calc(100vh - 87px)" }}>
      {/* ---- 账号 + 刷新 ---- */}
      <Flex align="center" gap={8} style={{ flexShrink: 0 }} wrap>
        <Text type="secondary">账号</Text>
        <Select
          style={{ width: 240 }}
          value={accountId ?? undefined}
          placeholder="选择店铺账号"
          onChange={(value) => setAccountId(value)}
          options={accounts.map((item) => ({
            value: item.id,
            // 只显示店铺名 —— 不带「（8）」这类账号 ID 后缀（用户 2026-10-18 口径）
            label: item.nickname,
          }))}
        />
        <Button
          icon={<ReloadOutlined />}
          onClick={() => {
            if (isCheckup) void loadCheckup();
            else {
              void loadList();
              void loadCounts();
            }
          }}
        >
          刷新
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          实时数据
        </Text>
      </Flex>

      {/* ---- ark 门禁（纯本地判定）：确定没有千帆令牌 ⇒ 结构化提示补登，不静默空表 ---- */}
      {arkBlocked ? (
        <Alert
          type="warning"
          showIcon
          style={{ flexShrink: 0 }}
          message="该账号未导入千帆（ark）登录态，违规数据只能从千帆后台读取"
          description="请到「账号矩阵」对该账号补登千帆登录态后回到本页刷新；本页不会借用其他账号的会话。"
        />
      ) : null}

      {/*
        ---- 凭证失效（**上游**判定，本地门禁看不见）----
        `arkBlocked` 只回答「本地有没有令牌」；「令牌**过期**」只有平台知道。
        不区分这两者的话，过期账号会渲染成一张干净的空表 —— 用户看到的是
        「本店没有任何违规」，而真相是「根本没读到」。这类静默说假话比报错贵得多。
      */}
      {upstreamError ? (
        <Alert
          type="error"
          showIcon
          style={{ flexShrink: 0 }}
          message={upstreamError}
          description="平台侧登录凭证已失效或读取失败。请到「账号矩阵」重新登录千帆后点「刷新」；下方空表不代表本店没有违规，只代表本次没读到。"
          action={
            <Button size="small" onClick={() => setReloadTick((n) => n + 1)}>
              重试
            </Button>
          }
        />
      ) : null}


      {/* ---- 状态卡（千帆两条）---- */}
      <Row gutter={10} style={{ flexShrink: 0 }}>
        <Col span={12}>
          <Card size="small">
            <Flex align="center" gap={16}>
              <Space direction="vertical" size={0}>
                <Text type="secondary">严重违规</Text>
                <Text style={{ fontSize: 22 }}>{severeCount}</Text>
              </Space>
              <Text type="secondary" style={{ flex: 1 }}>
                {severeCount} 个严重影响经营的违规
              </Text>
              <Button type="link" size="small" onClick={() => setTab("violations")}>
                查看全部
              </Button>
            </Flex>
          </Card>
        </Col>
        <Col span={12}>
          <Card size="small">
            <Flex align="center" gap={16}>
              <Space direction="vertical" size={0}>
                <Text type="secondary">高危预警</Text>
                <Text style={{ fontSize: 22 }}>{highRisk}</Text>
              </Space>
              <Text type="secondary" style={{ flex: 1 }}>
                {highRisk} 个即将影响经营的违规预警
              </Text>
              <Button type="link" size="small" onClick={() => setTab("warnings")}>
                查看全部
              </Button>
            </Flex>
          </Card>
        </Col>
      </Row>

      {/* ---- 主卡：子 Tab + 筛选 + 表格 ---- */}
      <Card
        size="small"
        style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
        styles={{
          body: { padding: 12, flex: 1, minHeight: 0, display: "flex", flexDirection: "column" },
        }}
        title={
          <Tabs
            size="small"
            activeKey={isCheckup ? "checkup" : tab}
            onChange={(key) => {
              if (key === "checkup") {
                openCheckup();
              } else {
                setTab(key as ViolationTabKey);
              }
            }}
            items={[
              { key: "checkup", label: "店铺体检" },
              { key: "violations", label: "违规管理" },
            ]}
          />
        }
        extra={
          <Space>
            <Dropdown
              trigger={["click"]}
              disabled={selectedKeys.length === 0}
              menu={{
                items: [
                  { key: "mark_read", label: BATCH_ACTION_LABELS.mark_read },
                  { key: "mark_unread", label: BATCH_ACTION_LABELS.mark_unread },
                  { type: "divider" as const },
                  { key: "export", label: BATCH_ACTION_LABELS.export },
                  { type: "divider" as const },
                  {
                    key: "delete_related",
                    label: BATCH_ACTION_LABELS.delete_related,
                    danger: true,
                  },
                ],
                onClick: ({ key }) => {
                  const action = key as BatchActionKey;
                  if (action === "delete_related") {
                    const picked = rows.filter((row) =>
                      selectedKeys.includes(row.violation_id ?? "")
                    );
                    void deleteRelated(picked);
                    return;
                  }
                  if (BATCH_CONFIRM_ACTIONS.includes(action)) {
                    modal.confirm({
                      title: BATCH_ACTION_LABELS[action],
                      content: `将对选中的 ${selectedKeys.length} 条记录执行「${BATCH_ACTION_LABELS[action]}」。`,
                      okText: "确定",
                      cancelText: "取消",
                      onOk: () => void runBatchAction(action),
                    });
                    return;
                  }
                  void runBatchAction(action);
                },
              }}
            >
              <Button
                size="small"
                disabled={selectedKeys.length === 0 || deleting}
                loading={deleting}
              >
                批量操作（{selectedKeys.length}）
              </Button>
            </Dropdown>
            <Button
              size="small"
              icon={<WarningOutlined />}
              onClick={openCheckup}
            >
              店铺体检
            </Button>
          </Space>
        }
      >
        {isCheckup ? (
          <CheckupPanel
            loading={checkupLoading}
            tab={checkupTab}
            onTab={setCheckupTab}
            risks={checkupRisks}
            riskTotal={checkupRiskTotal}
            advice={checkupAdvice}
            negativeFeedback={negativeFeedback}
            onOpenViolation={(id) => {
              const row = checkupRisks.find((item) => item.violation_id === id);
              if (row) void openDetail(row);
            }}
          />
        ) : (
          <>
            <Tabs
              size="small"
              activeKey={tab}
              onChange={(key) => {
                setTab(key as ViolationTabKey);
                setPageNo(1);
              }}
              items={VIOLATION_TABS.map((item) => ({ key: item.key, label: item.label }))}
              style={{ flexShrink: 0 }}
            />

            {quickFilters.length > 0 ? (
              <Flex align="center" gap={8} style={{ flexShrink: 0, marginBottom: 8 }} wrap>
                <Text type="secondary">快捷筛选：</Text>
                <Segmented
                  size="small"
                  value={quick}
                  onChange={(value) => {
                    setQuick(value as QuickFilterKey);
                    setPageNo(1);
                  }}
                  options={quickFilters.map((filter) => ({
                    value: filter.key,
                    label: quickFilterLabel(filter, counts),
                  }))}
                />
              </Flex>
            ) : null}

            {/* 筛选表单：只在「全部」态可用（千帆点快捷会重置其余条件） */}
            {(tab === "violations" || tab === "warnings" || tab === "spot-checks") && quick === "all" ? (
              <Form layout="inline" style={{ flexShrink: 0, rowGap: 8, marginBottom: 8 }}>
                <Form.Item label={tab === "warnings" ? "预警单号或预警对象ID" : "违规单号或违规对象ID"}>
                  <Input
                    allowClear
                    style={{ width: 220 }}
                    placeholder="请输入"
                    value={keyword}
                    onChange={(event) => setKeyword(event.target.value)}
                    onPressEnter={() => {
                      setPageNo(1);
                      void loadList();
                    }}
                  />
                </Form.Item>
                {tab !== "spot-checks" ? (
                  <Form.Item label={tab === "warnings" ? "预警对象" : "违规对象"}>
                    <Select
                      allowClear
                      style={{ width: 160 }}
                      value={entityType}
                      placeholder="全部"
                      onChange={(value) => setEntityType(value)}
                      options={(meta?.violation_entity || []).map((item) => ({
                        value: item.value,
                        label: item.label,
                      }))}
                    />
                  </Form.Item>
                ) : null}
                <Form.Item label={tab === "warnings" ? "预警原因" : "违规原因"}>
                  <Select
                    allowClear
                    style={{ width: 260 }}
                    value={reasonName}
                    placeholder="全部"
                    onChange={(value) => setReasonName(value)}
                    options={reasonOptions(meta?.reason_tags)}
                  />
                </Form.Item>
                {tab === "violations" ? (
                  <Form.Item label="处罚状态">
                    <Select
                      allowClear
                      style={{ width: 140 }}
                      value={status}
                      placeholder="请选择"
                      onChange={(value) => setStatus(value)}
                      options={(meta?.violation_status || []).map((item) => ({
                        value: item.value,
                        label: item.label,
                      }))}
                    />
                  </Form.Item>
                ) : null}
                {tab === "spot-checks" ? (
                  <Form.Item label="处罚状态">
                    <Select
                      allowClear
                      style={{ width: 140 }}
                      value={spotPunishStatus}
                      placeholder="请输入"
                      onChange={(value) => setSpotPunishStatus(value)}
                      options={SPOT_CHECK_PUNISH_STATUSES.map((item) => ({
                        value: item,
                        label: item,
                      }))}
                    />
                  </Form.Item>
                ) : null}
                <Form.Item label="申诉状态">
                  <Select
                    allowClear
                    style={{ width: 140 }}
                    value={appealFilter}
                    placeholder="全部"
                    onChange={(value) => setAppealFilter(value)}
                    options={SPOT_CHECK_APPEAL_FILTERS}
                  />
                </Form.Item>
                <Form.Item label={tab === "warnings" ? "预警时间" : "违规时间"}>
                  <DatePicker.RangePicker
                    style={{ width: 260 }}
                    value={dateRange as never}
                    onChange={(value) => setDateRange(value as never)}
                  />
                </Form.Item>
                <Form.Item>
                  <Space>
                    <Button
                      onClick={() => {
                        setQuick("all");
                        setKeyword("");
                        setEntityType(undefined);
                        setReasonName(undefined);
                        setStatus(undefined);
                        setSevereOnly(false);
                        setAppealFilter(undefined);
                        setUnreadOnly(false);
                        setDateRange(null);
                        setSpotPunishStatus(undefined);
                        setPageNo(1);
                      }}
                    >
                      重置
                    </Button>
                    <Button
                      type="primary"
                      onClick={() => {
                        setPageNo(1);
                        void loadList();
                      }}
                    >
                      查询
                    </Button>
                  </Space>
                </Form.Item>
              </Form>
            ) : null}

            {/* 免罚机会：规则说明 + 已累计豁免（千帆页头的统计与规则卡；概览接口失败则不渲染，
                不显示 0 —— 见设计 43 §1.5「高价商品卡」同口径）。 */}
            {tab === "exemptions" && exemptionOverview ? (
              <Card size="small" style={{ flexShrink: 0, marginBottom: 8 }}>
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  <Flex align="center" gap={12} wrap>
                    <Text strong>已累计豁免</Text>
                    <Text style={{ fontSize: 20 }}>
                      {renderText(exemptionOverview.explosiveSellerExemptPackageCnt)}
                    </Text>
                    <Tag color={exemptionOverview.hasExplosiveSpotOrders ? "green" : "default"}>
                      {exemptionOverview.hasExplosiveSpotOrders ? "有爆单订单" : "本店暂无爆单订单"}
                    </Tag>
                  </Flex>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    履约好标准：宽限期内 7 日发货超时率 ≤10%、承诺发货后 72 小时完成率 ≥99%，违规考核宽限期 7 天。
                  </Text>
                </Space>
              </Card>
            ) : null}

            {/* 仲裁列表首次进入的 1/3 引导卡（千帆原文「新增仲裁列表」，右上角 1/3 —— 只有一页）。 */}
            {tab === "arbitrations" ? (
              <Alert
                type="info"
                showIcon
                style={{ flexShrink: 0, marginBottom: 8 }}
                message={
                  <Flex align="center" gap={8} wrap>
                    <Text strong>新增仲裁列表</Text>
                    <Tag>1/3</Tag>
                  </Flex>
                }
                description="售后仲裁会出现在这里；可申诉的仲裁请在申诉截止前处理，超时视为放弃。"
              />
            ) : null}

            <div className="violation-table" style={{ flex: 1, minHeight: 0 }}>
              {/* ⚠️ indicator={<></>}：转圈由下方 <WaitMask open={loading}> 统一画，
                  同一状态同时驱动两处会叠成两层圈。此处只保留挡交互 + 撑高度。 */}
              <Spin indicator={<></>} spinning={loading} style={{ height: "100%" }}>
                <Table
                  size="small"
                  rowKey={(record) =>
                    String(
                      (record as Record<string, unknown>).violation_id ??
                        (record as Record<string, unknown>).violationId ??
                        JSON.stringify(record).slice(0, 32),
                    )
                  }
                  columns={currentColumns as ColumnsType<Record<string, unknown>>}
                  dataSource={currentRows as Record<string, unknown>[]}
                  rootClassName="violation-table"
                  scroll={{ x: TABLE_SCROLL_X, y: "100%" }}
                  pagination={{
                    current: pageNo,
                    pageSize,
                    total,
                    size: "small",
                    showSizeChanger: true,
                    pageSizeOptions: PAGE_SIZE_OPTIONS.map(String),
                    showTotal: () => renderTotalText(total, pageSize),
                    onChange: (nextPage, nextSize) => {
                      // 改每页条数时回到第 1 页（停在旧页码会落到「第 6 页 / 共 2 页」的空表）。
                      setPageNo(nextSize !== pageSize ? 1 : nextPage);
                      setPageSize(nextSize);
                    },
                  }}
                  rowSelection={{
                    selectedRowKeys: selectedKeys,
                    onChange: (keys) => setSelectedKeys(keys.map(String)),
                  }}
                  locale={{ emptyText: <Empty description="暂无数据" /> }}
                />
              </Spin>
<WaitMask open={loading} title="正在加载违规数据" />
            </div>
          </>
        )}
      </Card>

      {/* ---- 详情抽屉 ---- */}
      <Drawer
        title="违规详情"
        width={720}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        destroyOnHidden
      >
        {detailLoading ? (
          <Spin />
        ) : detail ? (
          <Space direction="vertical" size={16} style={{ width: "100%" }}>
            <Text type="secondary">
              本系统不代提交申诉、不代标已读；下列信息为千帆后台实时只读数据。
            </Text>
            <Descriptions column={1} size="small" bordered title="违规对象">
              <Descriptions.Item label="对象">
                {renderText(detail.entity_name)}（{renderText(detail.entity_type_name)}）
              </Descriptions.Item>
              <Descriptions.Item label="编号">{renderText(detail.entity_id)}</Descriptions.Item>
            </Descriptions>
            <Descriptions column={1} size="small" bordered title="违规信息">
              <Descriptions.Item label="违规原因">
                {renderText(detail.violation_reason)}
              </Descriptions.Item>
              <Descriptions.Item label="违规详情">
                <div style={{ whiteSpace: "pre-wrap" }}>
                  {renderText(detail.violation_desc)}
                </div>
              </Descriptions.Item>
              <Descriptions.Item label="处罚方式">
                {(detail.action_details || []).length ? (
                  <Space direction="vertical" size={2}>
                    {detail.action_details.map((action: ViolationActionDetail, index: number) => (
                      <Text key={`${action.code || action.name}-${index}`}>
                        {renderText(action.name)}
                        {action.time_info ? ` · ${action.time_info}` : ""}
                      </Text>
                    ))}
                  </Space>
                ) : (
                  renderActions(detail.actions)
                )}
              </Descriptions.Item>
              <Descriptions.Item label="违规单号">
                {renderText(detail.violation_id)}
              </Descriptions.Item>
              <Descriptions.Item label="违规时间">{renderTime(detail.violation_time)}</Descriptions.Item>
            </Descriptions>

            <Descriptions column={1} size="small" bordered title="处罚解除建议">
              <Descriptions.Item label="平台建议">
                <div style={{ whiteSpace: "pre-wrap" }}>{renderText(detail.modify_advice)}</div>
                {detail.modify_advice_link ? (
                  <Link href={detail.modify_advice_link} target="_blank">
                    查看更多
                  </Link>
                ) : null}
              </Descriptions.Item>
            </Descriptions>

            <Descriptions column={1} size="small" bordered title="操作历史">
              <Descriptions.Item label="申诉记录">
                {appealRecords.length === 0 ? "暂无申诉记录" : `${appealRecords.length} 条`}
              </Descriptions.Item>
              <Descriptions.Item label="违规证据">
                {!evidenceLoaded
                  ? "-"
                  : evidence
                    ? "平台已提供证据（详见千帆后台）"
                    : "平台未提供证据"}
              </Descriptions.Item>
            </Descriptions>

            <Divider style={{ margin: 0 }} />
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Text>认为判定有误？可到千帆后台申诉</Text>
              {detail.can_appeal ? (
                <>
                  <Text type="secondary">申诉成功处罚将直接撤销、违规不成立，对您店铺无后续影响</Text>
                  {/*
                    ⚠️ 按钮文案必须与顶部说明一致（用户 2026-10-18）：
                    本系统**不代提交申诉** ⇒ 这里不能叫「去申诉」（读起来像真能提交），
                    也不能用 primary（主按钮 = 鼓励点，暗示可完成提交）。
                    改成默认按钮 + 「查看申诉材料要求」：打开的是**只读模板**，
                    真实的提交入口在千帆后台。
                  */}
                  <Button onClick={() => setAppealOpen(true)}>查看申诉材料要求</Button>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    本系统只展示申诉表单与材料要求，不代提交；提交请在千帆后台完成。
                  </Text>
                </>
              ) : (
                <Text type="secondary">{renderText(detail.appeal_status_name)}</Text>
              )}
            </Space>
          </Space>
        ) : (
          <Empty description="没有取到详情" />
        )}
      </Drawer>

      {/* ---- 申诉弹窗（一比一复刻，但**不代提交**）---- */}
      <Modal
        title="申诉材料要求（只读）"
        open={appealOpen}
        centered
        onCancel={() => setAppealOpen(false)}
        // ⚠️ 不许写「确认提交」—— 这个按钮**不会提交**任何东西（用户 2026-10-18）
        okText="知道了"
        cancelText="取消"
        cancelButtonProps={{ style: { display: "none" } }}
        onOk={() => {
          // 只是关闭说明弹窗 —— 这里**没有**任何提交动作（只读边界）。
          setAppealOpen(false);
        }}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          {/*
            ⚠️ 这一屏是**材料要求说明**，不是可填写的表单（用户 2026-10-18）：
            原来放了可输入的申诉说明 + 可选文件的「选择文件」，但两者都不会被提交
            —— 用户填完点提交才发现是假的。只读展示更诚实。
          */}
          <Text type="secondary">认真提交申诉材料成功率更高</Text>
          <div>
            <Text>申诉说明</Text>
            <div style={{ marginTop: 4 }}>
              <Text type="secondary">
                不超过 {APPEAL_NOTE_MAX} 字，说明实际情况与依据（在千帆后台填写）。
              </Text>
            </div>
          </div>
          <div>
            <Text>申诉凭证</Text>
            <div style={{ marginTop: 4 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                单个文件 10 MB 以内，支持 jpg、png、pdf、zip、rar 格式，最多 5 个。
              </Text>
            </div>
          </div>
          <Text type="warning">本系统仅展示申诉材料要求，不代提交；请在千帆后台完成提交。</Text>
        </Space>
      </Modal>
    </Flex>
  );
}

// --------------------------------------------------------------------------- #
// 店铺体检面板
// --------------------------------------------------------------------------- #
function CheckupPanel(props: {
  loading: boolean;
  tab: CheckupTabKey;
  onTab: (tab: CheckupTabKey) => void;
  risks: ViolationRow[];
  riskTotal: number;
  advice: CheckupAdviceResponse | null;
  negativeFeedback: ViolationPagedResponse | null;
  onOpenViolation: (violationId: string) => void;
}) {
  const { loading, tab, onTab, risks, riskTotal, advice, negativeFeedback, onOpenViolation } = props;
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);

  const highPrice = advice?.high_price;
  const negativeTotal = negativeFeedback?.total ?? 0;
  const adviceItems = (advice?.items ?? []) as Record<string, unknown>[];
  const negativeItems = (negativeFeedback?.items ?? []) as Record<string, unknown>[];

  // 顶部三个「✓」项：与千帆一致，按当前 Tab 的数据源判断「有无」
  const hasViolationRisk = riskTotal > 0;
  const hasNegativeRisk = negativeTotal > 0;
  const hasAdvice = adviceItems.length > 0;

  /** 各 Tab 的列定义（千帆原页是一个表格随 Tab 换列，不是多个卡片）。 */
  const columnsFor = (key: CheckupTabKey): ColumnsType<Record<string, unknown>> => {
    if (key === "advice") {
      return [
        {
          title: "风险场景",
          key: "scene",
          width: 280,
          render: (_: unknown, raw) => renderText(raw.title ?? raw),
        },
        { title: "风险影响", key: "impact", width: 300, render: () => "-" },
        {
          title: "处理建议",
          key: "advice",
          render: (_: unknown, raw) => (
            <div style={{ whiteSpace: "pre-wrap" }}>{renderText(raw.desc ?? raw.title ?? raw)}</div>
          ),
        },
        { title: "操作", key: "action", width: 100, render: () => "-" },
      ];
    }
    if (key === "negative-feedback-risk") {
      return [
        {
          title: "风险场景",
          key: "scene",
          width: 280,
          render: (_: unknown, raw) => renderText(raw.badcaseType),
        },
        { title: "风险影响", key: "impact", width: 300, render: () => "-" },
        {
          title: "处理建议",
          key: "advice",
          render: (_: unknown, raw) => renderText(raw.desc),
        },
        { title: "操作", key: "action", width: 100, render: () => "-" },
      ];
    }
    // 违规风险（含「全部」）
    return [
      {
        title: "风险场景",
        dataIndex: "entity_type_name",
        width: 280,
        render: (_: unknown, raw) => {
          const row = raw as unknown as ViolationRow;
          return (
            <Space direction="vertical" size={0}>
              <Text>{renderText(row.entity_type_name)}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {renderText(row.violation_reason)}
              </Text>
            </Space>
          );
        },
      },
      {
        title: "风险影响",
        dataIndex: "actions",
        width: 300,
        render: (_: unknown, raw) => {
          const row = raw as unknown as ViolationRow;
          return (
            <Space direction="vertical" size={0}>
              <Tag color={statusTagColor(row.status)}>{renderText(row.status_name)}</Tag>
              <Text>{renderActions(row.actions)}</Text>
            </Space>
          );
        },
      },
      {
        title: "处理建议",
        dataIndex: "modify_advice",
        render: (_: unknown, raw) => (
          <div style={{ whiteSpace: "pre-wrap" }}>{renderText((raw as unknown as ViolationRow).modify_advice)}</div>
        ),
      },
      {
        title: "操作",
        key: "action",
        width: 100,
        render: (_: unknown, raw) => {
          const violationId = (raw as unknown as ViolationRow).violation_id;
          return (
            <Button type="link" size="small" onClick={() => violationId && onOpenViolation(violationId)}>
              去查看
            </Button>
          );
        },
      },
    ];
  };

  // 「全部」= 违规风险 + 负反馈风险 + 经营建议 三类合并成一张表
  const activeKey: CheckupTabKey = tab;
  const rows: Record<string, unknown>[] = useMemo(() => {
    const v = risks as unknown as Record<string, unknown>[];
    const n = negativeItems;
    const a = adviceItems;
    if (activeKey === "advice") return a;
    if (activeKey === "negative-feedback-risk") return n;
    if (activeKey === "violation-risk") return v;
    return [...v, ...n, ...a];
  }, [activeKey, risks, negativeItems, adviceItems]);

  // 换 Tab 回第 1 页（否则停在第 3 页会出现「空表」）
  useEffect(() => {
    setPage(1);
  }, [activeKey]);

  // 顶部一句话：千帆原页是「店铺经营状况良好，未检测到待优化项」
  const headline =
    !hasViolationRisk && !hasNegativeRisk && !hasAdvice
      ? "店铺经营状况良好，未检测到待优化项"
      : `检测到影响经营的 ${riskTotal + negativeTotal + adviceItems.length} 个待优化项，建议优先处理可整改事项`;

  return (
    <Flex vertical gap={10} style={{ flex: 1, minHeight: 0 }}>
      {/* ① 状态条（千帆原页顶部那一行：图标 + 结论 + 三项 ✓ + 时间范围） */}
      <Card size="small" styles={{ body: { padding: "14px 16px" } }}>
        <Flex align="center" gap={12} wrap>
          <div
            style={{
              width: 40,
              height: 40,
              borderRadius: 8,
              background: hasViolationRisk || hasNegativeRisk ? "#fff7e6" : "#f6ffed",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 20,
              flexShrink: 0,
            }}
          >
            {hasViolationRisk || hasNegativeRisk ? "⚠️" : "🛡️"}
          </div>
          <Space direction="vertical" size={2}>
            <Text strong style={{ fontSize: 15 }}>{headline}</Text>
            <Space size={16} wrap>
              <Text type="secondary">
                {hasViolationRisk ? "✗ 存在违规风险" : "✓ 暂无违规风险"}
              </Text>
              <Text type="secondary">
                {hasAdvice ? "✗ 存在经营建议" : "✓ 暂无经营建议"}
              </Text>
              <Text type="secondary">
                {hasNegativeRisk ? "✗ 存在负反馈风险" : "✓ 暂无负反馈风险"}
              </Text>
              <Text type="secondary">
                体检时间范围：{checkupRangeText()}
              </Text>
            </Space>
          </Space>
        </Flex>
      </Card>

      {/* ② Tab + ③ 单表格（含表头 / 空态 / 分页），与千帆原页同构 */}
      <Card size="small" style={{ flex: 1, minHeight: 0, overflow: "auto" }} styles={{ body: { padding: 0 } }}>
        {/* ⚠️ 不要给 Tabs 设 height:100% —— 它会把表格挤出可视区（实测只剩空白） */}
        <Tabs
          activeKey={activeKey}
          onChange={(key) => onTab(key as CheckupTabKey)}
          style={{ padding: "0 16px" }}
          items={CHECKUP_TABS.map((item) => ({
            key: item.key,
            label: item.key === "violation-risk"
              ? `${item.label}(${riskTotal})`
              : item.key === "negative-feedback-risk"
                ? `${item.label}(${negativeTotal})`
                : item.label,
          }))}
        />
        {/* ⚠️ indicator={<></>}：转圈由 <WaitMask open={loading}> 统一画（同一状态两处驱动 ⇒ 双层圈）。 */}
        <Spin indicator={<></>} spinning={loading}>
          <Table
            size="small"
            rowKey={(record, index) => String(record.violation_id ?? `${activeKey}-${index}`)}
            columns={columnsFor(activeKey)}
            dataSource={rows}
            // ⚠️ 必须 fixed：默认 auto 布局下 antd 会把「处理建议」拉满，
            // 把「风险场景」挤成逐字竖排（实测：280px 设置被忽略）
            tableLayout="fixed"
            scroll={{ x: 1000 }}
            locale={{ emptyText: <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            pagination={{
              current: page,
              pageSize,
              total: rows.length,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 30, 40, 50],
              showTotal: (total: number) => `共 ${total} 条`,
              onChange: (nextPage: number, nextSize: number) => {
                setPage(nextPage);
                setPageSize(nextSize);
              },
            }}
          />
        </Spin>
      </Card>
      {/* 高价商品只在有数据时才补一行（千帆不显示 0） */}
      {highPrice && (highPrice.highPriceItemNum ?? 0) > 0 ? (
        <Text type="secondary">
          <Tag color="green">{renderText(highPrice.tagDesc)}</Tag>
          高价商品 {highPrice.highPriceItemNum} 个
        </Text>
      ) : null}
    </Flex>
  );
}

/** 体检时间范围：近 30 天（千帆原页固定展示「体检时间范围：起～止」）。 */
function checkupRangeText(): string {
  return `${dayjs().subtract(30, "day").format("YYYY.M.D")} ~ ${dayjs().format("YYYY.M.D")}`;
}

// --------------------------------------------------------------------------- #
// 小工具
// --------------------------------------------------------------------------- #
/** 平台错误 → 一句人话（后端把平台原文放在 `detail.message`）。 */
function describeError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const payload = detail as { message?: string; code?: string };
    if (payload.message) return payload.code ? `${payload.message}（${payload.code}）` : payload.message;
  }
  return fallback;
}

/**
 * 按中文表头从平台原始行里取值（赔付 / 仲裁 / 免罚的字段名因账号而异，
 * 本店这些列表均为空，所以不硬编码键名 —— 取不到就渲染 `-`，不编造）。
 */
function pickByTitle(row: Record<string, unknown>, title: string): unknown {
  const direct = row[title];
  if (direct !== undefined) return direct;
  const aliases: Record<string, string> = {
    赔付单编号: "compensateId",
    赔付对象: "displayName",
    赔付原因: "compensateReason",
    处罚方式: "punishAction",
    赔付时间: "compensateTime",
    申诉状态: "appealStatusName",
    处罚状态: "punishStatusName",
    仲裁ID: "arbitrationId",
    仲裁对象: "displayName",
    仲裁原因: "arbitrationReason",
    仲裁时间: "arbitrationTime",
    售后仲裁处理: "handleResult",
    仲裁处理状态: "arbitrationStatusName",
    "爆单日期(支付日期)": "payDate",
    宽限期内爆单订单量: "orderCount",
    违规考核宽限期: "gracePeriod",
    宽限期发货超时率: "delayRate",
    "宽限期后72小时发货率": "finishRate",
    是否满足履约好标准: "meetStandard",
  };
  const key = aliases[title];
  return key ? row[key] : undefined;
}
