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
  sourceSpecNames,
  realSpecNames,
  SINGLE_SPEC_NAME,
  localSkuImageList,
  buildSkuFillPlan,
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

  it("★ 核心判据：`sourceSpecNames(passedSkuRows(真实表), M)` **非空** ⇒ 按钮不 disabled", () => {
    const rows = passedSkuRows(REAL_TABLE, [], [], [], M);
    const names = sourceSpecNames(rows);
    //: 修复前：rows.length = 1 但 names = [] ⇒ `length === 0` 恒真 ⇒ 按钮恒灰。
    //: 修复后：names = ["规格"]。
    expect(rows.length).toBe(1);
    expect(names).toEqual([SINGLE_SPEC_NAME]);
    //: 直接断言「按钮 disabled 判据」（`publish-config-step2.tsx:2902`）为 false。
    expect(names.length === 0).toBe(false);
  });

  it("该行的 specValue = 整个名称（不是空串 —— 值不能被吞掉）", () => {
    const [row] = passedSkuRows(REAL_TABLE, [], [], [], M);
    expect(row.specName).toBe(SINGLE_SPEC_NAME);
    expect(row.specValue).toBe("A款");
    expect(row.stock).toBe(3);
  });

  it("`realSpecNames` 仍是空（数据包里确实**没有**真分类）—— UI 措辞据此区分", () => {
    const rows = passedSkuRows(REAL_TABLE, [], [], [], M);
    expect(realSpecNames(rows)).toEqual([]);
    expect(sourceSpecNames(rows)).toEqual([SINGLE_SPEC_NAME]);
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
    expect(sourceSpecNames(rows)).toEqual([SINGLE_SPEC_NAME]);
    expect(rows.map((r) => r.specValue)).toEqual(["A款", "B款", "C款"]);
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
    const plan = buildSkuFillPlan(rows, dims, { [SINGLE_SPEC_NAME]: "颜色分类" });
    //: 修复前这里会 `skipped.push("SKU 名称里没有可识别的规格分类…")` 且 dims = []。
    expect(plan.dims.map((d) => d.dimName)).toEqual(["颜色分类"]);
    expect(plan.rows.length).toBe(2);
    expect(plan.skipped).toEqual([]);
  });
});

describe("规格分类读「规格分类」固定列（用户口径 2026-10-15：拆冒号逻辑移除）", () => {
  //: ⚠️ **口径已变**（原断言见下方 docstring，保留作为考古）：
  //: 旧口径 = `specName` 从 `sku名称` **按冒号拆**（`颜色分类:xxx` ⇒ `颜色分类`）。
  //: 用户口径 2026-10-15：
  //:   「`specName = parsed.category || SINGLE_SPEC_NAME`，是从 sku名称 拆冒号得
  //:     ⇒ **移除逻辑**，应读 sku 预检通过的**规格分类固定列**」
  //: 原因：拆冒号是「分类由代码派生」时期的产物；现在分类是用户填的**真列**，
  //: 再猜名称形态会出现两份矛盾的事实（用户实测：填了「颜色分类」却被报
  //: 「平台规格类型里没有与数据包『规格』对应的维度」）。

  it("读「规格分类」列 ⇒ specName = 该列的值（与 sku名称 形态无关）", () => {
    const table = {
      sheet_name: "全部",
      columns: ["规格分类", "sku名称", "库存"],
      rows: [
        ["颜色分类", "颜色分类:【中古风】实木三斗柜", "3"],
        ["颜色分类", "【中古风】实木六斗柜", "3"],
      ],
    } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    expect(sourceSpecNames(rows)).toEqual(["颜色分类"]);
    expect(realSpecNames(rows)).toEqual(["颜色分类"]);
    //: ⚠️ 值 = **整个 sku 名称**（分类已独立成列，名称里的前缀不再切掉）。
    expect(rows.map((r) => r.specValue)).toEqual([
      "颜色分类:【中古风】实木三斗柜",
      "【中古风】实木六斗柜",
    ]);
  });

  it("**没有冒号也不再退化成「规格」**（名称形态不再参与分类判定）", () => {
    //: 这正是真机场景：`sku名称` 是 `A款` 这种纯值，而分类在独立那一列里。
    const table = {
      sheet_name: "全部",
      columns: ["规格分类", "sku名称", "库存"],
      rows: [
        ["颜色分类", "A款", "3"],
        ["尺寸", "1m", "3"],
      ],
    } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    expect(sourceSpecNames(rows)).toEqual(["颜色分类", "尺寸"]);
    expect(sourceSpecNames(rows)).not.toContain(SINGLE_SPEC_NAME);
  });

  it("分类列缺失（旧数据包）⇒ 退化为单维度 `规格`，不丢行", () => {
    //: ⚠️ 宁可让用户看到占位名自己去改映射，也不静默丢整行
    //: （丢了会让「预检通过SKU导入」按钮恒灰，用户完全无从下手）。
    const table = {
      sheet_name: "全部",
      columns: ["sku名称", "库存"],
      rows: [["A款", "3"]],
    } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    expect(sourceSpecNames(rows)).toEqual([SINGLE_SPEC_NAME]);
    expect(rows).toHaveLength(1);
  });

  it("分类列为空（有列但该行没填）⇒ 同样退化，不丢行", () => {
    const table = {
      sheet_name: "全部",
      columns: ["规格分类", "sku名称", "库存"],
      rows: [["", "A款", "3"]],
    } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    expect(sourceSpecNames(rows)).toEqual([SINGLE_SPEC_NAME]);
  });
});

describe("本地图判据不受影响（SKU01.jpg 对不上 A款 = 该行确实被 block，与原判据一致）", () => {
  it("`lookupLocalSkuImage` 对不上的行，`localRel` 仍是 null（block 由向导层按此算）", () => {
    const list = localSkuImageList(REAL_SKU_MEDIA);
    const [row] = passedSkuRows(REAL_TABLE, [], [], list, M);
    expect(row.localRel).toBeNull();
  });
});
