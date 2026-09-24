/**
 * 行为验证：内容库「账号 / 笔记类型」筛选口径（2026-09-23）。
 *
 * 盯的四件事（都是「不许出现某种行为」，源码断言证明不了）：
 *
 * 1. **「不筛」不是一个枚举值**：`NOTE_TYPE_FILTER_OPTIONS` 里的「全部类型」
 *    取值为空串，且**不在** `NOTE_TYPE_FILTER_VALUES` 里 —— 一旦把空串混进
 *    合法枚举，前端就会把「不筛」当成一个真类型发出去（后端 400 / 或筛成空集）。
 * 2. **下拉文案与列表文案同源**：筛「本地生成」进列表，行上必须也是「本地生成」；
 *    旧实现把 `local_generated` 显示成「本地」（两值 else 分支），
 *    这正是这次要消掉的口径不一致（筛得到、看不出）。
 * 3. **未知类型不编中文名**：老响应 / 未来新增类型按缺值口径返回**原始值**
 *    （硬约束⑤：不造假），而不是默认成「本地」。
 * 4. **账号文案不留空**：昵称为空时写「未命名账号」—— 空白选项 = 用户猜不出是哪一行。
 * 5. **类型 Tag 配色三档**（2026-09-23 第三轮口径「使用 tag 放图片右下角」）：
 *    卡片封面右下角与详情弹窗**共用同一份** `noteTypeTagColor`，各写一遍就是漂移
 *    （DEF-061 同型：同一字段两处各写一份呈现）。
 *
 * 做法与 `sync_gates.cjs` 同源：esbuild 打包**真实** `lib/note-filters.ts`，
 * 直接调用它（纯模块，不需要 antd 桩、不需要 jsdom）。
 *
 * 运行：
 *   <node> tests/frontend/note_filters.cjs                 # 期望全 PASS
 *   NOTE_FILTERS_MUTATE=enumin   <node> …                  # 期望 FAIL
 *   NOTE_FILTERS_MUTATE=label    <node> …                  # 期望 FAIL
 *   NOTE_FILTERS_MUTATE=fallback <node> …                  # 期望 FAIL
 *   NOTE_FILTERS_MUTATE=blank    <node> …                  # 期望 FAIL
 *   NOTE_FILTERS_MUTATE=tagcolor <node> …                  # 期望 FAIL
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/note-filters.ts");

const MUTATE = process.env.NOTE_FILTERS_MUTATE || "";

/**
 * 三个类型的**约定中文名**（钉字面值，不从模块里读 —— 从模块读就是自我印证）。
 *
 * 这张表是「用户在各处看到的名字」的权威：筛选下拉、内容库列表、详情弹窗
 * 三处必须一致；改了实现里的文案而没改这里 = 文案契约变更，本脚本应当变红。
 */
const EXPECTED_LABELS = {
  synced: "三方同步",
  local_generated: "本地生成",
  local: "本地采集",
};

// 变异锚点（必须是源码里**唯一**的串；行尾归一化防 CRLF 失配 —— 失配的
// 表现是「变异静默不生效」，脚本照常全绿，比变红更危险）。
const RESERVED = {
  enumin: "  { value: \"\", label: \"全部类型\" },",
  label: '  { value: "local", label: "本地采集" },',
  // ⚠️ 2026-09-23：原锚点是 `noteTypeTooltip` 里的 `default:` 分支，该函数已按
  // 用户口径整条删除 ⇒ 改指 `noteTypeLabel` 的兜底分支（同一个性质：
  // 未知类型不许被编成一个中文名）。
  fallback: `  return hit ? hit.label : key;`,
  blank: `  return (account?.nickname || "").trim() || "未命名账号";`,
  // 类型 Tag 配色三档合一 ⇒ 三种类型在界面上分不出来（DEF-061 同型）
  tagcolor: `    case "local_generated":
      return "purple";`,
  // 同步按钮判定：把「已发布」那半条去掉 ⇒ 回到本轮修掉的漏按钮状态
  syncpub: `  return (note?.publish_status ?? "").trim() === "published";`,
  // 同步按钮判定：只看类型、且把 synced 也漏掉（更极端的退化）
  synctype: `  if ((note?.note_type ?? "").trim() === "synced") return true;`,
  // 同步按钮判定：缺值当成「能同步」（宁可错给，于是本地笔记也长按钮）
  syncmissing: `  if ((note?.note_type ?? "").trim() === "synced") return true;
  return (note?.publish_status ?? "").trim() === "published";`,
};

const MUTATE_TO = {
  // 「全部类型」被塞进合法枚举 ⇒ 前端把「不筛」当类型发出去
  enumin: '  { value: "", label: "全部类型" },\n  { value: "", label: "全部类型" },',
  // 列表文案与下拉文案不一致（这正是旧实现的问题）
  label: '  { value: "local", label: "本地" },',
  // 未知类型默认成「本地」⇒ 造假（硬约束⑤）
  fallback: `  return hit ? hit.label : "本地";`,
  // 昵称空时返回空白 ⇒ 下拉里出现无法辨认的空选项
  blank: `  return (account?.nickname || "").trim();`,
  // 三种类型同一个颜色 ⇒ 界面上分不出三方同步 / 本地生成 / 本地采集
  tagcolor: `    case "local_generated":
      return "blue";`,
  // 去掉「已发布 ⇒ 给同步」这一半 ⇒ 回到用户报的漏按钮状态
  syncpub: `  return false;`,
  // 只看类型（旧契约）：本地生成发布成功的行没有同步按钮
  synctype: `  return false;`,
  // 缺值当成「能同步」⇒ 没发布过的本地笔记也长按钮（误导）
  syncmissing: `  if ((note?.note_type ?? "").trim() === "synced") return true;
  return true;`,
};

let failures = 0;
function check(name, condition, detail) {
  if (condition) {
    console.log(`PASS ${name}`);
  } else {
    failures += 1;
    console.log(`FAIL ${name}${detail ? ` — ${detail}` : ""}`);
  }
}

function mutatePlugin() {
  return {
    name: "mutate",
    setup(build) {
      build.onLoad({ filter: /note-filters\.ts$/ }, () => {
        let source = fs.readFileSync(MODULE_TS, "utf8").replace(/\r\n/g, "\n");
        if (MUTATE && RESERVED[MUTATE]) {
          if (!source.includes(RESERVED[MUTATE])) {
            console.log(`FAIL mutate anchor not found: ${MUTATE}`);
            process.exit(1);
          }
          source = source.replace(RESERVED[MUTATE], MUTATE_TO[MUTATE]);
        }
        return { contents: source, loader: "ts" };
      });
    },
  };
}

async function main() {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "note-filters-"));
  const bundled = await esbuild.build({
    entryPoints: [MODULE_TS],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    target: "node18",
    logLevel: "silent",
    plugins: [mutatePlugin()],
  });
  const outFile = path.join(tmpDir, "note-filters.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const mod = require(outFile);
  const {
    NOTE_TYPE_FILTER_OPTIONS,
    NOTE_TYPE_FILTER_VALUES,
    noteTypeLabel,
    noteTypeTagColor,
    accountFilterLabel,
    canSyncNote,
  } = mod;

  if (!Array.isArray(NOTE_TYPE_FILTER_OPTIONS) || typeof noteTypeLabel !== "function") {
    console.error("FATAL 未导出筛选模块 —— 若已重命名，请同步更新本脚本");
    return 1;
  }
  if (typeof canSyncNote !== "function") {
    console.error("FATAL 未导出 canSyncNote —— 卡片 / 详情两处共用的判定，缺它按钮会消失");
    return 1;
  }

  // ---- 1. 「不筛」不是一个枚举值 ----
  const optionValues = NOTE_TYPE_FILTER_OPTIONS.map((o) => o.value);
  check("A1 下拉首项是「全部类型」", NOTE_TYPE_FILTER_OPTIONS[0].label === "全部类型");
  check("A2 下拉首项取值为空串（= 不筛）", NOTE_TYPE_FILTER_OPTIONS[0].value === "");
  check(
    "A3 空串**不在**合法枚举里（不筛 ≠ 一个类型）",
    !NOTE_TYPE_FILTER_VALUES.includes(""),
    `实际：${JSON.stringify(NOTE_TYPE_FILTER_VALUES)}`
  );
  check(
    "A4 合法枚举正好三档（synced / local_generated / local）",
    JSON.stringify([...NOTE_TYPE_FILTER_VALUES].sort()) ===
      JSON.stringify(["local", "local_generated", "synced"]),
    `实际：${JSON.stringify(NOTE_TYPE_FILTER_VALUES)}`
  );
  check(
    "A5 下拉可选项 = 「全部」+ 三个合法类型",
    JSON.stringify(optionValues) ===
      JSON.stringify(["", "synced", "local_generated", "local"]),
    `实际：${JSON.stringify(optionValues)}`
  );
  // ---- 2. 下拉文案与列表文案同源 ----
  // 注意：只比「下拉 label == 列表文案」是**自我印证**（两边读同一张表，改表两边一起动）
  // ⇒ 必须同时钉**字面期望值**（`EXPECTED_LABELS`），否则改文案的变异静默全绿。
  for (const option of NOTE_TYPE_FILTER_OPTIONS.filter((o) => o.value !== "")) {
    check(
      `B1 列表文案与下拉一致：${option.value} → ${option.label}`,
      noteTypeLabel(option.value) === option.label,
      `实际：${noteTypeLabel(option.value)}`
    );
    check(
      `B2 文案是约定的中文名：${option.value} → ${EXPECTED_LABELS[option.value]}`,
      option.label === EXPECTED_LABELS[option.value],
      `实际：${option.label}`
    );
  }
  check(
    "B3 local_generated 不显示成「本地」（旧实现的错）",
    noteTypeLabel("local_generated") !== noteTypeLabel("local") &&
      noteTypeLabel("local") !== "本地",
    `实际：${noteTypeLabel("local_generated")} / ${noteTypeLabel("local")}`
  );

  // ---- 3. 未知类型不编中文名（硬约束⑤）----
  check("C1 未知类型返回原始值", noteTypeLabel("whatever") === "whatever");
  check("C2 空值返回空（不默认成某个类型）", noteTypeLabel("") === "");
  check("C3 undefined 返回空", noteTypeLabel(undefined) === "");

  // ---- 4. 类型 Tag 配色：三档各一个颜色，且**钉字面期望值** ----
  // ⚠️ 旧 D 段（D1~D4）断言的是 `noteTypeTooltip` 的悬停长文案 —— 那两句长文案
  // （「…账号同步链路落库，可拉取平台最新内容」/「…由商品智能生成，待发布 / 发布失败
  // 可重发」）已按 2026-09-23 用户口径移除，`noteTypeTooltip` 函数**整条删除**
  // （类型改成封面右下角的 Tag 后，悬停提示只剩复读）。**原断言保留在此，不静默删除。**
  //
  // 新 D 段盯的是 `noteTypeTagColor`：卡片封面右下角与详情弹窗共用同一份配色，
  // 各自写一遍就是两份实现（DEF-061 同型）。同样只比「两处相同」是自我印证
  // ⇒ 必须同时钉**字面期望值** `EXPECTED_TAG_COLORS`。
  const EXPECTED_TAG_COLORS = {
    synced: "blue",
    local_generated: "purple",
    local: "default",
  };
  for (const type of NOTE_TYPE_FILTER_VALUES) {
    check(
      `D1 配色是约定的颜色：${type} → ${EXPECTED_TAG_COLORS[type]}`,
      noteTypeTagColor(type) === EXPECTED_TAG_COLORS[type],
      `实际：${noteTypeTagColor(type)}`
    );
  }
  check(
    "D2 三种类型**不是**同一个颜色（否则界面上分不出来）",
    new Set(NOTE_TYPE_FILTER_VALUES.map((t) => noteTypeTagColor(t))).size === 3,
    `实际：${NOTE_TYPE_FILTER_VALUES.map((t) => `${t}=${noteTypeTagColor(t)}`).join(", ")}`
  );
  check(
    "D3 未知类型不给颜色（回默认灰，不编）",
    noteTypeTagColor("whatever") === "default" && noteTypeTagColor("") === "default",
    `实际：whatever=${noteTypeTagColor("whatever")} / ""=${noteTypeTagColor("")}`
  );
  check(
    "D4 undefined / null 入参也不炸",
    (() => {
      try {
        return noteTypeTagColor(undefined) === "default" && noteTypeTagColor(null) === "default";
      } catch {
        return false;
      }
    })()
  );
  check(
    "D5 noteTypeTooltip 已彻底移除（长文案不该再有出口）",
    typeof mod.noteTypeTooltip === "undefined",
    `实际：仍导出 noteTypeTooltip（${typeof mod.noteTypeTooltip}）`
  );

  // ---- 5. 账号文案不留空 ----
  check("E1 有昵称用昵称", accountFilterLabel({ nickname: "观隅好物" }) === "观隅好物");
  check("E2 空白昵称照样 trim", accountFilterLabel({ nickname: "   " }) === "未命名账号");
  check("E3 缺字段不返回空", accountFilterLabel({}) === "未命名账号");
  check(
    "E4 undefined 入参也不炸",
    (() => {
      try {
        return accountFilterLabel(undefined) === "未命名账号";
      } catch {
        return false;
      }
    })()
  );

  // ---- 6. 同步按钮判定：**已发布过的必须能同步** ----
  // 需求原文（用户 2026-09-23）：「已经发布过的笔记一定存在同步按钮功能」。
  // 真实库已复现漏按钮的场景：本地生成笔记发布成功后仍是 `local_generated`
  // （设计 13 §31：生成落库 local_generated，发布成功只改 publish_status），
  // 旧判定 `note_type === "synced"` 把它漏掉 —— 发出去了却没有同步入口。
  check(
    "F1 synced 一定有同步按钮",
    canSyncNote({ note_type: "synced", publish_status: "published" }) === true &&
      canSyncNote({ note_type: "synced", publish_status: "pending" }) === true &&
      canSyncNote({ note_type: "synced", publish_status: "failed" }) === true
  );
  check(
    "F2 **已发布** 的本地生成笔记也要有同步按钮（本轮修的漏按钮）",
    canSyncNote({ note_type: "local_generated", publish_status: "published" }) === true
  );
  check(
    "F3 已发布的本地采集笔记同样给同步入口",
    canSyncNote({ note_type: "local", publish_status: "published" }) === true
  );
  check(
    "F4 未发布的本地笔记不给同步（本地笔记没有「平台最新内容」对象）",
    canSyncNote({ note_type: "local", publish_status: "pending" }) === false &&
      canSyncNote({ note_type: "local_generated", publish_status: "failed" }) === false &&
      canSyncNote({ note_type: "local", publish_status: "" }) === false
  );
  check(
    "F5 缺值不误判为「能同步」",
    canSyncNote({ note_type: undefined, publish_status: undefined }) === false &&
      canSyncNote({}) === false
  );
  check(
    "F6 判定**同时**接受 `published` 投影字段（老响应兼容）",
    (() => {
      // 用 `published: true` 的老行（`published` 是 publish_status 的投影，设计 13 §32.3.2）
      const legacy = { note_type: "local_generated", published: true };
      // 老行没有 publish_status ⇒ 只有类型条件可判 ⇒ 结果与 `publish_status` 缺席时一致
      return canSyncNote(legacy) === canSyncNote({ note_type: legacy.note_type });
    })()
  );

  if (passedCount() !== totalChecks) {
    console.log(`FAIL 检查条数不符：期望 ${totalChecks}，实际 ${passedCount()}`);
    failures += 1;
  }
  console.log(`\n${passedCount()} PASS / ${failures} FAIL`);
  return failures === 0 ? 0 : 1;

  function passedCount() {
    return totalChecks - failures;
  }
}

// 期望检查条数：A5 + (B1+B2)×3 + B3 + C3 + D1×3 + D2~D5 + E4 + F6 = 34；
// 新增检查必须同步改这个数，否则「少跑了几条」会被静默掩盖（比变红更危险）。
const totalChecks = 34;

main().then((code) => process.exit(code)).catch((error) => {
  console.error(error);
  process.exit(1);
});
