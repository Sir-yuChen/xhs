/**
 * 「批量同步商品信息」弹窗（2026-10-06 用户口径，v2）。
 *
 * 用户原话：「批量同步商品信息 · 按 ID 同步 20 个 —— 弹窗勾选商品后，将勾选的商品
 * 使用**列表**在弹窗中展示，使用 **tab** 的方式分为**按条件同步**和**按 ID 同步商品信息**，
 * 点击同步等待同步商品结果，如果按条件同步则**直接关闭弹窗触发定时任务后台执行**」。
 *
 * ## 两个 Tab 是**两种执行模型**，不是两种筛选方式
 *
 * | Tab | 执行模型 | 关闭时机 |
 * |---|---|---|
 * | **按条件同步** | 触发**后台任务**（整店，按全量 / 时间区间） | **点同步即关弹窗** —— 任务在后台跑，进度去「账号矩阵 → 任务日志」看 |
 * | **按 ID 同步商品信息** | **前台等待**（后端按 ID 整店扫描后回结果） | 结果回来才关（逐条状态就在本 Tab 里） |
 *
 * ⚠️ 为什么条件同步要**先关弹窗**：它是整店翻页 + 限流等待（几十秒到几分钟），
 * 留在弹窗里等就是「一个不知道要转多久的圈」；关掉它、把进度交给任务日志，
 * 用户才能干别的（与账号卡片页那套做法一致，两边同一个观感）。
 *
 * ## 逐条结果必须落在**提交时那份 ID 清单**上
 *
 * 后端返回 `{synced, not_found, scanned, matched, note}` 五个数 —— **不给逐条明细**
 * （平台按页过滤，命中的混在多页里）。做法：提交前把 ID 全部铺成「同步中」行，
 * 拿到结果按 `not_found` 标「已更新 / 未找到」。**只增不改顺序** ⇒ 用户看到的始终是
 * 「我提交过的每一个」，而不是「服务端最后返回的那批」（后者会让没命中的凭空消失）。
 */
import {
  Alert,
  App,
  Button,
  DatePicker,
  Empty,
  Input,
  Modal,
  Radio,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { SyncOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchQueueRecords,
  fetchXhsShopItems,
  readErrorDetail,
  syncSpecifiedShopItems,
} from "../../../lib/api";
import type { SyncOptions } from "../../../components/platforms/sync-options-modal";
import { MAX_OPERATE_ITEMS } from "../../../lib/shop-items-view";
import type { XhsShopItem } from "../../../types";

const { Text } = Typography;

/** 无限列表每页条数（与台账列表同一档，滚动一屏大约触发两次取数）。 */
const PAGE_SIZE = 50;

/** 轮询批次终态的间隔与上限（同步单件通常 1~2 秒；上限 2 分钟兜底）。 */
const POLL_INTERVAL_MS = 1500;
const POLL_MAX_ATTEMPTS = 80;

type GoodsSyncOutcome = { item_id: string; status: "done" | "failed"; reason: string };

/**
 * 按批次号轮询「同步商品信息」批次树，等全部子行终态后返回逐件结果。
 * 超时返回已知的部分结果（未终态的件不出现 ⇒ 调用方标 missing，不硬凑成功）。
 */
async function pollGoodsSyncBatch(batchNo: string): Promise<GoodsSyncOutcome[]> {
  for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    try {
      const page = await fetchQueueRecords(undefined, batchNo);
      const batch = (page.items ?? [])[0];
      const children = (batch?.children ?? []) as Array<{
        task_type: string;
        payload?: Record<string, unknown> | null;
      }>;
      const items = children.filter((c) => c.task_type === "xhs_goods_sync_item");
      if (!batch || items.length === 0) continue;
      const allDone = items.every((c) => {
        const p = (c.payload ?? {}) as { ok?: boolean };
        return p.ok === true || p.ok === false;
      });
      if (!allDone) continue;
      return items.map((c) => {
        const p = (c.payload ?? {}) as { item_id?: string; ok?: boolean; reason?: string };
        return {
          item_id: String(p.item_id ?? ""),
          status: p.ok ? ("done" as const) : ("failed" as const),
          reason: String(p.reason ?? ""),
        };
      });
    } catch {
      //: 单次轮询失败不打断（网络抖动）；重试到上限为止。
    }
  }
  return [];
}

/** 一行「要同步的商品 + 它的同步状态」。`todo` = 还没提交过（打开时铺的清单）。 */
type SyncRow = {
  item_id: string;
  /** 商品名（主表勾选 / 从台账补充时已知；**手输 ID** 时为空 ⇒ 只显示 ID）。 */
  title: string;
  /** 缩略图地址（同上：手输 ID 时为空 ⇒ 显示占位）。 */
  image: string;
  status: "todo" | "pending" | "ok" | "missing" | "failed";
  note: string;
};

/** 按分隔符切 ID（中英文逗号 / 换行 / 空格都认，与列表关键词同宽容度）。 */
function parseIds(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const part of text.split(/[\s,，;；]+/)) {
    const id = part.trim();
    if (id && !seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  return out;
}

/** 今天（本地时区）的 `YYYY-MM-DD`，用作结束日期默认值。 */
function today(): string {
  return dayjs().format("YYYY-MM-DD");
}

/** 待同步行（「我要同步的是这些」的清单项）。 */
function todoRow(itemId: string, title = "", image = ""): SyncRow {
  return { item_id: itemId, title, image, status: "todo", note: "待同步" };
}

export function SyncSpecifiedItemsModal({
  open,
  platformAccountId,
  items,
  onClose,
  onSynced,
  onConditionSync,
}: {
  open: boolean;
  platformAccountId: number | null;
  /**
   * 主表勾选的商品 ID —— 打开后**以列表形式展示**在本弹窗的「按 ID 同步」Tab 里
   * （2026-10-06 用户口径「将勾选的商品使用列表在弹窗中展示」）。
   *
   * ⚠️ **不再「打开即同步」**：新口径是「**点击同步**等待同步商品结果」⇒
   * 用户要先看到自己选了哪些，再决定点（v1 的自动开跑会让「点击同步」这个动作落空）。
   */
  items: { item_id: string; title: string; image: string }[];
  onClose: () => void;
  /** 按 ID 同步成功后的回调（页面用来重拉当前页 —— 台账数据变了）。 */
  onSynced: () => void;
  /**
   * 「按条件同步」确认 ⇒ **关弹窗 + 触发后台任务**（由页面执行，它才有
   * `syncShopGoods` 那套门禁与轮询状态）。⚠️ 本弹窗**不等待**这条路的结果。
   */
  onConditionSync: (options: SyncOptions) => void;
}) {
  const { message } = App.useApp();
  const [tab, setTab] = useState("condition");
  const [draft, setDraft] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [rows, setRows] = useState<SyncRow[]>([]);
  const [summary, setSummary] = useState("");
  /** 等待蒙版「已耗时（秒）」（2026-10-11 用户口径「等待期间加蒙版统计耗时」）。 */
  const [waitElapsedSec, setWaitElapsedSec] = useState(0);
  const waitStartedAtRef = useRef<number | null>(null);
  const waitTickRef = useRef<number | null>(null);
  /** 待同步列表里勾选的行（批量移除用）。 */
  const [checkedRows, setCheckedRows] = useState<string[]>([]);
  /** 是否展开「从台账选择补充」（次级入口：台账里还没有的商品只能手输 ID）。 */
  const [pickerOpen, setPickerOpen] = useState(false);
  /** 台账候选列表的**名称模糊查询**（2026-10-06 用户口径「列表之前名称模糊查询」）。 */
  const [keyword, setKeyword] = useState("");

  // ---- 条件同步（Tab 1）----
  const [scope, setScope] = useState<"full" | "range">("full");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState(today());

  // ---- 无限列表（Tab 2 的次级入口）----
  const [options, setOptions] = useState<XhsShopItem[]>([]);
  const [total, setTotal] = useState(0);
  const [pageNo, setPageNo] = useState(1);
  const [loadingMore, setLoadingMore] = useState(false);
  /** 请求序号：切账号 / 重开弹窗后，**在途的旧响应必须丢弃**（否则列表被旧数据覆盖）。 */
  const requestSeq = useRef(0);

  const loadPage = useCallback(async (accountId: number, page: number, seq: number) => {
    setLoadingMore(true);
    try {
      const response = await fetchXhsShopItems({
        platform_account_id: accountId,
        page_no: page,
        page_size: PAGE_SIZE,
        //: 2026-10-06 用户口径：「列表之前名称模糊查询」⇒ 关键词透给后端（服务端筛，
        //: 不是前端在已加载的那一页里过滤 —— 那样只会搜到「看过的」）。
        ...(keyword.trim() ? { keyword: keyword.trim() } : {}),
      });
      //: ⚠️ 丢弃过期响应（账号换了 / 弹窗重开）—— 不丢的话会出现
      //: 「列表里混着上一个账号的商品」，而用户会照着它去同步。
      if (seq !== requestSeq.current) return;
      setOptions((current) => (page === 1 ? response.items : [...current, ...response.items]));
      setTotal(response.total ?? 0);
    } catch (error) {
      //: ⚠️ 2026-10-06 改为**要说话**：原来「安静吞掉」⇒ 用户点开「从台账选择补充」
      //: 看到空列表，完全不知道为什么（本次实测报的就是这个观感）。
      message.error(readErrorDetail(error, "台账列表加载失败"));
    } finally {
      if (seq === requestSeq.current) setLoadingMore(false);
    }
  }, [keyword]);

  /** 进入等待：起表。**幂等**（已在等待时不重起秒表 —— 否则耗时被反复清零）。 */
  const beginWait = useCallback(() => {
    if (waitStartedAtRef.current === null) {
      waitStartedAtRef.current = Date.now();
      setWaitElapsedSec(0);
      if (waitTickRef.current !== null) window.clearInterval(waitTickRef.current);
      waitTickRef.current = window.setInterval(() => {
        if (waitStartedAtRef.current !== null) {
          setWaitElapsedSec(Math.floor((Date.now() - waitStartedAtRef.current) / 1000));
        }
      }, 1000);
    }
  }, []);
  /** 结束等待：**停表**。 */
  const endWait = useCallback(() => {
    waitStartedAtRef.current = null;
    if (waitTickRef.current !== null) {
      window.clearInterval(waitTickRef.current);
      waitTickRef.current = null;
    }
  }, []);

  /** 真正发请求：把一份 ID 清单交给后端，入队后凭批次号轮询等终态。 */
  const runSync = useCallback(
    async (ids: string[]) => {
      if (!platformAccountId) return;
      //: ⚠️ 提交即把**全部** ID 标成「同步中」（用户马上看到「我要同步的是这些」），
      //: 再去补真实状态 —— 反过来的话慢扫描期间列表是空的，像是没提交成功。
      //: ⚠️ **保留已知的名称 / 缩略图**：重铺只为把状态改成「同步中」，
      //: 丢掉它们会让列表在同步过程中退回裸 ID（用户刚看到的商品名消失）。
      setRows((current) => {
        const known = new Map(current.map((row) => [row.item_id, row]));
        return ids.map((id) => ({
          item_id: id,
          title: known.get(id)?.title ?? "",
          image: known.get(id)?.image ?? "",
          status: "pending" as const,
          note: "",
        }));
      });
      setSummary("");
      setRunning(true);
      beginWait();
      try {
        //: 2026-10-11 改**走同步商品信息队列**：`items/sync` 只入队（返回
        //: `{enqueued, already, batch_no, note}`），真实同步由 worker 异步执行。
        //: 前端凭 `batch_no` 轮询批次树，等全部子行终态后按子行 payload 回填结果。
        const result = await syncSpecifiedShopItems(platformAccountId, ids);
        const batchNo = String(result.batch_no || "");
        if (!batchNo) {
          //: 没批次号 = 一次都没入队成功（全是非法 ID / 已在队列）—— 如实报。
          setSummary(result.note || "没有可执行的商品同步项");
          message.warning(result.note || "没有可执行的商品同步项");
          onSynced();
          return;
        }
        setSummary(`已加入同步商品信息队列（批次 ${batchNo}），执行中…`);
        message.info(`已入队 ${result.enqueued} 件商品，执行中…`);
        const outcome = await pollGoodsSyncBatch(batchNo);
        const failed = outcome.filter((o) => o.status === "failed");
        setRows((current) => {
          //: 终态行**带着名称与缩略图**（不然结果出来后列表反而变差）。
          const known = new Map(current.map((row) => [row.item_id, row]));
          return ids.map((id) => {
            const hit = outcome.find((o) => o.item_id === id);
            const status = !hit ? ("missing" as const) : (hit.status === "failed" ? "failed" as const : "ok" as const);
            const note = !hit
              ? "平台侧没有这件商品（ID 是否正确 / 是否已删除）"
              : hit.reason || "已更新本地台账";
            return {
              item_id: id,
              title: known.get(id)?.title ?? "",
              image: known.get(id)?.image ?? "",
              status,
              note,
            };
          });
        });
        if (failed.length) {
          message.warning(`已入队 ${ids.length} 件，${failed.length} 件同步失败（原因见行说明）`);
        } else {
          message.success(`已更新 ${ids.length} 件商品`);
        }
        onSynced();
      } catch (error) {
        //: 用 lib 的**唯一**实现（与账号卡片页同一句话，DEF-044）。
        const text = readErrorDetail(error, "同步失败");
        setRows((current) =>
          current.map((row) =>
            row.status === "pending" ? { ...row, status: "failed", note: text } : row,
          ),
        );
        setSummary(text);
        message.error(text);
      } finally {
        endWait();
        setRunning(false);
      }
    },
    [platformAccountId, message, onSynced, beginWait, endWait],
  );

  //: 打开 ⇒ 把勾选集**铺成待同步列表**（用户口径「将勾选的商品使用列表在弹窗中展示」）。
  //: 有勾选 ⇒ 落在「按 ID」Tab（点这个按钮就是来同步这些的）；没勾选 ⇒ 落在「按条件」。
  //: ⚠️ 依赖里**没有** `itemIds`：它是打开那一刻页面给的值，由 `open` 这一轮读取
  //: （把数组放进依赖会因为父组件每次渲染新建数组而反复重置清单）。
  useEffect(() => {
    if (!open || !platformAccountId) {
      return;
    }
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    setOptions([]);
    setTotal(0);
    setPageNo(1);
    setPicked([]);
    setSummary("");
    setDraft("");
    setPickerOpen(false);
    setKeyword("");
    setScope("full");
    setStartDate("");
    setEndDate(today());
    setTab(items.length > 0 ? "id" : "condition");
    setRows(items.map((row) => todoRow(row.item_id, row.title, row.image)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, platformAccountId]);

  /**
   * 按**显式关键词**取一页（搜索框用）。
   *
   * ⚠️ 为什么需要它：`loadPage` 读的是 `keyword` **state**，而搜索框刚 `setKeyword`
   * 时 state 还没生效 ⇒ 用 `loadPage` 会拿**旧词**去查（换了关键词却没生效的观感）。
   * 这里把词当参数传，取数行为与用户看到的输入框**同一次**。
   */
  async function loadPageWithKeyword(
    accountId: number,
    page: number,
    seq: number,
    kw: string,
  ) {
    setLoadingMore(true);
    try {
      const response = await fetchXhsShopItems({
        platform_account_id: accountId,
        page_no: page,
        page_size: PAGE_SIZE,
        ...(kw.trim() ? { keyword: kw.trim() } : {}),
      });
      if (seq !== requestSeq.current) return;
      setOptions(response.items);
      setTotal(response.total ?? 0);
    } catch (error) {
      message.error(readErrorDetail(error, "台账列表加载失败"));
    } finally {
      if (seq === requestSeq.current) setLoadingMore(false);
    }
  }

  /** 滚到底 ⇒ 取下一页（**这就是「无限列表」**：不用翻页器）。 */
  function handleScroll(event: React.UIEvent<HTMLDivElement>) {
    if (!platformAccountId || loadingMore) return;
    const target = event.currentTarget;
    const nearBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 80;
    if (!nearBottom) return;
    if (options.length >= total) return;
    const next = pageNo + 1;
    setPageNo(next);
    void loadPage(platformAccountId, next, requestSeq.current);
  }

  /** 「按条件同步」：**关弹窗 + 触发后台任务**（不在这里等结果）。 */
  function submitConditionSync() {
    if (!platformAccountId) return;
    if (scope === "range" && !startDate) {
      message.warning("请选择起始日期（或切到全量同步）");
      return;
    }
    const start = startDate ? dayjs(startDate) : null;
    const end = endDate ? dayjs(endDate) : null;
    //: 起止写反时**在前端就交换**（与后端同一口径，用户看到的就是实际生效的区间）。
    const reversed = Boolean(start && end && start.isAfter(end));
    onConditionSync(
      scope === "full"
        ? { scope: "full", startDate: "", endDate: "" }
        : {
            scope: "range",
            startDate: reversed ? endDate : startDate,
            endDate: reversed ? startDate : endDate,
          },
    );
    onClose();
  }

  /** 「按 ID 同步」：发请求并**在本 Tab 等结果**。 */
  async function submitIdSync() {
    if (running) return;
    //: 优先同步「还没跑过」的那批；都跑过了 ⇒ 对当前清单重跑（拉最新数据）。
    const todo = rows.filter((row) => row.status === "todo").map((row) => row.item_id);
    const targets = todo.length > 0 ? todo : rows.map((row) => row.item_id);
    if (targets.length === 0) {
      message.info("先勾选要同步的商品（或手动填入商品 ID）");
      return;
    }
    if (targets.length > MAX_OPERATE_ITEMS) {
      message.warning(`单次最多同步 ${MAX_OPERATE_ITEMS} 个商品，本次 ${targets.length} 个。`);
      return;
    }
    await runSync(targets);
  }

  /**
   * 把「从台账选择」勾的 / 手输的 ID 并进待同步列表（**只增不重复**）。
   *
   * ⚠️ 从台账来的一并带上**名称与缩略图**（列表里直接能认出来）；
   * 手输 ID 的没有元数据 ⇒ 留空，由同步结果补全（缺值口径：不猜）。
   */
  function addItems(rowsToAdd: { item_id: string; title: string; image: string }[]) {
    if (rowsToAdd.length === 0) return;
    setRows((current) => {
      const have = new Set(current.map((row) => row.item_id));
      const added = rowsToAdd
        .filter((row) => !have.has(row.item_id))
        .map((row) => todoRow(row.item_id, row.title, row.image));
      const next = [...current, ...added];
      if (next.length > MAX_OPERATE_ITEMS) {
        message.warning(`最多 ${MAX_OPERATE_ITEMS} 个/次，超出部分未加入。`);
        return next.slice(0, MAX_OPERATE_ITEMS);
      }
      return next;
    });
    setPicked([]);
    setDraft("");
  }

  /** 手输 ID（无元数据）。 */
  function addIds(ids: string[]) {
    addItems(ids.map((id) => ({ item_id: id, title: "", image: "" })));
  }

  const statusTag = (status: SyncRow["status"]) =>
    status === "pending" ? (
      <Tag icon={<SyncOutlined spin />} color="blue">
        同步中
      </Tag>
    ) : status === "todo" ? (
      <Tag>待同步</Tag>
    ) : status === "ok" ? (
      <Tag color="green">已更新</Tag>
    ) : status === "missing" ? (
      <Tag color="orange">未找到</Tag>
    ) : (
      <Tag color="red">失败</Tag>
    );

  /**
   * 「商品信息」单元格（2026-10-06 用户口径：「优化为**商品信息列**，缩略图，商品名称，
   * 商品ID；注意商品名称不要过长使用 **tip** 展示全部」）。
   *
   * ⚠️ 抽成共用函数：**待同步列表与台账候选列表必须长得一样**（用户口径
   * 「台账列表商品名称改为商品信息列与待同步列展示一样」）—— 两处各写一份迟早分叉。
   */
  function itemInfoCell(row: { item_id: string; title: string; image: string }) {
    return (
      <Space size={8}>
        {row.image ? (
          <img
            src={row.image}
            alt=""
            width={40}
            height={40}
            style={{ objectFit: "cover", borderRadius: 4, flex: "0 0 auto" }}
          />
        ) : (
          <div
            style={{
              width: 40,
              height: 40,
              borderRadius: 4,
              background: "#f0f0f0",
              flex: "0 0 auto",
            }}
          />
        )}
        <div style={{ minWidth: 0 }}>
          {/* ⚠️ 名称用 Tooltip 兜全：列里截断，悬停看全称（用户口径「用 tip 展示全部」）。 */}
          <Tooltip title={row.title || "（名称待同步后补全）"}>
            <div
              style={{
                maxWidth: 260,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {row.title || "-"}
            </div>
          </Tooltip>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {row.item_id}
          </Text>
        </div>
      </Space>
    );
  }

  const resultColumns = [
    { title: "商品信息", key: "item", render: (_v: unknown, row: SyncRow) => itemInfoCell(row) },
    {
      title: "同步状态",
      dataIndex: "status",
      width: 110,
      render: (value: SyncRow["status"]) => statusTag(value),
    },
    { title: "说明", dataIndex: "note", render: (value: string) => value || "-" },
  ];

  const rangeInvalid = scope === "range" && !startDate;
  const idRunning = running && tab === "id";

  return (
    <Modal
      centered
      open={open}
      title="批量同步商品信息"
      //: 2026-10-06 用户口径「弹窗全屏的 80%」。
      width="80vw"
      onCancel={onClose}
      //: ⚠️ 按 ID 在跑时不许关（结果就在这个 Tab 里，关了就丢）——
      //: 与上架/下架的结果弹窗同一条纪律（2026-10-06 那轮的口径）。
      maskClosable={false}
      closable={!idRunning}
      //: 2026-10-06 用户口径「滚动条隐藏」。
      //: ⚠️ 只**隐藏外观**、不禁用滚动：`scroll.y` 还在，滚轮 / 拖拽照常。
      //: 两套写法都要给 —— Firefox 认 `scrollbar-width`，Chromium 认 `::-webkit-scrollbar`。
      styles={{
        body: { maxHeight: "64vh", overflow: "auto" },
      }}
      className="sync-items-modal"
      footer={
        //: ⚠️ 两个 Tab 的按钮是**两种执行模型**：条件是「触发即关」（后台跑），
        //: 按 ID 是「等结果」（前台等）。同一个位置，语义随 Tab 变。
        tab === "condition" ? (
          <Space>
            <Button onClick={onClose}>取消</Button>
            <Button type="primary" icon={<SyncOutlined />} onClick={submitConditionSync}>
              同步（后台执行）
            </Button>
          </Space>
        ) : (
          <Space>
            <Button onClick={onClose} disabled={running}>
              {running ? "同步中…" : "关闭"}
            </Button>
            <Button
              type="primary"
              icon={<SyncOutlined spin={running} />}
              loading={running}
              disabled={rows.length === 0}
              onClick={() => void submitIdSync()}
            >
              同步 {rows.length} 件商品
            </Button>
          </Space>
        )
      }
    >
      {/* 2026-10-11 用户口径「等待期间加蒙版统计耗时」：按 ID 同步入队后轮询等终态
          时整块盖住，tip 带已耗时，等结果返回再撤蒙版（关不掉 = 结果不丢）。 */}
      <Spin
        spinning={idRunning}
        tip={idRunning ? `同步中… 已耗时 ${waitElapsedSec}s` : ""}
      >
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
          {
            key: "condition",
            label: "按条件同步",
            children: (
              <Space direction="vertical" size={12} style={{ width: "100%" }}>
                <Alert
                  type="info"
                  showIcon
                  message="按条件同步整个店铺（后台执行）"
                  description="点「同步」后弹窗立即关闭，任务在后台跑；进度去「账号矩阵 → 任务日志」看。"
                />
                <Radio.Group
                  value={scope}
                  onChange={(e) => setScope(e.target.value as "full" | "range")}
                  style={{ width: "100%" }}
                >
                  <Space direction="vertical" style={{ width: "100%" }}>
                    <Radio value="full">全量同步（翻到平台没有下一页为止）</Radio>
                    <Radio value="range">按时间区间同步</Radio>
                  </Space>
                </Radio.Group>
                {scope === "range" && (
                  <Space wrap>
                    <DatePicker
                      value={startDate ? dayjs(startDate) : null}
                      onChange={(d: Dayjs | null) => setStartDate(d ? d.format("YYYY-MM-DD") : "")}
                      placeholder="起始日期"
                    />
                    <Text type="secondary">至</Text>
                    <DatePicker
                      value={endDate ? dayjs(endDate) : null}
                      onChange={(d: Dayjs | null) =>
                        setEndDate(d ? d.format("YYYY-MM-DD") : today())
                      }
                      placeholder="结束日期"
                    />
                  </Space>
                )}
                {rangeInvalid && <Text type="warning">请选择起始日期（或切到全量同步）。</Text>}
                <Text type="secondary" style={{ fontSize: 12 }}>
                  商品按创建时间过滤；不选区间则翻完整个商品列表。
                </Text>
              </Space>
            ),
          },
          {
            key: "id",
            label: "按 ID 同步商品信息",
            children: (
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  下面是要同步的商品（主表勾选的 + 你补充的）；点「同步」后在这里等结果。
                </Text>
                <Table<SyncRow>
                  rowKey="item_id"
                  size="small"
                  pagination={false}
                  scroll={{ y: 240 }}
                  dataSource={rows}
                  //: 2026-10-06 用户口径：「待同步信息列表新增复选框列，支持**批量移除**」；
                  //: 复选框列宽固定 **50px**。
                  rowSelection={{
                    selectedRowKeys: checkedRows,
                    onChange: (keys) => setCheckedRows(keys.map(String)),
                    columnWidth: 50,
                  }}
                  locale={{
                    emptyText: <Empty description="还没有要同步的商品（从台账选或手输 ID）" />,
                  }}
                  columns={resultColumns}
                />
                {/* 批量移除（勾选后一次剔掉；执行中不给动 —— 清单要与提交的一致）。 */}
                {rows.length > 0 && (
                  <Space size={8}>
                    <Button
                      size="small"
                      danger
                      disabled={checkedRows.length === 0 || running}
                      onClick={() => {
                        setRows((current) =>
                          current.filter((row) => !checkedRows.includes(row.item_id)),
                        );
                        setCheckedRows([]);
                      }}
                    >
                      移除选中的 {checkedRows.length} 件
                    </Button>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      已选 {checkedRows.length} / {rows.length}
                    </Text>
                  </Space>
                )}
                {summary ? (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {summary}
                  </Text>
                ) : null}

                {/* 次级入口：补充商品（台账里还没有的只能手输 ID）。 */}
                <Space size={8} wrap>
                  <Button
                    size="small"
                    onClick={() => {
                      const next = !pickerOpen;
                      setPickerOpen(next);
                      //: ⚠️ **必须在这里拉第一页**：v2 起打开弹窗只铺清单、不取台账，
                      //: 不加载就点开只会看到一个空列表（2026-10-06 用户实测报的就是这个）。
                      if (next && platformAccountId) {
                        setOptions([]);
                        setTotal(0);
                        setPageNo(1);
                        void loadPage(platformAccountId, 1, requestSeq.current);
                      }
                    }}
                  >
                    {pickerOpen ? "收起台账列表" : "从台账选择补充"}
                  </Button>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    最多 {MAX_OPERATE_ITEMS} 个/次
                  </Text>
                </Space>
                {pickerOpen && (
                  <Space direction="vertical" size={8} style={{ width: "100%" }}>
                    {/* 名称模糊查询（2026-10-06 用户口径「列表之前名称模糊查询」）：
                        落到**服务端** `keyword`（不是前端在已加载页里过滤）。 */}
                    <Input.Search
                      allowClear
                      placeholder="按商品名称 / ID 模糊查询"
                      value={keyword}
                      onChange={(event) => setKeyword(event.target.value)}
                      onSearch={(value) => {
                        setKeyword(value);
                        if (!platformAccountId) return;
                        const seq = requestSeq.current + 1;
                        requestSeq.current = seq;
                        setOptions([]);
                        setTotal(0);
                        setPageNo(1);
                        setPicked([]);
                        //: ⚠️ 关键词刚 setState，这里直接用入参 value 拼取数
                        //: （等 state 生效再取会拿到旧词 —— 实测过这类竞态）。
                        void loadPageWithKeyword(platformAccountId, 1, seq, value);
                      }}
                      style={{ maxWidth: 320 }}
                    />
                    {/* ⚠️ `virtual` + 固定 `scroll.y` 才是**真·无限列表**：不用翻页器、
                        不渲染全部行（2000+ 行会卡住）。滚动事件挂在 Table 的
                        `.ant-table-body` 上 —— 用 `onScroll` 透传到 wrapper。 */}
                    <div onScroll={handleScroll} style={{ maxHeight: 240, overflow: "auto" }}>
                      <Table<XhsShopItem>
                        rowKey="item_id"
                        size="small"
                        virtual
                        pagination={false}
                        scroll={{ y: 220, x: "max-content" }}
                        onScroll={handleScroll}
                        dataSource={options}
                        rowSelection={{
                          selectedRowKeys: picked,
                          onChange: (keys) => setPicked(keys.map(String)),
                          //: 复选框列宽（用户口径「复选框列 50px」）。
                          columnWidth: 50,
                        }}
                        locale={{
                          emptyText: loadingMore ? (
                            <Empty description="加载中…" />
                          ) : (
                            <Empty description="该账号台账里还没有商品（先执行「按条件同步」）" />
                          ),
                        }}
                        //: 2026-10-06 用户口径：「台账列表商品名称改为**商品信息列**与待同步列
                        //: 展示一样」⇒ 同一个 `itemInfoCell`（缩略图 + 名称 Tip + ID）。
                        columns={[
                          {
                            title: "商品信息",
                            key: "item",
                            render: (_v: unknown, row: XhsShopItem) => itemInfoCell(row),
                          },
                          { title: "状态", dataIndex: "status_label", width: 90 },
                        ]}
                        footer={() => (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            已加载 {options.length} / {total} 条
                            {options.length < total ? "（滚到底继续加载）" : ""}
                          </Text>
                        )}
                      />
                    </div>
                    <Button
                      size="small"
                      type="primary"
                      disabled={picked.length === 0}
                      onClick={() =>
                        addItems(
                          options
                            .filter((option) => picked.includes(option.item_id))
                            .map((option) => ({
                              item_id: option.item_id,
                              title: option.title || "",
                              image: option.image || "",
                            })),
                        )
                      }
                    >
                      加入待同步（{picked.length}）
                    </Button>
                    <Input.TextArea
                      value={draft}
                      onChange={(event) => setDraft(event.target.value)}
                      rows={2}
                      disabled={running}
                      placeholder="也可直接粘商品 ID（逗号 / 换行分隔），例如：6a581608ab42de00152e782c"
                    />
                    <Button
                      size="small"
                      disabled={!draft.trim()}
                      onClick={() => addIds(parseIds(draft))}
                    >
                      加入待同步
                    </Button>
                  </Space>
                )}
              </Space>
            ),
          },
        ]}
        />
      </Spin>
      {/* 滚动条隐藏（2026-10-06 用户口径）：只藏**外观**，滚轮 / 拖拽照常 ——
          给「细滚动条」而不是 `overflow: hidden`（后者会让内容滚不动）。
          ⚠️ 挂在 `.sync-items-modal` 下，只影响本弹窗，不动全局。 */}
      <style>{`
        .sync-items-modal .ant-table-body,
        .sync-items-modal .ant-table-content,
        .sync-items-modal .ant-modal-body {
          scrollbar-width: thin;
        }
        .sync-items-modal .ant-table-body::-webkit-scrollbar,
        .sync-items-modal .ant-table-content::-webkit-scrollbar,
        .sync-items-modal .ant-modal-body::-webkit-scrollbar {
          width: 6px;
          height: 6px;
        }
        .sync-items-modal .ant-table-body::-webkit-scrollbar-thumb,
        .sync-items-modal .ant-table-content::-webkit-scrollbar-thumb,
        .sync-items-modal .ant-modal-body::-webkit-scrollbar-thumb {
          background: rgba(0, 0, 0, 0.18);
          border-radius: 3px;
        }
        .sync-items-modal .ant-table-body::-webkit-scrollbar-track,
        .sync-items-modal .ant-table-content::-webkit-scrollbar-track,
        .sync-items-modal .ant-modal-body::-webkit-scrollbar-track {
          background: transparent;
        }
      `}</style>
    </Modal>
  );
}
