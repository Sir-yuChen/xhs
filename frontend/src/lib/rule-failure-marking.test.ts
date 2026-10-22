/** 规则处理异常 ⇒ 标红 + 取消勾选（用户口径 2026-10-21）。
 *
 *  > 「规则处理异常，无法处理的行数据**红色标记并取消勾选**」
 *  > 「无法计算的列应红框展示，**适用于所有规则**，
 *  >   规则无法处理的数据，该行红框展示取消勾选，**需用户手动处理**」
 *
 *  这里钉的是**判据本身**（`numericCellIssue` 与「规则回报失败行」）——
 *  页面接线由 `tests/backend/test_page_optimizations.py` 的源码守卫盯。
 */
import { describe, expect, it } from "vitest";

import { numericCellIssue } from "./precheck-publish";
import { applyPatternToColumn } from "./table-pattern";

describe("numericCellIssue：数值能否参与计算", () => {
  it("空 / 全空白 ≠ 可计算", () => {
    expect(numericCellIssue("")).toBe("为空");
    expect(numericCellIssue("   ")).toBe("为空");
    expect(numericCellIssue(null)).toBe("为空");
    expect(numericCellIssue(undefined)).toBe("为空");
  });

  it("非数值 ≠ 可计算（含真机见过的 `-`）", () => {
    //: 真实数据包 `券后价格` 列有 24 行是 `-`（不是 0、不是空）。
    expect(numericCellIssue("-")).toBe("不是数字");
    expect(numericCellIssue("abc")).toBe("不是数字");
    //: ⚠️ 带中文单位也认不出 —— 后端 `_as_number_or_none` 同口径。
    expect(numericCellIssue("20元")).toBe("不是数字");
  });

  it("**≤0 不可计算**（用户口径的核心新增）", () => {
    expect(numericCellIssue("0")).toContain("小于等于 0");
    expect(numericCellIssue("0.00")).toContain("小于等于 0");
    expect(numericCellIssue("-5")).toContain("小于等于 0");
  });

  it("正数可计算（含 ￥ / 千分位写法）", () => {
    expect(numericCellIssue("1")).toBe("");
    expect(numericCellIssue("20.00")).toBe("");
    expect(numericCellIssue("￥1,200")).toBe("");
    expect(numericCellIssue("0.01")).toBe("");
  });
});

describe("applyPatternToColumn：回报处理异常的行", () => {
  const table = {
    columns: ["名称", "价格"],
    column_kinds: ["text", "numeric"],
    rows: [
      ["SKU01-甲", "10"],
      ["无编号行", "20"], //: 正则提不出 ⇒ 原样保留，但必须报出来
      ["SKU03-丙", "30"],
    ],
  } as unknown as { rows: (string | null)[][]; columns: string[]; column_kinds: string[] };

  it("命中行被覆盖，未命中行**记录为失败**（不是静默跳过）", () => {
    const out = applyPatternToColumn(table, 0, "SKU(\\d+)-(.+)") as unknown as {
      rows: (string | null)[][];
      ruleFailedRows?: number[];
    };
    //: 命中行拿到捕获组内容（`$1`/`$2` 语义由 `applyPatternToCell` 决定，这里只判「变了」）。
    expect(out.rows[0][0]).not.toBe("SKU01-甲");
    expect(out.rows[2][0]).not.toBe("SKU03-丙");
    //: ⚠️ 未命中行保持原值（不写空），但**必须进失败名单**。
    expect(out.rows[1][0]).toBe("无编号行");
    expect(out.ruleFailedRows).toEqual([1]);
  });

  it("作用域外的行不进失败名单（规则没碰它）", () => {
    const out = applyPatternToColumn(table, 0, "SKU(\\d+)-(.+)", [0]) as unknown as {
      ruleFailedRows?: number[];
    };
    expect(out.ruleFailedRows).toEqual([]);
  });

  it("正则语法错误 ⇒ 原表返回（不产生失败行，因为没有执行）", () => {
    const out = applyPatternToColumn(table, 0, "([unclosed") as unknown as {
      ruleFailedRows?: number[];
      rows: (string | null)[][];
    };
    expect(out.ruleFailedRows).toBeUndefined();
    expect(out.rows[0][0]).toBe("SKU01-甲");
  });
});
