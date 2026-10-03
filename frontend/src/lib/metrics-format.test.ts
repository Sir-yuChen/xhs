/**
 * 指标仪表盘纯逻辑的用例（设计 17 §13.10，M2 切片 3）。
 *
 * 三条判据各有守卫，且都能被变异抓住：
 * 1. **不得补零** —— 空序列判空、折线数据原样透传（不补天）；
 * 2. **价格换元且保留区间语义** —— 除以 100、单值不写区间、缺值不渲染成 0；
 * 3. **互动构成**四项。
 */

import { describe, expect, it } from "vitest";

import {
  engagementPieData,
  fenToYuan,
  isEmptySeries,
  latestNoteTotals,
  latestShopTotals,
  priceRangeText,
  snapshotDayCount,
  toNoteChartData,
  toShopChartData,
  type NoteTrendPoint,
  type ShopTrendPoint,
} from "./metrics-format";

function notePoint(overrides: Partial<NoteTrendPoint> = {}): NoteTrendPoint {
  return {
    stat_date: "2026-10-04",
    likes: 0,
    collects: 0,
    comments: 0,
    shares: 0,
    views: 0,
    engagement: 0,
    ...overrides,
  };
}

function shopPoint(overrides: Partial<ShopTrendPoint> = {}): ShopTrendPoint {
  return {
    stat_date: "2026-10-04",
    stock: 0,
    sales: 0,
    sales_30d: 0,
    price_min_fen: 0,
    price_max_fen: 0,
    cart_count: 0,
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// ① 分 → 元
// --------------------------------------------------------------------------- //
describe("fenToYuan", () => {
  it("分转元保留两位小数", () => {
    expect(fenToYuan(10680)).toBe("106.80");
    expect(fenToYuan(932)).toBe("9.32");
    expect(fenToYuan(0)).toBe("0.00");
  });

  it("缺值渲染 '-' 而不是 0（缺值口径）", () => {
    // ⚠️ 变异守护：把缺值改成 return "0.00" ⇒ 本用例必红。
    // 把「没采到」渲染成「价格是 0」是伪造数据。
    expect(fenToYuan(null)).toBe("-");
    expect(fenToYuan(undefined)).toBe("-");
    expect(fenToYuan(Number.NaN)).toBe("-");
    expect(fenToYuan(Number.POSITIVE_INFINITY)).toBe("-");
  });
});

// --------------------------------------------------------------------------- //
// ② 价格区间（⚠️ 不是求和）
// --------------------------------------------------------------------------- //
describe("priceRangeText", () => {
  it("两端不同 ⇒ 区间文案", () => {
    expect(priceRangeText(932, 1260400)).toBe("9.32 ~ 12604.00");
  });

  it("两端相同 ⇒ 单值（不写 '9.90 ~ 9.90'）", () => {
    // ⚠️ 变异守护：去掉 `low === high` 分支 ⇒ 本用例必红。
    expect(priceRangeText(990, 990)).toBe("9.90");
  });

  it("两端都缺 ⇒ '-'", () => {
    expect(priceRangeText(null, null)).toBe("-");
    expect(priceRangeText(undefined, undefined)).toBe("-");
  });

  it("只有一端 ⇒ 那个值（不编另一端）", () => {
    expect(priceRangeText(null, 500)).toBe("5.00");
    expect(priceRangeText(500, null)).toBe("5.00");
  });

  it("绝不求和（两件 100 元 ≠ 200 元）", () => {
    // ⚠️ 变异守护：把实现改成 `(min+max)/100` ⇒ 本用例必红。
    // 价格求和没有业务含义 —— 后端给的就是区间最值。
    expect(priceRangeText(10000, 30000)).toBe("100.00 ~ 300.00");
    expect(priceRangeText(10000, 30000)).not.toBe("400.00");
  });
});

// --------------------------------------------------------------------------- //
// ③ 空态判据（⚠️ 不得补零）
// --------------------------------------------------------------------------- //
describe("isEmptySeries", () => {
  it("空 / null / undefined ⇒ 真空态", () => {
    expect(isEmptySeries([])).toBe(true);
    expect(isEmptySeries(null)).toBe(true);
    expect(isEmptySeries(undefined)).toBe(true);
  });

  it("有一条就算非空（**不要求天数够**）", () => {
    expect(isEmptySeries([notePoint()])).toBe(false);
  });
});

describe("toNoteChartData", () => {
  it("原样透传、**不补零天**", () => {
    // ⚠️ 变异守护（本切片最核心）：在实现里补日期（把 10-03 / 10-04 之间插一个 10-04 之外的
    // 空缺天并填 0）⇒ 本用例的 `length` 断言必红。
    // 「没采的那天」用 0 填充 = 把「没采」画成「互动掉到 0」（伪造数据，硬约束⑤）。
    const series = [
      notePoint({ stat_date: "2026-10-01", views: 100, likes: 5, engagement: 9 }),
      notePoint({ stat_date: "2026-10-04", views: 200, likes: 8, engagement: 15 }),
    ];
    const data = toNoteChartData(series);
    expect(data).toHaveLength(2); // ⚠️ **不因为跨了 3 天就补成 4 条**
    expect(data[0].stat_date).toBe("2026-10-01");
    expect(data[1].stat_date).toBe("2026-10-04");
  });

  it("字段名映射到中文图例", () => {
    const data = toNoteChartData([
      notePoint({ views: 1, likes: 2, collects: 3, comments: 4, shares: 5, engagement: 14 }),
    ]);
    expect(data[0]).toMatchObject({
      阅读: 1,
      点赞: 2,
      收藏: 3,
      评论: 4,
      转发: 5,
      互动: 14,
    });
  });

  it("空序列 ⇒ 空数组（交给空态，不喂给 recharts）", () => {
    expect(toNoteChartData([])).toEqual([]);
  });
});

describe("toShopChartData", () => {
  it("价格换元（÷100），且上下界各成一条线（不是求和）", () => {
    // ⚠️ 变异守护：去掉 `/100`（直接传分）⇒ 本用例必红（图上价格会差 100 倍）。
    const data = toShopChartData([
      shopPoint({ price_min_fen: 932, price_max_fen: 1260400, stock: 10, sales: 2 }),
    ]);
    expect(data[0].价格下界).toBeCloseTo(9.32);
    expect(data[0].价格上界).toBeCloseTo(12604);
    expect(data[0].库存).toBe(10);
    expect(data[0].销量).toBe(2);
  });

  it("空序列 ⇒ 空数组", () => {
    expect(toShopChartData([])).toEqual([]);
  });
});

// --------------------------------------------------------------------------- //
// ④ 互动构成 / 最新值 / 天数
// --------------------------------------------------------------------------- //
describe("engagementPieData", () => {
  it("四项且顺序固定（环比色一致）", () => {
    const pie = engagementPieData({ likes: 1, collects: 2, comments: 3, shares: 4 });
    expect(pie.map((row) => row.name)).toEqual(["点赞", "收藏", "评论", "转发"]);
    expect(pie.reduce((sum, row) => sum + row.value, 0)).toBe(10);
  });
});

describe("latestNoteTotals", () => {
  it("取最后一天（序列已按日期升序）", () => {
    const totals = latestNoteTotals([
      notePoint({ stat_date: "2026-10-03", views: 1 }),
      notePoint({ stat_date: "2026-10-04", views: 9 }),
    ]);
    expect(totals.stat_date).toBe("2026-10-04");
    expect(totals.views).toBe(9);
  });

  it("空序列 ⇒ 0 + 空日期（由空态承担说明，不在数值上造假）", () => {
    const totals = latestNoteTotals([]);
    expect(totals.stat_date).toBe("");
    expect(totals.views).toBe(0);
  });
});

describe("latestShopTotals", () => {
  it("取最后一天并保留分单位（换算交给展示层）", () => {
    const totals = latestShopTotals([
      shopPoint({ price_min_fen: 932, price_max_fen: 1260400, stock: 7 }),
    ]);
    expect(totals.stock).toBe(7);
    expect(totals.price_min_fen).toBe(932);
  });

  it("空序列 ⇒ 0", () => {
    expect(latestShopTotals([]).stock).toBe(0);
  });
});

describe("snapshotDayCount", () => {
  it("数**有快照的天**，与查询窗口无关", () => {
    // 查询 30 天但只有 2 天有数据 ⇒ 必须报 2（不能报 30）。
    // ⚠️ 变异守护：改成 `series.length`（点数）在「同一天多行」时会错；
    // 这里用同一天两行验证去重语义。
    expect(
      snapshotDayCount([{ stat_date: "2026-10-04" }, { stat_date: "2026-10-04" }]),
    ).toBe(1);
    expect(
      snapshotDayCount([{ stat_date: "2026-10-03" }, { stat_date: "2026-10-04" }]),
    ).toBe(2);
    expect(snapshotDayCount([])).toBe(0);
  });
});
