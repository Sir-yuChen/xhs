/**
 * 行为验证：发布门禁与发布态（设计 13 §31 门禁 + §32.3 状态唯一字段）。
 *
 * 盯的六件事（都是「不许出现某种行为」，源码断言证明不了）：
 *
 * 1. **可发布判据只有一条**：`local_generated` + `pending`/`failed` ——
 *    采集 / 同步笔记不许出现发布入口（它们本来就是平台已存在或别人的内容）；
 * 2. **已发布不许重发**（后端 `NOTE_ALREADY_PUBLISHED` 的同款前端侧）——
 *    重复发布是验收硬指标「重复发布 0 次」的用户侧入口；
 * 3. **批量跳过必须明示**：选 5 篇只有 2 篇可发 ⇒ targets=2 / skipped=3，
 *    静默丢 3 篇 = 用户以为都发了；
 * 4. **状态只有一个来源**（§32.3.4）：`publish_status` 翻译成**一张**标签 ——
 *    卡片上不许再并排「已发布/待发布」开关 + 「发布失败」tag 两个可能矛盾的状态；
 *    空值（迁移前的历史形态）按「待发布」显示，因为迁移后每篇笔记都有状态，
 *    空值不再是「不适用」（旧口径「空 → 返回 null 不渲染」已被 §32.3 取代）；
 * 5. **失败态必须被识别**：`failed` 时标签要打上「附完整失败原因 Tooltip」的标记 ——
 *    识别不出失败态 = 用户看不到失败原因（问题③的直接回归）；
 * 6. **顺序保持**：批量目标按传入顺序（顺序处理，限流由平台节奏承担）。
 *
 * 做法与 `sync_gates.cjs` 同源：esbuild 打包**真实** `lib/note-publish-gates.ts`，
 * 直接调用它（纯模块，不需要 antd 桩、不需要 jsdom）。
 *
 * 运行：
 *   <node> tests/frontend/note_publish.cjs                    # 期望全 PASS
 *   NOTE_PUBLISH_MUTATE=gate    <node> …                      # 期望 FAIL
 *   NOTE_PUBLISH_MUTATE=repub   <node> …                      # 期望 FAIL
 *   NOTE_PUBLISH_MUTATE=skip    <node> …                      # 期望 FAIL
 *   NOTE_PUBLISH_MUTATE=label   <node> …                      # 期望 FAIL
 *   NOTE_PUBLISH_MUTATE=order   <node> …                      # 期望 FAIL
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/note-publish-gates.ts");

const MUTATE = process.env.NOTE_PUBLISH_MUTATE || "";

// 变异锚点（必须是源码里**唯一**的串；行尾归一化防 CRLF 失配 —— 失配的
// 表现是「变异静默不生效」，脚本照常全绿，比变红更危险）。
// ⚠️ `label` 锚的是 `publishStatusTag` 里判定失败态的那一行（§32.3.4）：
// 把失败态抹掉 ⇒ 卡片不再挂失败原因 Tooltip ⇒ 问题③直接回归。
// （旧锚点指向已被 §32.3 删除的 `publishStatusLabel`，锚不到会 exit(1) ——
//  而变异运行本来就期望失败，所以断裂会被永久掩盖，这正是要盯的失效模式。）
const RESERVED = {
  gate: '  return noteType === "local_generated" && PUBLISHABLE_STATUSES.includes(publishStatus as never);',
  repub: 'export const PUBLISHABLE_STATUSES = ["pending", "failed"] as const;',
  skip: "      skipped += 1;",
  label: '    failed: status === "failed",',
  order: "    targets.push(note);",
};

const MUTATE_TO = {
  // 所有类型都可发布 ⇒ 采集/同步笔记也出现发布入口（必然 400）
  gate: "  return true;",
  // 已发布也可「重发」⇒ 重复发布硬指标失守
  repub: 'export const PUBLISHABLE_STATUSES = ["pending", "failed", "published"] as const;',
  // 跳过不计数 ⇒ 确认框少说「跳过 3 篇」，用户以为都发了
  skip: "      skipped += 0;",
  // 失败态识别不出来 ⇒ 失败原因 Tooltip 不挂，用户只看到「发布失败」看不到为什么
  label: "    failed: false,",
  // 目标顺序反转 ⇒ 顺序处理变成逆序（限流节奏与失败定位都对不上）
  order: "    targets.unshift(note);",
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
      build.onLoad({ filter: /note-publish-gates\.ts$/ }, () => {
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
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "note-publish-"));
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
  const outFile = path.join(tmpDir, "note-publish-gates.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const mod = require(outFile);
  const { canPublish, publishStatusTag, nextManualStatus, splitPublishTargets } = mod;

  // ---- 1. 可发布判据 ----
  check("pending 可发布", canPublish("local_generated", "pending") === true);
  check("failed 可重发", canPublish("local_generated", "failed") === true);
  check("synced 不可发布", canPublish("synced", "pending") === false);
  check("local 不可发布", canPublish("local", "pending") === false);
  check("已发布不可重发", canPublish("local_generated", "published") === false);
  check("发布中不可再发", canPublish("local_generated", "publishing") === false);
  check("空状态不适用", canPublish("local_generated", "") === false);
  check("空状态不适用(undefined)", canPublish("synced", undefined) === false);

  // ---- 2. 状态标签：唯一来源 + 失败态可识别（§32.3.4）----
  const tagPending = publishStatusTag("local_generated", "pending");
  check("标签 待发布", tagPending.text === "待发布");
  check("标签 发布失败", publishStatusTag("local_generated", "failed").text === "发布失败");
  check("标签 已发布", publishStatusTag("synced", "published").text === "已发布");
  check("标签 发布中", publishStatusTag("synced", "publishing").text === "发布中");
  // 空值 = 迁移前的历史形态 ⇒ 按「待发布」显示（旧口径「空 → null 不渲染」已被 §32.3 取代）
  check("标签 空值按待发布", publishStatusTag("synced", "").text === "待发布");
  check("标签 未知值按待发布", publishStatusTag("synced", "whatever").text === "待发布");
  // 失败态必须被标记（卡片要挂完整失败原因 Tooltip）—— 这条是问题③的守门
  check("失败态被标记", publishStatusTag("local_generated", "failed").failed === true);
  check("非失败态不标记", tagPending.failed === false);
  // 本地生成的状态由发布链路管理 ⇒ 不给手动切换入口；其余类型可切
  check("本地生成不可手切", tagPending.toggleable === false);
  check("同步笔记可手切", publishStatusTag("synced", "pending").toggleable === true);

  // ---- 2b. 手动切换目标（开关语义：已发布 ⇄ 待发布）----
  check("已发布 → 待发布", nextManualStatus("published") === "pending");
  check("待发布 → 已发布", nextManualStatus("pending") === "published");
  check("失败 → 已发布", nextManualStatus("failed") === "published");
  check("空值 → 已发布", nextManualStatus("") === "published");

  // ---- 3. 批量拆分（跳过明示 + 顺序保持）----
  const notes = [
    { id: 1, note_type: "local_generated", publish_status: "pending" },
    { id: 2, note_type: "synced", publish_status: "" },
    { id: 3, note_type: "local_generated", publish_status: "failed" },
    { id: 4, note_type: "local_generated", publish_status: "published" },
    { id: 5, note_type: "local", publish_status: "" },
  ];
  const { targets, skipped } = splitPublishTargets(notes, [1, 2, 3, 4, 5]);
  check("批量目标=2", targets.length === 2, `got ${targets.length}`);
  check("批量跳过=3", skipped === 3, `got ${skipped}`);
  check(
    "批量顺序保持",
    targets.map((t) => t.id).join(",") === "1,3",
    `got ${targets.map((t) => t.id).join(",")}`
  );

  // 选中里有不在当前列表的（翻页后勾选跨页）⇒ 计入跳过，不静默
  const cross = splitPublishTargets(notes, [1, 99]);
  check("跨页勾选计入跳过", cross.targets.length === 1 && cross.skipped === 1);

  // 全不可发布 ⇒ targets 空（调用方给「没有可发布」提示，不发请求）
  const none = splitPublishTargets(notes, [2, 4, 5]);
  check("全不可发布 targets 空", none.targets.length === 0 && none.skipped === 3);

  if (failures > 0) {
    console.log(`\n${failures} FAILED${MUTATE ? ` (mutate=${MUTATE})` : ""}`);
    process.exit(1);
  }
  console.log(`\nALL PASS${MUTATE ? ` (mutate=${MUTATE})` : ""}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
