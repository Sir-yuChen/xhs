/**
 * 行为验证：运行时配置中心的**纯逻辑**（设计 10 §7.6，2026-09-27）。
 *
 * 盯的四件事（都是「源码里看着对、行为上是错的」那一类）：
 *
 * 1. **布尔渲染成「开 / 关」**：给运营看的页面不能出现 `true` / `false`；
 * 2. **缺值渲染 `-` 不渲染 `0`**（硬约束⑤）：`null` 与 `0` 是两件事 ——
 *    「不知道」被显示成「是 0」会让用户照着 0 去排查；
 * 3. **表单值 → 提交值按类型分派**：`bool` 必须提交**布尔**（不是字符串 `"true"`）、
 *    数值必须提交**数字**（`null` = 没填，交给后端 422，不在这里猜成 0）；
 * 4. **生效性标记来自后端的 `is_live`**：前端不自己按 key 名猜（猜 = 第二份白名单）。
 *
 * 做法与 `sync_gates.cjs` 同源：esbuild 打包**真实** `lib/settings-view.ts`，
 * 直接调用它（纯模块，不需要 antd 桩、不需要 jsdom）。
 *
 * 运行：
 *   <node> tests/frontend/settings_view.cjs                  # 期望全 PASS
 *   SETTINGS_VIEW_MUTATE=booltext   <node> …                 # 期望 FAIL
 *   SETTINGS_VIEW_MUTATE=dashzero   <node> …                 # 期望 FAIL
 *   SETTINGS_VIEW_MUTATE=boolraw    <node> …                 # 期望 FAIL
 *   SETTINGS_VIEW_MUTATE=guesslive  <node> …                 # 期望 FAIL
 *   SETTINGS_VIEW_MUTATE=category   <node> …                 # 期望 FAIL
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/settings-view.ts");

const MUTATE = process.env.SETTINGS_VIEW_MUTATE || "";

// 变异锚点（必须是源码里**唯一**的串；行尾归一化防 CRLF 失配 —— 失配的表现是
// 「变异静默不生效、脚本照常全绿」，比变红更危险）。
const RESERVED = {
  booltext: '  if (type === "bool") return value ? "开" : "关";',
  dashzero: '  if (value === null || value === undefined) return "-";',
  boolraw: '  if (type === "bool") return values.bool_value;',
  guesslive: "  if (setting.is_live) {",
  category: "  return category === \"all\" ? settings : settings.filter((item) => item.category === category);",
};

const MUTATE_TO = {
  // 布尔原样渲染 true / false（面向运营的页面出现程序字面量）
  booltext: '  if (type === "bool") return String(value);',
  // 缺值渲染 0 ⇒ 把「不知道」说成「是 0」（硬约束⑤）
  dashzero: '  if (value === null || value === undefined) return "0";',
  // bool 回退到文本框 ⇒ 提交字符串 "true"/"false"（后端按 bool 解 = 静默错值）
  boolraw: '  if (type === "bool") return values.text_value;',
  // 前端自己按 key 名猜生效性 ⇒ 第二份白名单，必然与后端漂移
  guesslive: '  if (String(setting.key).includes("rate_limit")) {',
  // 分组筛选失效（永远返回全部）⇒ 用户以为筛过了
  category: "  return settings;",
};

const mutatePlugin = {
  name: "settings-view-mutate",
  setup(build) {
    build.onLoad({ filter: /lib[\\/]settings-view\.ts$/ }, (args) => {
      let source = fs.readFileSync(args.path, "utf8").replace(/\r\n/g, "\n");
      if (MUTATE) {
        const needle = RESERVED[MUTATE];
        if (!needle) throw new Error(`未知变异：${MUTATE}`);
        if (!source.includes(needle)) {
          throw new Error(`变异锚点未命中（实现已改？）：${needle}`);
        }
        source = source.replace(needle, MUTATE_TO[MUTATE]);
      }
      return { contents: source, loader: "ts" };
    });
  },
};

let passed = 0;
let failed = 0;

function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed += 1;
    console.log(`PASS  ${label}`);
  } else {
    failed += 1;
    console.log(
      `FAIL  ${label}\n      期望：${JSON.stringify(expected)}\n      实际：${JSON.stringify(actual)}`
    );
  }
}

/** 造一行配置（只填被测逻辑用得到的字段）。 */
function setting(overrides) {
  return {
    id: 1,
    key: "k",
    value: 0,
    raw_value: "0",
    value_type: "int",
    category: "sync",
    label: "标签",
    description: "",
    requires_restart: false,
    is_dangerous: false,
    is_live: false,
    live_read_point: "",
    updated_by: null,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

async function main() {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "settings-view-"));
  const bundled = await esbuild.build({
    entryPoints: [MODULE_TS],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    target: "node18",
    logLevel: "silent",
    plugins: [mutatePlugin],
  });
  const outFile = path.join(tmpDir, "settings-view.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const mod = require(outFile);

  // ---- 1) 值渲染 ----
  check(
    "布尔渲染成「开 / 关」而不是 true / false",
    [
      mod.renderSettingValue(setting({ value: true, value_type: "bool" })),
      mod.renderSettingValue(setting({ value: false, value_type: "bool" })),
    ],
    ["开", "关"]
  );
  check(
    "缺值渲染 `-`（不渲染 0）",
    [
      mod.renderSettingValue(setting({ value: null, value_type: "int" })),
      mod.renderSettingValue(setting({ value: undefined, value_type: "str" })),
    ],
    ["-", "-"]
  );
  check(
    "真的 0 要渲染成 0（与缺值区分）",
    mod.renderSettingValue(setting({ value: 0, value_type: "int" })),
    "0"
  );
  check(
    "json 值渲染成字符串",
    mod.renderSettingValue(setting({ value: { a: 1 }, value_type: "json" })),
    '{"a":1}'
  );

  // ---- 2) 生效性标记（来自后端 is_live，前端不猜） ----
  check(
    "is_live=true ⇒ 实时生效",
    mod.liveBadge(setting({ is_live: true, live_read_point: "rate_limiter" })).label,
    "实时生效"
  );
  check(
    "is_live=false ⇒ 仅存档",
    mod.liveBadge(setting({ key: "crawl_rate_limit_per_minute", is_live: false })).label,
    "仅存档"
  );

  // ---- 3) 表单值 ↔ 提交值 ----
  check(
    "bool 提交**布尔**而不是字符串",
    mod.formValuesToSubmitValue({ ...mod.emptyFormValues(), value_type: "bool", bool_value: true }),
    true
  );
  check(
    "bool=false 提交 false（不是空串）",
    mod.formValuesToSubmitValue({ ...mod.emptyFormValues(), value_type: "bool", bool_value: false }),
    false
  );
  check(
    "int 提交数字",
    mod.formValuesToSubmitValue({
      ...mod.emptyFormValues(),
      value_type: "int",
      number_value: 60,
      text_value: "ignored",
    }),
    60
  );
  check(
    "数值没填 ⇒ 提交 null（交给后端 422，不猜成 0）",
    mod.formValuesToSubmitValue({ ...mod.emptyFormValues(), value_type: "int", number_value: null }),
    null
  );
  check(
    "str 提交文本",
    mod.formValuesToSubmitValue({
      ...mod.emptyFormValues(),
      value_type: "str",
      text_value: "hello",
    }),
    "hello"
  );

  // 编辑行 → 表单初值（round-trip）
  const roundTrip = mod.settingToFormValues(
    setting({ value: 60, value_type: "int", key: "crawl_rate_limit_per_minute" })
  );
  check("int 行 → 表单 number_value", roundTrip.number_value, 60);
  check("int 行 → 表单 value_type", roundTrip.value_type, "int");
  check(
    "json 行 → 表单文本是 JSON 串",
    mod.settingToFormValues(setting({ value: { a: 1 }, value_type: "json" })).text_value,
    '{"a":1}'
  );
  check(
    "缺值行 → 表单文本是空串（不是 'null'）",
    mod.settingToFormValues(setting({ value: null, value_type: "str" })).text_value,
    ""
  );

  // 新增提交体带 key / 编辑提交体不带 key
  const createPayload = mod.formValuesToCreatePayload({
    ...mod.emptyFormValues(),
    key: "  custom_key  ",
    value_type: "int",
    number_value: 5,
  });
  check("新增提交体带 key（且 trim）", createPayload.key, "custom_key");
  check("新增提交体带 value", createPayload.value, 5);
  const updatePayload = mod.formValuesToUpdatePayload({
    ...mod.emptyFormValues(),
    key: "custom_key",
    value_type: "int",
    number_value: 6,
  });
  check("编辑提交体**不带** key（改键要删旧建新）", "key" in updatePayload, false);
  check("编辑提交体带 value", updatePayload.value, 6);

  // ---- 4) 分组 ----
  const rows = [
    setting({ id: 1, key: "a", category: "sync" }),
    setting({ id: 2, key: "b", category: "feature" }),
    setting({ id: 3, key: "c", category: "sync" }),
  ];
  check("分组筛选项：全部 + 去重后的分组（保序）", mod.categoryOptions(rows), [
    { value: "all", label: "全部" },
    { value: "feature", label: "实验特性" },
    { value: "sync", label: "同步与限流" },
  ]);
  check(
    "筛选：all 返回全部",
    mod.filterByCategory(rows, "all").map((r) => r.key),
    ["a", "b", "c"]
  );
  check(
    "筛选：按分组收口",
    mod.filterByCategory(rows, "sync").map((r) => r.key),
    ["a", "c"]
  );
  check("未知分组回落原名（不显示空白）", mod.categoryLabel("weird"), "weird");

  console.log(`\n${passed} PASS / ${failed} FAIL`);
  if (failed > 0) process.exit(1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
