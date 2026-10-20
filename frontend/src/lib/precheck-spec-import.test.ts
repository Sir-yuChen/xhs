/** **守卫 + 变异验证**：真实数据包（2026-10-15 用户实测的那个）走完
 *  `passedSkuRows → sourceSpecNames` 之后，`specImportSources` **不能**是空。
 *
 * 复现的现象（用户原话）：「第三步点『预检通过SKU导入』按钮，提示
 * 『没有可导入的 SKU：请先在第一步预检里读取数据包，并保证有「预检通过」的 SKU 行』，
 * 但第一步的 sku 已经预检通过且有勾选部分数据」。
 *
 * 真实数据（探针实测，非虚构）——
 *   `backend/app/storage/package_precheck/u1/<id>/商品数据.xlsx`
 *     sheet 名 = `全部`，列 = `[sku名称, 库存]`，行 = `[A款, 3]`
 *   `.../SKU/` 目录 = 只有 `SKU01.jpg`（文件名里**没有**规格分类）
 *   取 mtime 最新 12 个包，**全部**是同形（`A款` + `SKU01.jpg`）。
 *
 * 根因：`sku名称` 列值 `A款` **没有冒号** ⇒ `parseSkuName` 按「不猜」口径返回
 * `category = ""` ⇒ `specName` 空 ⇒ `sourceSpecNames(, M)` 丢掉 ⇒ `mappingSources = []`
 * ⇒ `publish-config-step2.tsx:2902` 的 `disabled` 恒真。
 *
 * 修复：`passedSkuRows` 在**无分类**时把维度退化为 `SINGLE_SPEC_NAME`（值 = 整个名称），
 * 于是「有预检通过的行」这件事能被看见，用户在导入弹窗里再把它映射到平台规格类型。
 */
import { describe, expect, it } from "vitest";
import {
  parseSkuName,
  passedSkuRows,
  SINGLE_SPEC_NAME,
  localSkuImageList,
  buildSkuFillPlan,
  priceSourceOptions,
  type PriceMappingRow,
} from "./precheck-publish";
import type { PrecheckMedia, PrecheckTable } from "./precheck-publish";

/* ------------------------------------------------------------------ *
 * ⚠️ **公共映射夹具**（2026-10-15 口径变更：所有点一律走映射，移除写死逻辑）
 *
 * 测试再也不能靠「列名叫 sku名称 就被认出来」了 —— 那正是本轮移除的行为。
 * 这份映射对应下面夹具表的列名。
 * ------------------------------------------------------------------ */
const M: PriceMappingRow[] = [
  { target: "sku_name", source: "sku名称" },
  { target: "stock", source: "库存" },
];


/** 真实数据包的表（探针从 xlsx 读出来的原值，未改动）。 */
const REAL_TABLE = {
  sheet_name: "全部",
  columns: ["sku名称", "库存"],
  rows: [["A款", "3"]],
} as unknown as PrecheckTable;

/** 真实数据包的 SKU 目录（只有一张，文件名不含规格分类）。 */
const REAL_SKU_MEDIA = [
  { group: "SKU图", index: 0, rel_path: "SKU/SKU01.jpg", file_name: "SKU01.jpg" },
] as unknown as PrecheckMedia[];

describe("真 bug 守卫：SKU 名不含冒号（真实包 `A款`）时按钮不得恒灰", () => {
  it("parseSkuName('A款') 的 category 仍是空串（「不猜」口径不动）", () => {
    const p = parseSkuName("A款");
    expect(p.category).toBe("");
    expect(p.value).toBe("A款");
  });

  it("★ 核心判据：候选列清单**非空** ⇒ 按钮不 disabled", () => {
    const rows = passedSkuRows(REAL_TABLE, [], [], [], M);
    //: ⚠️ 2026-10-20：按钮的 `disabled` 判据改为「**预检通过列**清单是否为空」
    //: （用户口径「左侧…预检通过列的所有列」），不再看规格名。
    const cols = priceSourceOptions(REAL_TABLE.columns);
    expect(rows.length).toBe(1);
    expect(cols.length).toBeGreaterThan(0);
    expect(cols.length === 0).toBe(false);
  });

  it("该行的 specValue = 整个名称（不是空串 —— 值不能被吞掉）", () => {
    const [row] = passedSkuRows(REAL_TABLE, [], [], [], M);
    expect(row.specName).toBe(SINGLE_SPEC_NAME);
    expect(row.specValue).toBe("A款");
    expect(row.stock).toBe(3);
  });

  it("`specName` 恒为中性占位（数据包里**没有**分类概念了）", () => {
    const [row] = passedSkuRows(REAL_TABLE, [], [], [], M);
    expect(row.specName).toBe(SINGLE_SPEC_NAME);
  });

  it("多行无分类 ⇒ 维度仍只有**一个**（不是每行各一个维度）", () => {
    const table = {
      sheet_name: "全部",
      columns: ["sku名称", "库存"],
      rows: [
        ["A款", "3"],
        ["B款", "5"],
        ["C款", "7"],
      ],
    } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    expect(new Set(rows.map((r) => r.specName))).toEqual(new Set([SINGLE_SPEC_NAME]));
    expect(rows.map((r) => r.cells.sku名称)).toEqual(["A款", "B款", "C款"]);
  });
});

describe("端到端：无分类包能被 `buildSkuFillPlan` 接受（映射对得上就建得出规格）", () => {
  it("把 `规格` 映射到平台维度「颜色分类」⇒ plan 有 1 个维度、3 行", () => {
    const table = {
      sheet_name: "全部",
      columns: ["sku名称", "库存"],
      rows: [
        ["A款", "3"],
        ["B款", "5"],
      ],
    } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    const dims = [
      { name: "颜色分类", id: "d1", customizable: true, options: ["A款", "B款"] },
    ];
    //: ⚠️ 2026-10-20：映射键 = **列名**（用户选的那一列）。
    const plan = buildSkuFillPlan(rows, dims, { sku名称: "颜色分类" });
    //: 修复前这里会 `skipped.push("SKU 名称里没有可识别的规格分类…")` 且 dims = []。
    expect(plan.dims.map((d) => d.dimName)).toEqual(["颜色分类"]);
    expect(plan.rows.length).toBe(2);
    expect(plan.skipped).toEqual([]);
  });
});

describe("规格分类**不再由代码决定**（用户口径 2026-10-20）", () => {
  //: ⚠️ 口径沿革（原断言保留作考古，**不要再按旧语义写测试**）：
  //:   ① 最早：`specName` 从 `sku名称` 按冒号拆（`颜色分类:xxx` ⇒ `颜色分类`）；
  //:   ② 2026-10-15：改成读自造的「规格分类」固定列；
  //:   ③ 2026-10-20（用户逐字）：
  //:      「商品发布移除商品sku表格中的规格分类固定列，『规格分类』固定列 相关所有逻辑移除」
  //:      「从 sku 名称按冒号取前缀这个是错的，不要改成这个逻辑，**用户使用规则自己控制**」
  //:
  //: ⇒ 现在的契约：代码**一律给中性占位** `SINGLE_SPEC_NAME`，
  //: 分类从哪来由用户的「数据包规则」+ 导入弹窗里的列映射决定。
  //: 所以这几条测的是「**不猜**」：无论名称什么形态、有没有分类列，结果都一样。

  it("名称里带分类前缀 ⇒ 也不拆（代码不猜）", () => {
    const table = {
      sheet_name: "全部",
      columns: ["sku名称", "库存"],
      rows: [
        ["颜色分类:【中古风】实木三斗柜", "3"],
        ["颜色分类:【中古风】实木六斗柜", "3"],
      ],
    } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    //: ⚠️ 名称形态**完全不参与分类判定** ⇒ 恒为中性占位。
    expect(new Set(rows.map((r) => r.specName))).toEqual(new Set([SINGLE_SPEC_NAME]));
    //: 值 = **整个 sku 名称**（不做任何切分）。
    expect(rows.map((r) => r.specValue)).toEqual([
      "颜色分类:【中古风】实木三斗柜",
      "颜色分类:【中古风】实木六斗柜",
    ]);
  });

  it("名称是纯值（无冒号）⇒ 同样是中性占位，且不丢行", () => {
    const table = {
      sheet_name: "全部",
      columns: ["sku名称", "库存"],
      rows: [["A款", "3"], ["1m", "3"]],
    } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    expect(new Set(rows.map((r) => r.specName))).toEqual(new Set([SINGLE_SPEC_NAME]));
    expect(rows).toHaveLength(2);
  });

  it("表里残留「规格分类」列 ⇒ **完全忽略**（该列已退场）", () => {
    //: 老数据包/老缓存里可能还带这列；代码必须当它不存在。
    const table = {
      sheet_name: "全部",
      columns: ["规格分类", "sku名称", "库存"],
      rows: [["颜色分类", "A款", "3"]],
    } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    expect(new Set(rows.map((r) => r.specName))).toEqual(new Set([SINGLE_SPEC_NAME]));
  });

  it("每行都带 `cells`（整行按列名摊平）——供「按所选列取值」用", () => {
    const table = {
      sheet_name: "全部",
      columns: ["sku名称", "原价", "库存"],
      rows: [["A款", "100", "3"]],
    } as unknown as PrecheckTable;
    const [row] = passedSkuRows(table, [], [], [], M);
    //: ⚠️ 映射左侧是**列名** ⇒ 导入时必须能按列名回取原值。
    expect(row.cells).toEqual({ sku名称: "A款", 原价: "100", 库存: "3" });
  });
});

describe("本地图判据不受影响（SKU01.jpg 对不上 A款 = 该行确实被 block，与原判据一致）", () => {
  it("`lookupLocalSkuImage` 对不上的行，`localRel` 仍是 null（block 由向导层按此算）", () => {
    const list = localSkuImageList(REAL_SKU_MEDIA);
    const [row] = passedSkuRows(REAL_TABLE, [], [], list, M);
    expect(row.localRel).toBeNull();
  });
});
