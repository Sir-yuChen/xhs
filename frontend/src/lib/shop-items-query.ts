/**
 * 商品列表的**请求构造**（DEF-093，2026-10-05）。
 *
 * ## 为什么单独成文件
 *
 * 列表查询与 CSV 导出**必须命中同一个集合**（设计 20 §2.2 明文口径：导出「复用同一份
 * 筛选」，不许重新实现一份）。修这个缺陷之前，两处各拼各的：
 *
 * - 列表 `load()` 拼的是 `create_time_from` / `create_time_to`（对，后端要的就是它）；
 * - 导出却拿 `formValuesToDraft()` 的**本地缓存键名** `create_from` / `create_to`
 *   ⇒ 导出端点的模型带 `extra="forbid"` ⇒ **必然 422**
 *   （页面只显示「请求失败，请稍后重试」，用户拿不到任何指向性原因）。
 *
 * **列表一直是好的，只有导出烧** —— 这正是「同一件事两份实现」的典型后果：
 * 后端测试自己拼正确键名，永远绿；浏览器里点一下就 100% 复现。
 *
 * 现在两处都调用 `formValuesToQuery()`，差别只有「带不带分页」。
 *
 * ## 边界（谁负责什么）
 *
 * | 关注点 | 归属 |
 * |---|---|
 * | 表单值 → 查询条件（本文件） | 前端，**唯一一份** |
 * | 本地筛选缓存（`filter-cache.ts` 的 draft） | 前端，键名与请求体**故意不同**，互不代替 |
 * | 查询条件 → 后端过滤语义 | 后端（`_apply_filters` 一处） |
 *
 * ⚠️ 字段名必须与后端 `ShopItemsRequest` / `ItemsExportRequest` **逐字一致**；
 * 后端开了 `extra="forbid"`，多一个键就是 422（不是「静默丢弃」——那更坏）。
 */

import dayjs from "dayjs";

import type { XhsNoteCountOp, XhsShopItemStatus, XhsShopItemsQuery } from "../types";
import { noteCountFilter, type SortState } from "./shop-items-view";

/**
 * 搜索区表单的值（antd Form 的 `useForm<QueryForm>()`）。
 *
 * 日期是 **dayjs 对象对**（`RangePicker` 只认 dayjs）；落缓存时由
 * `formValuesToDraft` 转成 `YYYY-MM-DD` 字符串。
 */
export type QueryForm = {
  keyword?: string;
  status: XhsShopItemStatus;
  price_from?: number;
  price_to?: number;
  stock_from?: number;
  stock_to?: number;
  sales_from?: number;
  sales_to?: number;
  /** 创建时间区间（千帆的「创建时间」筛选）。 */
  create_time_range?: [dayjs.Dayjs | null, dayjs.Dayjs | null] | null;
  /** 挂链笔记数：比较符（默认「小于」）+ 指定篇数（留空 = 不筛）。v1.34 */
  note_count_op?: XhsNoteCountOp;
  note_count?: number;
};

/** `formValuesToQuery` 的上下文（表单之外的、来自页面状态的那几个维度）。 */
export type QueryContext = {
  accountId: number;
  /** 不传 = 不带 `page_no`（**导出走的就是这条** —— 导出是全量集合，不是一页）。 */
  pageNo?: number;
  /** 同上；导出不传。 */
  pageSize?: number;
  /** 当前排序；`null` = 无排序态 ⇒ **不带** `sort_field` / `order`。 */
  sort?: SortState | null;
  // ⚠️ 分组筛选（`groupFilter`）已随「彻底移除商品分组」退场（2026-10-05）。
};

/**
 * antd Form 值 → **列表 / 导出共用的查询条件**。
 *
 * 口径细节（都在这里一处，别再散到调用方）：
 *
 * 1. **日期按天传**（`YYYY-MM-DD`），结束日由后端补 `23:59:59.999` ——
 *    前端不替后端算毫秒（少一份「谁补的」的争论）。
 * 2. **挂链笔记数留空 = 不带这个参数**（= 不筛），不是「小于 0」
 *    —— 兜成 0 会一进页面就把有挂链笔记的商品全筛掉（`noteCountFilter` 负责）。
 * 3. **无排序态不带** `sort_field` / `order`：让后端用它自己的稳定默认序。
 *    前端自己编一个默认字段等于替后端做主 = 第二份真相源。
 * 4. **分页只给列表**：导出带上分页参数就只会导出一页（而链路判据是
 *    「CSV 行数 == 列表命中总数」）。
 */
export function formValuesToQuery(
  values: QueryForm,
  options: QueryContext
): XhsShopItemsQuery {
  const range = values.create_time_range;
  const { accountId, pageNo, pageSize, sort: sortArg } = options;
  return {
    platform_account_id: accountId,
    // ⚠️ 分页**只给列表**（导出是全量集合，分页参数会把它切成一页）
    ...(pageNo !== undefined ? { page_no: pageNo } : {}),
    ...(pageSize !== undefined ? { page_size: pageSize } : {}),
    keyword: values.keyword?.trim() || undefined,
    //: 状态默认「全部」（用户口径 2026-10-11「搜索区间状态默认全部」）——
    //: 与 `EMPTY_SHOP_ITEMS_DRAFT.status` 同一取值（lib 唯一一份默认值）。
    status: values.status || "all",
    price_from: values.price_from ?? undefined,
    price_to: values.price_to ?? undefined,
    stock_from: values.stock_from ?? undefined,
    stock_to: values.stock_to ?? undefined,
    sales_from: values.sales_from ?? undefined,
    sales_to: values.sales_to ?? undefined,
    create_time_from: range?.[0] ? dayjs(range[0]).format("YYYY-MM-DD") : undefined,
    create_time_to: range?.[1] ? dayjs(range[1]).format("YYYY-MM-DD") : undefined,
    ...noteCountFilter(values.note_count_op, values.note_count),
    ...(sortArg
      ? { sort_field: sortArg.field, order: sortArg.order === "ascend" ? "asc" : "desc" }
      : {}),
    // ⚠️ 分组筛选（`group_id`）已随「彻底移除商品分组」退场（2026-10-05）。
  };
}
