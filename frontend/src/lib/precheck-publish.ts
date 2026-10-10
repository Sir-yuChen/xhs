/**
 * 预检产物 → 发布体载荷的**纯映射**（设计 25 §6）。
 *
 * 单独成模块的理由：这一步全是「哪列是什么」的业务判断，塞在 500 行的
 * 弹窗组件里既没法单测，改一列映射就得开浏览器点一遍。抽出来能 vitest 直测。
 *
 * ## ⚠️ 刻意**不做**的事：SKU 图与规格名对齐
 *
 * 用户 2026-10-08 口径：「**不要将 SKU 图和规格对齐**，因为图片名称和 SKU
 * 名称可能对应不上，没有固定规则」。
 *
 * 之前这里按文件名基名配对（`SKU01_颜色分类(【中古风】实木三斗柜).jpg`
 * ↔ `颜色分类:【中古风】实木三斗柜`），看着能对上，但那是**这一份数据包的
 * 巧合**，不是规则：换个包序号前缀、括号形态、命名习惯全变。**静默配错图
 * 比不配更糟** —— 用户会以为图对了，实际发布出去是错的。所以这里一律
 * `image_path: null`，让用户在发布页自己挑。
 *
 * 另外注意真包 `图片` 列装的是**外部 URL**（1688 的），不是数据包里的
 * 相对路径，更不能当本地路径塞给后端。
 */

/** 一条预检媒体（与 `api.ts` 的 `PrecheckMediaItem` 对齐，只取这里用得到的）。 */
export type PrecheckMedia = {
  group: string;
  index: number;
  rel_path: string;
  file_name: string;
  ocr_ok?: boolean | null;
  has_forbidden?: boolean;
};

/** 一张表（表头名 = 工作表名，不带后缀；`rows` 是原始文本）。 */
export type PrecheckTable = {
  title: string;
  sheet_name: string;
  columns: string[];
  rows: (string | null)[][];
  /** 来源 xlsx 文件名（表格顶部展示）。 */
  source_file?: string;
  /** 「商品属性图」OCR 的 KV 对；有值 ⇒ 属性表（不进「商品sku表格」页签）。 */
  kv?: { key: string; value: string }[] | null;
};

export type PublishSkuDraft = {
  name: string;
  price_yuan: number | null;
  stock: number | null;
  image_path: null;
};

/* ------------------------------------------------------------------ *
 * 表格·规格列的**批量修改**（用户口径 2026-10-09）
 *
 * 「规格的价格，和库存均支持批量修改，原参数基础上加减去固定值或
 *   原参数值基础倍数+固定值，或直接统一修改成固定值」
 *
 * 三种模式（`BatchEditMode`）：
 *   - `delta`  ：原值 + 固定值（固定值可负 ⇒ 就是「加减」）
 *   - `scale`  ：原值 × 倍数 + 固定值
 *   - `fixed`  ：一律改成固定值（不看原值）
 *
 * ⚠️ 纯函数、不碰 DOM：批次一改就是几十行，必须能 vitest 直测（见
 * `precheck-publish.test.ts`）。
 * ------------------------------------------------------------------ */

export type BatchEditMode = "delta" | "scale" | "fixed";

export type BatchEditRule = {
  mode: BatchEditMode;
  /** `delta` 的加数（可负）/ `scale` 的固定加数 / `fixed` 的目标值。 */
  operand: number;
  /** 仅 `scale` 用：倍数（默认 1）。 */
  factor?: number;
  /** 结果下限（价格 ≥ 0；缺省 0）。 */
  min?: number;
  /** 结果上限（库存常给 99999）；缺省无上限。 */
  max?: number;
  /**
   * 只改**有值**的单元格（缺省 true）。
   *
   * ⚠️ 缺值口径：原值为空（`null`）**不参与**批量，保持空 ——
   * 「假数据比没有数据更糟」（设计 27 §实现口径）。置 false 才会给空值也套公式。
   */
  onlyFilled?: boolean;
};

/** 单个值按规则算一次（**不落库、不改原值**）。 */
export function applyBatchRule(
  value: number | null,
  rule: BatchEditRule,
): number | null {
  if (value === null || !Number.isFinite(value)) {
    //: 空值：默认跳过（保持空）；显式 `onlyFilled: false` 时按 0 参与。
    if (rule.onlyFilled !== false) return null;
  }
  const base = value === null || !Number.isFinite(value) ? 0 : value;
  let next: number;
  switch (rule.mode) {
    case "delta":
      next = base + (rule.operand || 0);
      break;
    case "scale":
      next = base * (rule.factor ?? 1) + (rule.operand || 0);
      break;
    case "fixed":
      next = rule.operand || 0;
      break;
    default:
      next = base;
  }
  const min = rule.min ?? 0;
  const max = rule.max ?? Number.POSITIVE_INFINITY;
  if (!Number.isFinite(next)) return value;
  return Math.min(max, Math.max(min, next));
}

/** 一条 SKU 行按规则重算价格 / 库存（`price` / `stock` 任一为 null ⇒ 该列不动）。 */
export function applyBatchRuleToSku(
  sku: PublishSkuDraft,
  rules: { price?: BatchEditRule; stock?: BatchEditRule },
): PublishSkuDraft {
  return {
    ...sku,
    price_yuan: rules.price ? applyBatchRule(sku.price_yuan, rules.price) : sku.price_yuan,
    stock: rules.stock ? applyBatchRule(sku.stock, rules.stock) : sku.stock,
  };
}

/**
 * 表格里的**列名 → 下标**（价格 / 库存批量修改要用它定位单元格）。
 *
 * 与 `tableToSkus` 用**同一套别名**，否则「批量改的列」和「读出的列」
 * 不是同一列 —— 那会改了个寂寞（静默失效）。
 */
export const PRICE_COLUMN_ALIASES = ["原价", "价格", "单价", "price"] as const;
export const STOCK_COLUMN_ALIASES = ["库存", "stock", "数量"] as const;

/** 找价格列下标（找不到 ⇒ -1）。 */
export function priceColumnIndex(columns: string[]): number {
  return columnIndex(columns, [...PRICE_COLUMN_ALIASES]);
}

/** 找库存列下标（找不到 ⇒ -1）。 */
export function stockColumnIndex(columns: string[]): number {
  return columnIndex(columns, [...STOCK_COLUMN_ALIASES]);
}

/** 按规则批量改一张表的某一列（原地不改，返回新表）。
 *
 * `onlyRows` 给定行下标集合 ⇒ **只改这些行**（用户口径 2026-10-09：
 * 「规格列表加一个复选框支持选中的再批量修改」）；
 * 省略/为空数组 ⇒ 整列都改。
 */
export function applyBatchRuleToTable(
  table: PrecheckTable,
  column: number,
  rule: BatchEditRule,
  onlyRows?: readonly number[],
): PrecheckTable {
  if (column < 0) return table;
  const scope = onlyRows && onlyRows.length > 0 ? new Set(onlyRows) : null;
  return {
    ...table,
    rows: (table.rows ?? []).map((row, rowIndex) => {
      //: 指定了选中行 ⇒ 范围外的行原样不动（一个字符都不碰）。
      if (scope && !scope.has(rowIndex)) return row;
      return row.map((cell, ci) => {
        if (ci !== column) return cell;
        const raw = parseYuan(cell);
        const next = applyBatchRule(raw, rule);
        //: 空值跳过 ⇒ 保持原样（不写 `"0"`，不造假）。
        if (next === null) return cell;
        //: 价格保留两位小数、整数不补 `.00`（与平台展示口径一致）。
        return Number.isInteger(next) ? String(next) : next.toFixed(2);
      });
    }),
  };
}

/** 统计某列**有值**的行数（确认文案用；可选只在给定行里数）。 */
export function countFilledInColumn(
  table: PrecheckTable,
  column: number,
  onlyRows?: readonly number[],
): number {
  if (column < 0) return 0;
  const scope = onlyRows && onlyRows.length > 0 ? new Set(onlyRows) : null;
  return (table.rows ?? []).filter(
    (row, rowIndex) =>
      (!scope || scope.has(rowIndex)) && parseYuan(row[column]) !== null,
  ).length;
}

/** 只替换数组里**指定下标**的元素（其余引用原样返回）。
 *
 * ⚠️ 用途：页面把 `tables.filter(...)` 的**子集**交给面板，面板改完必须映射回
 * **完整数组**再上抛，否则被过滤掉的元素会丢（用户实测：第二次改库存无响应 ——
 * 属性表被 drop ⇒ 页签重建 ⇒ 状态被重置）。
 */
export function replaceAt<T>(list: T[], index: number, next: T): T[] {
  return list.map((item, i) => (i === index ? next : item));
}

/** SKU 表（非属性表）在**完整 `tables`** 里的下标；属性表（有 `kv`）被排除。 */
export function skuTableIndices(tables: PrecheckTable[]): number[] {
  return tables.map((t, i) => (Array.isArray(t.kv) ? -1 : i)).filter((i) => i >= 0);
}

/** 把面板（只显示 SKU 表）改后的第 `panelIndex` 张表嵌回**完整数组**。
 *
 * ⚠️ 这是「第二次改库存无响应」的修复点：面板只拿到 SKU 子集，若直接上抛子集，
 * 属性表（kv）会被丢掉 ⇒ 页签集合变化 ⇒ 面板重挂载、`operand` 归零 ⇒ 按钮回到禁用。
 */
export function mergePanelTable(
  full: PrecheckTable[],
  panelIndex: number,
  next: PrecheckTable,
): PrecheckTable[] {
  const real = skuTableIndices(full)[panelIndex];
  if (real === undefined) return full;
  return replaceAt(full, real, next);
}

/** 区域 → 发布体里的去处。`main` 单列，其余合并进详情图。 */
export function splitByGroup(media: PrecheckMedia[]): {
  mainImages: string[];
  detailImages: string[];
  skuImages: string[];
} {
  const mainImages: string[] = [];
  const detailImages: string[] = [];
  const skuImages: string[] = [];
  for (const item of media) {
    if (item.group === "main") mainImages.push(item.rel_path);
    else if (item.group === "sku") skuImages.push(item.rel_path);
    else if (item.group === "detail") detailImages.push(item.rel_path);
    // 视频不进发布体 images（真机抓包：images/imageDescriptions 都是图片对象）
  }
  return { mainImages, detailImages, skuImages };
}

/** 从一张表里找列下标（大小写无关，支持别名）。 */
function columnIndex(columns: string[], aliases: string[]): number {
  const lowered = columns.map((c) => String(c ?? "").replace(/\s+/g, "").toLowerCase());
  for (const alias of aliases) {
    const hit = lowered.findIndex((c) => c === alias);
    if (hit >= 0) return hit;
  }
  return -1;
}

/** 文本 → 元；`1280` / `1280.50` / `¥1280` / 空 ⇒ null（不造假）。 */
export function parseYuan(raw: string | number | null | undefined): number | null {
  if (raw === null || raw === undefined || raw === "") return null;
  if (typeof raw === "number") return Number.isFinite(raw) ? raw : null;
  const cleaned = String(raw).replace(/[^\d.-]/g, "");
  if (!cleaned || cleaned === "-" || cleaned === ".") return null;
  const value = Number(cleaned);
  return Number.isFinite(value) ? value : null;
}

/** 文本 → 整数库存；非法 ⇒ 0（前端渲染约定，缺值不当假数据）。 */
export function parseStock(raw: string | number | null | undefined): number | null {
  const value = parseYuan(raw);
  return value === null ? null : Math.max(0, Math.trunc(value));
}

/**
 * 预检表格 → 发布体 SKU 行。
 *
 * 列名按**别名**认（不同数据包列名会变）：`sku名称` / `颜色分类` / `规格名`…
 * `原价` / `价格`；`库存` / `stock`。
 *
 * ⚠️ **价格优先取「原价」**：`计算价格` 在真包里是**空列**（公式列没算出来），
 * 取它会把所有 SKU 变成「待补」。原价缺失才回退到计算价格。
 *
 * ⚠️ `image_path` **恒为 null** —— 见文件头「刻意不做的事」。
 */
export function tableToSkus(table: PrecheckTable | null | undefined): PublishSkuDraft[] {
  if (!table || !table.columns?.length) return [];
  const nameCol = columnIndex(table.columns, ["sku名称", "颜色分类", "规格名", "skuname", "name"]);
  const priceCol = columnIndex(table.columns, ["原价", "价格", "单价", "price"]);
  const calcPriceCol = columnIndex(table.columns, ["计算价格", "计算价", "calcprice"]);
  const stockCol = columnIndex(table.columns, ["库存", "stock", "数量"]);
  if (nameCol < 0) return [];

  const rows = table.rows ?? [];

  return rows.map((row) => {
    const name = String(row[nameCol] ?? "").trim();
    const price = parseYuan(priceCol >= 0 ? row[priceCol] : null)
      ?? parseYuan(calcPriceCol >= 0 ? row[calcPriceCol] : null);
    return {
      name,
      price_yuan: price,
      stock: parseStock(stockCol >= 0 ? row[stockCol] : null),
      //: 恒 null：图片名与 SKU 名没有固定对应关系（用户 2026-10-08 口径）。
      image_path: null,
    };
  }).filter((sku) => sku.name !== "");
}

/**
 * 预检产物 → `publishShopItem` 的载荷片段。
 *
 * 只在**有预检产物**时用；没有时返回 `null`，调用方走旧的草稿发布路径
 * （两条路径并存，不互相干扰）。
 */
export function buildPublishPayloadFromPrecheck(
  precheck: { token: string; media: PrecheckMedia[]; tables: PrecheckTable[] } | null | undefined,
): {
  precheck_token: string;
  main_images: string[];
  detail_images: string[];
  skus: PublishSkuDraft[];
} | null {
  if (!precheck?.token) return null;
  const { mainImages, detailImages } = splitByGroup(precheck.media ?? []);
  const table = (precheck.tables ?? [])[0];
  const skus = tableToSkus(table);
  return {
    precheck_token: precheck.token,
    main_images: mainImages,
    detail_images: detailImages,
    skus,
  };
}