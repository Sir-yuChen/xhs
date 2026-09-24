/**
 * 行为验证：全局拦截器必须把后端的**结构化** `detail` 如实交出来。
 *
 * 背景（DEF-058，用户 2026-09-21 实测报出）：后端对「可自救的失败」一律返回
 * `{code, message, platform_message}`；旧拦截器只认 `typeof detail === "string"`，
 * 于是**一律**降级成「请求失败，请稍后重试」——用户看到「我赞过的 / 我收藏的 /
 * 互动消息全部请求失败」，却拿不到任何可执行信息。用户口径是
 * 「**只有接口异常才真实抛出异常信息**」。
 *
 * 为什么不能只用源码断言：`"Array.isArray(detail)" in source` 这类断言在
 * 「函数被改成 `return ""`」时**依然为真** —— 它证明的是「代码长这样」，
 * 不是「函数这样跑」。这里用 esbuild 打包**真实** `lib/api.ts`，拿到真实的
 * response 拦截器并驱动它，再断言 antd 收到的文案。
 *
 * 运行（不需要 jsdom，antd 用桩替换）：
 *
 *   <node> tests/frontend/detail_message.cjs                  # 期望 13 PASS
 *   DETAIL_MUTATE=generic    <node> tests/frontend/detail_message.cjs   # 期望 FAIL
 *   DETAIL_MUTATE=silent     <node> tests/frontend/detail_message.cjs   # 期望 FAIL
 *   DETAIL_MUTATE=validation <node> tests/frontend/detail_message.cjs   # 期望 FAIL
 *
 * 三个变异开关正是「守卫承重」的证据：
 *   generic    —— 结构化 detail 的分支失效 ⇒ 真原因退回通用文案；
 *   silent     —— 拦截器不再尊重 `_silent` ⇒ 页面自己呈现失败时又被全局弹一次；
 *   validation —— 422 数组分支失效 ⇒ 校验错误又只剩英文 axios 文案兜底。
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const API_TS = path.join(PROJECT, "frontend/src/lib/api.ts");

const MUTATE = process.env.DETAIL_MUTATE || "";

// --- antd 桩：只记录 `message.error` 收到了什么 -------------------------------
//
// ⚠️ 桩必须放在**产物的 node_modules 里、并把 antd 设为 external**。
// 若改用 esbuild 的 `alias`，桩会被**内联进 bundle**：那样 bundle 里 `message.error`
// 推的是它自己那份数组，脚本这边 `require()` 拿到的是**另一份** —— 于是断言永远看到
// 「一条都没弹」（本脚本第一次跑就是这么假的：7 PASS / 4 FAIL，全部 B 段）。
const stubDir = fs.mkdtempSync(path.join(os.tmpdir(), "detail-message-"));
const antdStubDir = path.join(stubDir, "node_modules", "antd");
fs.mkdirSync(antdStubDir, { recursive: true });
fs.writeFileSync(
  path.join(antdStubDir, "package.json"),
  JSON.stringify({ name: "antd", version: "0.0.0", main: "index.js" })
);
fs.writeFileSync(
  path.join(antdStubDir, "index.js"),
  "const seen = [];\n" +
    "exports.__toasts = seen;\n" +
    "exports.message = {\n" +
    "  error: (text) => seen.push(text),\n" +
    "  success: () => {},\n" +
    "  warning: () => {},\n" +
    "};\n"
);
const antdStub = antdStubDir;

// --- 打包真实 api.ts（并借 onLoad 做内存态变异 / 追加 http 导出） -------------
const RESERVED = {
  generic: 'if (detail && typeof detail === "object") {',
  silent: "if (!originalRequest?._silent) {",
  validation: "if (Array.isArray(detail)) return validationDetailMessage(detail);",
};

const MUTATE_TO = {
  generic: "if (false) {",
  silent: "if (true) {",
  validation: "", // 整行删掉：422 数组再也没有可读原因
};

const mutatePlugin = {
  name: "detail-message-mutate",
  setup(build) {
    build.onLoad({ filter: /lib[\\/]api\.ts$/ }, (args) => {
      let source = fs.readFileSync(args.path, "utf8");
      if (MUTATE) {
        const needle = RESERVED[MUTATE];
        if (!needle) throw new Error(`未知变异：${MUTATE}`);
        if (!source.includes(needle)) {
          throw new Error(`变异锚点未命中（实现已改？）：${needle}`);
        }
        source = source.replace(needle, MUTATE_TO[MUTATE]);
      }
      // `http` 原本是模块私有：追加一行导出，才能拿到**真实**的拦截器去驱动。
      return { contents: `${source}\nexport { http as __http };\n`, loader: "ts" };
    });
  },
};

// --- 断言 ---------------------------------------------------------------------
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

function makeError({ status = 400, detail, silent = false }) {
  return {
    response: { status, data: { detail } },
    config: silent ? { _silent: true } : {},
  };
}

async function main() {
  // ⚠️ 必须用异步 `build`：esbuild 的 **同步** API 不接受 `plugins` / `alias`，
  // 而这两样正是本脚本的关键（内存态变异 + antd 桩）。
  const bundled = await esbuild.build({
    entryPoints: [API_TS],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    target: "node18",
    logLevel: "silent",
    // 不打包 antd：让它成为运行期 `require("antd")`，落回桩所在的 node_modules。
    external: ["antd"],
    plugins: [mutatePlugin],
  });

  const outFile = path.join(stubDir, "api.bundle.cjs");
  fs.writeFileSync(outFile, bundled.outputFiles[0].text);
  const api = require(outFile);
  const stub = require(antdStub);

  const { detailMessage, __http } = api;
  if (typeof detailMessage !== "function") {
    console.error("FATAL 未导出 detailMessage —— 若已重命名，请同步更新本脚本");
    return 1;
  }

  // A. 纯函数：从 `detail` 里取出**给人看的那句话**
  check("A1 字符串 detail 原样返回", detailMessage("平台原话"), "平台原话");
  check(
    "A2 结构化 detail 取 message（不再退化成通用文案）",
    detailMessage({ code: "WEB_SESSION_REQUIRED", message: "该账号缺少网页版登录态" }),
    "该账号缺少网页版登录态"
  );
  check(
    "A3 message 优先于 platform_message",
    detailMessage({ code: "X", platform_message: "平台侧原话", message: "本地话术" }),
    "本地话术"
  );
  check(
    "A4 只有 platform_message 时也交出来",
    detailMessage({ code: "X", platform_message: "平台侧原话" }),
    "平台侧原话"
  );
  check(
    "A5 422 数组还原成「字段路径 + 原因」",
    detailMessage([{ loc: ["body", "account_id"], msg: "Field required", type: "missing" }]),
    "body.account_id：Field required"
  );
  check("A6 空白 message 不算原因 → 交空串", detailMessage({ message: "   " }), "");
  check(
    "A7 数组项没有 loc 时只交原因",
    detailMessage([{ msg: "Input should be a valid integer" }]),
    "Input should be a valid integer"
  );
  check(
    "A8 多项校验错误合并成一句（不再丢掉后续项）",
    detailMessage([
      { loc: ["body", "account_id"], msg: "Field required" },
      { loc: ["body", "kind"], msg: "Field required" },
    ]),
    "body.account_id：Field required；body.kind：Field required"
  );

  // B. 真实拦截器：toast 文案 + 是否尊重 `_silent`
  const handlers = (__http?.interceptors?.response?.handlers ?? []).filter(Boolean);
  if (!handlers.length) {
    console.error("FATAL 拿不到 response 拦截器 —— 无法驱动真实代码路径");
    return 1;
  }
  const onRejected = handlers[0].rejected;
  const toastCount = () => stub.__toasts.length;

  const before = toastCount();
  await onRejected(
    makeError({ detail: { code: "WEB_SESSION_REQUIRED", message: "该账号缺少网页版登录态" } })
  ).catch(() => {});
  const toast = stub.__toasts[stub.__toasts.length - 1];
  check(
    "B1 结构化 detail 的**真原因**上屏（不是「请求失败，请稍后重试」）",
    toast,
    "该账号缺少网页版登录态"
  );

  const afterFirst = toastCount();
  await onRejected(makeError({ detail: "平台原话" })).catch(() => {});
  check("B2 字符串 detail 也照旧上屏", stub.__toasts[stub.__toasts.length - 1], "平台原话");

  const afterSecond = toastCount();
  await onRejected(
    makeError({ status: 422, detail: [{ loc: ["body", "account_id"], msg: "Field required" }] })
  ).catch(() => {});
  check(
    "B3 422 校验错误的**字段与原因**也上屏（不再是英文 axios 兜底）",
    stub.__toasts[stub.__toasts.length - 1],
    "body.account_id：Field required"
  );

  const afterThird = toastCount();
  await onRejected(makeError({ detail: { code: "X", message: "不该弹" }, silent: true })).catch(() => {});
  check("B4 `_silent` 的失败不弹 toast（展示权归调用点）", toastCount(), afterThird);

  // 前置校验：前三次确实各弹了一条，说明 B4 的「没弹」不是「压根没跑」。
  check("B5 前三次失败各弹一条（B4 的对照）", afterThird - before, 3);

  console.log(`\n${passed} PASS / ${failed} FAIL${MUTATE ? `  [变异模式：${MUTATE}]` : ""}`);
  return failed === 0 ? 0 : 1;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`FATAL ${error && error.message ? error.message : error}`);
    process.exit(1);
  });
