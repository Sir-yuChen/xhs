/**
 * 「当前账号范围」—— **每条业务线一个跨页共享的账号选择**（设计 32 §5.4；2026-10-11）。
 *
 * ## 它解决什么
 *
 * 在此之前，账号维度是**每页各存一份**（实测 `lib/filter-cache.ts`）：
 *
 * ```
 * FILTER_CACHE_KEYS.library.accountId   = "xhs.filters.library.account-id"
 * FILTER_CACHE_KEYS.myNotes.accountId   = "xhs.filters.my-notes.account-id"
 * FILTER_CACHE_KEYS.shopItems.accountId = "xhs.filters.shop-items.account-id"
 * ```
 *
 * ⇒ 用户在内容库选了账号 A、切到「我的笔记」仍是账号 B（或反之），界面上**看不出**
 * 自己换过账号。这是「用错账号操作」类事故的**结构性来源** ——
 * 而本项目的账号维度是**数据隔离键**（ADR-021），选错账号不是体验问题，是数据问题。
 *
 * ## 粒度：每**业务线**一份，不是全局一份
 *
 * 店铺线与内容线**天然是两批账号**（一个账号可以两条线都占，但两条线的**工作对象**
 * 完全不同：商品台账 vs 笔记）。全局一份会强迫用户在两条线之间来回改账号 ——
 * 那是把「统一」做成了「互相干扰」。
 *
 * ## 为什么是纯函数 + localStorage（而不是 React Context）
 *
 * 本模块只做「读写 + 迁移」的**纯逻辑**，页面用 `useAccountScope` 包一层。
 * 好处：迁移规则（下面那段）可以被**单测直接驱动**，不需要渲染组件 ——
 * 本项目的 vitest 只覆盖纯逻辑模块（`environment=node`），把迁移规则写进组件里
 * 就等于**写了一条没有测试的规则**。
 */

import { clearFilterCache, readFilterCache, writeFilterCache } from "./filter-cache";
import { useCallback, useEffect, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import type { BusinessLine } from "../types";

/** 每条业务线各自记住的账号 id（`null` = 未选）。 */
export type AccountScope = Record<BusinessLine, number | null>;

/** 账号范围的缓存键（**每业务线一个**，与页面级筛选键刻意分开）。 */
export const ACCOUNT_SCOPE_KEYS: Record<BusinessLine, string> = {
  content: "xhs.scope.content.account-id",
  shop: "xhs.scope.shop.account-id",
  shared: "xhs.scope.shared.account-id",
};

/**
 * **一次性迁移源**：旧键（每页一份）→ 新键（每业务线一份）。
 *
 * ⚠️ 不迁移的后果：老用户已经选好的账号会被**静默重置**成「未选」——
 * 用户看到的是「我的账号选择不见了」，而代码里没有任何报错。
 * 这与「切账号先清绑定态」那类**有意的**清空不是一回事：那个是消除竞态，这个纯属丢数据。
 *
 * 迁移顺序 = 数组顺序（同一业务线内**最后一个可解析的值胜出**）；
 * 内容线内部三页的语义完全相同（都是「打创作者接口的账号」），所以取谁都成立。
 */
export const LEGACY_ACCOUNT_KEYS: Record<BusinessLine, readonly string[]> = {
  content: [
    "xhs.filters.library.account-id",
    "xhs.filters.my-notes.account-id",
  ],
  shop: ["xhs.filters.shop-items.account-id"],
  // 「平台」线没有页面级旧键（账号矩阵 / 数据洞察等本来就没有 per-page 账号缓存）。
  shared: [],
};

/** 把缓存里的原始值收敛成 `number | null`（非数字 / 非正数一律当未选）。 */
export function sanitizeAccountId(raw: unknown): number | null {
  if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) return Math.trunc(raw);
  if (typeof raw === "string" && raw.trim() !== "") {
    const parsed = Number(raw);
    if (Number.isFinite(parsed) && parsed > 0) return Math.trunc(parsed);
  }
  return null;
}

/**
 * 读一条业务线的当前账号：**先新键，再旧键（迁移），最后 null**。
 *
 * 迁移是**读时一次性**完成的（读到旧值就写进新键、并清掉旧键）：
 * 用户不需要做任何动作，也不会看到账号跳变。
 * 与 `usePersistedFilter` 的「惰性初值 + 首挂载自愈」同源思路。
 */
export function readAccountScope(line: BusinessLine): number | null {
  const current = sanitizeAccountId(readFilterCache(ACCOUNT_SCOPE_KEYS[line]));
  if (current !== null) return current;

  for (const legacyKey of LEGACY_ACCOUNT_KEYS[line]) {
    const legacy = sanitizeAccountId(readFilterCache(legacyKey));
    if (legacy !== null) {
      writeFilterCache(ACCOUNT_SCOPE_KEYS[line], legacy);
      // 清旧键：留着的话，用户在新键上改成别的账号后，下次「新键缺失」的场合
      // （清缓存 / 换浏览器配置）又会被旧值覆盖回来。
      clearFilterCache(legacyKey);
      return legacy;
    }
  }
  return null;
}

/** 写一条业务线的当前账号（`null` = 清空选择）。 */
export function writeAccountScope(line: BusinessLine, accountId: number | null): void {
  writeFilterCache(ACCOUNT_SCOPE_KEYS[line], accountId);
}

/** 读全部三条线（页面首挂载用）。 */
export function readAllAccountScopes(): AccountScope {
  return {
    content: readAccountScope("content"),
    shop: readAccountScope("shop"),
    shared: readAccountScope("shared"),
  };
}

/**
 * 页面用的 Hook：一条业务线的当前账号。
 *
 * 接口与 `useState<number | null>` **同形**（含函数式更新）—— 页面里已有
 * `setSelectedAccountId(current => …)` 这种调用，收窄成「只吃值」会让它们全部报错，
 * 而那种改写是纯噪声（同一个 setter 换两个名字）。
 *
 * ⚠️ 挂在**每条业务线**上（内容线各页共用一个键、店铺线各页共用一个键），
 * 所以「内容库选账号 A → 切到我的笔记」看到的就是 A ——
 * 这正是本模块存在的理由（此前三页三个键，切换后各记各的）。
 *
 * 迁移（旧键 → 新键）在 `readAccountScope` 里一次性完成，本 Hook 不重复判。
 */
export function useAccountScope(
  line: BusinessLine,
): [number | null, Dispatch<SetStateAction<number | null>>] {
  const [value, setValue] = useState<number | null>(() => readAccountScope(line));

  useEffect(() => {
    writeAccountScope(line, value);
  }, [line, value]);

  //: 写入口收口在这里 —— 值一律过 `sanitizeAccountId`，
  //: 页面传 `NaN` / 空串 / 函数式更新都不会把坏值落到缓存里。
  const select = useCallback<Dispatch<SetStateAction<number | null>>>((next) => {
    setValue((prev) => sanitizeAccountId(typeof next === "function" ? next(prev) : next));
  }, []);
  return [value, select];
}

