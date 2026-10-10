/**
 * 调用参数 KV 编辑器（设计 22 §5，2026-10-06 用户口径）。
 *
 * ## 用户口径
 *
 * 「定时任务调用参数使用 kv 输入框，**k 不要使用下拉框选择**」——
 * 所以 **key 是自由文本输入框**，没有 key 下拉。
 *
 * 🔄 **2026-10-07 追加口径：value 不再一律自由文本**
 * 「优化定时任务，当调用参数为执行账号时使用下拉复选框」—— `account_ids`
 * 这一行的 **value 改成账号下拉多选**（数据源 = 本人账号列表，只展示账号名称）。
 * 其余键的 value 仍是自由文本。
 *
 * 为什么 `account_ids` 必须特殊：它是**引用型参数**（值 = 账号主键），
 * 手抄 id 既可能抄错（静默跑到别的账号），也可能抄到别人的账号（归属校验 400）。
 * 下拉把「有哪些合法取值」直接摆出来，抄错这条路消失。
 * ⚠️ 这条**取代**了原先 `job-form-modal` 的「账号对照：id=昵称」提示行 ——
 * 名字直接显示在选项里，那行提示成了重复的第二份真相源（原句留在 git 历史）。
 *
 * ## 为什么仍是 KV 而不是固定字段表单
 *
 * 参数键由后端注册表下发（`spec.params_editable`），前端**不硬编码键名** ——
 * 新增一个任务参数只改注册表，前端零改动。用 KV 让「未知键」天然可表达。
 *
 * ## 已知键的提示（不阻断）
 *
 * `KNOWN_KEY_HINTS` 给已知键一句中文说明 + 示例，作为 placeholder 提示；
 * 用户也可以填注册表里没见过的键（由**后端**做白名单裁决并回 400）——
 * 前端**不拦**，因为「前端拦得住」是幻觉，真正的门禁必须在后端。
 *
 * ⚠️ 白名单是**后端**的事（`_validate_run_params` / `_apply_custom_params`）。
 * 这里只做「多值转数组」这种**形状**转换，形状错了后端会 400。
 */
import { Button, Input, Select, Space } from "antd";

/** 一行参数（key 是字符串；多值 value 以逗号分隔存储，下游统一解析）。 */
export type ParamRow = { key: string; value: string };

/** 执行账号参数键（**唯一**需要下拉多选的键；别处不要再写这个字面量）。 */
export const ACCOUNT_IDS_KEY = "account_ids";

/**
 * 账号选项（执行账号下拉的数据源）。
 *
 * ⚠️ 由**调用方**拉取后注入 —— 编辑器自己不发网络请求：同一份账号列表再拉一次
 * 就是第二个真相源，且「执行一次」弹窗一打开就会多一次往返。
 */
export type ParamAccountOption = { id: number; nickname: string };

/** 账号选项文案（缺昵称退 `账号 #id`，**不猜名字**，硬约束⑤）。 */
export function accountOptionLabel(account: ParamAccountOption): string {
  const name = (account.nickname ?? "").trim();
  return name || `账号 #${account.id}`;
}

/** `"1,2,3"` → `["1","2","3"]`（下拉的受控值；空串 ⇒ 空数组 = 本人全部账号）。 */
export function splitMultiValue(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

/**
 * 枚举型参数的取值表（key → 取值 → 中文名）。
 *
 * 2026-10-07：「每日发布商业笔记」的 `generate_mode` 是**单选**（用户口径
 * 「指定生成笔记的方式千帆智能笔记或LLM生成单选」）—— 自由文本会拼出
 * `qianfan ` / `Qianfan` 这类后端不认的值。**只有枚举键走下拉**，其余键的 value
 * 仍是自由文本（key 本身也恒为自由文本，2026-10-06 口径未变）。
 */
export const PARAM_ENUM_OPTIONS: Record<string, Record<string, string>> = {
  generate_mode: { qianfan: "千帆智能笔记", llm: "LLM 生成" },
  // 《每日发布商业笔记》发布失败重试开关（2026-10-07 用户口径三件套之一）：
  // 取值与后端 `_coerce_bool` 的 `_TRUE_WORDS` / `_FALSE_WORDS` **同值** ——
  // 下拉里选得出来的值后端必须认（跨语言同值由守卫钉住，别只改一侧）。
  retry_on_failure: { true: "重试", false: "不重试" },
};

/**
 * 参数的**中文名**（唯一真相源，2026-10-07 从 `job-form-modal.tsx` 下移到此）。
 *
 * 两处都用它：① 参数行**最前面的只读中文名列**（用户口径「中文参数名称写到最前面，
 * 不允许编辑只能预览」）；② 弹窗里的校验文案。放在本模块是因为①就在本模块渲染 ——
 * 映射与渲染处同源，不会出现「改了名字但界面没跟着变」。
 *
 * 缺键回落顺序：`PARAM_LABELS` → `paramLabel` 的 `KNOWN_KEY_HINTS` → **裸 key**（缺值不猜）。
 */
//: 中文名一律**简短**（2026-10-07 用户口径「中文名称简短展示全部」）：列宽按最短能放下的
//: 写，**不做省略号截断** —— 一个需要 Tooltip 才看得全的名字就不算「简短」。
export const PARAM_LABELS: Record<string, string> = {
  account_ids: "执行账号",
  retention_days: "保留天数",
  days: "同步天数",
  auto_task_id: "自动任务 ID",
  limit: "发布篇数",
  interval_seconds: "发布间隔",
  generate_mode: "生成方式",
  retry_on_failure: "失败重试",
  max_retries: "重试次数",
  retry_interval_seconds: "重试间隔",
};

/** 取参数中文名（缺值不猜：退到提示文案，再退到裸 key）。 */
export function paramLabel(key: string): string {
  const trimmed = key.trim();
  if (!trimmed) return "";
  return PARAM_LABELS[trimmed] ?? KNOWN_KEY_HINTS[trimmed] ?? trimmed;
}

/**
 * 参数的**单位**（2026-10-07 用户口径「存在单位的放后面」）。
 *
 * 单位**不写进中文名**（「发布间隔（秒）」= 名字里塞了单位），而是作为值输入框的**后缀**
 * 显示 —— 后缀跟着输入框走，一眼就知道填的是秒还是天，且改单位只改这一处。
 * 无单位的键不出现在表里。
 */
export const PARAM_UNITS: Record<string, string> = {
  interval_seconds: "秒",
  retry_interval_seconds: "秒",
  days: "天",
  retention_days: "天",
};

/** 已知键的说明（**只做 value 输入框的 placeholder**；`Tooltip` 已于 2026-10-07 按用户口径移除）。
 * 也不是白名单 —— 真正的白名单在后端。
 *
 * 🔄 2026-10-07 用户口径「tip 内容全部精简只提示核心内容即可」：
 * 原先每条都写成「名词（解释；补充说明）」的整句，弹窗里一眼看过去全是字。
 * 现在**只留核心名词** —— 取值形态由控件本身表达（下拉 / 数字框比文字更直接）。
 */
export const KNOWN_KEY_HINTS: Record<string, string> = {
  account_ids: "执行账号",
  retention_days: "保留天数",
  days: "同步天数",
  auto_task_id: "自动任务 ID",
  limit: "发布篇数",
  interval_seconds: "发布间隔",
  generate_mode: "生成方式",
  retry_on_failure: "发布失败重试",
  max_retries: "重试次数",
  retry_interval_seconds: "重试间隔",
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
    if (key === ACCOUNT_IDS_KEY) {
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

/**
 * 用**注册表默认值**作骨架合并出参数行（2026-10-07 用户口径「不允许随意添加和删除，
 * 必须有默认值」）。
 *
 * 规则（**唯一实现** —— 新建 / 编辑两个弹窗共用，DEF-044）：
 * 1. 键集合 = `defaults` 的键（= 后端 `params_editable`，顺序也照它）；
 * 2. 值优先取 `params` 里已有的（用户改过的、或存量行里的）；
 * 3. `params` 里**多出来**的键也保留在末尾 —— **不静默丢弃**（丢了 = 用户看不到自己
 *    存的值，而它执行时仍可能生效，这种「看不见但生效」正是要避免的）。
 */
export function mergeParamDefaults(
  params: Record<string, unknown>,
  defaults: Record<string, unknown>
): ParamRow[] {
  const merged: Record<string, unknown> = { ...defaults };
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) merged[key] = value;
  }
  return paramsObjectToRows(merged);
}

/** 去掉 key 为空的行（后端按 key 收敛，空 key 行不该发出去）。 */
export function nonEmptyRows(rows: ParamRow[]): ParamRow[] {
  return rows.filter((row) => row.key.trim().length > 0);
}

export interface ParamsEditorProps {
  rows: ParamRow[];
  onChange: (rows: ParamRow[]) => void;
  /**
   * 是否允许**增删行**。2026-10-07 用户口径「调用参数不允许随意添加和删除，
   * 必须有默认值」⇒ 两个弹窗都传 `false`（行由注册表 `params_editable` 决定，
   * 固定渲染）。**参数保留只为兼容**（默认 `false`，别处不要再传 `true`）。
   */
  editable?: boolean;
  /** 账号下拉的数据源（只在出现 `account_ids` 行时才用得上）。 */
  accounts?: ParamAccountOption[];
  /** 后端下发的键集合（`params_editable`）—— 非白名单键的行**不渲染**（只读展示）。 */
  allowedKeys?: string[];
}

/**
 * KV 行编辑器。
 *
 * 🔄 **2026-10-07 用户口径「系统内置任务的调用参数不可删除不可添加；非系统内置任务
 * 可删除、可添加调用参数」** —— 所以本组件的行为**由 `editable` 决定**：
 *
 * * `editable === false`（内置任务）：行**固定**（来自注册表 `params_editable`），
 *   key 只读，没有「+ 添加参数」也没有「删」；
 * * `editable === true`（非内置任务）：可增删行、可改 key（自己建的任务、参数喂给
 *   自己的执行函数，无跨租户面）。
 *
 * value 控件按 key 决定：`account_ids` = 账号下拉多选 / 枚举键 = 下拉单选 /
 * 其余 = 自由文本。
 * `allowedKeys` 之外的行不渲染（内置任务按白名单收口；非内置传 undefined 不过滤）。
 */
export function ParamsEditor({
  rows,
  onChange,
  editable = false,
  accounts = [],
  allowedKeys,
}: ParamsEditorProps) {
  // 只渲染白名单内的键（未传 allowedKeys ⇒ 不额外过滤，保持调用方自己决定）。
  const visibleRows = rows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => !allowedKeys || allowedKeys.includes(row.key.trim()));

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
      {visibleRows.map(({ row, index }) => {
        const trimmedKey = row.key.trim();
        const isAccountKey = trimmedKey === ACCOUNT_IDS_KEY;
        // 枚举键（如 `generate_mode`）走**下拉单选** —— 自由文本会拼出后端不认的值。
        const enumOptions = PARAM_ENUM_OPTIONS[trimmedKey];
        // ⚠️ **不用 `Space.Compact`**（2026-10-07 修「中英文名展示不一致长」）：
        // 它把子项都变成 flex 项（`flex-shrink` 默认可压缩，还会把相邻边框焊成一组），
        // 于是 inline 的 `width: 180` 在行宽不足时被按比例压掉 —— 同一个 180px 两列
        // 显示出来不一样长。改用显式 flex + 三列各 `flex: none`，宽度即所见。
        return (
          <div key={index} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {/*
             * 2026-10-07 用户口径「调用参数的中文参数名称写到**最前面**，不允许编辑
             * 只能预览」：行首固定一列中文名，恒 `readOnly`。
             *
             * 它**不是第二个真相源** —— 取值优先 `PARAM_LABELS[key]`（新增/修改弹窗里
             * 那份映射，与后端 `JobSpec.name` 同批维护），缺则回落 `KNOWN_KEY_HINTS`，
             * 再缺才显示裸 key（缺值不猜，硬约束⑤）。
             * ⚠️ `readOnly` 是**写死的**，不随 `editable` 变 —— 用户改 key 时中文名跟着
             * 变（受控），用户不能手改中文名本身（它不是参数的一部分）。
             */}
            {/* 无 Tooltip（2026-10-07 用户口径「移除tip」）：名字已收短、列宽放得下全部，
                再挂一个写着同样文字的提示是多余的。 */}
            <Input
              value={paramLabel(row.key.trim())}
              readOnly
              // 2026-10-07 用户口径「中英文名称长度统一 180px」：两列同宽，
              // 三列（中文名 / key / 值）对齐成表，不像拼贴。
              style={{ width: 180, flex: "none", cursor: "default" }}
              placeholder="参数名称"
            />
            {/* key：内置任务**只读**（参数由注册表决定）；非内置可改。无 Tooltip。 */}
            <Input
              value={row.key}
              readOnly={!editable}
              placeholder="key（如 account_ids）"
              // 2026-10-07 用户口径「中英文名称长度统一 180px」：与中文名列同宽。
              // ⚠️ 不做省略号 —— 靠列宽保证 `retry_interval_seconds` 这类长键完整可读。
              style={{ width: 220, flex: "none", cursor: editable ? "text" : "default" }}
              onChange={
                editable ? (event) => updateRow(index, { key: event.target.value }) : undefined
              }
            />
            {enumOptions ? (
              <Select
                value={row.value.trim() || undefined}
                placeholder="请选择"
                style={{ width: 350, flex: "none" }}
                onChange={(value: string) => updateRow(index, { value })}
                options={Object.entries(enumOptions).map(([value, label]) => ({ value, label }))}
              />
            ) : isAccountKey ? (
              // 执行账号 = **下拉多选**（2026-10-07 用户口径）。空 = 本人全部账号。
              // ⚠️ 刻意**不提供「全选」**（用户口径：有上限的多选不要全选按钮）——
              //    antd 的多选默认也没有它，不要顺手加。
              <Select
                mode="multiple"
                value={splitMultiValue(row.value)}
                placeholder="留空 = 本人全部账号"
                style={{ width: 350, flex: "none" }}
                optionFilterProp="label"
                onChange={(values: string[]) => updateRow(index, { value: values.join(",") })}
                options={accounts.map((account) => ({
                  value: String(account.id),
                  label: accountOptionLabel(account),
                }))}
              />
            ) : (
              <Input
                value={row.value}
                placeholder={KNOWN_KEY_HINTS[row.key.trim()] ?? "value"}
                // 单位作**后缀**展示（用户口径「存在单位的放后面」）。
                suffix={PARAM_UNITS[row.key.trim()] ?? undefined}
                // ⚠️ **必须给宽度**（2026-10-07 修「值列长短不一致」的真凶）：
                // 不设宽度时这个 `Input` 在 flex 容器里默认 `flex-grow:1`，会撑满
                // 剩余空间 ⇒ 它比上面那两个固定 200px 的下拉/多选**长出一截**。
                // 三个分支（枚举下拉 / 账号多选 / 普通文本）必须同宽同 `flex:none`。
                style={{ width: 350, flex: "none" }}
                onChange={(event) => updateRow(index, { value: event.target.value })}
              />
            )}
            {/* 「删」只在**非内置**任务出现（内置任务的参数不可删除）。 */}
            {editable && <Button onClick={() => removeRow(index)}>删</Button>}
          </div>
        );
      })}
      {/* 「+ 添加参数」同上：只有非内置任务能加。 */}
      {editable && (
        <Button block type="dashed" onClick={addRow}>
          + 添加参数
        </Button>
      )}
    </Space>
  );
}
