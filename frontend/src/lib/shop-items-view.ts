/**
 * 商品列表页的**纯逻辑**（设计 13 §30，v1.32 本地化改造）。
 *
 * 页面（`shop-items-page.tsx`）只消费这里的函数；行为验证脚本
 * `tests/frontend/shop_items_view.cjs` 用 esbuild 打包**本模块**直接驱动
 * （与 `sync-gates.ts` 同款做法：源码断言证明不了「函数怎么跑」）。
 *
 * 口径（前四条是用户 2026-09-22 拍板的，后三条 2026-09-23 上机验收追加）：
 * 1. 账号下拉**不按类型过滤**（`sub_type` 预判 = 第二份真相源，门禁归后端）；
 * 2. 本地台账**有时效**：同步时间必须可见，缺值不渲染 0；
 * 3. 审核态四相**本地无法区分**（平台列表响应不含 card_type）⇒ 安静说明，
 *   不假装能筛（缺值口径）；
 * 4. SKU 明细缺值渲染 `-`，**不渲染 0**（「读不到」≠「读到 0」）；
 * 5. **只有「上架时间」可排、且默认不排序**（见 `SORT_FIELD_BY_COLUMN` / `resolveSort`）：
 *    进来表头没有箭头，点一下才由**后端**重排（升 → 降 → 取消三级）；
 * 6. **千帆可用性是前置门禁**（见 `ledgerPrerequisite`）：不通过就一个提示、不发请求；
 * 7. **批量删除只删本地台账行**（见 `batchDeleteBlock` / `batchDeleteHint`）：
 *    平台商品不动、关系行不动，两个连带后果写进确认框。
 */

import { channelUnavailable } from "./sync-gates";
import type { XhsLoginChannels } from "../types";

/** 账号下拉项的最小形状（`PlatformAccount` 的超集）。 */
export type ShopAccountLike = {
  id: number;
  nickname: string;
  sub_type: string;
  platform: string;
};

/**
 * 状态筛选键里**无法从本地台账区分**的四个审核态。
 *
 * 依据（2026-09-22 真机核对）：平台 `search_item_v2` 响应**不含 `card_type`**
 * （1974 条台账 0 命中），审核态四相只能从查询条件表达、不能按行推导。
 * 选中它们 ⇒ 空集 + 一行安静说明，**不猜**。
 */
export const UNDERIVABLE_STATUSES: readonly string[] = [
  "auditing",
  "audit_rejected",
  "audit_passed",
  "rectify",
];

/** 账号下拉候选：**只留小红书账号，不按 sub_type 过滤**（v1.32 用户口径）。 */
export function selectableAccounts<T extends { platform: string }>(accounts: T[]): T[] {
  return accounts.filter((account) => account.platform === "xhs");
}

/**
 * 账号选项标签：**只显示账号名称**（2026-09-24 用户口径）。
 *
 * 演进：v1.32 去掉类型后缀（「（创作者）」随类型过滤一并删除）→ 2026-09-24
 * 连店铺标注也去掉，四处下拉统一成「只展示账号名称」。
 *
 * 为什么连店铺也去掉：账号 → 店铺是 **1:1**（DB 部分唯一索引强制），下拉里
 * 再标一遍店名是**同一关系的第二处展示**，而它每次都占用一行宽度。店铺信息
 * 该由页面主体（店铺列 / 绑定态）回答，下拉只负责「选哪个账号」。
 *
 * `shopName` 参数保留（调用方与既有行为脚本的签名不动），但不再参与拼接 ——
 * 缺省兜底也不再写「未绑店铺」，那是页面主体的活。
 */
export function accountLabel(nickname: string, _shopName?: string): string {
  return (nickname || "").trim() || "未命名账号";
}

/**
 * 审核态筛选的安静说明（25–30 字，一行）：选中四个审核态之一时展示。
 * 其余状态（含 `all`）返回空串 —— 不制造噪声。
 */
export function statusFilterHint(status: string): string {
  if (UNDERIVABLE_STATUSES.includes(status)) {
    return "本地台账暂无法区分审核态，该筛选查不到数据";
  }
  return "";
}

/** SKU / 数值单元格：缺值渲染 `-`（`null` 保持「取不到」，不显示成 0）。 */
export function skuCell(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : String(value);
}

/**
 * 挂链笔记数筛选的比较符（v1.34，用户口径「商品列表搜索条件添加挂链笔记数，
 * **默认小于指定篇数**」）。
 *
 * 顺序即下拉顺序：「小于」排第一 —— 它既是默认值，也是这个筛选最常用的意图
 * （找出「还没怎么挂笔记」的商品）。⚠️ 后端 `NOTE_COUNT_OPS` 白名单与这里
 * **逐个相等**（未知值后端 400），两处必须一起改。
 */
export const NOTE_COUNT_OPS = ["lt", "lte", "eq", "gte", "gt"] as const;

export type NoteCountOp = (typeof NOTE_COUNT_OPS)[number];

/** 比较符 → 中文（下拉展示用）。 */
export const NOTE_COUNT_OP_LABELS: Record<NoteCountOp, string> = {
  lt: "小于",
  lte: "不大于",
  eq: "等于",
  gte: "不小于",
  gt: "大于",
};

/** 默认比较符：**小于**（用户口径「默认小于指定篇数」）。 */
export const DEFAULT_NOTE_COUNT_OP: NoteCountOp = "lt";

/** 下拉选项（顺序即 `NOTE_COUNT_OPS`）。 */
export const NOTE_COUNT_OP_OPTIONS: { value: NoteCountOp; label: string }[] = NOTE_COUNT_OPS.map(
  (value) => ({ value, label: NOTE_COUNT_OP_LABELS[value] })
);

/**
 * 挂链笔记数 → 请求参数（v1.34）。**留空 = 不筛**，不是「小于 0」。
 *
 * 为什么空值不兜成 `0`：`0` 是**有意义的阈值**（「一篇都没挂」）—— 把它当成「没填」
 * 会让用户一进页面就只看到 0 篇的商品。缺省态**不许偷偷筛掉数据**。
 * 非白名单比较符落到默认 `lt`（与 `resolveSort` 同族：界面只会送出白名单值，
 * 这条是给「将来有人手改」兜底；后端另有 400 硬闸）。
 */
export function noteCountFilter(
  op: string | undefined,
  value: number | null | undefined
): { note_count_op?: NoteCountOp; note_count?: number } {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return {};
  }
  const normalized = (NOTE_COUNT_OPS as readonly string[]).includes(op || "")
    ? (op as NoteCountOp)
    : DEFAULT_NOTE_COUNT_OP;
  return { note_count_op: normalized, note_count: Math.max(0, Math.trunc(Number(value))) };
}

/**
 * 表头排序（v1.32 用户口径：「排序字段和方向在表头实现，上下箭头实现」；
 * 2026-09-23 上机口径**收紧为只剩一列**：「商品台账的只能根据上架时间列排序，
 * 其它列不支持排序」）。
 *
 * 为什么只留一列（不是漏配）：商品从同步落库时**整批同源**，价格 / 库存 / 销量
 * 在业务上没有「按它排就看得懂」的语义（价格区间、库存含告警规格、销量有双口径），
 * 给了箭头反而让人以为列表按那个口径过滤过。**表头有箭头的列 = 后端真会排的列**，
 * 多一个会排的列就多一份「界面在暗示什么」的解释成本。
 *
 * 列 dataIndex → 后端 `sort_field`。
 */
export const SORT_FIELD_BY_COLUMN: Record<string, string> = {
  on_shelf_time: "on_shelf_time",
};

/** 排序状态：字段 + antd 的方向（`ascend` / `descend`）。 */
export type SortState = { field: string; order: "ascend" | "descend" };

/**
 * 默认排序态：**无**（用户 2026-09-23 上机口径「默认不排序，只有点击触发后进行排序，
 * 参考 antd 组件中的排序」）。
 *
 * 原先是「上架时间倒序」—— 一进页面表头就挂着一支高亮箭头、列表也已经是排过的，
 * 用户分不清「这个序是页面自带的还是我点的」。现在回到 antd 原生交互：**进来无箭头**，
 * 点一下升序、再点降序、第三下取消，每次都回服务端重排。
 * 「不带 `sort_field`」= 让后端用它自己的稳定默认序（`create_time` 倒序）；底部
 * 「当前排序」行仍如实渲染**后端回传的生效口径**（设计 13 §32.1），不因此变成空。
 */
export const DEFAULT_SORT: SortState | null = null;

/**
 * 每页条数候选（分页器 `pageSizeOptions`）与默认值。
 *
 * 抽到 lib 是因为它有**第二个消费者**：本地缓存的取值域校验（2026-09-26
 * 「筛选条件落本地缓存」）—— 缓存里若是 `999` 这种页面选不出来的值，分页器会
 * 显示一个用户没法理解的「每页 999 条」。两处各写一遍 = 迟早对不上。
 */
export const SHOP_ITEMS_PAGE_SIZES = [20, 50, 100] as const;
export const DEFAULT_SHOP_ITEMS_PAGE_SIZE = 20;

/** 缓存的排序态校验（形状对不上就回「无排序」，不是回某个默认字段）。 */
export function sanitizeSortState(raw: unknown, fallback: SortState | null): SortState | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return fallback;
  const candidate = raw as { field?: unknown; order?: unknown };
  const field = typeof candidate.field === "string" ? candidate.field : "";
  const order = candidate.order === "ascend" || candidate.order === "descend" ? candidate.order : null;
  return field && order ? { field, order } : fallback;
}

/** 排序字段 → 中文名（v1.32 起表头箭头驱动排序；这里给「当前排序」提示行用）。 */
export const SORT_FIELD_LABELS: Record<string, string> = {
  create_time: "创建时间",
  on_shelf_time: "上架时间",
  off_shelf_time: "下架时间",
  last_audit_time: "审核时间",
  price_min_fen: "价格",
  stock: "库存",
  item_sale_qty_30: "近 30 天销量",
};

/**
 * 「当前排序」一行文案（设计 13 §32.1）。
 *
 * 入参是**后端回传的生效口径**（`sort_field` / `order`），不是前端自己的猜想 ——
 * 后端对未知字段会回退（默认 `create_time`），回退结果只有它知道。所以「无排序」
 * 时这行照样有值（后端仍在按它的稳定默认序返回），这就是它比表头箭头更可靠的地方：
 * 箭头表达「用户点了什么」，这行表达「数据实际按什么排的」。
 */
export function sortSummary(sortField: string, order: string): string {
  const name = SORT_FIELD_LABELS[sortField] || sortField || SORT_FIELD_LABELS.on_shelf_time;
  return `${name} · ${order === "asc" ? "升序" : "降序"}`;
}

/**
 * antd sorter 回调 → 新的排序状态（`null` = 无排序）。
 *
 * 与 antd 原生三级循环一致：升序 → 降序 → **取消**（`order` 为 `null`/`undefined`）。
 * 取消 ⇒ 返回 `null`（不回某个默认序）—— 此时**不带排序参数**请后端用它的默认序，
 * 表头箭头随之消失。
 *
 * 非白名单列（将来若有人给别的列加错箭头）同样落 `null`：**表头有箭头的列必须是
 * 后端真会排的列**，点了它却改变列表顺序才叫欺骗。
 */
export function resolveSort(
  dataIndex: string | undefined,
  order: string | null | undefined
): SortState | null {
  const field = dataIndex ? SORT_FIELD_BY_COLUMN[dataIndex] : undefined;
  if (!field || (order !== "ascend" && order !== "descend")) {
    return null;
  }
  return { field, order };
}

/**
 * 前置门禁不通过时的**唯一**提示（用户 2026-09-23 口径：只提示这一句）。
 *
 * 「只需提示，请在账号信息卡片中扫码登录千帆后再来查看，移除其它错误提示」——
 * 所以这里只有一句话，不含原因分类、不含操作步骤、不追加第二条提示。原因分类
 * 交给 `ledgerPrerequisite().reason`（给测试与将来的扩展留口，**不出现在界面上**）。
 */
export const QIANFAN_PREREQUISITE_HINT = "请在账号信息卡片中扫码登录千帆后再来查看。";

/** 前置门禁的判定结果。 */
export type LedgerPrerequisite = {
  /** 通过 ⇒ 才允许发商品列表请求（不通过时一个请求都不发）。 */
  ok: boolean;
  /**
   * 不通过的原因：`unbound` = 没有绑店铺；`ark_unavailable` = 千帆登录态确定不可用。
   * **不用于界面文案**（界面只有 `QIANFAN_PREREQUISITE_HINT` 一句）。
   */
  reason: "" | "unbound" | "ark_unavailable";
};

/**
 * 商品台账的**前置校验**（用户 2026-09-23 上机口径：「将校验当前账号下是否存在
 * 可用的千帆账号作为前置校验」）。
 *
 * 两个条件缺一不可：
 * 1. **已绑店铺**（`binding.bound`）—— 没有店铺就没有台账（后端 `/items` 也会 400）；
 * 2. **千帆登录态不是「确定不可用」**—— 判据与账号卡片、同步按钮**同一个**
 *    `channelUnavailable`（ark 通道）：`unknown`（未检测 / 探活没结论）**放行**，
 *    那是 DEF-057 红线（没问到 ≠ 不可用）。
 *
 * 为什么要在前端先拦一道：不拦的话，切到一个没绑千帆的账号时会**连着冒三种提示**
 * （列表 400 的 toast + 页面错误块 + 未绑定说明），用户看到的是「乱七八糟的异常」。
 * 拦在这里 ⇒ 只剩一句话（`QIANFAN_PREREQUISITE_HINT`），且**零请求**。
 */
export function ledgerPrerequisite(
  bound: boolean | undefined,
  channels: XhsLoginChannels | undefined
): LedgerPrerequisite {
  if (!bound) {
    return { ok: false, reason: "unbound" };
  }
  if (channelUnavailable(channels, "ark")) {
    return { ok: false, reason: "ark_unavailable" };
  }
  return { ok: true, reason: "" };
}

// --------------------------------------------------------------------------- #
// 批量删除（v1.36，用户口径「商品列表新增批量删除操作」）
// --------------------------------------------------------------------------- #

/**
 * 批量删除单次上限。**必须与后端 `qianfan.MAX_BATCH_DELETE` 逐个相等**
 * （`test_page_optimizations.py` 直接读本文件比对，改一处不改另一处会红）。
 *
 * 为什么前后端各有一份而不是只留后端：前端要在**发请求前**就把「超了」拦成一句
 * 说明（别让用户点下去才收 400）；后端那份是硬闸（前端只是界面，不是边界）。
 * 这与 `NOTE_COUNT_OPS` 同一处理：值域小、语义单一，两份相等比多一个查询接口便宜。
 */
export const MAX_BATCH_DELETE = 200;

/** 页面上删除按钮的勾选键 → 去重保序的 `item_id`（去空白、丢空串）。 */
export function normalizeDeleteIds(
  keys: readonly (string | number)[] | undefined
): string[] {
  const seen = new Set<string>();
  const ids: string[] = [];
  for (const key of keys || []) {
    const id = String(key ?? "").trim();
    if (id && !seen.has(id)) {
      seen.add(id);
      ids.push(id);
    }
  }
  return ids;
}

/** `normalizeDeleteIds` 的判据：`""` = 可以删；否则是不让删的原因。 */
export type BatchDeleteBlock = "" | "empty" | "too_many";

/**
 * 能不能发批量删除。**三种取值各有各的出口**（页面据此禁用按钮 / 给文案）——
 * 不要把它压成 boolean：`empty` 与 `too_many` 要给用户**不同**的下一步。
 */
export function batchDeleteBlock(keys: readonly (string | number)[] | undefined): BatchDeleteBlock {
  const ids = normalizeDeleteIds(keys);
  if (!ids.length) {
    return "empty";
  }
  if (ids.length > MAX_BATCH_DELETE) {
    return "too_many";
  }
  return "";
}

/**
 * 批量删除确认框的正文（**必须把两个连带后果写清**，不藏）。
 *
 * 用户口径（2026-09-23）定的是「只删本地台账行」，那这件事就有两个用户**看不到**
 * 的后果，确认框是唯一能讲清的地方：
 * 1. 平台侧还有的商品，下次「同步商品」会把它**写回来** ⇒ 删除是清台账，不是拉黑；
 * 2. 笔记↔商品关系**一并删**（⚠️ 2026-09-23 用户口径「删除商品时也需要删除笔记与
 *    商品的关联关系」，**推翻** v1.36 的「关系不动」）⇒ 挂载这些商品的笔记仍在，
 *    但它们的挂载关系没了；商品被同步写回时关系**不会**自动接上（要重新解析）。
 *
 * 只说「确认删除这 N 个商品？」= 让用户在不知道后果的情况下点确定。
 */
export function batchDeleteHint(count: number): string {
  return (
    `将从本地台账删除 ${count} 个商品行。\n\n` +
    "① 这些商品的笔记↔商品关联关系一并删除（笔记本身不受影响，商品被同步写回后需重新解析挂载）；\n" +
    "② 平台侧仍有这些商品，下次「同步商品」会把它们重新写回台账。"
  );
}

