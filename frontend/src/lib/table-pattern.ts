/**
 * SKU 表格的**正则取值**（覆盖原列）—— 用户口径 2026-10-13。
 *
 * ## 用户口径（本轮）
 *
 * > 「表格列通过正则对原数据中的列取值操作后**覆盖原始列**」
 * > 「默认**覆盖全部**，如果存勾选行则**只覆盖勾选行**」
 * > 「数值列不支持只能读取原始值」
 *
 * ## 两条硬约束（为什么这么写）
 *
 * ① **不做派生列**：正则结果**写回原列**（不是新增一列）。
 *    我上一轮建议的「原列只读 + 派生列」已被用户明确否掉。
 * ② **必须能撤销**：覆盖是破坏性的（原值没了）。所以调用方要**先存快照**
 *    （`snapshotRows`），撤销时整表还原 —— 否则用户正则写错一位，
 *    几十行原始数据就永久丢了，只能重新读一遍数据包。
 *
 * ⚠️ 作用域与 `BatchColumnBar` **完全一致**（同一个 `onlyRows` 语义）：
 *    没勾选 = 整列；勾选了 N 行 = 只改这 N 行。
 *    两处若不一致，会出现「批量改是整列、正则改是选中行」这种自相矛盾。
 */

/** 列类型（后端判、随表下发；前端**不猜**）。 */
export type ColumnKind = "numeric" | "text" | "empty";

/** 数值列**不给正则入口**（用户口径：「数值列不支持只能读取原始值」）。 */
export function canApplyPattern(kind: ColumnKind | undefined): boolean {
  return kind === "text";
}

/** **这一列要不要显示表头设置图标**（2026-10-14 用户口径再修订）。

    用户原话：
    > 「**所有列均表头有设置图标**，**不再只局限于文本列**」

    ⚠️ 与 `canApplyPattern` 的区别（两者**不再是同一个判据**）：
      · `canApplyPattern` 管的是**正则取值**那个区块 —— **仍然只给文本列**
        （数值列给正则没有意义，且会让用户误以为能对数字做正则）；
      · 本函数管的是**表头要不要出现齿轮** —— **所有列都要**
        （因为「数据包规则」里还有 `fixed_text` / `math_scale` 这类
         **本来就是给数值列的**操作，用户口径：「可完全取代当前表格顶部
         对列的批量操作功能」）。

    ⚠️ **空列（`empty`）也要给**：那是「这一列整列没读到值」，用户可能正想用
    规则去看/处理它 —— 不给图标他会以为这列坏了。 */
export function hasColumnGear(kind: ColumnKind | undefined): boolean {
  return Boolean(kind);
}

/** 正则预览的一行结果（弹窗里给用户看「会变成什么」）。 */
export type PatternPreviewRow = {
  /** 行下标（原表里的）。 */
  rowIndex: number;
  /** 覆盖前的原值。 */
  before: string | null;
  /** 覆盖后的值；`null` = 没命中（**保持原值**）。 */
  after: string | null;
};

/** 正则执行结果（逐行）。 */
export type PatternOutcome = {
  /** 命中并产生了新值的行。 */
  changed: PatternPreviewRow[];
  /** 在作用域内、但**没命中**的行数（保持原值 —— 不写空串）。 */
  missed: number;
  /** 作用域内的总行数。 */
  total: number;
  /** 正则非法时的原因（空 = 合法）。 */
  error: string;
};

/**
 * 编译用户正则（**不抛错**，非法时回 `null` + 原因）。
 *
 * ⚠️ 用 `g` 标志会在 `exec` 时带 `lastIndex` 状态，逐行跑会**跳行**
 * （经典陷阱：同一正则连跑 26 行，奇数行被跳过）。所以这里统一不带 `g`。
 */
export function compilePattern(pattern: string): { re: RegExp | null; error: string } {
  const text = String(pattern ?? "").trim();
  if (!text) return { re: null, error: "正则不能为空" };
  try {
    return { re: new RegExp(text), error: "" };
  } catch (error) {
    return {
      re: null,
      error: `正则语法错误：${error instanceof Error ? error.message : String(error)}`,
    };
  }
}

/**
 * **提取值**：正则对单个单元格取值。
 *
 * 取值优先级（用户口径：正则「取值」，不是「替换」）：
 *   1. 有捕获组 ⇒ 用**第 1 个捕获组**（用户写 `^(.+?);` 就是想要那一段）；
 *   2. 无捕获组 ⇒ 用整个匹配串（`match[0]`）。
 *
 * 没命中 ⇒ `null`（调用方**保持原值**，不写空串 —— 「缺值留空不造假」
 * 在这里的反面：**改不动就别改**，静默清空比不改更糟）。
 */
export function applyPatternToCell(
  raw: string | null,
  re: RegExp,
): string | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const text = String(raw);
  //: ⚠️ 不用 `g` 标志，`exec` 无状态 ⇒ 可安全复用同一个 `re` 跑整列。
  const m = text.match(re);
  if (!m) return null;
  const picked = m.length > 1 ? m[1] : m[0];
  if (picked === undefined || picked === null) return null;
  return String(picked);
}

/**
 * **预演**正则对一整列的效果（**不落库、不改原值**）。
 *
 * @param values   该列的全部值（与表格行等长）
 * @param pattern  用户正则
 * @param onlyRows 作用域行下标（`null` / 空 = 整列）
 */
export function previewPattern(
  values: readonly (string | null)[],
  pattern: string,
  onlyRows?: readonly number[] | null,
): PatternOutcome {
  const { re, error } = compilePattern(pattern);
  if (!re) return { changed: [], missed: 0, total: 0, error };

  const scope =
    onlyRows && onlyRows.length > 0 ? new Set(onlyRows) : null;
  const changed: PatternPreviewRow[] = [];
  let missed = 0;
  let total = 0;

  values.forEach((value, rowIndex) => {
    if (scope && !scope.has(rowIndex)) return;
    total += 1;
    const after = applyPatternToCell(value, re);
    if (after === null) {
      missed += 1;
      return;
    }
    changed.push({ rowIndex, before: value, after });
  });

  return { changed, missed, total, error: "" };
}

/**
 * **应用**正则到一张表的一列（返回**新表**，原表不动）。
 *
 * ⚠️ 作用域与 `previewPattern` **必须一致**（都走 `onlyRows`）——
 * 预览显示「命中 20/26」而实际只改了 5 行，是本轮最容易出的静默不一致。
 */
/** **直接按「逐行结果」写值**（数据包规则执行用，用户口径 2026-10-14）。
 *
 *  ⚠️ 与 `applyPatternToColumn` 的分工：
 *    · 那条 = 「给定正则 → 前端逐行匹配 → 覆盖」；
 *    · 这条 = 「后端已算好每行结果 → 照写」（提取语义**单一实现**在后端，DEF-044）。
 *
 *  ⚠️ `after === null` ⇒ **留空**（该行提不出），**不写回原值**：
 *  把原值留在格子里，用户会以为规则生效了，拿着旧值去发布
 *  —— 那是本项目最坏的一类静默错（AGENTS.md 硬约束 5）。
 */
export function applyValuesToColumn<
  T extends { rows: (string | null)[][] },
>(
  table: T,
  column: number,
  changed: readonly { rowIndex: number; after: string | null }[],
): T {
  if (changed.length === 0) return table;
  const byIndex = new Map(changed.map((c) => [c.rowIndex, c.after]));
  return {
    ...table,
    rows: table.rows.map((row, i) => {
      if (!byIndex.has(i)) return row;
      const next = [...row];
      next[column] = byIndex.get(i) ?? null;
      return next;
    }),
  };
}

export function applyPatternToColumn<T extends { rows: (string | null)[][] }>(
  table: T,
  column: number,
  pattern: string,
  onlyRows?: readonly number[] | null,
): T {
  const { re } = compilePattern(pattern);
  if (!re || column < 0) return table;
  const scope = onlyRows && onlyRows.length > 0 ? new Set(onlyRows) : null;
  /** **规则处理异常的行**（用户口径 2026-10-21：「规则处理异常，无法处理的
   *  行数据红色标记并取消勾选」）。
   *
   *  ⚠️ 「没命中就原样保留」本身是对的（不该写空），但**必须回报**：
   *  否则用户看到「执行成功」，却不知道有几十行根本没被处理 ——
   *  与「静默失败」同类（硬约束 5）。
   */
  const failed: number[] = [];

  const next = {
    ...table,
    rows: table.rows.map((row, rowIndex) => {
      if (scope && !scope.has(rowIndex)) return row;
      const value = applyPatternToCell(row[column], re);
      //: 没命中 ⇒ 原样保留（不是写空），但**记入失败行**。
      if (value === null) {
        failed.push(rowIndex);
        return row;
      }
      return row.map((cell, ci) => (ci === column ? value : cell));
    }),
  } as T & { ruleFailedRows?: number[] };
  next.ruleFailedRows = failed;
  return next;
}

/** 表的**原始值快照**（撤销用；调用方在第一次覆盖前存一次）。
 *
 *  ⚠️ 快照**按列**记录（`columns[列下标] = 该列的原值`）—— 用户口径 2026-10-15：
 *  「**恢复数据应只恢复当前列而不是全表**」。
 *  存整表 `rows` 时，点任意一列的「还原原始值」会把**整张表所有列**都撤回，
 *  用户在另一列辛苦改的成果一并无损丢失（且不可逆）。
 *
 *  ⚠️ 这是**一份**快照、由 `applyPatternToColumn` 与 `applyValuesToColumn` 两条
 *  写值路径**共用**（`table + column` 复合键天然把两份语义并成一份）——
 *  若按「每次动作各存一份快照」，用户在两条路径间切换会撤到中间态。
 */
export type TableSnapshot = {
  key: string;
  /** `列下标 → 该列原值`（**逐列深拷贝**）。 */
  columns: Record<number, (string | null)[]>;
};

/** 某张表某个**单元格**的快照键（`sheet_name#表下标#列下标`）。
 *
 *  ⚠️ 用**复合键字符串**（不用 `number[]`）：数组按引用比较，同一列存两次会
 *  判成两个键，快照去重就失效了。`#` 不与 `sheet_name`（Excel 表名，不含 `#`）冲突。
 */
export function snapshotKey(tableIndex: number, column: number): string {
  return `#${tableIndex}#${column}`;
}

/** 生成某张表**某一列**的快照（key = `sheet_name` + 表下标 + 列下标）。 */
export function snapshotTable(
  table: { sheet_name: string; rows: (string | null)[][] },
  index: number,
  column: number,
): TableSnapshot {
  return {
    key: `${table.sheet_name}${snapshotKey(index, column)}`,
    columns: { [column]: table.rows.map((r) => r[column] ?? null) },
  };
}

/** 从快照还原**该快照覆盖的那些列**（本实现里恰好一列；未覆盖的列原样返回）。
 *
 *  ⚠️ 只碰快照里那几列的格子 —— 「改 A 列、还原 B 列」时 A 列必须留在改后值，
 *  这是本轮的核心断言（旧实现整表覆盖 ⇒ 必红）。
 */
export function restoreRows<T extends { rows: (string | null)[][] }>(
  table: T,
  snapshot: TableSnapshot | undefined,
): T {
  if (!snapshot) return table;
  return {
    ...table,
    rows: table.rows.map((row, ri) => {
      let next: (string | null)[] | null = null;
      for (const [key, colValues] of Object.entries(snapshot.columns)) {
        const ci = Number(key);
        //: ⚠️ 快照行数与现表不一致（表被换过）⇒ 越界不写，宁可不还原。
        if (ri >= colValues.length) continue;
        if (!next) next = [...row];
        next[ci] = colValues[ri] ?? null;
      }
      return next ?? row;
    }),
  };
}

/** 供弹窗显示的一句话结果（用户口径「异常提炼成简单明了」）。 */
export function patternOutcomeText(outcome: PatternOutcome): string {
  if (outcome.error) return outcome.error;
  if (outcome.total === 0) return "作用范围里没有可处理的行";
  return `命中 ${outcome.changed.length} 行，未命中 ${outcome.missed} 行（未命中的保持原值）`;
}
