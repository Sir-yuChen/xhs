/**
 * 行为验证：`/auth/refresh` 自己返回 401 时，401 拦截器必须**终止**，
 * 而不是回头再调一次 `refreshAccessToken()` 形成自我递归。
 *
 * 背景（2026-09-27 打包版 1.0.2 实测）：
 *   `refreshAccessToken()` 用的是**同一个** `http` 实例。它自己拿到 401 时，
 *   这个 401 又回到同一个响应拦截器；而那次请求的 `config._authRetry` 是
 *   `undefined`、`getRefreshToken()` 又仍然非空（旧令牌还在 localStorage），
 *   于是拦截器的分支条件为假 ⇒ 又走「刷新 ⇒ 重试」⇒ 无限递归，承诺**永不 settle**。
 *   上游 `bootstrapAuth()` 永远 await ⇒ `use-auth` 的 status 卡在 `checking`
 *   ⇒ 界面停在「正在验证登录状态…」，服务端 5 分钟被打了 **8.8 万次**
 *   `POST /api/auth/refresh` 401（`D:\SpiderXHS\logs\launcher.log` 8.6MB）。
 *
 * 为什么不用源码断言：把 `_authRetry` 从那次 `http.post` 的 config 里摘掉，
 * 源码里照样能找到 `_authRetry` 这几个字母（拦截器里就写着）——`"in source"` 恒真。
 * 规则是「**这个失败必须以有界次数结束**」，只有真的把拦截器跑起来、数它发了几次
 * 请求才算证明。做法与 `login_state.cjs` / `detail_message.cjs` 同源：
 * esbuild 打包**真实** `frontend/src/lib/api.ts`，只把 `axios` / `antd` 换成可控桩。
 *
 * 触发条件不是边角：「登录已过期」是**每个用户都会走到**的正常路径
 * （refresh token 过期 / 库被重置 / 用户被删），所以这是 P1。
 *
 * 运行：
 *
 *   <node> tests/frontend/auth_refresh_terminates.cjs                  # 期望 6 PASS
 *   AUTH_MUTATE=retry         <node> ...   # 期望 FAIL C1/C6（摘掉 _authRetry ⇒ 自我递归）
 *   AUTH_MUTATE=silent        <node> ...   # 期望 FAIL C2（摘掉 _silent ⇒ 拦截器多弹一次）
 *   AUTH_MUTATE=shortcircuit  <node> ...   # 期望 FAIL C1/C6（拦截器不再短路 ⇒ 连标记也拦不住）
 *
 * 三个变异正是「守卫承重」的证据。变异锚点未命中时脚本**直接 FATAL 退出**——
 * 锚点失配会静默不生效、脚本照常全绿，比变红更危险（2026-09-26 已在
 * `library_item_filter.cjs` 上踩过一次）。
 */
const fs = require("fs");
const path = require("path");

const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));
const API_TS = path.join(PROJECT, "frontend/src/lib/api.ts");

/** 单次调用里 `/auth/refresh` 被请求超过这个次数 = 已经是在自我递归，不再等它自然收敛。 */
const LOOP_LIMIT = 40;
/** 承诺必须在这个时间内 settle —— 原缺陷的签名就是「永不 settle」。 */
const SETTLE_TIMEOUT_MS = 5000;

// --- 变异：锚点必须唯一命中，否则 FATAL ------------------------------------
const REFRESH_CONFIG = "{ _authRetry: true, _silent: true } as never";
const INTERCEPTOR_GUARD =
  "error.response?.status !== 401 || originalRequest?._authRetry || !getRefreshToken()";

const MUTATIONS = {
  retry: {
    from: REFRESH_CONFIG,
    to: "{ _silent: true } as never",
    why: "摘掉刷新请求的 `_authRetry` ⇒ 它自己的 401 又进恢复分支",
  },
  silent: {
    from: REFRESH_CONFIG,
    to: "{ _authRetry: true } as never",
    why: "摘掉 `_silent` ⇒ 失败呈现权不再归调用点，拦截器多弹一次",
  },
  shortcircuit: {
    from: INTERCEPTOR_GUARD,
    to: "error.response?.status !== 401 || !getRefreshToken()",
    why: "拦截器不再认 `_authRetry` ⇒ 连标记过的刷新请求也逃不掉",
  },
};

function fatal(message) {
  process.stdout.write(`FATAL ${message}\n`);
  process.exit(1);
}

// --- 受控 axios / antd 桩 ---------------------------------------------------
function makeHarness() {
  const state = {
    refreshCount: 0,
    toasts: [],
    seen: [],
    calls: new Map(),
    looped: false,
  };

  const routes = new Map(); // `post /auth/refresh` -> ({ call, config }) => { status, data }
  const server = {
    route(method, url, handler) {
      routes.set(`${method.toLowerCase()} ${url}`, handler);
    },
  };

  const storage = new Map();
  globalThis.window = {
    localStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
  };

  const antdMessage = { error: (text) => state.toasts.push(String(text)) };

  let requestHandler = null;
  let responseErrorHandler = null;

  function makeError(config, status, data) {
    const error = new Error(`Request failed with status code ${status}`);
    error.isAxiosError = true;
    error.config = config;
    error.response = { status, data, config };
    return error;
  }

  async function dispatch(config) {
    if (!config.headers) config.headers = {};
    if (requestHandler) {
      const replaced = requestHandler(config);
      if (replaced) config = replaced;
    }

    const key = `${String(config.method || "get").toLowerCase()} ${config.url}`;
    const call = (state.calls.get(key) || 0) + 1;
    state.calls.set(key, call);
    state.seen.push({ key, call, headers: { ...config.headers }, config });

    if (key === "post /auth/refresh") {
      state.refreshCount += 1;
      if (state.refreshCount > LOOP_LIMIT) {
        // 已经是在递归里了：同步抛出把整条 promise 链解开，别让它真的跑到天荒地老。
        state.looped = true;
        throw new Error(`LOOP DETECTED: /auth/refresh 已被请求 ${state.refreshCount} 次`);
      }
    }

    const handler = routes.get(key);
    if (!handler) {
      throw makeError(config, 404, { detail: `stub has no route for ${key}` });
    }
    const result = handler({ call, config }) || { status: 200, data: {} };
    if (result.status >= 200 && result.status < 300) {
      return { status: result.status, data: result.data, config, headers: {} };
    }
    const error = makeError(config, result.status, result.data);
    if (!responseErrorHandler) throw error;
    return responseErrorHandler(error);
  }

  function instance(...args) {
    let config;
    if (typeof args[0] === "string") {
      const extra = args[1] || {};
      config = { ...extra, url: args[0], method: extra.method || "get" };
    } else {
      config = { ...(args[0] || {}) };
    }
    return dispatch(config);
  }
  instance.interceptors = {
    request: { use: (fn) => { requestHandler = fn; return 0; } },
    response: { use: (_ok, err) => { responseErrorHandler = err; return 0; } },
  };
  for (const method of ["get", "delete"]) {
    instance[method] = (url, extra) => dispatch({ ...(extra || {}), url, method });
  }
  for (const method of ["post", "put", "patch"]) {
    instance[method] = (url, data, extra) => dispatch({ ...(extra || {}), url, method, data });
  }
  instance.create = () => instance;
  instance.isAxiosError = (error) => Boolean(error && error.isAxiosError);

  return { axios: instance, antdMessage, server, state };
}

// --- 打包真实 api.ts（axios / antd 换成桩；可选变体） ------------------------
function apiPlugin(mutation) {
  return {
    name: "api-harness",
    setup(build) {
      build.onResolve({ filter: /^axios$/ }, () => ({ path: "axios", namespace: "stub" }));
      build.onResolve({ filter: /^antd$/ }, () => ({ path: "antd", namespace: "stub" }));
      build.onLoad({ filter: /.*/, namespace: "stub" }, (args) => ({
        contents:
          args.path === "axios"
            ? "module.exports = globalThis.__HARNESS__.axios; module.exports.default = globalThis.__HARNESS__.axios;"
            : "module.exports.message = globalThis.__HARNESS__.antdMessage;",
        loader: "js",
      }));
      if (!mutation) return;
      build.onLoad({ filter: /lib[\\/]api\.ts$/ }, (args) => {
        // ⚠️ 行尾归一化：源码可能是 CRLF，多行锚点带 `\n` 会失配（失配=变异静默不生效）。
        let source = fs.readFileSync(args.path, "utf8").replace(/\r\n/g, "\n");
        const hits = source.split(mutation.from).length - 1;
        if (hits !== 1) {
          fatal(`变异锚点未命中（${hits} 处）: ${JSON.stringify(mutation.from)}`);
        }
        source = source.replace(mutation.from, mutation.to);
        return { contents: source, loader: "ts" };
      });
    },
  };
}

async function buildApi(mutationName) {
  const mutation = mutationName ? MUTATIONS[mutationName] : null;
  if (mutationName && !mutation) fatal(`未知变异: ${mutationName}`);
  const result = await esbuild.build({
    entryPoints: [API_TS],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    logLevel: "silent",
    plugins: [apiPlugin(mutation)],
  });
  return result.outputFiles[0].text;
}

/** 每次调用都用**全新的模块实例 + 全新的桩**，互不串状态。 */
async function freshApi(bundleCode) {
  const harness = makeHarness();
  globalThis.__HARNESS__ = harness;
  const module = { exports: {} };
  const evaluate = new Function("module", "exports", "require", bundleCode);
  evaluate(module, module.exports, () => {
    throw new Error("bundle 不应有外部 require");
  });
  return { api: module.exports, harness };
}

function withTimeout(promise, label) {
  let timer;
  return Promise.race([
    promise.finally(() => clearTimeout(timer)),
    new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} 未在 ${SETTLE_TIMEOUT_MS}ms 内 settle`)), SETTLE_TIMEOUT_MS);
    }),
  ]);
}

// --- 断言记账 ---------------------------------------------------------------
const results = [];
function check(id, ok, detail) {
  results.push({ id, ok, detail });
  process.stdout.write(`${ok ? "PASS" : "FAIL"}  ${id} ${detail}\n`);
}

const STALE = "stale-refresh-token";

/** C1：库被重置 / 令牌失效 ⇒ 必须**有界**地失败（一次刷新请求，然后 reject）。 */
async function c1(bundle) {
  const { api, harness } = await freshApi(bundle);
  globalThis.window.localStorage.setItem("spider_xhs_refresh_token", STALE);
  harness.server.route("post", "/auth/refresh", () => ({
    status: 401,
    data: { detail: "Invalid refresh token" },
  }));

  const outcome = await withTimeout(api.bootstrapAuth(), "bootstrapAuth")
    .then(() => "resolved")
    .catch((error) => (error && /未在 .* settle/.test(error.message) ? "hung" : "rejected"));

  const bounded = harness.state.refreshCount <= 1 && !harness.state.looped;
  check(
    "C1",
    outcome === "rejected" && bounded,
    `stale token + refresh 401 ⇒ ${outcome}，/auth/refresh 请求 ${harness.state.refreshCount} 次（要求 ≤1 且不递归）`
  );
}

/** C2：刷新失败的呈现权归调用点 ⇒ 拦截器不许自己弹 toast。 */
async function c2(bundle) {
  const { api, harness } = await freshApi(bundle);
  globalThis.window.localStorage.setItem("spider_xhs_refresh_token", STALE);
  harness.server.route("post", "/auth/refresh", () => ({
    status: 401,
    data: { detail: "Invalid refresh token" },
  }));

  await withTimeout(api.bootstrapAuth(), "bootstrapAuth").catch(() => undefined);

  check(
    "C2",
    harness.state.toasts.length === 0,
    `刷新请求自身失败不弹全局提示（实际 ${harness.state.toasts.length} 次：${JSON.stringify(harness.state.toasts)}）`
  );
}

/** C3：完全没有令牌 ⇒ 直接匿名，一个刷新请求都不该发。 */
async function c3(bundle) {
  const { api, harness } = await freshApi(bundle);
  const user = await api.bootstrapAuth();
  check(
    "C3",
    user === null && harness.state.refreshCount === 0,
    `无令牌 ⇒ 匿名且零刷新请求（user=${JSON.stringify(user)}，请求 ${harness.state.refreshCount} 次）`
  );
}

/** C4：正常路径没被这行标记破坏 —— 刷新成功 → 用新令牌取 /auth/me。 */
async function c4(bundle) {
  const { api, harness } = await freshApi(bundle);
  globalThis.window.localStorage.setItem("spider_xhs_refresh_token", "good-token");
  harness.server.route("post", "/auth/refresh", () => ({
    status: 200,
    data: { access_token: "NEW-ACCESS", token_type: "bearer" },
  }));
  harness.server.route("get", "/auth/me", () => ({
    status: 200,
    data: { id: 7, username: "tester" },
  }));

  const user = await withTimeout(api.bootstrapAuth(), "bootstrapAuth");
  const me = harness.state.seen.find((entry) => entry.key === "get /auth/me");
  const header = me ? me.headers.Authorization : undefined;

  check(
    "C4",
    Boolean(user) && user.id === 7 && header === "Bearer NEW-ACCESS" && harness.state.refreshCount === 1,
    `刷新成功 ⇒ 取到用户 /auth/me 带新令牌（user=${user && user.id}，Authorization=${header}，刷新 ${harness.state.refreshCount} 次）`
  );
}

/** C5：老路径必须还在 —— 普通接口 401 ⇒ 刷新一次 ⇒ 重试一次 ⇒ 成功。 */
async function c5(bundle) {
  const { api, harness } = await freshApi(bundle);
  globalThis.window.localStorage.setItem("spider_xhs_refresh_token", "good-token");
  harness.server.route("post", "/auth/refresh", () => ({
    status: 200,
    data: { access_token: "NEW-ACCESS", token_type: "bearer" },
  }));
  harness.server.route("get", "/xhs/analytics/overview", ({ call }) =>
    call === 1
      ? { status: 401, data: { detail: "Token expired" } }
      : { status: 200, data: { notes: 3 } }
  );

  const overview = await withTimeout(api.fetchXhsOverview(), "fetchXhsOverview");
  const calls = harness.state.calls.get("get /xhs/analytics/overview") || 0;

  check(
    "C5",
    overview && overview.notes === 3 && calls === 2 && harness.state.refreshCount === 1,
    `过期 ⇒ 刷新一次 + 重试一次即成功（接口调用 ${calls} 次，刷新 ${harness.state.refreshCount} 次）`
  );
}

/** C6：每次 bootstrap 各自**有界**，重复调用不会累积成风暴。 */
async function c6(bundle) {
  const { api, harness } = await freshApi(bundle);
  globalThis.window.localStorage.setItem("spider_xhs_refresh_token", STALE);
  harness.server.route("post", "/auth/refresh", () => ({
    status: 401,
    data: { detail: "Invalid refresh token" },
  }));

  let hung = false;
  for (let i = 0; i < 3; i += 1) {
    await withTimeout(api.bootstrapAuth(), `bootstrapAuth#${i + 1}`)
      .then(() => undefined)
      .catch((error) => {
        if (error && /未在 .* settle/.test(error.message)) hung = true;
      });
  }

  check(
    "C6",
    !hung && harness.state.refreshCount <= 3 && !harness.state.looped,
    `连续 3 次 bootstrap 各最多一次刷新（实际 ${harness.state.refreshCount} 次，hang=${hung}）`
  );
}

const SCENARIOS = [c1, c2, c3, c4, c5, c6];

async function main() {
  const mutationName = process.env.AUTH_MUTATE || "";
  const bundle = await buildApi(mutationName || null);
  if (mutationName) {
    process.stdout.write(`== 变异 ${mutationName}：${MUTATIONS[mutationName].why}\n`);
  }

  for (const scenario of SCENARIOS) {
    try {
      await scenario(bundle);
    } catch (error) {
      check(scenario.name.toUpperCase(), false, `抛异常：${error && error.message}`);
    }
  }

  const failed = results.filter((row) => !row.ok);
  process.stdout.write(
    failed.length === 0 ? "ALL INVARIANTS HOLD\n" : `INVARIANT(S) VIOLATED: ${failed.map((r) => r.id).join(", ")}\n`
  );
  process.exit(failed.length === 0 ? 0 : 1);
}

main().catch((error) => {
  process.stdout.write(`FATAL ${error && error.stack}\n`);
  process.exit(1);
});
