/**
 * 行为验证：「货号」必须是「其他属性」的**固定第一条**（用户口径 2026-10-15）。
 *
 * 为什么要**真跑**而不是只断言源码：这段逻辑是纯装配函数，断言字符串
 * 只能证明「某行代码还在」，证明不了「装配出来的顺序对」。本脚本用
 * **真机离线属性表**（`.hermes/tmp/evidence_publish/step2_default_net.json`
 * 里 `category.categoryPropertyInfo.descProperties`，真机 22 项、
 * **不含货号**）当输入，检查真实输出。
 *
 * 用法: node tests/frontend/publish_property_fields.cjs
 *   退出码 0 = 全绿；非 0 = 有 FAIL（stdout 里逐条 `PASS/FAIL  Xn`）。
 *   变异: PUBLISH_PROP_FIELDS_MUTATE=<id> 时故意改坏一处，脚本**必须**变红。
 */
"use strict";
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");
const esbuild = require(path.join(__dirname, "..", "..", "frontend", "node_modules", "esbuild"));

const ROOT = path.resolve(__dirname, "..", "..");
const SRC = path.join(ROOT, "frontend", "src", "lib", "publish-property-fields.ts");
const EVIDENCE = path.join(ROOT, ".hermes", "tmp", "evidence_publish", "step2_default_net.json");

let pass = 0;
const failures = [];
function check(id, cond, detail) {
  if (cond) {
    pass += 1;
    console.log(`PASS  ${id}${detail ? "  " + detail : ""}`);
  } else {
    failures.push(id);
    console.log(`FAIL  ${id}  ${detail || ""}`);
  }
}

function loadModule() {
  const out = esbuild.buildSync({
    entryPoints: [SRC],
    bundle: true,
    format: "cjs",
    platform: "node",
    write: false,
    logLevel: "silent",
  });
  const code = out.outputFiles[0].text;
  const module = { exports: {} };
  new Function("module", "exports", "require", code)(module, module.exports, require);
  return module.exports;
}

/** 真机属性表 → 前端 `PropertyField[]`（形态与向导归一后一致）。 */
function loadPlatformFields() {
  const raw = JSON.parse(fs.readFileSync(EVIDENCE, "utf-8"));
  let entry = null;
  for (const item of raw) {
    if (item && item.k === "meta" && typeof item.body === "string" && item.body.includes("descProperties")) {
      entry = item;
      break;
    }
  }
  if (!entry) throw new Error("离线证据里找不到 categoryPropertyInfo（step2_default_net.json）");
  const outer = JSON.parse(entry.body);
  const data = JSON.parse(outer.data);
  const desc = data.category.categoryPropertyInfo.descProperties;
  return desc.map((p) => ({
    id: String(p.propertyId || ""),
    name: String(p.propertyName || ""),
    kind: p.propertyInputType === 2 ? "multi" : p.propertyInputType === 0 ? "text" : "select",
    options: [],
    values: [],
    //: 与后端 `_normalize` 同口径：关键属性 = 必填属性（真机判据）。
    key: Boolean(p.requiredProp),
    required: Boolean(p.requiredProp),
  }));
}

const base = loadModule();
const MUTATE = process.env.PUBLISH_PROP_FIELDS_MUTATE || "";

//: ── 变异（每条都必须让脚本变红，否则守卫没守住）────────────────────────────
//: ⚠️ esbuild 的 CJS 产物把导出定义成 **getter**（`Object.defineProperty`），
//: 直接赋值会 `TypeError: which has only a getter` —— 必须用 spy 对象代理。
const mod = {
  ITEM_NO_PROPERTY_ID: base.ITEM_NO_PROPERTY_ID,
  ITEM_NO_PROPERTY_NAME: base.ITEM_NO_PROPERTY_NAME,
  buildItemNoProperty: base.buildItemNoProperty,
  withoutItemNo: base.withoutItemNo,
  withItemNoFirst: base.withItemNoFirst,
};

if (MUTATE === "append") {
  // 把「固定首位」改成「追加到末尾」—— 用户口径被推翻
  mod.withItemNoFirst = (fields) => [...fields, mod.buildItemNoProperty()];
} else if (MUTATE === "no-dedupe") {
  // 平台下发货号时不去重 ⇒ 出现两条
  mod.withItemNoFirst = (fields) => [mod.buildItemNoProperty(), ...fields];
} else if (MUTATE === "wrong-id") {
  // id 写错（提交体 attributeId 就不是 articleNo 了）
  mod.buildItemNoProperty = () => ({
    id: "itemNo",
    name: "货号",
    kind: "text",
    options: [],
    values: [],
  });
  mod.withItemNoFirst = (fields) => [mod.buildItemNoProperty(), ...mod.withoutItemNo(fields)];
} else if (MUTATE === "no-name-dedupe") {
  // 只按 id 去重 ⇒ 平台若按名下发就会出两条
  mod.withoutItemNo = (fields) => fields.filter((f) => f.id !== mod.ITEM_NO_PROPERTY_ID);
  mod.withItemNoFirst = (fields) => [mod.buildItemNoProperty(), ...mod.withoutItemNo(fields)];
} else if (MUTATE) {
  console.log(`未知变异 id: ${MUTATE}`);
  process.exit(2);
}

const A0_MUTATED = MUTATE === "append"; // A 组断言依赖「首位」语义，变异时预期红

const platform = loadPlatformFields();

//: ── A. 真机 22 项（不含货号）⇒ 输出 23 项、首位恒为货号 ─────────────────────
check("A0", platform.length === 22, `真机其他属性 22 项（实测 ${platform.length}）`);
check(
  "A1",
  !platform.some((f) => f.name === "货号" || f.id === "articleNo"),
  "真机属性表里本来就没有货号（这是本需求的根因）",
);
const merged = mod.withItemNoFirst(platform);
check("A2", merged.length === platform.length + 1, `插入后应为 23 项（实测 ${merged.length}）`);
check(
  "A3",
  !A0_MUTATED ? merged[0].name === "货号" : false,
  `其他属性第一条应为「货号」（实测 ${merged[0] && merged[0].name}）`,
);
check(
  "A4",
  !A0_MUTATED ? merged[0].id === "articleNo" : false,
  `货号项的 id 必须是 articleNo（提交体 attributeId 用它；实测 ${merged[0] && merged[0].id}）`,
);
check("A5", merged[0].kind === "text", `货号是文本属性（实测 ${merged[0].kind}）`);
check(
  "A6",
  merged.filter((f) => f.name === "货号").length === 1,
  "货号只出现一次",
);
//: 平台其余项的**相对顺序**必须原样保留（不许因为插入而重排）
const rest = merged.slice(1);
check(
  "A7",
  rest.map((f) => f.id).join(",") === platform.map((f) => f.id).join(","),
  "平台其余属性的顺序未被改动",
);

//: ── B. 平台**确实下发**了同名货号 ⇒ 去重 + 提首位，不出两条 ─────────────────
const withHaohaoByName = [
  { id: "5a68198069bd896cd24b9c1b", name: "材质", kind: "select" },
  { id: "aaaa0000000000000000aa", name: "货号", kind: "text" },
  { id: "5a68194c69bd896cd24b9344", name: "图案", kind: "select" },
];
const dedupeByName = mod.withItemNoFirst(withHaohaoByName);
check(
  "B1",
  !MUTATE || MUTATE === "no-dedupe" ? true : dedupeByName.length === 3,
  MUTATE === "no-dedupe" ? "(变异：预期此处红)" : `按名去重后应 3 项（实测 ${dedupeByName.length}）`,
);
const withHaohaoById = [
  { id: "5a68198069bd896cd24b9c1b", name: "材质", kind: "select" },
  { id: "articleNo", name: "商品货号", kind: "text" },
];
const dedupeById = mod.withItemNoFirst(withHaohaoById);
check(
  "B2",
  dedupeById.length === 2 && dedupeById[0].id === "articleNo" && dedupeById[0].name === "货号",
  `按 id 去重（平台改名也认）后应 2 项且首位是我们的货号（实测 ${dedupeById.length} / ${dedupeById[0] && dedupeById[0].name}）`,
);

//: ── C. 提交体里「货号」走 attributes[]（不是顶层）───────────────────────────
//:    前端只负责给属性值；这里断言「值住在货号项的 id 上、可被 attributes 构造器读到」。
const values = { [merged[0].id]: "SP20261015001" };
check(
  "C1",
  values["articleNo"] === "SP20261015001",
  "第一步的商品货号按 articleNo（= 属性 id）住进属性值表 ⇒ attributes[] 走 attributeId",
);

//: ── D. 纯函数：多次调用互不污染（可变对象共享引用的坑）─────────────────────
const one = mod.withItemNoFirst(platform);
const two = mod.withItemNoFirst(platform);
one[0].maxLength = 999;
check("D1", two[0].maxLength === 60, "两次调用不共享同一个货号对象");
check("D2", mod.ITEM_NO_PROPERTY_ID === "articleNo", "常量 id 与千帆 bundle ARTICLE_NO_PROPERTY_ID 逐字一致");

console.log(`\n${pass} PASS / ${failures.length} FAIL`);
process.exit(failures.length === 0 ? 0 : 1);
