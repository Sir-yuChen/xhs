/**
 * 行为验证：**页面筛选条件的本地缓存**（2026-09-26 用户口径「梳理所有商品列表，
 * 我的笔记，内容库页，筛选条件和账号选择后放本地缓存，避免每次均需要重新选择」）。
 *
 * 盯的四件事（都是「不许出现某种行为」，而源码断言证明不了）：
 *
 * 1. **坏缓存不能把页面打红**：隐私模式 / 配额满 / 手改过的半截 JSON 一律静默回
 *    默认值。尤其是「解析失败要顺手删掉那条 key」—— 留着的话每次进页面都要再解析
 *    一次坏 JSON（而且它永远不会自愈）。
 * 2. **取值域必须校验**：旧枚举（后端改过的状态值）、`"0"` 这种字符串数字、
 *    `NaN` 都不能流进筛选条件 —— 流进去就是「界面选得出、后端不认」或「把不筛
 *    当成 0 来筛」。
 * 3. **写入是尽力而为**：配额满时 `setItem` 抛异常不许冒泡到调用方（那会让一次
 *    纯体验性的缓存写失败把页面行为打断）。
 * 4. **键清单不许撞**：一个 key 被两页用 = 两页互相覆盖对方的筛选（用户看到
 *    「选了 A 页的账号，B 页的筛选也变了」）。
 *
 * 做法与 `note_filters.cjs` 同源：esbuild 打包**真实** `lib/filter-cache.ts`，
 * 塞一个假的 `window.localStorage` 直接驱动它（纯模块，不需要 jsdom / antd 桩）。
 *
 * 运行：
 *   <node> tests/frontend/persisted_filters.cjs                 # 期望全 PASS
 *   PERSISTED_FILTERS_MUTATE=stale   <node> …                   # 期望 FAIL
 *   PERSISTED_FILTERS_MUTATE=throw   <node> …                   # 期望 FAIL
 *   PERSISTED_FILTERS_MUTATE=enum    <node> …                   # 期望 FAIL
 *   PERSISTED_FILTERS_MUTATE=fields  <node> …                   # 期望 FAIL
 *   PERSISTED_FILTERS_MUTATE=draft   <node> …                   # 期望 FAIL
 *   PERSISTED_FILTERS_MUTATE=dupkey  <node> …                   # 期望 FAIL
 *   PERSISTED_FILTERS_MUTATE=tabs    <node> …                   # 期望 FAIL
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/filter-cache.ts");

const MUTATE = process.env.PERSISTED_FILTERS_MUTATE || "";

// 变异锚点（源码里**唯一**的串；行尾归一化防 CRLF 失配）
const RESERVED = {
  // 解析失败不删那条 key ⇒ 坏缓存永远留着（每次进页面都解析一次）
  stale: "    clearFilterCache(key);\n    return null;",
  // 写失败的 catch 改成往外抛 ⇒ 一次缓存写失败打断页面行为
  throw: "    store.setItem(key, JSON.stringify(value));\n  } catch {\n    /* 尽力而为 */",
  // 枚举不校验 ⇒ 旧枚举 / 表外值直接进筛选条件
  enum: '  return (raw: unknown, fallback: T) =>\n    typeof raw === "string" && (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback;',
  // 对象型筛选直接吃原值 ⇒ 脏字段（字符串数字 / 多出来的键）流进请求体
  fields: "    return next as T;",
  // 草稿不校验 status / 比较符 ⇒ 空状态发给后端（变成「未知状态」400）
  draft: "    status: asNonEmptyString(fields.status, EMPTY_SHOP_ITEMS_DRAFT.status),",
  // 两个页面用同一个 key ⇒ 互相覆盖对方的筛选
  dupkey: '    keyword: "xhs.filters.library.keyword",',
  // 页签集合少一个 ⇒ 缓存里那个页签被静默退回默认
  tabs: 'export const MY_NOTES_TAB_VALUES = ["notes", "liked", "collected", "interactions"] as const;',
};

const MUTATE_TO = {
  stale: "    return null;",
  throw: "    store.setItem(key, JSON.stringify(value));\n  } catch {\n    throw new Error(\"quota\");",
  enum: "  return (raw: unknown, fallback: T) => raw as T;",
  fields: "    return raw as T;",
  draft: "    status: fields.status,",
  dupkey: '    keyword: "xhs.filters.my-notes.tab",',
  tabs: 'export const MY_NOTES_TAB_VALUES = ["notes", "liked", "collected"] as const;',
};

const mutatePlugin = {
  name: "filter-cache-mutate",
  setup(build) {
    build.onLoad({ filter: /lib[\\/]filter-cache\.ts$/ }, (args) => {
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

function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed += 1;
    console.log(`PASS  ${name}`);
  } else {
    failed += 1;
    console.log(`FAIL  ${name}\n      期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
  }
}

/** 假 localStorage：`mode` 用来模拟隐私模式（读抛）/ 配额满（写抛）。 */
const store = new Map();
let mode = "ok";
const fakeLocalStorage = {
  getItem(key) {
    if (mode === "denied") throw new Error("localStorage denied");
    return store.has(key) ? store.get(key) : null;
  },
  setItem(key, value) {
    if (mode === "quota") throw new Error("QuotaExceededError");
    store.set(key, String(value));
  },
  removeItem(key) {
    store.delete(key);
  },
};

globalThis.window = { localStorage: fakeLocalStorage };

async function main() {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "filter-cache-"));
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
  const outFile = path.join(tmpDir, "filter-cache.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const mod = require(outFile);
  const {
    FILTER_CACHE_KEYS,
    MY_NOTES_TAB_VALUES,
    EMPTY_SHOP_ITEMS_DRAFT,
    readFilterCache,
    writeFilterCache,
    clearFilterCache,
    asBoolean,
    asEnum,
    asFields,
    asFiniteNumber,
    asNumberEnum,
    asString,
    sanitizeShopItemsDraft,
    NOTE_TYPE_FILTER_ALL_VALUES,
    PUBLISH_STATUS_FILTER_VALUES,
  } = mod;

  for (const name of ["readFilterCache", "writeFilterCache", "sanitizeShopItemsDraft"]) {
    if (typeof mod[name] !== "function") {
      console.error(`FATAL 未导出 ${name} —— 若已重命名，请同步更新本脚本`);
      return 1;
    }
  }

  // ------------------------------------------------------------------ A. 存储层
  // 「坏缓存不许把页面打红」：这一段的每一条都对应一次真实场景（隐私模式 / 手改）。
  const KEY = FILTER_CACHE_KEYS.library.keyword;

  writeFilterCache(KEY, "露营装备");
  check("A1 写入后读回原值", readFilterCache(KEY), "露营装备");
  check("A2 没存过的 key 读回 null（不是 undefined / 不是崩）", readFilterCache("xhs.filters.never-used"), null);

  store.set(KEY, "{半截写入");
  check("A3 坏 JSON 读回 null（当没存过）", readFilterCache(KEY), null);
  check("A3b 坏 JSON **顺手删掉**（否则永远自愈不了）", store.has(KEY), false);

  store.set(KEY, JSON.stringify({ nested: true }));
  check("A4 值类型由调用方的校验器保证（读只管是不是合法 JSON）", readFilterCache(KEY), { nested: true });

  mode = "denied";
  check("A5 隐私模式（读抛异常）不崩、读回 null", readFilterCache(KEY), null);

  mode = "quota";
  let threw = false;
  try {
    writeFilterCache(KEY, "x");
  } catch {
    threw = true;
  }
  check("A6 配额满（写抛异常）不冒泡给调用方", threw, false);
  mode = "ok";

  clearFilterCache(KEY);
  check("A7 删掉后读回 null", readFilterCache(KEY), null);

  delete globalThis.window;
  check("A8 没有 window（SSR / 非浏览器）也不崩", readFilterCache(KEY), null);
  globalThis.window = { localStorage: fakeLocalStorage };

  // ------------------------------------------------------------ B. 取值域校验
  check("B1 asString 放行空串（它就是「不筛」）", asString("", "默认"), "");
  check("B1b asString 非字符串回默认", asString(123, "默认"), "默认");
  check("B2 asBoolean 只认真正的布尔（`\"true\"` 不猜）", [asBoolean("true"), asBoolean(true)], [false, true]);
  check(
    "B3 asFiniteNumber 拒 NaN / Infinity / 字符串数字",
    [asFiniteNumber(NaN), asFiniteNumber(Infinity), asFiniteNumber("3"), asFiniteNumber(3)],
    [null, null, null, 3]
  );
  const publishEnum = asEnum(PUBLISH_STATUS_FILTER_VALUES);
  check("B4 asEnum 合法值保留", publishEnum("published", "all"), "published");
  check("B4b asEnum 表外 / 旧枚举值回默认（不流进请求）", publishEnum("archived", "all"), "all");
  const typeEnum = asEnum(NOTE_TYPE_FILTER_ALL_VALUES);
  check("B4c asEnum 认「不筛」（空串是合法取值）", typeEnum("", "local"), "");
  const sizeEnum = asNumberEnum([20, 50, 100]);
  check("B5 asNumberEnum 只能从候选里挑", [sizeEnum(50, 20), sizeEnum(999, 20)], [50, 20]);

  const thresholds = { max_browse: 0, max_pay_order: 0, max_pay_gmv: 0 };
  const fieldsSanitizer = asFields(thresholds);
  check(
    "B6 asFields 类型对不上的字段回默认 + 多出来的键丢掉",
    fieldsSanitizer({ max_browse: "0", max_pay_order: 5, hacked: true }, thresholds),
    { max_browse: 0, max_pay_order: 5, max_pay_gmv: 0 }
  );
  check("B7 asFields 整份脏值（数组 / 字符串）回默认", fieldsSanitizer("nope", thresholds), thresholds);
  const queryDefaults = { keyword: "", note_type: "", published_before: null };
  check(
    "B7b asFields 可空标量字段保留 null，也认时间戳",
    asFields(queryDefaults)({ ...queryDefaults, published_before: 1758888888000 }, queryDefaults),
    { keyword: "", note_type: "", published_before: 1758888888000 }
  );

  // ------------------------------------------------------ C. 商品列表条件草稿
  check("C1 空 / 脏输入 → 清空态（不崩、不造字段）", sanitizeShopItemsDraft(undefined), EMPTY_SHOP_ITEMS_DRAFT);
  check("C1b 数组也算脏输入", sanitizeShopItemsDraft([1, 2, 3]), EMPTY_SHOP_ITEMS_DRAFT);
  const draft = sanitizeShopItemsDraft({
    keyword: "连衣裙",
    status: "on_shelf",
    price_from: 10,
    price_to: 99,
    create_from: "2026-09-01",
    create_to: "2026-09-26",
    note_count_op: "gte",
    note_count: 3,
    junk: "丢掉我",
  });
  check("C2 合法草稿逐字段保留", [draft.keyword, draft.price_from, draft.create_to, draft.note_count_op, draft.note_count], ["连衣裙", 10, "2026-09-26", "gte", 3]);
  check("C2b 多出来的键不进草稿", "junk" in draft, false);
  check(
    "C2c 比较符表外值回默认（与后端同一份 NOTE_COUNT_OPS）",
    sanitizeShopItemsDraft({ ...EMPTY_SHOP_ITEMS_DRAFT, note_count_op: "~=" }).note_count_op,
    "lt"
  );
  check(
    "C2d 空状态回默认（空串发给后端 = 未知状态 400）",
    sanitizeShopItemsDraft({ ...EMPTY_SHOP_ITEMS_DRAFT, status: "" }).status,
    "on_shelf"
  );

  // ------------------------------------------------------------- D. 键清单
  const namespaces = Object.values(FILTER_CACHE_KEYS);
  const allKeys = namespaces.flatMap((group) => Object.values(group));
  check("D1 三个页面各一份命名空间", [namespaces.length, allKeys.length], [3, 17]);
  check("D1b 键名全部带页面前缀", allKeys.every((key) => key.startsWith("xhs.filters.")), true);
  check("D2 全局无重复键（重复 = 两页互相覆盖）", new Set(allKeys).size, allKeys.length);

  // ---------------------------------------------------------------- E. 页签
  check("E1 我的笔记页签顺序（渲染顺序与合法取值同一份）", MY_NOTES_TAB_VALUES, [
    "notes",
    "liked",
    "collected",
    "interactions",
  ]);

  console.log(`\n${passed} PASS / ${failed} FAIL`);
  return failed ? 1 : 0;
}

main().then(
  (code) => process.exit(code),
  (error) => {
    console.error(error);
    process.exit(1);
  }
);
