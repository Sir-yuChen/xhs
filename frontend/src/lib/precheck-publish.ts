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
  /** ⚠️ **区域名 = 数据包一级目录名原值**（不是 `main`/`sku` 四值之一）。 */
  group: string;
  /** 区域**参考类别** —— 只作展示提示，**不参与过滤**（用户口径 2026-10-13）。 */
  kind?: "main" | "video" | "sku" | "detail" | "other";
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
  const clamped = Math.min(max, Math.max(min, next));
  //: ⚠️ 用户口径（2026-10-11）：「列值最终计算**保留两位小数四舍五入**」。
  //: 不加这步会出浮点毛刺 —— 例 18.8 × 1.7 = 31.960000000000004，
  //: 原样写进价格表后提交，平台侧可能因小数位过多被拒（也难看）。
  //: `Math.round(x*100)/100` 是两位小数的标准做法（不引入 toFixed 的字符串化）。
  return Math.round(clamped * 100) / 100;
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

/** 价格列下标 —— ⚠️ **走映射**（`售价` 那条的来源列；没配 ⇒ -1）。
 *
 *  用户口径 2026-10-15：「所有点**均走映射**，移除写死的逻辑」。
 *  ⚠️ 不再按别名猜「原价 / 价格 / 单价」—— 猜出来的不是用户配的那一列就是**静默错值**。
 */
export function priceColumnIndex(
  columns: string[],
  priceMapping: readonly PriceMappingRow[] = [],
): number {
  return sourceColumnIndex(columns, priceMapping, "price");
}

/** 库存列下标 —— ⚠️ **走映射**（`库存` 那条的来源列；没配 ⇒ -1）。 */
export function stockColumnIndex(
  columns: string[],
  priceMapping: readonly PriceMappingRow[] = [],
): number {
  return sourceColumnIndex(columns, priceMapping, "stock");
}

/** SKU 名称列（拆分规格分类 / SKU 名称的唯一来源）。 */
export const SKU_NAME_COLUMN_ALIASES = ["sku名称", "skuname", "规格名", "颜色分类", "name"] as const;

/** **数据包没有规格分类时的单一维度名**。
 *
 *  背景（真实数据包 2026-10-15 实测）：`sku名称` 列的值可能是**纯规格值**、不带
 *  `分类:` 前缀 —— 如 `A款`。`parseSkuName("A款").category` 按设计口径返回**空串**
 *  （「不猜」）。若把它当「没有规格分类」丢掉，整条「预检通过 SKU 导入」链路会塌：
 *  候选清单为空 ⇒ 第二步按钮**恒灰** + 恒提示「没有可导入的 SKU」，
 *  而用户在第一步明明看到有预检通过、且勾选了行。
 *
 *  ⇒ 需要区分「**没有分类**」与「**解析不出来**」：数据包只有值、没有分类时，
 *  规格维度**退化为一个**（下面的名字就是那个维度的名字），值仍然是每行的名称。
 *  用户可在导入弹窗把它映射到任意平台规格类型（这正是弹窗存在的意义）。
 *
 *  ⚠️ **不是**数据形态分流（不给新包形加 if）：判据是「名称里有没有 `:`」这一条
 *  通用规则，任何包（有分类 / 无分类）都走同一个 `parseSkuName`。
 */
export const SINGLE_SPEC_NAME = "规格";

/** **按映射取列，没配才退回别名猜**（用户口径 2026-10-15：「正常应**全部使用映射关系**
 *  得到列值」「预检阶段的派生判定（哪列是 SKU 名、哪列是线上图）**应走映射**」）。
 *
 *  ⚠️ 为什么必须这样：用户把「SKU 名称」映射到某列，就是**明确指定**了那一列。
 *  此时若代码仍按别名猜，会出现「用户改了映射、程序读的还是老列」——
 *  **静默错值**，而且用户看不出哪里不对（界面显示的是他配的，行为是猜的）。
 *  ⚠️ 映射没配 / 指向的列不存在 ⇒ **退回别名**：映射是可选的便利，
 *  不是必须的配置（老数据包 / 用户没动过映射时仍要能跑）。
 */
export function columnByMappingOrAlias(
  columns: readonly string[],
  rows: readonly PriceMappingRow[],
  target: PriceTargetKey,
): number {
  return sourceColumnIndex(columns, rows, target);
}

/** **规格文本**（用户口径 2026-10-21 的核心修正）。
 *
 *  > 「SKU 名称也应无需再让用户配置映射关系，当用户配置完**商品 SKU 映射关系**
 *  >   指定的列**去重后就是对该组的 sku 规格**」
 *
 *  数据包实证（`店透-SKU预览-表格-1073821618329-2026-10-21.csv`，456 行 × 13 列）：
 *    · `颜色分类` 去重 24 个（`奇遇童话01…24`，与 `SKU图/` 的 24 张图**一一对应**）；
 *    · `尺寸` 去重 19 个；两者**组合**去重 **456** = 行数（无重复组合）。
 *    · `SKU信息` 列是「颜色;尺寸」的拼串（与 `颜色分类 + ";" + 尺寸` 逐字一致）。
 *  ⇒ 规格 = 用户选中的那几列**按行拼接 + 去重**，而不是让用户再映射一个「SKU 名称」列。
 *
 *  ⚠️ 分隔符用 `" / "`：与第二步 `spec_combinations` 的 `c.spec` **同形**
 *  （维度按 `dims` 顺序、` / ` 分隔）—— 不同形的话价格表按 key 对齐时行会凭空多出来。
 *  ⚠️ 值本身**不再拆冒号**（用户口径 2026-10-20：「从 sku 名称按冒号取前缀这个是错的」）。
 */
export function skuSpecTextOf(
  rowCells: Record<string, string>,
  specColumns: readonly string[],
): string {
  return specColumns
    .map((col) => String(rowCells[col] ?? "").trim())
    .filter((v) => !!v)
    .join(" / ");
}

/** **SKU 图（固定列）的本行值**（用户口径 2026-10-21）。
 *
 *  > 「规格图不应在价格库存映射关系表中再次进行配置，应为在预检步骤中
 *  >   SKU 图是固定列，应直接取规格对应行的固定列 SKU图」
 *
 *  ⚠️ 唯一来源 = 表格里名为 `SKU 图` 的那列（`SKU_IMAGE_COL`）本行值 ——
 *  那是「SKU 图自动匹配 + 手工切换」写进 `rows` 的结果。
 *  ⚠️ 不再接受「用户再映射一个列当规格图」：两份来源必然漂移
 *  （表格显示 A、发布用的是 B，且看不出来哪个对）。
 */
export function skuImageRelOf(
  table: PrecheckTable | null | undefined,
  rowIndex: number,
): string | null {
  if (!table || !table.columns?.length) return null;
  const col = table.columns.findIndex((c) => String(c).trim() === SKU_IMAGE_COL);
  if (col < 0) return null;
  const raw = table.rows?.[rowIndex]?.[col];
  const text = String(raw ?? "").trim();
  return text || null;
}

/** ⚠️ **SKU 名称列**：优先走映射（`sku_name`），没配才按别名猜。 */
/** **第一步的 SKU 名称定位列**（用户口径 2026-10-21，修「全表被判图片未匹配」）。
 *
 *  背景：`sku_name` 已从价格库存下拉里删除（用户口径「移除下拉中的 SKU 图和
 *  SKU 名称」），但第一步判「本地图有没有对上」仍然要**一行一个文本**。
 *
 *  ⇒ 判据退化为「表格里**内容最像一列标识**的那一列」：
 *    取**首个**满足「非空率 ≥ 50% 且平均长度 ≤ 40 字」的列 —
 *    店铺名/商品名这类长文本、图片链接这类超长串会被跳过，
 *    `SKU信息` / `颜色分类` / `尺寸` 这类会命中。
 *
 *  ⚠️ 这**只是配图用的文本**，不参与规格拆分、不改任何数据、不上屏。
 *  ⚠️ 不许改成「按列名猜」（`SKU名称`/`规格名` 别名那套已退场，用户明确反对）。
 *  ⚠️ 一条都找不到 ⇒ 返回 0（用第一列），**绝不返回 -1** ——
 *     返回 -1 会让整表判成「图片未匹配」，那正是本轮要修的 bug。
 */
export function firstUsableColumnIndex(
  table: { columns: string[]; rows: (string | null | undefined)[][] } | null | undefined,
): number {
  const columns = table?.columns ?? [];
  const rows = table?.rows ?? [];
  if (columns.length === 0) return 0;
  if (rows.length === 0) return 0;
  for (let c = 0; c < columns.length; c += 1) {
    let filled = 0;
    let charTotal = 0;
    for (const row of rows) {
      const text = String(row?.[c] ?? "").trim();
      if (!text) continue;
      filled += 1;
      charTotal += text.length;
    }
    if (filled === 0) continue;
    const fillRate = filled / rows.length;
    const avgLen = charTotal / filled;
    if (fillRate >= 0.5 && avgLen <= 40) return c;
  }
  //: 全都不合格 ⇒ 仍给 0（宁可错配几行，也不要把整表判成不通过）。
  return 0;
}

export function skuNameColumnIndex(
  columns: string[],
  priceMapping: readonly PriceMappingRow[] = [],
  /** **商品 SKU 映射的列名**（用户口径 2026-10-21：这几列就是规格本身）。
   *
   *  ⚠️ 它是**兜底**，不是替代：`priceMapping` 里配了 `sku_name` 仍然优先。
   *  为什么需要兜底：用户口径已定「SKU 名称无需再配置映射」——
   *  价格库存表里已经没有这一行了 ⇒ 只认 `priceMapping` 会恒返回 -1
   *  ⇒ `passedSkuRows` 恒空 ⇒「预检通过 SKU 导入」拿不到任何行。
   *  这不违反「移除写死的逻辑」：兜底用的是**用户亲手选的列**，不是代码猜列名。
   */
  specColumns: readonly string[] = [],
): number {
  //: ① 价格库存表里显式配了 `sku_name` ⇒ 用它（老配置仍然有效）。
  const mapped = sourceColumnIndex(columns, priceMapping, "sku_name");
  if (mapped >= 0) return mapped;
  //: ② ⚠️ **不再按列名别名猜**（用户口径 2026-10-15：
  //:    「移除写死的逻辑」，`sku名称 / 规格名 / 颜色分类` 那套别名已退场）——
  //:    代码不替用户判断哪一列是 SKU 名称。
  //: ③ 兜底 = 商品 SKU 映射的**第一列**（用户亲手选过的，最可靠）。
  const first = specColumns.map((c) => String(c).trim()).find((c) => !!c);
  if (!first) return -1;
  return columns.findIndex((c) => String(c).trim() === first);
}

/** **商品 SKU 映射的列名清单**（按用户配置的顺序）—— SKU 名称 / 价格的取值基准。
 *
 *  ⚠️ 用户口径 2026-10-21：「当用户配置完**商品 SKU 映射关系**指定的列
 *  **去重后就是对该组的 sku 规格**」⇒ 这几列是第一真相源。
 *
 *  ⚠️ 它**不是** `priceMapping`（那是价格库存表）。早期两者混用，导致
 *  「价格库存没配 SKU 名称 ⇒ 一行都读不出」。分开之后语义才清楚。
 */
export function rowsFirstMappedSource(specColumns: readonly string[]): string {
  return specColumns.map((c) => String(c).trim()).find((c) => !!c) ?? "";
}


/** **「SKU 图」预览列的列名**（屏幕上第 2 列）。
 *
 *  ⚠️ 它不是 xlsx 里读到的列，而是**渲染层自造**的（表格列 key = `__matched_image`）——
 *  值来自 `appliedSkuImages[行]`（用户点「应用匹配结果」后的确定结果），
 *  **不在 `table.rows` 里**。
 *
 *  ⚠️ 用户口径 2026-10-15：「下拉不完整，**没有 SKU 图片列**，
 *  应展示商品 sku 表格的**所有列**」⇒ 它必须出现在映射下拉里。
 *  ⇒ 在候选清单里**算一列**（`priceSourceOptions`），取值时**特判**
 *  （`sourceColumnIndex` 返回 `VIRTUAL_COL_INDEX`）。
 */
export const SKU_IMAGE_COL = "SKU 图";

/** **把「SKU 图」作为真列插进表格**（用户口径 2026-10-15：「把第一列**放入 rows**，
 *  它是**固定列**」）。
 *
 *  ⚠️ 它是商品 sku 表格的**第一列**（表格渲染时那一列的 `render` 直接读
 *  `rows[i][index]`）—— 进 `rows` 才能被**映射 / 规则 / 取值**统一处理，
 *  而不是只活在渲染层的 `appliedSkuImages` 里（那是两处真相源）。
 *  ⚠️ 位置 = 数据列**第 0 位**（屏幕上它在最左，见表格 `columns` 的第一项）。
 *  ⚠️ 幂等：已有这列就不动。
 *  ⚠️ 值是**数据包内的相对路径**；没匹配到的行留 `null`（不造假）。
 *  ⚠️ 它是**固定列**（`column_keys` 不带 `__manual_` 前缀 ⇒ 不可删）。
 */
export function withSkuImageColumn<
  T extends {
    columns: string[];
    rows: (string | null)[][];
    column_kinds?: string[];
    column_keys?: string[];
  },
>(table: T, relByRowIndex: Readonly<Record<number, string>> = {}): T {
  if (table.columns.includes(SKU_IMAGE_COL)) return table;
  return {
    ...table,
    columns: [SKU_IMAGE_COL, ...table.columns],
    rows: table.rows.map((r, i) => [relByRowIndex[i] ?? null, ...r]),
    column_kinds: table.column_kinds ? ["text", ...table.column_kinds] : table.column_kinds,
    column_keys: table.column_keys ? [SKU_IMAGE_COL, ...table.column_keys] : table.column_keys,
  };
}

/** **「后处理的固定表格列」插入**（用户口径 2026-10-15，2026-10-20 收敛为一列）。
 *
 *  ⚠️ 现在只剩 `SKU 图` 一列（商品 sku 表格的**第一列**，值 = 匹配结果，存在 `rows` 里）。
 *  ⚠️ **「规格分类」固定列已按用户口径整体移除**：
 *    「商品发布移除商品sku表格中的规格分类固定列」「『规格分类』固定列 相关所有逻辑移除」
 *    —— 不要再加回来。
 *  ⚠️ 幂等：已在的列跳过。
 */
export function withDerivedColumns<
  T extends {
    columns: string[];
    rows: (string | null)[][];
    column_kinds?: string[];
    column_keys?: string[];
  },
>(table: T, skuImageByRowIndex: Readonly<Record<number, string>> = {}): T {
  return withSkuImageColumn(table, skuImageByRowIndex);
}

/** 某列**能不能删**（用户口径 2026-10-15）：
 *  > 「手动新增的列支持**整列删除**，**非手动新增和固定列不支持删除**」
 *
 *  ⚠️ 判据只看 key 前缀 —— 固定列（xlsx 原始列 + 「规格分类」）都没有手动前缀。
 *  ⚠️ `column_keys` 缺失（后端暂时不下发）⇒ 一律不可删：
 *  **宁可少给一个删除按钮，也不能让用户删掉原始列**（原始列删了没法还原）。
 */
export function canDropColumn(key: string | undefined | null): boolean {
  return isManualColumn(key);
}

/* ------------------------------------------------------------------ *
 * 需求 2：SKU 名称按**第一个冒号**拆成「规格分类 + SKU 名称」
 * ------------------------------------------------------------------ */

/**
 * `颜色分类:【中古风】实木三斗柜` ⇒ `{ category: "颜色分类", value: "【中古风】实木三斗柜" }`。
 *
 * 用户口径 2026-10-11：「规格sku表中sku名称使用第一个：号作为分割符号，分割为
 * 规格分类和sku名称」。
 *
 * ⚠️ **只切第一个冒号**，半角 `:` 与全角 `：` 都认（数据包两种都出现过）。
 * 值里可能还有冒号（`尺寸:1m:加宽`）⇒ 用 `indexOf` 而非 `split`，
 * 后面的冒号原样留在 `value` 里（不能吞掉用户真实的规格值）。
 *
 * 没有冒号 ⇒ `category` 空、整串当 `value`（**不猜**：不把第一个词当分类）。
 */
export function parseSkuName(raw: string | null | undefined): {
  category: string;
  value: string;
} {
  const text = String(raw ?? "").trim();
  if (!text) return { category: "", value: "" };
  const half = text.indexOf(":");
  const full = text.indexOf("：");
  let cut = -1;
  if (half >= 0 && full >= 0) cut = Math.min(half, full);
  else cut = Math.max(half, full);
  if (cut < 0) return { category: "", value: text };
  return {
    category: text.slice(0, cut).trim(),
    value: text.slice(cut + 1).trim(),
  };
}

/* ------------------------------------------------------------------ *
 * 需求 3：数据包 `SKU/` 目录的图按 **SKU 名称**（不是文件名）与表格行对齐
 * ------------------------------------------------------------------ */

/** 从数据包 SKU 目录的**文件名**里抽出 SKU 名称（去掉 `SKU01_` 与扩展名）。
 *
 * 真包实测（2026-10-11）：
 * `SKU01_颜色分类(【中古风】实木三斗柜).jpg`
 * ⇒ `颜色分类:【中古风】实木三斗柜`（文件名用 `(...)` 包规格值，表格用 `:`）。
 */
export function skuImageNameFromFile(fileName: string): string {
  let text = String(fileName ?? "").split(/[\\/]/).pop() ?? "";
  text = text.replace(/\.[A-Za-z0-9]+$/, ""); // 去扩展名
  text = text.replace(/^\s*SKU\s*\d+\s*[_-]?\s*/i, ""); // 去 `SKU01_`
  const m = text.match(/^([^()（）]+)[（(](.+)[）)]$/); // `分类(值)` ⇒ `分类:值`
  if (m) return `${m[1].trim()}:${m[2].trim()}`;
  return text.trim();
}

/** 比对用的规范化名字：小写 + 去空白 + 括号统一成半角（`【】` 保留，它有语义）。 */
function normalizeSkuKey(value: string): string {
  return String(value ?? "")
    .replace(/\s+/g, "")
    .replace(/（/g, "(")
    .replace(/）/g, ")")
    .toLowerCase();
}

/* ------------------------------------------------------------------ *
 * 需求 5：本地图对齐 —— **规则可配置的正则匹配**（用户口径 2026-10-11）
 *
 * > 「本地图匹配正则在商品 sku 列表的本地图片列表头添加规格配置按钮，使用 tip 弹窗，
 * >   只需要配置一个正则即可，将当前匹配规则默认配置在输入框中，输入框点击重新匹配
 * >   触发重新对 sku 所有行与本地 sku 图进行匹配对齐」
 *
 * 默认规则 = 命名规则的**逆用**（见 `skuImageNameFromFile`）：
 *   本地图名 `SKU01_颜色分类(【中古风】实木三斗柜).jpg`
 *   ⇒ 抽出 `颜色分类:【中古风】实木三斗柜`，与表格 `sku名称` 列同形后**等值比对**。
 *
 * 用户改了正则 ⇒ 用新正则重新抽取本地图名 ⇒ 对所有行重跑对齐。
 * ------------------------------------------------------------------ */

/**
 * **默认匹配正则**（用户口径「将当前匹配规则默认配置在输入框中」）。
 *
 * 捕获组语义（必须两个组，顺序固定）：
 *   组 1 = **规格分类**（如 `颜色分类`）
 *   组 2 = **真实 SKU 名称**（如 `【中古风】实木三斗柜`）
 * 拼接成 `组1:组2`，与表格 `sku名称` 列同形。
 *
 * 逐字对应命名规则 `{前缀}{序号}_{规格分类}({真实SKU名})`：
 *   `^\s*SKU\s*\d+\s*[_-]?\s*([^()（）]+)[（(](.+)[）)]\s*$`
 *    └─ 前缀+自增序号+「_」─┘ └─规格分类─┘└─括号内真实名─┘
 *
 * ⚠️ **不带扩展名部分再单独剥**（正则只管主体）—— 见 `matchSkuImageName`。
 */
export const DEFAULT_SKU_IMAGE_PATTERN =
  "^\\s*SKU\\s*\\d+\\s*[_-]?\\s*([^()（）]+)[（(](.+)[）)]\\s*$";

/** 正则匹配后的结果（`key` = 拼好的 `规格分类:真实名`）。 */
export type SkuImageMatch = { key: string; groups: string[] } | null;

/**
 * 用**用户可配的正则**从本地图文件名抽取比对键。
 *
 * @param fileName 本地图文件名（可带目录前缀与扩展名）
 * @param pattern  用户配置的正则（捕获组 1=规格分类、2=SKU 名称）
 * @returns 命中 ⇒ `{key, groups}`；没命中 / 正则非法 ⇒ `null`（**不猜**）
 */
export function matchSkuImageName(fileName: string, pattern: string): SkuImageMatch {
  const base = String(fileName ?? "").split(/[\\/]/).pop() ?? "";
  //: 扩展名总是先剥掉（正则按「命名规则」写，不掺扩展名，用户改起来更简单）。
  const stem = base.replace(/\.[A-Za-z0-9]+$/, "");
  let re: RegExp;
  try {
    re = new RegExp(pattern);
  } catch {
    //: 正则语法错 ⇒ 不匹配（调用方据 `null` 保持原状/提示，不抛错打断渲染）。
    return null;
  }
  const m = stem.match(re);
  if (!m) return null;
  const groups = m.slice(1).map((g) => String(g ?? "").trim());
  //: 两个组都要有值才拼键 —— 缺一个就是「没按规则解析出来」，不算命中。
  const key = groups.length >= 2 && groups[0] && groups[1] ? `${groups[0]}:${groups[1]}` : "";
  return { key: key || groups[0] || "", groups };
}

/**
 * 校验用户输入的正则（供配置弹窗给即时反馈，**不抛错**）。
 *
 * @returns 合法 ⇒ `null`；非法 ⇒ 给用户看的错误说明。
 */
export function validateSkuImagePattern(pattern: string): string | null {
  const text = String(pattern ?? "").trim();
  if (!text) return "正则不能为空";
  try {
    const re = new RegExp(text);
    //: 捕获组数量是这个规则的核心契约（组1=规格分类、组2=SKU 名称）——
    //: 少于 2 个组时抽不出「分类:名称」，必须提前说清楚而不是默默匹配不上。
    const probe = "SKU01_颜色分类(甲)".match(re);
    if (!probe || probe.length < 3) {
      return "正则需要 2 个捕获组：第 1 组=规格分类、第 2 组=SKU 名称";
    }
    if (!re.test("SKU01_颜色分类(甲)") && !probe) {
      return "正则无法匹配形如「SKU01_颜色分类(甲)」的文件名";
    }
    return null;
  } catch (error) {
    return `正则语法错误：${error instanceof Error ? error.message : String(error)}`;
  }
}

/** 数据包 `SKU/` 目录的媒体 → **按加载顺序排列**的 `[规范名, 相对路径]` 列表。
 *
 * ⚠️ **保留数组而非 map**：匹配结果要「取第一个」时，「第一个」必须由
 * **加载顺序**决定（数据包里 `SKU01…SKU26` 的自然顺序）。
 *
 * @param pattern 抽取规则（缺省用 `DEFAULT_SKU_IMAGE_PATTERN`）。
 */
export function localSkuImageList(
  media: readonly PrecheckMedia[],
  pattern: string = DEFAULT_SKU_IMAGE_PATTERN,
): { key: string; relPath: string; fileName: string }[] {
  const out: { key: string; relPath: string; fileName: string }[] = [];
  for (const item of media) {
    if (item.group !== "sku") continue;
    const fileName = item.file_name || item.rel_path;
    const hit = matchSkuImageName(fileName, pattern);
    out.push({
      key: hit ? normalizeSkuKey(hit.key) : "",
      relPath: item.rel_path,
      fileName,
    });
  }
  return out;
}

/**
 * **可参与本地图匹配的 SKU 媒体**。
 *
 * ⚠️ 用户口径 2026-10-21 修订（**取代** 2026-10-11 的「违规图不允许被匹配到」原句）：
 *
 * > 「检测到违规词的媒体资源放开也可选择，检查完默认是不选择的，不再固定为不可选，
 * >   决策权交给用户」
 *
 * 决策权既然交给用户，本函数就不能再按 `has_forbidden` 静默剔除 —— 否则会出现
 * 「用户勾了那张图，但规格表里永远匹配不到」这种**最难查的错位**（勾选态与匹配结果
 * 不一致，且没有任何提示）。违规图的风险提示由界面承担（红框 + 角标 + 默认不勾）。
 *
 * 唯一保留的硬判据是 **是否真跑过检测**：
 *   - `ocr_ok === true` ⇒ 放行（用户勾了就参与匹配）；
 *   - `ocr_ok === null/undefined` ⇒ 排除（未检测不许当合规，防免检通道）。
 *
 * ⚠️ 这是**唯一**的过滤实现：名称模糊查询与手选弹窗都必须从这里取候选。
 */
export function selectableSkuMedia<T extends PrecheckMedia>(media: readonly T[]): T[] {
  return media.filter((m) => m.ocr_ok === true);
}

/** **一个格子的数值是否可用于计算**（用户口径 2026-10-21）。
 *
 *  > 「红框中**非数值且小于等于 0**，则**无法计算应红框提示**」
 *
 *  判据（三条，缺一不可）：
 *    ① 空 / 全空白 ⇒ 不可用（没有值可算）；
 *    ② 解析不出数值（`"-"` / `"abc"` / `"20元"`）⇒ 不可用；
 *    ③ 数值 **≤ 0** ⇒ 不可用（价格 / 库存为 0 或负数在电商里没有意义，
 *       继续 ×1.8 只会把错误放大 —— 用户明确要求这种格子不许参与计算）。
 *
 *  ⚠️ **单一实现**：单元格标红、规则执行前的可算性检查、批量修改都调它 ——
 *  三处各写一份会出现「标红了但还算」这种自相矛盾的界面。
 */
export function numericCellIssue(raw: string | null | undefined): string {
  const text = String(raw ?? "").trim();
  if (!text) return "为空";
  //: 允许 `￥`/`¥` 与千分位逗号（与后端 `_as_number_or_none` 同口径），但**不认**中文单位。
  const cleaned = text.replace(/[\s￥¥,]/g, "");
  if (!/^-?\d+(\.\d+)?$/.test(cleaned)) return "不是数字";
  const value = Number(cleaned);
  if (!Number.isFinite(value)) return "不是数字";
  if (value <= 0) return "小于等于 0，无法计算";
  return "";
}

/**
 * 在红线过滤之上，再按**媒体勾选状态**收窄（用户口径 2026-10-11）。
 *
 * > 「当用户在媒体资源 sku 区域中取消了勾选，则对应的 sku 规格列表的图也要被移除」
 *
 * ⚠️ **单一实现**：组件（`TablesPanel` 的候选集）与测试都用这一个函数 ——
 * 逻辑写在组件里就没法被 vitest 覆盖（`.tsx` 不被 lib 测试导入），
 * 那样这条规则只是「看起来实现了」而没有守卫。
 *
 * ⚠️ 判据必须是 `=== true`：**键缺失算未勾选**。写成 `!== false` 会把
 * 「从未出现在 checked 里的图」当成已勾选 ⇒ 取消勾选不生效、提示数恒 0（静默失效）。
 */
export function filterCheckedSkuMedia<T extends PrecheckMedia>(
  list: readonly T[],
  checked: Record<string, boolean>,
): T[] {
  return list.filter((m) => checked[`${m.group}/${m.rel_path}`] === true);
}

/**
 * **因取消勾选而被移除的 SKU 图**（用户口径 2026-10-11 要给提示）。
 *
 * 口径：**本来可选**（`selectableSkuMedia` 的口径：已检测）但**未勾选**的那些。
 * 未检测的图另有红框/角标提示 ⇒ 不重复计入（否则提示数虚高，用户会以为算错了）。
 *
 * ⚠️ 2026-10-21 起违规图也**算「本来可选」**（用户把决策权拿走了），
 * 所以用户不勾它们时会出现在这条提示里 —— 这是对的：那正是「你少用了这几张图」。
 */
export function countUncheckedSkuMedia(
  media: readonly PrecheckMedia[],
  checked: Record<string, boolean>,
): number {
  return selectableSkuMedia(media).filter(
    (m) => checked[`${m.group}/${m.rel_path}`] !== true,
  ).length;
}

 /** 按 SKU 名称在**已加载的本地 SKU 图**里查找（用户口径 2026-10-11 **最终版**）。
 *
 * ## 判据：按命名规则**精确对齐**
 *
 * > 「sku 图片名称的规则，前缀固定加自增序号 +「_」+ 规格分类 + 括号中是真实的
 * >   sku 名称」
 *
 * 文件名按规则解析后得到 `规格分类:真实 SKU 名称`，与表格 `sku名称` 列**同形**
 * （解析在 `skuImageNameFromFile`，见其文档）⇒ 这里就是**等值比对**：
 *
 * 1. **完整名**（`颜色分类:【中古风】实木三斗柜`）—— 表格那条；
 * 2. **规格值兜底**（`【中古风】实木三斗柜`）—— 兼容不带「规格分类」的历史包。
 *
 * ⚠️ **不再用通配符模糊匹配**：上一版按「前后通配取第一个」是**规则不明**时的权宜
 * （用户当时说「没有固定规则」）；规则现已明确 ⇒ 通配符只会**引入误配**
 * （前缀重叠的两条规格互相吃掉，正是「静默配错图比不配更糟」）。
 *
 * ⚠️ 全部不命中 ⇒ `null`（不猜、不按序号兜底）—— 上层据此给「选择本地图」入口。
 */
export function lookupLocalSkuImage(
  list: readonly { key: string; relPath: string }[],
  skuName: string,
): string | null {
  if (!skuName || list.length === 0) return null;
  const parsed = parseSkuName(skuName);
  //: ① 完整名精确（表格 `sku名称` 与文件名解析结果同形）。
  const byFull = list.find((it) => it.key === normalizeSkuKey(skuName));
  if (byFull) return byFull.relPath;
  //: ② 规格值兜底（兼容文件名不带「规格分类」的包）。
  if (parsed.value) {
    const byValue = list.find((it) => it.key === normalizeSkuKey(parsed.value));
    if (byValue) return byValue.relPath;
  }
  return null;
}

/** 兼容旧签名（`Record` 查表）—— 内部转成数组后走同一套模糊匹配。 */
export function lookupLocalSkuImageFromMap(
  map: Record<string, string>,
  skuName: string,
): string | null {
  return lookupLocalSkuImage(
    Object.entries(map).map(([key, relPath]) => ({ key, relPath })),
    skuName,
  );
}

/* ------------------------------------------------------------------ *
 * 需求 5：线上图 / 本地图 —— **已放弃图片内容比对**（用户口径 2026-10-11 最终版）
 *
 * 原来的 `compareSkuImages()`（HEAD 取 CDN ETag / MD5 指纹 / 尺寸降级）**整段移除**，
 * 因为它回答不了实际问题，且三条路实测都不成立：
 *
 * | 方案 | 实测结果 |
 * |---|---|
 * | 小红书 CDN `ETag` == MD5 | ✅ 成立，但**只对 `material_space/<uuid>`**；`arkgoods/<id>` 裸链 404/403（需签名） |
 * | 1688 外部图床（真包「图片」列现状） | ❌ 无 ETag / 无 Content-MD5（只有 Content-Length） |
 * | 尺寸降级判据 | ❌ 同尺寸不同图会**假通过** |
 * | 下载后 pHash 画面比对 | ❌ 需下载（用户否决，风控）；且实测**异图距离仅 4/64**，与同图 0/64 的阈值窗口重叠 ⇒ 会误判 |
 *
 * ⇒ 用户 2026-10-11 改口径：「将拆分后的 sku 名称去查本地 sku 图名进行对齐即为通过了预检」。
 * 判据变成**确定性的存在性检查**（`lookupLocalSkuImage` 能不能查到），
 * 零网络请求、零风控风险、也不会误判。
 * ------------------------------------------------------------------ */

/**
 * **预选通过**的 SKU 规格集合（用户口径 2026-10-11 需求 4/5）。
 *
 * > 「商品sku表格中的所有列默认全选，未选中的不允许进入配置商品参数也就是预选未通过」
 * > 「预检不通过则默认置灰不勾选，第二步配置参数时后续逻辑注意只是有预检通过的sku规格」
 *
 * 单一实现，**两处共用**：预检页算勾选/置灰，第二步据此过滤价格库存表。
 * 分成两份实现必然分叉 —— 一边放行一边拦截，用户会看到「第一步勾着的规格
 * 第二步没了」这种自相矛盾的界面（与 `lookup_spec_image` 同一条纪律）。
 *
 * ⚠️ 判据是 **勾选** 与 **被拦（`different`）** 两件事，**`unknown` 不在此列**：
 * 用户口径「其余提示需人工检查并不勾选且无需整行置灰」⇒ 判不了的行只是
 * **默认不勾选**，用户自己看一眼就能勾上；它不该进 `blockedRows`。
 * 所以这里拦的只有确定性结论 —— 否则「判不了」会被当成「不合格」误杀。
 *
 * @param table       SKU 表
 * @param blockedRows 预选**未通过**的行下标（两图判定为 `different`）
 * @param pickedRows  用户勾选的行下标（默认全选；**未勾选 ⇒ 预选未通过**）
 * @returns 通过的 SKU 规格名集合（`规格名:值` 原样，用作 `priceRows[].spec` 的判据）
 */
export function passedSkuSpecs(
  table: PrecheckTable | null | undefined,
  blockedRows: readonly number[],
  pickedRows: readonly number[],
  /** **用户配的价格库存映射**（SKU 名称列优先走它；用户口径 2026-10-15）。 */
  priceMapping: readonly PriceMappingRow[] = [],
): Set<string> {
  if (!table || !table.columns?.length) return new Set();
  const nameCol = skuNameColumnIndex(table.columns, priceMapping);
  if (nameCol < 0) return new Set();
  const blocked = new Set(blockedRows);
  const picked = new Set(pickedRows);
  const out = new Set<string>();
  (table.rows ?? []).forEach((row, index) => {
    //: 两个条件都要：**勾选了** 且 **没被判定拦下**。
    //: 只看勾选 ⇒ 判定为「不一样」的行手滑勾上就漏进第二步；
    //: 只看判定 ⇒ 用户故意取消勾选的规格又被放回去（预选形同虚设）。
    //: ⚠️ 与 `passedSkuRows` **同一口径**：空 `picked` = 全选（表格默认全选，
    //: 且勾选态可能还没上抛）—— 两处不一致会让第一步勾着的规格在第二步消失。
    if (blocked.has(index) || (pickedRows.length > 0 && !picked.has(index))) return;
    const name = String(row[nameCol] ?? "").trim();
    if (!name) return;
    //: ⚠️ **产出与 `spec_combinations` 的 `c.spec` 同形**（`分类:名称` 或裸名称）——
    //: 这是【真 bug 修复 2026-10-15】的判据：「确认导入后价格和库存列表**没有导入
    //: 对接 sku 数据」。根因是两边形态不一致（一个裸名、一个带前缀）⇒ 一个都对不上
    //: ⇒ `visible` 为空 ⇒ 价格库存表一行都不落（且不报错）。
    //:
    //: ⚠️ **不做名称形态推断**（用户口径 2026-10-20：
    //: 「从 sku 名称按冒号取前缀这个是错的，不要改成这个逻辑，用户使用规则自己控制」）。
    //: ⇒ 裸名称原样入集合（与 `spec_combinations` 的 `c.spec` 对齐由用户配的规则保证）。
    out.add(name);
  });
  return out;
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
  /** 计算结果为空 / 非数值的行（红框 + 取消勾选，用户口径 2026-10-21）。 */
  const badRows = new Set<number>();
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
        if (next === null) {
          badRows.add(rowIndex);
          return cell;
        }
        //: 价格保留两位小数、整数不补 `.00`（与平台展示口径一致）。
        return Number.isInteger(next) ? String(next) : next.toFixed(2);
      });
    }),
    //: ⚠️ 用户口径 2026-10-21：「当前计算规则**当前行数为空或非数值**时，
    //: **红框展示且该数据取消勾选**」——
    //: 算不出来的行必须**标记出来**（`batchRuleBadRows`），界面据此画红框 + 取消勾选。
    //: 只靠「值没变」看不出来：用户以为规则生效了，其实这一行被跳过了。
    batchRuleBadRows: [...badRows].sort((a, b) => a - b),
  } as PrecheckTable & { batchRuleBadRows: number[] };
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

/** SKU 表在**完整 `tables`** 里的下标。
 *
 * ⚠️ 用户口径 2026-10-11：属性图 OCR 已整链移除 ⇒ 后端不再产出「属性 KV 表」，
 * 原来的 `Array.isArray(t.kv)` 判据**失去载体**（字段已从 `PrecheckTable` 删除）。
 * 保留函数与返回值形状：它是面板↔完整数组的下标桥，删掉会连带 `mergePanelTable`
 * 一起返工，而「有哪些表」这件事现在就是**数据包里的全部表**。
 * 旧实现在 docstring 里存照：`tables.map((t,i) => Array.isArray(t.kv) ? -1 : i)`。 */
export function skuTableIndices(tables: PrecheckTable[]): number[] {
  return tables.map((_, i) => i);
}

/** 把面板改后的第 `panelIndex` 张表嵌回**完整数组**。
 *
 * ⚠️ 这是「第二次改库存无响应」的修复点：若面板直接上抛子集，表集合会变化
 * ⇒ 面板重挂载、`operand` 归零 ⇒ 按钮回到禁用。
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

/** 区域 → 发布体里的去处。

 *  ⚠️ **本函数已退化为「按 `kind` 参考分类」的兜底**（用户口径 2026-10-13）：
 *  发布位现在由**区域映射弹窗**（`zoneMap`）显式指定，主图/详情图/视频各取哪些
 *  **区域名**由用户选，不再靠 `group === "main"` 这类目录名猜测
 *  （`group` 是目录名原值，猜不准且**静默失效**）。
 *
 *  保留它是因为仍有调用方需要「一份按语义分的粗分组」，但**新代码不该用它
 *  决定发布位** —— 那会绕过用户的映射选择。
 */
export function splitByGroup(media: PrecheckMedia[]): {
  mainImages: string[];
  detailImages: string[];
  skuImages: string[];
} {
  const mainImages: string[] = [];
  const detailImages: string[] = [];
  const skuImages: string[] = [];
  for (const item of media) {
    //: ⚠️ 用 `kind`（后端下发的参考类别）而不是 `group`（目录名原值）。
    if (item.kind === "main") mainImages.push(item.rel_path);
    else if (item.kind === "sku") skuImages.push(item.rel_path);
    else if (item.kind === "detail") detailImages.push(item.rel_path);
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
export function tableToSkus(
  table: PrecheckTable | null | undefined,
  /** **用户配的价格库存映射**（价格库存映射关系表）。
   *
   *  ⚠️ **走映射是必须的**（用户口径 2026-10-15：「正常应**全部使用映射关系**得到列值」）：
   *  下面那套别名（`原价`/`价格`/`库存`…）是**猜列名**——用户把「原价」映射到别的列、
   *  或数据包列名换了（真包就有 `SKU信息` 这种），猜出来的就不是他配的那一列。
   *  后果是**静默错值**：用户改了映射、提交体却还按老列名取值。
   *  ⚠️ 缺省 ⇒ 退回别名猜测（老调用方 / 没有映射时仍能工作）。
   */
  priceMapping: readonly PriceMappingRow[] = [],
  /** 售价的回落列（原价取不到时用它；用户口径「原价优先」）。 */
  priceFallbackSource: string = DEFAULT_PRICE_FALLBACK_SOURCE,
): PublishSkuDraft[] {
  if (!table || !table.columns?.length) return [];
  //: ⚠️ **一律走映射**（用户口径 2026-10-15：「所有点**均走映射**，移除写死的逻辑」）。
  const nameCol = skuNameColumnIndex(table.columns, priceMapping);
  if (nameCol < 0) return [];

  //: ⚠️ **优先用映射**（用户配的为准）；没配那一条才退回别名猜测。
  const priceCol = sourceColumnIndex(table.columns, priceMapping, "price");
  const stockCol = sourceColumnIndex(table.columns, priceMapping, "stock");
  //: ⚠️ 回落列也**走映射**（它同样是一个 target：`price_fallback`）——
  //: 此前按列名现查「计算价格」，是写死逻辑。
  const calcPriceCol = sourceColumnIndex(table.columns, priceMapping, "price_fallback");

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
  /** **用户配的价格库存映射**（透传给 `tableToSkus`；缺省 ⇒ 退回别名猜测）。
   *
   *  ⚠️ 用户口径 2026-10-15：「正常应**全部使用映射关系**得到列值」——
   *  提交体是**真实写操作**，用猜的列名取值一旦猜错就是**发错价**，代价最大。
   */
  priceMapping: readonly PriceMappingRow[] = [],
  priceFallbackSource: string = DEFAULT_PRICE_FALLBACK_SOURCE,
): {
  precheck_token: string;
  main_images: string[];
  detail_images: string[];
  skus: PublishSkuDraft[];
} | null {
  if (!precheck?.token) return null;
  const { mainImages, detailImages } = splitByGroup(precheck.media ?? []);
  const table = (precheck.tables ?? [])[0];
  const skus = tableToSkus(table, priceMapping, priceFallbackSource);
  return {
    precheck_token: precheck.token,
    main_images: mainImages,
    detail_images: detailImages,
    skus,
  };
}

/* ------------------------------------------------------------------ *
 * 2026-10-11 第五批：预检 SKU 行 → 第二步「自动加载参数」第 9 步填充计划
 *
 * 需求（用户原话）：「在发布商品第二步配置参数的自动加载参数按钮逻辑中生成属性后
 * 新增填写 sku 信息，将第一步预检通过的且勾选的 sku 列表中的数据自动填充到商品规格
 * 和价格及库存列表中」；并「上传规格对应的规格图（**上传本地图**）」。
 *
 * 三条口径（用户拍板）：
 *   ① 规格名可能**文本不一致**（1688 叫「颜色分类」，平台叫「颜色」）⇒
 *      **按顺序映射 + 值域校验**，且**写明**映射关系（不静默）；
 *   ② 价格取 `原价`（**优先**）→ 空则 `计算价格`；库存取 `库存`；`ID` ⇒ 商家编码；
 *   ③ 规格图上传失败 ⇒ 按既有**重试配置**重试；仍失败则**跳过上传**，
 *      但该行的规格名/值 + 价格/库存/商家编码**照常填**。
 * ------------------------------------------------------------------ */

/** 平台侧的一个规格维度（来自 `fetchCategoryProperties().sale_properties`）。 */
export type PlatformSpecDim = {
  /** 平台规格名（如「颜色」）。 */
  name: string;
  /** 平台维度 id（提交时 `variants[].attributeId` 用）。 */
  id: string;
  /** 是否允许自定义输入（false ⇒ 只能用平台候选值）。 */
  customizable: boolean;
  /** 平台候选值（`select` / `multi` 用；带平台 valueId）。 */
  values?: { name: string; value_id: string }[];
  /** 有时平台只给裸名单（没有 value_id）。 */
  options?: string[];
};

/** 预检表格里**一行**已通过且勾选的 SKU（向导从 `tables[0]` + 勾选态导出）。 */
export type PassedSkuRow = {
  /** 原始 sku 名称（`颜色分类:【中古风】实木三斗柜`，含规格分类前缀）。 */
  rawName: string;
  /** 规格分类（规格维度名，如「颜色分类」）——**读第一步那张「规格分类」固定列**。
   *
   *  ⚠️ **不再从 `sku名称` 拆冒号**（用户口径 2026-10-15：那条逻辑**移除**）。
   *  拆冒号是「分类由代码派生」时期的产物；现在分类是用户填的**真列**，
   *  再猜名称形态会产生两份矛盾的事实（用户实测：填了「颜色分类」却被报
   *  「没有与数据包『规格』对应的维度」）。
   *  ⚠️ 列不存在 / 该行没填 ⇒ `SINGLE_SPEC_NAME`（不是空串）—— 空串会被
   *  当成「没有规格分类」丢掉，整条导入链路一起塌。
   *  ⚠️ 2026-10-20 起**恒为该值**（分类不由代码决定，见 `passedSkuRows`）。 */
  specName: string;
  /** 规格值 —— **整个 sku 名称**（分类已独立成列，名称里的前缀不再切掉）。 */
  specValue: string;
  /** **原价**（第一步 SKU 表 `原价` 列）—— 售价倍率的**基数**（用户口径：
   *  「在价格与库存的原价列基础上默认 1.7 倍」）。取不到 ⇒ null。 */
  originalPrice: number | null;
  /** 售价（原价优先，空则计算价格）；取不到 ⇒ null。
   *  ⚠️ 这是**未乘倍率**的原始值 —— 倍率在导入时按用户配置现算（见 `buildSkuFillPlan`）。 */
  price: number | null;
  /** 库存；取不到 ⇒ null。 */
  stock: number | null;
  /** 商家编码（来自映射到「商家编码」的那一列，默认 `ID`）。 */
  sellerCode: string;
  /** **条形码**（映射到「条形码」的那一列）。
   *
   *  ⚠️ 平台提交体认 `skuData[].barcode`（后端一直支持，此前**前端下拉漏了它**
   *  ⇒ 永远配不出来，用户实测 2026-10-15）。
   *  ⚠️ **可选**：没配这条映射时就该是 `undefined`（**不造假**，也不要空串 ——
   *  空串会让提交体带上一个 `barcode: ""`，平台可能当「填了但非法」）。
   */
  barcode?: string;
  /** **当前库存**（映射到「当前库存」的列；平台侧是线上现值，只读回显）。
   *  ⚠️ 可选：没配映射就不给（不造假）。 */
  currentStock?: number | null;
  /** **规格ID**（映射到「规格ID」的列）。
   *  ⚠️ 可选：平台侧是生成物，我方只透传用户提供的值。 */
  specId?: string;
  /** **本地图**在数据包里的相对路径（用户口径：「上传本地图」）；未对齐 ⇒ null。
   *
   *  ⚠️ 这是**按 SKU 名称对齐**那条老路的产物（`lookupLocalSkuImage`）。
   *  用户口径 2026-10-15：「不再走现在默认的逻辑，**应走映射逻辑**」⇒
   *  规格图现在由**映射**（`spec_image` 的来源列）决定，见 `specImageRel`。 */
  localRel: string | null;
  /** **规格图**（按映射取到的那一列的值）。
   *
   *  ⚠️ 来源列由 `spec_image` 这条映射决定（通常是「SKU 图」虚拟列）。
   *  ⚠️ 与 `localRel` 的区别：`localRel` 是「代码按名称猜」，本条是「用户指定的列」。
   *  配了映射就**只认映射**（用户口径：不再走默认逻辑）。
   *  ⚠️ **可选**：没配 `spec_image` 映射时就该是 `undefined`（不造假）。
   */
  specImageRel?: string | null;
  /** 本地图文件名（上传时按内容 MD5 重命名，这里只作 `origin_name`）。 */
  localFileName: string;
  /** **整行原始值**（按表头列名索引）—— 用户口径 2026-10-20：
   *  「导入sku数据弹窗中的商品sku的映射关系应左侧…**预检通过列的所有列**，
   *   实际导入规格时需**对选择的列数据进行去重处理**」。
   *
   *  ⚠️ 映射左侧现在选的是**列名**（不是规格分类名）⇒ 导入时必须能按列名
   *  从每一行取值，所以这里要留一份原始行。缺这份数据就只能拿
   *  `specName`/`specValue` 那对旧字段，用户选别的列会**静默取不到值**。
   *  ⚠️ **必填**：只有 `passedSkuRows` 能产出它，凡是构造 `PassedSkuRow` 的地方
   *  都必须给（不给 = 编译不过，而不是运行时静默取空）。
   */
  cells: Record<string, string>;
};

/** 填充计划的**逐规格值结果**（含跳过原因 —— 静默跳过比不填更糟）。 */
export type SkuValuePlan = {
  /** 该规格值在第二步 `specValues[平台维度名]` 里要写的形式。 */
  value: string;
  /** 数据包里的原值（用于写明映射：`【中古风】… → 【中古风】…`）。 */
  rawValue: string;
  /** 命中的平台 valueId（有则带上，提交必需）；自定义/无候选 ⇒ `""`。 */
  valueId: string;
  /** 三态：`standard` 命中平台候选 / `custom` 用原值新建 / `unusable` 用不了。 */
  match: "standard" | "custom" | "unusable";
  /** `unusable` 时的原因（写进进度卡，用户要知道**为什么**没填上）。 */
  reason: string;
};

/** 填充计划：一个平台维度 + 它下面所有要填的值。 */
export type SkuDimPlan = {
  /** 平台维度名（写进 `specTypes`）。 */
  dimName: string;
  /** 平台维度 id。 */
  dimId: string;
  /** 数据包里的规格分类名（**可能和 `dimName` 不同**，正是要「写明」的那点）。 */
  sourceName: string;
  /** 该维度下所有要填的值（保序）。 */
  values: SkuValuePlan[];
};

/** 整份填充计划（第二步据此一次性落 `specTypes` / `specValues` / `priceRows`）。 */
export type SkuFillPlan = {
  /** 逐维度的规格计划；长度 = 平台维度数（只取用到的）。 */
  dims: SkuDimPlan[];
  /** 逐行的价格 / 库存 / 商家编码（按 `spec` 组合文本对齐价格表的行 key）。 */
  rows: {
    spec: string;
    /** **售价**（= 原价原值 / 回落计算价格原值，**不乘倍率**）。 */
    price: number | null;
    /** **市场价** = 原价 × 倍率（用户口径 2026-10-11：市场价才是 1.7 倍那个）。 */
    marketPrice: number | null;
    stock: number | null;
    sellerCode: string;
    /** ⚠️ **条形码**（映射到「条形码」的那一列）。缺映射 / 缺值 ⇒ 空串
     *  （用户口径 2026-10-15：下拉漏了它 ⇒ 永远配不出来）。 */
    barcode: string;
    /** **当前库存 / 规格ID**（用户口径 2026-10-21：固定列都要能映射）。 */
    currentStock?: number | null;
    specId?: string;
  }[];
  /** 规格值 → 本地图（上传用）；key = `${dimName}\u0000${value}`。 */
  images: { dimName: string; value: string; localRel: string; localFileName: string }[];
  /** 人可读的映射说明（**写明**规格名对应关系，用户口径 ①）。 */
  notes: string[];
  /** 跳过项及原因（规格名/值对不上、或者多维度无法拆分）。 */
  skipped: string[];
};

/** 值域匹配（用户口径 ① 的「值域校验」）。
 *
 * 三态：
 *   · 命中平台候选 ⇒ `standard`（**用平台标准值**，带 valueId —— 提交必需）
 *   · 未命中但平台允许自定义 ⇒ `custom`（原值新建）
 *   · 未命中且不允许自定义 ⇒ `unusable`（**不硬填**，否则提交必被平台拒）
 *
 * ⚠️ 比对用「归一化」：小写 + 去首尾空白 + 全角括号转半角 —— 真机数据里
 * `【中古风】实木三斗柜` 这类值平台多带空格差异（与 `normalizeSkuKey` 同口径）。
 */
export function matchSpecValue(
  raw: string,
  dim: PlatformSpecDim,
): { match: SkuValuePlan["match"]; value: string; valueId: string; reason: string } {
  const want = normalizeSkuKey(raw);
  const candidates = dim.values ?? [];
  for (const candidate of candidates) {
    if (normalizeSkuKey(candidate.name) === want) {
      return { match: "standard", value: candidate.name, valueId: candidate.value_id ?? "", reason: "" };
    }
  }
  for (const option of dim.options ?? []) {
    if (normalizeSkuKey(option) === want) {
      return { match: "standard", value: option, valueId: "", reason: "" };
    }
  }
  if (dim.customizable) {
    return { match: "custom", value: raw, valueId: "", reason: "" };
  }
  return {
    match: "unusable",
    value: raw,
    valueId: "",
    reason: `平台「${dim.name}」不接受自定义值「${raw}」，且候选里没有它`,
  };
}

/** 建填充计划：预检行 × 平台规格维度 → 第二步要落的全部数据。
 *
 * 映射策略（用户口径 ① = A 方案）：
 *   1. **维度数相同** ⇒ 按**顺序**对应（数据包第 1 个规格分类 ↔ 平台第 1 个维度）
 *   2. 逐值做**值域校验**（`matchSpecValue`），三态各自处理
 *   3. **维度数不同** ⇒ 不猜，整体跳过 + 写明原因（多维度 SKU 名拆不出来）
 *
 * ⚠️ 为什么按顺序而不是按名字：1688 的规格分类名（`颜色分类`）与小红书类目规格名
 * （`颜色`）**本来就不保证一致**（用户明确说「规格名可能文本不一致」）。按名字硬匹配
 * 在本真包上会**全量落空**；按顺序是唯一可行解，且把对应关系写进 `notes` 让人可核。
 */
export function buildSkuFillPlan(
  rows: readonly PassedSkuRow[],
  dims: readonly PlatformSpecDim[],
  /** **用户确认过的**映射：数据包规格分类名 → 平台维度名（弹窗里默认精确匹配、
   *  可手改）。缺省 ⇒ 退回**文本精确匹配**（同名才算，不做顺序猜测）。 */
  mapping: Readonly<Record<string, string>> = {},
  /** **售价倍率**（用户口径 2026-10-11：原价 × 1.7，可调）。
   *  作用于**原价**：`originalPrice` 有值 ⇒ `原价 × k`；没有 ⇒ 回落计算价格（不乘）。 */
  priceMultiplier: number = DEFAULT_PRICE_MULTIPLIER,
  /** **商品 SKU 映射指定的列**（映射左侧 = 预检通过列名，按用户配置的顺序）。
   *
   *  ⚠️ 用户口径 2026-10-21：「当用户配置完**商品 SKU 映射关系**指定的列
   *  **去重后就是对该组的 sku 规格**」——
   *  ⇒ 价格库存的键用**这几列按行拼接**出来的完整规格文本（`skuSpecTextOf`），
   *  不再依赖「SKU 名称」映射行。
   *  缺省（空数组）= 退回原来的「按维度值去重」行为（向后兼容测试与老调用）。 */
  specColumns: readonly string[] = [],
): SkuFillPlan {
  const plan: SkuFillPlan = { dims: [], rows: [], images: [], notes: [], skipped: [] };
  if (rows.length === 0) {
    plan.skipped.push("没有「预检通过且已勾选」的 SKU 行，规格与价格库存保持现状");
    return plan;
  }
  if (dims.length === 0) {
    plan.skipped.push("平台未返回该类目的规格类型，无法自动建规格（请人工选择规格类型）");
    return plan;
  }

  //: ⚠️ 用户口径 2026-10-20：映射左侧 = **预检通过列名** ⇒ 候选直接取
  //: `mapping` 的键（用户选过的列），不再从 `specName` 派生。
  //: 「有映射」就是用户明确说过「这一列要导入」，比任何名称推断都可靠。
  const sourceNames: string[] = Object.keys(mapping).filter((k) => k.trim() !== "");
  if (sourceNames.length === 0) {
    plan.skipped.push("还没有配置「商品 SKU 映射关系」，规格与价格库存保持现状");
    return plan;
  }

  //: ⚠️ 用户口径 2026-10-20：映射左侧已是**预检通过列名**（不再是规格分类名）。
  //: ⇒ `mapping` 的键是**列名**，值是平台维度名。没配映射的列 ⇒ 不导入
  //: （宁可漏填，也不把值填错维度）。
  const usable = sourceNames.filter((name) => {
    const target = mapping[name];
    return Boolean(target) && dims.some((d) => d.name === target);
  });
  if (usable.length === 0) {
    plan.skipped.push(
      `平台规格类型里没有与所选列 ${sourceNames.map((n) => `「${n}」`).join("、")} 对应的维度` +
        `（平台有：${dims.map((d) => d.name).join("、") || "无"}）——` +
        "请在导入弹窗里配置规格关系后重试",
    );
    return plan;
  }

  //: 每个被映射的列 → 它的平台维度 + 该维度下要填的值（逐维度独立）。
  type DimPlan = { dim: PlatformSpecDim; sourceName: string; values: SkuValuePlan[] };
  const perDim = new Map<string, DimPlan>();
  for (const name of usable) {
    const dim = dims.find((d) => d.name === mapping[name]);
    if (dim && !perDim.has(dim.name)) {
      perDim.set(dim.name, { dim, sourceName: name, values: [] });
    }
  }

  //: 逐行建「组合文本」：把每个维度各自的值拼成 `维度名:值 / 维度名:值`。
  //: ⚠️ 必须与第二步 `spec_combinations` 的**同形**（同一组顺序、` / ` 分隔），
  //: 否则价格表按 key 对齐时对不上（行会凭空多出来 / 已填值丢失）。
  //:
  //: ⚠️⚠️ 用户报 2026-10-21：「商品规格映射分组应与**商品 SKU 映射关系配置顺序**一致，
  //: 现在是顺序是倒的」——
  //: 原实现 `dims.map(...)` 按**平台规格类型**的顺序排维度，与用户在弹窗里
  //: 配映射的顺序无关 ⇒ 用户配「颜色分类 → 尺寸」，导出却按平台序（常是倒的）。
  //: ⇒ 顺序改为**用户映射的配置顺序**（`usable` = 已配来源的列，按行序）。
  //: ⚠️ 第二步的 `specTypes` 也必须用同一顺序（页面按 `plan.dims` 落 `specTypes`），
  //: 否则两处 `spec` 字符串不同形、价格库存行对不上。
  const dimOrder = usable
    .map((name) => mapping[name])
    .filter((n): n is string => !!n && perDim.has(n));
  const seenValues = new Map<string, Set<string>>(dimOrder.map((n) => [n, new Set<string>()]));
  /** 已经推过规格图的「维度+值」（用户口径 2026-10-21：**按规格值去重**，不按行）。 */
  const pushedSpecImages = new Set<string>();
  const rawRows = new Map<number, {
    price: number | null;
    marketPrice: number | null;
    stock: number | null;
    sellerCode: string;
    barcode: string;
    //: 固定列（用户口径 2026-10-21：当前库存 / 规格ID 也走映射）。
    currentStock: number | null;
    specId: string;
    parts: { dimName: string; value: string }[];
    /** 行级规格文本（商品 SKU 映射那几列拼接；用户口径 2026-10-21）。 */
    specText?: string;
  }>();
  //: 行级规格文本去重（同一规格重复出现只取第一条，并说明）。
  const perfSeen = new Set<string>();

  rows.forEach((row, index) => {
    //: ⚠️ 用户口径 2026-10-20：映射左侧是**列名**（不再是规格分类名）⇒
    //: 一行里可能有**多个**被映射的列（如「颜色」列 +「尺寸」列），要**全取**。
    //: 旧写法只取 `row.specValue` 一个值 ⇒ 用户选第二列时**静默取不到**。
    const hitEntries: { entry: DimPlan; raw: string }[] = [];
    for (const colName of usable) {
      const entry = perDim.get(mapping[colName] ?? "");
      if (!entry) continue;
      //: 按**列名**取本行值（`cells` 是 `passedSkuRows` 摊平好的整行）。
      //: ⚠️ **不做任何兜底**：映射左侧是列名，就只按列名取 ——
      //: 回退到 `specValue` 会把「用户没选的那一列」的值填进规格，是静默填错。
      const raw = String(row.cells?.[colName] ?? "").trim();
      if (!raw) continue;
      hitEntries.push({ entry, raw });
    }
    if (hitEntries.length === 0) {
      //: 所选列在本行全空 ⇒ 跳过并**说明原因**（静默跳过比不填更糟）。
      plan.skipped.push(`「${row.rawName || "(空名称)"}」所选列没有值，已跳过该行`);
      return;
    }
    //: 按数据包**行**归组（同一行的多个维度合成一个 spec 组合）。
    const slot = rawRows.get(index) ?? {
      price: null, marketPrice: null, stock: null, sellerCode: "", barcode: "",
      //: ⚠️ 这两个字段必须与 `rawRows` 的 Map 值类型**完全一致**，
      //: 否则 TS 会把字面量类型推到「缺字段」那一支，后面 `slot.specText` 全报错。
      currentStock: null, specId: "", parts: [],
    };
    //: ⚠️ **2026-10-21 修的真问题**：同一列里同一个值会出现在**多行**上
    //: （本数据包 `颜色分类=奇遇童话01` 有 ~19 行），早先按值去重写进
    //: `entry.values` 只会留下 24 个值、而售价/库存是**逐行**不同的 ⇒
    //: 「颜色 24 × 尺寸 19 = 456 个组合，价格只来自其中 24 行」这种对不上的形态。
    //: ⇒ 售价 / 库存 / 商家编码 / 条形码 **按行取**，键 = 该行拼接出的完整规格文本
    //:   （见 `skuSpecTextOf`，与 `spec_combinations` 同形），不再按维度值去重合并。
    const specText = skuSpecTextOf(row.cells ?? {}, specColumns);
    slot.specText = specText;
    for (const { entry, raw } of hitEntries) {
      const hit = matchSpecValue(raw, entry.dim);
      if (hit.match === "unusable") {
        plan.skipped.push(hit.reason);
        continue;
      }
      //: ⚠️ **值去重**（用户口径 2026-10-20：「实际导入规格时需**对选择的列数据进行
      //: 去重处理**」）—— 同一维度下同名值只出现一次。这是**导入时**去重，
      //: 不是选列时去重：左侧清单保持原样，用户能看到全部列。
      const seen = seenValues.get(entry.dim.name)!;
      const key = normalizeSkuKey(hit.value);
      if (!seen.has(key)) {
        seen.add(key);
        entry.values.push({
          value: hit.value, rawValue: raw, valueId: hit.valueId,
          match: hit.match, reason: "",
        });
      }
      slot.parts.push({ dimName: entry.dim.name, value: hit.value });
    }
    if (slot.parts.length === 0) return;   //: 一个维度都没匹配上 ⇒ 这行不建组合。
    //: ⚠️ 用户口径 2026-10-11（最终）：「其它规则中应是**市场价**为原价的 1.7 倍
    //: 支持调整，**不是售价**」⇒ 售价 = 原价**原值**；市场价 = 原价 × 倍率。
    slot.price = row.price;
    const base = row.originalPrice;
    slot.marketPrice =
      base !== null && base !== undefined ? applyPriceMultiplier(base, priceMultiplier) : null;
    slot.stock = row.stock;
    slot.sellerCode = row.sellerCode;
    //: ⚠️ 条形码同样**走映射**（`passedSkuRows` 已按映射取好）。
    slot.barcode = row.barcode ?? "";
    //: ⚠️⚠️ **当前库存 / 规格ID 也必须在这里落**（用户口径 2026-10-21：
    //: 「当前库存、规格ID 列配置的映射没有映射成功」）。
    //:
    //:  根因：`passedSkuRows` 早就按映射取好了这两个值，但本函数**只给槽位
    //:  初始化为 null/""、从未赋值** ⇒ `plan.rows[].currentStock` 恒 null、
    //:  `specId` 恒 ""，页面落值那步自然拿不到（售价/库存/条形码都不受影响，
    //:  所以看起来"只有这两列没映射成功"，极易误判成映射配置问题）。
    //:  ⇒ 与 `barcode` 同一条规则：**有就带上、没有保持 null/""**（不造假）。
    slot.currentStock = row.currentStock ?? null;
    slot.specId = row.specId ?? "";
    rawRows.set(index, slot);

    //: ⚠️ **规格图走映射**（用户口径 2026-10-15：「不再走现在默认的逻辑，
    //: 应走映射逻辑」）——来源 = `spec_image` 那条映射取到的值（`row.specImageRel`）。
    //: ⚠️ 旧实现取 `row.localRel`（代码按 SKU 名称**猜**出来的那张图）——
    //: 那条路已随「规格图也走映射」退场。
    //: ⚠️ 文件名从路径末段取（上传时按内容 MD5 重命名，这里只作 `origin_name`）。
    //: ⚠️⚠️ 用户口径 2026-10-21（真机 456 行报「上传规格图 25/455」后定论）：
    //: 「**导入规格图应只在第一个组需要上传，且是去重后的数据行对应规格图**」——
    //: 规格图挂的是**规格值**（颜色分类 24 个），不是 SKU **行**（456 行）。
    //: 旧实现按行 push ⇒ 456 行就要传 456 张，同一张图重复传 19 次
    //: （19 = 该颜色下的尺寸数），进度显示 `25/455` 且白等几十秒。
    //: ⇒ 只收**第一个维度**的**首次出现**（同一个规格值只留第一张），
    //: 后面的行若该值已有图就不再加。
    if (row.specImageRel) {
      const first = hitEntries[0];
      const dimName = first.entry.dim.name;
      const value = slot.parts[0]?.value ?? "";
      //: 规格名称由用户配置的映射列决定（`dimName` / `value` 来自映射关系），
      //: 代码不写死任何规格名 —— 用户口径：「规格名称不是固定死的是用户配置的映射列」。
      //: ⇒ 去重键 = 第一维度的「维度名 + 值」（即该映射列下的规格名称）。
      const imgKey = `${dimName}\u0000${value}`;
      if (!pushedSpecImages.has(imgKey)) {
        pushedSpecImages.add(imgKey);
        plan.images.push({
          dimName,
          value,
          localRel: row.specImageRel,
          localFileName: row.specImageRel.split("/").pop() ?? "",
        });
      }
    }
  });

  //: 落 dims（保序 = `dims` 的平台顺序，与 `spec_combinations` 一致）。
  for (const dimName of dimOrder) {
    const entry = perDim.get(dimName)!;
    if (entry.values.length === 0) continue;
    plan.dims.push({
      dimName, dimId: entry.dim.id, sourceName: entry.sourceName, values: entry.values,
    });
    plan.notes.push(
      entry.sourceName === dimName
        ? `规格名一致（文本精确匹配）：${entry.sourceName} → 平台「${dimName}」`
        : `按你指定的映射：${entry.sourceName} → 平台「${dimName}」`,
    );
    const standard = entry.values.filter((v) => v.match === "standard").length;
    plan.notes.push(
      `「${dimName}」规格值 ${entry.values.length} 个：命中平台候选 ${standard} 个` +
        `${entry.values.length - standard > 0 ? `，按原值自定义 ${entry.values.length - standard} 个` : ""}`,
    );
  }

  //: 落 rows：按 `dimOrder` 拼组合文本（顺序必须与 `spec_combinations` 一致）。
  for (const slot of rawRows.values()) {
    if (slot.parts.length === 0) continue;
    const ordered = dimOrder
      .map((n) => slot.parts.find((p) => p.dimName === n))
      .filter((p): p is { dimName: string; value: string } => Boolean(p));
    //: 多维度必须**每个维度都有值**才是合法组合（与 `spec_combinations` 同判据）。
    if (ordered.length !== dimOrder.length) continue;
    //: ⚠️ **键 = 该行拼接出的完整规格文本**（用户口径 2026-10-21）。
    //: 原实现拼 `${dimName}:${value}` —— 它按**维度值**标识，同一组 (颜色,尺寸)
    //: 只会产生**一行**价格；而这个数据包里 456 行的售价/库存**逐行不同**，
    //: 按维度值合并 = 444 行价格丢失（只留 24 行）。
    //: ⇒ 有 `specColumns` 时用行级规格文本作键；否则退回旧行为。
    const spec =
      slot.specText && specColumns.length > 0
        ? slot.specText
        : ordered.map((p) => `${p.dimName}:${p.value}`).join(" / ");
    //: ⚠️ 同一行级规格重复出现（真包 456 行组合无重复，但别的包可能有）
    //: ⇒ **保留第一条**并记一条 note，不静默覆盖（覆盖会让后一行的价格吃掉前一行）。
    if (perfSeen.has(spec)) {
      plan.skipped.push(`规格「${spec}」出现多次，只取第一行的价格库存`);
      continue;
    }
    perfSeen.add(spec);
    plan.rows.push({
      spec,
      price: slot.price,
      marketPrice: slot.marketPrice,
      stock: slot.stock,
      sellerCode: slot.sellerCode,
      barcode: slot.barcode,
      currentStock: slot.currentStock,
      specId: slot.specId,
    });
  }

  if (plan.dims.length === 0) {
    plan.skipped.push("没有任何规格值可导入（值可能都不符合平台候选且不可自定义）");
  }
  return plan;
}

/** 从预检表格 + 勾选态导出**「预检通过且已勾选」的 SKU 行**（供第 9 步填充）。
 *
 * ⚠️ **单一实现**：与 `passedSkuSpecs`（第二步过滤规格用）**同一套判据** ——
 * 两处若各写一份，第一步勾着的规格会在第二步的某一处消失（DEF-044 同型）。
 * 本函数是 `passedSkuSpecs` 的超集：后者只要 `spec` 集合，本函数要**整行数据**
 * （价格 / 库存 / 商家编码 / 本地图），因此共用 `blocked`/`picked` 与列定位函数。
 *
 * 三条口径（用户拍板 2026-10-11）：
 *   · 价格 = `原价`（**优先**）→ 空则 `计算价格`；
 *   · 库存 = `库存`；商家编码 = `ID` 列；
 *   · 本地图 = 该行按名称对齐到的 `SKU/` 里的图（`localImages`，即**本地图列**那张）。
 */
export function passedSkuRows(
  table: PrecheckTable | null | undefined,
  blockedRows: readonly number[],
  pickedRows: readonly number[],
  localImages: readonly { key: string; relPath: string }[] = [],
  /** **价格库存列映射**（用户口径 2026-10-11：动态可调整；默认 = 现行逻辑）。
   *  缺省用 `DEFAULT_PRICE_MAPPING`（原价 / 库存 / ID）。 */
  priceMapping: readonly PriceMappingRow[] = DEFAULT_PRICE_MAPPING,
  /** **商品 SKU 映射指定的列**（用户口径 2026-10-21：这几列就是规格）。
   *
   *  ⚠️ 价格库存表里已**没有**「SKU 名称」这一行 ⇒ 这一列从哪来？
   *  答案在这里：用商品 SKU 映射的**第一列**作为「一行一个 SKU」的定位列
   *  （`skuNameColumnIndex` 的第 3 参兜底）。
   *  ⚠️ 它只用于「**这一行算不算一条 SKU**」与「空行跳过」，
   *  不再当作「规格分类:规格值」去拆（拆分已整条退场）。
   */
  specColumns: readonly string[] = [],
): PassedSkuRow[] {
  if (!table || !table.columns?.length) return [];
  //: ⚠️ SKU 名称列**优先走映射**（用户把「SKU 名称」映射到哪列，就用哪列）；
  //: 没配才退到商品 SKU 映射的第一列（用户口径 2026-10-21）。
  const nameCol = skuNameColumnIndex(table.columns, priceMapping, specColumns);
  if (nameCol < 0) return [];

  const columns = table.columns;
  const priceCol = sourceColumnIndex(columns, priceMapping, "price");
  const stockCol = sourceColumnIndex(columns, priceMapping, "stock");
  const codeCol = sourceColumnIndex(columns, priceMapping, "seller_code");
  //: ⚠️ **条形码**（用户口径 2026-10-15：下拉漏了它；后端一直认 `barcode`）。
  const barcodeCol = sourceColumnIndex(columns, priceMapping, "barcode");
  //: ⚠️ **当前库存 / 规格ID**（用户口径 2026-10-21：「价格及库存**非动态列**
  //: 均需要用户配置映射」「售价、**当前库存**、改后库存、市场价均必须配置」，
  //: 且「缺少规格ID…固定列必须保持一致」）。
  //: 两者都按**普通映射列**取值（与 barcode 同一条路），取不到 ⇒ 空（不造假）。
  const currentStockCol = sourceColumnIndex(columns, priceMapping, "current_stock");
  const specIdCol = sourceColumnIndex(columns, priceMapping, "spec_id");
  //: ⚠️ **SKU 图 = 固定列的「SKU 图」**（用户口径 2026-10-21，取代 2026-10-15 的
  //: 「规格图也走映射」）—— 不再从 `priceMapping` 里找来源列。
  const specImageSourceCol = columns.findIndex((c) => String(c).trim() === SKU_IMAGE_COL);
  //: ⚠️ **回落列也走映射**（`price_fallback`）—— 此前按裸列名查「计算价格」，
  //: 是写死逻辑（用户口径 2026-10-15：所有点均走映射）。
  const fallbackCol = sourceColumnIndex(columns, priceMapping, "price_fallback");

  const blocked = new Set(blockedRows);
  const picked = new Set(pickedRows);
  //: ⚠️ **空 `picked` = 全选**（用户口径「商品 sku 表格中的所有列默认全选」）。
  //: 为什么必须这样：勾选态住在 `TablesPanel` 内部、靠 `SelectionReporter` 上抛，
  //: 而那个面板**只在「媒体资源」页签挂载**。用户若直接进第二步、从没打开过页签，
  //: 上抛永远不发生 ⇒ `picked` 恒为 `[]` ⇒ 一行都导不出 ⇒
  //: 「预检通过SKU导入」按钮恒灰（用户实测的 bug）。
  const pickedAll = pickedRows.length === 0;
  const out: PassedSkuRow[] = [];
  (table.rows ?? []).forEach((row, index) => {
    if (blocked.has(index)) return;      //: 预检未通过（**这个不因空集而放开**）
    if (!pickedAll && !picked.has(index)) return;  //: 用户显式取消勾选的行
    const rawName = String(row[nameCol] ?? "").trim();
    if (!rawName) return;
    //: ⚠️ **规格分类不由代码猜**（用户口径 2026-10-20，逐字）：
    //: 「统一改为从 sku 名称按冒号取前缀（没有就使用单维度名）**这个是错的，
    //:   不要改成这个逻辑，用户使用规则自己控制**」。
    //:
    //: ⇒ 代码**不做任何名称形态推断**。分类从哪来？由用户在自己配置的
    //: 「数据包规则」里决定（如 `copy_extract` 从 sku 名称按分隔符取段），
    //: 规则跑完写成表格里的一列，用户再把那一列选进映射。规则是唯一真相源。
    //:
    //: ⚠️ 因此这里**退回单一维度** `SINGLE_SPEC_NAME`：它是「没有分类」时的
    //: 中性占位，用户在映射弹窗里自己指定平台规格类型即可 ——
    //: 宁可让用户显式配一次，也不要代码替他猜错一个分类名。
    const specName = SINGLE_SPEC_NAME;
    //: 值 = 完整 sku 名称（不做任何切分：前缀是用户的事，不是代码的事）。
    const specValue = rawName;
    //: 本地图列那张（按 SKU 名称对齐）—— 用户口径「上传本地图」。
    const relPath = lookupLocalSkuImage(localImages, rawName);
    const original = priceCol >= 0 ? firstNumber(row[priceCol]) : null;

    out.push({
      rawName,
      specName,
      specValue,
      originalPrice: original,
      //: ⚠️ 回落列只在**原价取不到**时用（用户口径「原价优先」）。
      price: original ?? (fallbackCol >= 0 ? firstNumber(row[fallbackCol]) : null),
      stock: stockCol >= 0 ? firstNumber(row[stockCol]) : null,
      sellerCode: codeCol >= 0 ? String(row[codeCol] ?? "").trim() : "",
      //: ⚠️ **没配映射 / 缺列 ⇒ 整个键不给**（不是空串）——
      //: 空串会被当成「填了但非法」，平台可能直接拒；不给键才是「没填」。
      ...(barcodeCol >= 0 ? { barcode: String(row[barcodeCol] ?? "").trim() } : {}),
      //: **当前库存 / 规格ID**：与 barcode 同一条路（配了映射才给键，不造假）。
      ...(currentStockCol >= 0
        ? { currentStock: firstNumber(row[currentStockCol]) }
        : {}),
      ...(specIdCol >= 0 ? { specId: String(row[specIdCol] ?? "").trim() } : {}),
      localRel: relPath,
      localFileName: relPath ? relPath.split("/").pop() ?? "" : "",
      //: ⚠️ **规格图走映射**（用户口径 2026-10-15：「不再走现在默认的逻辑，
      //: 应走映射逻辑」）——来源列 = `spec_image` 那条映射指的那一列。
      //: ⚠️ 它可能是「SKU 图」虚拟列（值在 `skuImageByRow` 里，不在 `row` 里）
      //: 或某个真实列（值就在 `row` 里）—— 两条路都覆盖。
      //: ⚠️ 「SKU 图」现在是**真列** ⇒ 与别的列一样按 `row[colIndex]` 取。
      specImageRel: specImageSourceCol >= 0
        ? String(row[specImageSourceCol] ?? "").trim()
        : null,
      //: 整行原始值（按列名索引）—— 供「按用户选的列取值 + 去重」用。
      cells: cellsOfRow(table.columns, row),
    });
  });
  return out;
}

/** 把一行按**列名**摊平成 `{列名: 值}`（值统一 trim 成字符串）。
 *
 *  ⚠️ 用列名而不是下标：映射里存的是列名，用户换数据包后列顺序会变，
 *  下标会**静默错位**（把「价格」当「库存」导入）。
 */
function cellsOfRow(
  columns: readonly string[],
  row: readonly (string | null)[],
): Record<string, string> {
  const out: Record<string, string> = {};
  columns.forEach((name, i) => {
    out[String(name)] = String(row[i] ?? "").trim();
  });
  return out;
}

/** 取第一个能解析的数（用于「原价优先、空则计算价格」）。 */
function firstNumber(...candidates: unknown[]): number | null {
  for (const raw of candidates) {
    if (raw === null || raw === undefined) continue;
    const text = String(raw).trim().replace(/[,\s￥¥]/g, "");
    if (!text) continue;
    const n = Number(text);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/** 价格口径：**原价优先**，空才回落计算价格（用户口径 ②）。 */
export function firstPrice(original: unknown, computed: unknown): number | null {
  return firstNumber(original) ?? firstNumber(computed);
}

/** 在列名里找一个（多别名，大小写不敏感）—— 与批量修改共用同一套别名口径。 */
export function findColumnIndex(columns: readonly string[], aliases: readonly string[]): number {
  const wanted = aliases.map((a) => a.toLowerCase());
  return columns.findIndex((c) => wanted.includes(String(c).trim().toLowerCase()));
}

/** 规格映射的**默认值**：**文本精确匹配**（用户口径 2026-10-11）。
 *
 * 同名 ⇒ 选中它；没有同名 ⇒ `""`（**不猜**，让用户在弹窗里手选）。
 * 返回 `{ 数据包规格分类名: 平台维度名 | "" }`。
 */
export function defaultSpecMapping(
  sourceNames: readonly string[],
  dims: readonly PlatformSpecDim[],
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const name of sourceNames) {
    const hit = dims.find((d) => d.name.trim() === name.trim());
    out[name] = hit ? hit.name : "";
  }
  return out;
}

/** 映射是否**齐全**（每一项都指向一个存在的平台维度）—— 弹窗据此禁用「确认」。 */
export function isSpecMappingComplete(
  mapping: Readonly<Record<string, string>>,
  dims: readonly PlatformSpecDim[],
): boolean {
  const names = new Set(dims.map((d) => d.name));
  const entries = Object.entries(mapping);
  if (entries.length === 0) return false;
  return entries.every(([, target]) => target !== "" && names.has(target));
}

/** **售价倍率**默认值（用户口径 2026-10-11）：
 *  「商品售价规则是在价格与库存的**原价列**基础上默认 1.7 倍，参数可调整」。
 *
 *  ⚠️ 语义澄清（避免歧义）：这里的「原价列」指**第一步预检 SKU 表的 `原价` 列**
 *  （数据包的进货/成本价，真值 1280–4980）；乘 1.7 得到的是**第二步的售价**
 *  （`PriceRow.price`）。不是第二步价格表里那个「市场价」（`market_price`）。
 */
export const DEFAULT_PRICE_MULTIPLIER = 1.7;

/** 倍率合法区间（下限 1：不打折卖；上限 10：挡住手滑多打一个 0）。 */
export const PRICE_MULTIPLIER_MIN = 1;
export const PRICE_MULTIPLIER_MAX = 10;

/** 按倍率算售价：`原价 × 倍率`，保留 **2 位小数**（平台价格口径）。
 *
 *  缺值口径：原价取不到 ⇒ `null`（不造假、不填 0）。
 *  四舍五入到分（`Math.round(n * 100) / 100`）—— 浮点误差会让 `1280 × 1.7`
 *  变成 `2175.9999999999995`，直接写进价格表会被平台判为非法精度。
 */
export function applyPriceMultiplier(
  original: number | null,
  multiplier: number,
): number | null {
  if (original === null || !Number.isFinite(original)) return null;
  const k = Number.isFinite(multiplier) && multiplier > 0 ? multiplier : 1;
  return Math.round(original * k * 100) / 100;
}

/** 把倍率夹到合法区间（弹窗输入框用；非法值 ⇒ 回默认 1.7）。 */
export function clampPriceMultiplier(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return DEFAULT_PRICE_MULTIPLIER;
  return Math.min(PRICE_MULTIPLIER_MAX, Math.max(PRICE_MULTIPLIER_MIN, n));
}

/* ------------------------------------------------------------------ *
 * 规格映射（用户口径 2026-10-11 最终版）：**成对的关系行**，最多两行。
 *
 * 用户原话（列名 + 约束）：
 *   · 两列文本改为「**预选通过规格类型**」与「**平台规格类型**」；
 *   · **两列均不支持重复选择**；
 *   · **最多可添加两行**规格关系；
 *   · 该两列**必填、不能为空、不能重复**。
 *
 * ⚠️ 为什么从「映射表」改成「成对行」：数据包可能有多个规格分类（多维度），
 * 而平台最多也支持 2 个规格类型 ⇒ 需要**一一配对**，而不是把左侧拍平成一列。
 * ------------------------------------------------------------------ */

/** 一行规格关系：预选通过规格类型 ↔ 平台规格类型。 */
export type SpecMappingRow = {
  /** 左列：**预选通过规格类型**（数据包里的规格分类名，如「颜色分类」）。 */
  source: string;
  /** 右列：**平台规格类型**（平台类目下发，如「颜色」）。 */
  target: string;
};

/** 规格映射**最多两行**（真机：平台最多选 2 个规格类型 —— 见 `SPEC_RULE`）。 */
export const MAX_SPEC_MAPPING_ROWS = 2;

/** 校验一整份规格映射，返回**逐行**的错误（空数组 = 全部合法）。
 *
 * 判据（用户口径）：
 *   ① **必填**：任一侧为空 ⇒ 报错（「不能为空」）；
 *   ② **不重复**：左列、右列**各自**都不许重复（「不支持重复选择」）；
 *   ③ 行数上限：> `MAX_SPEC_MAPPING_ROWS` ⇒ 报错（「最多两行」）。
 *
 * ⚠️ 只报**非空行**的重复：一行还没填完时不该因为「两行都空」而报重复
 * （那会让用户刚点「添加」就看见红字）。
 */
export function validateSpecMapping(rows: readonly SpecMappingRow[]): string[] {
  const errors: string[] = [];
  const rowLimit = rows.length > MAX_SPEC_MAPPING_ROWS ? `最多 ${MAX_SPEC_MAPPING_ROWS} 行` : "";

  const empties: number[] = [];
  const dupSource: string[] = [];
  const dupTarget: string[] = [];
  const seenSource = new Set<string>();
  const seenTarget = new Set<string>();
  rows.forEach((row, i) => {
    const source = row.source.trim();
    const target = row.target.trim();
    if (!source && !target) return;   //: 整行未填：不报（刚添加的空行）
    if (!source || !target) empties.push(i + 1);
    if (source) {
      if (seenSource.has(source) && !dupSource.includes(source)) dupSource.push(source);
      seenSource.add(source);
    }
    if (target) {
      if (seenTarget.has(target) && !dupTarget.includes(target)) dupTarget.push(target);
      seenTarget.add(target);
    }
  });
  //: 用户口径「只保留核心」⇒ 每类一条、不带行号。
  const parts2: string[] = [];
  if (rowLimit) parts2.push(rowLimit);
  if (empties.length > 0) parts2.push("两列均不能为空");
  if (dupSource.length > 0) parts2.push(`预选通过规格类型重复：${dupSource.join("、")}`);
  if (dupTarget.length > 0) parts2.push(`平台规格类型重复：${dupTarget.join("、")}`);
  //: 同样**合并成一行**（用户口径「放一行提示」）。
  if (parts2.length > 0) errors.push(parts2.join("；"));
  return errors;
}

/** 映射是否可以提交（无错误**且**至少有一行填完整）。 */
export function isSpecMappingReady(rows: readonly SpecMappingRow[]): boolean {
  const filled = rows.filter((r) => r.source.trim() !== "" && r.target.trim() !== "");
  return filled.length > 0 && validateSpecMapping(rows).length === 0;
}

/** 两行映射 ⇒ `数据包规格分类名 → 平台维度名` 的查表结构（供 `buildSkuFillPlan` 用）。 */
export function mappingFromRows(
  rows: readonly SpecMappingRow[],
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const source = row.source.trim();
    const target = row.target.trim();
    if (source && target) out[source] = target;
  }
  return out;
}

/** 默认映射行：按**文本精确匹配**给右侧，最多取 `MAX_SPEC_MAPPING_ROWS` 行。
 *
 *  ⚠️ 用户口径「默认文本精确匹配」：左侧取数据包里的规格分类名（保序），
 *  右侧找**同名**的平台维度；找不到 ⇒ 右侧留空（**不猜**，让用户手选）。
 */
export function defaultSpecMappingRows(
  sourceNames: readonly string[],
  dims: readonly PlatformSpecDim[],
): SpecMappingRow[] {
  return sourceNames.slice(0, MAX_SPEC_MAPPING_ROWS).map((name) => {
    const hit = dims.find((d) => d.name.trim() === name.trim());
    return { source: name, target: hit ? hit.name : "" };
  });
}

/* ------------------------------------------------------------------ *
 * 价格库存**列映射**（用户口径 2026-10-11）：
 * 「将价格库存列表与预检通过的列也配置成动态可调整的，当前映射逻辑配置的为默认逻辑」
 *
 * 即：不再写死「原价 → 售价、库存 → 库存、ID → 商家编码」，
 * 而是让用户在弹窗里给每个**目标列**挑一个**来源列**；默认值仍是那套。
 * ------------------------------------------------------------------ */

/** 价格库存表里**可被映射**的目标列。 */
export type PriceTargetKey =
  | "price"
  //: ⚠️ **当前库存**（千帆固定列，只读回显；用户口径：也要能配映射）
  | "current_stock"
  | "stock"
  | "seller_code"
  | "market_price"
  //: ⚠️ 平台提交体认 `skuData[].barcode`（后端一直支持，此前下拉漏了它）。
  | "barcode"
  //: ⚠️ **规格ID**（千帆固定列 `规格ID`，用户口径 2026-10-21：
  //: 「缺少规格ID…价格及库存固定列必须保持一致」）——
  //: 它是**平台生成的 SKU 标识**，我方不生成，但用户可能想从数据包某列映射进来
  //: （例如数据包里已有卖家侧规格 ID，要一并带过去对账）。
  | "spec_id"
  //: ⚠️ **SKU 名称列**（用户口径 2026-10-15：「预检阶段的派生判定（哪列是 SKU 名、
  //: 哪列是线上图）**应走映射**」）。
  //: 此前它是靠**别名猜**（`sku名称` / `规格名` / `颜色分类`…）—— 数据包列名一换就猜空，
  //: 而猜空的后果是整表读不出 SKU 名（缓存里看起来「这包没数据」，用户无从下手）。
  | "sku_name"
  //: **回落列**（`原价` 取不到时用；不乘倍率）。
  | "price_fallback"
  //: **规格分类列**（第一步那张固定列；它本身也是可映射的目标）。
  | "spec_category"
  //: **规格图**（用户口径 2026-10-15：「右侧的价格库存中，**SKU图**、售价、市场价、
  //: 库存所有必须配置」）—— 目标 = 该行的规格图，来源 = 表格里的「SKU 图」列
  //: 或别的列。⚠️ 此前它**不在映射表里**，走的是「本地图匹配」的默认推导
  //: （用户口径：不再走现在默认的逻辑，**应走映射逻辑**）。
  | "spec_image"
  ;

/** 目标列的中文名（弹窗里显示）。 */
export const PRICE_TARGET_LABELS: Record<PriceTargetKey, string> = {
  price: "售价",
  //: 当前库存（线上现值）
  current_stock: "当前库存",
  market_price: "市场价",
  //: ⚠️ 用户口径 2026-10-21（真机 CDP 抓包，设计 24 §23）：平台把库存拆成
  //: **当前库存**（线上现值，只读回显）+ **改后库存**（本次提交的新值）。
  //: 能映射的只有**改后库存** —— `当前库存` 是平台给的，不是我们写的。
  //: 标签写「改后库存」与平台表头逐字一致，用户对着两边看得懂。
  stock: "改后库存",
  seller_code: "商家编码",
  sku_name: "SKU 名称",
  price_fallback: "售价回落列",
  spec_category: "规格分类",
  spec_image: "SKU图",
  //: ⚠️ **条形码**（用户口径 2026-10-15：「价格库存列下拉不完整，**条形码列没有**」）。
  //: 平台提交体认的是 `skuData[].barcode`（`shop_item_publish_service.py:234`）——
  //: 后端一直支持，**只是下拉里没给** ⇒ 用户永远配不出条形码映射。
  barcode: "条形码",
  spec_id: "规格ID",
};

/** 目标列下拉的候选（**两列都自由选择** —— 用户口径 2026-10-11）。 */
/*: ⚠️ `PRICE_TARGET_OPTIONS` **已删除**（2026-10-21）——
 *  它与 `PRICE_TABLE_TARGETS` 是同一件事的两份清单，上一轮只改了其中一份
 *  ⇒ 下拉读的是另一份 ⇒ 用户看到的还是旧选项（`current_stock` 甚至以英文键名显示）。
 *  ⇒ 唯一清单 = `PRICE_TABLE_TARGETS`，所有引用直接用它。 */

/** ⚠️ **价格及库存表真正有的列**（= 表头列的 target 子集）。
 *
 *  用户报障 2026-10-15：
 *  > 「价格库存映射关系表区域的**价格库存列下拉框**与**价格及库存**存在
 *  >   **不一致的列**，应保持一致」
 *
 *  ⚠️ 根因：下拉直接用了 `PRICE_TARGET_OPTIONS` **全量** —— 那是「导入所需的
 *  target **总集**」，不是「价格库存表的列清单」。多出来两个：
 *    · `price_fallback`（售价回落列）—— 那是**规则参数**（原价取不到时的回落），
 *      住在「其它规则」区，**不是这张表的列**；
 *    · `spec_category`（规格分类）—— 那是**商品 SKU 映射**的来源，同上。
 *
 *  ⚠️ 反过来 `sku_name` / `spec_image` 也**不在**表头里，但它们**必须**留在
 *  这个下拉里 —— 导入要拿它们去取「SKU 名称」「SKU 图」（用户口径 2026-10-15：
 *  「SKU 名称也加入预置的必修行，它确实是导入必须的」）。所以这份清单 =
 *  表头列 ∪ 导入必需来源，**不含**规则参数类 target。
 */
export const PRICE_TABLE_TARGETS: PriceTargetKey[] = [
  //: ⚠️⚠️ 这是**导入弹窗下拉真正用的清单**（`publish-config-step2.tsx` 读它）。
  //: 用户口径 2026-10-21（截图为证）：
  //:   「**移除下拉中的 SKU 图和 SKU 名称**」「价格库存列的下拉框中应包含
  //:    **当前价格及库存全部固定列**」——
  //:
  //: ⇒ 必须与千帆价格及库存表的**非动态列**逐个对应（真机 CDP 实读，设计 24 §23）：
  //:   售价 / 当前库存 / 改后库存 / 市场价 / 规格ID / 条形码 / 商家编码
  //:
  //: ⚠️ **教训**：上一轮改的是 `PRICE_TARGET_OPTIONS`，而下拉读的是**这份**清单
  //: （两份清单并存 = 改了没生效，且 `current_stock` 没进这份清单时
  //:  会把**英文键名**直接显示给用户）。现在两份内容一致，且这份是唯一数据源
  //: （`PRICE_TARGET_OPTIONS` 已改为引用它，见下方）。
  "price",         // 售价
  "current_stock", // 当前库存
  "stock",         // 改后库存
  "market_price",  // 市场价
  "spec_id",       // 规格ID
  "barcode",       // 条形码
  "seller_code",   // 商家编码
];

/** 一行价格库存映射：目标列 ← 来源列（预检表格里的列名，`""` = 不填）。 */
export type PriceMappingRow = {
  /** 目标列（用户自由选择）。⚠️ **可为空** —— 新添加的行默认两列都空
   *  （用户口径 2026-10-11：「点击添加行，添加的行应为空**不设置默认值**」）。 */
  target: PriceTargetKey | "";
  /** 来源列名（预检表格的**任意列**）。空 = 未选。 */
  source: string;
};

/** **默认列映射**（用户口径：「当前映射逻辑配置的**为默认逻辑**」）。
 *
 *  即把现行写死的那套表达成数据：
 *    · 售价 ← `原价`（**优先**；为空时回落 `计算价格` —— 见 `fallbackSource`）
 *    · 库存 ← `库存`
 *    · 商家编码 ← `ID`
 *
 *  ⚠️ 只有「售价」有**回落列**：原价空是数据常态（真包 26/26 有值、但其它包可能空），
 *  而库存/商家编码空就是空（不猜、不拿别的列顶替）。
 */
export const DEFAULT_PRICE_MAPPING: readonly PriceMappingRow[] = [];

/** **弹窗打开时预置的行**（用户口径 2026-10-15）：
 *
 *  > 「右侧的价格库存中，**SKU图、售价、市场价、库存**所有必须配置，
 *  >   弹窗打开时就**默认展示**，它们对应的**左侧下拉默认为空**展示『请选择』文本」
 *  ⚠️ 另加 **`sku_name`**（用户口径 2026-10-15：「把『SKU 名称』也加入预置的必修行
 *  （变成 5 行）—— 它确实是导入必须的」）：没它读不出任何行。
 *
 *  ⚠️ 与 `DEFAULT_PRICE_MAPPING`（空）的区别：那是「**要不要预填来源列**」，
 *  这是「**要不要预置哪几行目标**」。用户要的是：**行先摆出来**（让他知道
 *  这四项必须配），但**来源列一律留空**（不替他猜是哪一列）。
 *  ⚠️ 顺序按用户列举的顺序（SKU图 / 售价 / 市场价 / 库存）——
 *  界面上就是这四行，顺序变了用户会以为少了一项。
 *  ⚠️ `seller_code` / `barcode` / … **不预置**（它们是可选的额外映射，
 *  用户要就自己点「+」加）。
 */
export const REQUIRED_PRICE_TARGETS: readonly PriceTargetKey[] = [
  //: ⚠️ **用户口径 2026-10-21（推翻 2026-10-14/15 的「SKU 名称 + SKU图 必配」）**：
  //
  //: > 「规格图不应在价格库存映射关系表中再次进行配置，应为在预检步骤中 SKU 图是
  //: >   固定列，应直接取规格对应行的固定列 SKU图；另外 SKU 名称也应无需再让用户
  //: >   配置映射关系，当用户配置完商品 SKU 映射关系指定的列去重后就是对该组的 sku 规格」
  //
  //: ⇒ `sku_name` / `spec_image` **不再是必配行**：
  //:   · SKU 名称 = 商品 SKU 映射关系里那几列**去重组合**出来的规格（见 `skuSpecTextOf`）；
  //:   · SKU 图   = 表格固定的「SKU 图」列（`SKU_IMAGE_COL`）本行值。
  //: 两个都由代码按**固定判据**取，不让用户再配一遍（配两遍 = 两份真相源，必然漂移）。
  //: ⚠️ 用户口径 2026-10-21：「**售价、当前库存、改后库存、市场价均必须配置**」。
  "price",
  "current_stock",
  "stock",
  "market_price",
];

/** 预置的映射行（目标固定、来源**全空**）。 */
export function defaultPriceMappingRows(): PriceMappingRow[] {
  return REQUIRED_PRICE_TARGETS.map((target) => ({ target, source: "" }));
}
//: ⚠️ 用户口径 2026-10-11：「价格库存映射关系表中**市场价不做默认映射**」——
//: 市场价**不在默认行里**（用户要就自己加一行去配）；但「市场价 = 原价 × 倍率」
//: 的算法仍在（`buildSkuFillPlan`），只是**默认没有映射行** ⇒ 默认不写市场价。

/** 售价的**回落来源列**（原价取不到时用它，且**不乘倍率**）。 */
export const DEFAULT_PRICE_FALLBACK_SOURCE = "";

/** 从预检表格列名里挑「源列」的候选（**任意列**都可当来源 —— 用户口径「动态可调整」）。 */
export function priceSourceOptions(columns: readonly string[]): string[] {
  //: ⚠️ **「SKU 图」现在是 `columns` 里的一列**（用户口径 2026-10-15：
  //: 「SKU 图应该给放回 rows 中」）⇒ 候选**直接就是全部列**，不用再手工补。
  //: （原实现手工把虚拟列插到最前 —— 那是它不在表格里时期的补丁，已撤。）
  return columns.map((c) => String(c));
}

/** 校验价格库存映射（用户口径 2026-10-11）：
 *  ① **目标列**不许重复；
 *  ② **来源列也不支持重复映射** —— 同一列不能被两个目标列同时用
 *     （一个源列喂两个目标会造成「同一个数被当售价又当库存」这类静默错误）；
 *  ③ 来源列可以不填（= 该目标列不自动填，允许，不是错误）。
 */
export function validatePriceMapping(rows: readonly PriceMappingRow[]): string[] {
  const errors: string[] = [];
  const seenTarget = new Set<string>();
  const seenSource = new Set<string>();

  //: ⚠️ 用户口径 2026-10-11：「异常提炼成简单明了，**只保留核心**」。
  //: 原实现逐行逐字段报（一行空来源 + 一个重复目标 + 一个重复来源 = 三句并排），
  //: 用户看到的是同一件事被说三遍 ⇒ 现在**每类问题只报一条**，且不带行号。
  const emptySources: string[] = [];
  const dupTargets: string[] = [];
  //: （`dupSources` / `seenSource` 已随「来源列允许重复」一并退场。）

  for (const row of rows) {
    const label = row.target ? PRICE_TARGET_LABELS[row.target] : "未选";
    if (row.target && seenTarget.has(row.target)) {
      if (!dupTargets.includes(label)) dupTargets.push(label);
    }
    if (row.target) seenTarget.add(row.target);

    const source = row.source.trim();
    if (!source) {
      if (!emptySources.includes(label)) emptySources.push(label);
      continue;
    }
    //: ⚠️ **来源列允许重复**（用户口径 2026-10-21：「价格库存映射关系表应
    //: **支持重复选择映射列**」）。同一列本来就可能喂给多个目标：
    //:   · 「库存」→ 当前库存，同时 → 改后库存（把现库存原样带上）；
    //:   · 「价格」→ 售价，同时 → 市场价；
    //:   · 「SKUID」→ 商家编码，同时 → 规格ID。
    //: ⇒ 不再收集/上报「来源列重复」，`seenSource` 一并退场
    //:   （留着它只会让下一个人又把它接回错误提示里）。
  }

  //: ⚠️ 用户口径 2026-10-11：多条问题要「**放一行提示**」——
  //: 不是把三条各占一行，而是用 `；` 接成**一句**，弹窗里只占一行高度。
  const parts: string[] = [];
  if (rows.some((r) => !r.target)) parts.push("价格库存列不能为空");
  if (emptySources.length > 0) parts.push("来源列不能为空");
  if (dupTargets.length > 0) parts.push(`价格库存列重复：${dupTargets.join("、")}`);
  if (parts.length > 0) errors.push(parts.join("；"));
  return errors;
}

/** 映射是否可提交（无错误）。 */
export function isPriceMappingReady(rows: readonly PriceMappingRow[]): boolean {
  return rows.length > 0 && validatePriceMapping(rows).length === 0;
}

/** **「必须配」的四项还没配来源列的**（用户口径 2026-10-15）：
 *
 *  > 「右侧的价格库存中，**SKU图、售价、市场价、库存**所有**必须配置**」
 *  > 「再点击确认导入映射关系时**校验 SKU图的映射关系**，不再走现在默认的逻辑，
 *  >   应走映射逻辑」
 *
 *  ⚠️ 返回**中文名清单**（给提示语直接用）—— 说「有必填项未配」等于没说，
 *  用户得知道**是哪几项**。
 *  ⚠️ 判据是「这一项**有没有配来源列**」，不是「有没有这一行」——
 *  预置行本来就在（`defaultPriceMappingRows`），要配的是**来源**。
 */
export function missingRequiredPriceTargets(
  rows: readonly PriceMappingRow[],
): string[] {
  return REQUIRED_PRICE_TARGETS.filter((target) => {
    const hit = rows.find((r) => r.target === target);
    return !hit || !hit.source.trim();
  }).map((target) => PRICE_TARGET_LABELS[target]);
}

/** 按映射行取某个源列在表格里的下标（找不到 ⇒ `-1`，调用方按「不填」处理）。 */
export function sourceColumnIndex(
  columns: readonly string[],
  rows: readonly PriceMappingRow[],
  target: PriceTargetKey,
): number {
  const mapped = rows.find((r) => r.target === target);
  if (!mapped || !mapped.source.trim()) return -1;
  //: ⚠️ **「SKU 图」现在是真列**（用户口径 2026-10-15：「SKU 图应该给放回 rows 中」）
  //: ⇒ 不需要虚拟列哨兵了 —— 它与「规格分类」一样走 `withXxxColumn` 插进
  //: `table.columns` / `table.rows`，取值就是普通的 `row[colIndex]`。
  //: 原虚拟列方案（`VIRTUAL_COL_INDEX = -2` + 处处特判）**已撤**：
  //: 那是「值不在表格里」时期的权宜，进 rows 后特判反而成了**第二份实现**。
  return columns.findIndex((c) => String(c).trim() === mapped.source.trim());
}


/* ------------------------------------------------------------------ *
 * 需求 2026-10-15：**手动新增列**（+ 整列删除）
 * ------------------------------------------------------------------ */

/** 手动新增列的**标记前缀**（`column_key`）。
 *
 *  ⚠️ 为什么要一个 key：表头是**中文名**（用户可改、可重复），不能当身份；
 *  而「这列是不是手动加的」必须在**删除按钮的可见性**上判准
 *  （用户口径：**非手动新增和固定列不支持删除**）。
 *  ⇒ 用一个不会被用户改到的字段承载身份。
 *  ⚠️ 前缀用 `__manual_` —— 与既有的 `__matched_image` 等自造列同一命名空间。
 */
export const MANUAL_COLUMN_PREFIX = "__manual_";

/** 某列是不是**手动新增**的（判删除按钮要不要给）。 */
export function isManualColumn(key: string | undefined | null): boolean {
  return String(key ?? "").startsWith(MANUAL_COLUMN_PREFIX);
}

/** 生成手动列的英文名（**中文名非必填的兜底**）。
 *
 *  用户口径 2026-10-15：
 *  > 「弹窗用户设置表头**中文和英文名**（**英文名非必填为空则按照一定的顺序规则
 *  >   自动生成**）」
 *
 *  ⚠️ 「一定的顺序规则」= `col_1` / `col_2` / …（**按已有手动列的最大序号 +1**，
 *  不是按总列数）—— 否则删掉 col_1 再加一列会撞名。
 *  ⚠️ 撞名兜底：与现有英文名冲突就继续往后找，**不返回重复值**
 *  （重复的英文名会让第二步的映射查表拿到任意一条，静默错值）。
 */
export function nextManualColumnKey(usedKeys: readonly string[]): string {
  const used = new Set(usedKeys.map((k) => String(k)));
  let n = 1;
  //: ⚠️ 只看**手动列**的序号上限：`col_1` 被删后再加，序号仍从 1 起
  //: （用户删列是「不要这列」，不是「留个坑」）—— 但若 `col_1` 还在，就跳到 2。
  const manualNums = usedKeys
    .map((k) => /^col_(\d+)$/.exec(String(k))?.[1])
    .filter((x): x is string => !!x)
    .map((x) => Number(x));
  if (manualNums.length > 0) n = Math.max(...manualNums) + 1;
  while (used.has(`col_${n}`)) n += 1;
  return `col_${n}`;
}

/** 手动新增列的**中文名**兜底（用户不填英文名时，中文名本身是必填的）。 */
export function nextManualColumnName(columns: readonly string[]): string {
  const used = new Set(columns.map((c) => String(c).trim()));
  let n = 1;
  while (used.has(`新增列${n}`)) n += 1;
  return `新增列${n}`;
}

/** 在 SKU 表里**整列删除**某一列（返回新表；下标越界 ⇒ 原表不动）。
 *
 *  ⚠️ 与 `applyValuesToColumn` 一样是**纯函数**（不改原表），
 *  这样调用方能在删除前先算「删了会影响什么」，再决定要不要二次确认。
 */
export function dropTableColumn<
  T extends { columns: string[]; rows: (string | null)[][]; column_kinds?: string[]; column_keys?: string[] },
>(table: T, colIndex: number): T {
  if (colIndex < 0 || colIndex >= table.columns.length) return table;
  const cut = <U,>(arr: U[] | undefined): U[] | undefined =>
    arr ? arr.filter((_, i) => i !== colIndex) : arr;
  return {
    ...table,
    columns: table.columns.filter((_, i) => i !== colIndex),
    rows: table.rows.map((r) => r.filter((_, i) => i !== colIndex)),
    column_kinds: cut(table.column_kinds),
    column_keys: cut(table.column_keys),
  };
}

/** 在 SKU 表里**追加一列**（空值；未知的 kind 一律 `text`）。
 *
 *  ⚠️ 新列**追加在最右**（不是插在第 2 位）—— 用户口径只对「规格分类」要求固定第 2 位，
 *  手动列没有位置要求，且插在中间会让已有列的下标整体错位
 *  （第二步按**列名**映射没事，但用户刚填的表头顺序会变）。
 */
export function addTableColumn<
  T extends { columns: string[]; rows: (string | null)[][]; column_kinds?: string[]; column_keys?: string[] },
>(table: T, name: string, key: string): T {
  return {
    ...table,
    columns: [...table.columns, name],
    rows: table.rows.map((r) => [...r, null]),
    column_kinds: table.column_kinds ? [...table.column_kinds, "text"] : table.column_kinds,
    column_keys: table.column_keys ? [...table.column_keys, key] : table.column_keys,
  };
}
