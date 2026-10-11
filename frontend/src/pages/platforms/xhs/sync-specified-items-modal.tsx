/**
 * 「批量同步商品信息」弹窗（2026-10-06 用户口径，v2 → 2026-10-11 v4）。
 *
 * 用户原话（v2）：「批量同步商品信息 · 按 ID 同步 20 个 —— 弹窗勾选商品后，将勾选的商品
 * 使用**列表**在弹窗中展示，使用 **tab** 的方式分为**按条件同步**和**按 ID 同步商品信息**，
 * 点击同步等待同步商品结果，如果按条件同步则**直接关闭弹窗触发定时任务后台执行**」。
 *
 * 🔄 **v4 追加口径（2026-10-11，用户原话逐字）**：
 *
 * > 优化执行 id 同步商品，点击同步按钮之后加入队列**不再轮询查询结果**，提示
 * > **XX 条已经加入队列中，执行进度请到详情批次 XXXX 查看**，然后**关闭弹窗**
 *
 * ⚠️ 这条**取代**了同日 v3 的「前台等结果（整块蒙版 + 已耗时秒表 + 执行中不可关）」：
 * 那时两条 Tab 是「条件 = 触发即关 / 按 ID = 前台等」；现在**两条都是触发即关** ——
 * 真实同步由队列 worker 异步跑，进度统一去「队列记录」按批次号看。
 *
 * | Tab | 执行模型 | 关闭时机 |
 * |---|---|---|
 * | **按条件同步** | 触发**后台任务**（整店，按全量 / 时间区间） | **点同步即关弹窗** |
 * | **按 ID 同步商品信息** | **入队**（每件一个队列项，挂一个批次号） | **点同步即关弹窗** |
 *
 * ⇒ 弹窗里**没有**任何「等结果」的 UI：没有轮询、没有蒙版、没有秒表。用户提交后立刻
 * 回到列表干活，结果改去「队列记录」看（那里本来就有批次树 + 逐件 payload）。
 * 旧实现（`pollGoodsSyncBatch` / `beginWait` / `endWait` / `waitElapsedSec`）**整段删除**，
 * 不留影子 —— 留着一个没人调的轮询只会让下一个人以为还有前台等待这条路径。
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
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { SyncOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import { useCallback, useEffect, useRef, useState } from "react";

import { fetchXhsShopItems, readErrorDetail, syncSpecifiedShopItems } from "../../../lib/api";
import type { SyncOptions } from "../../../components/platforms/sync-options-modal";
import { MAX_OPERATE_ITEMS } from "../../../lib/shop-items-view";
import type { XhsShopItem } from "../../../types";

const { Text } = Typography;

/** 无限列表每页条数（与台账列表同一档，滚动一屏大约触发两次取数）。 */
const PAGE_SIZE = 50;

/** 一行「要同步的商品 + 它的队列状态」。`todo` = 还没提交过（打开时铺的清单）。 */
type SyncRow = {
  item_id: string;
  /** 商品名（主表勾选 / 从台账补充时已知；**手输 ID** 时为空 ⇒ 只显示 ID）。 */
  title: string;
  /** 缩略图地址（同上：手输 ID 时为空 ⇒ 显示占位）。 */
  image: string;
  status: "todo" | "queued" | "already";
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

/**
 * 入队回执 → 给用户看的**一句话**（用户口径逐字「XX 条已经加入队列中，
 * 执行进度请到详情批次 XXXX 查看」）。
 *
 * ⚠️ 计数只用**后端回执的两个数**（`enqueued` / `already`），不在前端自己数：
 * 后端会去重（重复的 ID、已在队列的件）—— 前端按提交清单数会在「有重复 /
 * 已在队列」时说多，而用户会拿着这个数去批次里核对。
 */
export function enqueueNotice(result: {
  enqueued?: number;
  already?: number;
  batch_no?: string;
  note?: string;
}): string {
  const enqueued = Number(result.enqueued ?? 0) || 0;
  const already = Number(result.already ?? 0) || 0;
  const batchNo = String(result.batch_no || "");
  if (!batchNo) {
    //: 一次都没入队成功（全是非法 ID / 全在队列）⇒ 如实报后端那句话，不编批次号。
    return String(result.note || "没有可执行的商品同步项");
  }
  const head = `${enqueued} 条已经加入队列中`;
  const tail = already > 0 ? `（另有 ${already} 条已在队列中）` : "";
  return `${head}${tail}，执行进度请到「队列记录」的详情批次 ${batchNo} 查看`;
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

  const loadPage = useCallback(
    async (accountId: number, page: number, seq: number) => {
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
    },
    [keyword],
  );

  /**
   * 真正发请求：把一份 ID 清单交给后端**入队**，拿到批次号后**提示 + 关弹窗**
   * （2026-10-11 v4 用户口径）。
   *
   * ⚠️ **这里不轮询**（v3 的 `pollGoodsSyncBatch` 整段删除）：逐件结果由队列 worker 写进
   * 批次树，用户去「队列记录」按批次号看 —— 前端再抄一份进度就是第二个真相源。
   */
  const runSync = useCallback(
    async (ids: string[]) => {
      if (!platformAccountId) return;
      setRunning(true);
      try {
        //: 2026-10-11 改**走同步商品信息队列**：`items/sync` 只入队（返回
        //: `{enqueued, already, batch_no, note}`），真实同步由 worker 异步执行。
        const result = await syncSpecifiedShopItems(platformAccountId, ids);
        const notice = enqueueNotice(result);
        //: 行上的状态只为「再打开弹窗时看得出上次提交到哪了」；提交瞬间就关窗，
        //: 不追求把逐件终态画出来（那正是本次去掉的轮询）。
        //: ⚠️ 后端只回**计数**（不去重逐条回执）⇒ 全清单标「已入队」，已入队的件
        //: 由汇总文案说明（前端不去猜哪几件、那是第二个真相源）。
        const submitted = new Set(ids);
        setRows((current) =>
          current.map((row) =>
            submitted.has(row.item_id)
              ? { ...row, status: "queued" as const, note: "已加入同步队列，进度见队列记录" }
              : row,
          ),
        );
        if (Number(result.enqueued ?? 0) > 0 || String(result.batch_no || "")) {
          message.success(notice);
        } else {
          message.warning(notice);
        }
        onSynced();
        //: 用户口径收尾：「然后关闭弹窗」。
        onClose();
      } catch (error) {
        //: 用 lib 的**唯一**实现（与账号卡片页同一句话，DEF-044）。
        const text = readErrorDetail(error, "同步失败");
        setRows((current) =>
          current.map((row) =>
            row.status === "todo" ? { ...row, note: `入队失败：${text}` } : row,
          ),
        );
        message.error(text);
      } finally {
        setRunning(false);
      }
    },
    [platformAccountId, message, onSynced, onClose],
  );

  //: 打开 ⇒ 把勾选集**铺成待同步列表**（用户口径「将勾选的商品使用列表在弹窗中展示」）。
  //: 有勾选 ⇒ 落在「按 ID」Tab（点这个按钮就是来同步这些的）；没勾选 ⇒ 落在「按条件」。
  //: ⚠️ 依赖里**没有** `items`：它是打开那一刻页面给的值，由 `open` 这一轮读取
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

  /** 「按 ID 同步」：**入队后即关弹窗**（2026-10-11 v4 口径）。 */
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
    status === "queued" ? (
      <Tag color="blue">已入队</Tag>
    ) : status === "already" ? (
      <Tag color="orange">已在队列</Tag>
    ) : (
      <Tag>待同步</Tag>
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
      title: "队列状态",
      dataIndex: "status",
      width: 110,
      render: (value: SyncRow["status"]) => statusTag(value),
    },
    { title: "说明", dataIndex: "note", render: (value: string) => value || "-" },
  ];

  const rangeInvalid = scope === "range" && !startDate;

  return (
    <Modal
      centered
      open={open}
      title="批量同步商品信息"
      //: 2026-10-06 用户口径「弹窗全屏的 80%」。
      width="80vw"
      onCancel={onClose}
      //: ⚠️ 2026-10-11 v4：两条 Tab 都是**触发即关**（不再是「按 ID 在前台等结果」）
      //: ⇒ 不再需要「执行中不许关」那套锁（原 `maskClosable={false}` + `closable={!idRunning}`
      //: 随 v3 的「前台等结果」一起退场）。留着它反而会让用户以为「关了会丢结果」。
      styles={{
        body: { maxHeight: "64vh", overflow: "auto" },
      }}
      className="sync-items-modal"
      footer={
        //: ⚠️ 两个 Tab 的按钮是**两种执行模型**，但收尾一致：都是「触发即关」。
        //: 条件是后台任务（页面轮询状态），按 ID 是入队（进度去队列记录按批次号看）。
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
              取消
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
                <Alert
                  type="info"
                  showIcon
                  message="按 ID 同步（加入队列）"
                  description="点「同步」后这批商品会加入同步队列，弹窗立即关闭；逐件执行进度去「队列记录」按批次号查看。"
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  下面是要同步的商品（主表勾选的 + 你补充的）。
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
                {/* 批量移除（勾选后一次剔掉；提交中不给动 —— 清单要与提交的一致）。 */}
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
      {/* 滚动条：**已并入全局口径**（2026-10-11 用户口径「前端所有滚动条全部统一隐藏，
          只保留功能」）⇒ 这里**不再**写局部滚动条样式。

          ⚠️ 必须删掉旧的局部规则而不是留着：它写的是更具体的
          `.sync-items-modal .ant-table-body { scrollbar-width: thin }` +
          `::-webkit-scrollbar { width: 6px }`，**优先级高于** `global.css` 里的
          `* { scrollbar-width: none }` ⇒ 留着就这一处会继续露出细滚动条，
          「全部统一隐藏」当场破功。
          规则保留在文档里（原口径）：只藏**外观**，滚轮 / 拖拽照常 ——
          不能用 `overflow: hidden`（那会让内容滚不动）。 */}
    </Modal>
  );
}
