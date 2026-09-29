/**
 * 行为验证：队列记录页（设计 13 §35.11）的**纯逻辑**。
 *
 * ⚠️ **批次形态（2026-09-26 两轮口径，后者覆盖前者）**：早间「不需要队列批次，
 * 队列是常驻，只需要队列详情记录即可」把批次层删了；当日晚间「**将队列执行详情表
 * 使用批次+详情父子树风格展示，批次逻辑不再移除，生成唯一批次号**」把它请回来 ——
 * 形态是**一次入队动作 = 一个唯一批次号**（批次行 = 父节点，队列项 = 子节点）。
 *
 * 盯的十五件事（都是「错了用户会看错数据」，源码结构断言证明不了）：
 *
 * 1. **类型中文名不许猜**：白名单外 / 没见过的类型**原样显示** —— 造一个好看的
 *   名字会让用户把没见过的类型当成已知类型排查（DEF-057 同型：把未知说成已知）；
 * 2. **可取消 / 可重试的判据必须与后端一致**（`tasks.py` 的条件更新只动
 *    `pending` / `running`；retry 只认 `failed` / `exhausted`）—— 前端多给一个
 *    按钮，用户点下去只会拿到「原样返回」的静默无操作；
 * 3. **缺值渲染 `-` 不渲染 0**：没跑完 / 没开始的计时不是 0 毫秒（硬约束⑤）；
 * 4. **失败摘要必须带真实原因**（同步失败不落库到笔记行，`payload.reason` 是
 *    唯一一次给原因的地方，吞了用户就失去自救能力 —— DEF-058 同型）；
 * 5. **商品生成队列的主体口径**（§31.10）：生成前笔记不存在 ⇒ 显示商品名
 *    （`item_name` 优先、退 `item_id`、再退 `-`），终态后的「生成了哪篇笔记」
 *    从 `note_title` / `note_id` 取 —— 判据落在 payload 上，不回头拉笔记；
 * 6. **来源按钮级固定值**（2026-09-26 用户口径「来源列不能是未知」+「来源不能是
 *    简单的手动或自动，应具体到按钮」）：九个按钮级入口各有中文名，空值 / 遗留
 *    `manual` 归「页面按钮（早期数据）」，**不许出现「未知」**；
 * 7. **可清理判据**（2026-09-26 用户口径「清理逻辑不包含待重试和进行中状态的
 *    记录，前端不允许被选中」）：只有 `done` / `cancelled` 可清；
 * 8. **执行详情多行文案**：主体 + 结果（失败带真实原因）；
 * 9. **批次父子树**（2026-09-26 晚间口径）：批次行在页面白名单里、「这一行是不是
 *    批次」的判据是 `batch_no` 有值（**数据判据**，不是类型清单）、队列项永不被
 *    当成父节点渲染；
 * 10. **队列归属反查**：批次行也要能落回它的队列（否则按队列筛选时父行消失，
 *    父子树只剩明细）；
 * 11. **批次摘要取后端 `summary`**（`batch_rollup` 唯一实现）：前端不重算 ——
 *    重算就是第二份判据（DEF-044），空批次不硬凑「成功 0 · 失败 0」；
 * 12. **清理的两个维度**（2026-09-26 用户口径「支持按照批次清理记录，批次中存在正在
 *    执行或待执行得记录，则不支持该批次全部清理……分为两个维度，批次维度和批次内的
 *    操作」）：**批次维度**是**原子动作** —— 批内有「正在执行 / 待执行」或「待重试」
 *    的明细就不许整批清（`batchCleanBlockReason` 给出原因，与后端
 *    `batch_clean_blockers` 同判据）；**批次内维度**是单条清；
 * 13. **清理结果分层报账**（`cleanOutcomeText`）：清了几条（批次几个 / 明细几条）、
 *    多少**被挡**（批内有活 / 待重试）、多少**状态不可清被跳过** —— 三种都要说，
 *    只说「已清理 N 条」会让用户以为剩下的也清掉了；
 * 14. **账号列**（2026-09-26 用户口径「批次记录执行的账号」）：明细行取自己
 *    `payload.account_id`（入队快照），批次行取后端**按子行派生**的 `accounts` ——
 *    一次批量可以跨账号勾选 ⇒ 批次级**不存在「唯一账号」**，多账号必须显
 *    「N 个账号」+ Tooltip 列清是哪几个（硬报第一个账号 = 报错）；账号名缺失退
 *    `账号 #id`（与 `笔记 #pk` 同型的可辨认兜底），id 都没有才 `-`。
 * 15. **批次行筛选**（2026-09-26 用户口径「将队列执行记录表中的状态筛选放到批次内，
 *    父子树表不使用同一个表头，在表头状态列加筛选条件」）：队列 + 状态两个条件**只
 *    作用于顶级（批次）行** —— 命中批次 ⇒ 它的一整批明细原样展示，明细行**从不**被
 *    单独筛掉（否则「共 N 条明细」会被筛成「只显示 1 条」，与后端 `summary` 的计数
 *    对不上）。这是「父子树不使用同一个表头」的落地：筛选是**批次（父）行**的条件。
 *
 * 做法与 `sync_gates.cjs` 同源：esbuild 打包**真实** `lib/task-records.ts` 直接调
 * （纯模块，不需要 antd 桩、不需要 jsdom）。
 *
 * 运行：
 *   <node> tests/frontend/task_records.cjs              # 期望全 PASS
 *   TASK_RECORDS_MUTATE=typelabel <node> …              # 期望 FAIL
 *   TASK_RECORDS_MUTATE=group    <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=cancel   <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=retry    <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=duration <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=outcome  <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=genitem  <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=source   <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=subject  <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=clean    <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=batchtypes <node> …             # 期望 FAIL
 *   TASK_RECORDS_MUTATE=batchrow  <node> …              # 期望 FAIL
 *   TASK_RECORDS_MUTATE=queuekey  <node> …              # 期望 FAIL
 *   TASK_RECORDS_MUTATE=queuelabel <node> …            # 期望 FAIL
 *   TASK_RECORDS_MUTATE=batchdetail <node> …            # 期望 FAIL
 *   TASK_RECORDS_MUTATE=batchblock <node> …             # 期望 FAIL
 *   TASK_RECORDS_MUTATE=cleanreport <node> …            # 期望 FAIL
 *   TASK_RECORDS_MUTATE=account  <node> …               # 期望 FAIL
 *   TASK_RECORDS_MUTATE=accountname <node> …            # 期望 FAIL
 *   TASK_RECORDS_MUTATE=statusdeep <node> …             # 期望 FAIL
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/task-records.ts");

const MUTATE = process.env.TASK_RECORDS_MUTATE || "";

// 变异锚点（源码里**唯一**的串；行尾归一化防 CRLF 失配）
const RESERVED = {
  typelabel: "  return TASK_TYPE_LABELS[taskType] || taskType || \"未知类型\";",
  group: "  if (taskType.startsWith(\"xhs_note_sync\") || taskType === \"xhs_shop_goods_sync\") return \"sync\";",
  cancel: "  return (TASK_OPEN_STATUSES as readonly string[]).includes(status);",
  retry: "  return (TASK_RETRYABLE_STATUSES as readonly string[]).includes(status);",
  duration: "  if (durationMs === null || durationMs === undefined) return \"-\";",
  outcome: "  return reason || code || \"失败（平台未给原因）\";",
  source: "  return TASK_SOURCE_LABELS[key] || key;",
  genitem: "  const name = String(payload?.item_name ?? \"\").trim();",
  // 可清理判据被抹成「都能清」⇒ 待重试 / 进行中的行也能被选中清理（丢任务）
  clean: "  return (CLEANABLE_STATUSES as readonly string[]).includes(status);",
  // 队列项主体快照被抹掉 ⇒ 同步/发布在执行详情里只剩「笔记 #123」
  subject: "  const title = String(payload.note_title ?? \"\").trim();",
  // 页面白名单掉回「只有队列项」⇒ 批次父行被过滤掉，父子树只剩明细
  batchtypes: "  return QUEUE_TASK_TYPES.includes(taskType);",
  // 批次判据恒 false ⇒ 批次行被当成普通行渲染，父子树塌成平铺
  batchrow: "  return Boolean(String(row?.batch_no ?? \"\").trim());",
  // 归属反查只认队列项 ⇒ 按队列筛选时批次父行消失（明细成了孤儿）
  queuekey: "    if (queue.item_task_type === taskType || queue.batch_task_type === taskType) {",
  // 类型列改显示队列 **key**（英文）⇒ 用户看到 `note_sync`，2026-09-26 口径的中文名落空
  queuelabel: "  return hit?.label || taskTypeLabel(taskType);",
  // 总数只算成功+失败 ⇒ 进行中 / 待处理 / 取消的明细被漏掉（总数对不上）
  batchdetail: "  const total = Number(summary.total) || 0;",
  // 批次维度阻塞判据被抹成「永远可清」⇒ 批内还有在跑 / 待跑的明细也显示可整批清理
  batchblock: "  if (!parts.length) return null;",
  // 清理报账吞掉「有批次被挡」⇒ 用户以为剩下的都清掉了（实际一条没动）
  cleanreport: "    ? `；${blockedBatches} 个批次因明细还在执行中 / 待重试，整批未清理`",
  // 跨账号批次被抹成「就报第一个账号」⇒ 页面上写着 1 个账号、明细却有 3 个（说错）
  account: "    return `${ids.length} 个账号`;",
  // 账号映射退化成只报 id ⇒ 「账号」列全是 `账号 #1`，用户认不出是哪个账号
  accountname: "    map[String(id)] = name || `账号 #${id}`;",
  // 明细行也被同一个状态条件筛掉 ⇒ 「共 N 条」被筛成「只显示 1 条」（与 summary 对不上）
  statusdeep: "    return queueMatched && statusMatched;",
};

const MUTATE_TO = {
  // 未知类型也造一个名字 ⇒ 用户把没见过的类型当成已知类型排查
  typelabel: "  return TASK_TYPE_LABELS[taskType] || \"未知任务\";",
  // 店铺商品同步掉出 sync 组 ⇒ 筛选「同步」时它消失
  group: "  if (taskType.startsWith(\"xhs_note_sync\")) return \"sync\";",
  // done 也可取消 ⇒ 前端多一个无效按钮（后端条件更新会拒绝）
  cancel: "  return status !== \"done\";",
  // cancelled 也可重试 ⇒ 用户点了拿到静默无操作
  retry: "  return status !== \"done\";",
  // 缺值渲染 0ms ⇒ 把「不知道」说成「0 毫秒」
  duration: "  if (durationMs === null || durationMs === undefined) return \"0ms\";",
  // 摘要吞掉真实原因 ⇒ 失败只剩一句「失败」
  outcome: "  return \"失败\";",
  // 全部来源显示成同一个值 ⇒ 按钮级消歧失效
  source: "  return \"手动\";",
  // 商品名优先级被抹掉 ⇒ 生成队列详情抽屉只显示商品 ID（有名字也不显示）
  genitem: "  const name = \"\";",
  // 一律可清 ⇒ 待重试 / 进行中的记录也能被选中删除（用户失去重试入口 / 丢任务）
  clean: "  return true;",
  // 主体恒空 ⇒ 执行详情第一行退化
  subject: "  const title = \"\";",
  // 白名单只留队列项 ⇒ 批次父行被 isQueueTaskType 过滤掉
  batchtypes: "  return QUEUE_ITEM_TASK_TYPES.includes(taskType);",
  // 永不认批次行 ⇒ 批次行按普通行渲染（无批次号列 / 无派生状态）
  batchrow: "  return false;",
  // 归属反查只认队列项 ⇒ 批次父行落不回队列（筛选时消失）
  queuekey: "    if (queue.item_task_type === taskType) {",
  // 队列中文名被换成 key（英文）⇒ 「类型」列又变回内部代码，用户得自己翻译
  queuelabel: "  return hit?.key || taskTypeLabel(taskType);",
  // 总数只算成功 + 失败 ⇒ 进行中 / 待处理 / 取消的明细被漏掉（总数对不上）
  batchdetail: "  const total = (Number(summary.done) || 0) + (Number(summary.failed) || 0);",
  // 永远说「可以整批清理」⇒ 批内有活 / 待重试明细时也放行（丢任务 / 丢重试入口）
  batchblock: "  return null;",
  // 被挡下的批次一声不吭 ⇒ 用户把「没清掉」当成「清干净了」
  cleanreport: "    ? \"\"",
  // 跨账号批次只报第一个账号 ⇒ 「1 个账号」与明细里的 3 个账号对不上
  account: "    return names[String(ids[0])] || `账号 #${ids[0]}`;",
  // 账号名一律退成 `账号 #id` ⇒ 用户在「账号」列认不出是哪个账号（id 不是给人看的）
  accountname: "    map[String(id)] = `账号 #${id}`;",
  // 明细行也被状态条件切一遍（父子共用同一个筛选条件）⇒ 计数与后端 summary 对不上
  statusdeep:
    "    if (!(queueMatched && statusMatched)) return false;\n" +
    "    const kids = (row as { children?: { status?: string }[] }).children;\n" +
    '    if (Array.isArray(kids) && status !== "all") {\n' +
    "      (row as { children?: unknown }).children = kids.filter((child) => child.status === status);\n" +
    "    }\n" +
    "    return true;",
};

const mutatePlugin = {
  name: "task-records-mutate",
  setup(build) {
    build.onLoad({ filter: /lib[\\/]task-records\.ts$/ }, (args) => {
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

async function main() {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "task-records-"));
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
  const outFile = path.join(tmpDir, "task-records.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const mod = require(outFile);
  const {
    taskTypeLabel,
    taskTypeGroup,
    taskStatusLabel,
    taskStatusColor,
    canCancelTask,
    canRetryTask,
    canCleanTask,
    isQueueItemTask,
    isQueueBatchTask,
    isQueueTaskType,
    isBatchRow,
    queueKeyForTask,
    queueLabelForTask,
    isProductGenItem,
    genItemProductText,
    genItemNoteText,
    formatDuration,
    itemOutcomeText,
    hasChildren,
    taskSourceLabel,
    taskSourceTip,
    executionDetailLines,
    batchExecutionDetailLines,
    batchCleanBlockReason,
    filterBatchRows,
    cleanOutcomeText,
    accountNameMap,
    batchAccountIds,
    taskAccountText,
    taskAccountTip,
    TASK_TYPE_LABELS,
    QUEUE_TASK_TYPES,
    QUEUE_BATCH_TASK_TYPES,
  } = mod;

  if (typeof taskTypeLabel !== "function") {
    console.error("FATAL 未导出 taskTypeLabel —— 若已重命名，请同步更新本脚本");
    return 1;
  }

  // A. 类型中文名：白名单内映射对、**表外原样**、空串有兜底
  check("A1 队列项类型有中文名", taskTypeLabel("xhs_note_sync_item"), "同步笔记");
  check("A2 队列项类型有中文名（发布）", taskTypeLabel("xhs_note_publish_item"), "发布笔记");
  check("A3 账号级同步行有中文名", taskTypeLabel("xhs_note_sync"), "账号全量同步");
  check("A4 表外类型**原样**显示（不造名）", taskTypeLabel("some_new_type"), "some_new_type");
  check("A5 空串兜底", taskTypeLabel(""), "未知类型");
  check(
    "A6 白名单覆盖清理任务认的全部类型",
    [
      "xhs_note_sync_item",
      "xhs_note_publish_item",
      "xhs_note_sync",
    ].every((t) => typeof TASK_TYPE_LABELS[t] === "string" && TASK_TYPE_LABELS[t].length > 0),
    true
  );
  // A7-A10 队列白名单与批次父行（§31.10 商品生成 + 2026-09-26 批次层）
  check("A7 生成队列项有中文名", taskTypeLabel("xhs_note_gen_item"), "生成商品笔记");
  // ⚠️ 2026-09-27 §35.18：详情补齐队列（`xhs_note_hydrate_item` / `xhs_note_hydrate_batch`）
  // 随「补齐详情」按钮一并移除 ⇒ 白名单只剩三个队列（各含项 + 批次父行）。
  check(
    "A8 队列白名单收满队列项 + 批次父行（父子树的两级都在）",
    QUEUE_TASK_TYPES.slice().sort(),
    [
      "xhs_note_gen_batch",
      "xhs_note_gen_item",
      "xhs_note_publish_batch",
      "xhs_note_publish_item",
      "xhs_note_sync_batch",
      "xhs_note_sync_item",
    ]
  );
  check("A9 批次父行有中文名（同步）", taskTypeLabel("xhs_note_sync_batch"), "同步批次");
  check(
    "A10 批次类型都有中文名（不许露出生代码）",
    QUEUE_BATCH_TASK_TYPES.every((t) => String(TASK_TYPE_LABELS[t] ?? "").length > 0),
    true
  );

  // B. 分组（筛选用）
  check("B1 同步组含队列项与账号级", taskTypeGroup("xhs_note_sync_item"), "sync");
  check("B2 同步组含店铺商品同步", taskTypeGroup("xhs_shop_goods_sync"), "sync");
  check("B3 发布组含队列项与定时发布", taskTypeGroup("xhs_note_publish_item"), "publish");
  check("B4 发布组含定时发布", taskTypeGroup("creator_publish_scheduler"), "publish");
  check("B5 AI 组", taskTypeGroup("ai_generate_note"), "ai");
  check("B6 抓取/监控组", taskTypeGroup("monitoring_crawl"), "crawl");
  check("B7 表外类型归 other", taskTypeGroup("mystery"), "other");
  check("B8 生成组归 AI（内容出自平台官方 AI）", taskTypeGroup("xhs_note_gen_item"), "ai");

  // C. 状态映射
  check("C1 状态中文名", taskStatusLabel("running"), "进行中");
  check("C2 状态颜色（done=success）", taskStatusColor("done"), "success");
  check("C3 状态颜色（failed=error）", taskStatusColor("failed"), "error");
  check("C4 状态颜色（cancelled=warning）", taskStatusColor("cancelled"), "warning");
  check("C5 未知状态原样", taskStatusLabel("weird"), "weird");
  check("C6 未知状态颜色 default", taskStatusColor("weird"), "default");

  // D. 可取消 / 可重试（必须与后端 `tasks.py` 的条件更新一致）
  check("D1 pending 可取消", canCancelTask("pending"), true);
  check("D2 running 可取消", canCancelTask("running"), true);
  check("D3 done 不可取消", canCancelTask("done"), false);
  check("D4 failed 不可取消", canCancelTask("failed"), false);
  check("D5 cancelled 不可取消", canCancelTask("cancelled"), false);
  check("D6 failed 可重试", canRetryTask("failed"), true);
  check("D7 exhausted 可重试", canRetryTask("exhausted"), true);
  check("D8 done 不可重试", canRetryTask("done"), false);
  check("D9 cancelled 不可重试", canRetryTask("cancelled"), false);
  check("D10 pending 不可重试（在跑的重试什么）", canRetryTask("pending"), false);

  // D'. 可清理判据（2026-09-26 用户口径「清理逻辑不包含待重试和进行中状态的记录，
  //     前端不允许被选中」）：只有 done / cancelled 可清；待重试（failed /
  //     exhausted）与进行中（pending / running）一律不可清 —— 删了就是夺用户的
  //     重试入口 / 丢任务。
  check("D11 done 可清理", canCleanTask("done"), true);
  check("D12 cancelled 可清理", canCleanTask("cancelled"), true);
  check("D13 failed（待重试）不可清理", canCleanTask("failed"), false);
  check("D14 exhausted（待重试）不可清理", canCleanTask("exhausted"), false);
  check("D15 pending（进行中）不可清理", canCleanTask("pending"), false);
  check("D16 running（进行中）不可清理", canCleanTask("running"), false);
  check("D17 表外状态不可清（不猜）", canCleanTask("weird"), false);

  // E. 缺值口径与格式
  check("E1 缺值渲染 -", formatDuration(null), "-");
  check("E2 undefined 也渲染 -", formatDuration(undefined), "-");
  check("E3 负值渲染 -（时钟回拨不是 -5s）", formatDuration(-100), "-");
  check("E4 亚秒按 ms", formatDuration(420), "420ms");
  check("E5 秒级一位小数", formatDuration(1200), "1.2s");
  check("E6 分钟补零", formatDuration(185000), "3m 05s");
  check("E7 小时格式", formatDuration(8040000), "2h 14m");

  // F. 结果摘要（失败必须带真实原因）
  check("F1 成功摘要", itemOutcomeText({ ok: true }), "成功");
  check("F2 失败带原因 + code", itemOutcomeText({ reason: "网页会话失效", code: "WEB_SESSION_REQUIRED" }), "网页会话失效（WEB_SESSION_REQUIRED）");
  check("F3 只有原因", itemOutcomeText({ reason: "平台限流" }), "平台限流");
  check("F4 只有 code", itemOutcomeText({ code: "RATE_LIMITED" }), "RATE_LIMITED");
  check("F5 都没有也有兜底（不渲染空行）", itemOutcomeText({}), "失败（平台未给原因）");

  // G. 结构判据（队列项 vs 批次父行）
  check("G1 队列项是最细粒度", isQueueItemTask("xhs_note_sync_item"), true);
  check("G2 批次行不是队列项", isQueueItemTask("xhs_note_sync_batch"), false);
  check("G3 批次行有子行 ⇒ 展示子任务数", hasChildren("xhs_note_sync_batch", [{ id: 1 }]), true);
  check("G4 队列项没有子行概念", hasChildren("xhs_note_sync_item", [{ id: 1 }]), false);
  check("G5 空 children 不算有", hasChildren("xhs_note_sync_batch", []), false);
  check("G6 生成队列项也是最细粒度", isQueueItemTask("xhs_note_gen_item"), true);
  check("G7 生成批次行不是队列项", isQueueItemTask("xhs_note_gen_batch"), false);
  check("G8 生成项主体是商品", isProductGenItem("xhs_note_gen_item"), true);
  check("G9 同步项主体不是商品", isProductGenItem("xhs_note_sync_item"), false);
  check("G10 发布项主体不是商品", isProductGenItem("xhs_note_publish_item"), false);

  // I. 商品生成队列的主体口径（§31.10：生成前笔记不存在 ⇒ 商品名 + 终态后笔记标题）
  check("I1 商品名优先（入队时台账快照）", genItemProductText({ item_id: "ITEM-1", item_name: "测试桌布" }), "测试桌布");
  check("I2 缺商品名退 item_id", genItemProductText({ item_id: "ITEM-1" }), "ITEM-1");
  check("I3 都没有渲染 -（缺值口径）", genItemProductText({}), "-");
  check("I4 payload 缺失也渲染 -", genItemProductText(null), "-");
  check("I5 终态后显示生成的笔记标题", genItemNoteText({ note_title: "测试桌布真好用", note_id: "local_abc" }), "测试桌布真好用");
  check("I6 缺标题退 note_id", genItemNoteText({ note_id: "local_abc" }), "local_abc");
  check("I7 都没写返回空串（调用方不渲染那一行）", genItemNoteText({}), "");

  // H. 来源（2026-09-25 用户口径「显示来源，使用 tip 展示说明」）：
  //    定时任务触发时同一次执行在「任务中心 · 任务日志」里还有一条调度记录 ——
  //    不显示来源，用户会以为系统在偷偷跑。
  //    2026-09-26 用户两次口径：「来源列不能是未知，排查所有来源后给每个来源固定值」+
  //    「来源不能是简单的手动或自动，应具体到按钮」⇒ 九个按钮级入口九个固定中文名；
  //    空值 / 遗留 `manual`（迁移 d3e4f5a6b7c8 回填的存量行）归「页面按钮（早期数据）」。
  check("H1 内容库单条同步按钮", taskSourceLabel("note_sync_single"), "内容库-同步按钮");
  check("H2 内容库批量同步按钮", taskSourceLabel("note_sync_batch"), "内容库-批量同步按钮");
  check("H3 内容库单条发布按钮", taskSourceLabel("note_publish_single"), "内容库-发布按钮");
  check("H4 内容库批量发布按钮", taskSourceLabel("note_publish_batch"), "内容库-批量发布按钮");
  check("H5 商品列表生成笔记按钮", taskSourceLabel("product_note_generate_single"), "商品列表-生成笔记按钮");
  check(
    "H6 商品列表批量生成笔记按钮",
    taskSourceLabel("product_note_generate_batch"),
    "商品列表-批量生成笔记按钮"
  );
  check("H7 重试按钮", taskSourceLabel("retry"), "队列记录-重试按钮");
  check("H8 恢复来源", taskSourceLabel("recovery"), "应用重启恢复");
  check("H9 定时来源", taskSourceLabel("scheduled"), "定时任务");
  check("H10 遗留 manual 归早期数据（不冒充具体按钮）", taskSourceLabel("manual"), "页面按钮（早期数据）");
  check("H11 空来源兜底也是早期数据不是未知", taskSourceLabel(""), "页面按钮（早期数据）");
  check("H12 null 来源兜底同上", taskSourceLabel(null), "页面按钮（早期数据）");
  check("H13 表外来源原样（不造名）", taskSourceLabel("mystery"), "mystery");
  check("H14 来源里不许出现「未知」", /未知/.test(JSON.stringify([
    taskSourceLabel(""), taskSourceLabel(null), taskSourceLabel("mystery"),
  ])), false);
  check("H15 单条同步说明讲清「单条」", /单条/.test(taskSourceTip("note_sync_single")), true);
  check("H16 生成说明讲清「商品列表」", /商品列表/.test(taskSourceTip("product_note_generate_single")), true);
  check(
    "H17 定时说明必须提到任务日志（消歧）",
    /任务中心 · 任务日志/.test(taskSourceTip("scheduled")),
    true
  );
  check("H18 重试说明讲清「这一跑来自重试」", /这一跑来自重试/.test(taskSourceTip("retry")), true);
  check("H19 恢复说明讲清「重启续跑」", /重启/.test(taskSourceTip("recovery")), true);
  check("H20 早期数据说明讲清「不记录具体按钮」", /早期数据/.test(taskSourceTip("manual")), true);
  check("H21 空来源也有说明（不返回空串）", taskSourceTip("").length > 0, true);
  check("H22 表外来源说明带原值", taskSourceTip("mystery").includes("mystery"), true);

  // J. **批次父子树**（2026-09-26 晚间用户口径「批次+详情父子树风格展示，批次逻辑
  //    不再移除，生成唯一批次号」）：一次入队动作 = 一个唯一批次号；批次行是**父
  //    节点**（展开出逐条明细），队列项是**子节点**（最细粒度，永不被当成父节点）。
  //    ⚠️「这一行是不是批次」必须走**数据判据** `batch_no` 有值，不是写死类型清单 ——
  //    新队列上线后端自动发号，前端零改动；明细行不携带批次号 ⇒ 判 false
  //    （2026-09-26 用户口径「批次号 / 记录列只展示批次行」：「批次号」与「批次创建
  //    时间」两列只对父节点渲染），列表顶级行也只有批次行（不做存量兼容）。
  check("J1 队列项仍是最细粒度", isQueueItemTask("xhs_note_sync_item"), true);
  check("J2 批次父行不是队列项（不参与 worker 认领判据）", isQueueItemTask("xhs_note_gen_batch"), false);
  check("J3 批次父行在页面白名单里（否则父子树只剩明细）", isQueueTaskType("xhs_note_sync_batch"), true);
  check("J4 批次父行被判为批次", isQueueBatchTask("xhs_note_gen_batch"), true);
  check("J5 队列项不是批次行", isQueueBatchTask("xhs_note_gen_item"), false);
  check("J6 有 batch_no 才是批次行（数据判据，不写死类型）", isBatchRow({ batch_no: "NB20260926153324-7K2M" }), true);
  check("J7 无 batch_no 的行不是批次行（顶级行只有批次行，不做存量兼容）", isBatchRow({ batch_no: "" }), false);
  check("J8 缺字段 / null / undefined 都不当批次行（防御）", [isBatchRow({}), isBatchRow(null), isBatchRow(undefined)], [false, false, false]);
  check("J9 批次父行有子行 ⇒ 可展开出明细", hasChildren("xhs_note_sync_batch", [{ id: 1 }]), true);
  check("J10 队列项带 children 也不当父节点（脏数据防御）", hasChildren("xhs_note_sync_item", [{ id: 1 }]), false);

  // L. **队列归属反查**（筛选器用后端下发的清单反查，前端不写死类型）：
  //    批次行也要能落回它的队列 —— 否则按队列筛选时父行消失，父子树只剩明细。
  //    L5+ 是**「类型」列的队列中文名**（2026-09-26 第三轮口径「类型展示为执行队列
  //    中文展示」）：名字只许来自后端 `QUEUE_REGISTRY.label`，不许前端拼、不许
  //    在未注册类型上编一个队列名。
  const _queues = [
    { key: "note_sync", label: "笔记同步队列", item_task_type: "xhs_note_sync_item", batch_task_type: "xhs_note_sync_batch" },
    { key: "note_publish", label: "笔记发布队列", item_task_type: "xhs_note_publish_item", batch_task_type: "xhs_note_publish_batch" },
  ];
  check("L1 队列项落回它的队列", queueKeyForTask("xhs_note_sync_item", _queues), "note_sync");
  check("L2 批次行落回同一个队列（否则筛选时父行消失）", queueKeyForTask("xhs_note_sync_batch", _queues), "note_sync");
  check("L3 发布批次落回发布队列", queueKeyForTask("xhs_note_publish_batch", _queues), "note_publish");
  check("L4 未注册类型返回 null（不猜队列）", queueKeyForTask("xhs_note_gen_item", _queues), null);
  check("L5 类型列展示队列**中文名**（取后端下发的 label）", queueLabelForTask("xhs_note_sync_item", _queues), "笔记同步队列");
  check("L6 批次行与它的队列项同名（父子树同一队列，不出现两种叫法）", queueLabelForTask("xhs_note_sync_batch", _queues), "笔记同步队列");
  check("L7 发布队列项的中文名", queueLabelForTask("xhs_note_publish_item", _queues), "笔记发布队列");
  check("L8 未注册类型回退中文类型名（不编队列名）", queueLabelForTask("xhs_note_sync", _queues), "账号全量同步");
  check("L9 队列清单为空也不崩（回退中文类型名）", queueLabelForTask("xhs_note_sync_item", []), "同步笔记");
  check("L10 从未见过的类型原样显示（不造名）", queueLabelForTask("mystery_type", _queues), "mystery_type");

  // M. **批次行的执行详情**（2026-09-26 用户口径「批次+详情父子树」）：这一份回答
  //    「这一批整体怎么样」—— 计数**一律取后端 `summary`**（`batch_rollup` 唯一
  //    实现），前端不重算（重算 = 第二份判据，DEF-044）；空批次不硬凑
  //    「成功 0 · 失败 0」，缺值按 0（计数缺值渲染 0 是既有口径）。
  check(
    "M1 总数取后端 summary（不按可见子行重算）",
    batchExecutionDetailLines({ summary: { total: 5, done: 3, failed: 1, running: 1 } })[0],
    "共 5 条明细"
  );
  check(
    "M2 只列非零计数（顺序：成功 / 失败 / 取消 / 进行中 / 待处理）",
    batchExecutionDetailLines({ summary: { total: 5, done: 3, failed: 1, running: 1 } })[1],
    "成功 3 · 失败 1 · 进行中 1"
  );
  check(
    "M3 全终态且零明细 ⇒ 说明没入队成功，不硬凑计数",
    batchExecutionDetailLines({ summary: { total: 0 } })[1],
    "这次动作没有入队成功的项"
  );
  check(
    "M4 取消 / 待处理也进计数",
    batchExecutionDetailLines({ summary: { total: 2, cancelled: 1, pending: 1 } })[1],
    "取消 1 · 待处理 1"
  );
  check(
    "M5 summary 缺失也不抛错（缺值按 0）",
    batchExecutionDetailLines({}),
    ["共 0 条明细", "这次动作没有入队成功的项"]
  );

  // K. 执行详情多行文案（2026-09-26 用户口径「执行详情，使用多行简单列出执行具体内容」）
  const _item = (over = {}) => ({
    task_type: "xhs_note_gen_item",
    status: "done",
    payload: { item_id: "ITEM-1", item_name: "测试桌布" },
    ...over,
  });
  check(
    "K1 生成队列项：商品名 + 已生成《标题》",
    executionDetailLines(
      _item({ payload: { item_id: "ITEM-1", item_name: "测试桌布", note_title: "桌布真好用" } })
    ),
    ["测试桌布", "已生成《桌布真好用》"]
  );
  check(
    "K2 生成队列项缺商品名退 item_id",
    executionDetailLines(_item({ payload: { item_id: "ITEM-1" } })),
    ["ITEM-1", "已生成"]
  );
  check(
    "K3 生成失败带真实原因（行上是唯一一次给原因的地方）",
    executionDetailLines(
      _item({
        status: "failed",
        payload: { item_id: "ITEM-1", item_name: "测试桌布", reason: "网页会话失效", code: "WEB_SESSION_REQUIRED" },
      })
    ),
    ["测试桌布", "网页会话失效（WEB_SESSION_REQUIRED）"]
  );
  check(
    "K4 同步/发布队列项：主体 = 入队快照的笔记标题",
    executionDetailLines({
      task_type: "xhs_note_sync_item",
      status: "done",
      payload: { note_pk: 12, note_title: "春日穿搭", ok: true },
    }),
    ["《春日穿搭》", "成功"]
  );
  check(
    "K5 缺快照退 #note_pk（早期数据不编标题）",
    executionDetailLines({
      task_type: "xhs_note_publish_item",
      status: "done",
      payload: { note_pk: 12, ok: true },
    }),
    ["笔记 #12", "成功"]
  );
  check(
    "K6 同步失败也带真实原因（不落库到笔记行，行上是唯一一次）",
    executionDetailLines({
      task_type: "xhs_note_sync_item",
      status: "failed",
      payload: { note_pk: 12, note_title: "春日穿搭", reason: "平台限流", code: "RATE_LIMITED" },
    }),
    ["《春日穿搭》", "平台限流（RATE_LIMITED）"]
  );
  check("K7 未终态队列项报待处理", executionDetailLines({
    task_type: "xhs_note_sync_item",
    status: "pending",
    payload: { note_pk: 12, note_title: "春日穿搭" },
  }), ["《春日穿搭》", "待处理"]);
  check("K8 进行中队列项报进行中", executionDetailLines({
    task_type: "xhs_note_sync_item",
    status: "running",
    payload: { note_pk: 12, note_title: "春日穿搭" },
  }), ["《春日穿搭》", "进行中"]);
  check("K9 已取消队列项报已取消", executionDetailLines({
    task_type: "xhs_note_sync_item",
    status: "cancelled",
    payload: { note_pk: 12, note_title: "春日穿搭" },
  }), ["《春日穿搭》", "已取消"]);

  // N. **清理的两个维度**（2026-09-26 用户口径「支持按照批次清理记录，批次中存在正在
  //    执行或待执行得记录，则不支持该批次全部清理……分为两个维度，批次维度和批次内的
  //    操作」）：批次维度是**原子动作** —— 批内有活 / 待重试的明细就不许整批清（原因
  //    是给用户看的，必须带条数与出路）；批次内维度只受 `canCleanTask` 管（D11-D17）。
  check(
    "N1 批内有正在执行 / 待执行 ⇒ 不能整批清理",
    typeof batchCleanBlockReason({ summary: { total: 3, done: 1, running: 1, pending: 1 } }),
    "string"
  );
  check(
    "N2 原因报出条数（用户要知道几条在跑）",
    /2 条正在执行 \/ 待执行/.test(
      String(batchCleanBlockReason({ summary: { total: 3, running: 1, pending: 1 } }))
    ),
    true
  );
  check(
    "N3 批内只有待重试 ⇒ 也不能整批清理（删了失去重试入口）",
    /1 条待重试/.test(String(batchCleanBlockReason({ summary: { total: 2, done: 1, failed: 1 } }))),
    true
  );
  check(
    "N4 活 + 待重试同时存在 ⇒ 两类分开报（不合并成一坨）",
    String(batchCleanBlockReason({ summary: { total: 3, running: 1, failed: 1 } })),
    "本批还有 1 条正在执行 / 待执行 · 1 条待重试的明细，不能整批清理；可展开明细逐条清理已完成的记录。"
  );
  check(
    "N5 批内全是已完成 / 已取消 ⇒ 可整批清理",
    batchCleanBlockReason({ summary: { total: 2, done: 1, cancelled: 1 } }),
    null
  );
  check("N6 summary 缺失也不误判成不可清（0 条明细没有阻塞项）", batchCleanBlockReason({}), null);
  check(
    "N7 原因必须给出路（展开明细逐条清）",
    /逐条清理/.test(String(batchCleanBlockReason({ summary: { running: 1 } }))),
    true
  );

  // O. **清理结果分层报账**（同一口径）：清了几条（批次几个 / 明细几条）、多少**被挡**、
  //    多少**状态不可清被跳过** —— 三种都要说，只说「已清理 N 条」会让用户以为剩下的
  //    也清掉了。
  const _cleanOk = cleanOutcomeText({
    cleaned: 3,
    skipped: 0,
    requested: 3,
    batches: { cleaned: 1, blocked: 0 },
    items: { cleaned: 2, skipped: 0 },
  });
  check("O1 全清成功 ⇒ success", _cleanOk.level, "success");
  check("O2 报账带批次 / 明细分层", _cleanOk.text, "已清理 3 条记录（批次 1 个 · 明细 2 条）。");
  const _cleanBlocked = cleanOutcomeText({
    cleaned: 2,
    skipped: 1,
    requested: 3,
    batches: { cleaned: 0, blocked: 1 },
    items: { cleaned: 2, skipped: 0 },
  });
  check("O3 有批次被挡 ⇒ warning（不许报 success）", _cleanBlocked.level, "warning");
  check(
    "O4 被挡原因必须说出来（否则用户以为清干净了）",
    /1 个批次因明细还在执行中 \/ 待重试，整批未清理/.test(_cleanBlocked.text),
    true
  );
  check(
    "O5 一条都没清且被挡 ⇒ warning 不是 info",
    cleanOutcomeText({
      cleaned: 0,
      skipped: 1,
      requested: 1,
      batches: { cleaned: 0, blocked: 1 },
    }).level,
    "warning"
  );
  check(
    "O6 没有可清记录（没被挡）⇒ info",
    cleanOutcomeText({ cleaned: 0, skipped: 0, requested: 0 }).level,
    "info"
  );
  check("O7 缺字段不抛错（宽容）", cleanOutcomeText({}).level, "info");

  // P. **账号列**（2026-09-26 用户口径「批次记录执行的账号」）：明细行取自己的
  //    `payload.account_id`（入队快照），批次行取后端**按子行派生**的 `accounts`。
  //    ⚠️ 跨账号批次必须显「N 个账号」—— 一次批量可以跨账号勾选，批次级硬报一个
  //    账号名就是**说错**（页面上写 1 个、明细里 3 个）。
  const _names = accountNameMap([
    { id: 1, nickname: "观隅好物" },
    { id: 2, nickname: "   " }, // 没名字 ⇒ 退 `账号 #id`（可辨认兜底，不造名字）
    { id: 0, nickname: "不该进来" }, // 非正整数 id ⇒ 不进映射
  ]);
  check("P1 账号映射只放账号名（店铺 / 类型 / (#id) 都不带）", _names["1"], "观隅好物");
  check("P2 账号没名字 ⇒ 退 `账号 #id`（可辨认兜底）", _names["2"], "账号 #2");
  check("P3 非正整数 id 不进映射", Object.keys(_names).sort(), ["1", "2"]);
  check(
    "P4 明细行取自己 payload.account_id 的账号名",
    taskAccountText({ batch_no: null, payload: { account_id: 1 } }, _names),
    "观隅好物"
  );
  check("P5 明细行没有账号 ⇒ `-`（不编一个）", taskAccountText({ batch_no: null, payload: {} }, _names), "-");
  check(
    "P6 账号已删 / 不在清单里 ⇒ 退 `账号 #id`（不退 `-`，至少知道是哪个 id）",
    taskAccountText({ batch_no: null, payload: { account_id: 9 } }, _names),
    "账号 #9"
  );
  check(
    "P7 批次行只有一个账号 ⇒ 直接显账号名（不是「1 个账号」）",
    taskAccountText({ batch_no: "NB1", accounts: [1] }, _names),
    "观隅好物"
  );
  check(
    "P8 批次行跨账号 ⇒ 「N 个账号」（硬报第一个账号就是说错）",
    taskAccountText({ batch_no: "NB1", accounts: [1, 2, 9] }, _names),
    "3 个账号"
  );
  check(
    "P9 多账号 Tooltip 必须列清是哪几个（否则用户只知道「有 3 个」）",
    taskAccountTip({ batch_no: "NB1", accounts: [1, 2, 9] }, _names),
    "观隅好物、账号 #2、账号 #9"
  );
  check(
    "P10 单账号 / 没有账号 ⇒ 不弹 Tooltip（没额外可说的）",
    [
      taskAccountTip({ batch_no: "NB1", accounts: [1] }, _names),
      taskAccountTip({ batch_no: "NB1", accounts: [] }, _names),
      taskAccountTip({ batch_no: null, payload: { account_id: 1 } }, _names),
    ],
    ["", "", ""]
  );
  check(
    "P11 批次账号 id 列表滤掉非法值（0 / 负数 / 非数 / null）",
    batchAccountIds({ accounts: [1, 0, -3, "2", "x", null] }),
    [1, 2]
  );
  check(
    "P12 批次行没有 accounts 字段 ⇒ `-`（不做「从 children 重算」的第二份判据）",
    taskAccountText({ batch_no: "NB1" }, _names),
    "-"
  );

  // Q. **批次行筛选**（2026-09-26 用户口径「将队列执行记录表中的状态筛选放到批次内，
  //    父子树表不使用同一个表头，在表头状态列加筛选条件」）：队列 + 状态两个条件
  //    **只作用于顶级（批次）行**；命中批次 ⇒ 一整批明细原样展示（明细行**从不**被
  //    单独筛掉）。⚠️ Q3 是这一段的重点：明细里有 `failed`、父行是 `done` 时，筛
  //    「成功」必须仍然显示**两条**明细 —— 切明细会让「共 2 条」变成「1 条」，
  //    与后端 `summary` 的计数对不上（父子树不共用同一个筛选条件）。
  const _filterQueues = [
    { key: "note_sync", item_task_type: "xhs_note_sync_item", batch_task_type: "xhs_note_sync_batch" },
    { key: "note_generate", item_task_type: "xhs_note_gen_item", batch_task_type: "xhs_note_gen_batch" },
  ];
  const _batchDone = {
    task_type: "xhs_note_sync_batch",
    status: "done",
    batch_no: "NB20260926153324-7K2M",
    children: [
      { task_type: "xhs_note_sync_item", status: "done" },
      { task_type: "xhs_note_sync_item", status: "failed" }, // 父行 done / 明细有失败
    ],
  };
  const _batchRunning = {
    task_type: "xhs_note_gen_batch",
    status: "running",
    batch_no: "NB20260926153325-8P4Q",
    children: [{ task_type: "xhs_note_gen_item", status: "running" }],
  };
  const _rows = [_batchDone, _batchRunning];
  check(
    "Q1 队列筛选：只留命中队列的批次行",
    filterBatchRows(_rows, { queueKey: "note_generate", queues: _filterQueues }).map((r) => r.batch_no),
    ["NB20260926153325-8P4Q"]
  );
  check(
    "Q2 状态筛选：只留状态命中的批次行（按**父行派生**状态判）",
    filterBatchRows(_rows, { queueKey: "all", queues: _filterQueues, status: "done" }).map((r) => r.batch_no),
    ["NB20260926153324-7K2M"]
  );
  check(
    "Q3 ⚠️ 明细行**不参与**筛选：命中的批次整批原样（父 done / 明细含 failed 也不切）",
    filterBatchRows(_rows, { queueKey: "all", queues: _filterQueues, status: "done" })[0].children.length,
    2
  );
  check(
    "Q4 两个条件是「与」（队列命中但状态不命中 ⇒ 空）",
    filterBatchRows(_rows, { queueKey: "note_sync", queues: _filterQueues, status: "running" }).length,
    0
  );
  check("Q5 全 `all` / 缺字段 ⇒ 原样返回（不筛）", filterBatchRows(_rows, {}).length, 2);
  check(
    "Q6 队列命中只看**顶级行**（明细的类型不参与命中）",
    filterBatchRows(
      [
        {
          task_type: "xhs_note_sync_batch",
          status: "done",
          children: [{ task_type: "xhs_note_gen_item", status: "done" }],
        },
      ],
      { queueKey: "note_generate", queues: _filterQueues }
    ).length,
    0
  );

  console.log(`\n${passed} PASS / ${failed} FAIL`);
  return failed === 0 ? 0 : 1;
}

main().then((code) => process.exit(code));
