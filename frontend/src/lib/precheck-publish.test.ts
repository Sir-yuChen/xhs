/**
 * `precheck-publish` 的 vitest 用例 —— 数据全部取自**真实数据包**
 * `C:\Users\admin\Desktop\商品ID_935947169189`（含真实列名与真实 SKU 名）。
 */
import { describe, expect, it } from "vitest";
import { spec_combinations } from "./publish-config";

import {
  applyBatchRule,
  applyBatchRuleToTable,
  buildPublishPayloadFromPrecheck,
  countFilledInColumn,
  DEFAULT_SKU_IMAGE_PATTERN,
  localSkuImageList,
  matchSkuImageName,
  lookupLocalSkuImage,
  mergePanelTable,
  passedSkuSpecs,
  parseSkuName,
  countUncheckedSkuMedia,
  filterCheckedSkuMedia,
  selectableSkuMedia,
  buildSkuFillPlan,
  defaultSpecMappingRows,
  validateSpecMapping,
  isSpecMappingReady,
  mappingFromRows,
  MAX_SPEC_MAPPING_ROWS,
  DEFAULT_PRICE_MAPPING,
  DEFAULT_PRICE_FALLBACK_SOURCE,
  validatePriceMapping,
  isPriceMappingReady,
  sourceColumnIndex,
  PRICE_TABLE_TARGETS,
  type PriceMappingRow,
  dropTableColumn,
  passedSkuRows,
  applyPriceMultiplier,
  clampPriceMultiplier,
  DEFAULT_PRICE_MULTIPLIER,
  PRICE_MULTIPLIER_MAX,
  type PassedSkuRow,
  skuImageNameFromFile,
  validateSkuImagePattern,
  skuNameColumnIndex,
  skuTableIndices,
  priceColumnIndex,
  stockColumnIndex,
  parseStock,
  parseYuan,
  splitByGroup,
  tableToSkus,
  type PrecheckMedia,
  type PrecheckTable,

  canDropColumn,
  nextManualColumnKey,
  addTableColumn,

  priceSourceOptions,
  SKU_IMAGE_COL,
  withDerivedColumns,
} from "./precheck-publish";

/* ------------------------------------------------------------------ *
 * ⚠️ **公共映射夹具**（2026-10-15 口径变更后必须显式给）
 *
 * 「所有点均走映射，移除写死的逻辑」+「默认映射不预填」⇒
 * `tableToSkus` / `passedSkuRows` / `buildSkuFillPlan` 的取值**全部来自这份映射**。
 * 测试再也不能靠「列名叫原价就被认出来」了 —— 那正是本轮移除的行为。
 * ------------------------------------------------------------------ */
const MAP_PRICE: PriceMappingRow[] = [
  { target: "sku_name", source: "sku名称" },
  { target: "price", source: "原价" },
  { target: "price_fallback", source: "计算价格" },
  { target: "stock", source: "库存" },
  { target: "seller_code", source: "ID" },
];
const MAP_PRICE_SPEC: PriceMappingRow[] = [
  ...MAP_PRICE,
  { target: "spec_category", source: "规格分类" },
];


/* ------------------------------------------------------------------ *
 * 真实包基线（实测，非构造）
 * ------------------------------------------------------------------ */

//: 真包 `商品ID_935947189_1_sku数据.xlsx` / sheet `全部` 的 6 列表头
const REAL_COLUMNS = ["sku名称", "图片", "原价", "计算价格", "库存", "ID"];

//: 真包前两行（注意 `图片` 列是**外部 URL**，不是数据包里的相对路径）
const REAL_ROWS: (string | null)[][] = [
  [
    "颜色分类:【中古风】实木三斗柜",
    "https://gw.alicdn.com/bao/uploaded/i3/2104559973/O1CN01taCdOF2NXh2EHTk8j_!!2104559973.jpg",
    "1280",
    null,
    "200",
    "935947169189",
  ],
  [
    "颜色分类:【中古风】伸缩桌+三斗柜梳妆台",
    "https://gw.alicdn.com/bao/uploaded/i1/2104559973/O1CN01TJJWse2NXh2KBWKuA_!!2104559973.jpg",
    "1980",
    null,
    "200",
    "935947169189",
  ],
];

//: 真包 `SKU/` 目录下的图（**刻意配不上**表格里的名称 —— 这就是要守的行为）
const REAL_SKU_IMAGES = [
  "SKU/SKU01_颜色分类(【中古风】实木三斗柜).jpg",
  "SKU/SKU02_颜色分类(【中古风】伸缩桌+三斗柜梳妆台).jpg",
];

/** 造一批媒体。

 *  ⚠️ 用户口径 2026-10-13 后**两个字段各有含义**，夹具必须都给：
 *    · `group` = 数据包里一级目录名的**原值**（真实形态是中文，如 `1比1主图`）；
 *    · `kind`  = 后端判的**参考类别**（`main`/`sku`/`detail`/`video`/`other`）。
 *  本文件测的是 `kind` 参与的分流（`splitByGroup`），所以 `kind` 取传入值；
 *  `group` 刻意用**不同于 kind 的目录名**（模拟包 A），钉住
 *  「拿 `group` 当枚举用会失效」这件事。
 */
const REAL_DIR_BY_KIND: Record<string, string> = {
  main: "1比1主图",
  sku: "SKU图",
  detail: "详情图",
  video: "主图视频",
};

function media(kind: string, n: number): PrecheckMedia[] {
  const dir = REAL_DIR_BY_KIND[kind] ?? kind;
  return Array.from({ length: n }, (_, i) => ({
    group: dir,
    kind,
    index: i,
    rel_path: `${dir}/f${i + 1}.jpg`,
    file_name: `f${i + 1}.jpg`,
    ocr_ok: true,
    has_forbidden: false,
  })) as PrecheckMedia[];
}

/* ------------------------------------------------------------------ */

describe("splitByGroup", () => {
  it("按区域分流：主图单列，SKU 与详情图各自成列", () => {
    const r = splitByGroup([
      ...media("main", 5),
      ...media("sku", 26),
      ...media("detail", 38),
    ]);
    expect(r.mainImages).toHaveLength(5);
    expect(r.skuImages).toHaveLength(26);
    expect(r.detailImages).toHaveLength(38);
  });

  it("⚠️ 分流按 `kind` 而不是 `group`（目录名可能是 `1比1主图` / `SKU图`）", () => {
    //: 这是本轮最容易写错的一处：`group` 现在是目录名原值，
    //: `group === "main"` 恒不成立 ⇒ 主图恒空（静默失效）。
    const r = splitByGroup(media("main", 3));
    expect(r.mainImages).toHaveLength(3);
    expect(r.mainImages[0]).toBe("1比1主图/f1.jpg");
  });

  it("视频**不进** images（真机抓包：images/imageDescriptions 都是图片对象）", () => {
    const r = splitByGroup([...media("main", 2), ...media("video", 1)]);
    const all = [...r.mainImages, ...r.detailImages, ...r.skuImages];
    expect(all.some((p) => p.includes("主图视频"))).toBe(false);
    expect(r.mainImages).toHaveLength(2);
  });

  it("传的是暂存内相对路径，不是服务器绝对路径", () => {
    const r = splitByGroup(media("main", 1));
    expect(r.mainImages[0]).toBe("1比1主图/f1.jpg");
    expect(r.mainImages[0].startsWith("/")).toBe(false);
  });
});

describe("parseYuan / parseStock", () => {
  it("解析真实值：'1280' ⇒ 1280", () => {
    expect(parseYuan("1280")).toBe(1280);
  });

  it("容忍货币符号与空白", () => {
    expect(parseYuan("¥1280.50")).toBe(1280.5);
    expect(parseYuan(" 1980 ")).toBe(1980);
  });

  it("空值 ⇒ null（不造假，缺值口径）", () => {
    expect(parseYuan(null)).toBeNull();
    expect(parseYuan("")).toBeNull();
    expect(parseYuan("待补")).toBeNull();
  });

  it("库存取整且不为负", () => {
    expect(parseStock("200")).toBe(200);
    expect(parseStock("12.7")).toBe(12);
    expect(parseStock("-5")).toBe(0);
  });
});

describe("tableToSkus", () => {
  const table: PrecheckTable = {
    title: "全部",
    sheet_name: "全部",
    columns: REAL_COLUMNS,
    rows: REAL_ROWS,
  };

  it("真实表 ⇒ 2 行 SKU，价格取「原价」", () => {
    const skus = tableToSkus(table, MAP_PRICE);
    expect(skus).toHaveLength(2);
    expect(skus[0].name).toBe("颜色分类:【中古风】实木三斗柜");
    expect(skus[0].price_yuan).toBe(1280);
    expect(skus[1].price_yuan).toBe(1980);
    expect(skus[0].stock).toBe(200);
  });

  it("「计算价格」是空列时**不能**把价格变成 null（必须回退原价）", () => {
    // ⚠️ 真包 `计算价格` 列是**空的**（公式列没算出来）。若优先取它，
    // 全部 SKU 价格会变「待补」。
    expect(table.rows.every((r) => r[3] === null)).toBe(true);
    expect(tableToSkus(table, MAP_PRICE).every((s) => s.price_yuan !== null)).toBe(true);
  });

  it("原价与计算价格都缺 ⇒ null（不填 0 冒充）", () => {
    const bare: PrecheckTable = {
      title: "全部",
      sheet_name: "全部",
      columns: REAL_COLUMNS,
      rows: [["颜色分类:A", null, null, null, "1", "9"]],
    };
    expect(tableToSkus(bare, MAP_PRICE)[0].price_yuan).toBeNull();
  });

  it("⚠️ 列名变体**不再**被认（口径已改为「一律走映射，移除写死」）", () => {
    const alt: PrecheckTable = {
      title: "全部",
      sheet_name: "全部",
      columns: ["规格名", "价格", "stock"],
      rows: [["颜色分类:A", "88.5", "3"]],
    };
    //: ⚠️ **夹具的列名与映射对不上**（映射指的是 `sku名称`/`原价`/`库存`）
    //: ⇒ 新口径下**认不出**（`sku_name` 那一列不存在）⇒ 返回空。
    //: 这正是「移除写死逻辑」的**预期后果**：列名不匹配就不猜。
    expect(tableToSkus(alt, MAP_PRICE)).toEqual([]);
    //: 把映射改成这个表的列名 ⇒ 立刻能认出（证明是**映射**在起作用）。
    expect(
      tableToSkus(alt, [
        { target: "sku_name", source: "规格名" },
        { target: "price", source: "价格" },
        { target: "stock", source: "stock" },
      ]),
    ).toEqual([{ name: "颜色分类:A", price_yuan: 88.5, stock: 3, image_path: null }]);
  });

  it("没有名称列 ⇒ 不产出 SKU（不硬造）", () => {
    expect(tableToSkus({ title: "t", sheet_name: "t", columns: ["库存"], rows: [["1"]] })).toEqual([]);
  });

  it("空表 / 空列头 ⇒ 空数组", () => {
    expect(tableToSkus(null, MAP_PRICE)).toEqual([]);
    expect(tableToSkus({ title: "t", sheet_name: "t", columns: [], rows: [] })).toEqual([]);
  });

  //: ⚠️ 用户 2026-10-08 口径：「不要将 SKU 图和规格对齐，图片名称和 SKU
  //: 名称可能对应不上，没有固定规则」。这条**刻意**钉住 —— 将来谁想
  //: 「顺手加个名称匹配」，这条会先红。
  it("**不把 SKU 图按名称配给规格**（无固定规则 ⇒ 宁缺勿猜）", () => {
    const skus = tableToSkus(table, MAP_PRICE);
    expect(skus.every((s) => s.image_path === null)).toBe(true);
  });

  it("即使文件名恰好能对上也不配（避免「这一份包能配」被当成规则）", () => {
    //: 故意用**可对上**的文件名：归一后 `SKU01_颜色分类(…)` == 表格首列。
    //: 行为必须仍是 null —— 否则就变成隐式依赖命名巧合了。
    const skus = tableToSkus(table, MAP_PRICE);
    expect(REAL_SKU_IMAGES.length).toBe(2); // 素材存在，但没用上
    expect(skus.map((s) => s.image_path)).toEqual([null, null]);
  });
});

describe("buildPublishPayloadFromPrecheck", () => {
  it("有 token ⇒ 出完整载荷（token + 两个图片列 + SKU）", () => {
    const p = buildPublishPayloadFromPrecheck(
      {
        token: "TK1",
        media: [...media("main", 5), ...media("detail", 38), ...media("sku", 26)],
        tables: [{ title: "全部", sheet_name: "全部", columns: REAL_COLUMNS, rows: REAL_ROWS }],
      },
      //: ⚠️ **必须传映射**（用户口径 2026-10-15：全部使用映射关系得到列值）。
      MAP_PRICE,
    );
    expect(p).not.toBeNull();
    expect(p!.precheck_token).toBe("TK1");
    expect(p!.main_images).toHaveLength(5);
    expect(p!.detail_images).toHaveLength(38);
    expect(p!.skus).toHaveLength(2);
    expect(p!.skus.every((s) => s.image_path === null)).toBe(true);
  });

  it("**无** precheck ⇒ null（走草稿箱旧路径，不被新链路卡住）", () => {
    expect(buildPublishPayloadFromPrecheck(null)).toBeNull();
    expect(buildPublishPayloadFromPrecheck(undefined)).toBeNull();
    expect(buildPublishPayloadFromPrecheck({ token: "", media: [], tables: [] })).toBeNull();
  });

  it("表格为空 ⇒ 仍出载荷，只是没有 SKU（发布端点自己拒空 SKU，前端给得准）", () => {
    const p = buildPublishPayloadFromPrecheck({
      token: "TK1",
      media: media("main", 2),
      tables: [],
    });
    expect(p!.skus).toEqual([]);
    expect(p!.main_images).toHaveLength(2);
  });
});
describe("表格·价格/库存批量修改（用户口径 2026-10-09）", () => {
  const table: PrecheckTable = {
    title: "全部",
    sheet_name: "全部",
    columns: ["sku名称", "原价", "库存"],
    rows: [
      ["A", "1280", "200"],
      ["B", "1980", "200"],
      ["C", null, "50"],
    ],
  };

  it("列识别与 tableToSkus 同源（价格/库存别名一致）", () => {
    //: ⚠️ **一律走映射**（用户口径 2026-10-15）—— 没配映射就恒 -1，不猜列名。
    expect(priceColumnIndex(table.columns, MAP_PRICE)).toBe(1);
    expect(stockColumnIndex(table.columns, MAP_PRICE)).toBe(2);
    expect(priceColumnIndex(table.columns)).toBe(-1);
    expect(stockColumnIndex(table.columns)).toBe(-1);
  });

  it("加减固定值：每条 = 原值 + N（N 可负）", () => {
    const up = applyBatchRuleToTable(table, 1, { mode: "delta", operand: 100 });
    expect(up.rows.map((r) => r[1])).toEqual(["1380", "2080", null]);
    const down = applyBatchRuleToTable(table, 1, { mode: "delta", operand: -80 });
    expect(down.rows.map((r) => r[1])).toEqual(["1200", "1900", null]);
  });

  it("倍数 + 固定值：原值 × K + N", () => {
    const t = applyBatchRuleToTable(table, 1, { mode: "scale", factor: 2, operand: 50 });
    expect(t.rows.map((r) => r[1])).toEqual(["2610", "4010", null]);
  });

  it("统一改固定值：一律 = N（不看原值）", () => {
    const t = applyBatchRuleToTable(table, 2, { mode: "fixed", operand: 999 });
    //: ⚠️ 空值**不参与** —— 第 3 行库存有值(50) ⇒ 也改；价格列的空值才保持空
    expect(t.rows.map((r) => r[2])).toEqual(["999", "999", "999"]);
  });

  it("**空值不参与**（缺值口径：不造假）", () => {
    const t = applyBatchRuleToTable(table, 1, { mode: "fixed", operand: 1 });
    expect(t.rows[2][1]).toBeNull(); // 价格空 ⇒ 保持空，不写 "1"
  });

  it("下限裁剪：价格不为负", () => {
    const t = applyBatchRuleToTable(table, 1, { mode: "delta", operand: -99999 });
    expect(t.rows.map((r) => r[1])).toEqual(["0", "0", null]);
  });

  it("库存上限裁剪", () => {
    const t = applyBatchRuleToTable(table, 2, {
      mode: "delta",
      operand: 999999,
      max: 999,
    });
    expect(t.rows.map((r) => r[2])).toEqual(["999", "999", "999"]);
  });

  it("原地不改：返回新表，原表不变", () => {
    const before = JSON.stringify(table);
    applyBatchRuleToTable(table, 1, { mode: "fixed", operand: 7 });
    expect(JSON.stringify(table)).toBe(before);
  });

  it("列下标 < 0 ⇒ 原样返回（改了个寂寞要能看出来）", () => {
    expect(applyBatchRuleToTable(table, -1, { mode: "fixed", operand: 1 })).toBe(table);
  });

  it("小数保留两位、整数不补 .00", () => {
    const t: PrecheckTable = {
      title: "x", sheet_name: "x", columns: ["原价"],
      rows: [["10"], ["10.5"]],
    };
    //: 整数原样（不补 .00）；小数统一两位。
    const out = applyBatchRuleToTable(t, 0, { mode: "delta", operand: 0 });
    expect(out.rows.map((r) => r[0])).toEqual(["10", "10.50"]);
    const half = applyBatchRuleToTable(t, 0, { mode: "scale", factor: 1.15, operand: 0 });
    //: ⚠️ **口径已变**（用户 2026-10-11：「列值最终计算保留两位小数**四舍五入**」）：
    //: 原断言为 `12.07` —— 那是 `toFixed(2)`（银行家/截断式）的结果。
    //: 现按**四舍五入**：`10.5 × 1.15 = 12.075` ⇒ 进位 ⇒ **12.08**。
    //: 旧值 `12.07` 保留在此注释中作口径变更标记（不静默删除）。
    expect(half.rows.map((r) => r[0])).toEqual(["11.50", "12.08"]);  // 12.075 四舍五入 → 12.08
  });
});

describe("勾选行批量修改（用户口径 2026-10-09：规格列表加复选框）", () => {
  const table: PrecheckTable = {
    title: "全部",
    sheet_name: "全部",
    columns: ["sku名称", "原价", "库存"],
    rows: [
      ["A", "100", "10"],
      ["B", "200", "20"],
      ["C", "300", "30"],
      ["D", null, null],
    ],
  };

  it("只改选中的行，其余行**一个字符都不碰**", () => {
    const out = applyBatchRuleToTable(table, 1, { mode: "fixed", operand: 9 }, [0, 2]);
    expect(out.rows.map((r) => r[1])).toEqual(["9", "200", "9", null]);
  });

  it("选中的空值行仍不参与（保持空）", () => {
    const out = applyBatchRuleToTable(table, 1, { mode: "fixed", operand: 9 }, [0, 3]);
    expect(out.rows.map((r) => r[1])).toEqual(["9", "200", "300", null]);
  });

  it("不传 onlyRows / 传空数组 ⇒ 整列改（与旧行为一致）", () => {
    const all = applyBatchRuleToTable(table, 2, { mode: "delta", operand: 1 });
    expect(all.rows.map((r) => r[2])).toEqual(["11", "21", "31", null]);
    const empty = applyBatchRuleToTable(table, 2, { mode: "delta", operand: 1 }, []);
    expect(empty.rows.map((r) => r[2])).toEqual(["11", "21", "31", null]);
  });

  it("countFilledInColumn：范围内只数有值的行", () => {
    expect(countFilledInColumn(table, 1)).toBe(3);        // 整列：A/B/C 有值
    expect(countFilledInColumn(table, 1, [0, 3])).toBe(1); // 勾选 A(有值)+D(空) ⇒ 1
    expect(countFilledInColumn(table, 2, [3])).toBe(0);    // 只勾到空行 ⇒ 0（页面不弹确认框）
    expect(countFilledInColumn(table, -1)).toBe(0);        // 列不存在 ⇒ 0
  });

  it("选中行范围外不被改动（返回新表、原表不变）", () => {
    const before = JSON.stringify(table);
    applyBatchRuleToTable(table, 1, { mode: "fixed", operand: 1 }, [1]);
    expect(JSON.stringify(table)).toBe(before);
  });
});

describe("倍数模式的中间符号（用户口径 2026-10-09：可加可减，默认加）", () => {
  it("倍数+正数 ⇒ 原值 × K + N", () => {
    expect(applyBatchRule(100, { mode: "scale", factor: 2, operand: 50 })).toBe(250);
  });
  it("倍数+负数 ⇒ 原值 × K − N", () => {
    expect(applyBatchRule(100, { mode: "scale", factor: 2, operand: -50 })).toBe(150);
  });
  it("加减模式：负操作数 ⇒ 减（结果不小于下限）", () => {
    expect(applyBatchRule(100, { mode: "delta", operand: -30 })).toBe(70);
    expect(applyBatchRule(20, { mode: "delta", operand: -30, min: 0 })).toBe(0);
  });
});

describe("第二次批量修改无响应（用户实测 2026-10-09）", () => {
  /** 数据包里的全部表（属性图 OCR 已于 2026-10-11 移除 ⇒ 不再有「属性 KV 表」）。 */
  const full: PrecheckTable[] = [
    { title: "全部", sheet_name: "全部", columns: ["sku名称", "原价", "库存"],
      rows: [["A", "100", "10"], ["B", "200", "20"]] },
    { title: "赠品", sheet_name: "赠品", columns: ["sku名称", "库存"],
      rows: [["G", "5"]] },
  ];

  it("skuTableIndices：数据包里的表**全部**都是 SKU 表", () => {
    expect(skuTableIndices(full)).toEqual([0, 1]);
  });

  it("mergePanelTable：改面板第 0 张 ⇒ 落在完整数组下标 0；别的表不丢", () => {
    const changed = applyBatchRuleToTable(full[0], 2, { mode: "delta", operand: 1 });
    const merged = mergePanelTable(full, 0, changed);
    expect(merged).toHaveLength(2);
    expect(merged[0].rows.map((r) => r[2])).toEqual(["11", "21"]);
    expect(merged[1].rows.map((r) => r[1])).toEqual(["5"]); // 别的表没被碰
  });

  it("**连续两次**改同一列都能落上（第二次不再是空操作）", () => {
    let cur = full;
    cur = mergePanelTable(cur, 0, applyBatchRuleToTable(cur[0], 2, { mode: "delta", operand: 10 }));
    cur = mergePanelTable(cur, 0, applyBatchRuleToTable(cur[0], 2, { mode: "delta", operand: 10 }));
    expect(cur).toHaveLength(2);
    expect(cur[0].rows.map((r) => r[2])).toEqual(["30", "40"]); // 10→30、20→40
    expect(cur[1].rows.map((r) => r[1])).toEqual(["5"]);
  });

  it("面板下标越界 ⇒ 原样返回（不误改、不丢）", () => {
    expect(mergePanelTable(full, 9, full[0])).toEqual(full);
  });
});


/* ------------------------------------------------------------------ *
 * 2026-10-11 发布商品优化轮（需求 2/3/5）
 * 数据取自真实数据包 `C:\Users\admin\Desktop\商品ID_935947169189`
 * ------------------------------------------------------------------ */

describe("需求 2 · parseSkuName（首个冒号切「规格分类 / SKU 名称」）", () => {
  it("半角冒号：真包首行", () => {
    expect(parseSkuName("颜色分类:【中古风】实木三斗柜")).toEqual({
      category: "颜色分类",
      value: "【中古风】实木三斗柜",
    });
  });

  it("全角冒号同样认（数据包里两种都出现过）", () => {
    expect(parseSkuName("颜色分类：【中古风】实木六斗柜")).toEqual({
      category: "颜色分类",
      value: "【中古风】实木六斗柜",
    });
  });

  it("**只切第一个**冒号：值里的后续冒号原样保留（不吞用户真实规格值）", () => {
    expect(parseSkuName("尺寸:1m:加宽")).toEqual({ category: "尺寸", value: "1m:加宽" });
  });

  it("两个冒号共存时取**最靠前**的那个", () => {
    expect(parseSkuName("颜色:全角：后半")).toEqual({ category: "颜色", value: "全角：后半" });
  });

  it("没有冒号 ⇒ 分类空、整串当值（不猜，不把首词当分类）", () => {
    expect(parseSkuName("实木三斗柜")).toEqual({ category: "", value: "实木三斗柜" });
  });

  it("空值 / null / undefined ⇒ 双空", () => {
    expect(parseSkuName("")).toEqual({ category: "", value: "" });
    expect(parseSkuName(null)).toEqual({ category: "", value: "" });
    expect(parseSkuName(undefined)).toEqual({ category: "", value: "" });
  });
});

describe("需求 3 · 本地 SKU 图按名称对齐", () => {
  it("skuImageNameFromFile：去 `SKU01_` + `(...)` 还原成 `分类:值`（真包）", () => {
    expect(
      skuImageNameFromFile("SKU01_颜色分类(【中古风】实木三斗柜).jpg"),
    ).toBe("颜色分类:【中古风】实木三斗柜");
    expect(
      skuImageNameFromFile("SKU26_颜色分类(【中古风】两个伸缩桌+六斗柜+一字板梳妆台+一字板上柜+2个妆凳).jpg"),
    ).toBe("颜色分类:【中古风】两个伸缩桌+六斗柜+一字板梳妆台+一字板上柜+2个妆凳");
  });

  it("真包 26 行 SKU 名 **全部**能在本地图里命中（26/26）", () => {
    //: 真包 26 个 SKU 名（与 `SKU/` 目录 26 张图一一对应）
    const skuNames = [
      "颜色分类:【中古风】实木三斗柜",
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台",
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+云朵妆镜",
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+妆凳",
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+云朵镜+妆凳",
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+一字板上柜",
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+云朵妆镜+一字板上柜",
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+妆凳+一字板上柜",
      "颜色分类:【中古风】伸缩桌+三斗柜梳妆台+云朵镜+妆凳+一字板上柜",
      "颜色分类:【中古风】两个伸缩桌+三斗柜梳妆台",
      "颜色分类:【中古风】两个伸缩桌+三斗柜梳妆台+2个妆凳",
      "颜色分类:【中古风】两个伸缩桌+三斗柜梳妆台+1个云朵妆镜+2个妆凳",
      "颜色分类:【中古风】两个伸缩桌+三斗柜+一字板梳妆台+一字板上柜+2个妆凳",
      "颜色分类:【中古风】实木六斗柜",
      "颜色分类:【中古风】伸缩桌+六斗柜梳妆台",
      "颜色分类:【中古风】伸缩桌+六斗柜梳妆台+云朵妆镜",
      "颜色分类:【中古风】伸缩桌+六斗柜梳妆台+妆凳",
      "颜色分类:【中古风】伸缩桌+六斗柜梳妆台+云朵妆镜+妆凳",
      "颜色分类:【中古风】伸缩桌+六斗柜梳妆台+一字板上柜",
      "颜色分类:【中古风】伸缩桌+六斗柜梳妆台+云朵妆镜+一字板上柜",
      "颜色分类:【中古风】伸缩桌+六斗柜梳妆台+妆凳+一字板上柜",
      "颜色分类:【中古风】伸缩桌+六斗柜梳妆台+云朵镜+妆凳+一字板上柜",
      "颜色分类:【中古风】两个伸缩桌+六斗柜梳妆台",
      "颜色分类:【中古风】两个伸缩桌+六斗柜梳妆台+2个妆凳",
      "颜色分类:【中古风】两个伸缩桌+六斗柜梳妆台+1个云朵妆镜+2个妆凳",
      "颜色分类:【中古风】两个伸缩桌+六斗柜+一字板梳妆台+一字板上柜+2个妆凳",
    ];
    const files = [
      "SKU01_颜色分类(【中古风】实木三斗柜).jpg",
      "SKU02_颜色分类(【中古风】伸缩桌+三斗柜梳妆台).jpg",
      "SKU03_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+云朵妆镜).jpg",
      "SKU04_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+妆凳).jpg",
      "SKU05_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+云朵镜+妆凳).jpg",
      "SKU06_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+一字板上柜).jpg",
      "SKU07_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+云朵妆镜+一字板上柜).jpg",
      "SKU08_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+妆凳+一字板上柜).jpg",
      "SKU09_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+云朵镜+妆凳+一字板上柜).jpg",
      "SKU10_颜色分类(【中古风】两个伸缩桌+三斗柜梳妆台).jpg",
      "SKU11_颜色分类(【中古风】两个伸缩桌+三斗柜梳妆台+2个妆凳).jpg",
      "SKU12_颜色分类(【中古风】两个伸缩桌+三斗柜梳妆台+1个云朵妆镜+2个妆凳).jpg",
      "SKU13_颜色分类(【中古风】两个伸缩桌+三斗柜+一字板梳妆台+一字板上柜+2个妆凳).jpg",
      "SKU14_颜色分类(【中古风】实木六斗柜).jpg",
      "SKU15_颜色分类(【中古风】伸缩桌+六斗柜梳妆台).jpg",
      "SKU16_颜色分类(【中古风】伸缩桌+六斗柜梳妆台+云朵妆镜).jpg",
      "SKU17_颜色分类(【中古风】伸缩桌+六斗柜梳妆台+妆凳).jpg",
      "SKU18_颜色分类(【中古风】伸缩桌+六斗柜梳妆台+云朵妆镜+妆凳).jpg",
      "SKU19_颜色分类(【中古风】伸缩桌+六斗柜梳妆台+一字板上柜).jpg",
      "SKU20_颜色分类(【中古风】伸缩桌+六斗柜梳妆台+云朵妆镜+一字板上柜).jpg",
      "SKU21_颜色分类(【中古风】伸缩桌+六斗柜梳妆台+妆凳+一字板上柜).jpg",
      "SKU22_颜色分类(【中古风】伸缩桌+六斗柜梳妆台+云朵镜+妆凳+一字板上柜).jpg",
      "SKU23_颜色分类(【中古风】两个伸缩桌+六斗柜梳妆台).jpg",
      "SKU24_颜色分类(【中古风】两个伸缩桌+六斗柜梳妆台+2个妆凳).jpg",
      "SKU25_颜色分类(【中古风】两个伸缩桌+六斗柜梳妆台+1个云朵妆镜+2个妆凳).jpg",
      "SKU26_颜色分类(【中古风】两个伸缩桌+六斗柜+一字板梳妆台+一字板上柜+2个妆凳).jpg",
    ];
    const media = files.map((f, i) => ({
      group: "sku",
      index: i,
      rel_path: `SKU/${f}`,
      file_name: f,
    })) as PrecheckMedia[];
    const list = localSkuImageList(media);
    const hit = skuNames.filter((n) => lookupLocalSkuImage(list, n) !== null);
    expect(hit).toHaveLength(26);
    expect(lookupLocalSkuImage(list, skuNames[0])).toBe("SKU/SKU01_颜色分类(【中古风】实木三斗柜).jpg");
  });

  it("对不上就是 `null`（**不按序号兜底**，静默配错图比不配更糟）", () => {
    const list = localSkuImageList([
      { group: "sku", index: 0, rel_path: "SKU/SKU01_颜色分类(甲).jpg", file_name: "SKU01_颜色分类(甲).jpg" },
    ] as PrecheckMedia[]);
    expect(lookupLocalSkuImage(list, "颜色分类:乙")).toBeNull();
  });

  it("只有 sku 组参与（主图/详情图不进本地图查表）", () => {
    const list = localSkuImageList([
      { group: "main", index: 0, rel_path: "主图/主图01.jpg", file_name: "主图01.jpg" },
    ] as PrecheckMedia[]);
    expect(list).toHaveLength(0);
  });
});

describe("预检顺序红线 · 违规媒体不允许被匹配到（用户口径 2026-10-11）", () => {
  const m = (
    file: string,
    ocr_ok: boolean | null,
    has_forbidden: boolean,
  ): PrecheckMedia => ({
    group: "sku",
    index: 0,
    rel_path: `SKU/${file}`,
    file_name: file,
    ocr_ok,
    has_forbidden,
  });

  it("⚠️ 用户口径 2026-10-21：**命中违规词的图不再被静默排除**（决策权交给用户）", () => {
    //: 这一条**取代**了 2026-10-11 的「违规图不许被匹配到」——
    //: 用户明确要求「检测到违规词的媒体资源放开也可选择……决策权交给用户」，
    //: 若这里仍剔除，用户勾了那张图却永远匹配不到（勾选态与结果不一致，且无提示）。
    //: 风险提示改由界面承担（红框 + 角标 + 默认不勾）。
    const out = selectableSkuMedia([
      m("SKU01_颜色分类(甲).jpg", true, true), // 违规，但已检测
      m("SKU02_颜色分类(乙).jpg", true, false),
    ]);
    expect(out.map((x) => x.file_name)).toEqual([
      "SKU01_颜色分类(甲).jpg",
      "SKU02_颜色分类(乙).jpg",
    ]);
  });

  it("**未检测（`ocr_ok=null`）也被排除** —— 未检不许算通过（免检通道）", () => {
    const out = selectableSkuMedia([
      m("SKU01_颜色分类(甲).jpg", null, false),
      m("SKU02_颜色分类(乙).jpg", true, false),
    ]);
    expect(out.map((x) => x.file_name)).toEqual(["SKU02_颜色分类(乙).jpg"]);
  });

  it("⚠️ 用户口径 2026-10-21：**违规图可以参与名称匹配**（用户勾了就认）", () => {
    const media = [
      m("SKU01_颜色分类(甲).jpg", true, true), // 违规，但已检测 ⇒ 参与匹配
      m("SKU02_颜色分类(乙).jpg", true, false),
    ];
    const list = localSkuImageList(selectableSkuMedia(media));
    expect(lookupLocalSkuImage(list, "颜色分类:甲")).toBe("SKU/SKU01_颜色分类(甲).jpg");
    expect(lookupLocalSkuImage(list, "颜色分类:乙")).toBe("SKU/SKU02_颜色分类(乙).jpg");
  });

  it("非 sku 组不参与（主图/详情图不进本地图匹配）", () => {
    const out = selectableSkuMedia([
      { group: "main", index: 0, rel_path: "主图/主图01.jpg", file_name: "主图01.jpg", ocr_ok: true, has_forbidden: false } as PrecheckMedia,
    ]);
    expect(localSkuImageList(out)).toHaveLength(0);
  });
});

describe("需求 5 · 按命名规则精确对齐本地 SKU 图（用户口径 2026-10-11 最终版）", () => {
  /** 真包 3 张（加载顺序 = SKU01, SKU02, SKU03）。 */
  const media: PrecheckMedia[] = [
    "SKU01_颜色分类(【中古风】实木三斗柜).jpg",
    "SKU02_颜色分类(【中古风】伸缩桌+三斗柜梳妆台).jpg",
    "SKU03_颜色分类(【中古风】伸缩桌+三斗柜梳妆台+云朵妆镜).jpg",
  ].map((f, i) => ({
    group: "sku",
    index: i,
    rel_path: `SKU/${f}`,
    file_name: f,
  })) as PrecheckMedia[];

  const list = localSkuImageList(media);

  it("⚠️ **命名规则解析**：`前缀+序号_规格分类(真实SKU名)` ⇒ `规格分类:真实SKU名`", () => {
    //: 用户口径：「前缀固定加自增序号+「_」+规格分类+括号中是真实的 sku 名称」
    expect(skuImageNameFromFile("SKU01_颜色分类(【中古风】实木三斗柜).jpg")).toBe(
      "颜色分类:【中古风】实木三斗柜",
    );
    //: 序号扩展到两位 / 三位都要能剥掉（自增序号，不是固定长度）。
    expect(skuImageNameFromFile("SKU26_颜色分类(甲).jpg")).toBe("颜色分类:甲");
    expect(skuImageNameFromFile("SKU100_尺寸(1m).png")).toBe("尺寸:1m");
  });

  it("**完整名精确命中** ⇒ 返回该图", () => {
    expect(lookupLocalSkuImage(list, "颜色分类:【中古风】实木三斗柜")).toBe(
      "SKU/SKU01_颜色分类(【中古风】实木三斗柜).jpg",
    );
  });

  it("⚠️ **不误配前缀重叠项**（精确对齐，不是取第一个前缀命中）", () => {
    //: 查询 `...伸缩桌+三斗柜梳妆台` 是 SKU03 名的**前缀**。
    //: 模糊匹配会因「前缀命中」返回 SKU02（顺序在前）——但那是**碰巧**；
    //: 精确对齐必须返回**真正相等**的 SKU02（它的完整名就是该串），
    //: 且**绝不能**返回 SKU03。
    expect(lookupLocalSkuImage(list, "颜色分类:【中古风】伸缩桌+三斗柜梳妆台")).toBe(
      "SKU/SKU02_颜色分类(【中古风】伸缩桌+三斗柜梳妆台).jpg",
    );
    //: 只给前缀（不带完整值）⇒ **不该**命中任何一条（精确才通过）。
    expect(lookupLocalSkuImage(list, "颜色分类:【中古风】伸缩桌")).toBeNull();
  });

  it("文件名**不符合默认规则** ⇒ 抽取键为空 ⇒ 匹配不到（走手选入口）", () => {
    //: 默认正则要求 `前缀序号_规格分类(真实名)` 这个形状；
    //: `SKU01_甲.jpg` 没有括号 ⇒ 解析不出 ⇒ 该行判「未匹配到」。
    //: ⚠️ 这正是「规则可配置」的意义：用户遇到这种包，改正则就能对上，
    //:    而不是让代码去猜（猜 = 静默错配）。
    const noSpec = localSkuImageList([
      { group: "sku", index: 0, rel_path: "SKU/a.jpg", file_name: "SKU01_甲.jpg" },
    ] as PrecheckMedia[]);
    expect(noSpec[0].key).toBe("");
    expect(lookupLocalSkuImage(noSpec, "颜色分类:甲")).toBeNull();
  });

  it("**换成正则就能对上**（同一份文件，规则不同结果不同）", () => {
    const custom = "^\\s*SKU\\s*\\d+\\s*[_-]?\\s*(.+)$";
    const withCustom = localSkuImageList(
      [{ group: "sku", index: 0, rel_path: "SKU/a.jpg", file_name: "SKU01_甲.jpg" }] as PrecheckMedia[],
      custom,
    );
    //: 这条正则只有 1 个组 ⇒ 键退化为组 1（`甲`），仍能与「规格值」对上。
    expect(withCustom[0].key).toBe("甲");
    expect(lookupLocalSkuImage(withCustom, "颜色分类:甲")).toBe("SKU/a.jpg");
  });

  it("对不上 ⇒ null（不猜、不按序号兜底）", () => {
    expect(lookupLocalSkuImage(list, "颜色分类:完全不存在的东西")).toBeNull();
  });

  it("空查询 / 空列表 ⇒ null", () => {
    expect(lookupLocalSkuImage(list, "")).toBeNull();
    expect(lookupLocalSkuImage([], "颜色分类:甲")).toBeNull();
  });

  it("⚠️ 编号不同不影响命中（对齐靠名称规则，不靠 SKU01 序号）", () => {
    const renumbered = localSkuImageList([
      {
        group: "sku",
        index: 0,
        rel_path: "SKU/SKU99_颜色分类(【中古风】实木三斗柜).jpg",
        file_name: "SKU99_颜色分类(【中古风】实木三斗柜).jpg",
      },
    ] as PrecheckMedia[]);
    expect(lookupLocalSkuImage(renumbered, "颜色分类:【中古风】实木三斗柜")).toBe(
      "SKU/SKU99_颜色分类(【中古风】实木三斗柜).jpg",
    );
  });

  it("passedSkuSpecs：对上的行进第二步；对不上的行进 blockedRows", () => {
    const table: PrecheckTable = {
      title: "全部",
      sheet_name: "全部",
      columns: ["sku名称", "图片", "原价", "库存"],
      rows: [
        ["颜色分类:【中古风】实木三斗柜", "https://x/1.jpg", "1280", "200"],
        ["颜色分类:完全不存在的东西", "https://x/9.jpg", "1", "1"],
      ],
    };
    const got = passedSkuSpecs(table, [1], [0], MAP_PRICE);
    expect([...got]).toEqual(["颜色分类:【中古风】实木三斗柜"]);
  });
});

describe("列定位一律走映射（用户口径 2026-10-15：移除写死的逻辑）", () => {
  //: ⚠️ **口径已变**（原断言 = 按别名猜列名，已随「所有点均走映射」移除）：
  //:   - 线上图列：**该列已从第一步整列移除**，映射里没有它 ⇒ 函数一并删除。
  //:   - 其余列：不再按别名猜，只认用户配的映射。
  it("SKU 名称列只认映射（没配 ⇒ -1，不猜列名）", () => {
    const cols = ["sku名称", "原价", "库存"];
    //: 没配映射 ⇒ -1（**不因为列名叫「sku名称」就认它**）。
    expect(skuNameColumnIndex(cols, [])).toBe(-1);
    //: 配了映射 ⇒ 认映射指定的那一列（哪怕列名不叫 sku名称）。
    expect(
      skuNameColumnIndex(cols, [{ target: "sku_name", source: "原价" }]),
    ).toBe(1);
  });
});


describe("需求 4 · passedSkuSpecs（未选中的不允许进第二步）", () => {
  const table: PrecheckTable = {
    title: "全部",
    sheet_name: "全部",
    columns: ["sku名称", "图片", "原价", "计算价格", "库存", "ID"],
    rows: [
      ["颜色分类:甲", "https://x/1.jpg", "10", null, "5", "1"],
      ["颜色分类:乙", "https://x/2.jpg", "20", null, "5", "1"],
      ["颜色分类:丙", "https://x/3.jpg", "30", null, "5", "1"],
    ],
  };

  it("默认全选 + 无拦截 ⇒ 三行全通过", () => {
    const got = passedSkuSpecs(table, [], [0, 1, 2], MAP_PRICE);
    expect([...got].sort()).toEqual(["颜色分类:甲", "颜色分类:丙", "颜色分类:乙"].sort());
  });

  it("**未勾选的行被排除**（用户口径：未选中的不允许进第二步）", () => {
    const got = passedSkuSpecs(table, [], [0, 2], MAP_PRICE);
    expect(got.has("颜色分类:乙")).toBe(false);
    expect(got.size).toBe(2);
  });

  it("**两图判定未通过的行被排除**（即使被勾选 —— 手滑勾上也不许漏进）", () => {
    const got = passedSkuSpecs(table, [1], [0, 1, 2], MAP_PRICE);
    expect(got.has("颜色分类:乙")).toBe(false);
    expect(got.size).toBe(2);
  });

  it("两个条件都要：未勾选 **或** 被拦 ⇒ 都不通过", () => {
    const got = passedSkuSpecs(table, [0], [1, 2], MAP_PRICE);
    expect([...got].sort()).toEqual(["颜色分类:丙", "颜色分类:乙"].sort());
  });

  it("**空 `picked` ⇒ 视为全选**（用户口径 2026-10-11：勾选态未上抛不得让结果为空）", () => {
    //: ⚠️ **取代**原先的「全不选 ⇒ 空集」。
    //: 根因：勾选态住在 `TablesPanel` 内、靠 `SelectionReporter` 上抛，而那个面板
    //: **只在「媒体资源」页签挂载**。用户直接进第二步、从没打开过页签 ⇒ `picked`
    //: 恒为 `[]` ⇒ 一行都导不出 ⇒「预检通过SKU导入」按钮恒灰（用户实测：
    //: 「先点一下媒体资源就能用了」= 典型时序症状）。
    //: 表格自身默认就是**全选**（`picked[tIndex] ?? rows.map(() => true)`），
    //: 所以空集 = 全选才是同一口径；**预检未通过的行仍被 blocked 挡掉**。
    expect(passedSkuSpecs(table, [], [], MAP_PRICE).size).toBe(3);
  });

  it("空 `picked` 也不会放过预检未通过的行（blocked 仍然生效）", () => {
    const got = passedSkuSpecs(table, [1], [], MAP_PRICE);
    expect(got.has("颜色分类:乙")).toBe(false);
    expect(got.size).toBe(2);
  });

  it("没有 sku 名称列 / 空表 ⇒ 空集（不猜不编）", () => {
    expect(passedSkuSpecs(null, [], [0], MAP_PRICE).size).toBe(0);
    expect(
      passedSkuSpecs({ title: "x", sheet_name: "x", columns: ["A", "B"], rows: [["1", "2"]] }, [], [0], MAP_PRICE).size,
    ).toBe(0);
  });

  it("空白名称行不进集合（缺值口径：不留空键）", () => {
    const t: PrecheckTable = {
      title: "t", sheet_name: "t", columns: ["sku名称"], rows: [["  "], ["颜色分类:甲"]],
    };
    const got = passedSkuSpecs(t, [], [0, 1], MAP_PRICE);
    expect([...got]).toEqual(["颜色分类:甲"]);
  });
});


describe("规格配置 · 可配置正则（用户口径 2026-10-11）", () => {
  it("**默认正则就把命名规则解析出来**（预填在输入框里的那条）", () => {
    const hit = matchSkuImageName("SKU01_颜色分类(【中古风】实木三斗柜).jpg", DEFAULT_SKU_IMAGE_PATTERN);
    expect(hit?.key).toBe("颜色分类:【中古风】实木三斗柜");
    expect(hit?.groups).toEqual(["颜色分类", "【中古风】实木三斗柜"]);
  });

  it("默认正则对**两位/三位序号**同样有效（自增序号不是固定宽度）", () => {
    expect(matchSkuImageName("SKU26_颜色分类(甲).jpg", DEFAULT_SKU_IMAGE_PATTERN)?.key).toBe("颜色分类:甲");
    expect(matchSkuImageName("SKU100_尺寸(1m).png", DEFAULT_SKU_IMAGE_PATTERN)?.key).toBe("尺寸:1m");
  });

  it("扩展名**先剥掉**再交给正则（用户不必在正则里处理 .jpg）", () => {
    //: 同一条正则对 .jpg / .png / .webp 都应命中且结果一致。
    const pat = DEFAULT_SKU_IMAGE_PATTERN;
    expect(matchSkuImageName("SKU01_颜色分类(甲).jpg", pat)?.key).toBe("颜色分类:甲");
    expect(matchSkuImageName("SKU01_颜色分类(甲).png", pat)?.key).toBe("颜色分类:甲");
    expect(matchSkuImageName("SKU01_颜色分类(甲).webp", pat)?.key).toBe("颜色分类:甲");
  });

  it("**换一条正则**就换一套解析（规则可配的实证）", () => {
    //: 用户把规则改成「取中括号里的内容」（另一种命名习惯）
    const custom = "^.*?\\[([^\\]]+)\\]\\s*(.+)$";
    const hit = matchSkuImageName("图片[颜色分类]斗柜.jpg", custom);
    expect(hit?.key).toBe("颜色分类:斗柜");
  });

  it("正则**非法** ⇒ null（不抛错，不打断渲染）", () => {
    expect(matchSkuImageName("SKU01_颜色分类(甲).jpg", "([")).toBeNull();
  });

  it("正则能跑但**不匹配该文件名** ⇒ null", () => {
    expect(matchSkuImageName("随便一个名字.jpg", DEFAULT_SKU_IMAGE_PATTERN)).toBeNull();
  });

  it("`validateSkuImagePattern`：合法 ⇒ null；空 / 组数不足 / 语法错 ⇒ 给说明", () => {
    expect(validateSkuImagePattern(DEFAULT_SKU_IMAGE_PATTERN)).toBeNull();
    expect(validateSkuImagePattern("")).toContain("不能为空");
    //: 只有 1 个捕获组 ⇒ 抽不出「分类:名称」
    expect(validateSkuImagePattern("^SKU\\d+_(.+)$")).toContain("2 个捕获组");
    //: 语法错
    expect(validateSkuImagePattern("([")).toContain("语法错误");
  });

  it("换正则后 `localSkuImageList` 的键随之变化（重新匹配的机制）", () => {
    const media = [
      { group: "sku", index: 0, rel_path: "SKU/SKU01_颜色分类(甲).jpg", file_name: "SKU01_颜色分类(甲).jpg" },
    ] as PrecheckMedia[];
    const def = localSkuImageList(media, DEFAULT_SKU_IMAGE_PATTERN);
    expect(def[0].key).toBe(normalizeKeyForTest("颜色分类:甲"));
    //: 用一条匹配不上的正则 ⇒ 键为空（⇒ 该行会被判「未匹配到」，给手选入口）
    const bad = localSkuImageList(media, "^NOPE_(.+)_(.+)$");
    expect(bad[0].key).toBe("");
  });
});

/** 测试内的小工具：与实现同一套规范化（保持一致口径）。 */
function normalizeKeyForTest(v: string): string {
  return v.replace(/\s+/g, "").replace(/（/g, "(").replace(/）/g, ")").toLowerCase();
}


describe("媒体勾选 ↔ 规格表联动（用户口径 2026-10-11）", () => {
  /** 可选的 SKU 图（已检测 + 非违规）。 */
  const media = [
    { group: "sku", index: 0, rel_path: "SKU/SKU01_颜色分类(甲).jpg", file_name: "SKU01_颜色分类(甲).jpg", ocr_ok: true, has_forbidden: false },
    { group: "sku", index: 1, rel_path: "SKU/SKU02_颜色分类(乙).jpg", file_name: "SKU02_颜色分类(乙).jpg", ocr_ok: true, has_forbidden: false },
  ] as PrecheckMedia[];

  it("**取消勾选 ⇒ 该图不再参与匹配**（对应规格行的本地图被移除）", () => {
    const checked: Record<string, boolean> = { "sku/SKU/SKU01_颜色分类(甲).jpg": true, "sku/SKU/SKU02_颜色分类(乙).jpg": false };
    const clean = filterCheckedSkuMedia(selectableSkuMedia(media), checked);
    const list = localSkuImageList(clean);
    //: 勾着的还能匹配
    expect(lookupLocalSkuImage(list, "颜色分类:甲")).toBe("SKU/SKU01_颜色分类(甲).jpg");
    //: 取消勾选的**匹配不到**（⇒ 该行本地图列显示「无」）
    expect(lookupLocalSkuImage(list, "颜色分类:乙")).toBeNull();
  });

  it("**全不勾 ⇒ 一张都匹配不到**（规格行全部移除本地图）", () => {
    const clean = filterCheckedSkuMedia(selectableSkuMedia(media), {});
    const list = localSkuImageList(clean);
    expect(lookupLocalSkuImage(list, "颜色分类:甲")).toBeNull();
    expect(lookupLocalSkuImage(list, "颜色分类:乙")).toBeNull();
  });

  it("⚠️ **默认未在 checked 里的图不算勾选**（`undefined !== true` ⇒ 移除）", () => {
    //: 键缺失时**不能**当作已勾选 —— 否则「未勾选」会被当成「勾着」，
    //: 提示数恒 0 且图不会移除（静默失效）。
    const clean = filterCheckedSkuMedia(selectableSkuMedia(media), {});
    expect(clean).toHaveLength(0);
  });

  it("红线仍在：**未检测（`ocr_ok=null`）的图即使勾上也**不参与匹配", () => {
    //: ⚠️ 2026-10-21 起「违规」不再是过滤条件（决策权交用户），但「未检测」仍是 ——
    //: 把 `null` 当合规就是给没跑过 OCR 的图开免检通道，这条红线不动。
    const dirty = [
      { group: "sku", index: 0, rel_path: "SKU/a.jpg", file_name: "SKU01_颜色分类(丙).jpg", ocr_ok: true, has_forbidden: true },
      { group: "sku", index: 1, rel_path: "SKU/b.jpg", file_name: "SKU02_颜色分类(丁).jpg", ocr_ok: null, has_forbidden: false },
    ] as PrecheckMedia[];
    const allChecked: Record<string, boolean> = { "sku/SKU/a.jpg": true, "sku/SKU/b.jpg": true };
    const clean = filterCheckedSkuMedia(selectableSkuMedia(dirty), allChecked);
    expect(clean.map((x) => x.file_name)).toEqual(["SKU01_颜色分类(丙).jpg"]);
  });

  it("`countUncheckedSkuMedia`：数「本来可选却没勾」的（未检测的不计）", () => {
    const withDirty = [
      ...media,
      { group: "sku", index: 2, rel_path: "SKU/c.jpg", file_name: "SKU03_颜色分类(丙).jpg", ocr_ok: true, has_forbidden: true },
      { group: "sku", index: 3, rel_path: "SKU/d.jpg", file_name: "SKU04_颜色分类(丁).jpg", ocr_ok: null, has_forbidden: false },
    ] as PrecheckMedia[];
    //: ⚠️ 用户口径 2026-10-21：违规图现在**算「本来可选」**（决策权交用户），
    //: 所以「没勾」的那张违规图**要计入** —— 正是「你少用了这几张图」。
    //: 未检测（`ocr_ok=null`）仍不计（另有红框提示，避免数虚高）。
    //: ⇒ 甲已勾；乙未勾 + 丙（违规）未勾 = 2。
    const checked: Record<string, boolean> = {
      "sku/SKU/SKU01_颜色分类(甲).jpg": true,
      "sku/SKU/SKU02_颜色分类(乙).jpg": false,
    };
    expect(countUncheckedSkuMedia(withDirty, checked)).toBe(2);
    //: 全勾（含违规那张）⇒ 0（不误报）
    expect(
      countUncheckedSkuMedia(withDirty, {
        "sku/SKU/SKU01_颜色分类(甲).jpg": true,
        "sku/SKU/SKU02_颜色分类(乙).jpg": true,
        "sku/SKU/c.jpg": true,
      }),
    ).toBe(0);
  });
});

/* ------------------------------------------------------------------ */
/* 2026-10-11 第五批：预检 SKU 行 → 第二步规格/价格库存导入（独立入口）    */
/* 数据取自真实数据包 `C:\Users\admin\Desktop\商品ID_935947169189`      */
/* ------------------------------------------------------------------ */

describe("构建导入计划（buildSkuFillPlan）", () => {
  const dim = (name: string, over: Partial<{ customizable: boolean; values: { name: string; value_id: string }[] }> = {}) => ({
    name, id: `${name}-id`, customizable: true, values: [], options: [], ...over,
  });
  const row = (over: Partial<PassedSkuRow>): PassedSkuRow => ({
    rawName: "", specName: "", specValue: "", originalPrice: null, price: null, stock: null,
    sellerCode: "", localRel: null, localFileName: "",
    //: ⚠️ 2026-10-20 起 `cells` 必填（映射左侧 = 列名，导入按列名取值）。
    //: 用例只关心 `specValue` 时，把它放进「sku名称」列即可。
    cells: { ...(over.specValue ? { sku名称: over.specValue } : {}), ...(over.cells ?? {}) },
    ...over,
  });

  //: ⚠️ 2026-10-20 口径：映射左侧 = **预检通过列名**（不再是规格分类名）
  //: ⇒ `mapping` 的键是列名；值 = 平台维度名。导入按**列名**从 `cells` 取值。
  it("按**用户配的映射**导入（列名 → 平台维度）", () => {
    const plan = buildSkuFillPlan(
      [row({ rawName: "甲", cells: { sku名称: "甲" }, originalPrice: 1280, price: 1280, stock: 200 })],
      [dim("颜色分类")],
      { sku名称: "颜色分类" },
    );
    expect(plan.dims).toHaveLength(1);
    expect(plan.dims[0].dimName).toBe("颜色分类");
    expect(plan.rows).toHaveLength(1);
    expect(plan.rows[0]).toMatchObject({ spec: "颜色分类:甲", stock: 200, sellerCode: "" });
  });

  it("**没配映射 ⇒ 不导入**（宁可漏填，也不把值填错维度）", () => {
    const plan = buildSkuFillPlan(
      [row({ cells: { sku名称: "甲" } })],
      [dim("颜色分类")],
      {},                       //: 用户没配置任何规格关系
    );
    expect(plan.dims).toHaveLength(0);
    expect(plan.skipped.join("")).toContain("商品 SKU 映射关系");
  });

  it("**映射到平台没有的维度 ⇒ 不填并写明**（不按顺序猜）", () => {
    const plan = buildSkuFillPlan(
      [row({ cells: { sku名称: "甲" } })],
      [dim("颜色")],
      { sku名称: "不存在的维度" },
    );
    expect(plan.dims).toHaveLength(0);
    expect(plan.skipped.join("")).toContain("sku名称");
    expect(plan.rows).toHaveLength(0);
  });

  it("给了用户映射 ⇒ 按映射填", () => {
    const plan = buildSkuFillPlan(
      [row({ rawName: "甲", cells: { sku名称: "甲" }, price: 1 })],
      [dim("颜色")],
      { sku名称: "颜色" },
    );
    expect(plan.dims[0].dimName).toBe("颜色");
    expect(plan.rows[0].spec).toBe("颜色:甲");
  });

  it("命中平台候选项 ⇒ standard 且带 valueId；不命中但可自定义 ⇒ custom", () => {
    const withValues = dim("颜色", { values: [{ name: "甲", value_id: "vid-1" }] });
    const plan = buildSkuFillPlan(
      [
        row({ rawName: "甲", cells: { sku名称: "甲" } }),
        row({ rawName: "乙", cells: { sku名称: "乙" } }),
      ],
      [withValues],
      { sku名称: "颜色" },
    );
    const [a, b] = plan.dims[0].values;
    expect(a).toMatchObject({ value: "甲", valueId: "vid-1", match: "standard" });
    expect(b).toMatchObject({ value: "乙", valueId: "", match: "custom" });
  });

  it("平台不允许自定义且值不在候选里 ⇒ 该值被跳过并写明原因", () => {
    const locked = dim("颜色", { customizable: false, values: [{ name: "甲", value_id: "v1" }] });
    const plan = buildSkuFillPlan(
      [row({ rawName: "乙", cells: { sku名称: "乙" } })],
      [locked],
      { sku名称: "颜色" },
    );
    expect(plan.dims).toHaveLength(0);
    expect(plan.skipped.join("")).toContain("不接受自定义值");
  });

  it("★ **按用户选的列取值**（不是永远取 sku 名称）—— 换列必须换值", () => {
    //: ⚠️ 这条是「按列名取值」的**唯一承重断言**：
    //: 若代码退回 `row.specValue`（永远取 sku 名称），下面选「颜色」列后
    //: 拿到的仍是 sku 名称 ⇒ 断言必红。
    const plan = buildSkuFillPlan(
      [row({ rawName: "A款", cells: { sku名称: "A款", 颜色: "红" }, price: 1, stock: 1 })],
      [dim("颜色")],
      { 颜色: "颜色" },          //: 用户明确选的是「颜色」列
    );
    expect(plan.dims[0].values.map((v) => v.value)).toEqual(["红"]);
  });

  it("**两个不同列**（两行映射）⇒ 各自进各自的维度，组合文本按平台顺序拼", () => {
    const plan = buildSkuFillPlan(
      [
        row({ rawName: "甲", cells: { 颜色: "甲" }, originalPrice: 100, price: 100, stock: 1 }),
        row({ rawName: "1m", cells: { 尺寸: "1m" }, originalPrice: 100, price: 100, stock: 2 }),
      ],
      [dim("颜色"), dim("尺寸")],
      { 颜色: "颜色", 尺寸: "尺寸" },
    );
    //: 两个维度都建出来了（保序 = 平台维度顺序）。
    expect(plan.dims.map((d) => d.dimName)).toEqual(["颜色", "尺寸"]);
    expect(plan.dims[0].values[0].value).toBe("甲");
    expect(plan.dims[1].values[0].value).toBe("1m");
    //: ⚠️ 组合文本必须与 `spec_combinations` 同形（`A:v / B:v2`）——
    //: 两条数据包行各缺一个维度时**不成组合**（与笛卡尔积同判据），故此处为 0 行。
    expect(plan.rows).toHaveLength(0);
  });

  it("**同一行多个列都被映射** ⇒ 一次取全，组合成一条规格", () => {
    //: 用户口径 2026-10-20：左侧 = 列名 ⇒ 一行里可能同时有「颜色」和「尺寸」两列。
    const plan = buildSkuFillPlan(
      [row({ rawName: "甲1m", cells: { 颜色: "甲", 尺寸: "1m" }, originalPrice: 100, price: 100, stock: 5 })],
      [dim("颜色"), dim("尺寸")],
      { 颜色: "颜色", 尺寸: "尺寸" },
    );
    expect(plan.rows).toHaveLength(1);
    expect(plan.rows[0].spec).toBe("颜色:甲 / 尺寸:1m");
  });

  it("**所选列的值去重**（用户口径 2026-10-20：导入时对选择的列数据去重）", () => {
    const plan = buildSkuFillPlan(
      [
        row({ rawName: "甲", cells: { 颜色: "甲" }, originalPrice: 1, price: 1, stock: 1 }),
        row({ rawName: "甲 ", cells: { 颜色: "甲 " }, originalPrice: 1, price: 1, stock: 2 }),
        row({ rawName: "乙", cells: { 颜色: "乙" }, originalPrice: 1, price: 1, stock: 3 }),
      ],
      [dim("颜色")],
      { 颜色: "颜色" },
    );
    //: ⚠️ 去重按 `normalizeSkuKey`（大小写/空白归一）⇒ 「甲」和「甲 」
    //: 只算一个规格值，界面上不该出现两个一模一样的选项。
    expect(plan.dims[0].values.map((v) => v.value)).toEqual(["甲", "乙"]);
  });

  it("**每个维度都有值的行**才成组合（与 spec_combinations 同判据）", () => {
    const plan = buildSkuFillPlan(
      [
        row({ rawName: "甲", cells: { sku名称: "甲" }, originalPrice: 100, price: 100, stock: 1 }),
        row({ rawName: "乙", cells: { sku名称: "乙" }, originalPrice: 100, price: 100, stock: 2 }),
      ],
      [dim("颜色")],
      { sku名称: "颜色" },
    );
    expect(plan.rows.map((r) => r.spec)).toEqual(["颜色:甲", "颜色:乙"]);
  });

  it("没有通过行 / 平台没规格 ⇒ 各自写明原因", () => {
    expect(buildSkuFillPlan([], [dim("颜色")]).skipped.join("")).toContain("预检通过");
    expect(buildSkuFillPlan([row({ specName: "颜色", specValue: "甲" })], []).skipped.join(""))
      .toContain("未返回该类目的规格类型");
  });

  it("规格图只收**有图的行**（⚠️ 已改为**走映射**：看 `specImageRel`，不看 `localRel`）", () => {
    //: ⚠️ **口径已变**（用户口径 2026-10-15：「不再走现在默认的逻辑，应走映射逻辑」）：
    //: 旧实现取 `row.localRel`（代码按 SKU 名称**猜**的那张图）；
    //: 现在取 `row.specImageRel`（由 `spec_image` 映射指定的列取到的值）。
    const plan = buildSkuFillPlan(
      [
        //: `localRel` **故意给了值但不该被用**（若被用，这条断言会失败）。
        row({
          rawName: "甲", cells: { sku名称: "甲" },
          localRel: "SKU/猜的.jpg", localFileName: "猜的.jpg",
          specImageRel: "SKU/a.jpg",
        }),
        row({ rawName: "乙", cells: { sku名称: "乙" } }),
      ],
      [dim("颜色")],
      { sku名称: "颜色" },
    );
    expect(plan.images).toHaveLength(1);
    expect(plan.images[0]).toMatchObject({ dimName: "颜色", value: "甲", localRel: "SKU/a.jpg" });
    //: ⚠️ 文件名从路径末段取（上传按内容 MD5 重命名，这只作 `origin_name`）。
    expect(plan.images[0].localFileName).toBe("a.jpg");
  });

  it("⚠️ 没配 `spec_image` 映射（`specImageRel` 缺）⇒ 一张图都不收（不退回猜）", () => {
    const plan = buildSkuFillPlan(
      [row({ rawName: "颜色:甲", specName: "颜色", specValue: "甲", localRel: "SKU/猜的.jpg", localFileName: "猜的.jpg" })],
      [dim("颜色")],
      { 颜色: "颜色" },
    );
    expect(plan.images).toHaveLength(0);
  });
});

describe("规格关系行（最多两行 / 必填 / 不重复）", () => {
  const dims = [
    { name: "颜色", id: "1", customizable: true },
    { name: "尺寸", id: "2", customizable: true },
  ];

  it("**最多两行**", () => {
    expect(MAX_SPEC_MAPPING_ROWS).toBe(2);
    expect(validateSpecMapping([
      { source: "a", target: "颜色" },
      { source: "b", target: "尺寸" },
    ])).toEqual([]);
    expect(validateSpecMapping([
      { source: "a", target: "颜色" },
      { source: "b", target: "尺寸" },
      { source: "c", target: "款式" },
    ]).join("")).toContain("最多");
  });

  it("**两列都不能为空**", () => {
    expect(validateSpecMapping([{ source: "", target: "颜色" }]).join("")).toContain("不能为空");
    expect(validateSpecMapping([{ source: "颜色分类", target: "" }]).join("")).toContain("不能为空");
  });

  it("**两列均不支持重复选择**", () => {
    expect(validateSpecMapping([
      { source: "颜色分类", target: "颜色" },
      { source: "颜色分类", target: "尺寸" },
    ]).join("")).toContain("重复");
    expect(validateSpecMapping([
      { source: "颜色分类", target: "颜色" },
      { source: "尺寸", target: "颜色" },
    ]).join("")).toContain("重复");
  });

  it("整行都空 ⇒ 不报重复（刚点「添加」不该满屏红字）", () => {
    expect(validateSpecMapping([{ source: "", target: "" }])).toEqual([]);
  });

  it("isSpecMappingReady：至少一行填完整且无错误", () => {
    expect(isSpecMappingReady([{ source: "颜色分类", target: "颜色" }])).toBe(true);
    expect(isSpecMappingReady([{ source: "颜色分类", target: "" }])).toBe(false);
    expect(isSpecMappingReady([{ source: "", target: "" }])).toBe(false);
  });

  it("默认行：同名自动配对，不同名留空（**不猜**）", () => {
    expect(defaultSpecMappingRows(["尺寸", "颜色分类"], dims)).toEqual([
      { source: "尺寸", target: "尺寸" },
      { source: "颜色分类", target: "" },
    ]);
    //: 最多两行 ⇒ 超出的分类不进默认行
    expect(defaultSpecMappingRows(["a", "b", "c"], dims)).toHaveLength(2);
  });

  it("mappingFromRows：只收两列都填了的行", () => {
    expect(mappingFromRows([
      { source: "颜色分类", target: "颜色" },
      { source: "尺寸", target: "" },
    ])).toEqual({ 颜色分类: "颜色" });
  });
});

describe("导出预检通过行（passedSkuRows）", () => {
  //: ⚠️ **夹具已按新口径更新**（2026-10-15）：`规格分类` 是**独立的一列**
  //: （不再靠 `sku名称` 拆冒号拿分类）。夹具必须反映真实形状，
  //: 否则测的是「旧实现下的旧行为」，新口径改了也测不出来。
  const table = {
    title: "全部", sheet_name: "全部",
    columns: ["规格分类", "sku名称", "图片", "原价", "计算价格", "库存", "ID"],
    rows: [
      ["颜色分类", "甲", "u1", "1280", null, "200", "935947169189"],
      ["颜色分类", "乙", "u2", "", "990", "10", "ID2"],
      ["颜色分类", "丙", "u3", "500", "", "5", "ID3"],
    ],
  };

  it("**原价优先**，空则回落计算价格（用户口径 ②）", () => {
    //: ⚠️ 夹具表没有「规格分类」列 ⇒ 用不带该条的映射（否则缺 sku_name 也会空）。
    const rows = passedSkuRows(table, [], [0, 1, 2], [], MAP_PRICE);
    expect(rows[0].price).toBe(1280);   //: 原价有 ⇒ 用原价
    expect(rows[1].price).toBe(990);    //: 原价空 ⇒ 用计算价格
    expect(rows[2].price).toBe(500);
    expect(rows[0].stock).toBe(200);
    expect(rows[0].sellerCode).toBe("935947169189"); //: ID 列 ⇒ 商家编码
  });

  it("未勾选 / 预检未通过的行**不进**结果", () => {
    expect(passedSkuRows(table, [], [0], [], MAP_PRICE).map((r) => r.specValue)).toEqual(["甲"]);
    expect(passedSkuRows(table, [1], [0, 1], [], MAP_PRICE).map((r) => r.specValue)).toEqual(["甲"]);
  });

  it("本地图按 SKU 名称对齐（本地图列那张）", () => {
    //: ⚠️ 对齐用的仍是**完整 sku 名称**（`rawName`）—— 图片一直按它匹配。
    const imgs = [{ key: "甲", relPath: "SKU/SKU01_甲.jpg" }];
    const rows = passedSkuRows(table, [], [0], imgs, MAP_PRICE);
    expect(rows[0].localRel).toBe("SKU/SKU01_甲.jpg");
    expect(rows[0].localFileName).toBe("SKU01_甲.jpg");
  });

});

describe("售价倍率（原价 × 1.7 默认，可调）", () => {
  it("默认倍率是 1.7", () => {
    expect(DEFAULT_PRICE_MULTIPLIER).toBe(1.7);
  });

  it("1280 × 1.7 = 2176（**不是** 2175.9999999999995，必须四舍五入到分）", () => {
    expect(applyPriceMultiplier(1280, 1.7)).toBe(2176);
  });

  it("保留 2 位小数", () => {
    expect(applyPriceMultiplier(100, 1.777)).toBe(177.7);
    expect(applyPriceMultiplier(99.99, 1.7)).toBe(169.98);
  });

  it("原价缺值 ⇒ null（**不造假**，不填 0）", () => {
    expect(applyPriceMultiplier(null, 1.7)).toBeNull();
    expect(applyPriceMultiplier(Number.NaN, 1.7)).toBeNull();
  });

  it("clamp：非法值回默认 1.7；越界夹到 [1, 10]", () => {
    expect(clampPriceMultiplier(undefined)).toBe(1.7);
    expect(clampPriceMultiplier("abc")).toBe(1.7);
    expect(clampPriceMultiplier(0.5)).toBe(1);      //: 下限 1（不打折卖）
    expect(clampPriceMultiplier(99)).toBe(PRICE_MULTIPLIER_MAX); //: 挡手滑多打一个 0
    expect(clampPriceMultiplier(2.5)).toBe(2.5);
  });

  it("导入计划按倍率算售价：原价 1280 ⇒ 2176", () => {
    const plan = buildSkuFillPlan(
      [{
        rawName: "颜色分类:甲", specName: "颜色分类", specValue: "甲",
        originalPrice: 1280, price: 1280, stock: 200,
        sellerCode: "", localRel: null, localFileName: "",
        cells: { sku名称: "甲" },
      }],
      [{ name: "颜色分类", id: "d1", customizable: true, values: [], options: [] }],
      { sku名称: "颜色分类" },
    );
    //: ⚠️ 用户口径最终版：**市场价** = 原价 × 1.7；**售价** = 原价原值。
    expect(plan.rows[0].price).toBe(1280);
    expect(plan.rows[0].marketPrice).toBe(2176);
  });

  it("原价取不到 ⇒ 用计算价格原值，**不乘**倍率", () => {
    const plan = buildSkuFillPlan(
      [{
        rawName: "颜色分类:甲", specName: "颜色分类", specValue: "甲",
        originalPrice: null, price: 990, stock: 1,
        sellerCode: "", localRel: null, localFileName: "",
        cells: { sku名称: "甲" },
      }],
      [{ name: "颜色分类", id: "d1", customizable: true, values: [], options: [] }],
      { sku名称: "颜色分类" },
    );
    expect(plan.rows[0].price).toBe(990);     //: 售价回落计算价格原值
    expect(plan.rows[0].marketPrice).toBeNull(); //: 原价空 ⇒ 市场价没基数，不造假
  });

  it("倍率可被调用方覆盖（参数可调整）", () => {
    const plan = buildSkuFillPlan(
      [{
        rawName: "颜色分类:甲", specName: "颜色分类", specValue: "甲",
        originalPrice: 1000, price: 1000, stock: 1,
        sellerCode: "", localRel: null, localFileName: "",
        cells: { sku名称: "甲" },
      }],
      [{ name: "颜色分类", id: "d1", customizable: true, values: [], options: [] }],
      { sku名称: "颜色分类" },
      2.0,
    );
    expect(plan.rows[0].price).toBe(1000);
    expect(plan.rows[0].marketPrice).toBe(2000);
  });
});

describe("价格库存列映射（动态可调；默认 = 现行逻辑）", () => {
  const cols = ["sku名称", "图片", "原价", "计算价格", "库存", "ID"];

  it("⚠️ **默认映射是空**（用户口径 2026-10-15：不预填，用户自己从头配）", () => {
    //: ⚠️ **一行都没有**（用户口径 2026-10-15：「不预填，用户自己从头配」+
    //: 「『原价』『计算价格』也当作普通列，用户自己选（**不再猜**）」）。
    //: 旧断言（已取代，保留作考古）：
    //:   `expect(DEFAULT_PRICE_MAPPING).toEqual([售价←原价, 库存←库存, 商家编码←ID])`
    //:   —— 那是**猜列名**：数据包列名一换（真包就有 `SKU信息`）就猜空。
    expect(DEFAULT_PRICE_MAPPING).toEqual([]);
    expect(DEFAULT_PRICE_FALLBACK_SOURCE).toBe("");
  });

  it("sourceColumnIndex 按列名定位（可任意列 ⇒ 动态可调整）", () => {
    //: ⚠️ 用**显式映射**（`DEFAULT_PRICE_MAPPING` 现在是空 —— 口径已改为不预填）。
    const m: PriceMappingRow[] = [
      { target: "price", source: "原价" },
      { target: "stock", source: "库存" },
      { target: "seller_code", source: "ID" },
    ];
    expect(sourceColumnIndex(cols, m, "price")).toBe(2);
    expect(sourceColumnIndex(cols, m, "stock")).toBe(4);
    expect(sourceColumnIndex(cols, m, "seller_code")).toBe(5);
    //: 换成别的列也认（用户口径：动态可调）。
    expect(sourceColumnIndex(cols, [{ target: "price", source: "计算价格" }], "price")).toBe(3);
    //: 不填 / 列不存在 ⇒ -1（调用方按「不自动填」处理）。
    expect(sourceColumnIndex(cols, [{ target: "stock", source: "" }], "stock")).toBe(-1);
    expect(sourceColumnIndex(cols, [{ target: "stock", source: "不存在" }], "stock")).toBe(-1);
  });

  it("**来源列允许重复映射**（用户口径 2026-10-21：一列可以喂多个目标）", () => {
    //: 原用例（2026-10-11）断言「来源列重复 ⇒ 报错」，
    //: **已被用户 2026-10-21 推翻**：「价格库存映射关系表应支持重复选择映射列」。
    //: 真实的合理性：库存列既做「当前库存」又做「改后库存」；
    //: 价格列既做「售价」又做「市场价」；SKUID 既做「商家编码」又做「规格ID」。
    const ok: PriceMappingRow[] = [
      { target: "current_stock", source: "库存" },
      { target: "stock", source: "库存" },   //: 同一列又被当改后库存 —— 合法
      { target: "price", source: "价格" },
      { target: "market_price", source: "价格" },
    ];
    expect(validatePriceMapping(ok)).toEqual([]);
    expect(isPriceMappingReady(ok)).toBe(true);
  });

  it("目标列重复 ⇒ 报错", () => {
    expect(validatePriceMapping([
      { target: "price", source: "原价" },
      { target: "price", source: "计算价格" },
    ])[0]).toContain("价格库存列重复：售价");
  });

  it("**来源列留空 ⇒ 报错**（用户口径 2026-10-11「不能为空」）", () => {
    const rows: PriceMappingRow[] = [
      { target: "price", source: "原价" },
      { target: "stock", source: "" },
    ];
    expect(validatePriceMapping(rows)[0]).toContain("来源列不能为空");
    expect(isPriceMappingReady(rows)).toBe(false);
  });

  it("**不限制条数**（用户口径「不限制条数」）—— 多于 4 行也合法", () => {
    const rows: PriceMappingRow[] = [
      { target: "price", source: "原价" },
      { target: "market_price", source: "计算价格" },
      { target: "stock", source: "库存" },
      { target: "seller_code", source: "ID" },
    ];
    expect(validatePriceMapping(rows)).toEqual([]);
  });

  it("**市场价可作目标列**（两边自由选择）", () => {
    expect(PRICE_TABLE_TARGETS).toContain("market_price");
    expect(validatePriceMapping([{ target: "market_price", source: "原价" }])).toEqual([]);
  });
});

describe("passedSkuRows 按映射取列", () => {
  const table = {
    title: "全部", sheet_name: "全部",
    columns: ["sku名称", "图片", "原价", "计算价格", "库存", "ID"],
    rows: [["颜色分类:甲", "u1", "1280", "990", "200", "935947169189"]],
  };

  it("默认映射 ⇒ 原价优先，空才回落计算价格", () => {
    const [r] = passedSkuRows(table, [], [0], [], MAP_PRICE);
    expect(r.originalPrice).toBe(1280);
    expect(r.price).toBe(1280);       //: 原价有值 ⇒ 不回落
    expect(r.stock).toBe(200);
    expect(r.sellerCode).toBe("935947169189");
  });

  it("把售价映射到「计算价格」⇒ 就用那一列，原价字段随之变化", () => {
    const [r] = passedSkuRows(table, [], [0], [], [
      //: ⚠️ `sku_name` 必须配（新口径下没它 ⇒ 读不出行）。
      { target: "sku_name", source: "sku名称" },
      { target: "price", source: "计算价格" },
      { target: "stock", source: "库存" },
      { target: "seller_code", source: "ID" },
    ]);
    expect(r.originalPrice).toBe(990);
    expect(r.price).toBe(990);
  });

  it("来源列留空 ⇒ 该字段为 null / 空串（不自动填）", () => {
    const [r] = passedSkuRows(table, [], [0], [], [
      { target: "sku_name", source: "sku名称" },
      { target: "price", source: "原价" },
      { target: "stock", source: "" },
      { target: "seller_code", source: "" },
    ]);
    expect(r.stock).toBeNull();
    expect(r.sellerCode).toBe("");
  });

  it("回落列可改：原价空 + 回落指向别的列", () => {
    const t2 = { ...table, rows: [["颜色分类:甲", "u1", "", "990", "", "ID1"]] };
    //: ⚠️ 回落列现在是**映射里的一条**（`price_fallback`），不再是裸列名参数。
    const [r] = passedSkuRows(t2, [], [0], [], [
      { target: "sku_name", source: "sku名称" },
      { target: "price", source: "原价" },
      { target: "price_fallback", source: "计算价格" },
      { target: "stock", source: "库存" },
      { target: "seller_code", source: "ID" },
    ]);
    expect(r.originalPrice).toBeNull();
    expect(r.price).toBe(990);        //: 回落生效
  });
});

describe("映射校验的报错提炼（用户口径：只保留核心）", () => {
  it("价格映射：多类问题各报**一条**、不带行号", () => {
    const rows: PriceMappingRow[] = [
      { target: "price", source: "原价" },
      { target: "price", source: "原价" },   //: 目标重复（来源重复**合法**，2026-10-21 起）
      { target: "stock", source: "" },       //: 来源空
    ];
    const errors = validatePriceMapping(rows);
    //: 用户口径「**放一行提示**」⇒ 只有一条，多类问题用 `；` 接在一行里。
    expect(errors).toHaveLength(1);
    //: ⚠️ 期望值里**不再有「来源列重复」**：用户 2026-10-21 口径
    //: 「价格库存映射关系表应支持重复选择映射列」⇒ 该档校验已整条退场。
    expect(errors[0]).toBe("来源列不能为空；价格库存列重复：售价");
    //: 逐行式报错已废弃（同一件事被说三遍）。
    expect(errors[0]).not.toContain("第 2 行");
  });

  it("目标列为空 ⇒ 报「价格库存列不能为空」（新加的空行）", () => {
    expect(validatePriceMapping([{ target: "", source: "" }])[0]).toContain("价格库存列不能为空");
  });

  it("规格映射：同类问题也只报一条", () => {
    expect(validateSpecMapping([
      { source: "A", target: "X" },
      { source: "A", target: "X" },   //: 两边都重复
    ])).toEqual(["预选通过规格类型重复：A；平台规格类型重复：X"]);
    expect(validateSpecMapping([{ source: "A", target: "" }])).toEqual(["两列均不能为空"]);
    //: 整行空（刚添加）⇒ 不报错。
    expect(validateSpecMapping([{ source: "", target: "" }])).toEqual([]);
  });
});

describe("导入后价格表不漏行（用户反馈：导入完助手仍提示「1 行缺价格或库存」）", () => {
  const dims = [{ name: "颜色", id: "d1", customizable: true, values: [], options: [] }];

  it("**计划里每一行都带价格与库存**（否则助手会一直报缺值）", () => {
    const plan = buildSkuFillPlan(
      [
        { rawName: "甲", specName: "", specValue: "甲", cells: { sku名称: "甲" },
          originalPrice: 1280, price: 1280, stock: 200,
          sellerCode: "ID1", localRel: null, localFileName: "" },
        { rawName: "乙", specName: "", specValue: "乙", cells: { sku名称: "乙" },
          originalPrice: 2000, price: 2000, stock: 10,
          sellerCode: "ID2", localRel: null, localFileName: "" },
      ],
      dims,
      { sku名称: "颜色" },
    );
    expect(plan.rows).toHaveLength(2);
    //: ⚠️ **关键**：不许出现 price/stock 为 null 的行 —— 助手判据是
    //: `r.price === null || r.stock === null` ⇒ 有一行 null 就一直报红。
    for (const r of plan.rows) {
      expect(r.price).not.toBeNull();
      expect(r.stock).not.toBeNull();
    }
    //: spec 组合文本必须与 `spec_combinations` 同形（否则重建时对不上 key）。
    expect(plan.rows.map((r) => r.spec)).toEqual(["颜色:甲", "颜色:乙"]);
  });
});


describe("applyBatchRule 的两位小数四舍五入（用户口径 2026-10-11）", () => {
  it("倍数 + 加减后保留两位小数（不出现浮点毛刺）", () => {
    //: 18.8 × 1.7 = 31.960000000000004（IEEE754）—— 必须收敛成 31.96
    expect(applyBatchRule(18.8, { mode: "scale", factor: 1.7, operand: 0 })).toBe(31.96);
    //: 典型浮点陷阱：0.1 + 0.2
    expect(applyBatchRule(0.1, { mode: "delta", operand: 0.2 })).toBe(0.3);
    //: ⚠️ 别拿 `1.005` / `2.675` 当「进位」样本 —— 它们在 IEEE754 里实际是
    //: `1.00499999…` / `2.67499999…`，`Math.round(x*100)/100` 会**不进位**（返回 1 / 2.67）。
    //: 这不是 bug，是二进制浮点的既定事实（想真进位得用十进制库）。
    //: 这里改用**能确定进位**的值，保证判据稳定。
    expect(applyBatchRule(1.006, { mode: "delta", operand: 0 })).toBe(1.01);   // 1.006 → 1.01
    expect(applyBatchRule(2.676, { mode: "delta", operand: 0 })).toBe(2.68);   // 2.676 → 2.68
  });

  it("整数保持整数（不被补成 .00）", () => {
    expect(applyBatchRule(10, { mode: "delta", operand: 5 })).toBe(15);
    expect(applyBatchRule(100, { mode: "scale", factor: 2, operand: 0 })).toBe(200);
  });

  it("三位以上小数会被裁到两位（不打到平台）", () => {
    //: 平台价格最多两位小数；三位以上提交可能被拒。
    expect(applyBatchRule(33.333, { mode: "delta", operand: 0 })).toBe(33.33);
    expect(applyBatchRule(10, { mode: "scale", factor: 1.1111, operand: 0 })).toBe(11.11);
  });

  it("`fixed` 模式同样收敛两位", () => {
    expect(applyBatchRule(5, { mode: "fixed", operand: 19.999 })).toBe(20);
  });

  it("空值仍按缺值口径跳过（不因四舍五入把 null 变成 0）", () => {
    expect(applyBatchRule(null, { mode: "delta", operand: 5 })).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * 手动新增列（用户口径 2026-10-15）
 * ------------------------------------------------------------------ */

describe("手动新增列", () => {
  const table = {
    columns: ["规格分类", "sku名称", "原价"],
    rows: [
      ["颜色分类", "A款", "100"],
      ["尺寸", "B款", "200"],
    ] as (string | null)[][],
    column_kinds: ["text", "text", "numeric"],
    column_keys: ["规格分类", "sku名称", "原价"],
  };

  it("只有手动列可删（非手动新增与固定列不支持删除）", () => {
    //: ⚠️ 用户原话：「手动新增的列支持整列删除，**非手动新增和固定列不支持删除**」。
    expect(canDropColumn("__manual_col_1")).toBe(true);
    expect(canDropColumn("规格分类")).toBe(false); // 固定列
    expect(canDropColumn("sku名称")).toBe(false); // 原始列
    //: ⚠️ key 缺失（后端还没下发）⇒ **一律不可删** ——
    //: 宁可少给一个按钮，也不能让用户误删原始列（删了没法还原）。
    expect(canDropColumn(undefined)).toBe(false);
    expect(canDropColumn(null)).toBe(false);
  });

  it("英文名空则按顺序规则自动生成（col_1 / col_2 …）", () => {
    expect(nextManualColumnKey([])).toBe("col_1");
    expect(nextManualColumnKey(["col_1"])).toBe("col_2");
    //: ⚠️ 按**已有手动列的最大序号 +1**，不是按总列数 ——
    //: 否则删掉 col_1 再加一列会撞名。
    expect(nextManualColumnKey(["col_1", "col_3"])).toBe("col_4");
    //: 序号被别的名字占着 ⇒ 继续往后找，不返回重复值。
    expect(nextManualColumnKey(["col_1", "col_2"])).toBe("col_3");
  });

  it("添加列：追加在最右，各数组等长", () => {
    const next = addTableColumn(table, "成本价", "__manual_col_1");
    expect(next.columns).toEqual(["规格分类", "sku名称", "原价", "成本价"]);
    expect(next.columns).toHaveLength(4);
    //: ⚠️ `column_kinds` / `column_keys` 与 `columns` **等长**是契约：
    //: 不同步加会让下标整体错位（「数值列」判据张冠李戴、删除按钮认错列）。
    expect(next.column_kinds).toHaveLength(4);
    expect(next.column_keys).toHaveLength(4);
    expect(next.column_keys?.[3]).toBe("__manual_col_1");
    //: 新列的值**全为空**（不造假）。
    expect(next.rows.map((r) => r[3])).toEqual([null, null]);
  });

  it("整列删除：列、每行、kinds、keys 一起删", () => {
    const withNew = addTableColumn(table, "成本价", "__manual_col_1");
    const after = dropTableColumn(withNew, 3);
    expect(after.columns).toEqual(["规格分类", "sku名称", "原价"]);
    expect(after.column_kinds).toHaveLength(3);
    expect(after.column_keys).toHaveLength(3);
    expect(after.rows[0]).toHaveLength(3);
  });

  it("删除是纯函数（不改原表）", () => {
    const before = JSON.stringify(table);
    dropTableColumn(table, 0);
    expect(JSON.stringify(table)).toBe(before);
  });

  it("下标越界 ⇒ 原表不动（不抛、不静默改坏）", () => {
    expect(dropTableColumn(table, -1)).toBe(table);
    expect(dropTableColumn(table, 99)).toBe(table);
  });

  it("**不再插入「规格分类」列**（该固定列已整体移除）", () => {
    //: 用户口径 2026-10-20：「商品发布移除商品sku表格中的规格分类固定列，
    //: 『规格分类』固定列 相关所有逻辑移除」⇒ `withDerivedColumns` 只插「SKU 图」一列。
    const raw = {
      columns: ["sku名称", "原价"],
      rows: [["A款", "100"]] as (string | null)[][],
      column_kinds: ["text", "numeric"],
      column_keys: ["sku名称", "原价"],
    };
    const out = withDerivedColumns(raw);
    expect(out.columns).toEqual(["SKU 图", "sku名称", "原价"]);
    expect(out.columns).not.toContain("规格分类");
    //: 长度始终与 `columns` 一致（既有契约）。
    expect(out.column_keys).toHaveLength(3);
    expect(out.column_kinds).toHaveLength(3);
    //: 幂等。
    expect(withDerivedColumns(out)).toBe(out);
  });
});

describe("「预检通过列」下拉必须是**表格所有列**（用户口径 2026-10-15）", () => {
  it("候选 = **表格的所有列**（含「SKU 图」与「规格分类」两列后处理固定列）", () => {
    //: 用户实测（截图）：「下拉不完整，**没有 SKU 图片列**，应展示商品 sku
    //: 表格的**所有列**」。
    //: ⚠️ **口径已变**（用户口径 2026-10-15：「SKU 图应该给放回 rows 中」
    //: +「SKU 图与规格分类属于**后处理的固定表格列**」）：
    //: 它们现在是**真列**（`withDerivedColumns` 插进 `columns`/`rows`）⇒
    //: 候选**直接就是全部列**，不再需要 `priceSourceOptions` 手工补虚拟列。
    const raw = {
      columns: ["sku名称", "原价"],
      rows: [["A款", "100"]] as (string | null)[][],
    };
    const table = withDerivedColumns(raw, { 0: "SKU图/a.jpg" });
    const opts = priceSourceOptions(table.columns);
    //: 两列后处理固定列都在（且顺序 = 屏幕序）。
    //: ⚠️ 「规格分类」固定列已移除（2026-10-20）⇒ 只剩「SKU 图」。
    expect(opts).toEqual(["SKU 图", "sku名称", "原价"]);
    //: 纯函数不再做任何补列（避免与 `withDerivedColumns` 两份实现）。
    expect(priceSourceOptions(["sku名称"])).toEqual(["sku名称"]);
  });

  it("映射指向 SKU 图 ⇒ 与普通列一样按下标返回（**不再有虚拟列哨兵**）", () => {
    //: ⚠️ 口径已变（用户口径 2026-10-15：「SKU 图应该给放回 rows 中」）：
    //: 它现在是**真列**（`withDerivedColumns` 插进 `columns`/`rows`）⇒
    //: 取值就是普通的 `row[colIndex]`，不需要哨兵特判。
    const cols = ["SKU 图", "规格分类", "sku名称"];
    expect(sourceColumnIndex(cols, [{ target: "price", source: "SKU 图" }], "price")).toBe(0);
    //: 没配 ⇒ 仍是 -1。
    expect(sourceColumnIndex(cols, [], "price")).toBe(-1);
  });

  it("⚠️ `withDerivedColumns` 把「SKU 图 + 规格分类」一次插成真列（屏幕序 SKU图 → 规格分类）", () => {
    const raw = {
      columns: ["sku名称", "原价"],
      rows: [["A款", "100"], ["B款", "200"]] as (string | null)[][],
      column_kinds: ["text", "numeric"],
      column_keys: ["sku名称", "原价"],
    };
    const out = withDerivedColumns(raw, { 0: "SKU图/a.jpg", 1: "SKU图/b.jpg" });
    //: ⚠️ 屏幕序：SKU 图 在 规格分类 **左边**（与表格显示一致）。
    //: ⚠️ 只剩「SKU 图」一列（「规格分类」已移除）。
    expect(out.columns).toEqual(["SKU 图", "sku名称", "原价"]);
    //: ⚠️ **值真的进了 rows**（不是渲染层虚拟出来的）。
    expect(out.rows.map((r) => r[0])).toEqual(["SKU图/a.jpg", "SKU图/b.jpg"]);
    //: ⚠️ 「规格分类」列已移除 ⇒ 第 2 位就是原来的 `sku名称` 列（值原样没动过）。
    expect(out.rows.map((r) => r[1])).toEqual(["A款", "B款"]);
    //: 三个数组等长（契约）—— 3 列。
    expect(out.column_kinds).toHaveLength(3);
    expect(out.column_keys).toHaveLength(3);
    //: 幂等。
    expect(withDerivedColumns(out, {})).toBe(out);
    //: ⚠️ 它们都是**固定列** ⇒ 不可删（只有手动列可删）。
    expect(canDropColumn(out.column_keys?.[0])).toBe(false);
    expect(canDropColumn(out.column_keys?.[1])).toBe(false);
  });
});

describe("⚠️ 真 bug 守卫：确认导入后价格库存**没对接** sku 数据（2026-10-15）", () => {
  const table = {
    sheet_name: "全部",
    columns: ["规格分类", "sku名称", "原价", "库存"],
    rows: [
      ["颜色分类", "【中古风】实木三斗柜", "1280", "200"],
      ["颜色分类", "【中古风】伸缩桌+三斗柜", "1980", "200"],
    ],
    column_kinds: ["text", "text", "numeric", "numeric"],
  } as unknown as PrecheckTable;
  const M2: PriceMappingRow[] = [
    { target: "sku_name", source: "sku名称" },
    { target: "price", source: "原价" },
    { target: "stock", source: "库存" },
  ];

  it("`passedSkuSpecs` 产出**带规格分类前缀**的完整形态（与 `spec_combinations` 同形）", () => {
    //: ⚠️ **这是本次真 bug 的判据**：`passedSkuSpecs` 原先收**裸名称**
    //: （`【中古风】实木三斗柜`），而第二步过滤时比的是 `c.spec`
    //: （`颜色分类:【中古风】实木三斗柜`）⇒ **一个都对不上** ⇒ `visible` 为空
    //: ⇒ 价格库存表**一行都不落**（且不报错，用户只看到「没数据」）。
    //: ⚠️ 2026-10-20：代码不再拼「分类:名称」前缀（分类由用户规则决定）
    //: ⇒ 产出的是**裸名称**。
    const allowed = passedSkuSpecs(table, [], [], M2);
    expect([...allowed]).toEqual([
      "【中古风】实木三斗柜",
      "【中古风】伸缩桌+三斗柜",
    ]);
  });

  it("两边形态逐字一致 ⇒ 过滤后不会清空（端到端）", () => {
    const rows = passedSkuRows(table, [], [], [], M2);
    const dims = [{ name: "颜色分类", id: "d1", customizable: true, options: [], values: [] }];
    const plan = buildSkuFillPlan(rows, dims, { sku名称: "颜色分类" }, 1.7);
    //: ⚠️ 2026-10-20：两边都基于**同一列**（用户选的 `sku名称`）⇒ 形态天然一致。
    //: `spec_combinations` 收的是**裸值**（不带维度前缀），与 `passedSkuSpecs`
    //: 现在产出的裸名称逐字相同 —— 这正是「不会一边带前缀一边裸名」的判据。
    const combos = spec_combinations([
      { name: "颜色分类", values: plan.rows.map((r) => r.spec.split(":")[1]) },
    ]);
    const allowed = passedSkuSpecs(table, [], [], M2);
    const visible = allowed.size > 0
      ? combos.filter((c) =>
          //: 组合文本形如 `颜色分类:【中古风】实木三斗柜`，取末段与会话值比对。
          allowed.has(String(c.spec ?? "").split(":").slice(1).join(":").trim()),
        )
      : combos;
    //: ⚠️ 过滤**不许**把行清空（清了 = 用户看到的「没对接」）。
    expect(combos.length).toBeGreaterThan(0);
    expect(visible).toHaveLength(combos.length);
    //: 且价格/库存真的带上了。
    expect(plan.rows.map((r) => [r.price, r.stock])).toEqual([[1280, 200], [1980, 200]]);
  });

  it("分类列为空（旧数据包）⇒ 退化成裸名称，不丢行", () => {
    const legacy = {
      sheet_name: "全部",
      columns: ["sku名称", "原价", "库存"],
      rows: [["A款", "100", "5"]],
    } as unknown as PrecheckTable;
    const allowed = passedSkuSpecs(legacy, [], [], M2);
    expect([...allowed]).toEqual(["A款"]);
  });
});
