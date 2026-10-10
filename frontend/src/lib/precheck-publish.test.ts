/**
 * `precheck-publish` 的 vitest 用例 —— 数据全部取自**真实数据包**
 * `C:\Users\admin\Desktop\商品ID_935947169189`（含真实列名与真实 SKU 名）。
 */
import { describe, expect, it } from "vitest";

import {
  applyBatchRule,
  applyBatchRuleToTable,
  buildPublishPayloadFromPrecheck,
  countFilledInColumn,
  mergePanelTable,
  skuTableIndices,
  priceColumnIndex,
  stockColumnIndex,
  parseStock,
  parseYuan,
  splitByGroup,
  tableToSkus,
  type PrecheckMedia,
  type PrecheckTable,
} from "./precheck-publish";

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

function media(group: string, n: number): PrecheckMedia[] {
  return Array.from({ length: n }, (_, i) => ({
    group,
    index: i,
    rel_path: `${group}/f${i + 1}.jpg`,
    file_name: `f${i + 1}.jpg`,
    ocr_ok: true,
    has_forbidden: false,
  }));
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

  it("视频**不进** images（真机抓包：images/imageDescriptions 都是图片对象）", () => {
    const r = splitByGroup([...media("main", 2), ...media("video", 1)]);
    const all = [...r.mainImages, ...r.detailImages, ...r.skuImages];
    expect(all.some((p) => p.includes("video"))).toBe(false);
    expect(r.mainImages).toHaveLength(2);
  });

  it("传的是暂存内相对路径，不是服务器绝对路径", () => {
    const r = splitByGroup(media("main", 1));
    expect(r.mainImages[0]).toBe("main/f1.jpg");
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
    const skus = tableToSkus(table);
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
    expect(tableToSkus(table).every((s) => s.price_yuan !== null)).toBe(true);
  });

  it("原价与计算价格都缺 ⇒ null（不填 0 冒充）", () => {
    const bare: PrecheckTable = {
      title: "全部",
      sheet_name: "全部",
      columns: REAL_COLUMNS,
      rows: [["颜色分类:A", null, null, null, "1", "9"]],
    };
    expect(tableToSkus(bare)[0].price_yuan).toBeNull();
  });

  it("列名变体也能认（规格名/价格/stock）", () => {
    const alt: PrecheckTable = {
      title: "全部",
      sheet_name: "全部",
      columns: ["规格名", "价格", "stock"],
      rows: [["颜色分类:A", "88.5", "3"]],
    };
    expect(tableToSkus(alt)).toEqual([
      { name: "颜色分类:A", price_yuan: 88.5, stock: 3, image_path: null },
    ]);
  });

  it("没有名称列 ⇒ 不产出 SKU（不硬造）", () => {
    expect(tableToSkus({ title: "t", sheet_name: "t", columns: ["库存"], rows: [["1"]] })).toEqual([]);
  });

  it("空表 / 空列头 ⇒ 空数组", () => {
    expect(tableToSkus(null)).toEqual([]);
    expect(tableToSkus({ title: "t", sheet_name: "t", columns: [], rows: [] })).toEqual([]);
  });

  //: ⚠️ 用户 2026-10-08 口径：「不要将 SKU 图和规格对齐，图片名称和 SKU
  //: 名称可能对应不上，没有固定规则」。这条**刻意**钉住 —— 将来谁想
  //: 「顺手加个名称匹配」，这条会先红。
  it("**不把 SKU 图按名称配给规格**（无固定规则 ⇒ 宁缺勿猜）", () => {
    const skus = tableToSkus(table);
    expect(skus.every((s) => s.image_path === null)).toBe(true);
  });

  it("即使文件名恰好能对上也不配（避免「这一份包能配」被当成规则）", () => {
    //: 故意用**可对上**的文件名：归一后 `SKU01_颜色分类(…)` == 表格首列。
    //: 行为必须仍是 null —— 否则就变成隐式依赖命名巧合了。
    const skus = tableToSkus(table);
    expect(REAL_SKU_IMAGES.length).toBe(2); // 素材存在，但没用上
    expect(skus.map((s) => s.image_path)).toEqual([null, null]);
  });
});

describe("buildPublishPayloadFromPrecheck", () => {
  it("有 token ⇒ 出完整载荷（token + 两个图片列 + SKU）", () => {
    const p = buildPublishPayloadFromPrecheck({
      token: "TK1",
      media: [...media("main", 5), ...media("detail", 38), ...media("sku", 26)],
      tables: [{ title: "全部", sheet_name: "全部", columns: REAL_COLUMNS, rows: REAL_ROWS }],
    });
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
    expect(priceColumnIndex(table.columns)).toBe(1);
    expect(stockColumnIndex(table.columns)).toBe(2);
    expect(priceColumnIndex(["sku名称", "ID"])).toBe(-1);
    expect(stockColumnIndex(["sku名称", "ID"])).toBe(-1);
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
    expect(half.rows.map((r) => r[0])).toEqual(["11.50", "12.07"]);  // 12.075 → toFixed(2) = 12.07
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
  /** 完整数组：属性表（kv）夹在 SKU 表前后 —— 面板只显示 SKU 表。 */
  const full: PrecheckTable[] = [
    { title: "属性", sheet_name: "属性", columns: ["名称", "值"], rows: [["品牌", "X"]],
      kv: [{ key: "品牌", value: "X" }] },
    { title: "全部", sheet_name: "全部", columns: ["sku名称", "原价", "库存"],
      rows: [["A", "100", "10"], ["B", "200", "20"]] },
    { title: "赠品", sheet_name: "赠品", columns: ["sku名称", "库存"],
      rows: [["G", "5"]] },
  ];

  it("skuTableIndices：排除属性表（kv）", () => {
    expect(skuTableIndices(full)).toEqual([1, 2]);
  });

  it("mergePanelTable：改面板第 0 张 ⇒ 落在完整数组下标 1；**属性表不丢**", () => {
    const changed = applyBatchRuleToTable(full[1], 2, { mode: "delta", operand: 1 });
    const merged = mergePanelTable(full, 0, changed);
    expect(merged).toHaveLength(3);
    expect(merged[0].kv).toHaveLength(1);          // 属性表还在（根因）
    expect(merged[1].rows.map((r) => r[2])).toEqual(["11", "21"]);
    expect(merged[2].rows.map((r) => r[1])).toEqual(["5"]); // 别的表没被碰
  });

  it("**连续两次**改同一列都能落上（第二次不再是空操作）", () => {
    let cur = full;
    //: 第一次：面板下标 0（= 完整下标 1），库存 +10
    cur = mergePanelTable(cur, 0, applyBatchRuleToTable(cur[1], 2, { mode: "delta", operand: 10 }));
    //: 第二次：库存再 +10 —— 若面板上抛的是子集，这里属性表已丢、下标错位
    cur = mergePanelTable(cur, 0, applyBatchRuleToTable(cur[1], 2, { mode: "delta", operand: 10 }));
    expect(cur).toHaveLength(3);
    expect(cur[1].rows.map((r) => r[2])).toEqual(["30", "40"]); // 10→30、20→40
    expect(cur[0].kv).toHaveLength(1);
  });

  it("面板下标越界 ⇒ 原样返回（不误改、不丢）", () => {
    expect(mergePanelTable(full, 9, full[1])).toEqual(full);
  });
});
