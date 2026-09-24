/**
 * 行为验证：登录态的**呈现映射**必须把 `unknown` 与 `expired` 分开。
 *
 * 背景（设计 13 §18.5）：登录态有五层取值 `missing / guest / valid / expired / unknown`。
 * 其中 `unknown` 表示「**存了、是登录态，但还没检测**」—— 把它是渲染成
 * 「已失效」就是 DEF-057 的同型红线：用「没检查」冒充「不可用」，用户会去做一次
 * 根本不需要的重登。
 *
 * 为什么不能只用源码断言：把 `unknown` 的 label 改成「已失效」，源码里照样能找到
 * `unknown` 这三个字母 —— `"unknown" in source` 依然为真。规则是「**不许出现某种映射**」，
 * 只有真实驱动映射函数才能证明它没被写坏。做法与 `detail_message.cjs` 同源：
 * esbuild 打包**真实** `lib/login-state.ts`，直接调用它。
 *
 * 2026-09-21 三平台拆分（用户拍板）：千帆与创作者平台**不再是同一份令牌**
 * （真机实测：创作者 jar 打千帆一律 `-13000`），于是这里是**三个** tag：
 * 千帆（ark）/ 创作者（creator）/ 网页版（www）。
 *
 * 运行（纯模块，不需要 antd 桩、不需要 jsdom）：
 *
 *   <node> tests/frontend/login_state.cjs                    # 期望 20 PASS
 *   <node> tests/frontend/login_state.cjs --self-check       # 一次进程跑完契约+全部变异（pytest 用这个）
 *   LOGIN_STATE_MUTATE=expired <node> tests/frontend/login_state.cjs   # 期望 FAIL
 *   LOGIN_STATE_MUTATE=label   <node> tests/frontend/login_state.cjs   # 期望 FAIL
 *   LOGIN_STATE_MUTATE=order   <node> tests/frontend/login_state.cjs   # 期望 FAIL
 *   LOGIN_STATE_MUTATE=missing <node> tests/frontend/login_state.cjs   # 期望 FAIL
 *   LOGIN_STATE_MUTATE=short   <node> tests/frontend/login_state.cjs   # 期望 FAIL
 *   LOGIN_STATE_MUTATE=shortlong <node> tests/frontend/login_state.cjs # 期望 FAIL
 *   LOGIN_STATE_MUTATE=tip     <node> tests/frontend/login_state.cjs   # 期望 FAIL
 *
 * 七个变异开关正是「守卫承重」的证据：
 *   expired   —— 未识别取值退化成「已失效」（替平台宣布令牌已死）；
 *   label     —— `unknown` 的文案与 `expired` 撞车（红线本身）；
 *   order     —— 平台顺序被打乱（千帆不再排第一）；
 *   missing   —— 字典缺项被当成 `unknown`（「没有这个平台」被说成「没检测」）；
 *   short     —— 三个平台的短名撞车（tag 长得一样，分不清哪个是千帆）；
 *   shortlong —— 短名等于全名（tag 里塞不下，把昵称挤没）；
 *   tip       —— 悬停说明又把后端长原因塞回去（用户口径：tip 太长了）。
 *
 * `--self-check`（2026-09-25 提速，见调研报告 P0-4）：契约 + 全部变异在**一个 node 进程**里
 * 依次跑完，输出 `SELF <variant> OK|BROKEN <passed>/<failed>` 摘要行。原来是每个变异起一个
 * 进程（6 次 node 冷启动 + 6 次 esbuild），单文件相关用例占全量回归约 100s；合并后约 30s。
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const MODULE_TS = path.join(PROJECT, "frontend/src/lib/login-state.ts");

const RESERVED = {
  expired: '  return "unknown";\n}',
  label: 'label: "未检测"',
  order: 'export const LOGIN_CHANNEL_ORDER: string[] = ["ark", "creator", "www"];',
  missing: "        view: VIEWS.missing,",
  short: '  ark: "千帆",',
  shortlong: '  www: "网页版",',
  tip: "  const head = `${row.shortLabel}令牌${row.view.label}`;",
};

const MUTATE_TO = {
  expired: '  return "expired";\n}',
  label: 'label: "已失效"',
  order: 'export const LOGIN_CHANNEL_ORDER: string[] = ["www", "creator", "ark"];',
  missing: "        view: VIEWS.unknown,",
  short: '  ark: "网页版",',
  shortlong: '  www: "网页版（PC）",',
  // 把后端长原因拼回 tip —— 正是用户说「太长了」的那个形态
  tip: "  const head = `${row.shortLabel}令牌${row.view.label}${row.reason ? `（${row.reason}）` : \"\"}`;",
};

// 变异插件工厂：mutate 由参数传入（单进程多变异时每个 variant 各建一个插件实例）
function makeMutatePlugin(mutate) {
  return {
    name: "login-state-mutate",
    setup(build) {
      build.onLoad({ filter: /lib[\\/]login-state\.ts$/ }, (args) => {
        // ⚠️ 行尾归一化：源码可能是 CRLF（Windows 编辑器留下的），多行锚点带 `\n`
        // 会失配 —— 失配的表现是「变异静默不生效」，脚本照常全绿，比变红更危险。
        let source = fs.readFileSync(args.path, "utf8").replace(/\r\n/g, "\n");
        if (mutate) {
          const needle = RESERVED[mutate];
          if (!needle) throw new Error(`未知变异：${mutate}`);
          if (!source.includes(needle)) {
            throw new Error(`变异锚点未命中（实现已改？）：${needle}`);
          }
          source = source.replace(needle, MUTATE_TO[mutate]);
        }
        return { contents: source, loader: "ts" };
      });
    },
  };
}

//: self-check 模式依次跑的 variant：空串 = 契约（必须全 PASS），其余必须至少一个 FAIL
const VARIANTS = ["", "expired", "label", "order", "missing", "short", "shortlong", "tip"];

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

// mutate = "" 跑契约；否则跑对应变异。返回 { passed, failed }，不碰 process.exit
// （单进程多变异靠这个可重入性：每次重新打包 + require 新临时文件）。
async function main(mutate) {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "login-state-"));
  const bundled = await esbuild.build({
    entryPoints: [MODULE_TS],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    target: "node18",
    logLevel: "silent",
    plugins: [makeMutatePlugin(mutate)],
  });
  const outFile = path.join(tmpDir, "login-state.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const mod = require(outFile);
  const { loginStateView, normalizeLoginState, loginChannelRows, loginTagTip } = mod;

  if (typeof loginStateView !== "function") {
    console.error("FATAL 未导出 loginStateView —— 若已重命名，请同步更新本脚本");
    return { passed: 0, failed: 1 };
  }
  if (typeof loginTagTip !== "function") {
    console.error("FATAL 未导出 loginTagTip —— 若已重命名，请同步更新本脚本");
    return { passed: 0, failed: 1 };
  }
  if (mutate) console.log(`\n===== variant: ${mutate} =====`);

  const STATES = ["missing", "guest", "valid", "expired", "unknown"];
  const views = Object.fromEntries(STATES.map((state) => [state, loginStateView(state)]));

  // A. 五种状态必须互不相同（压成一个值必然说谎）
  check(
    "A1 五种状态的文案互不相同",
    new Set(STATES.map((state) => views[state].label)).size,
    5
  );

  // B. **红线**：`unknown` 不等于 `expired`（文案、颜色、动作三处都要分开）
  check("B1 unknown 的文案 ≠ expired 的文案", views.unknown.label === views.expired.label, false);
  check("B2 unknown 的颜色 ≠ expired 的颜色", views.unknown.color === views.expired.color, false);
  // B3：拆分 + 自动检查后，`unknown` **不再可点** —— 「未检测」由进入页面时的探活解决，
  // 真探不出结论时刷新页面会再探一次（v1.31 起「检查」按钮已移除）。
  // 给 unknown 挂动作等于又造一个入口。
  check("B3 unknown 不给任何动作（自动检查负责探活）", views.unknown.action, "");
  check("B4 expired 的动作才是重新登录", views.expired.action, "login");

  // C. 映射失败的方向**只允许**落到 unknown
  check("C1 空串落到 unknown", normalizeLoginState(""), "unknown");
  check("C2 null 落到 unknown", normalizeLoginState(null), "unknown");
  check(
    "C3 后端将来新增的取值落到 unknown（**绝不落到 expired**）",
    normalizeLoginState("rate-limited"),
    "unknown"
  );

  // D. 各状态的动作文案（决定用户被引导去做什么）
  check("D1 missing 的入口是**登录**（首次导入）", views.missing.actionLabel, "登录");
  check("D2 valid 不给任何动作（已确认可用，别催）", views.valid.action, "");

  // E. 平台行：**永远三个、顺序固定、缺项合成 missing**
  const rows = loginChannelRows({
    www: { state: "expired", reason: "过期了", label: "网页版（PC）" },
    ark: { state: "unknown", reason: "", label: "千帆" },
  });
  check(
    "E1 顺序是 ark（千帆）在前、creator 次之、www 在后",
    rows.map((row) => row.channel),
    ["ark", "creator", "www"]
  );
  check(
    "E2 后端没给的平台合成 missing（「没有」≠「没检测」）",
    loginChannelRows({ ark: { state: "valid" } }).map((row) => row.view.state),
    ["valid", "missing", "missing"]
  );
  check(
    "E3 后端新增的未知平台也保留（放最后，不静默吞掉）",
    loginChannelRows({ zzz: { state: "valid" } }).map((row) => row.channel),
    ["ark", "creator", "www", "zzz"]
  );
  check("E4 reason 原样带出来（留作详情展示，不进 tip）", rows[0].reason, "");

  // F. 名称后面那几个 tag（设计 13 §18.14）：**短名必须能区分三个平台、且确实更短**。
  //
  // 这几条守的是「标签化」这个动作本身：把状态放到昵称旁边，位置是省下来了，
  // 但代价是每个 tag 只剩十来个字符 —— 短名一撞车（两条都叫「网页版」），
  // 用户就不知道「已失效」说的是哪个平台；短名一偷懒抄全名，
  // 昵称会被挤出卡片。**两个不变量都不是「有没有这个字段」，而是「值之间什么关系」**
  // —— 这正是源码断言做不到、必须真实驱动函数的那类规则。
  const tagged = loginChannelRows({
    ark: { state: "expired", label: "千帆商家后台" },
    creator: { state: "valid", label: "创作者平台" },
    www: { state: "missing", label: "网页版（PC）" },
  });
  const shortNames = tagged.map((row) => row.shortLabel);
  check(
    "F1 三个平台的短名互不相同且非空（否则 tag 长得一样，分不清哪个是千帆）",
    shortNames.every((name) => typeof name === "string" && name.length > 0) &&
      new Set(shortNames).size === shortNames.length,
    true
  );
  check(
    "F2 短名严格短于全名（tag 只有昵称旁边那点位置，塞全名会把昵称挤没）",
    tagged.every((row) => row.shortLabel.length < row.label.length),
    true
  );

  // G. 悬停说明**只留核心**（用户 2026-09-21 口径：tip 太长了）。
  //
  // 初版把「平台全名 + 状态 + 后端长原因 + 动作」四段全塞进去，一个气泡半屏高。
  // 现在只允许：哪个平台、什么状态、能做什么 —— 后端长原因（`row.reason`）
  // 是给「检测没得到结论」准备的全文，标签上放不下也不该放。
  const tipRow = (channel, state, reason) =>
    loginChannelRows({ [channel]: { state, reason, label: "x" } })[0];
  const LONG_REASON = "令牌已失效，请在网页版（xiaohongshu.com）重新登录后扫码续期。";
  check(
    "G1 可用状态的 tip 只有「平台 + 状态」",
    loginTagTip(tipRow("ark", "valid", "")),
    "千帆令牌可用"
  );
  check(
    "G2 失效状态的 tip 带上动作提示",
    loginTagTip(tipRow("ark", "expired", LONG_REASON)),
    "千帆令牌已失效，点击重新登录"
  );
  check(
    "G3 tip 不含后端长原因（「太长了」的那个东西）",
    loginTagTip(tipRow("www", "expired", LONG_REASON)).includes(LONG_REASON),
    false
  );
  check(
    "G4 tip 长度有界（≤16 字，气泡不能比 tag 本身还大）",
    loginTagTip(tipRow("creator", "missing", LONG_REASON)).length <= 16,
    true
  );

  console.log(`\n${passed} PASS / ${failed} FAIL${mutate ? `  [变异模式：${mutate}]` : ""}`);
  return { passed, failed };
}

// --self-check：一个进程跑完契约 + 全部变异。
// 每个 variant 的期望：契约全 PASS；变异至少一个 FAIL（守卫承重）。
// 摘要行 `SELF <variant> OK|BROKEN <p>/<f>` 供 pytest 解析。
async function selfCheck() {
  let broken = 0;
  for (const variant of VARIANTS) {
    let r;
    try {
      r = await main(variant);
    } catch (error) {
      // 变异锚点未命中 / 打包失败都算 BROKEN —— 静默不生效比变红更危险
      console.log(`SELF ${variant || "none"} BROKEN (throw: ${error && error.message})`);
      broken += 1;
      continue;
    }
    const expectFail = variant !== "";
    const ok = expectFail ? r.failed > 0 : r.failed === 0;
    if (!ok) broken += 1;
    console.log(`SELF ${variant || "none"} ${ok ? "OK" : "BROKEN"} ${r.passed}/${r.failed}`);
  }
  return broken === 0 ? 0 : 1;
}

if (process.argv.includes("--self-check")) {
  selfCheck()
    .then((code) => process.exit(code))
    .catch((error) => {
      console.error(`FATAL ${error && error.message ? error.message : error}`);
      process.exit(1);
    });
} else {
  main(process.env.LOGIN_STATE_MUTATE || "")
    .then((r) => process.exit(r.failed === 0 ? 0 : 1))
    .catch((error) => {
      console.error(`FATAL ${error && error.message ? error.message : error}`);
      process.exit(1);
    });
}
