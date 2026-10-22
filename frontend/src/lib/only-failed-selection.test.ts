/** 复现「只勾了 24 行却执行 456 行」的完整状态链路（用户口径 2026-10-21）。
 *
 *  这里把页面里那几行判据**原样搬出来**跑真实数据（456 行 / 24 行不通过），
 *  逐层验证 `picked` → `scopeRows` → `pickedRows` → 执行 `scope` 的规模。
 *
 *  ⚠️ 为什么要在 lib 里复刻而不是只读源码做字符串断言：
 *  这个 bug 已经「修」过 4 次，每次都以为改对了 —— 纯文本断言只能证明
 *  「代码里有这句话」，证明不了「算出来是 24 行」。这里跑真值。
 */
import { describe, expect, it } from "vitest";

const TOTAL = 456;
const FAILED = 24; // 前 24 行规则失败（与真机截图一致）

/** 页面上「是否不通过」的判据（简化为：前 24 行失败）。 */
const isBadRow = (i: number) => i < FAILED;

/** **表头全选键**的落值 —— 与页面表头 Checkbox 的 onChange 逐行对应。
 *
 *  ⚠️ 用户 2026-10-21 第 6 次报障：「我点的是**全选按钮**，点了一次」。
 *  老实现首次点击的基准是「整表全 true」⇒ 点「勾上」= 456 行全勾。
 */
function clickSelectAll(
  picked: Record<number, boolean[]>,
  tIndex: number,
  on: boolean,
  viewingFailedOnly: boolean,
  scopeSelectable: number[],
  scopeRows: number[],
): Record<number, boolean[]> {
  const seen = picked[tIndex];
  const cur = seen ?? new Array(TOTAL).fill(!viewingFailedOnly);
  const next = cur.slice();
  for (const i of scopeSelectable) next[i] = on;
  if (!on) for (const i of scopeRows) next[i] = false;
  return { ...picked, [tIndex]: next };
}

/** 「只看不通过」开关的落值 —— 与页面 onClick 里的实现逐行对应。 */
function toggleOnlyFailed(
  picked: Record<number, boolean[]>,
  tIndex: number,
  turningOn: boolean,
  scopeRows: number[],
): Record<number, boolean[]> {
  if (turningOn) {
    const cur = picked[tIndex] ?? new Array(TOTAL).fill(true);
    const next = new Array(TOTAL).fill(false);
    for (const i of scopeRows) next[i] = cur[i];
    return { ...picked, [tIndex]: next };
  }
  return { ...picked, [tIndex]: new Array(TOTAL).fill(true) };
}

/** 页面里 `pickedRows` 的算法（2026-10-21 最终版）。
 *
 *  ⚠️ 关键：**筛选开着时只取可见行**。用户从没点过任何勾选框
 *  （筛选下那 24 行默认就是勾着的），若不加这条收窄，
 *  `picked` 未定义 ⇒ 补成整表 456 行 ⇒ 规则改 456 行。
 */
function pickedRowsOf(
  picked: Record<number, boolean[]>,
  tIndex: number,
  viewingFailedOnly = false,
  scopeRows: number[] = [],
): number[] {
  return (picked[tIndex] ?? new Array(TOTAL).fill(true))
    .map((on, i) => (on ? i : -1))
    .filter((i) => i >= 0)
    .filter((i) => !viewingFailedOnly || scopeRows.includes(i));
}

describe("只看不通过 → 勾选 → 规则作用域", () => {
  it("① 打开筛选后，已选显示 24 行，且执行作用域也是 24 行", () => {
    const allRows = Array.from({ length: TOTAL }, (_, i) => i);
    const scopeRows = allRows.filter(isBadRow); // 只看不通过
    expect(scopeRows.length).toBe(FAILED);

    //: 用户点「只看不通过」（此时 picked 里还没有任何记录）
    let picked: Record<number, boolean[]> = {};
    picked = toggleOnlyFailed(picked, 0, true, scopeRows);

    const pickedRows = pickedRowsOf(picked, 0);
    //: ⚠️ 这是本次 bug 的核心断言：屏幕上 24 个勾，状态里就必须只有 24 行。
    expect(pickedRows.length).toBe(FAILED);
    expect(pickedRows).toEqual(scopeRows);

    //: 执行时 `scope = new Set(pickedRows)` ⇒ 24 行
    expect(new Set(pickedRows).size).toBe(FAILED);
  });

  it("② 关掉筛选 ⇒ 回到整表全选（老行为不变）", () => {
    const allRows = Array.from({ length: TOTAL }, (_, i) => i);
    const scopeRows = allRows.filter(isBadRow);
    let picked: Record<number, boolean[]> = {};
    picked = toggleOnlyFailed(picked, 0, true, scopeRows);
    picked = toggleOnlyFailed(picked, 0, false, allRows);
    expect(pickedRowsOf(picked, 0).length).toBe(TOTAL);
  });

  it("⑥ 筛选开着 + **点一次全选** ⇒ 作用域 = 24 行（真机操作序列）", () => {
    //: ⚠️ 这是用户真实操作：开「只看不通过」→ 点一下表头全选 → 执行规则。
    //: 老实现首次点击基准是「整表全 true」⇒ 点勾上变成 456 行全勾。
    const allRows = Array.from({ length: TOTAL }, (_, i) => i);
    const scopeRows = allRows.filter(isBadRow);
    const picked = clickSelectAll({}, 0, true, true, scopeRows, scopeRows);
    //: ⚠️ 必须断言**状态层本身**（不经 `pickedRowsOf` 的可见行收窄）——
    //: 否则收窄会把「状态里 456 行全勾」掩盖掉，变异测试实测 ESCAPED。
    expect(picked[0].filter(Boolean).length).toBe(FAILED);
    //: 并且未经收窄的原始作用域也必须是 24（不靠下游兜底）。
    expect(pickedRowsOf(picked, 0, false).length).toBe(FAILED);
  });

  it("⑦ 筛选开着 + 点一次全选再点一次（取消）⇒ 0 行", () => {
    const allRows = Array.from({ length: TOTAL }, (_, i) => i);
    const scopeRows = allRows.filter(isBadRow);
    let picked = clickSelectAll({}, 0, true, true, scopeRows, scopeRows);
    picked = clickSelectAll(picked, 0, false, true, scopeRows, scopeRows);
    expect(pickedRowsOf(picked, 0, true, scopeRows).length).toBe(0);
  });

  it("⑤ 筛选开着 + 从没点过勾选框 ⇒ 作用域 = 可见的 24 行（真机场景）", () => {
    //: ⚠️ 这就是用户真实操作：开「只看不通过」，屏幕上 24 行默认勾着，
    //: 他一个勾选框都没点，直接执行规则。`picked` 仍是 `{}`（未定义）。
    const allRows = Array.from({ length: TOTAL }, (_, i) => i);
    const scopeRows = allRows.filter(isBadRow);
    const rows = pickedRowsOf({}, 0, true, scopeRows);
    expect(rows.length).toBe(FAILED);
    expect(rows).toEqual(scopeRows);
  });

  it("③ 没开过筛选、也没点过任何勾 ⇒ 默认整表全选（老行为不变）", () => {
    expect(pickedRowsOf({}, 0).length).toBe(TOTAL);
  });

  it("④ 在筛选里取消一行 ⇒ 只少那一行，其余仍是 23", () => {
    const allRows = Array.from({ length: TOTAL }, (_, i) => i);
    const scopeRows = allRows.filter(isBadRow);
    let picked: Record<number, boolean[]> = {};
    picked = toggleOnlyFailed(picked, 0, true, scopeRows);
    const arr = picked[0].slice();
    arr[5] = false;
    picked = { 0: arr };
    const rows = pickedRowsOf(picked, 0);
    expect(rows.length).toBe(FAILED - 1);
    expect(rows).not.toContain(5);
  });
});
