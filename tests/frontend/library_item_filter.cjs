/**
 * 行为验证：内容库「挂链商品」筛选（设计 13 §25.8，2026-09-23）。
 *
 * 背景（用户两次报告同一商品）：挂链笔记弹窗 3 篇，内容库只看到 2 篇 ——
 * 缺的那篇排在 889 位且正文为空，关键词碰运气也找不到。修复 = 补验正文
 * （数据）+ `GET /api/notes?item_id=` 筛选（链路）+ 弹窗跳转（入口）。
 * 本脚本钉后两件的**前端契约**：
 *
 * A 段（驱动真实代码）：esbuild 打包**真实** `lib/api.ts`，桩掉 `http.get`
 *   捕获参数，直接调 `fetchSavedNotes` —— 断言 `item_id` 的 trim / 空串不筛 /
 *   与其它筛选共存。源码断言证明不了「参数真的这样映射」。
 * B 段（源码守卫，library-page.tsx）：URL `?item_id=` 初值、currentFilters /
 *   clearFilters 两处口径、URL 同步（replace）、输入框挂接、提示条、空态文案。
 * C 段（源码守卫，shop-items-page.tsx）：挂链弹窗的「在内容库中查看」跳转。
 *
 * 运行：
 *   <node> tests/frontend/library_item_filter.cjs            # 期望全 PASS
 *   LIB_ITEM_MUTATE=trim    <node> …                         # 期望 FAIL（A1/A2）
 *   LIB_ITEM_MUTATE=urlinit <node> …                         # 期望 FAIL（B1）
 *   LIB_ITEM_MUTATE=cfilter <node> …                         # 期望 FAIL（B2）
 *   LIB_ITEM_MUTATE=notice  <node> …                         # 期望 FAIL（B6）
 *   LIB_ITEM_MUTATE=jump    <node> …                         # 期望 FAIL（C1）
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));

const FILES = {
  api: path.join(PROJECT, "frontend/src/lib/api.ts"),
  library: path.join(PROJECT, "frontend/src/pages/platforms/xhs/library-page.tsx"),
  shop: path.join(PROJECT, "frontend/src/pages/platforms/xhs/shop-items-page.tsx"),
};

const MUTATE = process.env.LIB_ITEM_MUTATE || "";

// 变异锚点（必须是源码里**唯一**的串；先做 CRLF 归一化再匹配）。
const MUTATIONS = {
  // api.ts：不 trim ⇒ 带空格的 ID 直接发给后端（后端 trim 前它是裸串）
  trim: {
    file: "api",
    from: "item_id: platformOrFilters.item_id?.trim() || undefined,",
    to: "item_id: platformOrFilters.item_id,",
  },
  // library：URL 初值读取被删 ⇒ 弹窗跳转进来筛选不生效（用户二次报告的形态）
  urlinit: {
    file: "library",
    from: '() => new URLSearchParams(window.location.search).get("item_id") ?? ""',
    to: '() => ""',
  },
  // library：currentFilters 丢掉 item_id ⇒ 输入框改了也不发给后端
  cfilter: {
    file: "library",
    from: "item_id: itemIdFilter.trim() || undefined,",
    to: "item_id: undefined,",
  },
  // library：提示条不再渲染 ⇒ 用户面对只有原始 ID 的输入框，不知道在筛什么
  notice: { file: "library", from: "{itemIdFilter.trim() && (", to: "{false && (" },
  // shop：跳转查询键名写错 ⇒ 内容库读不到 item_id，跳过去等于没跳
  jump: {
    file: "shop",
    from: "navigate(`/platforms/xhs/library?item_id=${encodeURIComponent(notesItem.item_id)}`);",
    to: "navigate(`/platforms/xhs/library?itemid=${notesItem.item_id}`);",
  },
};

const read = (p) => fs.readFileSync(p, "utf8").replace(/\r\n/g, "\n");
const sources = { api: read(FILES.api), library: read(FILES.library), shop: read(FILES.shop) };

if (MUTATE) {
  const mutation = MUTATIONS[MUTATE];
  if (!mutation) {
    console.error(`FATAL 未知变异：${MUTATE}`);
    process.exit(1);
  }
  if (!sources[mutation.file].includes(mutation.from)) {
    console.error(`FATAL 变异锚点未命中（实现已改？）：${mutation.from}`);
    process.exit(1);
  }
  sources[mutation.file] = sources[mutation.file].replace(mutation.from, mutation.to);
}

// --- antd 桩（external，落回桩所在 node_modules；内联会让断言看到另一份数组） ---
const stubDir = fs.mkdtempSync(path.join(os.tmpdir(), "lib-item-filter-"));
const antdStubDir = path.join(stubDir, "node_modules", "antd");
fs.mkdirSync(antdStubDir, { recursive: true });
fs.writeFileSync(
  path.join(antdStubDir, "package.json"),
  JSON.stringify({ name: "antd", version: "0.0.0", main: "index.js" })
);
fs.writeFileSync(
  path.join(antdStubDir, "index.js"),
  "exports.message = { error: () => {}, success: () => {}, warning: () => {} };\n"
);

// --- 打包真实 api.ts（onLoad 喂**可能已变异**的源码 + 追加 http 导出） ----------
const feedPlugin = {
  name: "lib-item-filter-feed",
  setup(build) {
    build.onLoad({ filter: /lib[\\/]api\.ts$/ }, () => ({
      contents: `${sources.api}\nexport { http as __http };\n`,
      loader: "ts",
    }));
  },
};

let passed = 0;
let failed = 0;

function check(label, actual, expected) {
  if (actual === expected) {
    passed += 1;
    console.log(`PASS  ${label}`);
  } else {
    failed += 1;
    console.log(`FAIL  ${label}\n      期望：${JSON.stringify(expected)}\n      实际：${JSON.stringify(actual)}`);
  }
}

function has(label, source, needle) {
  check(label, source.includes(needle), true);
}

async function main() {
  const bundled = await esbuild.build({
    entryPoints: [FILES.api],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    target: "node18",
    logLevel: "silent",
    external: ["antd"],
    plugins: [feedPlugin],
  });
  const outFile = path.join(stubDir, "api.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const api = require(outFile);
  const { fetchSavedNotes, __http } = api;
  if (typeof fetchSavedNotes !== "function" || !__http) {
    console.error("FATAL 未导出 fetchSavedNotes / http —— 若已重命名，请同步更新本脚本");
    process.exit(1);
  }

  // A. 驱动真实 fetchSavedNotes：桩掉 http.get 捕获参数
  const captured = [];
  __http.get = (url, config) => {
    captured.push({ url, params: config?.params ?? {} });
    return Promise.resolve({ data: { items: [], total: 0, stats: {} } });
  };

  await fetchSavedNotes({ platform: "xhs", item_id: "  ITEM-1  " });
  check("A1 item_id 带空格 ⇒ trim 后发出", captured.at(-1).params.item_id, "ITEM-1");
  check("A1b 请求路径仍是 /notes", captured.at(-1).url, "/notes");

  await fetchSavedNotes({ platform: "xhs", item_id: "" });
  check("A2 空串 item_id = 不筛（不传参数）", captured.at(-1).params.item_id, undefined);

  await fetchSavedNotes({ platform: "xhs" });
  check("A3 不传 item_id = 不筛", captured.at(-1).params.item_id, undefined);

  await fetchSavedNotes({
    platform: "xhs",
    item_id: "ITEM-9",
    note_type: "synced",
    platform_account_id: 8,
  });
  check("A4 与其它筛选共存：item_id 在", captured.at(-1).params.item_id, "ITEM-9");
  check("A4b 与其它筛选共存：note_type 不被挤掉", captured.at(-1).params.note_type, "synced");
  check("A4c 与其它筛选共存：账号筛选不被挤掉", captured.at(-1).params.platform_account_id, 8);

  // B. library-page.tsx 源码守卫
  const lib = sources.library;
  has("B1 URL ?item_id= 初值读取在位（弹窗跳转落地的前提）", lib, 'new URLSearchParams(window.location.search).get("item_id")');
  has("B2 currentFilters 带 item_id（筛选变了一定发给后端）", lib, "item_id: itemIdFilter.trim() || undefined,");
  has("B3 clearFilters 清 state + 删 URL 参数（刷新不把筛选加回来）", lib, 'next.delete("item_id")');
  has("B4 URL 同步用 replace（不留历史戳）", lib, "setSearchParams(next, { replace: true })");
  has("B5 输入框挂接 changeItemIdFilter（不是裸 setState）", lib, "onChange={(e) => changeItemIdFilter(e.target.value)}");
  has("B6 提示条渲染门：仅筛选生效时出现", lib, "{itemIdFilter.trim() && (");
  has("B6b 提示条：商品 ID + 共 N 篇（当前列表为什么是这批）", lib, "挂链商品 {itemIdFilter.trim()}");
  has("B6c 提示条：命中篇数与关系表同口径", lib, "共 {total} 篇");
  has("B7 空态文案随筛选切换（不再谎称「内容库还是空的」）", lib, "这个商品在内容库里还没有挂链笔记");
  has("B8 useSearchParams 已导入", lib, 'import { Link, useNavigate, useSearchParams } from "react-router-dom";');

  // C. shop-items-page.tsx 源码守卫
  const shop = sources.shop;
  has("C1 跳转目标带 ?item_id=（与内容库读取键一字不差）", shop, "navigate(`/platforms/xhs/library?item_id=${encodeURIComponent(notesItem.item_id)}`);");
  has("C2 按钮文案「在内容库中查看」+ 篇数", shop, "在内容库中查看（{notesData.count} 篇）");
  has("C3 useNavigate 已导入", shop, 'import { useNavigate } from "react-router-dom";');
  has("C4 跳转前先关弹窗（路由切换不会自动关它）", shop, "setNotesOpen(false);");

  console.log(`\n${passed} passed / ${failed} failed`);
  process.exit(failed > 0 ? 1 : 0);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
