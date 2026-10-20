/** **真机端到端**：用真实数据包（用户 2026-10-15 实测的那个）跑完整链路，
 *  断言第二步「预检通过SKU导入」按钮的 `disabled` 判据为 **false**。
 *
 * 这条用例走的就是线上那条链路，一步不少：
 *   xlsx 读出的列/行 → passedSkuRows(tables[0], blocked, picked, localImages, M)
 *     → sourceSpecNames(, M) → `specImportSources`
 *     → `publish-config-step2.tsx:2902` 的 `disabled = length === 0`
 *
 * 前置：真实数据
 *   `backend/app/storage/package_precheck/u1/<id>/商品数据.xlsx`
 *   sheet=全部  columns=[sku名称, 库存]  rows=[A款, 3]
 */
import { describe, expect, it } from "vitest";
import {
  passedSkuRows,
  skuNameColumnIndex,
  SINGLE_SPEC_NAME,
  buildSkuFillPlan,
  priceSourceOptions,
  type PriceMappingRow,
} from "./precheck-publish";
import type { PrecheckTable } from "./precheck-publish";

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


/** 真实包（探针从 xlsx 原值读出）。 */
const COLUMNS = ["sku名称", "库存"];
const ROWS = [["A款", "3"]];

describe("真机端到端：真实包的按钮 disabled 判据", () => {
  it("列定位：**按映射**认出 `sku名称` 列（下标 0）", () => {
    //: ⚠️ **口径已变**（用户口径 2026-10-15：「所有点均走映射，移除写死的逻辑」）：
    //: 旧断言 `skuNameColumnIndex(COLUMNS) === 0` 靠**列名叫不叫 sku名称** 来认列；
    //: 现在必须由映射指出是哪一列（没配 ⇒ -1，不猜）。
    expect(skuNameColumnIndex(COLUMNS)).toBe(-1); //: 没配映射 ⇒ 不认
    expect(skuNameColumnIndex(COLUMNS, M)).toBe(0); //: 配了 ⇒ 认映射那一列
  });

  it("★ 按钮 disabled = false（修复前恒 true ⇒ 用户点不动 + 看到「没有可导入的 SKU」）", () => {
    const table = { sheet_name: "全部", columns: COLUMNS, rows: ROWS } as unknown as PrecheckTable;
    //: 用户的勾选态：第一步表格**默认全选** ⇒ 向导里 `picked` 可能是 []（空集 = 全选）
    //: 或显式 [0]（用户勾过）。两种都试，判据必须都不为 disabled。
    for (const picked of [[], [0]]) {
      const rows = passedSkuRows(table, [], picked, [], M);
      //: ⚠️ 2026-10-20：映射左侧 = **预检通过列名** ⇒ 候选不再从规格名派生。
      const specImportSources = priceSourceOptions(table.columns);
      // eslint-disable-next-line no-console
      console.log(
        `[真机] picked=${JSON.stringify(picked)} ⇒ 行数=${rows.length} ` +
          `specImportSources=${JSON.stringify(specImportSources)} ` +
          `disabled=${specImportSources.length === 0}`,
      );
      expect(specImportSources.length).toBeGreaterThan(0);
      //: 候选 = 表格的所有列（「sku名称」「库存」）。
      expect(specImportSources).toEqual(["sku名称", "库存"]);
      //: 这一条就是 `publish-config-step2.tsx:2902` 的表达式，逐字照抄。
      expect(specImportSources.length === 0).toBe(false);
    }
  });

  it("导入弹窗能真的建出规格（映射到平台维度后 plan.rows 非空）", () => {
    const table = { sheet_name: "全部", columns: COLUMNS, rows: ROWS } as unknown as PrecheckTable;
    const rows = passedSkuRows(table, [], [], [], M);
    const dims = [
      { name: "颜色分类", id: "d1", customizable: true, options: ["A款"] },
    ];
    //: ⚠️ 2026-10-20：映射键 = **列名**（用户选的那一列）。
    const plan = buildSkuFillPlan(rows, dims, { sku名称: "颜色分类" });
    // eslint-disable-next-line no-console
    console.log("[真机] plan.dims =", JSON.stringify(plan.dims.map((d) => d.dimName)),
      " plan.rows =", plan.rows.length, " skipped =", JSON.stringify(plan.skipped));
    expect(plan.dims.length).toBe(1);
    expect(plan.rows.length).toBe(1);
    expect(plan.rows[0].spec).toContain("A款");
  });
});
