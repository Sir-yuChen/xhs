/**
 * 行为验证：内容库「标签并入话题」（设计 13 §34.4，2026-09-25）。
 *
 * 背景（用户 ② 口径：「将当前系统的笔记标签表合并成话题表，将内容库笔记详情弹窗中
 * 标题后展示标签tag和添加标签tag合并到笔记话题功能中」）：`tags` / `note_tags`
 * 两表并入 `topics` / `note_topics`；详情弹窗标题栏的标签 chips + 「+ 添加标签」
 * 弹层整块移除，正文下方话题区一个入口管全部话题（AutoComplete 选已有 / 输新名）。
 * 本脚本钉这轮的**前端契约**：
 *
 * A 段（驱动真实代码）：esbuild 打包**真实** `lib/api.ts`，桩掉 `http.get` /
 *   `http.put` 捕获参数，直接调 `fetchTopics` / `updateSavedNoteTopics` ——
 *   断言话题字典走 `/topics`、PUT 载荷是**话题名数组**（后端 find-or-create）。
 * B 段（源码守卫，library-page.tsx）：
 *   B1 标题栏**不再有**标签 chips / 「添加标签」Popover（整块移除，不是藏起来）；
 *   B2 话题区渲染 `selectedNote.topics` 的**对象**（`t.id` / `t.name`，不是字符串）；
 *   B3 话题区「+」用 **AutoComplete**（选已有 / 输新名，标签能力搬过来了）；
 *   B4 筛选下拉走 `topic_id`（原 `tag_id`）+「全部话题」文案；
 *   B5 卡片话题读 `note.topics`（原 `note.tags`）。
 * C 段（源码守卫，api.ts）：无 `/tags` 路由、无 `batchTagNotes`。
 *
 * 运行：
 *   <node> tests/frontend/library_topics_merge.cjs            # 期望全 PASS
 *   LIB_TOPIC_MUTATE=titlebar <node> …                        # 期望 FAIL（B1）
 *   LIB_TOPIC_MUTATE=objects   <node> …                        # 期望 FAIL（B2）
 *   LIB_TOPIC_MUTATE=autocomplete <node> …                     # 期望 FAIL（B3）
 *   LIB_TOPIC_MUTATE=filter    <node> …                        # 期望 FAIL（B4）
 *   LIB_TOPIC_MUTATE=route     <node> …                        # 期望 FAIL（C1）
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));

const FILES = {
  api: path.join(PROJECT, "frontend/src/lib/api.ts"),
  library: path.join(PROJECT, "frontend/src/pages/platforms/xhs/library-page.tsx"),
};

const MUTATE = process.env.LIB_TOPIC_MUTATE || "";

// 变异锚点（必须是源码里**唯一**的串；先做 CRLF 归一化再匹配）。
const MUTATIONS = {
  // B1：把移除干净的标题栏标签 UI 加回来（chips + 加号 Popover）
  titlebar: [
    [
      `title={
          <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>
            {selectedNote?.title || "笔记详情"}
          </span>
        }`,
      `title={
          <Space size={[4, 4]} wrap>
            <span>{selectedNote?.title || "笔记详情"}</span>
            <Tooltip title="添加标签"><Popover content={null}><Tag icon={<PlusOutlined />} /></Popover></Tooltip>
            {(selectedNote?.tags ?? []).map((t) => (<Tag key={t.id} closable onClose={(e) => { e.preventDefault(); removeTagFromNote(t.id); }}>{t.name}</Tag>))}
          </Space>
        }`,
    ],
  ],
  // B2：话题 chips 改回按字符串渲染（旧 topics_json 形态）
  objects: [
    [
      "key={t.id}",
      "key={t}",
    ],
    [
      "#{t.name}",
      "#{t}",
    ],
  ],
  // B3：话题区「+」的 AutoComplete 下拉清空（丢掉「选已有」能力）
  autocomplete: [
    [
      "options={addableTopics.map((t) => ({ value: t.name, label: t.name }))}",
      "options={[]}",
    ],
  ],
  // B4：筛选回退 tag_id + 标签文案
  filter: [
    [
      'placeholder="全部话题"',
      'placeholder="全部标签"',
    ],
    [
      "topic_id: selectedTopicFilter ? Number(selectedTopicFilter) : undefined,",
      "tag_id: selectedTopicFilter ? Number(selectedTopicFilter) : undefined,",
    ],
  ],
  // B5：把话题渲染加回卡片（Card.Meta 之后）—— 用户口径「话题不要放卡片上」
  cards: [
    [
      `{getNotePublishTime(note) || formatSavedTime(note.created_at)}
                      </Text>
                    </div>
                  } />`,
      `{getNotePublishTime(note) || formatSavedTime(note.created_at)}
                      </Text>
                    </div>
                  } />
                  {(note.topics ?? []).slice(0, 4).map((t) => (
                    <Tag key={t.id} color="blue" style={{ fontSize: 11, margin: 0 }}>{t.name}</Tag>
                  ))}`,
    ],
  ],
  // C1：话题字典路由回退 /tags
  // ⚠️ 2026-09-25 v1.46：fetchTopics 加了 keyword 参数（远程模糊搜索），旧锚点
  // `await http.get<Paginated<NoteTopic>>("/topics");` 已不在源码里 —— 改用
  // `http.get<Paginated<NoteTopic>>("/topics"` 前缀（get + /topics 唯一）。
  route: [
    [
      'await http.get<Paginated<NoteTopic>>("/topics"',
      'await http.get<Paginated<NoteTopic>>("/tags"',
    ],
  ],
};

function readNorm(file) {
  return fs.readFileSync(file, "utf8").replace(/\r\n/g, "\n");
}

// 剥注释（`//` 行注释 + `/* */` 块注释），**不动字符串字面量** —— 与
// `test_page_optimizations.py::_code_only` 同一状态机：反向断言（X not in code）
// 不剪注释就会被自己的迁移说明命中（DEF-057 同型）。
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
      while (index < length && !(source[index] === "*" && source[index + 1] === "/")) index += 1;
      index += 2;
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

const sources = { api: readNorm(FILES.api), library: readNorm(FILES.library) };
if (MUTATE) {
  const edits = MUTATIONS[MUTATE];
  if (!edits) {
    console.error(`FATAL 未知变异：${MUTATE}`);
    process.exit(2);
  }
  for (const [from, to] of edits) {
    if (!sources.library.includes(from) && !sources.api.includes(from)) {
      console.error(`FATAL 变异锚点未命中（实现已改？）：${from.slice(0, 60)}…`);
      process.exit(2);
    }
    sources.library = sources.library.replace(from, to);
    sources.api = sources.api.replace(from, to);
  }
}

// --- antd 桩（external；内联会让断言看到另一份模块） ---
const stubDir = fs.mkdtempSync(path.join(os.tmpdir(), "lib-topics-merge-"));
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

// --- 打包真实 api.ts（onLoad 喂**可能已变异**的源码 + 追加 http 导出） ---
const feedPlugin = {
  name: "lib-topics-merge-feed",
  setup(build) {
    build.onLoad({ filter: /lib[\\/]api\.ts$/ }, () => ({
      contents: `${sources.api}\nexport { http as __http };\n`,
      loader: "ts",
    }));
  },
};

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
  const { fetchTopics, updateSavedNoteTopics, __http } = api;
  if (typeof fetchTopics !== "function" || typeof updateSavedNoteTopics !== "function" || !__http) {
    console.error("FATAL 未导出 fetchTopics / updateSavedNoteTopics / http —— 若已重命名，请同步更新本脚本");
    process.exit(1);
  }

  // --------------------------------------------------------------------------- //
  // A 段：驱动真实 api.ts（fetchTopics / updateSavedNoteTopics 的参数契约）
  // --------------------------------------------------------------------------- //
  const captured = [];
  __http.get = (url, config) => {
    captured.push({ method: "get", url, config });
    return Promise.resolve({ data: { items: [], total: 0 } });
  };
  __http.put = (url, body) => {
    captured.push({ method: "put", url, body });
    return Promise.resolve({ data: { id: 1 } });
  };

  await fetchTopics();
  check(
    "A1 话题字典走 GET /topics",
    captured.at(-1).method === "get" && captured.at(-1).url === "/topics",
    `实际：${captured.at(-1).url}`
  );

  await updateSavedNoteTopics(7, ["露营", "装备"]);
  const put = captured.at(-1);
  check(
    "A2 话题增删走 PUT /notes/{id}/topics，载荷是话题名数组",
    put.method === "put" && put.url === "/notes/7/topics"
      && JSON.stringify(put.body) === JSON.stringify({ topics: ["露营", "装备"] }),
    `实际：${put.url} ${JSON.stringify(put.body)}`
  );

  // --------------------------------------------------------------------------- //
  // B 段：library-page.tsx 源码守卫（先剥注释 —— 迁移说明里必然提到被删的字样）
  // --------------------------------------------------------------------------- //
  const librarySource = codeOnly(sources.library);
  check(
    "B1 标题栏无标签 chips / 添加标签 Popover（整块移除）",
    !librarySource.includes("添加标签")
      && !librarySource.includes("removeTagFromNote")
      && !librarySource.includes("selectedNote?.tags"),
    "标题栏或处理函数里还有标签残留"
  );
  check(
    "B2 话题 chips 按对象渲染（key={t.id}，不是字符串）",
    librarySource.includes("key={t.id}")
      && librarySource.includes("#{t.name}"),
    "话题 chips 还在按字符串渲染"
  );
  check(
    "B3 话题区「+」是 AutoComplete（选已有 / 输新名）",
    librarySource.includes("options={addableTopics.map((t) => ({ value: t.name, label: t.name }))}")
      && librarySource.includes("const addableTopics = availableTopics.filter"),
    "话题区没有 AutoComplete 或没有 addableTopics"
  );
  check(
    "B4 筛选走 topic_id + 「全部话题」",
    librarySource.includes("topic_id: selectedTopicFilter ? Number(selectedTopicFilter) : undefined,")
      && librarySource.includes('placeholder="全部话题"')
      && !librarySource.includes("selectedTagFilter"),
    "筛选还是 tag_id / 标签文案"
  );
  check(
    "B5 卡片不渲染话题；话题只在详情弹窗（selectedNote.topics）",
    !librarySource.includes("(note.topics ?? []).slice(0, 4)")
      && !librarySource.includes("note.topics ?? []).length > 0")
      && !librarySource.includes("note.tags")
      && librarySource.includes("selectedNote.topics"),
    "卡片上又出现话题渲染，或详情弹窗的话题区没了"
  );

  // --------------------------------------------------------------------------- //
  // C 段：api.ts 源码守卫（同样剥注释）
  // --------------------------------------------------------------------------- //
  const apiSource = codeOnly(sources.api);
  check(
    "C1 api.ts 无 /tags 路由、无 batchTagNotes",
    !apiSource.includes('"/tags"') && !apiSource.includes("batchTagNotes"),
    "api.ts 还有 /tags 或 batchTagNotes 残留"
  );
  check(
    "C2 api.ts 导出 fetchTopics",
    apiSource.includes("export async function fetchTopics("),
    "没有 fetchTopics 导出"
  );

  if (failures > 0) {
    console.error(`\n${failures} 项 FAIL`);
    process.exit(1);
  }
  console.log("\n全部 PASS");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
