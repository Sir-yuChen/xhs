import {
  CopyOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  FileAddOutlined,
  ReloadOutlined,
  SearchOutlined,
  ShoppingOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Divider,
  Empty,
  Form,
  Image,
  Input,
  InputNumber,
  message,
  Modal,
  Row,
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
import type { Dayjs } from "dayjs";
import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  batchGenerateProductNotes,
  bindXhsQianfanShop,
  deleteXhsShopItems,
  fetchAccounts,
  fetchXhsItemNotes,
  fetchXhsQianfanBinding,
  fetchXhsShopItemDetail,
  fetchXhsShopItems,
  generateProductNote,
} from "../../../lib/api";
import { yuan } from "../../../lib/format";
import { NoteDetailBody } from "../../../components/platforms/note-detail-modal";
import {
  accountLabel,
  batchDeleteBlock,
  batchDeleteHint,
  DEFAULT_NOTE_COUNT_OP,
  DEFAULT_SORT,
  ledgerPrerequisite,
  MAX_BATCH_DELETE,
  NOTE_COUNT_OP_OPTIONS,
  noteCountFilter,
  normalizeDeleteIds,
  QIANFAN_PREREQUISITE_HINT,
  resolveSort,
  SORT_FIELD_BY_COLUMN,
  selectableAccounts,
  skuCell,
  sortSummary,
  statusFilterHint,
  type SortState,
} from "../../../lib/shop-items-view";
import type {
  PlatformAccount,
  XhsItemNote,
  XhsItemNotesResponse,
  XhsLoginChannels,
  XhsNoteCountOp,
  XhsQianfanBinding,
  XhsShopItem,
  XhsShopItemDetail,
  XhsShopItemStatus,
  XhsShopItemsQuery,
  XhsShopItemsResponse,
} from "../../../types";

const { Text, Paragraph } = Typography;
const { RangePicker } = DatePicker;

/**
 * 「商品列表」页：千帆店铺商品的本地台账视图。
 *
 * 设计真相源 `docs/02_设计文档/12_功能设计_我的笔记与互动数据.md` §25 +
 * `docs/02_设计文档/13_功能设计_账号矩阵同步.md` §30（v1.32 本地化改造）。
 *
 * 四条口径（都是实测定的，别改）：
 * 1. **走本地数据**（§30.3）：列表查 `shop_products` 台账（「同步商品」任务落库），
 *    **零平台请求**；展示契约与实时查询时代逐字段一致；时效由「同步于 X」明示；
 * 2. **账号不过滤类型**（§30.1）：门禁在后端（未绑店 → 400），前端不预判；
 * 3. **筛选参数名照抄千帆页面自己发的请求**（`whole_keywords` / `card_type` /
 *    `price_from|price_to`（分）/ `stock_from|stock_to` / `sales_from|sales_to`）；
 * 4. **金额进出都按分**（后端契约），展示层除以 100 —— 只有一处换算，不会两头乘。
 */

/** 状态选项与后端 `QIANFAN_ITEM_STATUSES` 一一对应（顺序照千帆页面的 tab）。 */
const STATUS_OPTIONS: { value: XhsShopItemStatus; label: string }[] = [
  { value: "on_shelf", label: "售卖中" },
  { value: "all", label: "全部" },
  { value: "off_shelf", label: "已下架" },
  { value: "frozen", label: "已冻结" },
  { value: "rectify", label: "整改" },
  { value: "auditing", label: "审核中" },
  { value: "audit_rejected", label: "审核驳回" },
  { value: "audit_passed", label: "审核通过" },
];

/** 分 → 元（展示口径）。`null` 保持「取不到」，不显示成 0。 */
function priceText(item: XhsShopItem): string {
  const lo = yuan(item.price_min_fen);
  const hi = yuan(item.price_max_fen);
  if (lo === "-" && hi === "-") return "-";
  if (lo === hi) return `¥${lo}`;
  return `¥${lo} ~ ¥${hi}`;
}

/**
 * 商品缩略图边长（v1.35）。
 *
 * 用户 2026-09-23 上机口径：「商品图**放大 15px**」——原 56 → **71**。
 * 只有一处尺寸常量：占位块（无图时）与真图共用，改一个数两处一起动，
 * 不会出现「有图 71 / 没图 56」的错位。
 */
const ITEM_THUMB_SIZE = 71;

/**
 * 「N 图」角标的样式（v1.35）：**压在缩略图左上角**。
 *
 * 原先是跟在标题后的行内 Tag —— 标题单行化后它会把标题挤掉一截，且
 * 「图片有几张」本该贴着图片说。`pointerEvents: "none"` 是为了让点击
 * 穿透到图片上（角标不该吃掉「点开大图」的手感）；圆角只留左下 + 右上，
 * 与左上角贴合图片圆角（4px）。
 */
const IMAGE_COUNT_BADGE_STYLE: CSSProperties = {
  position: "absolute",
  top: 0,
  left: 0,
  margin: 0,
  padding: "0 4px",
  fontSize: 11,
  lineHeight: "16px",
  borderTopLeftRadius: 4,
  borderTopRightRadius: 0,
  borderBottomRightRadius: 6,
  borderBottomLeftRadius: 0,
  pointerEvents: "none",
};

async function copyToClipboard(value: string, okText: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      const holder = document.createElement("textarea");
      holder.value = value;
      holder.style.position = "fixed";
      holder.style.opacity = "0";
      document.body.appendChild(holder);
      holder.select();
      document.execCommand("copy");
      document.body.removeChild(holder);
    }
    message.success(okText);
  } catch {
    message.error("复制失败，请手动选中后复制");
  }
}

type QueryForm = {
  keyword?: string;
  status: XhsShopItemStatus;
  price_from?: number;
  price_to?: number;
  stock_from?: number;
  stock_to?: number;
  sales_from?: number;
  sales_to?: number;
  /** 创建时间区间（千帆的「创建时间」筛选）。 */
  create_time_range?: [Dayjs | null, Dayjs | null] | null;
  /** 挂链笔记数：比较符（默认「小于」）+ 指定篇数（留空 = 不筛）。v1.34 */
  note_count_op?: XhsNoteCountOp;
  note_count?: number;
};


/** 挂链笔记弹窗右栏信息区（同步链路笔记带互动指标；本地生成笔记带发布态）。 */
function renderItemNoteInfo(note: XhsItemNote) {
  const publishLabel: Record<string, { text: string; color: string }> = {
    pending: { text: "待发布", color: "orange" },
    publishing: { text: "发布中", color: "blue" },
    published: { text: "已发布", color: "green" },
    failed: { text: "发布失败", color: "red" },
  };
  const status = publishLabel[note.publish_status];
  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <div>
        <Text strong style={{ fontSize: 15 }}>
          {note.title || "（无标题）"}
        </Text>
      </div>
      <Space size={6} wrap>
        {note.note_type === "local_generated" ? (
          <Tag color="purple">本地生成</Tag>
        ) : note.note_type === "synced" ? (
          <Tag color="blue">三方同步</Tag>
        ) : (
          <Tag>本地</Tag>
        )}
        {status && <Tag color={status.color}>{status.text}</Tag>}
      </Space>
      {note.publish_status === "failed" && note.publish_error && (
        <Alert
          type="error"
          showIcon
          message="发布失败原因"
          description={note.publish_error}
        />
      )}
      <Descriptions size="small" column={1} style={{ marginTop: 4 }}>
        <Descriptions.Item label="笔记 ID">{note.note_id}</Descriptions.Item>
        <Descriptions.Item label="发布时间">{note.publish_time || "-"}</Descriptions.Item>
        {(note.browse_num != null || note.like_num != null) && (
          <Descriptions.Item label="互动">
            <Space size={10} wrap>
              <Text style={{ fontSize: 12 }}>阅读 {note.browse_num ?? "-"}</Text>
              <Text style={{ fontSize: 12 }}>赞 {note.like_num ?? "-"}</Text>
              <Text style={{ fontSize: 12 }}>藏 {note.collect_num ?? "-"}</Text>
              <Text style={{ fontSize: 12 }}>评 {note.comment_num ?? "-"}</Text>
              <Text style={{ fontSize: 12 }}>分享 {note.share_num ?? "-"}</Text>
            </Space>
          </Descriptions.Item>
        )}
        <Descriptions.Item label="笔记链接">
          <Tooltip title="点击复制">
            <Text
              style={{ fontSize: 12, cursor: "pointer", color: "#1677ff" }}
              ellipsis
              onClick={() => void copyToClipboard(note.note_url, "笔记链接已复制")}
            >
              {note.note_url}
            </Text>
          </Tooltip>
        </Descriptions.Item>
      </Descriptions>
      {note.content && (
        <div>
          <Divider style={{ margin: "8px 0" }} />
          <Paragraph style={{ fontSize: 13, whiteSpace: "pre-wrap", marginBottom: 0 }}>
            {note.content}
          </Paragraph>
        </div>
      )}
    </Space>
  );
}

export function XhsShopItemsPage() {
  const navigate = useNavigate();
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [accountsLoaded, setAccountsLoaded] = useState(false);
  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null);
  const [binding, setBinding] = useState<XhsQianfanBinding | null>(null);
  const [bindingLoaded, setBindingLoaded] = useState(false);
  const [data, setData] = useState<XhsShopItemsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorText, setErrorText] = useState("");
  const [pageNo, setPageNo] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  // v1.32：排序挪到表头（上下箭头），不再在搜索区单独放字段/方向下拉。
  // 状态即「当前生效的排序」—— 服务端分页，排序必须回服务端做（客户端
  // 只有当前页，本地排只会重排一页，是假排序）。
  //
  // v1.36（用户 2026-09-23 口径「默认不排序，只有点击触发后进行排序，参考 antd
  // 组件中的排序」）：初值是 **null = 无排序** —— 进来表头没有箭头、请求里也不带
  // `sort_field`（后端用它的稳定默认序）。点表头才开始有排序态（升 → 降 → 取消）。
  const [sort, setSort] = useState<SortState | null>(DEFAULT_SORT);
  const [form] = Form.useForm<QueryForm>();
  const [bindOpen, setBindOpen] = useState(false);
  const [bindCookie, setBindCookie] = useState("");
  const [binding_, setBinding_] = useState(false);
  // 商品 → 挂链笔记反查（关系表源，设计 13 §31.6；弹窗 + tabs）
  const [notesOpen, setNotesOpen] = useState(false);
  const [notesLoading, setNotesLoading] = useState(false);
  const [notesData, setNotesData] = useState<XhsItemNotesResponse | null>(null);
  const [notesItem, setNotesItem] = useState<XhsShopItem | null>(null);
  /** 挂链笔记弹窗当前 tab（一篇笔记一个 tab；单篇时隐藏 tab 栏）。 */
  const [notesTab, setNotesTab] = useState("0");
  // 商品笔记智能生成（设计 13 §31）
  const [selectedItemIds, setSelectedItemIds] = useState<string[]>([]);
  const [generatingItemId, setGeneratingItemId] = useState<string | null>(null);
  const [batchGenerating, setBatchGenerating] = useState(false);
  /** 批量删除进行中（v1.36）：只用来给按钮上 loading，判据本身在 lib（`batchDeleteBlock`）。 */
  const [batchDeleting, setBatchDeleting] = useState(false);
  // 商品详情弹窗（v1.32：点标题打开；数据来自 /item-detail，纯本地）
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<XhsShopItemDetail | null>(null);
  const [detailError, setDetailError] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const list = await fetchAccounts();
        // v1.32（设计 13 §30.1）：**不再按 sub_type 过滤** —— 千帆门禁由后端把着
        // （未绑店 → 400 QIANFAN_BINDING_REQUIRED），前端按类型预判「哪类账号有
        // 店铺」就是第二份真相源。用户口径：下拉展示所有已绑定的小红书账号。
        setAccounts(selectableAccounts(list));
        const first = selectableAccounts(list)[0];
        if (first && selectedAccountId === null) {
          setSelectedAccountId(first.id);
        }
      } catch (error) {
        message.error(error instanceof Error ? error.message : "账号列表加载失败");
      } finally {
        setAccountsLoaded(true);
      }
    })();
    // 仅首屏加载账号列表
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ⚠️ 2026-09-24：店铺列表状态整块移除。它过去的唯一读者是账号下拉的店铺标注，
  // 而标签现在「只展示账号名称」（用户口径）⇒ 再拉一次 `/qianfan/shops` 就是一次
  // 纯浪费的请求（还是每次进页面都发）。绑定态另有 `fetchXhsQianfanBinding` 回答。

  const loadBinding = useCallback(async (accountId: number) => {
    try {
      setBinding(await fetchXhsQianfanBinding(accountId));
    } catch {
      setBinding(null);
    } finally {
      setBindingLoaded(true);
    }
  }, []);

  const load = useCallback(
    async (accountId: number, page = pageNo, size = pageSize, sortArg = sort) => {
      const values = form.getFieldsValue();
      const range = values.create_time_range;
      const query: XhsShopItemsQuery = {
        platform_account_id: accountId,
        page_no: page,
        page_size: size,
        keyword: values.keyword?.trim() || undefined,
        status: values.status || "on_shelf",
        price_from: values.price_from ?? undefined,
        price_to: values.price_to ?? undefined,
        stock_from: values.stock_from ?? undefined,
        stock_to: values.stock_to ?? undefined,
        sales_from: values.sales_from ?? undefined,
        sales_to: values.sales_to ?? undefined,
        // 日期按天传给后端：结束日含当天（后端统一补 23:59:59.999 并转毫秒）
        create_time_from: range?.[0] ? dayjs(range[0]).format("YYYY-MM-DD") : undefined,
        create_time_to: range?.[1] ? dayjs(range[1]).format("YYYY-MM-DD") : undefined,
        // 挂链笔记数（v1.34）：留空 = **不带这个参数**（= 不筛），不是「小于 0」
        ...noteCountFilter(values.note_count_op, values.note_count),
        // v1.32：排序由表头箭头驱动（状态在 sort），回服务端排。
        // v1.36：**无排序态不带这两个参数** —— 让后端用它自己的稳定默认序
        // （`create_time` 倒序）；antd 三级循环的「取消排序」也落回这里。
        // 前端自己编一个默认字段（或送空串）都等于替后端做主 = 第二份真相源。
        ...(sortArg
          ? { sort_field: sortArg.field, order: sortArg.order === "ascend" ? "asc" : "desc" }
          : {}),
      };
      setLoading(true);
      setErrorText("");
      try {
        setData(await fetchXhsShopItems(query));
      } catch (error) {
        setData(null);
        const text = error instanceof Error ? error.message : "商品列表加载失败";
        setErrorText(text);
        message.error(text);
      } finally {
        setLoading(false);
      }
    },
    [form, pageNo, pageSize, sort]
  );

  /**
   * 搜索 / 重置的统一入口（v1.36，顺带**清掉勾选**）。
   *
   * 为什么要清：跨页保留勾选只该在**同一次查询内**成立。换了条件就是一个新集合，
   * 留着上一批勾选会让「我删的是不是我看到的」变成一个用户猜不出来的问题 ——
   * 按钮上那个「（N）」还在，人却看不见那 N 行。
   *
   * v1.36：**「重置」把排序也退回去**（用户口径「默认不排序」）—— 重置 = 回初始态，
   * 而初始态就是「没有排序」；「搜索」则保留用户当前点的排序（他刚点了列，不该被
   * 一次条件搜索悄悄清掉）。⚠️ `load` 的 `sortArg` 默认值取自 state，`setSort` 这一
   * 拍还没生效 ⇒ 必须显式把目标值传进去，否则会按**旧**排序重拉。
   */
  function runSearch(resetForm: boolean) {
    if (resetForm) form.resetFields();
    setSelectedItemIds([]);
    setPageNo(1);
    const nextSort = resetForm ? DEFAULT_SORT : sort;
    if (resetForm) setSort(nextSort);
    if (selectedAccountId) void load(selectedAccountId, 1, pageSize, nextSort);
  }

  /**
   * 账号 → 三个平台的登录态（**零平台请求**：随账号列表一起下发，设计 13 §18.5）。
   * 前置门禁只看其中的 `ark`（千帆）—— 判据复用 `ledgerPrerequisite`（唯一实现）。
   */
  const channelsByAccount = useMemo(() => {
    const map: Record<number, XhsLoginChannels> = {};
    for (const account of accounts) map[account.id] = account.login_channels || {};
    return map;
  }, [accounts]);

  /**
   * 商品台账的**前置校验**（用户 2026-09-23 口径：「将校验当前账号下是否存在
   * 可用的千帆账号作为前置校验」）。不通过 ⇒ 只渲染一句提示，**一个请求都不发**。
   */
  const prereq = useMemo(
    () =>
      ledgerPrerequisite(
        binding?.bound,
        selectedAccountId === null ? undefined : channelsByAccount[selectedAccountId]
      ),
    [binding?.bound, selectedAccountId, channelsByAccount]
  );

  useEffect(() => {
    if (selectedAccountId === null) return;
    // 切账号 = 换一个店铺：旧账号的绑定态**必须先清掉**。否则 `bindingLoaded` /
    // `binding.bound` 还是上一个账号的，下面那个「绑定后拉一页」的 effect 会抢在
    // `loadBinding` 回来之前用**新账号**调 `/items` —— 未绑店即 400，于是
    // toast + 页面错误块 + 未绑定说明**一起冒出来**（用户看到的「乱七八糟的异常」）。
    setBinding(null);
    setBindingLoaded(false);
    setData(null);
    setErrorText("");
    // 勾选也是「上一个账号的」：删除请求按 item_id 打到**新账号的店**只会删 0 行，
    // 但按钮上还挂着「批量删除（30）」= 界面在说一件不会发生的事。一起清掉。
    setSelectedItemIds([]);
    void loadBinding(selectedAccountId);
  }, [selectedAccountId, loadBinding]);

  useEffect(() => {
    if (selectedAccountId === null || !bindingLoaded) return;
    if (!prereq.ok) return;
    void load(selectedAccountId, pageNo, pageSize);
    // 绑定状态变化后重新拉一页
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountId, bindingLoaded, prereq.ok]);

  const accountOptions = useMemo(
    () =>
      accounts.map((account) => ({
        value: account.id,
        // 只展示账号名称（2026-09-24 用户口径）：类型后缀（v1.32 已去）与店铺标注
        // （本次去掉）都不再进 label。账号 → 店铺是 1:1，店名由列表主体展示。
        label: accountLabel(account.nickname),
      })),
    [accounts]
  );

  async function handleBind() {
    if (!selectedAccountId || bindCookie.trim().length < 8) {
      message.warning("请粘贴千帆 Cookie（须含 access-token-ark.xiaohongshu.com）");
      return;
    }
    setBinding_(true);
    try {
      const bound = await bindXhsQianfanShop(selectedAccountId, bindCookie.trim());
      setBinding(bound);
      setBindOpen(false);
      setBindCookie("");
      message.success(`已绑定千帆店铺：${bound.shop_name || "（未命名）"}`);
      setPageNo(1);
      await load(selectedAccountId, 1, pageSize);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "绑定失败");
    } finally {
      setBinding_(false);
    }
  }

  /** 打开「这个商品挂了哪些笔记」弹窗（关系表源，零平台请求；设计 13 §31.6）。
   *
   * 多篇笔记用 **tab 自然隔离**（一篇一个 tab），内容区复用内容库详情弹窗的
   * 同一份两栏版式（`NoteDetailBody`）—— 同一语义不留两份实现。
   */
  async function openItemNotes(item: XhsShopItem) {
    if (!selectedAccountId) return;
    setNotesItem(item);
    setNotesOpen(true);
    setNotesLoading(true);
    setNotesData(null);
    setNotesTab("0");
    try {
      setNotesData(await fetchXhsItemNotes(selectedAccountId, item.item_id));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "挂链笔记反查失败");
    } finally {
      setNotesLoading(false);
    }
  }

  /**
   * 单商品「生成笔记」（设计 13 §31.4）：调千帆后台智能生成，落库为待发布的
   * 本地生成笔记（内容库可见，之后在内容库发布）。
   */
  async function generateNote(item: XhsShopItem) {
    if (!selectedAccountId) return;
    setGeneratingItemId(item.item_id);
    try {
      const result = await generateProductNote(selectedAccountId, item.item_id);
      // 媒体口径如实报（设计 13 §32.2.1：视频优先，图 ≤6 / 视频 ≤1）；并说清
      // 「生成 ≠ 发布」—— 只有内容库点「发布」才走发布链路（§32.2.2）。
      const mediaText =
        result.video_count > 0 ? `${result.video_count} 个视频` : `${result.image_count} 张图片`;
      message.success(
        `已生成待发布笔记《${result.title || "未命名"}》（${mediaText}）；到内容库点「发布」才会真正发布`
      );
      // 刷新挂链笔记数（生成后该商品的映射多了本地生成那篇）
      if (notesItem && notesItem.item_id === item.item_id && notesOpen) {
        void openItemNotes(item);
      }
      if (selectedAccountId) void load(selectedAccountId, pageNo, pageSize);
    } catch (error) {
      const detail = (error as { response?: { data?: { detail?: { message?: string } } } })?.response?.data
        ?.detail;
      message.error(
        (detail && typeof detail === "object" && detail.message) ||
          (error instanceof Error ? error.message : "生成笔记失败")
      );
    } finally {
      setGeneratingItemId(null);
    }
  }

  /** 批量「生成笔记」（设计 13 §31.4 步 6）：选中行顺序生成，单项失败不阻断。 */
  async function batchGenerate() {
    if (!selectedAccountId || selectedItemIds.length === 0) return;
    setBatchGenerating(true);
    try {
      const result = await batchGenerateProductNotes(selectedAccountId, selectedItemIds);
      if (result.failed === 0) {
        message.success(
          `已生成 ${result.succeeded} 篇待发布笔记（到内容库点「发布」才会真正发布）`
        );
      } else {
        const firstError = result.results.find((r) => !r.ok)?.error || "";
        message.warning(
          `生成完成：成功 ${result.succeeded} 篇，失败 ${result.failed} 篇${firstError ? `（首例：${firstError}）` : ""}`
        );
      }
      setSelectedItemIds([]);
      if (selectedAccountId) void load(selectedAccountId, pageNo, pageSize);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "批量生成失败");
    } finally {
      setBatchGenerating(false);
    }
  }

  /**
   * 批量删除本地台账行（v1.36，用户口径「商品列表新增批量删除操作」）。
   *
   * 口径与两个连带后果**只在 lib 里**（`batchDeleteBlock` / `batchDeleteHint`），
   * 这里只负责编排：拒绝 → 说明，允许 → 确认框 → 调接口 → 清勾选 → 重拉本页。
   *
   * 三个「不许静默」的点：
   * 1. 拒绝原因分两种（没勾 / 超上限）给**不同**文案 —— 压成一句「无法删除」等于
   *    让用户自己猜；
   * 2. `deleted < requested` 时如实说（传进来不存在的 item_id 会少删），
   *    不把「请求了几条」当「删了几条」；
   * 3. 本页被整页删空且不在第一页 ⇒ **回退一页**再拉（否则停在一张空表上，
   *    用户以为整个列表没了）。
   */
  async function batchDelete() {
    if (!selectedAccountId) return;
    const ids = normalizeDeleteIds(selectedItemIds);
    setBatchDeleting(true);
    try {
      const result = await deleteXhsShopItems(selectedAccountId, ids);
      setSelectedItemIds([]);
      if (result.deleted === result.requested) {
        message.success(`已删除本地台账 ${result.deleted} 行`);
      } else {
        message.warning(
          `已删除 ${result.deleted} 行（提交 ${result.requested} 个，其余在本地台账里不存在）`
        );
      }
      const removedOnPage = data
        ? data.items.filter((item) => ids.includes(item.item_id)).length
        : 0;
      const nextPage =
        data && removedOnPage >= data.items.length && pageNo > 1 ? pageNo - 1 : pageNo;
      setPageNo(nextPage);
      await load(selectedAccountId, nextPage, pageSize);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "批量删除失败");
    } finally {
      setBatchDeleting(false);
    }
  }

  /** 删除按钮的入口：先按 lib 判据分流（拒绝就给一句准话），再弹确认框。 */
  function handleBatchDelete() {
    const block = batchDeleteBlock(selectedItemIds);
    if (block === "empty") {
      message.info("先勾选要删除的商品");
      return;
    }
    if (block === "too_many") {
      message.warning(
        `单次最多删除 ${MAX_BATCH_DELETE} 个商品，本次选了 ${normalizeDeleteIds(selectedItemIds).length} 个，请分批删除`
      );
      return;
    }
    const count = normalizeDeleteIds(selectedItemIds).length;
    Modal.confirm({
      title: `删除本地台账的 ${count} 个商品？`,
      // 确认框是**唯一**能讲清两个连带后果的地方（见 `batchDeleteHint` docstring）：
      // 换行靠 `whiteSpace: pre-wrap`，别把 ①② 挤成一行。
      content: (
        <div style={{ whiteSpace: "pre-wrap" }}>{batchDeleteHint(count)}</div>
      ),
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => batchDelete(),
    });
  }

  /**
   * 打开商品详情弹窗（v1.32，设计 13 §30.3.6）。
   *
   * 数据来自 `/item-detail`（**纯本地**：基本信息 + 图集 + SKU 规格明细）。
   * SKU 没补过 ⇒ `skus=[]` + `detail_synced_at=""`，渲染空态 + 说明，
   * **不渲染成 0 行**（缺值口径）。
   */
  async function openDetail(item: XhsShopItem) {
    if (!selectedAccountId) return;
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    setDetailError("");
    try {
      setDetail(await fetchXhsShopItemDetail(selectedAccountId, item.item_id));
    } catch (error) {
      setDetail(null);
      setDetailError(error instanceof Error ? error.message : "商品详情加载失败");
    } finally {
      setDetailLoading(false);
    }
  }

  const statusHint = statusFilterHint(form.getFieldValue("status") || "on_shelf");

  /**
   * 某列当前是否处于排序激活态（antd 的 sortOrder：'ascend' | 'descend' | null）。
   *
   * 2026-09-23 起只有「上架时间」一列会调它（其余列不再挂 `sorter`）—— 保留成
   * 通用形态，是为了下次若真要放开某列，改的是 `SORT_FIELD_BY_COLUMN` 与列定义
   * 两处，而不是在这里补一段新判断。
   *
   * v1.36：`sort` 可为 `null`（无排序态）⇒ 此时所有列都返回 `null`，进来不带箭头。
   */
  const sortOrderFor = (dataIndex: string) =>
    sort && sort.field === SORT_FIELD_BY_COLUMN[dataIndex] ? sort.order : null;

  /**
   * 表头箭头点击 → 换排序并回第一页重拉（**服务端排序**）。
   *
   * ⚠️ antd 的 Table.onChange 在**分页/筛选变化时也会带当前 sorter 回调** ——
   * 用「新状态与旧状态相同则不动作」挡掉（否则翻到第 2 页会被打回第 1 页）。
   *
   * v1.36：新状态可能是 `null`（第三下「取消排序」）⇒ 比较必须按 `?? null` 归一，
   * 否则「取消」会被误判成「没变化」而**吞掉这一次回到默认序的请求**。
   */
  function handleSortChange(sorter: unknown) {
    const next = (Array.isArray(sorter) ? sorter[0] : sorter) as
      | { field?: string | number; order?: string | null }
      | undefined;
    const nextSort = resolveSort(
      next?.field != null ? String(next.field) : undefined,
      next?.order
    );
    const unchanged =
      (nextSort?.field ?? null) === (sort?.field ?? null) &&
      (nextSort?.order ?? null) === (sort?.order ?? null);
    if (unchanged) return;
    setSort(nextSort);
    setPageNo(1);
    if (selectedAccountId) void load(selectedAccountId, 1, pageSize, nextSort);
  }

  /**
   * 列定义。
   *
   * ⚠️ **只有「上架时间」一列带 `sorter`**（用户 2026-09-23 上机口径：「商品台账的
   * 只能根据上架时间列排序，其它列不支持排序」）。判据与 `SORT_FIELD_BY_COLUMN`
   * 同源：那里只有一个键、这里只有一列挂箭头，**两处必须一起改**（表头箭头是
   * 「后端真会按它排」的承诺，多一个箭头就多一份误导）。
   *
   * v1.36：`sorter: true` 是 antd 的**服务端排序**语义（`getSortFunction` 对
   * `true` 返回 `false` ⇒ antd 不本地排序，只回调 `onChange`），配合受控 `sortOrder`
   * 就得到原生三态：升 → 降 → 取消。**别给它传比较函数**，那会变成对当前页的假排序。
   */
  const columns: ColumnsType<XhsShopItem> = [
    {
      title: "商品",
      dataIndex: "title",
      // 缩略图 56 → 71（+15px）后内容宽度跟着长：71 + 10(gap) + 300(文本) = 381，
      // 小号表格单元格左右各 8px ⇒ 380 会挤到换行，故放宽到 400。
      width: 400,
      render: (_value, item) => (
        <Space align="start" size={10}>
          {/*
            缩略图容器（v1.35）：相对定位，专门用来承载「N 图」角标。
            `lineHeight: 0` 是必要的 —— antd `Image` 外层的 `.ant-image` 是
            行内元素，行高会在图片下方留一条基线缝隙，角标 `top: 0` 就会跟着偏。
          */}
          <div style={{ position: "relative", flex: "0 0 auto", lineHeight: 0 }}>
            {item.image ? (
              <Image
                src={item.image}
                width={ITEM_THUMB_SIZE}
                height={ITEM_THUMB_SIZE}
                style={{ objectFit: "cover", borderRadius: 4 }}
                preview={{ src: item.image }}
              />
            ) : (
              <div
                style={{
                  width: ITEM_THUMB_SIZE,
                  height: ITEM_THUMB_SIZE,
                  background: "#f5f5f5",
                  borderRadius: 4,
                }}
              />
            )}
            {/*
              「N 图」角标：**图片左上角**（v1.35，原先是标题后的行内 Tag）。
              口径不变：只有多图（>1）才提示，单图不占位置。
            */}
            {item.image_count > 1 && (
              <Tag style={IMAGE_COUNT_BADGE_STYLE}>{item.image_count} 图</Tag>
            )}
          </div>
          <div style={{ minWidth: 0, maxWidth: 300 }}>
            {/*
              标题行（v1.35）：**单行展示**。
              商品名普遍偏长，换行会把整行商品卡撑高、右边几列基线全错开；
              改用 `…` 收起 + Tooltip 悬停看全文（与下面分类行、挂链笔记 Tab
              同一口径，ADR-018）。原「N 图」Tag 已移到缩略图左上角，不再挤占标题宽度。

              ⚠️ 这里用「Tooltip + 手写 CSS」而不是 `Typography.Link` 的
              `ellipsis={{ tooltip }}`：antd 6.3.7 的 `LinkProps.ellipsis`
              类型只收 `boolean`（`Text` 才收配置对象），传对象会 TS2322。
            */}
            <Tooltip title={item.title || "（无标题）"}>
              <a
                onClick={() => void openDetail(item)}
                style={{
                  fontSize: 13,
                  display: "block",
                  maxWidth: 300,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {item.title || "（无标题）"}
              </a>
            </Tooltip>
            {/* 商品 ID 行（与货号分两行，v1.32） */}
            <div style={{ marginTop: 2 }}>
              <Space size={4}>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  ID {item.item_id}
                </Text>
                <Tooltip title="点击复制商品 ID">
                  <CopyOutlined
                    style={{ fontSize: 11, cursor: "pointer" }}
                    onClick={() => void copyToClipboard(item.item_id, "商品 ID 已复制")}
                  />
                </Tooltip>
              </Space>
            </div>
            {/* 货号行 */}
            <div style={{ marginTop: 2 }}>
              <Text type="secondary" style={{ fontSize: 11 }}>
                货号 {item.article_no || "-"}
              </Text>
            </div>
            {/*
              分类行（合并到货号下一行，v1.32）。

              2026-09-23 上机口径：**分类不自动换行**。千帆的分类是「/」串起来的
              多级路径（如「户外/登山/野营/旅行用品 / 露营/野炊装备 / 饮水用具/
              盛水容器 / 水盆」），换行后这一格会顶成两行甚至三行，把整行商品卡
              撑高、和右边几列的基线全错开。改用 `…` 收起 + Tooltip：**完整分类
              悬停可见、不丢信息**（ADR-018 的口径：长文案不换行，收起而不是截断）。
            */}
            <div style={{ marginTop: 2 }}>
              <Text
                type="secondary"
                ellipsis={{ tooltip: item.category || "未分类" }}
                style={{ fontSize: 11, display: "block", maxWidth: 300 }}
              >
                {item.category || "未分类"}
              </Text>
            </div>
          </div>
        </Space>
      ),
    },
    {
      title: "价格",
      dataIndex: "price_min_fen",
      width: 150,
      render: (_value, item) => <Text strong>{priceText(item)}</Text>,
    },
    {
      title: "库存 / SKU",
      dataIndex: "stock",
      width: 130,
      render: (_value, item) => (
        <div>
          <div>
            <Text>{item.stock ?? "-"}</Text>
          </div>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {item.on_sale_sku_count ?? "-"}/{item.sku_count ?? "-"} 在售
          </Text>
          {item.stock_warning_sku_count > 0 && (
            <div>
              <Tag color="orange" style={{ fontSize: 11 }}>
                {item.stock_warning_sku_count} 个规格库存告警
              </Tag>
            </div>
          )}
        </div>
      ),
    },
    {
      title: "销量",
      dataIndex: "sales_30d",
      width: 110,
      render: (_value, item) => (
        <div>
          <Text>近 30 天 {item.sales_30d ?? "-"}</Text>
          <div>
            <Text type="secondary" style={{ fontSize: 11 }}>
              累计 {item.sales_total ?? "-"}
            </Text>
          </div>
        </div>
      ),
    },
    {
      title: "挂链笔记",
      dataIndex: "note_count",
      width: 100,
      // 2026-09-23 上机口径：「查看这些笔记（关系表口径，零平台请求）。本地关系 N 篇；
      // 平台侧记 M 篇…」这条 Tooltip 文案**移除** —— 与弹窗那条说明同一批清掉，
      // 挂链列就只剩一个可点的篇数（`platform_note_count` 仍在响应里，留给需要对照的
      // 消费者，界面上不再解释两个口径为什么不等）。
      render: (_value, item) =>
        item.note_count > 0 ? (
          <Tag
            color="geekblue"
            style={{ cursor: "pointer" }}
            onClick={() => void openItemNotes(item)}
          >
            {item.note_count} 篇
          </Tag>
        ) : (
          <Text type="secondary">0</Text>
        ),
    },
    {
      // 上架时间与状态合并为一列两行（v1.32）；表头箭头按上架时间排。
      // 宽度按「秒级时间」留足（设计 13 §32.1：`YYYY-MM-DD HH:MM:SS` 共 19 字符，
      // 原来按分钟精度配的 140px 会折行）。
      title: "上架时间 / 状态",
      dataIndex: "on_shelf_time",
      width: 168,
      sorter: true,
      sortOrder: sortOrderFor("on_shelf_time"),
      render: (_value, item) => (
        <div>
          <div>
            <Text style={{ fontSize: 12 }}>{item.on_shelf_time || "-"}</Text>
          </div>
          <Space direction="vertical" size={2} style={{ marginTop: 2 }}>
            <Space size={4} wrap>
              {item.status_label ? (
                <Tag color={item.buyable ? "green" : "default"}>{item.status_label}</Tag>
              ) : (
                <Text type="secondary" style={{ fontSize: 11 }}>
                  状态 -
                </Text>
              )}
              {item.frozen && <Tag color="red">已冻结</Tag>}
              {item.stock_warning_sku_count > 0 && <Tag color="orange">库存风险</Tag>}
            </Space>
          </Space>
        </div>
      ),
    },
    {
      title: "操作",
      key: "action",
      width: 130,
      render: (_value, item) => (
        <Space size={4}>
          {/* 生成笔记（设计 13 §31.4，用户需求③）：调千帆后台智能生成，落库待发布 */}
          <Tooltip title="调用千帆后台智能生成一篇挂本商品的笔记（待发布，之后到内容库发布）">
            <Button
              type="link"
              size="small"
              icon={<FileAddOutlined />}
              loading={generatingItemId === item.item_id}
              disabled={generatingItemId !== null && generatingItemId !== item.item_id}
              onClick={() => void generateNote(item)}
            >
              生成笔记
            </Button>
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Card
        title={
          <Space>
            <ShoppingOutlined />
            商品列表（千帆）
          </Space>
        }
        extra={
          <Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              账号
            </Text>
            <Select
              style={{ minWidth: 260 }}
              placeholder={accountsLoaded ? "选择小红书账号" : "加载中…"}
              value={selectedAccountId ?? undefined}
              options={accountOptions}
              notFoundContent="没有小红书账号"
              onChange={(value) => {
                setSelectedAccountId(value);
                setPageNo(1);
              }}
            />
          </Space>
        }
      >
        {accountsLoaded && accounts.length === 0 ? (
          <Alert
            type="warning"
            showIcon
            message="没有小红书账号可选"
            description={
              <Space direction="vertical" size={6}>
                <Text style={{ fontSize: 13 }}>
                  商品与千帆数据属于店铺经营侧；v1.32 起下拉不再按账号类型过滤，
                  所有小红书账号都能选，没绑店铺的账号会在查询时提示先绑定。
                </Text>
                <Button
                  size="small"
                  href="/platforms/xhs/accounts"
                  onClick={(event) => {
                    event.preventDefault();
                    window.location.assign("/platforms/xhs/accounts");
                  }}
                >
                  去「账号矩阵」添加账号
                </Button>
              </Space>
            }
          />
        ) : !bindingLoaded ? (
          <Spin />
        ) : !prereq.ok ? (
          /*
            前置门禁不通过（用户 2026-09-23 上机口径：「只需提示，请在账号信息卡片中
            扫码登录千帆后再来查看，移除其它错误提示，将校验当前账号下是否存在可用的
            千帆账号作为前置校验」）。

            一个 `Alert` 说完全部：**不含**旧的三步取 Cookie 说明、**不含**「登录态已失效」
            独立告警 —— 而且因为下面那个拉列表的 effect 已经按 `prereq.ok` 拦住，
            这次切换**一个请求都不发**，不会再冒出列表 400 的 toast 与错误块。
          */
          <Alert
            type="info"
            showIcon
            message="当前账号没有可用的千帆登录态"
            description={
              <Space direction="vertical" size={8}>
                <Text style={{ fontSize: 13 }}>{QIANFAN_PREREQUISITE_HINT}</Text>
                <Space size={8} wrap>
                  <Button
                    type="primary"
                    href="/platforms/xhs/accounts"
                    onClick={(event) => {
                      event.preventDefault();
                      window.location.assign("/platforms/xhs/accounts");
                    }}
                  >
                    去「账号矩阵」扫码登录千帆
                  </Button>
                  {/* 店铺绑定（账号 ↔ 店铺 1:1）与「千帆令牌」是两件事：扫码只补令牌，
                      首次接入仍需在这里绑一次店。入口保留，但不占提示的主位。 */}
                  <Button onClick={() => setBindOpen(true)}>绑定千帆（粘贴 Cookie）</Button>
                </Space>
              </Space>
            }
          />
        ) : statusHint ? (
          /*
            审核态筛选的安静说明（§30.5，25–30 字一行）。
            v1.32 已把「店铺：xxx」「本地台账 N 件 · 同步于 X」「数据说明」三个文本
            移除（店铺与状态口径在底部 Descriptions，同步时效出口在账号矩阵页），
            这里只剩状态说明这一种内容。

            ⚠️ **必须裸 `Text`，不能再套 `<Space direction="vertical">`**（v1.35 修）：
            antd `Card` 只在 `childNodes.length > 0` 时才渲染 `.ant-card-body`
            （`antd/es/card/Card.js`：`loading || childNodes.length ? <div …/> : null`），
            而 `React.Children.toArray` 会把 `null` / `false` **过滤掉**。原先那个
            `Space` 即使一个子项都没有也是**真元素** ⇒ body 照渲、上下 24px 内边距照留，
            默认状态（`statusHint` 为空）就在「商品列表（千帆）」标题下留出一条空白。
            改成裸 `Text` + `null` 兜底后，没说明时 body **整块不渲染**，空白消失
            （用户 2026-09-23 上机口径：「商品列表（千帆）下的空白处移除」）。
          */
          <Text type="secondary" style={{ fontSize: 12 }}>
            {statusHint}
          </Text>
        ) : null}

        {errorText && (
          <Alert
            style={{ marginTop: 12 }}
            type="error"
            showIcon
            message="商品列表加载失败"
            description={errorText}
          />
        )}
      </Card>

      {/* 前置门禁不通过（`prereq.ok === false`）⇒ 搜索区与台账都不渲染：
          没有可查的对象，摆出来只会让人以为能查。
          注意这是**渲染层**的拦，真正的「零请求」由上面那个按 `prereq.ok`
          守住的 effect 保证 —— 少写任何一半都只是半道防线。 */}
      {prereq.ok && (
        <>
        {/* 搜索条件：与千帆商品列表页一一对应（关键词 / 状态 / 价格 / 库存 / 销量）。
            **排序不在这里** —— v1.32 起挪到表头（上下箭头，服务端排序）。

            2026-09-23 上机口径（「搜索条件区域重新布局，布局在两行内，搜索重置等按钮
            放条件的最后」）：9 个条件 + 按钮组**恰好两行排完**，按钮组落在第二行末尾。
            原来按钮分散在两行各占一列，视线要在两行之间来回找，且第二行被 4 列窄
            `InputNumber` 挤得半行空半行满。 */}
        <Card size="small" style={{ marginTop: 12 }} title="搜索条件">
        <Form<QueryForm>
          form={form}
          layout="vertical"
          initialValues={{ status: "on_shelf", note_count_op: DEFAULT_NOTE_COUNT_OP }}
        >
            {/* 第一行：关键词(6) + 状态(4) + 价格区间(4+4) + 创建时间(6) = 24 */}
            <Row gutter={12}>
              <Col span={6}>
                <Form.Item label="关键词" name="keyword" style={{ marginBottom: 12 }}>
                  <Input
                    allowClear
                    placeholder="商品名称 / 商品 ID / 商品货号，多条用逗号隔开"
                    onPressEnter={() => selectedAccountId && void load(selectedAccountId, 1, pageSize)}
                  />
                </Form.Item>
              </Col>
              <Col span={4}>
                <Form.Item label="状态" name="status" style={{ marginBottom: 12 }}>
                  <Select options={STATUS_OPTIONS} />
                </Form.Item>
              </Col>
              <Col span={4}>
                <Form.Item label="价格下限（元）" name="price_from" style={{ marginBottom: 12 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最低售价" />
                </Form.Item>
              </Col>
              <Col span={4}>
                <Form.Item label="价格上限（元）" name="price_to" style={{ marginBottom: 12 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最高售价" />
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item label="创建时间" name="create_time_range" style={{ marginBottom: 12 }}>
                  <RangePicker
                    style={{ width: "100%" }}
                    placeholder={["开始日期", "截止日期"]}
                    allowClear
                  />
                </Form.Item>
              </Col>
            </Row>
            {/* 第二行：库存区间(3+3) + 销量区间(3+3) + 挂链笔记数(6) + 按钮组(6，右对齐收尾) = 24 */}
            <Row gutter={12}>
              <Col span={3}>
                <Form.Item label="库存下限" name="stock_from" style={{ marginBottom: 8 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最小库存" />
                </Form.Item>
              </Col>
              <Col span={3}>
                <Form.Item label="库存上限" name="stock_to" style={{ marginBottom: 8 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最大库存" />
                </Form.Item>
              </Col>
              <Col span={3}>
                <Form.Item label="销量下限" name="sales_from" style={{ marginBottom: 8 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最低销量" />
                </Form.Item>
              </Col>
              <Col span={3}>
                <Form.Item label="销量上限" name="sales_to" style={{ marginBottom: 8 }}>
                  <InputNumber style={{ width: "100%" }} min={0} placeholder="最高销量" />
                </Form.Item>
              </Col>
              {/*
                挂链笔记数（v1.34，用户口径「添加挂链笔记数，默认小于指定篇数」）：
                比较符默认「小于」+ 指定篇数。值**留空 = 不筛**（不是「小于 0」）——
                `0` 是有意义的阈值（「一篇都没挂」），缺省态不该偷偷筛掉数据。
                口径 = `note_goods` 关系表，与本列显示的篇数是**同一个数**（§33.8）。
              */}
              <Col span={6}>
                <Form.Item label="挂链笔记数" style={{ marginBottom: 8 }}>
                  <Space.Compact style={{ width: "100%" }}>
                    <Form.Item name="note_count_op" noStyle>
                      <Select
                        style={{ width: 96, flexShrink: 0 }}
                        options={NOTE_COUNT_OP_OPTIONS}
                      />
                    </Form.Item>
                    <Form.Item name="note_count" noStyle>
                      <InputNumber
                        style={{ width: "100%" }}
                        min={0}
                        placeholder="篇数（留空不筛）"
                      />
                    </Form.Item>
                  </Space.Compact>
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item label=" " style={{ marginBottom: 8 }}>
                  <Space style={{ width: "100%", justifyContent: "flex-end" }} wrap>
                    <Button
                      type="primary"
                      icon={<SearchOutlined />}
                      loading={loading}
                      onClick={() => runSearch(false)}
                    >
                      搜索
                    </Button>
                    <Button onClick={() => runSearch(true)}>重置</Button>
                    <Tooltip title="重新拉取当前条件的第一页">
                      <Button
                        icon={<ReloadOutlined />}
                        onClick={() => {
                          setPageNo(1);
                          if (selectedAccountId) void load(selectedAccountId, 1, pageSize);
                        }}
                      />
                    </Tooltip>
                  </Space>
                </Form.Item>
              </Col>
            </Row>
          </Form>
        </Card>

        <Card
          size="small"
          style={{ marginTop: 12 }}
          bodyStyle={{ padding: 12 }}
          title="商品台账"
          extra={
            <Space size={8}>
              <Tooltip
                title={
                  selectedItemIds.length > 0
                    ? `对选中的 ${selectedItemIds.length} 个商品逐个智能生成笔记（≤20 个/次，单项失败不阻断）`
                    : "先在表格里勾选商品（最多 20 个/次）"
                }
              >
                <Button
                  type="primary"
                  icon={<FileAddOutlined />}
                  disabled={selectedItemIds.length === 0}
                  loading={batchGenerating}
                  onClick={() => void batchGenerate()}
                >
                  批量生成智能笔记{selectedItemIds.length > 0 ? `（${selectedItemIds.length}）` : ""}
                </Button>
              </Tooltip>
              {/* 批量删除（v1.36）：**只删本地台账行** —— 平台商品不动、关系行不动。
                  两个连带后果写在确认框正文里（`batchDeleteHint`），按钮上的 Tooltip
                  只说「删什么、上限多少」，不重复那两句（换成两处说同一件事）。 */}
              <Tooltip
                title={
                  selectedItemIds.length > 0
                    ? `从本地台账删除选中的 ${selectedItemIds.length} 个商品行（≤${MAX_BATCH_DELETE} 个/次，平台商品不动）`
                    : "先在表格里勾选商品"
                }
              >
                <Button
                  danger
                  icon={<DeleteOutlined />}
                  disabled={selectedItemIds.length === 0}
                  loading={batchDeleting}
                  onClick={handleBatchDelete}
                >
                  批量删除{selectedItemIds.length > 0 ? `（${selectedItemIds.length}）` : ""}
                </Button>
              </Tooltip>
            </Space>
          }
        >
          {data ? (
            <Table<XhsShopItem>
              rowKey="item_id"
              size="small"
              loading={loading}
              columns={columns}
              dataSource={data.items}
              scroll={{ x: 1300 }}
              sortDirections={["descend", "ascend"]}
              rowSelection={{
                selectedRowKeys: selectedItemIds,
                onChange: (keys) => setSelectedItemIds(keys as string[]),
                // 跨页勾选要留得住（v1.36）：批量删除/生成是**跨页**操作，翻页就丢勾选
                // 会让用户以为「我勾了 30 个」而实际只剩当前页那几个。数量始终在
                // 按钮上可见（`批量删除（N）`），所以留住所选不会变成隐形状态。
                preserveSelectedRowKeys: true,
              }}
              onChange={(_pagination, _filters, sorter) => handleSortChange(sorter)}
              locale={{ emptyText: <Empty description="当前条件下没有商品" /> }}
              pagination={{
                current: pageNo,
                pageSize,
                total: data.total,
                showSizeChanger: true,
                pageSizeOptions: ["20", "50", "100"],
                showTotal: (total) => `共 ${total} 件商品`,
                onChange: (page, size) => {
                  setPageNo(page);
                  setPageSize(size);
                  if (selectedAccountId) void load(selectedAccountId, page, size);
                },
              }}
            />
          ) : (
            <Empty description={binding?.bound ? "点「搜索」拉取商品列表" : "绑定千帆后可查询商品"} />
          )}
          {data && (
            <Descriptions size="small" column={4} style={{ marginTop: 8 }}>
              <Descriptions.Item label="商品来源">本地台账（同步商品任务落库）</Descriptions.Item>
              <Descriptions.Item label="店铺">{data.shop_name || "-"}</Descriptions.Item>
              <Descriptions.Item label="状态口径">{data.status_label}</Descriptions.Item>
              <Descriptions.Item label="命中总数">{data.total}</Descriptions.Item>
              {/* 当前排序（设计 13 §32.1）：取**后端回传的生效口径**，不是前端自己的猜想 */}
              <Descriptions.Item label="当前排序">
                {sortSummary(data.sort_field, data.order)}
              </Descriptions.Item>
              <Descriptions.Item label="台账同步时间">{data.queried_at || "-"}</Descriptions.Item>
              <Descriptions.Item label="平台商品总数">
                {data.products_total > 0 ? data.products_total : "-"}
              </Descriptions.Item>
            </Descriptions>
          )}
        </Card>
        </>
      )}

      {/* 商品详情弹窗（v1.32，设计 13 §30.3.6）：基本信息 + 图集 + 规格详情 */}
      <Modal
        open={detailOpen}
        title="商品详情"
        width="80%"
        footer={
          <Space>
            <Button onClick={() => setDetailOpen(false)}>关闭</Button>
            <Button
              type="primary"
              disabled={!detail}
              onClick={() => {
                if (!detail) return;
                setDetailOpen(false);
                void openItemNotes(detail.item);
              }}
            >
              查看挂链笔记
            </Button>
          </Space>
        }
        onCancel={() => setDetailOpen(false)}
      >
        {detailLoading ? (
          <Spin />
        ) : detailError ? (
          <Alert type="error" showIcon message="商品详情加载失败" description={detailError} />
        ) : detail ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            {detail.platform_missing_at && (
              <Alert
                type="warning"
                showIcon
                message={`对账发现平台已没有这件商品（标记于 ${detail.platform_missing_at}）`}
                description="本地台账仍保留此行；平台重新上架后下一轮同步会自愈清除标记。"
              />
            )}
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="商品名称">{detail.item.title || "（无标题）"}</Descriptions.Item>
              <Descriptions.Item label="商品 ID">
                <Space size={4}>
                  {detail.item.item_id}
                  <CopyOutlined
                    style={{ fontSize: 11, cursor: "pointer" }}
                    onClick={() => void copyToClipboard(detail.item.item_id, "商品 ID 已复制")}
                  />
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="货号">{detail.item.article_no || "-"}</Descriptions.Item>
              <Descriptions.Item label="分类">{detail.item.category || "未分类"}</Descriptions.Item>
              <Descriptions.Item label="价格">{priceText(detail.item)}</Descriptions.Item>
              <Descriptions.Item label="库存">
                {detail.item.stock ?? "-"}
                {detail.item.sku_count != null ? `（${detail.item.sku_count} 个规格）` : ""}
              </Descriptions.Item>
              <Descriptions.Item label="销量">
                近 30 天 {detail.item.sales_30d ?? "-"} · 累计 {detail.item.sales_total ?? "-"}
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <Space size={4} wrap>
                  {detail.item.status_label || "状态 -"}
                  {detail.item.frozen && <Tag color="red">已冻结</Tag>}
                  {detail.item.buyable ? <Tag color="green">可购买</Tag> : <Tag>不可购买</Tag>}
                  {detail.item.stock_warning_sku_count > 0 && (
                    <Tag color="orange">{detail.item.stock_warning_sku_count} 个规格库存告警</Tag>
                  )}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">{detail.item.create_time || "-"}</Descriptions.Item>
              <Descriptions.Item label="上架时间">{detail.item.on_shelf_time || "-"}</Descriptions.Item>
              <Descriptions.Item label="更新时间">{detail.item.update_time || "-"}</Descriptions.Item>
              <Descriptions.Item label="挂链笔记">{detail.item.note_count} 篇</Descriptions.Item>
              <Descriptions.Item label="台账同步于">
                {detail.synced_at || "-"}
              </Descriptions.Item>
            </Descriptions>

            {detail.images.length > 0 && (
              <div>
                <Text strong style={{ fontSize: 13 }}>
                  商品图集（{detail.images.length} 张）
                </Text>
                <div style={{ marginTop: 6 }}>
                  <Image.PreviewGroup>
                    <Space size={8} wrap>
                      {detail.images.map((link) => (
                        <Image
                          key={link}
                          src={link}
                          width={84}
                          height={84}
                          style={{ objectFit: "cover", borderRadius: 4 }}
                        />
                      ))}
                    </Space>
                  </Image.PreviewGroup>
                </div>
              </div>
            )}

            <div>
              <Text strong style={{ fontSize: 13 }}>
                规格详情
              </Text>
              {detail.detail_synced_at ? (
                <Text type="secondary" style={{ fontSize: 11, marginInlineStart: 8 }}>
                  （补于 {detail.detail_synced_at}
                  {detail.sku_count != null ? ` · ${detail.sku_count} 个规格` : ""}）
                </Text>
              ) : null}
              {detail.skus.length > 0 ? (
                <Table
                  rowKey={(row) => row.sku_id || row.sku_name}
                  size="small"
                  style={{ marginTop: 6 }}
                  dataSource={detail.skus}
                  pagination={false}
                  scroll={{ y: 240 }}
                  columns={[
                    { title: "规格", dataIndex: "sku_name", render: (v: string) => v || "-" },
                    { title: "变体", dataIndex: "variant_name", render: (v: string) => v || "-" },
                    { title: "条形码", dataIndex: "barcode", render: (v: string) => v || "-" },
                    { title: "ERP 编码", dataIndex: "erp_code", render: (v: string) => v || "-" },
                    {
                      title: "价格",
                      dataIndex: "price_fen",
                      render: (v: number | null) => (v == null ? "-" : `¥${yuan(v)}`),
                    },
                    {
                      title: "原价",
                      dataIndex: "original_price_fen",
                      render: (v: number | null) => (v == null ? "-" : `¥${yuan(v)}`),
                    },
                    { title: "库存", dataIndex: "stock", render: (v: number | null) => skuCell(v) },
                    { title: "重量(g)", dataIndex: "weight", render: (v: number | null) => skuCell(v) },
                    {
                      title: "可售",
                      key: "buyable",
                      render: (_v, row) =>
                        row.buyable === null ? (
                          "-"
                        ) : (
                          <Tag color={row.buyable ? "green" : "default"}>
                            {row.buyable ? "可售" : "停售"}
                          </Tag>
                        ),
                    },
                  ]}
                />
              ) : (
                <Empty
                  style={{ marginTop: 6 }}
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="规格详情还没补齐（同步商品任务会自动多轮补足，届时这里展示各规格的价格 / 库存 / 条形码）"
                />
              )}
            </div>
          </Space>
        ) : null}
      </Modal>

      <Modal
        open={bindOpen}
        title="绑定千帆商家后台（商品列表来源）"
        okText="绑定"
        cancelText="取消"
        confirmLoading={binding_}
        onOk={() => void handleBind()}
        onCancel={() => setBindOpen(false)}
      >
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          <Text style={{ fontSize: 13 }}>
            要的是千帆自己的登录态：登录 https://ark.xiaohongshu.com/ → F12 → Network →
            任一请求 → 复制 Cookie，粘贴到下面（须含 access-token-ark.xiaohongshu.com）。
          </Text>
          <Input.TextArea
            rows={5}
            value={bindCookie}
            placeholder="粘贴千帆 Cookie（形如 a1=...; access-token-ark.xiaohongshu.com=...）"
            onChange={(event) => setBindCookie(event.target.value)}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            Cookie 会加密落库，仅用于读取你自己店铺的商品与笔记挂链数据；只读、不做任何写操作。
          </Text>
        </Space>
      </Modal>

      {/*
        商品 → 挂链笔记弹窗（设计 13 §31.6）：**关系表源**（note_goods join notes），
        零平台请求。多篇笔记用 tab 自然隔离；内容区复用内容库详情弹窗的同一份
        两栏版式（NoteDetailBody）—— 同一语义不留两份实现。
      */}
      <Modal
        open={notesOpen}
        // 尺寸（v1.36 用户口径「挂链笔记弹窗全屏 75%」）：宽高都取视口的 75%。
        // 原来的 80vw 是「尽量大」，与商品详情弹窗（80%）几乎同宽 —— 两个弹窗
        // 看起来一样大、层级感反而没了；收到 75% 后它是「一个更大的详情浮层」，
        // 与列表的对比更清楚。**不再设 maxWidth**：设了它就不是 75% 了
        // （宽屏上 75vw > 1600 会被截回，口径与实际不符）。
        width="75vw"
        centered
        // `overflow: "auto"` 而不是 `"hidden"`：75vw 下两栏装不下的视口区间只剩
        // ~900–918px（75vw−48 要 ≥ 300+320+20），与 `.note-detail-body` 那个
        // 900px 媒体查询几乎首尾相接 —— 留 `auto` 只是把那 18px 的缝补上：
        // 装得下时子列自己滚（`overflow-y: auto`），body 永不溢出，表现与
        // `hidden` 一致；装不下时它正好接住，不会把右栏裁掉。
        styles={{ body: { height: "75vh", overflow: "auto", paddingRight: 12 } }}
        title={notesItem ? `「${notesItem.title}」的挂链笔记` : "挂链笔记"}
        footer={null}
        onCancel={() => setNotesOpen(false)}
        destroyOnClose
      >
        <div>
          {/* 2026-09-23 上机口径：顶部那条「本地已映射 1937/1960 条商品笔记 ——
              反查走笔记↔商品关系表…」说明**整块移除**（弹窗里只要笔记本体）。
              覆盖率与口径解释留在列表页「挂链笔记」列的 Tooltip 里，不在这里重复。 */}
          {/*
            「在内容库中查看」（设计 13 §25.8）：这个弹窗证明「该商品有 N 篇
            挂链笔记」，但内容库 1964 条按时间倒排、老笔记排在第几百位，
            关键词也未必对得上（用户两次报告「内容库里少一篇」都是这个原因）。
            跳转带 `?item_id=` 直达内容库的挂链商品筛选视图 —— 从商品到
            「该商品的笔记」不再靠碰运气。先关弹窗再跳（路由切换不会自动关它）。
          */}
          {!notesLoading && notesData && notesData.notes.length > 0 && notesItem && (
            <div style={{ marginBottom: 12, textAlign: "right" }}>
              <Button
                size="small"
                icon={<DatabaseOutlined />}
                onClick={() => {
                  setNotesOpen(false);
                  navigate(`/platforms/xhs/library?item_id=${encodeURIComponent(notesItem.item_id)}`);
                }}
              >
                在内容库中查看（{notesData.count} 篇）
              </Button>
            </div>
          )}
          {notesLoading ? (
            <div style={{ padding: "48px 0", textAlign: "center" }}>
              <Spin />
            </div>
          ) : notesData && notesData.notes.length ? (
            notesData.notes.length === 1 ? (
              <NoteDetailBody
                mediaUrls={notesData.notes[0].images}
                videoUrl={notesData.notes[0].video_url}
                isVideo={Boolean(notesData.notes[0].video_url)}
                mediaNamePrefix={notesData.notes[0].note_id}
              >
                {renderItemNoteInfo(notesData.notes[0])}
              </NoteDetailBody>
            ) : (
              <Tabs
                activeKey={notesTab}
                onChange={setNotesTab}
                items={notesData.notes.map((note, index) => ({
                  key: String(index),
                  label: (
                    <Tooltip title={note.title || "（无标题）"}>
                      <span style={{ maxWidth: 180, display: "inline-block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {note.title || "（无标题）"}
                      </span>
                    </Tooltip>
                  ),
                  children: (
                    <NoteDetailBody
                      mediaUrls={note.images}
                      videoUrl={note.video_url}
                      isVideo={Boolean(note.video_url)}
                      mediaNamePrefix={note.note_id}
                    >
                      {renderItemNoteInfo(note)}
                    </NoteDetailBody>
                  ),
                }))}
              />
            )
          ) : (
            <Empty description="关系表里这个商品还没有挂链笔记（可能是未同步，也可能确实没挂；「生成笔记」会建立新的映射）" />
          )}
        </div>
      </Modal>
    </div>
  );
}
