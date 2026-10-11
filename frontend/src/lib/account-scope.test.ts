/**
 * 「当前账号范围」的**迁移规则**单测（设计 32 §5.4；A5）。
 *
 * ⚠️ 为什么这批用例是**承重**的：迁移规则最容易出的错是「老用户选好的账号被
 * 静默重置」—— 界面上不报错、不红，只是账号选择不见了。
 * 把规则写在组件里就等于没有测试（本项目 vitest 只覆盖纯逻辑模块）；
 * 所以规则落在 `lib/account-scope.ts`，由这里的用例直接驱动。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ACCOUNT_SCOPE_KEYS, LEGACY_ACCOUNT_KEYS, readAccountScope, sanitizeAccountId, writeAccountScope } from "./account-scope";
import { readFilterCache } from "./filter-cache";

/** 一个最小的假 localStorage（`filter-cache` 只用到这三个方法）。 */
function installFakeStorage() {
  const map = new Map<string, string>();
  const store = {
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
  vi.stubGlobal("window", { localStorage: store });
  return map;
}

describe("账号范围 · sanitizeAccountId", () => {
  it("只接受正数（数字或数字串），其余一律当未选", () => {
    expect(sanitizeAccountId(8)).toBe(8);
    expect(sanitizeAccountId("9")).toBe(9);
    expect(sanitizeAccountId(8.7)).toBe(8);
    // 下面这些都必须落到 null —— 落到 0 或 NaN 会让「按 id 取账号」静默取到错的行。
    expect(sanitizeAccountId(0)).toBeNull();
    expect(sanitizeAccountId(-1)).toBeNull();
    expect(sanitizeAccountId("")).toBeNull();
    expect(sanitizeAccountId("  ")).toBeNull();
    expect(sanitizeAccountId("abc")).toBeNull();
    expect(sanitizeAccountId(null)).toBeNull();
    expect(sanitizeAccountId(undefined)).toBeNull();
    expect(sanitizeAccountId(Number.NaN)).toBeNull();
  });
});

describe("账号范围 · 旧键一次性迁移", () => {
  let map: Map<string, string>;

  beforeEach(() => {
    map = installFakeStorage();
  });

  it("读不到新键时，从旧键（页面级）迁移并**清掉旧键**", () => {
    map.set(LEGACY_ACCOUNT_KEYS.content[0], JSON.stringify(11));

    expect(readAccountScope("content")).toBe(11);
    // 新键已经写上（跨页共享从这里开始）。
    expect(readFilterCache(ACCOUNT_SCOPE_KEYS.content)).toBe(11);
    // 旧键已清 —— 否则用户在新键上改过之后，某次新键缺失又会被旧值覆盖回来。
    expect(map.has(LEGACY_ACCOUNT_KEYS.content[0])).toBe(false);
  });

  it("新键已有值时不看旧键（新键是唯一真相源）", () => {
    map.set(ACCOUNT_SCOPE_KEYS.content, JSON.stringify(8));
    map.set(LEGACY_ACCOUNT_KEYS.content[0], JSON.stringify(11));

    expect(readAccountScope("content")).toBe(8);
    // 旧键**不动**：这次没走迁移路径，替用户清掉他别的页面还可能读的键是越权。
    expect(map.has(LEGACY_ACCOUNT_KEYS.content[0])).toBe(true);
  });

  it("同一业务线多个旧键：按登记顺序取，能解析的胜出", () => {
    // 内容线有两个旧键（内容库 / 我的笔记）；第一个是坏值时应当继续往下找。
    map.set(LEGACY_ACCOUNT_KEYS.content[0], JSON.stringify("not-an-id"));
    map.set(LEGACY_ACCOUNT_KEYS.content[1], JSON.stringify(10));

    expect(readAccountScope("content")).toBe(10);
  });

  it("三条业务线**互不干扰**（每线一份，不是全局一份）", () => {
    map.set(ACCOUNT_SCOPE_KEYS.content, JSON.stringify(8));
    map.set(ACCOUNT_SCOPE_KEYS.shop, JSON.stringify(9));

    expect(readAccountScope("content")).toBe(8);
    expect(readAccountScope("shop")).toBe(9);
    // 「平台」线没有旧键、也没写过 ⇒ 未选（不兜底成别的线的账号）。
    expect(readAccountScope("shared")).toBeNull();
  });

  it("写 null = 清空该线的选择（不是写一个 0）", () => {
    writeAccountScope("shop", 9);
    expect(readAccountScope("shop")).toBe(9);

    writeAccountScope("shop", null);
    expect(readAccountScope("shop")).toBeNull();
  });

  it("坏缓存不炸页面（非法 JSON 当没存过）", () => {
    map.set(ACCOUNT_SCOPE_KEYS.shop, "{not json");
    expect(readAccountScope("shop")).toBeNull();
  });

  it("「平台」线**没有**旧键可迁（不是忘了登记）", () => {
    expect(LEGACY_ACCOUNT_KEYS.shared).toEqual([]);
    // 三条线都登记了键（漏一条会让那条线的选择永远读不回来）。
    expect(Object.keys(ACCOUNT_SCOPE_KEYS).sort()).toEqual(["content", "shared", "shop"]);
    expect(Object.keys(LEGACY_ACCOUNT_KEYS).sort()).toEqual(["content", "shared", "shop"]);
  });
});
