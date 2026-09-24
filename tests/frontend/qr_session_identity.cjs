/**
 * Deterministic harness for the QR login panel's session-identity invariants.
 *
 * Runs the REAL component (frontend/src/components/account/qr-login-panel.tsx)
 * under React StrictMode in jsdom, with ../../lib/api replaced by a controllable
 * stub, and asserts four invariants that a phone scan depends on:
 *
 *   S1  one drawer open mints exactly ONE login session (StrictMode guard)
 *   S2  a superseded creation can never become the displayed QR
 *   S3  a poll response belonging to another session can never hijack the panel
 *   S4  the qianfan (ark) branch mints ITS OWN session and only that one
 *
 * S3 is the DEF-038 root cause: the screen showed QR-B while the poller watched
 * session-A, so the phone's confirmation was credited to a session nobody was
 * watching and the page never advanced.
 *
 * S4 exists because the qianfan branch (2026-09-21, §18.14) is a third ternary
 * arm of the same `startSession` and had **zero** behavioural coverage: this
 * harness used to die at bundle time on a missing mock export, so the whole file
 * was a no-op. Reviving it immediately showed the qianfan arm carries the same
 * StrictMode double-mint (see the QR_MUTATE=old output: createQianfan=2).
 *
 * Run (jsdom is intentionally NOT a project dependency):
 *
 *   <node> tests/frontend/qr_session_identity.cjs                 # expect 9 PASS
 *   QR_MUTATE=old <node> tests/frontend/qr_session_identity.cjs   # expect 4 FAIL
 *
 * The second form strips the three guards in memory as esbuild loads the
 * component, which is what proves the guards are load-bearing.  With jsdom
 * installed outside the project, point NODE_PATH at its node_modules.
 */
const path = require("path");
// tests/frontend/qr_session_identity.cjs -> project root
const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));

const { JSDOM } = require("jsdom");

// --- jsdom environment ------------------------------------------------------
const dom = new JSDOM(
  "<!doctype html><html><head></head><body><div id='root'></div></body></html>",
  { pretendToBeVisual: true, url: "http://localhost/" }
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;
// antd / rc-* code paths probe DOM constructors directly (SVGElement, Image,
// MutationObserver, ...), so the full interface set has to be reachable from
// the bundled module scope, not just from `window`.
const DOM_GLOBALS = [
  "HTMLElement", "HTMLDivElement", "HTMLSpanElement", "HTMLInputElement", "HTMLTextAreaElement",
  "HTMLButtonElement", "HTMLAnchorElement", "HTMLImageElement", "HTMLParagraphElement",
  "HTMLFormElement", "HTMLUListElement", "HTMLLIElement", "HTMLTableElement", "HTMLCanvasElement",
  "HTMLSelectElement", "HTMLStyleElement", "HTMLHeadElement", "HTMLBodyElement",
  "SVGElement", "SVGSVGElement", "SVGPathElement", "SVGUseElement", "SVGSymbolElement",
  "SVGGraphicsElement", "SVGAnimatedString",
  "Element", "Node", "NodeList", "DocumentFragment", "ShadowRoot", "Document", "Text", "Comment",
  "Event", "CustomEvent", "MouseEvent", "PointerEvent", "KeyboardEvent", "FocusEvent",
  "TouchEvent", "UIEvent", "WheelEvent", "InputEvent", "AnimationEvent", "TransitionEvent",
  "MutationObserver", "IntersectionObserver", "DOMParser", "XMLSerializer",
  "CSSStyleDeclaration", "CSSStyleSheet", "StyleSheet", "Blob", "File", "FileList", "FormData",
  "DataTransfer", "URL", "URLSearchParams", "Image", "DOMRect", "DOMRectReadOnly",
  "localStorage", "sessionStorage", "location", "history"
];
for (const key of DOM_GLOBALS) {
  const value = dom.window[key];
  if (value === undefined) continue;
  try {
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  } catch (err) {
    /* Non-configurable on this Node; antd does not need it. */
  }
}
// `window` methods must be bound or they throw "Illegal invocation".
for (const key of ["getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame", "matchMedia", "scrollTo", "focus", "blur"]) {
  const value = dom.window[key];
  if (typeof value === "function") {
    globalThis[key] = value.bind(dom.window);
  }
}
globalThis.requestAnimationFrame = globalThis.requestAnimationFrame || ((cb) => setTimeout(() => cb(Date.now()), 0));
globalThis.cancelAnimationFrame = globalThis.cancelAnimationFrame || ((id) => clearTimeout(id));
try {
  Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
} catch (err) {
  /* Node already defines navigator; the component does not read it. */
}
const matchMedia = (query) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent: () => false
});
dom.window.matchMedia = matchMedia;
globalThis.matchMedia = matchMedia;
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
dom.window.ResizeObserver = ResizeObserverStub;
globalThis.ResizeObserver = ResizeObserverStub;
globalThis.IS_REACT_ACT_ENVIRONMENT = false;

// --- stub for frontend/src/lib/api -----------------------------------------
const MOCK_API = `
const G = globalThis;
export async function createXhsPcQrLoginSession(payload) {
  G.__QR_LOG__.createPc.push(payload || {});
  const h = G.__QR_TEST__.createPc;
  return h ? await h(payload, G.__QR_LOG__.createPc.length)
           : { session_id: 9001, status: "pending", qr_url: "u", qr_image_data_url: "data:img/default" };
}
export async function createXhsCreatorQrLoginSession() {
  G.__QR_LOG__.createCreator.push(true);
  const h = G.__QR_TEST__.createCreator;
  return h ? await h(G.__QR_LOG__.createCreator.length)
           : { session_id: 9002, status: "pending", qr_url: "u", qr_image_data_url: "data:img/creator" };
}
export async function pollXhsLoginSession(sessionId, options) {
  G.__QR_LOG__.poll.push({ sessionId, options });
  const h = G.__QR_TEST__.poll;
  return h ? await h(sessionId, options, G.__QR_LOG__.poll.length)
           : { session_id: sessionId, status: "pending", qr_url: "u" };
}
export async function createXhsQianfanQrLoginSession(payload) {
  G.__QR_LOG__.createQianfan.push(payload || {});
  const h = G.__QR_TEST__.createQianfan;
  return h ? await h(payload, G.__QR_LOG__.createQianfan.length)
           : { session_id: 9003, status: "pending", qr_url: "u", qr_image_data_url: "data:img/qianfan" };
}
/*
  纯文案格式化。契约必须与 lib/api.ts::detailMessage 一致（字符串 → trim；
  422 数组 → "loc：msg" 用「；」拼接；对象 → message / platform_message）。
  那个函数的**真行为**由 detail_message.cjs 直接驱动 lib/api.ts 验证；这里只是
  让面板的错误分支能落地，本脚本不在此测它 —— 若两边契约分叉，责任在 lib/api.ts
  一侧的测试，不在这里复制一份断言。
*/
export function detailMessage(detail) {
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const parts = [];
    for (const item of detail) {
      if (!item || typeof item !== "object") continue;
      const msg = typeof item.msg === "string" ? item.msg.trim() : "";
      if (!msg) continue;
      const loc = Array.isArray(item.loc) ? item.loc.map((p) => String(p)).join(".") : "";
      parts.push(loc ? loc + "：" + msg : msg);
    }
    return parts.join("；");
  }
  if (detail && typeof detail === "object") {
    for (const candidate of [detail.message, detail.platform_message]) {
      if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
    }
  }
  return "";
}
`;

const ENTRY = `
import React, { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QrLoginPanel } from "./components/account/qr-login-panel";

const G = globalThis;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ⚠️ 等「条件成立」而不是死等固定毫秒（本仓实测固定 sleep 共 6.35s、脚本 8.8s）。
// React 18 render / 轮询都是异步的；轮询让「一就绪就继续」。
async function waitFor(predicate, { timeout = 2000, interval = 5 } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    let ok = false;
    try { ok = !!predicate(); } catch { ok = false; }
    if (ok) return true;
    if (Date.now() > deadline) return false;
    await new Promise((r) => setTimeout(r, interval));
  }
}


function reset() {
  G.__QR_LOG__ = { createPc: [], createCreator: [], createQianfan: [], poll: [] };
  G.__QR_TEST__ = {};
  return G.__QR_LOG__;
}
function mount() {
  const el = document.createElement("div");
  document.getElementById("root").appendChild(el);
  return { el, root: createRoot(el) };
}
function qrSrc(el) {
  const img = el.querySelector("img");
  return img ? img.getAttribute("src") : null;
}
function render(root, accountType, onConfirmed) {
  root.render(
    React.createElement(
      StrictMode,
      null,
      React.createElement(QrLoginPanel, { accountType, onConfirmed: onConfirmed || function () {} })
    )
  );
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: String(detail) });
}

/* S1 — React StrictMode invokes effects twice; only one session may be minted. */
async function scenario1() {
  const log = reset();
  G.__QR_TEST__.createPc = async () => ({
    session_id: 101, status: "pending", qr_url: "u101", qr_image_data_url: "data:img/101"
  });
  const { el, root } = mount();
  render(root, "pc");
  await waitFor(() => qrSrc(el));
  check("S1 StrictMode mount mints exactly one session", log.createPc.length === 1, "createPc=" + log.createPc.length);
  check("S1 that session's QR is the one displayed", qrSrc(el) === "data:img/101", "img=" + qrSrc(el));
  root.unmount();
  el.remove();
}

/* S2 — a creation superseded mid-flight must never be adopted. */
async function scenario2() {
  const log = reset();
  let releasePc;
  G.__QR_TEST__.createPc = () =>
    new Promise((res) => {
      releasePc = () =>
        res({ session_id: 201, status: "pending", qr_url: "u201", qr_image_data_url: "data:img/PC_LATE" });
    });
  G.__QR_TEST__.createCreator = async () => ({
    session_id: 202, status: "pending", qr_url: "u202", qr_image_data_url: "data:img/CREATOR"
  });
  const { el, root } = mount();
  render(root, "pc");
  await waitFor(() => log.createPc.length === 1);
  render(root, "creator");          // supersedes the still-pending PC creation
  await waitFor(() => log.createCreator.length === 1);
  releasePc();                      // the old creation finally answers
  await waitFor(() => qrSrc(el) === "data:img/CREATOR");
  check("S2 late superseded creation did not hijack the panel",
    qrSrc(el) === "data:img/CREATOR", "img=" + qrSrc(el));
  root.unmount();
  el.remove();
}

/* S3 — a poll response for another session must be dropped, not merged. */
async function scenario3() {
  const log = reset();
  G.__QR_TEST__.createPc = async () => ({
    session_id: 301, status: "pending", qr_url: "u301", qr_image_data_url: "data:img/301"
  });
  // Every poll answers with a *different* session id, like a late response.
  G.__QR_TEST__.poll = async () => ({ session_id: 999, status: "pending", qr_url: "u999" });
  const { el, root } = mount();
  render(root, "pc");
  await waitFor(() => log.poll.length >= 2, { timeout: 6000 });  // 原来硬等 4.6s：改成「轮询到 2 次就走」
  const ids = Array.from(new Set(log.poll.map((p) => p.sessionId)));
  check("S3 poller stayed on the displayed session", ids.length === 1 && ids[0] === 301, "polled ids=" + JSON.stringify(ids));
  check("S3 displayed QR unchanged", qrSrc(el) === "data:img/301", "img=" + qrSrc(el));
  check("S3 polling actually happened", log.poll.length >= 2, "poll calls=" + log.poll.length);
  root.unmount();
  el.remove();
}

/* S4 — the qianfan (ark) branch mints ITS OWN session, and only that one. */
async function scenario4() {
  const log = reset();
  G.__QR_TEST__.createQianfan = async () => ({
    session_id: 401, status: "pending", qr_url: "u401", qr_image_data_url: "data:img/QF"
  });
  const { el, root } = mount();
  render(root, "qianfan");
  await waitFor(() => qrSrc(el));
  check("S4 qianfan mount mints exactly one session", log.createQianfan.length === 1, "createQianfan=" + log.createQianfan.length);
  check("S4 qianfan QR is the one displayed", qrSrc(el) === "data:img/QF", "img=" + qrSrc(el));
  // Branch dispatch: a qianfan mount must not fall through to the pc/creator calls.
  check("S4 qianfan did not call pc/creator creators",
    log.createPc.length === 0 && log.createCreator.length === 0,
    "pc=" + log.createPc.length + " creator=" + log.createCreator.length);
  root.unmount();
  el.remove();
}

(async () => {
  await scenario1();
  await scenario2();
  await scenario3();
  await scenario4();
  console.log("@@JSON@@" + JSON.stringify(results));
})();
`;

// --- bundle + run -----------------------------------------------------------
const origLog = console.log;

(async () => {
  const built = await esbuild.build({
    stdin: { contents: ENTRY, resolveDir: path.join(PROJECT, "frontend/src"), loader: "jsx" },
    bundle: true,
    write: false,
    format: "iife",
    platform: "browser",
    logLevel: "silent",
    define: { "process.env.NODE_ENV": '"development"' },
    plugins: [
      {
        // Mutation mode: disable the three guards in-memory so the harness can
        // prove it actually detects the pre-fix behaviour. Nothing on disk is
        // touched — the plugin rewrites the source as esbuild loads it.
        name: "mutate-panel",
        setup(build) {
          const mutate = process.env.QR_MUTATE === "old";
          build.onLoad({ filter: /qr-login-panel\.tsx$/ }, async (args) => {
            const fs = require("fs");
            let contents = fs.readFileSync(args.path, "utf8");
            if (mutate) {
              const before = contents;
              contents = contents
                .replace("if (initialisedForRef.current === accountType) {", "if (false) {")
                .replace(/if \(generation !== createGenerationRef\.current\) \{/g, "if (false) {")
                .replace("if (polled.session_id !== activeSessionIdRef.current) {", "if (false) {")
                .replace("current && current.session_id === sessionId", "true");
              if (contents === before) {
                throw new Error("mutation matched nothing; guards were renamed");
              }
            }
            return { contents, loader: "tsx" };
          });
        }
      },
      {
        name: "mock-lib-api",
        setup(build) {
          build.onResolve({ filter: /lib\/api$/ }, () => ({ path: "mock-api", namespace: "mockapi" }));
          build.onLoad({ filter: /.*/, namespace: "mockapi" }, () => ({ contents: MOCK_API, loader: "js" }));
        }
      },
      {
        name: "stub-css",
        setup(build) {
          build.onResolve({ filter: /\.css$/ }, (args) => ({ path: args.path, namespace: "cssstub" }));
          build.onLoad({ filter: /.*/, namespace: "cssstub" }, () => ({ contents: "", loader: "js" }));
        }
      }
    ]
  });

  const code = built.outputFiles[0].text;

  const timer = setTimeout(() => {
    origLog("@@TIMEOUT@@ no result produced");
    process.exit(2);
  }, 40000);

  console.log = (...args) => {
    const line = args.join(" ");
    if (line.startsWith("@@JSON@@")) {
      clearTimeout(timer);
      const results = JSON.parse(line.slice("@@JSON@@".length));
      let failed = 0;
      for (const r of results) {
        if (!r.ok) failed += 1;
        origLog(`${r.ok ? "PASS" : "FAIL"}  ${r.name}   [${r.detail}]`);
      }
      origLog(failed === 0 ? "\nALL INVARIANTS HOLD" : `\n${failed} INVARIANT(S) VIOLATED`);
      process.exit(failed === 0 ? 0 : 1);
    }
    origLog(...args);
  };

  new Function(code)();
})().catch((err) => {
  origLog("HARNESS ERROR:", err && err.stack ? err.stack : err);
  process.exit(3);
});
