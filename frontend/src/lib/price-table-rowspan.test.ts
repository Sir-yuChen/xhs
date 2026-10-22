/** 价格及库存表的「主-总-分」合并判据（用户口径 2026-10-21）。
 *
 *  > 「参考这个商品重新设计多规格展示价格及库存表，
 *  >   第一个商品规格为**主-总-分**的结构表格」
 *
 *  真机（CDP 实读 `6a55be4d…`，设计 24 §23）：
 *   颜色分类 | 适用床尺寸 | 行号 | 规格图 | 售价* | 当前库存 | 改后库存 | 市场价 | 规格ID | 条形码 | 商家编码 | 操作
 *  「小熊【防尘款】」组 `rowspan=8` —— **第一维度按值合并**，其余维度逐行展开，
 *  规格图**一组一张**（只在组首行）。
 *
 *  ⚠️ 这里跑真实组合（24 色 × 若干尺寸），断言：
 *  ① 每个第一维度值**只出现一次**（rowSpan>0），其余行 rowSpan=0；
 *  ② 每组的 rowSpan 之和 == 总行数（漏算会让表格塌一行）；
 *  ③ 规格图只在组首行渲染。
 */
import { describe, expect, it } from "vitest";

type Row = { spec: string };

/** 从 `spec`（`维度名:值 / 维度名:值`）取某维度的值 —— 与页面 `dimOf` 同构。 */
function dimOf(spec: string, dim: string): string {
  for (const part of String(spec ?? "").split(" / ")) {
    const at = part.indexOf(":");
    if (at >= 0 && part.slice(0, at).trim() === dim) return part.slice(at + 1).trim();
  }
  return "";
}

/** 计算第一维度的 `rowSpan` 计划 —— 与页面 `onCell` 逐行对应。 */
function rowSpanPlan(rows: Row[], dim: string): number[] {
  const spans = new Array(rows.length).fill(0);
  let i = 0;
  while (i < rows.length) {
    const v = dimOf(rows[i].spec, dim);
    let span = 1;
    while (i + span < rows.length && dimOf(rows[i + span].spec, dim) === v) span += 1;
    spans[i] = span;
    i += span;
  }
  return spans;
}

/** 造 24 色 × 19 尺寸的真实组合（与数据包同形）。 */
function makeRows(): Row[] {
  const rows: Row[] = [];
  for (let c = 1; c <= 24; c += 1) {
    for (let s = 1; s <= 19; s += 1) {
      rows.push({ spec: `颜色分类:奇遇童话${String(c).padStart(2, "0")} / 款式:尺寸${s}` });
    }
  }
  return rows;
}

describe("价格库存表：主-总-分（第一维度合并）", () => {
  it("① 每个颜色组只出现一次，其余行 rowSpan=0", () => {
    const rows = makeRows();
    const spans = rowSpanPlan(rows, "颜色分类");
    const leaders = spans.map((s, i) => (s > 0 ? i : -1)).filter((i) => i >= 0);
    //: 24 个颜色 ⇒ 正好 24 个组首。
    expect(leaders.length).toBe(24);
    //: 每个组首的 span 都是 19（19 个尺寸）。
    for (const i of leaders) expect(spans[i]).toBe(19);
  });

  it("② rowSpan 之和 == 总行数（漏算会塌一行）", () => {
    const rows = makeRows();
    const spans = rowSpanPlan(rows, "颜色分类");
    expect(spans.reduce((a, b) => a + b, 0)).toBe(rows.length);
    expect(rows.length).toBe(456);
  });

  it("③ 规格图只在组首行：非组首的行为 null", () => {
    const rows = makeRows();
    const spans = rowSpanPlan(rows, "颜色分类");
    //: 页面判据：上一行同值 ⇒ 不画图。
    const shouldRender = rows.map(
      (r, i) => i === 0 || dimOf(rows[i - 1].spec, "颜色分类") !== dimOf(r.spec, "颜色分类"),
    );
    const leaders = spans.map((s, i) => (s > 0 ? i : -1)).filter((i) => i >= 0);
    expect(shouldRender.filter(Boolean).length).toBe(24);
    expect(shouldRender.map((b, i) => (b ? i : -1)).filter((i) => i >= 0)).toEqual(
      leaders,
    );
  });

  it("④ 只有一维时整表合成一格（rowSpan = 全集）", () => {
    const rows = [
      { spec: "颜色分类:甲" },
      { spec: "颜色分类:甲" },
      { spec: "颜色分类:甲" },
    ];
    expect(rowSpanPlan(rows, "颜色分类")).toEqual([3, 0, 0]);
  });

  it("⑤ 组顺序被打乱也要按**相邻**分组（不按值全局聚）", () => {
    //: ⚠️ 页面按「相邻同值」合并 —— 值不连续时会分成多组（与平台渲染一致）。
    const rows = [
      { spec: "颜色分类:甲" },
      { spec: "颜色分类:乙" },
      { spec: "颜色分类:甲" },
    ];
    expect(rowSpanPlan(rows, "颜色分类")).toEqual([1, 1, 1]);
  });
});
