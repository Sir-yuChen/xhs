/**
 * 调用参数 KV 编辑器（设计 22 §5，2026-10-06 用户口径）。
 *
 * ## 用户口径
 *
 * 「定时任务调用参数使用 kv 输入框，**k 不要使用下拉框选择**」——
 * 所以 **key 与 value 都是自由文本输入框**，没有 key 下拉。
 *
 * ## 为什么仍是 KV 而不是固定字段表单
 *
 * 参数键由后端注册表下发（`spec.params_editable`），前端**不硬编码键名** ——
 * 新增一个任务参数只改注册表，前端零改动。用 KV 让「未知键」天然可表达。
 *
 * ## 已知键的提示（不阻断）
 *
 * `KNOWN_KEY_HINTS` 给已知键一句中文说明 + 示例，作为 placeholder / placeholder
 * 提示；用户也可以填注册表里没见过的键（由**后端**做白名单裁决并回 400）——
 * 前端**不拦**，因为「前端拦得住」是幻觉，真正的门禁必须在后端。
 *
 * ⚠️ 白名单是**后端**的事（`_validate_run_params` / `_apply_custom_params`）。
 * 这里只做「多值转数组」这种**形状**转换，形状错了后端会 400。
 */
import { Button, Input, Space, Tooltip } from "antd";

/** 一行参数（key / value 都是字符串；多值用逗号分隔）。 */
export type ParamRow = { key: string; value: string };

/** 已知键的说明（placeholder 提示用；**不是白名单**）。 */
export const KNOWN_KEY_HINTS: Record<string, string> = {
  account_ids: "执行账号，填账号 ID，多个用逗号分隔（如 1,2,3）",
  retention_days: "记录表保留天数（如 15）",
  days: "同步时间区间（天，如 7）",
  auto_task_id: "自动任务 ID",
};

/**
 * KV 行 → `params`（按 key 类型解析：`account_ids` 逗号分隔转 int 数组，其余原样）。
 *
 * ⚠️ 这是**唯一实现**：新建/编辑任务与「执行一次」弹窗共用它，避免两份形状转换
 * 规则（DEF-044）。
 */
export function paramsRowsToObject(rows: ParamRow[]): Record<string, unknown> {
  const params: Record<string, unknown> = {};
  for (const row of rows) {
    const key = row.key.trim();
    const value = row.value.trim();
    if (!key) continue;
    if (key === "account_ids") {
      // 空 = 本人全部账号（历史口径）；非空按逗号分隔转 int 数组。
      params[key] = value
        ? value
            .split(",")
            .map((v) => v.trim())
            .filter(Boolean)
            .map((v) => Number(v))
        : [];
    } else {
      params[key] = value;
    }
  }
  return params;
}

/** `params` 对象 → KV 行（多值数组逗号拼接）。 */
export function paramsObjectToRows(params: Record<string, unknown>): ParamRow[] {
  const rows: ParamRow[] = [];
  for (const key of Object.keys(params)) {
    const value = params[key];
    if (Array.isArray(value)) {
      rows.push({ key, value: value.map((v) => String(v)).join(",") });
    } else if (value === null || value === undefined) {
      rows.push({ key, value: "" });
    } else {
      rows.push({ key, value: String(value) });
    }
  }
  return rows;
}

/** 去掉 key 为空的行（后端按 key 收敛，空 key 行不该发出去）。 */
export function nonEmptyRows(rows: ParamRow[]): ParamRow[] {
  return rows.filter((row) => row.key.trim().length > 0);
}

export interface ParamsEditorProps {
  rows: ParamRow[];
  onChange: (rows: ParamRow[]) => void;
  /** 是否显示「+ 添加参数」按钮（只读场景可关）。 */
  editable?: boolean;
}

/** KV 行编辑器：**key 与 value 都是自由文本输入框**（用户口径：k 不用下拉）。 */
export function ParamsEditor({ rows, onChange, editable = true }: ParamsEditorProps) {
  function updateRow(index: number, patch: Partial<ParamRow>) {
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  function addRow() {
    onChange([...rows, { key: "", value: "" }]);
  }

  function removeRow(index: number) {
    onChange(rows.filter((_, i) => i !== index));
  }

  return (
    <Space direction="vertical" size={6} style={{ width: "100%" }}>
      {rows.map((row, index) => (
        <Space.Compact key={index} style={{ width: "100%" }}>
          {/* key = **自由文本输入**（用户口径：「k 不要使用下拉框选择」）。 */}
          <Tooltip title={KNOWN_KEY_HINTS[row.key.trim()]}>
            <Input
              value={row.key}
              placeholder="key（如 account_ids）"
              style={{ width: 220 }}
              onChange={(event) => updateRow(index, { key: event.target.value })}
            />
          </Tooltip>
          <Input
            value={row.value}
            placeholder={KNOWN_KEY_HINTS[row.key.trim()] ?? "value（多个值用逗号分隔）"}
            onChange={(event) => updateRow(index, { value: event.target.value })}
          />
          {editable && <Button onClick={() => removeRow(index)}>删</Button>}
        </Space.Compact>
      ))}
      {editable && (
        <Button block type="dashed" onClick={addRow}>
          + 添加参数
        </Button>
      )}
    </Space>
  );
}
