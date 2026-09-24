/**
 * 行为验证：同步按钮的**三态门禁**与**结果行**（设计 13 §19）。
 *
 * 盯的四件事（都是「不许出现某种行为」，源码断言证明不了）：
 *
 * 1. **全不可用必须拦住**：www / ark / creator 全「确定不可用」时不发起同步，
 *    弹窗提示分别登录（用户 2026-09-21 原话）；
 * 2. **`unknown` 必须放行**：没检测 ≠ 不可用（DEF-057 红线）—— 把 unknown
 *    算进「不可用」会让用户去做一次根本不需要的重登；
 * 3. **分源摘要不许吞错误**：某个源失败时结果行必须带上原因（后端给了，
 *    前端抹掉 = 用户失去自救能力，DEF-058 同型）；
 * 4. **缺值渲染 `-` 不渲染 0**：`detail_total` 取不到时「不知道还剩多少」
 *    与「还剩 0 件」是两件事（硬约束⑤）。
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

  if (typeof noteSyncPlan !== "function") {
    console.error("FATAL 未导出 noteSyncPlan —— 若已重命名，请同步更新本脚本");
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

  // C. 源清单（§21.4：**www 源已退场**，creator 强制）
  check(
    "C1 只 creator 可用 ⇒ sources=[creator]",
    noteSyncPlan(channels({ www: "expired", creator: "valid", ark: "expired" }), "creator"),
    { blocked: false, needsLink: false, sources: ["creator"] }
  );
  check(
    "C2 creator + ark 可用 ⇒ sources=[creator, ark]",
    noteSyncPlan(channels({ www: "expired", creator: "valid", ark: "valid" }), "creator").sources,
    ["creator", "ark"]
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
    "上次同步 已落库 2 / 共 - 条 · 网页版 2 条；千帆商品笔记：ARK_NOTE_LIST_FAILED: 登录凭证缺失"
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
    ["上次同步 已落库 2 / 共 - 条 · 耗时 4 秒", "上次同步 已落库 2 / 共 - 条"]
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
    "上次同步 已落库 3 / 共 - 件 · 规格详情 3 件（剩余 -）"
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
    "上次同步 已落库 3 / 共 - 件 · 规格详情 2 件（剩余 3）"
  );
  check(
    "F3 没跑详情段（detail_done 缺失）⇒ 不拼详情行",
    goodsResultText({ status: "done", progress: { stored: 1, total: null }, note: "" }),
    "上次同步 已落库 1 / 共 - 件"
  );

  // G. 门禁判据与 v1.31 同一条尺子（www / ark 共用）
  check(
    "G1 www 与 ark 用同一个判据（expired 两边都拦）",
    [channelUnavailable(channels({ ...unknown, www: "expired" }), "www"),
     channelUnavailable(channels({ ...unknown, ark: "expired" }), "ark")],
    [true, true]
  );

  console.log(`\n${passed} PASS / ${failed} FAIL`);
  return failed === 0 ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error(error);
  process.exit(1);
});
