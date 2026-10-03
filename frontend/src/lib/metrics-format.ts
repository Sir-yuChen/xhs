/**
 * 指标仪表盘的**纯逻辑**（设计 17 §13.10，M2 切片 3，2026-10-04）。
 *
 * ## 为什么单独成文件
 *
 * 三条判据（**不得补零** / **价格换元且保留区间语义** / **空态判据**）都是
 * 「看一眼觉得对」的类型，**必须能被测试钉住**。若写进 TSX 组件，就只能靠渲染断言
 * （慢、脆、且要跑 DOM）；下沉成纯函数后 esbuild/vitest 直接跑（照 `lib/task-records.ts`
 * 与 `gateway-tree.ts` 的既有范式）。
 *
 * ⚠️ **本文件不 import 任何 React / antd**（保持可在 `environment: node` 下测）。
 */

/** 笔记趋势点（与 `lib/api.ts` 的 `NoteMetricsTrendPoint` 逐字段一致）。 */
export type NoteTrendPoint = {
  stat_date: string;
  likes: number;
  collects: number;
  comments: number;
  shares: number;
  views: number;
  engagement: number;
};

/** 商品趋势点（价格是**区间**，`*_fen` 单位是**分**）。 */
export type ShopTrendPoint = {
  stat_date: string;
  stock: number;
  sales: number;
  sales_30d: number;
  price_min_fen: number;
  price_max_fen: number;
  cart_count: number;
};

/**
 * 分 → 元（展示用）。
 *
 * ⚠️ **只做展示换算，不改数据口径**：库里/接口里一律是**整数分**（浮点金额会丢精度），
 * 只在渲染前除以 100。
 *
 * `undefined` / `null` / 非有限值 ⇒ `"-"`（缺值口径：**不把缺值渲染成 0**）。
 */
export function fenToYuan(fen: number | null | undefined): string {
  if (fen === null || fen === undefined || !Number.isFinite(fen)) {
    return "-";
  }
  const yuan = fen / 100;
  // 两位小数（价格语义）；整数元也补 `.00`，保持列对齐。
  return yuan.toFixed(2);
}

/**
 * 价格**区间**文案（⚠️ **不是求和**）。
 *
 * 后端 `trend_series` 给的是「该日全部商品的 `min(price_min_fen)` ~ `max(price_max_fen)`」——
 * 这是**区间最值**：两件 100 元的商品加起来不是「200 元的价格」，求和没有业务含义。
 *
 * - 两端都缺 ⇒ `"-"`
 * - 同值 ⇒ 单值（`"9.90"`，不写成 `"9.90 ~ 9.90"`）
 * - 否则 `"低 ~ 高"`
 */
export function priceRangeText(
  priceMinFen: number | null | undefined,
  priceMaxFen: number | null | undefined,
): string {
  const low = fenToYuan(priceMinFen);
  const high = fenToYuan(priceMaxFen);
  if (low === "-" && high === "-") {
    return "-";
  }
  if (low === "-") {
    return high;
  }
  if (high === "-" || low === high) {
    return low;
  }
  return `${low} ~ ${high}`;
}

/**
 * 「该不该画空态」的唯一判据。
 *
 * ⚠️ **不许把空数组喂给 recharts**：那会画出一条贴 0 轴的基线，视觉上等价于
 * 「数据是 0」——而真相往往是「那天没采」（`trend` 端点**只返回有快照的天**）。
 * 把「没采」画成「掉到 0」是伪造数据（硬约束⑤）。
 */
export function isEmptySeries(series: readonly unknown[] | null | undefined): boolean {
  return !series || series.length === 0;
}

/**
 * 笔记趋势 → 折线图数据（**原样透传，不补零**）。
 *
 * ⚠️ **本函数刻意不做任何日期补齐**。`trend` 只返回有快照的天；
 * 若在这里补零，「没采的那天」会被画成「互动掉到 0」。
 * 缺失的天在图上表现为**点与点之间跨度更大**（这才是诚实的呈现）。
 */
export function toNoteChartData(series: readonly NoteTrendPoint[]) {
  return series.map((point) => ({
    stat_date: point.stat_date,
    阅读: point.views,
    点赞: point.likes,
    收藏: point.collects,
    评论: point.comments,
    转发: point.shares,
    互动: point.engagement,
  }));
}

/**
 * 商品趋势 → 折线图数据（销量/库存；**价格换元**）。
 *
 * ⚠️ 价格的「区间最值」语义在图表里用两条线表达（下界/上界），**不做求和**。
 */
export function toShopChartData(series: readonly ShopTrendPoint[]) {
  return series.map((point) => ({
    stat_date: point.stat_date,
    库存: point.stock,
    销量: point.sales,
    近30天销量: point.sales_30d,
    价格下界: point.price_min_fen / 100,
    价格上界: point.price_max_fen / 100,
    加购: point.cart_count,
  }));
}

/** 互动构成（饼图）—— 四项之和必须等于后端给的 `engagement`。 */
export function engagementPieData(
  totals: { likes: number; collects: number; comments: number; shares: number },
) {
  return [
    { name: "点赞", value: totals.likes },
    { name: "收藏", value: totals.collects },
    { name: "评论", value: totals.comments },
    { name: "转发", value: totals.shares },
  ];
}

/** 把趋势序列的**最后一天**当作「最新值」，用于总览卡（无数据 ⇒ 全 0 + 由空态承担说明）。 */
export function latestNoteTotals(series: readonly NoteTrendPoint[]) {
  const last = series.length > 0 ? series[series.length - 1] : null;
  return {
    stat_date: last?.stat_date ?? "",
    views: last?.views ?? 0,
    likes: last?.likes ?? 0,
    collects: last?.collects ?? 0,
    comments: last?.comments ?? 0,
    shares: last?.shares ?? 0,
    engagement: last?.engagement ?? 0,
  };
}

/** 商品侧「最新值」同上。 */
export function latestShopTotals(series: readonly ShopTrendPoint[]) {
  const last = series.length > 0 ? series[series.length - 1] : null;
  return {
    stat_date: last?.stat_date ?? "",
    stock: last?.stock ?? 0,
    sales: last?.sales ?? 0,
    sales_30d: last?.sales_30d ?? 0,
    price_min_fen: last?.price_min_fen ?? 0,
    price_max_fen: last?.price_max_fen ?? 0,
    cart_count: last?.cart_count ?? 0,
  };
}

/** 有快照的天数（**不是**「查询窗口的天数」—— 两者不同，别混）。 */
export function snapshotDayCount(series: readonly { stat_date: string }[]): number {
  return new Set(series.map((row) => row.stat_date)).size;
}
