/**
 * 行为验证：内容库筛选栏话题下拉的**远程模糊搜索**（设计 13 §34.4，2026-09-25）。
 *
 * 背景（用户口径「内容库搜索区域中的话题下拉框支持输入模糊查询，控制查询频率」）：
 * 话题字典随同步链路持续增长（全量一次拉完再前端过滤只是把成本挪到进页面时）⇒
 * 筛选栏话题下拉改**远程搜索**：后端 `GET /topics?keyword=` LIKE 过滤，前端
 * `showSearch` + `filterOption={false}` + `onSearch` 经 **300ms 防抖**才发请求。
 * 本脚本钉这轮的**前端契约**：
 *
 * A 段（驱动真实代码）：esbuild 打包**真实** `lib/api.ts`，桩掉 `http.get`
 *   捕获参数，直接调 `fetchTopics` —— 断言 keyword 的透传规则（trim 后传 /
 *   空与全空白不传）。
 * B 段（驱动真实代码）：esbuild 打包**真实** `lib/topic-search.ts`，直接调
 *   `createTopicSearchDebouncer` —— 断言防抖语义：窗口内不触发、连续输入只发
 *   最后一次、cancel 后不触发、窗口常量 = 300ms。
 * C 段（源码守卫，library-page.tsx）：Select 是远程搜索形态（showSearch +
 *   filterOption={false}）、onSearch 接的是防抖器、竞态序号 + loading 在位。
 * D 段（源码守卫，backend/app/api/topics.py）：`keyword` Query 参数 + SQL 层
 *   `Topic.name.like` 过滤（不是拉全量回内存再筛）。
 *
 * 运行：
 *   <node> tests/frontend/library_topic_search.cjs            # 期望全 PASS
 *   LIB_TOPIC_SEARCH_MUTATE=nodebounce   <node> …             # 期望 FAIL（C2）
 *   LIB_TOPIC_SEARCH_MUTATE=localfilter  <node> …             # 期望 FAIL（C1）
 *   LIB_TOPIC_SEARCH_MUTATE=nolike       <node> …             # 期望 FAIL（D2）
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));

const FILES = {
  api: path.join(PROJECT, "frontend/src/lib/api.ts"),
  topicSearch: path.join(PROJECT, "frontend/src/lib/topic-search.ts"),
  library: path.join(PROJECT, "frontend/src/pages/platforms/xhs/library-page.tsx"),
  topicsApi: path.join(PROJECT, "backend/app/api/topics.py"),
};

const MUTATE = process.env.LIB_TOPIC_SEARCH_MUTATE || "";

// 变异锚点（必须是源码里**唯一**的串；先做 CRLF 归一化再匹配）。
const MUTATIONS = {
  // C2：onSearch 绕过防抖器直连查询函数 ⇒ 每次击键都打后端
  nodebounce: [
    {
      target: "library",
      from: "onSearch={(kw) => topicDebouncerRef.current?.search(kw)}",
      to: "onSearch={(kw) => { void searchTopicsByKeyword(kw); }}",
    },
  ],
  // C1：退回本地过滤（filterOption 默认 true）⇒ keyword 永不到后端
  localfilter: [
    {
      target: "library",
      from: "filterOption={false}",
      to: "filterOption={true}",
    },
  ],
  // D2：keyword 拉了不用（去掉 SQL 层 LIKE）⇒ 搜索退化成全量
  nolike: [
    {
      target: "topicsApi",
      from: 'statement = statement.where(Topic.name.like(f"%{keyword.strip()}%"))',
      to: "statement = statement  # 变异：LIKE 过滤被去掉",
    },
  ],
};

function readNorm(file) {
  return fs.readFileSync(file, "utf8").replace(/\r\n/g, "\n");
}

// 剥注释（`//` 行注释 + `/* */` 块注释），**不动字符串字面量** —— 与
// `test_page_optimizations.py::_code_only` 同一状态机：反向断言不剪注释就会被
// 自己的迁移说明命中（DEF-057 同型）。
function codeOnly(source) {
  const out = [];
  let index = 0;
  const length = source.length;
  while (index < length) {
    const char = source[index];
    const nxt = index + 1 < length ? source[index + 1] : "";
    if (char === "/" && nxt === "/") {
      while (index < length && source[index] !== "\n") index += 1;
      continue;
    }
    if (char === "/" && nxt === "*") {
      index += 2;
      // ⚠️ 步进必须是 1：`index += 2` 会让扫描相位错开，`*/` 落在奇数位时
      // 永远配对不上 ⇒ 一路吃到下一个块注释结尾，中间的真实代码被整段吞掉
      // （本脚本首发时实测踩中：useMemo 块被吞 ⇒ C3 误红）。
      while (index < length && !(source[index] === "*" && source[index + 1] === "/")) index += 1;
      continue;
    }
    if (char === '"' || char === "'" || char === "`") {
      const quote = char;
      out.push(char);
      index += 1;
      while (index < length) {
        out.push(source[index]);
        if (source[index] === "\\" && index + 1 < length) {
          out.push(source[index + 1]);
          index += 2;
          continue;
        }
        if (source[index] === quote) {
          index += 1;
          break;
        }
        index += 1;
      }
      continue;
    }
    out.push(char);
    index += 1;
  }
  return out.join("");
}

const sources = {
  api: readNorm(FILES.api),
  topicSearch: readNorm(FILES.topicSearch),
  library: readNorm(FILES.library),
  topicsApi: readNorm(FILES.topicsApi),
};
if (MUTATE) {
  const edits = MUTATIONS[MUTATE];
  if (!edits) {
    console.error(`FATAL 未知变异：${MUTATE}`);
    process.exit(2);
  }
  for (const edit of edits) {
    const source = sources[edit.target];
    if (!source.includes(edit.from)) {
      console.error(`FATAL 变异锚点未命中（实现已改？）：${edit.from.slice(0, 60)}…`);
      process.exit(2);
    }
    sources[edit.target] = source.replace(edit.from, edit.to);
  }
}

let failures = 0;
function check(name, condition, detail) {
  if (condition) {
    console.log(`PASS ${name}`);
  } else {
    failures += 1;
    // FAIL 也走 stdout（与其它行为脚本一致：pytest 断言 `FAIL Xn` 在 stdout 里）
    console.log(`FAIL ${name}${detail ? ` —— ${detail}` : ""}`);
  }
}

// 条件等待（AGENTS.md：禁固定 sleep 盲等 —— 判据用**内容**不用「元素在不在」）
async function waitFor(condition, timeout = 1000, step = 20) {
  const start = Date.now();
  for (;;) {
    if (condition()) return true;
    if (Date.now() - start >= timeout) return false;
    await new Promise((resolve) => setTimeout(resolve, step));
  }
}

async function bundle(entry, extraPlugins = []) {
  const bundled = await esbuild.build({
    entryPoints: [entry],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    target: "node18",
    logLevel: "silent",
    external: ["antd"],
    plugins: extraPlugins,
  });
  return bundled.outputFiles[0].text;
}

async function main() {
  // --------------------------------------------------------------------------- //
  // A 段：驱动真实 api.ts（fetchTopics 的 keyword 透传契约）
  // --------------------------------------------------------------------------- //
  const stubDir = fs.mkdtempSync(path.join(os.tmpdir(), "lib-topic-search-"));
  // antd 桩（external；内联会让断言看到另一份模块）
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

  const apiFeed = {
    name: "lib-topic-search-feed-api",
    setup(build) {
      build.onLoad({ filter: /lib[\\/]api\.ts$/ }, () => ({
        contents: `${sources.api}\nexport { http as __http };\n`,
        loader: "ts",
      }));
    },
  };
  const apiBundlePath = path.join(stubDir, "api.bundle.cjs");
  fs.writeFileSync(apiBundlePath, await bundle(FILES.api, [apiFeed]));
  const api = require(apiBundlePath);
  const { fetchTopics, __http } = api;
  if (typeof fetchTopics !== "function" || !__http) {
    console.error("FATAL 未导出 fetchTopics / http —— 若已重命名，请同步更新本脚本");
    process.exit(1);
  }

  const captured = [];
  __http.get = (url, config) => {
    captured.push({ url, config });
    return Promise.resolve({ data: { items: [], total: 0 } });
  };

  await fetchTopics("露营");
  check(
    "A1 keyword 非空 ⇒ params.keyword 透传",
    captured.at(-1).url === "/topics"
      && captured.at(-1).config?.params?.keyword === "露营",
    `实际：${captured.at(-1).url} ${JSON.stringify(captured.at(-1).config)}`
  );

  await fetchTopics("  露营  ");
  check(
    "A2 keyword 前后空白 ⇒ trim 后透传（后端 LIKE 不受空白干扰）",
    captured.at(-1).config?.params?.keyword === "露营",
    `实际：${JSON.stringify(captured.at(-1).config)}`
  );

  await fetchTopics("");
  check(
    "A3 空串 ⇒ 不带 params（后端按全量处理）",
    captured.at(-1).config?.params === undefined,
    `实际：${JSON.stringify(captured.at(-1).config)}`
  );

  await fetchTopics("   ");
  check(
    "A4 全空白 ⇒ 同样不带 params（空查询不打后端）",
    captured.at(-1).config?.params === undefined,
    `实际：${JSON.stringify(captured.at(-1).config)}`
  );

  // --------------------------------------------------------------------------- //
  // B 段：驱动真实 topic-search.ts（防抖语义）
  // --------------------------------------------------------------------------- //
  const topicSearchFeed = {
    name: "lib-topic-search-feed",
    setup(build) {
      build.onLoad({ filter: /lib[\\/]topic-search\.ts$/ }, () => ({
        contents: sources.topicSearch,
        loader: "ts",
      }));
    },
  };
  const tsBundlePath = path.join(stubDir, "topic-search.bundle.cjs");
  fs.writeFileSync(tsBundlePath, await bundle(FILES.topicSearch, [topicSearchFeed]));
  const ts = require(tsBundlePath);
  const { createTopicSearchDebouncer, TOPIC_SEARCH_DEBOUNCE_MS } = ts;
  if (typeof createTopicSearchDebouncer !== "function") {
    console.error("FATAL 未导出 createTopicSearchDebouncer —— 若已重命名，请同步更新本脚本");
    process.exit(1);
  }

  check(
    "B1 防抖窗口常量 = 300ms",
    TOPIC_SEARCH_DEBOUNCE_MS === 300,
    `实际：${TOPIC_SEARCH_DEBOUNCE_MS}`
  );

  const runs = [];
  const debouncer = createTopicSearchDebouncer((kw) => runs.push(kw));
  debouncer.search("a");
  debouncer.search("ab");
  debouncer.search("露营");
  // 窗口内（200ms < 300ms）不该有任何触发
  const earlyFired = await waitFor(() => runs.length > 0, 200);
  check(
    "B2 防抖窗口内不触发（200ms 时仍未发请求）",
    !earlyFired,
    `提前触发了：${JSON.stringify(runs)}`
  );
  const settled = await waitFor(() => runs.length > 0, 1000);
  check(
    "B3 连续输入只发最后一次（露营一次）",
    settled && runs.length === 1 && runs[0] === "露营",
    `实际：${JSON.stringify(runs)}`
  );

  const cancelledRuns = [];
  const debouncer2 = createTopicSearchDebouncer((kw) => cancelledRuns.push(kw));
  debouncer2.search("x");
  debouncer2.cancel();
  const cancelledFired = await waitFor(() => cancelledRuns.length > 0, 700);
  check(
    "B4 cancel 后不触发（卸载 / 关闭下拉时清定时器）",
    !cancelledFired,
    `cancel 失效：${JSON.stringify(cancelledRuns)}`
  );

  // --------------------------------------------------------------------------- //
  // C 段：library-page.tsx 源码守卫（先剥注释）
  // --------------------------------------------------------------------------- //
  const librarySource = codeOnly(sources.library);
  check(
    "C1 话题筛选 Select 是远程搜索形态（showSearch + filterOption={false}）",
    librarySource.includes("showSearch")
      && librarySource.includes("filterOption={false}"),
    "筛选下拉还在本地过滤"
  );
  check(
    "C2 远程搜索接线完整（onSearch 接防抖器 + import + 竞态序号 + loading）",
    librarySource.includes("onSearch={(kw) => topicDebouncerRef.current?.search(kw)}")
      && librarySource.includes('from "../../../lib/topic-search"')
      && librarySource.includes("createTopicSearchDebouncer(")
      && librarySource.includes("topicSearchSeq.current")
      && librarySource.includes("loading={topicSearching}"),
    "onSearch 没接防抖器 / 缺 import / 竞态防护 / loading 之一"
  );
  check(
    "C3 选中项常驻 options（先选再清输入不会渲染出裸 value）",
    librarySource.includes("const picked = availableTopics.find((t) => String(t.id) === selectedTopicFilter)")
      && librarySource.includes("return picked && !base.some((t) => t.id === picked.id) ? [picked, ...base] : base;"),
    "选中项合并逻辑没了"
  );

  // --------------------------------------------------------------------------- //
  // D 段：backend/app/api/topics.py 源码守卫（SQL 层过滤，不是拉全量回内存）
  // --------------------------------------------------------------------------- //
  const topicsSource = sources.topicsApi;
  check(
    "D1 list_topics 有 keyword Query 参数",
    topicsSource.includes("keyword: Optional[str] = Query("),
    "没有 keyword 参数"
  );
  check(
    "D2 keyword 非空走 SQL 层 Topic.name.like 过滤",
    topicsSource.includes('statement = statement.where(Topic.name.like(f"%{keyword.strip()}%"))'),
    "LIKE 过滤没了（搜索会退化成全量）"
  );
  check(
    "D3 空 / 全空白 = 不过滤（全量）",
    topicsSource.includes("if keyword and keyword.strip():"),
    "空关键词判定条件没了"
  );

  if (failures > 0) {
    console.error(`\n${failures} 项 FAIL`);
    process.exit(1);
  }
  console.log("\n全部 PASS");
}

main().catch((error) => {
  console.error(`FATAL ${error?.stack || error}`);
  process.exit(1);
});
