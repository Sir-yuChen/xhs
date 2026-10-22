/** 规则执行的「作用域 ↔ 结果」下标映射（用户口径 2026-10-21 第 5 次报障）。
 *
 *  现象：「只勾选了 24 行，但是**覆盖执行了全部**」。
 *
 *  ⚠️ 真根因：`values` 与「结果回写用的行号表」**顺序不同**。
 *  · 老写法 `rows.map(...).filter((_, i) => scope.has(i))` 产出的 `values`
 *    是**表内顺序**（0,1,2,…）；
 *  · 而回写用的 `targetRows = [...scope]` 是 **Set 的插入顺序**
 *    （= 用户点勾选的先后）。
 *  两者一旦不同序，`res.values[i]` 就会被写进 `targetRows[i]` 这条**错的行**上 ——
 *  用户看到的就是「我没勾的行也被改了」。
 *
 *  这里用 456 行真数据把「勾选乱序」这一情形跑出来，断言：
 *  ① 只有勾选的那 24 行被改；② 每行改成的值就是该行的结果（不错位）。
 */
import { describe, expect, it } from "vitest";

const TOTAL = 456;

/** 模拟一行数据：每行「券后价格」列的原值都不同，便于发现错位。 */
const makeRows = () =>
  Array.from({ length: TOTAL }, (_, i) => [`orig-${i}`, `sku-${i}`]);

/** 后端行为：把传入的 values 逐个算成固定文本。 */
const fakeBackend = (values: (string | null)[], text: string) =>
  values.map(() => text);

/** 执行 + 回写（与页面 `runPickedRule` → `applyValuesToColumn` 同构）。 */
function runRule(
  rows: string[][],
  column: number,
  picked: number[],
  text: string,
): string[][] {
  //: ⚠️ 关键：values 与 targetRows 必须**同一份有序数组**逐个取值。
  const targetRows = [...new Set(picked)].sort((a, b) => a - b);
  const values = targetRows.map((i) => rows[i]?.[column] ?? null);
  const results = fakeBackend(values, text);

  const out = rows.map((r) => r.slice());
  results.forEach((after, i) => {
    const rowIndex = targetRows[i];
    if (rowIndex === undefined) return;
    out[rowIndex][column] = after;
  });
  return out;
}

describe("规则作用域回写：不许错位、不许改到没勾的行", () => {
  it("① 勾 24 行（乱序）⇒ 只有这 24 行变成 20", () => {
    const rows = makeRows();
    //: 故意打乱：模拟用户先勾第 300 行、再勾第 7 行……
    //: ⚠️ 必须**真的乱序**（降序/跳跃混排）：早先这组数恰好接近升序，
    //: 于是「按插入顺序取值」的错误实现算出来一样，变异 ESCAPED（实测）。
    const picked = [455, 401, 400, 366, 311, 300, 250, 240, 199, 150, 128, 123,
                    99, 88, 77, 55, 44, 33, 22, 12, 7, 3, 1, 0];
    expect(new Set(picked).size).toBe(24);

    const out = runRule(rows, 0, picked, "20");

    const changed = out
      .map((r, i) => (r[0] !== rows[i][0] ? i : -1))
      .filter((i) => i >= 0);
    //: ⚠️ 核心断言：改动的行**恰好**是勾选的那 24 行，一个不多一个不少。
    expect(changed.sort((a, b) => a - b)).toEqual(
      [...picked].sort((a, b) => a - b),
    );
    for (const i of picked) expect(out[i][0]).toBe("20");
    //: 没勾的行**一个字符都不能变**（这是用户最在意的）。
    for (let i = 0; i < TOTAL; i += 1) {
      if (!picked.includes(i)) expect(out[i][0]).toBe(rows[i][0]);
    }
  });

  it("② values 必须按**表内顺序**取，不能按勾选的插入顺序", () => {
    //: ⚠️ 这条才是咬得住「错位」的判据：旧实现 `[...scope]` 是 Set 插入顺序，
    //: 而 `res.values[i]` 是按 values 的顺序回来的 ⇒ 两者不同序就会写错行。
    //: 前面那条用例里 targetRows 已经排过序，所以两种写法算出来一样、咬不住
    //: （实测变异 ESCAPED）。这里直接对比**两种顺序**的结果，必须不同。
    const rows = makeRows();
    const picked = [400, 3, 250, 9];
    const byTableOrder = [...new Set(picked)].sort((a, b) => a - b); // 正确
    const byPickOrder = [...new Set(picked)]; // 错误（插入顺序）

    const correct = byTableOrder.map((i) => rows[i]?.[0] ?? null);
    const wrong = byPickOrder.map((i) => rows[i]?.[0] ?? null);
    //: 输入不同序 ⇒ 若代码按错的那个序取，结果数组顺序会变 → 回写必然错位。
    expect(correct).not.toEqual(wrong);
    expect(correct).toEqual(["orig-3", "orig-9", "orig-250", "orig-400"]);
    expect(wrong).toEqual(["orig-400", "orig-3", "orig-250", "orig-9"]);
  });

  it("③ values 与 targetRows 同序（错位会直接反映成改错行）", () => {
    const rows = makeRows();
    const picked = [400, 3, 250, 9];
    const targetRows = [...new Set(picked)].sort((a, b) => a - b);
    const values = targetRows.map((i) => rows[i]?.[0] ?? null);
    //: 第 i 个 value 必须来自 targetRows[i] 这一行（不是 picked[i]）。
    expect(values).toEqual(targetRows.map((i) => `orig-${i}`));
    expect(targetRows).toEqual([3, 9, 250, 400]);
  });
});
