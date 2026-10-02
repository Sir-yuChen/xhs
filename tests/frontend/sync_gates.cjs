/**
 * 行为验证：同步按钮的**三态门禁**与**结果行**（设计 13 §19）。
 *
 * 盯的五件事（都是「不许出现某种行为」，源码断言证明不了）：
 *
 * 1. **全不可用必须拦住**：www / ark / creator 全「确定不可用」时不发起同步，
 *    弹窗提示分别登录（用户 2026-09-21 原话）；
 * 2. **`unknown` 必须放行**：没检测 ≠ 不可用（DEF-057 红线）—— 把 unknown
 *    算进「不可用」会让用户去做一次根本不需要的重登；
 * 3. **分源摘要不许吞错误**：某个源失败时结果行必须带上原因（后端给了，
 *    前端抹掉 = 用户失去自救能力，DEF-058 同型）；
 * 4. **缺值渲染 `-` 不渲染 0**：`detail_total` 取不到时「不知道还剩多少」
 *    与「还剩 0 件」是两件事（硬约束⑤）；
 * 5. **两条门禁互不牵连**（**§44**，2026-09-30 用户拍板拆分）：「同步笔记」
 *    只认 creator、「同步商品笔记」只认 ark —— 合并成一个计划对象必然出现
 *    「一个平台不可用却拦住另一个按钮」，正是拆分要修的语义。
 *
 * 做法与 `login_state.cjs` 同源：esbuild 打包**真实** `lib/sync-gates.ts`，
 * 直接调用它（纯模块，不需要 antd 桩、不需要 jsdom）。
 *
 * 运行：
 *   <node> tests/frontend/sync_gates.cjs                  # 期望全 PASS
 *   SYNC_GATES_MUTATE=blocked   <node> …                  # 期望 FAIL
 *   SYNC_GATES_MUTATE=unknown   <node> …                  # 期望 FAIL
 *   SYNC_GATES_MUTATE=needslink <node> …                  # 期望 FAIL
 *   SYNC_GATES_MUTATE=source    <node> …                  # 期望 FAIL
 *   SYNC_GATES_MUTATE=dash      <node> …                  # 期望 FAIL
 *   SYNC_GATES_MUTATE=goodsgate <node> …                  # 期望 FAIL
 *   SYNC_GATES_MUTATE=cancel    <node> …                  # 期望 FAIL（§24.8）
 *   SYNC_GATES_MUTATE=terminalnotice <node> …             # 期望 FAIL（§35.18）
 *   SYNC_GATES_MUTATE=progressdash <node> …               # 期望 FAIL（§35.18.6）
 *   SYNC_GATES_MUTATE=cancelsuffix <node> …               # 期望 FAIL（§35.18.6）
 *   SYNC_GATES_MUTATE=arkleak   <node> …                  # 期望 FAIL（§44）
 *   SYNC_GATES_MUTATE=goodsnotegate <node> …              # 期望 FAIL（§44）
 *   SYNC_GATES_MUTATE=goodsnotesrc <node> …               # 期望 FAIL（§44）
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/sync-gates.ts");

const MUTATE = process.env.SYNC_GATES_MUTATE || "";

// 变异锚点（必须是源码里**唯一**的串；行尾归一化防 CRLF 失配 —— 失配的
// 表现是「变异静默不生效」，脚本照常全绿，比变红更危险）。
const RESERVED = {
  blocked: "    blocked: !creatorOk,",
  unknown: 'export const UNAVAILABLE_STATES = ["missing", "guest", "expired"];',
  wwwleak: "  if (creatorOk) {",
  source: "        if (entry.error) {",
  limited: "        if (entry.limited) {",
  dash: 'const remaining = result.detail_total == null ? "-" : String(result.detail_total);',
  goodsgate: "    blocked: channelUnavailable(channels, \"ark\"),",
  cancel: '  if (result.status === "cancelled") {',
  // H 组（§35.17.4 首查驱动轮询 / §35.18 终态提示）—— 2026-09-27
  phaseactive: "  if (SYNC_ACTIVE_STATES.includes(status.status)) {",
  phaseidle: '  return status.last_result ? "finished" : "idle";',
  terminalnotice: '  if (!result || result.status === "done") {',
  // I 组（§35.18.6 进度分母三态 / 取消说明剥离）—— 2026-09-27 用户报障
  progressdash: "  if (total == null) {",
  cancelsuffix: '    const note = (result.note || "").replace(/(^|；)已被用户取消$/, "");',
  // J 组（§44：普通笔记 / 商品笔记两条独立门禁）—— 2026-09-30 用户拍板拆分
  arkleak: "  if (creatorOk) {",
  goodsnotegate: "    blocked: gate.blocked,",
  goodsnotesrc: '    sources: gate.blocked ? [] : ["ark"],',
};

const MUTATE_TO = {
  // 创作者会话不可用也放行 ⇒ 硬门禁失效，用户点了才由后端吼
  blocked: "    blocked: false,",
  // unknown 算「不可用」⇒ DEF-057 红线
  unknown: 'export const UNAVAILABLE_STATES = ["missing", "guest", "expired", "unknown"];',
  // www 泄漏进源清单（creator 不可用也列源）⇒ 退化成旧的三源契约
  wwwleak: "  if (true) {",
  // 分源摘要吞掉失败原因 ⇒ 用户看不到「网页版：XSEC_TOKEN_REQUIRED」
  source: "        if (false && entry.error) {",
  // 被限流也显示成「N 条」⇒ 把「没跑完」伪装成「没数据」（§19.11 真机教训）
  limited: "        if (false && entry.limited) {",
  // 缺值渲染 0 而不是 `-`
  dash: 'const remaining = result.detail_total == null ? "0" : String(result.detail_total);',
  // 千帆不可用也放行同步商品
  goodsgate: "    blocked: false,",
  // 取消分支失效 ⇒ cancelled 退回「上次同步 …」误导用户（§24.8）
  cancel: '  if (false && result.status === "cancelled") {',
  // 「进行中」判据失效 ⇒ 在跑的任务被当成结束，首查当场把账号摘出轮询集
  phaseactive: "  if (false && SYNC_ACTIVE_STATES.includes(status.status)) {",
  // 已结束一律当 idle ⇒ 轮询停不下来（对着不会再变的任务无限 5s 轮询）
  phaseidle: '  return "idle";',
  // 成功也弹提示 ⇒ 每次同步完都打扰用户（用户口径：只留按钮态「同步中」）
  terminalnotice: '  if (!result) {',
  // 分母缺失也照渲染 ⇒ 回到「已落库 15 / 共 null 条」（用户读成「卡住了」）
  progressdash: "  if (false) {",
  // 不剥取消后缀 ⇒ 结果行出现两次「取消」（「已取消 · …；已被用户取消」）
  cancelsuffix: '    const note = result.note || "";',
  // ark 泄漏回「同步笔记」的源清单 ⇒ 又变回混合双源（§44 拆分白做）
  arkleak:
    '  if (creatorOk) {\n    if (!channelUnavailable(channels, "ark")) sources.push("ark");',
  // 商品笔记的硬门禁失效 ⇒ creator 会话不可用的账号也能点（点了必失败）
  goodsnotegate: "    blocked: false,",
  // 商品笔记把 creator 源混进来 ⇒ 又回到「一次任务跑两个源」的旧语义
  goodsnotesrc: '    sources: gate.blocked ? [] : ["ark", "creator"],',
};

const mutatePlugin = {
  name: "sync-gates-mutate",
  setup(build) {
    build.onLoad({ filter: /lib[\\/]sync-gates\.ts$/ }, (args) => {
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
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "sync-gates-"));
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
  const outFile = path.join(tmpDir, "sync-gates.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const mod = require(outFile);
  const { noteSyncPlan, goodsGate, syncResultText, goodsResultText, channelUnavailable } = mod;
  const { syncTaskPhase, syncTerminalNotice, syncProgressText, goodsNoteSyncPlan } = mod;

  if (typeof noteSyncPlan !== "function") {
    console.error("FATAL 未导出 noteSyncPlan —— 若已重命名，请同步更新本脚本");
    return 1;
  }
  if (typeof goodsNoteSyncPlan !== "function") {
    console.error("FATAL 未导出 goodsNoteSyncPlan —— §44 拆分的商品笔记门禁丢了？");
    return 1;
  }

  // 构造登录态（loginChannelRows 的输入形状：三个平台 + view.state）
  const channels = (states) => ({
    www: { state: states.www, reason: "", label: "网页版（PC）" },
    creator: { state: states.creator, reason: "", label: "创作者平台" },
    ark: { state: states.ark, reason: "", label: "千帆商家后台" },
  });
  const missing = { www: "missing", creator: "missing", ark: "missing" };
  const unknown = { www: "unknown", creator: "unknown", ark: "unknown" };

  // A. **创作者平台会话是唯一硬门禁**（§21.4，2026-09-22 用户口径：
  //    「同步笔记按钮该账号必须存在有效的创作者登录会话，不存在有效创作者平台
  //    登录会话的提示补齐后再触发」）
  check(
    "A1 creator 不可用 ⇒ blocked（哪怕 www 是 valid）",
    noteSyncPlan(channels({ www: "valid", creator: "missing", ark: "valid" }), "creator").blocked,
    true
  );
  check(
    "A2 guest / expired 也算确定不可用 ⇒ blocked",
    noteSyncPlan(channels({ www: "valid", creator: "expired", ark: "valid" }), "creator").blocked,
    true
  );

  // B. **unknown 放行**（DEF-057：没查到 ≠ 不可用）
  check(
    "B1 creator unknown ⇒ 放行（让同步真发一次）",
    noteSyncPlan(channels(unknown), "creator").blocked,
    false
  );

  // C. 源清单（§21.4：**www 源已退场**，且 §44 起 **ark 也退场** —— 普通笔记只有 creator）
  check(
    "C1 只 creator 可用 ⇒ sources=[creator]",
    noteSyncPlan(channels({ www: "expired", creator: "valid", ark: "expired" }), "creator"),
    { blocked: false, needsLink: false, sources: ["creator"] }
  );
  check(
    "C2 **ark 不再混进普通笔记**（§44）：creator + ark 都可用 ⇒ 仍只列 [creator]",
    noteSyncPlan(channels({ www: "expired", creator: "valid", ark: "valid" }), "creator").sources,
    ["creator"]
  );
  check(
    "C3 **www 永不参与**：creator 不可用 ⇒ blocked + 空 sources（www valid 也一样）",
    noteSyncPlan(channels({ www: "valid", creator: "missing", ark: "valid" }), "creator"),
    { blocked: true, needsLink: false, sources: [] }
  );
  check(
    "C4 needsLink 恒 false（主页链接录入窗已停用）",
    noteSyncPlan(channels({ www: "valid", creator: "valid", ark: "valid" }), "pc").needsLink,
    false
  );

  // D. 同步商品门禁（§19.4）
  check("D1 ark 确定不可用 ⇒ blocked", goodsGate(channels(missing)).blocked, true);
  check("D2 ark valid ⇒ 放行", goodsGate(channels({ ...unknown, ark: "valid" })).blocked, false);
  check(
    "D3 ark unknown ⇒ 放行（让同步真发一次，失败原因后端给）",
    goodsGate(channels(unknown)).blocked,
    false
  );
  check(
    "D4 失效与未登录的状态要能区分（文案按它二选一）",
    [goodsGate(channels({ ...unknown, ark: "expired" })).arkState,
     goodsGate(channels({ ...unknown, ark: "missing" })).arkState],
    ["expired", "missing"]
  );

  // E. 结果行：分源摘要必须带上失败的源（不许吞）
  // ⚠️ 2026-09-27 §35.18.6 **取代**了「分母缺失渲染 `/ 共 -`」的旧断言：分母不可知
  //    （增量 / 区间同步会早停）时只报「已落库 N 条」—— 把「未知」渲染成占位符会被
  //    读成「卡住了」，正是报障里用户反复取消同步的诱因。**旧原句保留在版本历史里**。
  check(
    "E1 分源摘要：成功的报条数、失败的带原因",
    syncResultText(
      {
        status: "done",
        progress: { stored: 2, total: null },
        note: "",
        sources: [
          { source: "www", stored: 2, total: 30, error: "" },
          { source: "ark", stored: 0, total: 0, error: "ARK_NOTE_LIST_FAILED: 登录凭证缺失" },
        ],
      },
      "条"
    ),
    "上次同步 已落库 2 条 · 网页版 2 条；千帆商品笔记：ARK_NOTE_LIST_FAILED: 登录凭证缺失"
  );
  check(
    "E2 失败任务整体：错误原因必须在行里",
    syncResultText({ status: "failed", progress: null, note: "", error: "NOTE_SOURCES_UNAVAILABLE: …" }, "条"),
    "同步失败：NOTE_SOURCES_UNAVAILABLE: …"
  );
  check(
    "E3 被限流的源：说「未跑完」，不许显示成 0 条",
    syncResultText(
      {
        status: "done",
        progress: { stored: 10, total: 1961 },
        note: "",
        sources: [
          { source: "www", stored: 10, total: null, error: "", limited: false },
          { source: "ark", stored: 0, total: 1960, error: "", limited: true },
        ],
      },
      "条"
    ),
    "上次同步 已落库 10 / 共 1961 条 · 网页版 10 条；千帆商品笔记：被限流未跑完"
  );
  // E4/E5（§24.8）：取消是终态的一种 + 耗时展示（后端算好 duration_ms）
  check(
    "E4 cancelled：说「已取消」并带耗时（不回退「同步已结束」）",
    syncResultText(
      { status: "cancelled", progress: { stored: 3, total: 10 }, note: "已被用户取消", error: "", duration_ms: 125000 },
      "条"
    ),
    "已取消 · 耗时 2 分 5 秒"
  );
  check(
    "E5 终态带耗时；duration_ms 缺值时不拼（不渲染 0 秒）",
    [
      syncResultText({ status: "done", progress: { stored: 2, total: null }, note: "", duration_ms: 4200 }, "条"),
      syncResultText({ status: "done", progress: { stored: 2, total: null }, note: "" }, "条"),
    ],
    ["上次同步 已落库 2 条 · 耗时 4 秒", "上次同步 已落库 2 条"]
  );

  // F. 商品结果行：详情进度 + 缺值渲染 `-`（不渲染 0）
  check(
    "F1 详情剩余取不到 ⇒ 渲染 - 不渲染 0",
    goodsResultText({
      status: "done",
      progress: { stored: 3, total: null },
      note: "",
      detail_done: 3,
      detail_total: null,
    }),
    "上次同步 已落库 3 件 · 规格详情 3 件（剩余 -）"
  );
  check(
    "F2 详情剩余有值 ⇒ 照实显示",
    goodsResultText({
      status: "done",
      progress: { stored: 3, total: null },
      note: "",
      detail_done: 2,
      detail_total: 3,
    }),
    "上次同步 已落库 3 件 · 规格详情 2 件（剩余 3）"
  );
  check(
    "F3 没跑详情段（detail_done 缺失）⇒ 不拼详情行",
    goodsResultText({ status: "done", progress: { stored: 1, total: null }, note: "" }),
    "上次同步 已落库 1 件"
  );

  // G. 门禁判据与 v1.31 同一条尺子（www / ark 共用）
  check(
    "G1 www 与 ark 用同一个判据（expired 两边都拦）",
    [channelUnavailable(channels({ ...unknown, www: "expired" }), "www"),
     channelUnavailable(channels({ ...unknown, ark: "expired" }), "ark")],
    [true, true]
  );

  // H. **首查驱动轮询**（§35.17.4，2026-09-27 用户口径）：
  //    「查一次任务记录表当前账号存在对应任务则展示页签，任务非结束状态（包括正常
  //      结束和非正常结束）则前端 5 秒查询一次，如果为结束状态则不再轮询查询，
  //      每次刷新按钮或进入页面时自动触发一次，**根据第一个获取的信息决定是否需要
  //      轮询**」。
  //
  //    这一组盯的是「要不要继续打后端」这个决策 —— 判错的两端都有害：
  //    把在跑的当结束 ⇒ 按钮永久停在「同步中」（H9 那类「看着像在跑其实没更新」）；
  //    把结束的当在跑 ⇒ 对一个不会再变的任务无限 5s 轮询，纯浪费且违反用户口径。
  check("H1 没有状态响应 ⇒ idle（不展示、不轮询）", syncTaskPhase(null), "idle");
  check(
    "H2 running ⇒ active（继续 5s 轮询）",
    syncTaskPhase({ task_id: 7, status: "running", progress: { stored: 3, total: 10 } }),
    "active"
  );
  check(
    "H3 pending ⇒ active（排队中也是未结束）",
    syncTaskPhase({ task_id: 7, status: "pending", progress: null }),
    "active"
  );
  check(
    "H4 任务已结束（状态端点回 idle、结果在 last_result）⇒ finished（停止轮询）",
    syncTaskPhase({
      task_id: null,
      status: "idle",
      last_result: { task_id: 7, status: "done", progress: { stored: 10, total: 10 }, note: "", error: "" },
    }),
    "finished"
  );
  check(
    "H5 没有进行中也没有历史 ⇒ idle（用户口径的「不存在对应任务」）",
    syncTaskPhase({ task_id: null, status: "idle", last_result: null }),
    "idle"
  );
  check(
    "H6 **非正常结束**（failed）也算结束 ⇒ finished（不再轮询）",
    syncTaskPhase({
      task_id: null,
      status: "idle",
      last_result: { task_id: 8, status: "failed", progress: null, note: "", error: "NOTE_SOURCES_UNAVAILABLE: …" },
    }),
    "finished"
  );
  check(
    "H7 取消（cancelled）也是终态 ⇒ finished",
    syncTaskPhase({
      task_id: null,
      status: "idle",
      last_result: { task_id: 9, status: "cancelled", progress: { stored: 3, total: 10 }, note: "已被用户取消", error: "" },
    }),
    "finished"
  );

  // H8–H11：终态提示（§35.18，2026-09-27 用户口径「进度卡移除，只留按钮态」）——
  //    卡片里不再有进度区块 ⇒ 非正常终态**必须**由轻提示报出真原因，
  //    成功则**不许**打扰（否则每次同步完都弹一条，纯噪声）。
  check(
    "H8 成功（done）⇒ 空串（不打扰；进度与结果去任务中心「任务日志」看）",
    syncTerminalNotice(
      { status: "done", progress: { stored: 10, total: 10 }, note: "", error: "" },
      "条",
      syncResultText
    ),
    ""
  );
  check(
    "H9 失败（failed）⇒ 带真原因（静默吞掉失败 = 用户点了半天不知道发生了什么）",
    syncTerminalNotice(
      { status: "failed", progress: null, note: "", error: "NOTE_SOURCES_UNAVAILABLE: …" },
      "条",
      syncResultText
    ),
    "同步失败：NOTE_SOURCES_UNAVAILABLE: …"
  );
  check(
    "H10 取消（cancelled）也算没成功 ⇒ 说「已取消」并带耗时",
    syncTerminalNotice(
      { status: "cancelled", progress: { stored: 3, total: 10 }, note: "已被用户取消", error: "", duration_ms: 125000 },
      "条",
      syncResultText
    ),
    "已取消 · 耗时 2 分 5 秒"
  );
  check(
    "H11 没有结果（null）⇒ 空串（首查/轮询拿不到 last_result 时不许凭空报一句）",
    [syncTerminalNotice(null, "条", syncResultText), syncTerminalNotice(undefined, "条", syncResultText)],
    ["", ""]
  );

  // ---- I 组：进度分母三态 + 取消说明剥离（§35.18.6，2026-09-27 用户报障） ----
  //   用户原话：「最新包中的笔记同步后，详情弹窗内全部是：列表源不带正文……
  //   同步笔记为何出现这些场景，数据不全」。根因是**任务被取消 ⇒ 收尾段整段没跑**，
  //   而诱因之一是**进度条恒 0%**（分母取了账号总量 1951，区间同步却只落个位数）
  //   ⇒ 用户以为卡死而反复取消。这里盯四件事：
  //   ① 只有**无界全量**才报分母；② 有界（total 缺失）**不许**渲染「/ 共 -」；
  //   ③ 详情补齐阶段优先报它；④ 取消结果行**不许**把「已被用户取消」后缀漏出来。
  check(
    "I1 无界全量 ⇒ 报分母（全量会翻到底，分母真实）",
    syncProgressText({ stored: 15, total: 1951 }, "条"),
    "已落库 15 / 共 1951 条"
  );
  check(
    "I2 有界同步（total 缺失）⇒ 只报已落库，**不渲染「/ 共 -」**",
    syncProgressText({ stored: 15, total: null }, "条"),
    "已落库 15 条"
  );
  check(
    "I3 详情补齐阶段优先（那一段才是「还要等多久」）",
    syncProgressText({ stored: 15, total: null, detail_done: 3, detail_total: 13 }, "条"),
    "已落库 15 条 · 详情补齐 3 / 13"
  );
  check(
    "I4 取消 + 业务说明 ⇒ 剥掉「已被用户取消」后缀，说明保留（不重复说「取消」）",
    syncResultText(
      {
        status: "cancelled",
        progress: { stored: 10 },
        note: "正文与图集待补 13 条，再次同步可继续补；已被用户取消",
        error: "",
        duration_ms: 23000,
      },
      "条"
    ),
    "已取消 · 正文与图集待补 13 条，再次同步可继续补 · 耗时 23 秒"
  );
  check(
    "I5 取消只有后缀（没有业务说明）⇒ 只留「已取消」（既有行为不回归）",
    syncResultText(
      { status: "cancelled", progress: { stored: 3, total: 10 }, note: "已被用户取消", error: "", duration_ms: 125000 },
      "条"
    ),
    "已取消 · 耗时 2 分 5 秒"
  );

  // ---- J 组：两条门禁**互不牵连**（§44，2026-09-30 用户拍板拆分） ----
  //   用户诉求：把「同步笔记」拆成「同步普通笔记」+「同步商品笔记」两个独立任务与入口，
  //   在账号信息卡上分开展示、分开触发。拆分的**语义核心**就是这一组：
  //   ① 商品笔记的硬门禁是 **ark**（不是 creator）；
  //   ② 它只跑 ark 源（把 creator 混进来 = 又回到旧的多源任务）；
  //   ③ 两条链路**各自独立**：creator 挂了不该拦住商品笔记，反之亦然
  //      （合并成一个计划对象必然在其中一边错，这正是要防的）。
  check(
    "J1 商品笔记：ark 不可用 ⇒ blocked（哪怕 creator 是 valid）",
    goodsNoteSyncPlan(channels({ www: "valid", creator: "valid", ark: "missing" })).blocked,
    true
  );
  check(
    "J2 商品笔记：ark unknown ⇒ 放行（DEF-057，让同步真发一次）",
    goodsNoteSyncPlan(channels(unknown)).blocked,
    false
  );
  check(
    "J3 商品笔记：ark 可用 ⇒ sources 只含 [ark]（creator 不混进来，§44）",
    goodsNoteSyncPlan(channels({ www: "expired", creator: "expired", ark: "valid" })),
    { blocked: false, needsLink: false, sources: ["ark"] }
  );
  check(
    "J4 **两条链路互不牵连**：creator 挂 / ark 通 ⇒ 笔记 blocked、商品笔记放行",
    [
      noteSyncPlan(channels({ www: "valid", creator: "expired", ark: "valid" }), "creator").blocked,
      goodsNoteSyncPlan(channels({ www: "valid", creator: "expired", ark: "valid" })).blocked,
    ],
    [true, false]
  );

  console.log(`\n${passed} PASS / ${failed} FAIL`);
  return failed === 0 ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error(error);
  process.exit(1);
});
