/**
 * 页面筛选条件 / 账号选择的**本地缓存**（2026-09-26 用户口径：
 * 「梳理所有商品列表，我的笔记，内容库页，筛选条件和账号选择后放本地缓存，
 * 避免每次均需要重新选择」）。
 *
 * **唯一实现**（DEF-044）：三个页面共用这一份 —— 页面只声明「哪个 key + 默认值 +
 * 取值域」，读写 / 容错 / 自愈都在这里。各页自己拼 `localStorage.getItem` 必然
 * 出现三种容错口径（一处 try/catch、一处崩、一处把坏值当合法值用）。
 *
 * 三条硬口径：
 *
 * 1. **缓存是「体验」不是「真相」** —— 任何读失败（隐私模式、配额满、手改过的
 *    脏 JSON）都必须**静默回默认值**，绝不能让一个坏缓存把页面打红（读失败时
 *    顺手删掉那条 key，免得每次进页面都解析一次坏 JSON）。
 * 2. **取值域校验** —— 缓存里的值要过**当前代码认的取值集合**（枚举筛选）。
 *    后端改了枚举 / 前端改了默认值之后，旧缓存不能变成「界面选了一个后端不认的
 *    值」（那会 400 或筛成空集，用户看不出原因）。
 * 3. **账号类缓存要能自愈** —— 账号可能已被删除，缓存里的 id 就成了下拉里不存在的
 *    裸数字 + 恒空列表。页面在账号列表到手后校验一次（见各页的 `*_SELF_HEAL`
 *    注释），失配就退回默认并覆盖缓存。
 *
 * 不缓存的东西（刻意的）：**分页游标 / 页码**（每次进页面从第一页开始）、
 * 详情弹窗状态、`page`（列表累加）、搜索结果（关键词只是**条件**，搜不搜由页面决定）。
 */

import { useEffect, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import { NOTE_TYPE_FILTER_VALUES, type NoteTypeFilter } from "./note-filters";
import { PUBLISH_STATUS_FILTER_OPTIONS, type PublishStatusFilter } from "./note-publish-gates";
import { NOTE_COUNT_OPS, type NoteCountOp } from "./shop-items-view";

/**
 * 缓存键清单 —— **每页一份命名空间**，键名带页面前缀（`xhs.filters.<page>.<field>`）。
 *
 * 为什么不按页面存一个大对象：一个页面十几个筛选，存成一个对象就得在每次写的时候
 * 把整份对象拼一遍（漏一个字段 = 那个筛选静默不生效）；一个字段一个 key 时，
 * 字段之间互不影响，读写的形状也就和 `useState` 一一对应。
 */
export const FILTER_CACHE_KEYS = {
  /** 内容库 `/platforms/xhs/library`。 */
  library: {
    keyword: "xhs.filters.library.keyword",
    topicId: "xhs.filters.library.topic-id",
    publishStatus: "xhs.filters.library.publish-status",
    accountId: "xhs.filters.library.account-id",
    noteType: "xhs.filters.library.note-type",
    itemId: "xhs.filters.library.item-id",
    hasAssets: "xhs.filters.library.has-assets",
    hasComments: "xhs.filters.library.has-comments",
  },
  /** 我的笔记 `/platforms/xhs/my-notes`。 */
  myNotes: {
    accountId: "xhs.filters.my-notes.account-id",
    tab: "xhs.filters.my-notes.tab",
    interactionKind: "xhs.filters.my-notes.interaction-kind",
    noTrafficThresholds: "xhs.filters.my-notes.no-traffic-thresholds",
    noTrafficQuery: "xhs.filters.my-notes.no-traffic-query",
  },
  /** 商品列表 `/platforms/xhs/shop-items`。 */
  shopItems: {
    accountId: "xhs.filters.shop-items.account-id",
    form: "xhs.filters.shop-items.form",
    pageSize: "xhs.filters.shop-items.page-size",
    sort: "xhs.filters.shop-items.sort",
  },
} as const;

/** 「我的笔记」四个页签（渲染顺序 + 合法取值**同一份**，页面拿它渲染 Tab 栏）。 */
export const MY_NOTES_TAB_VALUES = ["notes", "liked", "collected", "interactions"] as const;
export type MyNotesTab = (typeof MY_NOTES_TAB_VALUES)[number];

// --------------------------------------------------------------------------- #
// 存储层：localStorage 的最小封装（容错全在这里）
// --------------------------------------------------------------------------- #

/** 只需要这三个方法 —— 测试里也就能直接塞一个假 store。 */
type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/**
 * 拿到 localStorage，**拿不到就返回 null**（不是抛）。
 *
 * 两种真实场景都会让 `window.localStorage` 本身抛异常：浏览器隐私模式、被沙箱
 * iframe 嵌住。缓存是尽力而为的能力，不是页面能不能用的前提。
 */
function storage(): StorageLike | null {
  try {
    if (typeof window === "undefined") return null;
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}

/** 读一条缓存；**坏值一律当「没存过」**并把那条 key 删掉（见文件头口径 1）。 */
export function readFilterCache(key: string): unknown {
  const store = storage();
  if (!store) return null;
  let raw: string | null = null;
  try {
    raw = store.getItem(key);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    // 缓存对象形状由调用方的 sanitize 保证；这里只管「是不是合法 JSON」。
    return parsed;
  } catch {
    clearFilterCache(key);
    return null;
  }
}

/** 写一条缓存（写失败静默：配额满 / 隐私模式不该影响页面行为）。 */
export function writeFilterCache(key: string, value: unknown): void {
  const store = storage();
  if (!store) return;
  try {
    store.setItem(key, JSON.stringify(value));
  } catch {
    /* 尽力而为 */
  }
}

/** 删一条缓存（读坏值时自清，以及页面需要「忘掉这个筛选」时用）。 */
export function clearFilterCache(key: string): void {
  const store = storage();
  if (!store) return;
  try {
    store.removeItem(key);
  } catch {
    /* 尽力而为 */
  }
}

// --------------------------------------------------------------------------- #
// 取值域校验：缓存值必须落在**当前代码认的集合**里（口径 2）
// --------------------------------------------------------------------------- #

/** 校验器：拿到缓存原值 + 默认值，返回一个**一定能用**的值。 */
export type FilterSanitizer<T> = (raw: unknown, fallback: T) => T;

/** 字符串（非字符串一律回默认；空串是合法值 —— 它就是「不筛」）。 */
export function asString(raw: unknown, fallback = ""): string {
  return typeof raw === "string" ? raw : fallback;
}

/** 布尔（只认真正的 `true`；`"true"` / `1` 这类脏值不猜）。 */
export function asBoolean(raw: unknown, fallback = false): boolean {
  return typeof raw === "boolean" ? raw : fallback;
}

/** 有限数字（`NaN` / `Infinity` / 字符串数字都不算合法缓存值）。 */
export function asFiniteNumber(raw: unknown, fallback: number | null = null): number | null {
  return typeof raw === "number" && Number.isFinite(raw) ? raw : fallback;
}

/** 非空字符串（空 / 非字符串回默认）—— 账号 id 这类「必须有值」的缓存字段用。 */
export function asNonEmptyString(raw: unknown, fallback = ""): string {
  return typeof raw === "string" && raw.trim() ? raw : fallback;
}

/** 枚举筛选：值必须**逐字**在允许集合里（集合即后端认的取值，见各页 lib）。 */
export function asEnum<T extends string>(allowed: readonly T[]): FilterSanitizer<T> {
  return (raw: unknown, fallback: T) =>
    typeof raw === "string" && (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback;
}

/** 数字枚举（每页条数这类「只能从候选里挑」的值）。 */
export function asNumberEnum(allowed: readonly number[]): FilterSanitizer<number> {
  return (raw: unknown, fallback: number) =>
    typeof raw === "number" && allowed.includes(raw) ? raw : fallback;
}

/**
 * 对象型筛选（无流量弹窗的阈值 / 查询条件、商品列表的整份表单草稿）：
 * **按默认值的形状逐字段校验**，多出来的键丢掉、类型对不上的字段回默认。
 *
 * 为什么不整份 JSON 直接吃回去：`{"max_browse": "0"}`、`{"max_browse": null}`
 * 这类值一旦流进请求体，后端要么 422 要么把「不筛」当成 0 筛（数字类型的静默错值）。
 */
export function asFields<T extends object>(defaults: T): FilterSanitizer<T> {
  return (raw: unknown, fallback: T) => {
    const base = (fallback ?? defaults) as Record<string, unknown>;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return fallback ?? defaults;
    const source = raw as Record<string, unknown>;
    const next: Record<string, unknown> = { ...base };
    for (const [key, sample] of Object.entries(defaults as Record<string, unknown>)) {
      const value = source[key];
      if (typeof sample === "number") {
        if (typeof value === "number" && Number.isFinite(value)) next[key] = value;
      } else if (typeof sample === "boolean") {
        if (typeof value === "boolean") next[key] = value;
      } else if (sample === null) {
        // 可空标量（如 `published_before`）：null 保留，数字/字符串也认（时间戳两种形态）
        if (value === null || typeof value === "number" || typeof value === "string") {
          next[key] = value;
        }
      } else if (typeof value === "string") {
        next[key] = value;
      }
    }
    return next as T;
  };
}

// --------------------------------------------------------------------------- #
// Hook：与 useState 同形（`[value, setValue]`），额外在值变化时落缓存
// --------------------------------------------------------------------------- #

/**
 * 与 `useState` 同形、但**初值优先取缓存**、且每次变化自动落缓存的 state。
 *
 * 用法与 `useState` 一致（这正是刻意的：页面只需要把 `useState(...)` 换成
 * `usePersistedFilter(KEY, 默认值, 校验器)`，其余调用点一个字都不用改）：
 *
 * ```ts
 * const [accountFilter, setAccountFilter] = usePersistedFilter(
 *   FILTER_CACHE_KEYS.library.accountId, "", asString
 * );
 * ```
 *
 * `fallback` 支持函数形态（惰性初值，例如「URL 里有就用 URL 的」）。
 * 校验器省略 = 不做取值域校验（只用在不筛会立刻被后端 400 拦住的自由文本上）。
 */
export function usePersistedFilter<T>(
  key: string,
  fallback: T | (() => T),
  sanitize?: FilterSanitizer<T>
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    // 惰性初值：`fallback` 只在**首次挂载**读一次（函数形态用于「URL 优先」这类场景）。
    const initial = typeof fallback === "function" ? (fallback as () => T)() : fallback;
    const raw = readFilterCache(key);
    if (raw === null || raw === undefined) return initial;
    // 校验器负责「值域」；它自己不抛（内置校验器都是纯类型判断）。
    return sanitize ? sanitize(raw, initial) : (raw as T);
  });

  // 写只在 effect 里做一处：state updater 里写（React 严格模式下会被调用两次）
  // 与「谁调用谁写」都会让写入分散在多个地方 —— 一处写、一个判据。
  //
  // ⚠️ 首次挂载也会写：那份值可能刚被 sanitize 修正过（旧枚举 / 缺字段），
  // 顺手覆盖回缓存正是我们要的**自愈**（否则脏缓存会一直留着）。
  useEffect(() => {
    writeFilterCache(key, value);
  }, [key, value]);

  return [value, setValue];
}

// --------------------------------------------------------------------------- #
// 商品列表：「搜索条件」整份表单草稿（antd Form 不是 useState，单独一份读写）
// --------------------------------------------------------------------------- #

/**
 * 商品列表「搜索条件」的可缓存草稿 —— **纯 JSON 形状**（日期存 `YYYY-MM-DD` 字符串，
 * 不存 dayjs 对象：`JSON.stringify(dayjs)` 出来的是 `{"$d":...}` 这种脏值）。
 *
 * 字段名与千帆请求参数一致（`price_from` / `note_count_op`…），免得读的时候还要
 * 在脑子里做一次改名映射。
 */
export type ShopItemsFilterDraft = {
  keyword: string;
  /**
   * 千帆状态（与页面下拉、后端 `QIANFAN_ITEM_STATUSES` 同源）。
   * 这里只校验「非空字符串」：取值域由后端兜底（未知值 400 并回支持列表，不静默降级），
   * 而缓存里的值只可能来自本页下拉 —— 前端再抄一份状态白名单就是第二份真相源。
   */
  status: string;
  price_from: number | null;
  price_to: number | null;
  stock_from: number | null;
  stock_to: number | null;
  sales_from: number | null;
  sales_to: number | null;
  /** 创建时间区间（含当天；`YYYY-MM-DD`）。 */
  create_from: string | null;
  create_to: string | null;
  note_count_op: NoteCountOp;
  note_count: number | null;
};

/** 商品列表表单的清空态（「重置」= 回这里）。默认状态 `on_shelf` 与页面 `initialValues` 一致。 */
export const EMPTY_SHOP_ITEMS_DRAFT: ShopItemsFilterDraft = {
  keyword: "",
  status: "on_shelf",
  price_from: null,
  price_to: null,
  stock_from: null,
  stock_to: null,
  sales_from: null,
  sales_to: null,
  create_from: null,
  create_to: null,
  note_count_op: "lt",
  note_count: null,
};

/** 草稿校验：形状 + 取值域（比较符走 `NOTE_COUNT_OPS`，与后端同一份）。 */
export function sanitizeShopItemsDraft(raw: unknown): ShopItemsFilterDraft {
  const fields = asFields(EMPTY_SHOP_ITEMS_DRAFT)(raw, EMPTY_SHOP_ITEMS_DRAFT);
  return {
    ...fields,
    status: asNonEmptyString(fields.status, EMPTY_SHOP_ITEMS_DRAFT.status),
    note_count_op: (NOTE_COUNT_OPS as readonly string[]).includes(fields.note_count_op)
      ? fields.note_count_op
      : EMPTY_SHOP_ITEMS_DRAFT.note_count_op,
  };
}

// --------------------------------------------------------------------------- #
// 取值域常量的组合导出（页面只 import 一次，避免各页自己拼 allowed 列表）
// --------------------------------------------------------------------------- #

/** 发布状态四档（`all` = 不筛）—— 与内容库下拉同一份。 */
export const PUBLISH_STATUS_FILTER_VALUES = PUBLISH_STATUS_FILTER_OPTIONS.map(
  (option) => option.value
) as readonly PublishStatusFilter[];

/** 笔记类型（含 `""` = 不筛）—— 内容库下拉的合法取值。 */
export const NOTE_TYPE_FILTER_ALL_VALUES = [
  "",
  ...NOTE_TYPE_FILTER_VALUES,
] as readonly NoteTypeFilter[];
