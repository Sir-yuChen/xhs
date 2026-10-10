/**
 * 行为验证：定时任务中心（设计 15，2026-09-24；2026-09-24 验收修正为两页签）。
 *
 * 盯的四件事（源码断言 / 自我印证都证明不了）：
 *
 * 1. **两页签口径**：任务中心只剩「任务列表」+「任务日志」；「一次性任务」页签与
 *    「执行到期任务」按钮已按用户验收口径移除（补跑由调度器自动进行）。
 * 2. **at 与周期合并为单一列表**：任务列表渲染**全部** scheduled_jobs 行（不按
 *    trigger_type 拆两个页签/两个表格），列一致；操作列按状态区分 —— completed
 *    （只有 at 会到）不给暂停/恢复。
 * 3. **前后端 cron 口径一致**：预设「每周一、五 9:00」的 cron 必须是
 *    `0 0 9 * * 0,4`（APScheduler 0=周一）。旧 auto_tasks 的 isoweekday
 *    （1=周一）口径一旦混回前端，调度就会整体差一天。
 * 4. **cron 默认值与后端同值**：`emptyCronDraft()` 产出 `0 0 9 * * *`，
 *    与后端 AutoTask 惰性默认、`/schedule/jobs` 默认 schedule 逐字符一致。
 *
 * 做法与 note_filters.cjs 同源：esbuild 打包**真实**模块直接调用
 * （纯函数模块，不需要 antd 桩）；页面结构断言读**真实页面源文件**。
 *
 * 运行：
 *   <node> tests/frontend/schedule_center.cjs        # 期望全 PASS
 *   SCHEDULE_MUTATE=dow      <node> …                # 期望 FAIL
 *   SCHEDULE_MUTATE=label    <node> …                # 期望 FAIL
 *   SCHEDULE_MUTATE=atlabel  <node> …                # 期望 FAIL
 *   SCHEDULE_MUTATE=skipped  <node> …                # 期望 FAIL
 *   SCHEDULE_MUTATE=defaultcron <node> …             # 期望 FAIL
 *   SCHEDULE_MUTATE=manual   <node> …                # 期望 FAIL
 *   SCHEDULE_MUTATE=fulllabel <node> …               # 期望 FAIL
 *   SCHEDULE_MUTATE=typecol  <node> …                # 期望 FAIL
 *   SCHEDULE_MUTATE=targetcol <node> …               # 期望 FAIL（调用目标丢中文名）
 *   SCHEDULE_MUTATE=width70  <node> …                # 期望 FAIL（详情弹窗宽度漂移）
 *   SCHEDULE_MUTATE=accountscope <node> …            # 期望 FAIL（账号多选退回仅编辑态）
 *   SCHEDULE_MUTATE=progresskeep <node> …            # 期望 FAIL（失败退回 exception 态 ⇒ 百分比被顶掉）
 *   SCHEDULE_MUTATE=progressbar  <node> …            # 期望 FAIL（不定量条退回「进行中」文字）
 *   SCHEDULE_MUTATE=fakefull     <node> …            # 期望 FAIL（未知比例退回 percent={100} 满格冒充）
 *   SCHEDULE_MUTATE=unknownzero  <node> …            # 期望 FAIL（未知判据失效 ⇒ 未知被谎报成 0%）
 *   SCHEDULE_MUTATE=tworeads     <node> …            # 期望 FAIL（详情弹窗不再共用组件 ⇒ 同一行两个读数）
 *   SCHEDULE_MUTATE=pageread     <node> …            # 期望 FAIL（列表列绕开唯一实现自绘进度条）
 *   SCHEDULE_MUTATE=accountlabel <node> …            # 期望 FAIL（执行账号退回只看任务配置 ⇒ 又显「全部账号」）
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/schedule.ts");
const CRON_MODULE_TS = path.join(PROJECT, "frontend/src/lib/cron-builder.ts");
const PAGE_TSX = path.join(PROJECT, "frontend/src/pages/tasks/task-center-page.tsx");
const DETAIL_TSX = path.join(PROJECT, "frontend/src/components/schedule/job-log-detail-modal.tsx");
const PROGRESS_TSX = path.join(PROJECT, "frontend/src/components/schedule/job-log-progress.tsx");
const GLOBAL_CSS = path.join(PROJECT, "frontend/src/global.css");
const CRON_BUILDER_TSX = path.join(PROJECT, "frontend/src/components/schedule/cron-builder-modal.tsx");
const AUTO_OPS_TSX = path.join(PROJECT, "frontend/src/pages/platforms/xhs/auto-ops-panel.tsx");
const API_TS = path.join(PROJECT, "frontend/src/lib/api.ts");

const MUTATE = process.env.SCHEDULE_MUTATE || "";

// 变异锚点（必须是源码里**唯一**的串；行尾归一化防 CRLF 失配）
// ⚠️ 锚点必须逐字符等于源码（含缩进）—— 对象字面量里的键是**顶格**的，
//    曾经误写 2 空格缩进 ⇒ 锚点永不命中、变异静默失效（假通过）。见 DEF-063。
const RESERVED = {
  // 每周预设的 dow 用回 isoweekday（1,5）⇒ 调度整体差一天
  dow: `{ label: "每周一、五 9:00", cron: "0 0 9 * * 0,4" },`,
  // 错过策略文案漂移
  label: `once: "执行一次",`,
  // 类型**简写**文案漂移（R15：列表列用简写，漂了就会被当成别的档）
  atlabel: `at: "定时",`,
  // 日志状态四档少一档（skipped 没了 ⇒ 撞退避门的执行在 UI 上看不出来）
  skipped: `skipped: { color: "warning", label: "已跳过（退避中）" },`,
  // cron 构建器默认值从 9 点漂到 12 点（与后端默认不再同值）
  defaultcron: `hour: { mode: "value", value: "9" },`,
  // R14/R15：三档简写表的「手动」漂移
  manual: `  manual: "手动",\n  cron: "Cron",`,
  // R15：全称表漂移（表单会跟着漂）
  fulllabel: `  cron: "Cron 周期",\n  at: "定时一次",`,
  // R16：类型列偷偷包回 Tag（列表该是纯文本）
  typecol: `render: (type: string) => <Text>{triggerTypeLabel(type)}</Text>,`,
  // R19 后续：详情弹窗宽度退回固定 720（脱离 70% 口径）
  width70: `      width="70%"\n      destroyOnClose`,
  // 调用目标列退回「只有代码键」的单行（中文名丢掉）
  targetcol: `          <Text>{targetNames[key] ?? "-"}</Text>\n          <Text code type="secondary" style={{ fontSize: 12 }}>`,
  // R22（物理删除）反向变异：把分组字段偷偷加回 schedule 模块 ⇒ 必须被守卫抓住
  groupfield: `export type TriggerType =`,
  // R22 反向变异：详情弹窗快照块把「分组」行加回来
  groupsnapshot: `                <Descriptions.Item label="调用目标">`,
  // 用户口径（2026-09-25）反向变异：详情弹窗的调用目标**退回只显代码键**
  // （中文名丢掉）—— 这正是用户报的「调用目标需要配置中文名称」那个缺口，
  // 缺了就只剩 `system.queue_record_cleanup` 这种裸键。
  detailtarget: `                    <Text>{targetNames[jobKey] ?? "-"}</Text>\n                    <Text code type="secondary" style={{ fontSize: 12 }}>`,
  // 2026-09-27 反向变异：失败时退回 antd `exception` 态（百分比文字被 ✗ 图标顶掉
  // ⇒ 用户看不到「已执行的进度比例」，正是本轮要修的那件事）
  progresskeep: `strokeColor={record.status === "failed" ? "#ff4d4f" : undefined}`,
  // 2026-09-28 反向变异：比例未知的**不定量进度条**退回「进行中」文字
  // （用户口径「任务进度列应展示任务进度条，而不是文本进行中」原样复活）。
  // ⚠️ 同日第三轮换锚点：不定量条已从「满格 `percent={100}`」改成**滑动色块**，
  //    实现在 `job-log-progress.tsx`（唯一实现）—— 锚点跟着搬到那个类名上。
  progressbar: `className="log-progress-unknown"`,
  // 2026-09-28（同日第三轮，用户实测报障）反向变异：比例未知时**退回满格条冒充 100%**
  // —— 满格条与「100% 完成」视觉同形，用户看到「任务刚开跑进度条就 100%、点详情 0%」
  //   正是这么来的（设计 13 §43 的 D1）。
  fakefull: `    const height = size === "small" ? 6 : 8;`,
  // 2026-09-28 反向变异：**未知判据失效**（恒不未知）⇒ 后端还没上报的那段时间
  // 被谎报成 0% 进度条 —— 0% 会被读成卡死，是 2026-09-27 用户反复取消同步的诱因
  // （设计 13 §35.18.6：未知与 0% 是两件事）。
  unknownzero: `  return log.status === "running" && !log.progress;`,
  // 2026-09-28 反向变异：详情弹窗**不再共用唯一实现**（退回自己读 `log.progress`）
  // ⇒ 同一行数据在列表与弹窗里又变成两个读数（用户报障的原形态）。
  tworeads: `                <JobLogProgress log={log} size="default" maxWidth={220} />`,
  // 2026-09-28 反向变异：列表列**绕开唯一实现自绘进度条** ⇒ 两处呈现又会各说各话。
  pageread: `          <JobLogProgress log={record} />`,
  // DEF-066 反向变异：账号多选退回「仅编辑态」⇒ 新建态又没有账号多选了
  // （手抄 account_ids 过不了归属校验 ⇒ 用户建不出账号维度任务）
  accountscope: `(job?.params_editable ?? selectedTarget.params_editable)?.includes("account_ids")`,
  // 2026-09-27 反向变异：「执行账号」退回**只看 `params.account_ids`**（丢掉定向执行快照）
  // ⇒ 账号卡点「同步笔记」后，执行详情又显示「全部账号」（用户报障原样复活）
  accountlabel: `                {targetAccountId !== null ? (`,
};

const MUTATE_TO = {
  dow: `{ label: "每周一、五 9:00", cron: "0 0 9 * * 1,5" },`,
  label: `once: "合并执行",`,
  atlabel: `at: "定时任务",`,
  skipped: `skipped: { color: "warning", label: "已跳过" },`,
  defaultcron: `hour: { mode: "value", value: "12" },`,
  manual: `  manual: "定时任务",\n  cron: "Cron",`,
  fulllabel: `  cron: "周期任务",\n  at: "定时一次",`,
  typecol: `render: (type: string) => <Tag>{triggerTypeLabel(type)}</Tag>,`,
  width70: `      width={720}\n      destroyOnClose`,
  targetcol: `          <Text code style={{ fontSize: 12 }}>`,
  // R22 反向变异：在模块里复活分组常量（守卫应立刻红）
  groupfield: `export const JOB_GROUP_LABELS: Record<string, string> = { DEFAULT: "默认分组", SYSTEM: "系统分组" };\n\nexport type TriggerType =`,
  // R22 反向变异：快照块加回「分组」项
  groupsnapshot: `                <Descriptions.Item label="分组">{String(snapshot.job_group ?? "")}</Descriptions.Item>\n                <Descriptions.Item label="调用目标">`,
  // 用户口径（2026-09-25）反向变异：弹窗调用目标退回单行代码键
  detailtarget: `                    <Text code type="secondary" style={{ fontSize: 12 }}>`,
  progresskeep: `status={record.status === "failed" ? "exception" : "normal"}`,
  // 2026-09-28 反向变异：不定量滑动条退回「进行中」文字（进度条整条消失）
  progressbar: `className="log-progress-text"`,
  // 2026-09-28 反向变异：未知比例退回满格条冒充 100%
  fakefull: `    return <Progress percent={100} showInfo={false} size={size} status="active" />;
    const height = size === "small" ? 6 : 8;`,
  // 2026-09-28 反向变异：未知判据失效（永远不认为「未知」）
  unknownzero: `  return false;`,
  // 2026-09-28 反向变异：详情弹窗退回自绘进度条（直读 log.progress）
  tworeads: `                <Progress percent={Math.max(0, Math.min(100, log.progress ?? 0))} size="small" style={{ maxWidth: 220 }} />`,
  // 2026-09-28 反向变异：列表列退回自绘进度条
  pageread: `          <Progress percent={record.progress ?? 0} size="small" />`,
  // DEF-066 反向变异：账号多选退回编辑态专属（新建态丢失多选）
  accountscope: `job?.params_editable?.includes("account_ids")`,
  // 2026-09-27 反向变异：「执行账号」分支条件退回只看 `params.account_ids`
  // （= 又去读 job 行的任务配置「空 = 全部账号」）
  accountlabel: `                {accountIds !== null ? (`,
};

let failures = 0;
/** 剥掉块注释与行注释（`in source` 型断言一律用它 —— DEF-056 教训：\r\n *  解释性注释会命中自己写的关键词）。 */
function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
}
function check(name, condition, detail) {
  if (condition) {
    console.log(`PASS ${name}`);
  } else {
    failures += 1;
    console.log(`FAIL ${name}${detail ? ` — ${detail}` : ""}`);
  }
}

// 锚点归属：每个锚点只在其**所在文件**里替换。两个插件（schedule.ts /
// cron-builder.ts）都要跑，若让任一插件对「不属于自己」的锚点硬失败，
// 变异就永远命中不了（假通过）。见 DEF-063。
const RESERVED_FILE = {
  dow: CRON_MODULE_TS,
  defaultcron: CRON_MODULE_TS,
  label: MODULE_TS,
  atlabel: MODULE_TS,
  skipped: MODULE_TS,
  manual: MODULE_TS,
  fulllabel: MODULE_TS,
  typecol: PAGE_TSX,
  targetcol: PAGE_TSX,
  width70: DETAIL_TSX,
  groupfield: MODULE_TS,
  groupsnapshot: DETAIL_TSX,
  detailtarget: DETAIL_TSX,
  progresskeep: PAGE_TSX,
  progressbar: PROGRESS_TSX,
  fakefull: PROGRESS_TSX,
  unknownzero: PROGRESS_TSX,
  tworeads: DETAIL_TSX,
  pageread: PAGE_TSX,
  accountscope: path.join(PROJECT, "frontend/src/components/schedule/job-form-modal.tsx"),
  accountlabel: DETAIL_TSX,
};

// 页面 / 弹窗源码一律经本函数读取 —— 变异**作用在内存字符串上，不碰磁盘**。
// ⚠️ 原实现是「就地改写文件、跑完在 finally 里还原」：`-n auto` 下正向用例（读同一文件
//    做结构断言）会读到「已被改坏、还没还原」的内容 —— 2026-09-27 实测
//    `R22 日志详情弹窗不再取 snapshot.job_group` 随机变红，而单跑脚本 / `-n0` 全绿。
//    模块侧本来就是这个语义（`mutatePlugin` 在 onLoad 里替换），这里拉齐：变异不再污染
//    工作区，也不再与任何并行用例抢同一个文件。
function readSource(file) {
  const source = fs.readFileSync(file, "utf8");
  if (!MUTATE || RESERVED_FILE[MUTATE] !== file || !RESERVED[MUTATE]) return source;
  // ⚠️ 源码是 CRLF ⇒ 锚点（含 \n）必须先归一化再比对，否则永远「found 不到」
  //    （曾在 targetcol 上复现：锚点肉眼一致却报 anchor not found）。
  const normalized = source.replace(/\r\n/g, "\n");
  if (!normalized.includes(RESERVED[MUTATE])) {
    console.log(`FAIL mutate anchor not found: ${MUTATE} in ${path.basename(file)}`);
    process.exit(1);
  }
  return normalized.replace(RESERVED[MUTATE], MUTATE_TO[MUTATE]);
}

function mutatePlugin(targetFile) {
  return {
    name: "mutate",
    setup(build) {
      build.onLoad({ filter: new RegExp(`${path.basename(targetFile)}$`) }, () => {
        let source = fs.readFileSync(targetFile, "utf8").replace(/\r\n/g, "\n");
        if (MUTATE && RESERVED[MUTATE] && RESERVED_FILE[MUTATE] === targetFile) {
          if (!source.includes(RESERVED[MUTATE])) {
            console.log(`FAIL mutate anchor not found: ${MUTATE} in ${path.basename(targetFile)}`);
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
  // 页面侧变异已改为「读时内存变异」（见 `readSource`）—— 不再就地改文件，
  // 因此没有「跑完还原」这一步，`try/finally` 一并撤掉。
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "schedule-center-"));
  const bundled = await esbuild.build({
    entryPoints: [MODULE_TS],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    target: "node18",
    logLevel: "silent",
    plugins: [mutatePlugin(CRON_MODULE_TS), mutatePlugin(MODULE_TS)],
  });
  const outFile = path.join(tmpDir, "schedule.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const schedule = require(outFile);

  const {
    triggerTypeLabel,
    triggerTypeFullLabel,
    misfirePolicyLabel,
    concurrentLabel,
    jobStatusTag,
    logStatusTag,
    logTriggerLabel,
    triggerSummary,
    JOB_GROUP_LABELS,
  } = schedule;

  // R22 守卫：分组字段已**物理删除** ⇒ 模块不该再有它的任何导出。
  // （正向断言在页尾守卫组；这条紧贴模块加载，让 groupfield 反向变异**必红**。）
  check("R22 模块无 JOB_GROUP_LABELS 导出", JOB_GROUP_LABELS === undefined);

  if (typeof triggerSummary !== "function") {
    console.error("FATAL 未导出 schedule 模块 —— 若已重命名，请同步更新本脚本");
    process.exit(1);
  }

  // --------------------------------------------------------------- //
  // 1. 文案字面期望表（用户契约，不从模块里读 —— 读了就是自我印证）
  // --------------------------------------------------------------- //
  check("misfire.fire_now=立即执行", misfirePolicyLabel("fire_now") === "立即执行");
  check("misfire.once=执行一次", misfirePolicyLabel("once") === "执行一次");
  check("misfire.discard=放弃执行", misfirePolicyLabel("discard") === "放弃执行");
  // R15：列表类型用**简写**（用户口径「尽量简写」）；全称给表单 / Tooltip
  check("trigger.manual=手动", triggerTypeLabel("manual") === "手动");
  check("trigger.cron=Cron", triggerTypeLabel("cron") === "Cron");
  check("trigger.at=定时", triggerTypeLabel("at") === "定时");
  check("trigger 全称 manual=手动执行", triggerTypeFullLabel("manual") === "手动执行");
  check("trigger 全称 cron=Cron 周期", triggerTypeFullLabel("cron") === "Cron 周期");
  check("trigger 全称 at=定时一次", triggerTypeFullLabel("at") === "定时一次");
  check("trigger.interval 不再是一档（已并入 cron）", triggerTypeLabel("interval") === "interval");
  check("jobStatus.active=运行中", jobStatusTag("active").label === "运行中");
  check("jobStatus.completed=已完成", jobStatusTag("completed").label === "已完成");
  check("logStatus.skiipped 存在且带退避说明", logStatusTag("skipped").label.includes("退避"));
  check("logTrigger.manual=手动执行", logTriggerLabel("manual") === "手动执行");
  check("concurrent.forbid=禁止并发", concurrentLabel("forbid") === "禁止并发");

  // 触发器摘要：预设 cron 回中文，非预设回原文（不造第二个 cron 解释器）
  check(
    "triggerSummary 预设回中文",
    triggerSummary({ trigger_type: "cron", cron_expression: "0 0 9 * * *" }) === "每天 9:00"
  );
  check(
    "triggerSummary 非预设回原文",
    triggerSummary({ trigger_type: "cron", cron_expression: "0 30 8 15 * *" }) === "0 30 8 15 * *"
  );
  // R14：manual 档没有触发器，摘要固定为「手动触发（不自动执行）」
  check(
    "triggerSummary manual 回「手动触发」",
    triggerSummary({ trigger_type: "manual" }).includes("手动触发")
  );

  // --------------------------------------------------------------- //
  // 2. cron 纯函数（与后端 6 位口径一致）
  // --------------------------------------------------------------- //
  const bundledCron = await esbuild.build({
    entryPoints: [CRON_MODULE_TS],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    target: "node18",
    logLevel: "silent",
    plugins: [mutatePlugin(CRON_MODULE_TS), mutatePlugin(MODULE_TS)],
  });
  const cronOutFile = path.join(tmpDir, "cron.bundle.cjs");
  fs.writeFileSync(cronOutFile, bundledCron.outputFiles[0].text);
  const cron = require(cronOutFile);
  const { CRON_PRESETS, buildCron, emptyCronDraft, parseCron, cronLooksSane } = cron;

  check("默认草稿 = 0 0 9 * * *（与后端同值）", buildCron(emptyCronDraft()) === "0 0 9 * * *");
  check("cronLooksSane 接受 6 位", cronLooksSane("0 0 9 * * *") === true);
  check("cronLooksSane 拒绝 5 位", cronLooksSane("0 0 9 * *") === false);
  check("parseCron 解析 6 位", parseCron("0 0 9 * * *") !== null);
  check("parseCron 拒绝 5 位", parseCron("0 0 9 * *") === null);
  const weekly = parseCron("0 0 9 * * 0,4");
  check("parseCron 周字段识别为 list", weekly !== null && weekly.week.mode === "list" && weekly.week.value === "0,4");

  // 前后端口径契约：预设「每周一、五」必须是 APScheduler dow（0=周一）
  const weeklyPreset = CRON_PRESETS.find((item) => item.label === "每周一、五 9:00");
  check("每周预设 dow=0,4（0=周一，非 isoweekday）", weeklyPreset && weeklyPreset.cron === "0 0 9 * * 0,4");
  const dailyPreset = CRON_PRESETS.find((item) => item.label === "每天 9:00");
  check("每天预设 = 0 0 9 * * *", dailyPreset && dailyPreset.cron === "0 0 9 * * *");

  // --------------------------------------------------------------- //
  // 3. 页面结构（真实源文件）
  // --------------------------------------------------------------- //
  const page = readSource(PAGE_TSX);
  // 剥注释后的页面源码：`in source` 型断言一律用它（DEF-056 教训：解释性注释会
  // 命中自己写的关键词）。定义在此处，供第 3 / 6 / 9 节共用。
  const pageCode = page.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  // schedule 模块源码（R22 物理删除守卫用；同样剥注释）
  const scheduleCode = fs
    .readFileSync(MODULE_TS, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/[^\n]*/g, "");
  // 「任务列表」label 带计数后缀（模板字符串）；「任务日志」是普通字符串
  check("页签「任务列表」存在", page.includes("label: `任务列表"));
  check("页签「任务日志」存在", page.includes('label: "任务日志"'));
  // 验收口径 ①：「一次性任务」页签与「执行到期任务」按钮已移除
  check("「一次性任务」页签已移除", !page.includes("一次性任务"));
  check("「执行到期任务」按钮已移除", !page.includes("执行到期任务"));
  check("页面不再 import runDueTasks", !page.includes("runDueTasks"));
  // 验收口径 ②：at 与周期合并为单一列表（不再按 trigger_type 过滤渲染）
  check("任务列表渲染全部行", page.includes("dataSource={jobs}"));
  check("不再按 trigger_type 拆列表", !page.includes('trigger_type === "at"') && !page.includes('trigger_type !== "at"'));
  // 操作列按状态区分：completed（只有 at 会到）不给暂停/恢复
  check("completed 行不给暂停/恢复", page.includes('record.status !== "completed"'));
  // ⚠️ 原断言「builtin 行删除禁用」（`page.includes("record.is_builtin")`）**已被推翻**：
  //    用户口径「所有任务都是内置的」⇒ 内置不再是特权，删除一律放开。原文保留：
  //        check("builtin 行删除禁用", page.includes("record.is_builtin"));
  check("旧调度状态端点已不在页面", !page.includes("/tasks/scheduler/status"));

  const api = fs.readFileSync(API_TS, "utf8");
  check("api 指向 /schedule/status", api.includes('"/schedule/status"'));
  check("api 不再有 fetchSchedulerStatus", !api.includes("fetchSchedulerStatus"));
  check("api 有 cron 校验函数", api.includes("export async function validateCron"));
  check("api 有日志清空函数", api.includes("export async function cleanScheduleLogs"));

  // --------------------------------------------------------------- //
  // 4. 任务类型选择器（R14：三档 = 手动执行 / Cron 周期 / 定时一次）
  // --------------------------------------------------------------- //
  const fields = readSource(
    path.join(PROJECT, "frontend/src/components/schedule/schedule-fields.tsx")
  );
  // R15/R16：三档文案不再硬编码在表单里 —— 改判「走同源表」（见第 8 节）
  check("任务类型选择器三档齐全（manual/cron/at）", fields.includes('value="manual"') && fields.includes('value="cron"') && fields.includes('value="at"'));
  // 验收口径 R14：「间隔周期」并入 Cron，「间隔周期」字样不再出现在选择器
  check("「间隔周期」已从选择器移除", !fields.includes('value="interval"'));
  // 剥掉注释再判（注释里解释「字段已删除」不算代码残留 —— DEF-056 教训：
  // `in source` 型断言必须先剥注释，否则会被自己写的解释性注释命中）
  const fieldsCode = fields.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  check("interval 秒数输入区已移除", !fieldsCode.includes("interval_seconds"));
  check("新建默认 cron（每天 9:00）", fields.includes("DEFAULT_CRON = CRON_PRESETS[2].cron"));

  // --------------------------------------------------------------- //
  // 5. 自定义参数：KV 输入框（2026-10-06 用户口径，白名单驱动）
  // --------------------------------------------------------------- //
  const form = readSource(
    path.join(PROJECT, "frontend/src/components/schedule/job-form-modal.tsx")
  );
  // 2026-10-06 用户口径「定时任务调用参数新增/修改用 kv 输入框，kv 必填；编辑弹窗
  // 也使 key；v 多值用逗号分隔（如执行账号）」⇒ 三套旧控件（账号多选 / 保留期数字 /
  // 裸 JSON）统一成**一套 KV 编辑器**，白名单来自后端 `params_editable`。
  check(
    "KV 参数按 params_editable 驱动（建/改同款）",
    form.includes('job?.params_editable ?? selectedTarget.params_editable')
  );
  // 2026-10-06 用户口径追加「k 不要使用下拉框选择」⇒ KV 编辑器抽成共享组件
  // `params-editor.tsx`（key / value **都是自由文本输入**），新建/编辑与「执行一次」
  // 弹窗共用同一实现 —— 所以这几条判据指向共享组件，不再指 job-form-modal。
  const kvEditor = readSource(
    path.join(PROJECT, "frontend/src/components/schedule/params-editor.tsx")
  );
  // 剥注释后的编辑器源码：`in source` 型断言一律用它（DEF-056 同型 ——
  // 解释性注释里会写「全选」「账号对照」这些被守护的词）。
  const kvEditorCode = stripComments(kvEditor);
  check("KV 编辑器有 key/value 两个输入", kvEditor.includes('placeholder="key') && kvEditor.includes('placeholder='));
  // ⚠️ 2026-10-07：原先这条是「KV key 是自由文本（不是下拉）」判据 =
  // `!kvEditor.includes("<Select")` —— 用户口径「当调用参数为执行账号时使用下拉
  // 复选框」后组件里**必然**出现 `<Select`，那条宽判据被取代。收紧为**逐行判据**：
  // key 恒为 `<Input`（历史口径「k 不要使用下拉框选择」**未变**），下拉只挂在
  // `account_ids` 那一行的 value 上。
  check("KV key 是自由文本输入", kvEditor.includes('placeholder="key（如 account_ids）"'));
  check(
    "执行账号那一行的 value 是下拉多选（mode=multiple）",
    // ⚠️ 判据必须落在**调用形态**上：2026-10-07 加枚举下拉时把 `row.key.trim()`
    // 提成了局部 `trimmedKey` ⇒ 原来的「含 `row.key.trim() === ACCOUNT_IDS_KEY`」
    // 断（守卫失效但**不报错**）。锚点改成局部量赋值形态。
    kvEditor.includes("mode=\"multiple\"") &&
      kvEditor.includes("trimmedKey === ACCOUNT_IDS_KEY")
  );
  // ⚠️ 判据必须是**调用形态**，不能只查函数名是否存在：函数留着、调用点改回
  // `String(account.id)` 时「存在性」断言照样绿（实测 ESCAPED 过一次）。
  check("下拉选项文案走账号昵称（缺值退 账号 #id）", kvEditorCode.includes("label: accountOptionLabel(account)"));
  // ⚠️ 判据必须落在**剥注释后**的代码上：上面这两条的对立面（「全选」/「账号对照」）
  // 恰好会出现在我自己写的解释性注释里 ⇒ 不剥注释就是被自己的注释判红（DEF-056 同型）。
  check("执行账号下拉不做「全选」", !kvEditorCode.includes("全选"));
  // 2026-10-07 取代：value 不再有「多个值用逗号分隔」提示 —— 多值键（`account_ids`）
  // 是**下拉多选**，逗号拼接是内部表示，不该让用户手打；其余键都是单值。
  check("KV value 无「多个值用逗号分隔」提示（多值走下拉）", !kvEditorCode.includes("多个值用逗号分隔"));
  // 2026-10-07 取代：不再有「参数 key 不能为空」——内置任务 key 只读恒有值；
  // 非内置任务 key 为空的行由提交前过滤（`nonEmptyRows`）兜住，不弹误导性错误。
  check("无「参数 key 不能为空」文案（key 由注册表决定）", !stripComments(form).includes("参数 key 不能为空"));
  check("KV 必填校验（value 不能空）", form.includes("value 不能为空"));
  // account_ids：value 逗号分隔 → 提交时转 int 数组（后端仍收数组）。
  check("account_ids 逗号分隔转数组", kvEditor.includes('.split(",")'));
  // 账号数据源由调用方注入（组件自己不拉取 ⇒ 不留第二份真相源 / 不多一次往返）。
  check("账号数据源由调用方注入（编辑弹窗）", form.includes("accounts={accounts}"));
  // 2026-10-07 取代：原先 job-form-modal 有一行「账号对照：id=昵称」提示 ——
  // 名字现在就在下拉选项里，那行提示是重复的第二份真相源，已删除。
  check("不再有 id=昵称 的账号对照提示行", !stripComments(form).includes("账号对照"));
  // 2026-10-07 用户口径「编辑任务和新增弹窗全部重新布局，目前太长了，全屏的 70%」：
  // ① 尺寸 = 视口 70%（不写死像素）+ 内容区 70vh 内滚；② 表单**两栏**（不再单列竖排）。
  check("新建/编辑弹窗宽度是视口 70%", form.includes('width="70%"') && !form.includes("width={680}"));
  check("弹窗内容区 70vh 内滚", form.includes('maxHeight: "70vh"') && form.includes('overflowY: "auto"'));
  check(
    "表单两栏布局（调度配置 / 调用参数各一 Card）",
    form.includes('<Row gutter={16}') &&
      form.includes('title="调度配置"') &&
      form.includes('title="调用参数"')
  );
  // 2026-10-07 用户口径「编辑任务和新增弹窗**全部**重新布局……全屏的 70%」：
  // 「全部」= 同族的另外两个也要跟上（否则从新建弹窗里点开 Cron 构建器，
  // 尺寸突然缩回 640 像素，看着像两个产品的弹窗）。
  const cronBuilder = readSource(CRON_BUILDER_TSX);
  const autoOps = readSource(AUTO_OPS_TSX);
  check(
    "Cron 构建器弹窗同一尺寸口径",
    cronBuilder.includes('width="70%"') && cronBuilder.includes('maxHeight: "70vh"')
  );
  check(
    "自动运营编辑弹窗同一尺寸口径",
    autoOps.includes('width="70%"') && autoOps.includes('maxHeight: "70vh"')
  );
  // 2026-10-07 用户口径「tip 内容全部精简只提示核心内容即可」：
  // 判据 = **不含**解释性的括号补充（原先每条都写成「名词（解释；补充）」）。
  const hintBlock = kvEditorCode.slice(kvEditorCode.indexOf("KNOWN_KEY_HINTS"));
  check("已知键提示已精简（无括号补充说明）", !/（[^）]*[；;][^）]*）/.test(hintBlock));
  // 2026-10-07「每日发布商业笔记」参数字典（设计 15 §19）：新键的**中文名**与
  // **枚举下拉**都要在 —— 没有中文名用户只看到 `generate_mode` 裸键；没有下拉
  // 用户会手打出后端不认的取值（写操作，代价真实）。
  // 2026-10-07 追加：发布失败重试三件套也在同一字典里（用户口径
  // 「发布失败是否重试、重试次数最多 3 次、重试间隔时间」）。
  for (const key of [
    "limit",
    "interval_seconds",
    "generate_mode",
    "retry_on_failure",
    "max_retries",
    "retry_interval_seconds",
  ]) {
    // ⚠️ 2026-10-07：`PARAM_LABELS` 已从 `job-form-modal.tsx` **下移到** `params-editor.tsx`
    //（参数行的中文名列就在那里渲染，映射必须与渲染处同源）—— 判据随之落在 kvEditor 上；
    // `job-form-modal` 只保留一行透传导出。
    check(`参数 ${key} 有中文名`, kvEditor.includes(`  ${key}: "`));
    check(`参数 ${key} 有提示文案`, kvEditor.includes(`  ${key}: "`));
  }
  check(
    "generate_mode 是枚举下拉（取值固定）",
    // ⚠️ 锚点必须是**调用形态**：只查 `PARAM_ENUM_OPTIONS` 出现过时，把查表那行
    // 改成 `undefined`（枚举下拉永不渲染）守卫照样绿 —— 实测 ESCAPED 过一次。
    kvEditorCode.includes("generate_mode: { qianfan:") &&
      kvEditorCode.includes("const enumOptions = PARAM_ENUM_OPTIONS[trimmedKey];") &&
      // 分支形态也要钉：查表留着、但渲染分支恒假（下拉永不出现）同样应被判红。
      kvEditorCode.includes("{enumOptions ? (")
  );
  // 参数展示名映射（回退 label，不硬编码后端下发之外的键名）。
  check(
    "retry_on_failure 是枚举下拉（取值 true/false）",
    kvEditorCode.includes("retry_on_failure: { true:") &&
      kvEditorCode.includes('false: "不重试"')
  );
  check("参数 key 有展示名映射（在 params-editor，与渲染同源）", kvEditor.includes("const PARAM_LABELS"));
  check("job-form-modal 只透传 PARAM_LABELS（不下沉第二份映射）", form.includes('export { PARAM_LABELS } from "./params-editor"') && !form.includes("const PARAM_LABELS"));
  // 2026-10-07 用户口径「中文参数名称写到最前面，不允许编辑只能预览」。
  check("参数行**最前面**是中文名列", /paramLabel\(row\.key\.trim\(\)\)/.test(kvEditorCode) &&
    kvEditorCode.indexOf("paramLabel(row.key.trim())") < kvEditorCode.indexOf("value={row.key}"));
  check("中文名列恒只读（readOnly 写死，不随 editable 变）",
    /value=\{paramLabel\(row\.key\.trim\(\)\)\}\r?\n\s+readOnly\r?\n/.test(kvEditor));
  check("中文名取值走 paramLabel（缺值退提示/裸 key，不猜）", kvEditor.includes("export function paramLabel"));
  // 2026-10-07 用户口径「中文名称简短展示全部」（改后追加）：「存在单位的放后面」
  //「英文名也展示完整」。
  // ⚠️ 判据必须落在**剥注释后**的代码上：本文件自己的注释里就举了「发布间隔（秒）」
  // 当反例 ⇒ 不剥注释就是被自己的注释判红（DEF-056 同型）。
  check("中文名不含单位（单位放后面）", !/（秒）|（天）/.test(kvEditorCode));
  //（原「中文名列统一 180px」判据已被下面那条更严的取代：只查 width 测不出 flex 压缩。）
  check("单位作值输入框的后缀", kvEditorCode.includes("suffix={PARAM_UNITS[row.key.trim()] ?? undefined}"));
  check("单位表存在且有秒/天两档", kvEditor.includes("export const PARAM_UNITS") &&
    kvEditor.includes('interval_seconds: "秒"') && kvEditor.includes('days: "天"'));
  check("中文名短（≤6 字，够放全部不留省略号）",
    (kvEditor.match(/^\s+\w+: "([^"]+)",/gm) || []).every((l) => l.match(/"([^"]+)"/)[1].length <= 8));
  // 2026-10-07 用户口径「中英文名称长度统一 180px」（先「200px」后改 180，原值不再保留）。
  // 2026-10-07 用户口径「中英文名称长度统一 180px」+「参数值输入框固定 200px」：
  // ⚠️ 判据必须同时钉 `flex: "none"` —— 只查 width 时，`Space.Compact` 把子项变可压缩
  // flex 项后宽度被压掉，「两列同宽」在界面上并不成立（用户报「展示不一致长」即此因）。
  check(
    "中文列 180px / 英文列 220px 且不被 flex 压缩",
    /width: 180, flex: "none", cursor: "default"/.test(kvEditor) &&
      /width: 220, flex: "none", cursor: editable/.test(kvEditor)
  );
  // ⚠️ 必须是 **3 处**：值控件有三个分支（枚举下拉 / 账号多选 / 普通文本 Input）。
  // 上一轮只数 2 处 ⇒ 普通文本分支漏设宽度，它 `flex-grow` 撑满剩余空间，
  // 于是「值列依旧长短不一致」（用户实测报出）。按分支数钉死，漏一个即红。
  check(
    "参数值控件三个分支都固定 350px 且不被压缩",
    (kvEditor.match(/width: 350, flex: "none"/g) || []).length === 3
  );
  check(
    "参数行不用 Space.Compact（会把子项变可压缩 flex 项 ⇒ 两列不等宽）",
    !kvEditorCode.includes("<Space.Compact")
  );
  check("参数行没有 Tooltip（2026-10-07 用户口径「移除tip」）", !kvEditorCode.includes("<Tooltip"));
  check("Tooltip 不再被 import（真删不留影子）", !kvEditor.includes("Tooltip,"));
  check("执行账号 key 有展示名", kvEditor.includes('account_ids: "执行账号"'));
  // days（离线笔记同步时间区间）与 retention_days 都进展示名映射。
  check("时间区间 days 有展示名", kvEditor.includes('days: "同步天数'));
  check("保留期 retention_days 有展示名", kvEditor.includes('retention_days: "保留天数'));

  // --------------------------------------------------------------- //
  // 5b. 「执行一次」参数弹窗（设计 22 §5）
  // --------------------------------------------------------------- //
  const runModal = readSource(
    path.join(PROJECT, "frontend/src/components/schedule/run-job-modal.tsx")
  );
  // 关键语义：本次参数不写回任务配置（否则用户会以为改了定时配置）。
  check("执行一次弹窗说明「只对本次生效」", runModal.includes("只对本次执行生效"));
  check("执行一次弹窗复用共享 KV 编辑器", runModal.includes("ParamsEditor"));
  // 「执行一次」弹窗同一尺寸口径（形态一致，切来切去不像两个东西）
  check(
    "执行一次弹窗同一尺寸口径",
    runModal.includes('width="70%"') && runModal.includes('maxHeight: "70vh"')
  );
  // 2026-10-07：执行账号下拉在「执行一次」弹窗里同样可用（同一份注入形态）。
  check("账号数据源由调用方注入（执行一次弹窗）", runModal.includes("accounts={accounts}"));
  check("账号下拉的数据来自 fetchAccounts", runModal.includes('fetchAccounts("xhs")'));
  // 有可编辑参数才弹窗（无参数走 Popconfirm 二次确认）。
  check("任务中心按 params_editable 决定弹窗", page.includes("params_editable ?? []).length > 0"));
  check("无参数任务走二次确认", page.includes("<Popconfirm"));

  // --------------------------------------------------------------- //
  // 6. 列表列口径（用户 2026-09-24：撤下次/上次运行，加任务类型）
  // --------------------------------------------------------------- //
  check("任务列表有「任务类型」列", page.includes('"任务类型"'));
  check("不再展示「下次运行」列", !page.includes('"下次运行"'));
  check("不再展示「上次运行」列", !page.includes('"上次运行"'));
  // 日志筛选：任务类型下拉（默认「全部任务」）+ 任务下拉，均为 antd Select
  check("日志有任务类型下拉（默认全部任务）", page.includes("setLogTriggerType") && page.includes('label: "全部任务"'));
  check("日志任务筛选用 antd Select", !page.includes("<select"));
  check("执行账号列不在列表展示", !page.includes('"执行账号"'));

  // --------------------------------------------------------------- //
  // 7. 任务日志表列收敛为 6 项（R13，2026-09-24 用户口径）
  //    任务名称 / 任务类型 / 状态 / 触发方式 / 开始执行时间 / 任务进度
  //    其余信息全在详情弹窗。
  // --------------------------------------------------------------- //
  const logSection = page.slice(page.indexOf("const logColumns"), page.indexOf("const jobKeyOptions"));
  // 剥注释后的同一段（`in source` 型断言一律用它 —— DEF-056：解释性注释会命中自己
  // 写下的关键词；本轮注释里就出现了「进行中」三个字）
  const logSectionCode = pageCode.slice(
    pageCode.indexOf("const logColumns"),
    pageCode.indexOf("const jobKeyOptions")
  );
  // 进度条的唯一实现（2026-09-28 同日第三轮抽出，设计 13 §43）：列表列与详情弹窗共用，
  // 断言要在两个位置引用它 ⇒ 提前读，避免 TDZ。
  const progress = readSource(PROGRESS_TSX);
  const progressCode = progress.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  const css = readSource(GLOBAL_CSS);
  check("日志表有「任务名称」列", logSection.includes('title: "任务名称"'));
  check("日志表有「任务类型」列（快照口径）", logSection.includes('title: "任务类型"') && logSection.includes("job_trigger_type"));
  check("日志表有「状态」列", logSection.includes('title: "状态"'));
  check("日志表有「触发方式」列", logSection.includes('title: "触发方式"'));
  check("日志表有「开始执行时间」列", logSection.includes('title: "开始执行时间"'));
  // R13：一列合并「任务进度 + 耗时或异常」，进度用进度条渲染（**经唯一实现**）
  check("日志表有「任务进度」列", logSection.includes('title: "任务进度"'));
  check(
    "任务进度经唯一实现渲染（列内不再自绘 Progress）",
    logSectionCode.includes("<JobLogProgress") && !logSectionCode.includes("<Progress")
  );
  check("失败显异常 / 成功显耗时", logSection.includes("exception_info") && logSection.includes("duration_ms"));
  // 2026-09-27 用户口径「已执行的进度百分比保留」：失败**不切** antd `exception` 态
  // （该态会把百分比文字换成 ✗ 图标 ⇒ 用户看不到「跑到多少了」），红色改由 strokeColor 表达。
  // ⚠️ 该渲染已随唯一实现搬进 `job-log-progress.tsx` ⇒ 断言改读那个文件（意图不变）。
  check(
    "失败保留百分比：不切 antd exception 态",
    progressCode.includes('strokeColor={log.status === "failed"') &&
      !progressCode.includes('status={log.status === "failed"')
  );
  check(
    "失败语义由红色进度条 + 红色原因行表达",
    progressCode.includes("#ff4d4f") && logSection.includes("exception_info")
  );
  // 2026-09-28 用户口径「任务日志列表的任务进度列应展示任务进度条，而不是文本进行中」：
  // 该列**任何状态**都必须是进度条。⚠️ **同日第三轮**（用户实测报障「任务进度列的进度条
  // 自动达到了 100% 但是此时任务刚刚开始，点击查看详情发现详情弹窗中进度条 0%」）：
  // 进度条的渲染**抽成唯一实现** `JobLogProgress`（比例已知 / 未知两态都在里面），
  // 列表列与详情弹窗**共用** ⇒ 同一行不可能再出现两个读数；「比例未知」渲染
  // **不定量滑动条**（**没有固定填充比例**），不再拿满格条冒充 100%（设计 13 §43）。
  check("进度条有唯一实现（JobLogProgress）", progressCode.includes("export function JobLogProgress"));
  check(
    "列表列改用唯一实现（不再自绘进度条）",
    logSectionCode.includes("<JobLogProgress log={record} />") && !logSectionCode.includes("<Progress")
  );
  check(
    "比例未知的分支条件是「在跑且未上报进度」",
    progressCode.includes('log.status === "running" && !log.progress')
  );
  check(
    "比例未知渲染不定量滑动条（不是满格条）",
    progressCode.includes('className="log-progress-unknown"')
  );
  check("满格条冒充已作废（不许 percent={100}）", !progressCode.includes("percent={100}"));
  check("不定量条不显百分比数字（无 showInfo）", !progressCode.includes("showInfo"));
  check(
    "不定量条真有滑动动画（组件类名与 global.css 对得上）",
    css.includes(".log-progress-unknown-bar") &&
      css.includes("@keyframes log-progress-unknown-slide") &&
      progressCode.includes('className="log-progress-unknown-bar"')
  );
  check("进度列不再有「进行中」文字回落", !logSectionCode.includes("进行中"));
  // 已移出列表的列（全部进详情弹窗）
  check("日志表不再单列「触发器」", !logSection.includes('title: "触发器"'));
  check("日志表不再单列「错过策略」", !logSection.includes('title: "错过策略"'));
  check("日志表不再单列「并发」", !logSection.includes('title: "并发"'));
  check("日志表不再单列「耗时」", !logSection.includes('title: "耗时"'));
  check("日志表不再单列「异常」", !logSection.includes('title: "异常"'));
  // 日志详情弹窗（配置快照 + 明细 + 被移出列表的字段都在这里）
  check("日志表有「详情」操作", page.includes("setDetailLog"));
  const detail = readSource(
    path.join(PROJECT, "frontend/src/components/schedule/job-log-detail-modal.tsx")
  );
  check("日志详情弹窗展示配置快照", detail.includes("config_snapshot"));
  // 2026-09-28（同日第三轮）：弹窗的进度条**必须与列表列共用唯一实现** ——
  // 旧实现自己读 `log.progress ?? 0`，于是「在跑且后端还没上报」时弹窗画 0%、
  // 列表列画满格条（看起来 100%）⇒ 同一行两个相反读数（用户实测报障）。
  check(
    "日志详情弹窗的进度条与列表列同源（共用 JobLogProgress）",
    detail.includes("<JobLogProgress log={log}") && !detail.includes("<Progress")
  );
  check("日志详情弹窗含触发器/错过策略/并发", detail.includes("triggerSummary") && detail.includes("misfirePolicyLabel") && detail.includes("concurrentLabel"));
  // 2026-09-27 用户口径「账号信息卡点同步笔记，执行详情的执行账号依旧展示的是全部账号，
  // 执行账号应是当前账号信息卡的店铺」：定向执行（账号卡按钮）的那条日志带
  // `params_override` ⇒ 显示**那一个账号的名称**；定时 / 手动「执行一次」的日志没有
  // 这一层 ⇒ 仍按任务配置显示「全部账号 / N 个账号」。
  check(
    "执行账号优先取定向执行的账号快照（params_override）",
    detail.includes("params_override") &&
      detail.includes("targetAccountId") &&
      detail.includes("account_name")
  );
  check(
    "定向执行分支在前、「全部账号」只作回落",
    detail.indexOf("targetAccountId !== null ?") > -1 &&
      detail.indexOf("targetAccountId !== null ?") < detail.indexOf('"全部账号"')
  );
  check("账号名称缺失回落「1 个账号」（不猜名字）", detail.includes('targetAccountName || "1 个账号"'));

  // --------------------------------------------------------------- //
  // 8. R16（2026-09-24 用户口径）：任务类型**不用 Tag**，纯文本渲染；
  //    且表单三档标签与列表文案**同源一表**（不许各拼各的）。
  // --------------------------------------------------------------- //
  // 任务列表「任务类型」列：纯 Text，不得包 Tag
  const colSec = (section, title) => {
    const i = section.indexOf(`title: "${title}"`);
    return i < 0 ? "" : section.slice(i, i + 400);
  };
  const listTypeCol = colSec(page.slice(0, page.indexOf("const logColumns")), "任务类型");
  check("任务列表类型列用纯文本（R16 不用 Tag）", listTypeCol.includes("<Text>") && !listTypeCol.includes("<Tag>"));
  const logTypeCol = colSec(logSection, "任务类型");
  check("日志表类型列用纯文本（R16 不用 Tag）", logTypeCol.includes("<Text>") && !logTypeCol.includes("<Tag>"));
  // 表单三档标签走同源表：不得再出现硬编码的中文三档
  check(
    "表单三档标签与列表同源（triggerTypeFullLabel）",
    fields.includes('triggerTypeFullLabel("manual")') &&
      fields.includes('triggerTypeFullLabel("cron")') &&
      fields.includes('triggerTypeFullLabel("at")')
  );
  check(
    "表单不再硬编码三档全称",
    !/<Radio\.Button value="manual">手动执行</.test(fields)
  );
  // 日志筛选不得再出现已退役的 interval 档
  check("日志类型筛选已无 interval 档", !page.includes('value: "interval"'));

  // --------------------------------------------------------------- //
  // 9. R18 / R19 / R20（2026-09-24 用户口径）
  //    R18：调用目标支持全部目标（含已用过的）；R19：日志详情弹窗两块布局；
  //    R20：新建任务弹窗去掉「分组」。
  // --------------------------------------------------------------- //
  // R18-①：前端不再硬编码可建目标表（旧 CREATABLE_JOB_KEYS 只有 1 项）
  check("R18 调用目标不再硬编码 CREATABLE_JOB_KEYS", !form.includes("CREATABLE_JOB_KEYS"));
  // R18-②：目标清单来自后端端点
  check("R18 目标清单走后端端点", form.includes("fetchScheduleTargets"));
  check("api 有 fetchScheduleTargets", api.includes("export async function fetchScheduleTargets"));
  check("api 指向 /schedule/targets", api.includes('"/schedule/targets"'));
  // R18-③：已建过的目标仍可选 —— 只提示一句，不禁用
  check("R18 已建目标仍可选（提示而非禁用）", form.includes("existing_count > 0"));
  check("R18 不对已建目标置灰", !form.includes("disabled={selectedTarget.existing_count"));
  // 标签只显示目标名（不拼 key，不拼分组）
  check("R18 目标下拉 label 只有名称", form.includes("label: item.name,") && !form.includes("（${item.key}）"));

  // R20：分组字段整条移除（含那句 helper 文案）
  check("R20 新建弹窗无「分组」字段", !form.includes('label="分组"'));
  check("R20 不再展示分组 helper 文案", !form.includes("分组："));
  check("R20 不再 import jobGroupLabel", !form.includes("jobGroupLabel"));

  // R19：日志详情弹窗两段布局（执行日志信息 / 快照配置），各成一个区块
  check("R19 弹窗含「执行日志信息」区块", detail.includes("执行日志信息"));
  check("R19 弹窗含「快照配置」区块", detail.includes("快照配置"));
  check("R19 两块各成 Card（不再嵌套 Descriptions.Item）", (detail.match(/<Card/g) || []).length >= 2);
  check("R19 快照不再是嵌套的 Descriptions.Item", !detail.includes('label="配置快照"'));
  // R19：日志信息块在前（打开先看这次跑得怎么样）
  check(
    "R19 日志信息块在快照块之前",
    detail.indexOf("执行日志信息") < detail.indexOf("快照配置")
  );
  // R19 后续口径：详情弹窗宽度 **70%**
  check("R19 详情弹窗宽度 70%", detail.includes('width="70%"'));
  // 调用目标列**两行**（中文名 + 代码键）
  // ⚠️ 用剥注释后的 pageCode（列里加了说明注释，用 raw page 会因窗口截断而误判）
  const targetCol = colSec(pageCode.slice(0, pageCode.indexOf("const logColumns")), "调用目标");
  check("调用目标列两行展示（中文名 + 代码键）", targetCol.includes("targetNames[key]") && targetCol.includes("{key}"));
  check("调用目标列第一行是中文名", targetCol.indexOf("targetNames[key]") < targetCol.indexOf("Text code"));
  check("调用目标名来自 /schedule/targets", pageCode.includes("fetchScheduleTargets"));

  // 用户口径（2026-09-25）：「调用目标需要配置中文名称」—— **日志详情弹窗也要**。
  // 原 R18 口径只覆盖了任务列表列，弹窗里一直是裸代码键（`system.xxx` 这种），
  // 用户看到的正是那个缺口。两份展示点必须同口径、同一数据源。
  check(
    "弹窗调用目标两行（中文名 + 代码键）",
    detail.includes("targetNames[jobKey]") && detail.includes("{jobKey ||")
  );
  check(
    "弹窗调用目标第一行是中文名",
    detail.indexOf("targetNames[jobKey]") < detail.indexOf("{jobKey ||")
  );
  check(
    "弹窗中文名走 prop（不另写映射表）",
    detail.includes("targetNames?: Record<string, string>")
  );
  check("页面把 targets 传给弹窗", pageCode.includes("targetNames={targetNames}"));
  // 2026-10-07 用户口径「执行详情弹窗·快照配置区域的执行账号应展示**具体执行了哪些账号**」：
  // 原来只显「N 个账号」；现在逐条列名字。
  const jobLogDetail = readSource(
    path.join(PROJECT, "frontend/src/components/schedule/job-log-detail-modal.tsx")
  );
  const jobLogDetailCode = stripComments(jobLogDetail);
  check("详情弹窗接收账号名映射（accountNames prop）", jobLogDetail.includes("accountNames?: AccountNameMap"));
  check("页面把账号名传给详情弹窗", pageCode.includes("accountNames={accountNames}"));
  check("页面用 lib 的 accountNameMap（不另写映射）", pageCode.includes("accountNameMap(await fetchAccounts("));
  check(
    "执行账号逐条列出具体账号名",
    jobLogDetailCode.includes("accountIds.map(") && jobLogDetailCode.includes("accountNames[String(id)]")
  );
  // 2026-10-07 用户口径「只展示账号名称不展示 id」：名字后面**不许**再挂 `（#id）`。
  check("账号名后不带 id 后缀", !jobLogDetailCode.includes("（#"));
  check(
    "缺名退 `账号 #id`（兜底，缺值不猜）",
    jobLogDetailCode.includes("accountNames[String(id)] || `账号 #${id}`")
  );
  check(
    "空 account_ids 仍显「全部账号」（不猜具体是谁）",
    jobLogDetailCode.includes('"全部账号"')
  );
  check("详情弹窗仍显示账号总数（共 N 个账号）", jobLogDetailCode.includes("共 {accountIds.length} 个账号"));

  // 🔄 2026-10-07 用户口径**取代** 2026-09-24 的「一律可删 / 不挂内置 Tag」：
  // 「定时任务新增表字段是否为内置任务，列表新增展示，内容任务不允许删除，前后端添加校验逻辑」。
  // ⚠️ 用上面已定义的 `pageCode`（已剥注释）
  check("任务列表有「任务属性」列（展示是否内置）", pageCode.slice(0, pageCode.indexOf("const logColumns")).includes('title: "任务属性"'));
  check("内置任务渲染「系统内置」Tag", pageCode.includes("系统内置"));
  check("非内置任务渲染「自定义」Tag", pageCode.includes("自定义"));
  check("内置任务的删除按钮置灰", pageCode.includes("actionLoading || record.is_builtin"));
  check("内置任务删除给出原因（Tooltip）", pageCode.includes("系统内置任务不可删除"));
  check("内置任务不给删除确认弹窗", pageCode.includes("disabled={record.is_builtin}"));

  //: `colSec` 的入参（不依赖被取代的 `nameCol`）。
  // 用户口径（2026-09-24）②：任务**分组**整条移除（列表不再有分组列）
  const listSec = pageCode.slice(0, pageCode.indexOf("const logColumns"));
  check("任务列表无「分组」列", !listSec.includes('title: "分组"'));
  check("页面不再 import jobGroupLabel", !pageCode.includes("jobGroupLabel"));
  check("页面不再引用 job_group 列", !listSec.includes("job_group"));

  // R20/R22 加强（2026-09-24 用户口径「**数据库物理删除**」）：分组不只是从 UI 藏起来，
  // 而是**连列带逻辑一起退**（迁移 k1l2m3n4o5p6 —— 与本轮真实库实跑对应）。
  // 下面这几条把「物理删除」钉死，防止它被悄悄改回「只去 UI、列还在」。
  // ⚠️ **锚点必须收紧到 schedule 区域**：原先查整个 `api.ts` 全文，
  // 而 2026-10-03 商品详情图加了 `regenerate?: { group: string; index: number }`
  // —— 它含 `group: string` 字面量 ⇒ 这条断言被**无关字段撞成假红**。
  // 收紧为「schedule 目标类型声明区」（`ScheduleTarget` 那一段）后不再误伤。
  const scheduleTypeSec = (() => {
    const i = api.indexOf("ScheduleTarget");
    return i >= 0 ? api.slice(i, i + 1200) : "";
  })();
  check(
    "R22 目标清单类型里没有 group 字段",
    !scheduleTypeSec.includes('group: "DEFAULT"') && !scheduleTypeSec.includes("group: string")
  );
  check("R22 日志详情弹窗不再取 snapshot.job_group", !detail.includes("snapshot.job_group"));
  check("R22 schedule 模块不再导出 JOB_GROUP_LABELS", !scheduleCode.includes("JOB_GROUP_LABELS"));
  check("R22 schedule 模块不再导出 jobGroupLabel", !scheduleCode.includes("export function jobGroupLabel"));

  // 用户口径（2026-09-25）③：**一切提示走 antd message 轻提示**，页面不留 inline
  // 消息条、也不再弹 notice 结果弹窗（弹窗遮罩比消息条更占空间；设计 04 §3.6）。
  // ⚠️ 取代 2026-09-24 的「一切提示走弹窗」口径，原断言留档：
  //   check("结果提示统一走 notice 弹窗", pageCode.includes("notify(") && pageCode.includes("open={notice !== null}"));
  check("页面无 inline Alert 消息条", !pageCode.includes("<Alert"));
  check("页面不再 import Alert", !page.includes("  Alert,"));
  check("结果提示统一走 antd message", pageCode.includes("App.useApp()") && !pageCode.includes("notify(") && !pageCode.includes("notice"));
  check("不再有 message 状态（旧 inline 条残留）", !pageCode.includes("setMessage"));
  check("不再有 notice 弹窗（2026-09-24 R22 残留）", !pageCode.includes("setNotice") && !pageCode.includes("open={notice"));
  check("不再有 jobNotice（旧启停弹窗残留）", !pageCode.includes("setJobNotice"));

  if (failures > 0) {
    console.log(`\n${failures} check(s) FAILED`);
    process.exitCode = 1;
    return;
  }
  console.log("\nAll checks PASS");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
