/**
 * `table-pattern.ts` 的行为守卫（用户口径 2026-10-13）。
 *
 * 本轮表格正则的**三条硬约束**，每条都单独钉：
 *   ① 结果**覆盖原列**（不是派生列）；
 *   ② 作用域 = 没勾选改整列 / 勾选了只改勾选行（与 `BatchColumnBar` 同语义）；
 *   ③ 数值列**不给**正则入口；没命中**保持原值**（不清空）。
 */

import { describe, expect, it } from "vitest";

import {
  applyPatternToCell,
  applyPatternToColumn,
  applyValuesToColumn,
  canApplyPattern,
  compilePattern,
  patternOutcomeText,
  previewPattern,
  restoreRows,
  snapshotKey,
  snapshotTable,
} from "./table-pattern";

/** 真包 B 的 `sku名称` 列形态。 */
const SKU_NAMES = [
  "颜色分类:【中古风】实木三斗柜",
  "颜色分类:【中古风】伸缩桌+三斗柜梳妆台",
  null,
  "颜色分类:【中古风】实木六斗柜",
];

/** 真包 A 的 `SKU信息` 列形态（`;` 分隔的三段）。 */
const SKU_INFO = [
  "木;中古色-实木三斗柜;上门安装",
  "木;中古色-【伸缩桌+三斗柜】梳妆台;上门安装",
];

describe("正则取值：捕获组优先，无组取整串", () => {
  it("有捕获组 ⇒ 取组 1", () => {
    expect(applyPatternToCell("颜色分类:实木三斗柜", /^([^:]+):(.+)$/)).toBe("颜色分类");
  });

  it("无捕获组 ⇒ 取整个匹配串", () => {
    expect(applyPatternToCell("abc-123", /\d+/)).toBe("123");
  });

  it("分段提取（包 A 的 `;` 形态）", () => {
    //: 「对原数据中的列取值」的典型用法：取中间那段规格值。
    expect(applyPatternToCell("木;中古色-实木三斗柜;上门安装", /^[^;]+;([^;]+);/)).toBe(
      "中古色-实木三斗柜",
    );
  });

  it("⚠️ 没命中 ⇒ null（调用方**保持原值**，不清空）", () => {
    expect(applyPatternToCell("没有冒号", /^([^:]+):(.+)$/)).toBeNull();
    expect(applyPatternToCell(null, /(.+)/)).toBeNull();
    expect(applyPatternToCell("", /(.+)/)).toBeNull();
  });

  it("⚠️ 同一正则连跑多行**不跳行**（不能带 `g` 标志）", () => {
    //: 经典陷阱：`/x/g` 有 `lastIndex` 状态，逐行 `exec` 会跳过奇数行。
    const re = /^([^:]+):/;
    const out = ["a:1", "b:2", "c:3", "d:4"].map((s) => applyPatternToCell(s, re));
    expect(out).toEqual(["a", "b", "c", "d"]);
  });
});

describe("正则合法性（不抛错，回原因）", () => {
  it("空 / 非法都给原因，且 `re` 为 null", () => {
    expect(compilePattern("").re).toBeNull();
    expect(compilePattern("").error).toBe("正则不能为空");
    const bad = compilePattern("([");
    expect(bad.re).toBeNull();
    expect(bad.error).toContain("正则语法错误");
  });
});

describe("预演：给用户看「会变成什么」再应用", () => {
  it("统计命中 / 未命中，并带上前后值", () => {
    const out = previewPattern(SKU_NAMES, "^([^:]+):(.+)$");
    expect(out.total).toBe(4);
    expect(out.changed.length).toBe(3); //: 3 行有冒号
    expect(out.missed).toBe(1); //: `null` 那行
    expect(out.changed[0].before).toBe("颜色分类:【中古风】实木三斗柜");
    expect(out.changed[0].after).toBe("颜色分类");
  });

  it("⚠️ 作用域 = 勾选行时，只统计勾选的那些", () => {
    const out = previewPattern(SKU_NAMES, "^([^:]+):(.+)$", [0, 3]);
    expect(out.total).toBe(2);
    expect(out.changed.map((c) => c.rowIndex)).toEqual([0, 3]);
  });

  it("非法正则 ⇒ error 有值、changed 为空（不半路改一半）", () => {
    const out = previewPattern(SKU_NAMES, "([");
    expect(out.error).toContain("正则语法错误");
    expect(out.changed).toEqual([]);
  });

  it("文案一句话给结论", () => {
    expect(patternOutcomeText(previewPattern(SKU_NAMES, "^([^:]+):(.+)$"))).toContain(
      "命中 3 行",
    );
  });
});

describe("应用：覆盖原列（不是派生列）", () => {
  const table = { sheet_name: "全部", rows: SKU_NAMES.map((v) => [v, "200"]) };

  it("覆盖原列的值，列数不变（**没有新增列**）", () => {
    const next = applyPatternToColumn(table, 0, "^([^:]+):");
    expect(next.rows[0][0]).toBe("颜色分类");
    expect(next.rows[0]).toHaveLength(2); //: 仍是 2 列 ⇒ 不是派生列
    expect(next.rows[0][1]).toBe("200"); //: 别的列没动
  });

  it("⚠️ 没命中的行**保持原值**（不清空）", () => {
    //: `null` 那行本来就空；再给一条没冒号的看看。
    const t = { sheet_name: "全部", rows: [["没有冒号", "1"]] };
    const next = applyPatternToColumn(t, 0, "^([^:]+):");
    expect(next.rows[0][0]).toBe("没有冒号");
  });

  it("⚠️ **默认覆盖全部行**（没给作用域）", () => {
    const next = applyPatternToColumn(table, 0, "^([^:]+):");
    expect(next.rows.map((r) => r[0])).toEqual([
      "颜色分类",
      "颜色分类",
      null, //: 原本就是 null ⇒ 保持
      "颜色分类",
    ]);
  });

  it("⚠️ **给了勾选行 ⇒ 只覆盖勾选行**，其余原样", () => {
    const next = applyPatternToColumn(table, 0, "^([^:]+):", [1]);
    expect(next.rows[0][0]).toBe("颜色分类:【中古风】实木三斗柜"); //: 未勾选 ⇒ 不动
    expect(next.rows[1][0]).toBe("颜色分类"); //: 勾选 ⇒ 改了
    expect(next.rows[3][0]).toBe("颜色分类:【中古风】实木六斗柜");
  });

  it("原表**不被就地修改**（返回新表）", () => {
    const before = JSON.stringify(table.rows);
    applyPatternToColumn(table, 0, "^([^:]+):");
    expect(JSON.stringify(table.rows)).toBe(before);
  });

  it("非法正则 / 列下标 -1 ⇒ 原表返回（不炸、不改）", () => {
    expect(applyPatternToColumn(table, 0, "([").rows[0][0]).toBe(
      "颜色分类:【中古风】实木三斗柜",
    );
    expect(applyPatternToColumn(table, -1, "^(.+)$").rows[0][0]).toBe(
      "颜色分类:【中古风】实木三斗柜",
    );
  });
});

describe("列类型：数值列不给正则入口", () => {
  it("只有 text 列可以套正则", () => {
    expect(canApplyPattern("text")).toBe(true);
    expect(canApplyPattern("numeric")).toBe(false);
    expect(canApplyPattern("empty")).toBe(false);
    //: 后端没下发 `column_kinds` 时（老响应）⇒ 不给入口，宁可不作为。
    expect(canApplyPattern(undefined)).toBe(false);
  });
});

describe("撤销：覆盖前按列存快照，只还原当前列", () => {
  /** ⚠️ 每个用例**各建一份**表 —— 共享夹具会被前面的用例就地改掉
   *  （`快照是深拷贝` 那条刻意改了 `table.rows[0][0]`），后续断言会跟着错。 */
  const freshTable = () => ({ sheet_name: "全部", rows: SKU_NAMES.map((v) => [v, "200"]) });

  it("还原回覆盖前的值", () => {
    const table = freshTable();
    const snap = snapshotTable(table, 0, 0);
    const changed = applyPatternToColumn(table, 0, "^([^:]+):");
    expect(changed.rows[0][0]).toBe("颜色分类");
    const back = restoreRows(changed, snap);
    expect(back.rows[0][0]).toBe("颜色分类:【中古风】实木三斗柜");
    expect(back.rows).toEqual(table.rows);
  });

  it("⚠️ 只有当前列回原值，别列改过的**留着**", () => {
    //: 本轮核心断言（用户口径 2026-10-15：「恢复数据应只恢复当前列而不是全表」）。
    //: 旧实现存整表快照 ⇒ 还原 B 列会把 A 列的成果一起撤掉 ⇒ 此条必红。
    const table = freshTable();
    //: ① 改 A 列（0）；② 改 B 列（1）—— 两列各存一份快照。
    const snapA = snapshotTable(table, 0, 0);
    const afterA = applyPatternToColumn(table, 0, "^([^:]+):");
    expect(afterA.rows[0][0]).toBe("颜色分类");

    const snapB = snapshotTable(afterA, 0, 1);
    const afterB = applyValuesToColumn(afterA, 1, [{ rowIndex: 0, after: "双十一价" }]);
    expect(afterB.rows[0][1]).toBe("双十一价");

    const back = restoreRows(afterB, snapB);
    expect(back.rows[0][1]).toBe("200");
    //: ⚠️ A 列**仍是改后**的值。
    expect(back.rows[0][0]).toBe("颜色分类");
    //: A 列的快照也还在（没被 B 列的还原删掉 ⇒ 仍可再撤 A）。
    expect(restoreRows(back, snapA).rows[0][0]).toBe("颜色分类:【中古风】实木三斗柜");
  });

  it("⚠️ 快照是**深拷贝**（之后改原表不影响快照）", () => {
    const table = freshTable();
    const snap = snapshotTable(table, 0, 0);
    table.rows[0][0] = "被改了";
    expect(snap.columns[0][0]).toBe("颜色分类:【中古风】实木三斗柜");
  });

  it("⚠️ 快照**只收了这一列**（列 1 不在里面）", () => {
    const snap = snapshotTable(freshTable(), 0, 0);
    expect(Object.keys(snap.columns)).toEqual(["0"]);
  });

  it("没有快照 ⇒ 原表返回（不炸）", () => {
    expect(restoreRows(freshTable(), undefined).rows[0][0]).toBe(
      "颜色分类:【中古风】实木三斗柜",
    );
  });

  it("快照键含**表下标 + 列下标**（同名 sheet / 同表不同列都不撞键）", () => {
    const table = freshTable();
    expect(snapshotTable(table, 0, 0).key).not.toBe(snapshotTable(table, 1, 0).key);
    //: ⚠️ 同表不同列必须不同键 —— 否则「改 A 列、还原 B 列」会共用一份快照。
    expect(snapshotTable(table, 0, 0).key).not.toBe(snapshotTable(table, 0, 1).key);
    //: 调用方查表用的复合键：`snapshotKey(表, 列)`，与 `snapshotTable` 的 key 后缀一致。
    expect(snapshotTable(table, 0, 1).key.endsWith(snapshotKey(0, 1))).toBe(true);
    //: 键是**同一个形态的字符串**（不是数组）—— 数组按引用比较会让快照去重失效。
    expect(typeof snapshotKey(0, 1)).toBe("string");
  });
});

describe("包 A 的 SKU信息 列（真实形态）", () => {
  it("取中间规格值", () => {
    //: ⚠️ `previewPattern` 收的是**正则字符串**（弹窗里用户输入的就是字符串），
    //: 传 `RegExp` 字面量会被 `compilePattern` 当空处理 ⇒ 一条都不命中。
    const out = previewPattern(SKU_INFO, "^[^;]+;([^;]+);");
    expect(out.changed.map((c) => c.after)).toEqual([
      "中古色-实木三斗柜",
      "中古色-【伸缩桌+三斗柜】梳妆台",
    ]);
  });
});
