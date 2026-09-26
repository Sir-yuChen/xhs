/**
 * Deterministic harness for the 2026-09-23 binding-dialog rework (设计 13 §18.15)
 * and the 2026-09-23 renew-dialog alignment (设计 13 §18.16).
 *
 * Runs the REAL components (add-account-drawer.tsx / renew-login-modal.tsx /
 * qr-login-panel.tsx / phone-login-panel.tsx / qianfan-email-login-panel.tsx)
 * under jsdom, with ../../lib/api replaced by a controllable stub, and asserts
 * the user-visible contract of the requirements:
 *
 *   A1  绑定弹窗是 Modal 且占全屏 50% 宽 × 75% 高（不是右侧 Drawer）
 *   A2  Cookie 入口不再展示（任何账号类型下都没有「Cookie」档）
 *   A3  「Creator」文案改为「创作者平台」
 *   A4  千帆 tab 有两种登录方式：二维码 / 邮箱密码（切到邮箱密码渲染表单）
 *   A5  二维码面板不再有「登录 PC 后同步 Creator 账号」勾选（任何模式）
 *   A5b PC + 手机验证码链路（绑定弹窗）
 *   A6  补登弹窗千帆档 = 二维码 / 邮箱密码，默认扫码，**无 Cookie 粘贴入口**
 *      （§18.16：补登与绑定弹窗功能相同；§18.15.4 第 3 条被取代）
 *   A7  补登弹窗网页版档 = 二维码 / 手机验证码，默认扫码；扫码/发码都带
 *      target_account_id（只续这一个账号）
 *   A8  补登弹窗创作者档 = 二维码 / 手机验证码（走 creator 扫码通道）
 *
 * Run:
 *   <node> tests/frontend/add_account_modal.cjs                        # expect all PASS
 *   <node> tests/frontend/add_account_modal.cjs --self-check           # one process: contract + all mutations (pytest uses this)
 *   MODAL_MUTATE=drawer <node> tests/frontend/add_account_modal.cjs    # expect A1 FAIL
 *   RENEW_MUTATE=default  <node> tests/frontend/add_account_modal.cjs  # expect A6/A7/A8 FAIL
 *   RENEW_MUTATE=matrix   <node> tests/frontend/add_account_modal.cjs  # expect A7/A8 FAIL
 *   RENEW_MUTATE=notarget <node> tests/frontend/add_account_modal.cjs  # expect A7/A8 FAIL
 *
 * `--self-check` (2026-09-25 P0-4 speedup, see investigation report): contract + all
 * mutations run sequentially in ONE node process and print
 * `SELF <variant> OK|BROKEN <p>/<f>` summary lines for pytest to parse. Before this,
 * each mutation spawned its own process (5x jsdom boot + esbuild); the related cases
 * cost ~100s of the full regression, now ~1 run.
 *
 * jsdom is intentionally NOT a project dependency; point NODE_PATH at its
 * node_modules when installed outside the project.
 */
const path = require("path");
const PROJECT = path.resolve(__dirname, "..", "..");
const esbuild = require(path.join(PROJECT, "frontend/node_modules/esbuild"));

const { JSDOM } = require("jsdom");

// --- jsdom environment (same set as qr_session_identity.cjs) ----------------
const dom = new JSDOM(
  "<!doctype html><html><head></head><body><div id='root'></div></body></html>",
  { pretendToBeVisual: true, url: "http://localhost/" }
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;
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
  G.__MODAL_LOG__.qr.push({ kind: "pc", payload });
  return { session_id: 9001, status: "pending", qr_url: "u", qr_image_data_url: "data:img/pc" };
}
export async function createXhsCreatorQrLoginSession(payload) {
  G.__MODAL_LOG__.qr.push({ kind: "creator", payload });
  return { session_id: 9002, status: "pending", qr_url: "u", qr_image_data_url: "data:img/creator" };
}
export async function createXhsQianfanQrLoginSession(payload) {
  G.__MODAL_LOG__.qr.push({ kind: "qianfan", payload });
  return { session_id: 9003, status: "pending", qr_url: "u", qr_image_data_url: "data:img/qf" };
}
export async function pollXhsLoginSession(sessionId) {
  return { session_id: sessionId, status: "pending", qr_url: "u" };
}
export async function sendXhsPhoneCode(payload) {
  G.__MODAL_LOG__.phone.push(payload);
  return { session_id: 9100, status: "pending", message: "sent" };
}
export async function confirmXhsPhoneLogin(payload) {
  G.__MODAL_LOG__.phone.push(payload);
  return {
    session_id: payload.session_id,
    status: "confirmed",
    account: { id: 78, nickname: "手机号账号", sub_type: "pc", action: "created" },
  };
}
export async function loginXhsQianfanByEmail(payload) {
  G.__MODAL_LOG__.email.push(payload);
  return {
    session_id: null,
    status: "confirmed",
    account: { id: 77, nickname: "观隅好物的店", sub_type: "qianfan", action: "created" },
    renewed_shop: { shop_user_id: "S1", shop_name: "观隅好物的店" },
  };
}
export async function renewXhsQianfanCookie() {
  return { id: 1 };
}
export function detailMessage(detail) {
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (detail && typeof detail === "object" && typeof detail.message === "string") return detail.message;
  return "";
}
`;

const ENTRY = `
import React, { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "antd";
import { AddAccountDrawer } from "./components/account/add-account-drawer";
import { RenewLoginModal } from "./components/account/renew-login-modal";
import { QrLoginPanel } from "./components/account/qr-login-panel";

const G = globalThis;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ⚠️ 等「条件成立」而不是死等固定毫秒（本仓实测固定 sleep 共 12.2s x 6 次调用 = 96s）。
// React 18 render 是异步调度；轮询让「一就绪就继续」。
// ⚠️ 超时必须 > react 首帧耗时但别太大：设成 3s 时，那些「本来就没有 img」的路径
// 会白等满 3 秒（实测把脚本从 5s 拖到 9s）。500ms 足够首帧。
async function waitFor(predicate, { timeout = 500, interval = 5 } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    let ok = false;
    try { ok = !!predicate(); } catch { ok = false; }
    if (ok) return true;
    if (Date.now() > deadline) return false;
    await new Promise((r) => setTimeout(r, interval));
  }
}
const modalEl = () => document.querySelector(".ant-modal");
const modalHas = (needle) => { const m = document.querySelector(".ant-modal"); return !!m && (m.textContent || "").includes(needle); };


function reset() {
  G.__MODAL_LOG__ = { qr: [], email: [], phone: [] };
  return G.__MODAL_LOG__;
}
function mount() {
  const el = document.createElement("div");
  document.getElementById("root").appendChild(el);
  const root = createRoot(el);
  const rawRender = root.render.bind(root);
  // 与真实应用结构一致：组件树整体挂在 antd App 之下 —— 页面/组件里的
  // App.useApp()（消息提醒，设计 04 §3.6）从它拿 message 实例；App 之外
  // 该 hook 返回的是空对象（antd 源码 context 默认值），调用即炸。
  // component={false} 用 Fragment 渲染，不引入额外 DOM 包裹层，
  // 下面所有 .ant-modal / .ant-drawer DOM 断言不受影响。
  root.render = (node) => rawRender(React.createElement(App, { component: false }, node));
  return { el, root };
}
function modalText() {
  const modal = document.querySelector(".ant-modal");
  return modal ? modal.textContent : "";
}
function segmentedLabels() {
  // antd Segmented 的选项渲染成带 role/label 的可点击元素；取全部文案备用
  return Array.from(document.querySelectorAll(".ant-segmented, .ant-segmented-item")).map(
    (el) => el.textContent || ""
  );
}
function clickByText(selector, text) {
  const nodes = Array.from(document.querySelectorAll(selector));
  const hit = nodes.find((el) => (el.textContent || "").trim() === text);
  if (!hit) return false;
  hit.click();
  return true;
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: String(detail) });
}

/* A1 — 绑定弹窗是 50vw × 75vh 的 Modal（不是右侧 420 Drawer）。 */
async function scenarioA1() {
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(AddAccountDrawer, {
      open: true, onClose: function () {}, onBound: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  const modal = document.querySelector(".ant-modal");
  const drawer = document.querySelector(".ant-drawer");
  check("A1 renders a Modal (not a Drawer)", !!modal && !drawer, "modal=" + !!modal + " drawer=" + !!drawer);
  // 宽来自 Modal 的 width prop（落在外层 .ant-modal）；高来自 styles.container
  // （落在内层面板 .ant-modal-container）—— 75vh 是用户当日修正后的口径（初版 50%）。
  const width = modal ? (modal.style.width || "") : "";
  check("A1 modal width is 50% of the viewport", width === "50vw", "width=" + JSON.stringify(width));
  const panel = document.querySelector(".ant-modal-container");
  const height = panel ? (panel.style.height || "") : "";
  check("A1 modal height is 75% of the viewport", height === "75vh", "height=" + JSON.stringify(height));
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A2/A3 — Cookie 入口不在；「创作者平台」文案在。 */
async function scenarioA2A3() {
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(AddAccountDrawer, {
      open: true, onClose: function () {}, onBound: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  const text = modalText();
  check("A2 no Cookie entry anywhere in the dialog", !text.includes("Cookie"), "text has Cookie=" + text.includes("Cookie"));
  check("A3 Creator renamed to 创作者平台", text.includes("创作者平台"), "labels=" + JSON.stringify(segmentedLabels().slice(0, 8)));
  check("A3 old 'Creator' wording gone from the dialog", !text.includes("Creator"), "text has Creator=" + text.includes("Creator"));
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A4 — 千帆 tab：二维码 / 邮箱密码两档，切到邮箱密码渲染表单。 */
async function scenarioA4() {
  const log = reset();
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(AddAccountDrawer, {
      open: true, onClose: function () {}, onBound: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  const clicked = clickByText(".ant-segmented-item", "千帆");
  check("A4 qianfan tab is clickable", clicked, "clicked=" + clicked);
  await waitFor(modalEl);
  let text = modalText();
  check("A4 qianfan methods are 二维码 / 邮箱密码",
    text.includes("邮箱密码") && text.includes("二维码"), "text=" + text.slice(0, 120));
  const switched = clickByText(".ant-segmented-item", "邮箱密码");
  check("A4 can switch to the email method", switched, "switched=" + switched);
  await waitFor(modalEl);
  text = modalText();
  const emailInput = document.querySelector('input[placeholder*="example.com"]');
  const pwdInput = document.querySelector('input[type="password"]');
  check("A4 email/password form renders for qianfan", !!emailInput && !!pwdInput,
    "email=" + !!emailInput + " pwd=" + !!pwdInput);
  // 提交一次：打桩的 api 记录 payload，账号经 onConfirmed 回报
  const button = Array.from(document.querySelectorAll("button")).find(
    (b) => (b.textContent || "").trim() === "登录"
  );
  check("A4 login button present", !!button, "button=" + !!button);
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A4b — 邮箱密码表单**真的能提交**（接线验证）：填值 → 点登录 → payload 形状正确。 */
async function scenarioA4b() {
  const log = reset();
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(AddAccountDrawer, {
      open: true, onClose: function () {}, onBound: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  clickByText(".ant-segmented-item", "千帆");
  await waitFor(modalEl);
  clickByText(".ant-segmented-item", "邮箱密码");
  await waitFor(modalEl);
  const emailInput = document.querySelector('input[placeholder*="example.com"]');
  const pwdInput = document.querySelector('input[type="password"]');
  // React 受控输入：走 native setter + input 事件（同真机抓包脚本的做法）
  const setValue = (input, value) => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value"
    ).set;
    setter.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  setValue(emailInput, "tester@example.com");
  setValue(pwdInput, "s3cret!");
  await waitFor(modalEl);
  const button = Array.from(document.querySelectorAll("button")).find(
    (b) => (b.textContent || "").trim() === "登录"
  );
  check("A4b login button enabled once both fields are filled", !!button && !button.disabled,
    "disabled=" + (button && button.disabled));
  if (button) button.click();
  await waitFor(() => modalHas("已加入账号矩阵"), { timeout: 2000 });
  const sent = log.email[0] || {};
  check("A4b email login submits email + pwd (binding: no target)",
    sent.email === "tester@example.com" && sent.pwd === "s3cret!"
      && !("target_account_id" in sent),
    JSON.stringify(sent));
  check("A4b success notice shown after confirmed",
    (modalText() || "").includes("已加入账号矩阵"), "text=" + (modalText() || "").slice(0, 80));
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A5 — 二维码面板任何模式下都没有同步 Creator 勾选。 */
async function scenarioA5() {
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(QrLoginPanel, {
      accountType: "pc", onConfirmed: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  const text = el.textContent || "";
  const checkbox = el.querySelector('input[type="checkbox"]');
  check("A5 no sync-Creator checkbox in the QR panel",
    !checkbox && !text.includes("同步 Creator"), "checkbox=" + !!checkbox);
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A5b — PC + 手机验证码路径渲染正常（用户 2026-09-23 报过这条链路）。 */
async function scenarioA5b() {
  const log = reset();
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(AddAccountDrawer, {
      open: true, onClose: function () {}, onBound: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  const switched = clickByText(".ant-segmented-item", "手机验证码");
  check("A5b PC tab can switch to phone method", switched, "switched=" + switched);
  await waitFor(modalEl);
  const phoneInput = document.querySelector('input[placeholder="请输入手机号"]');
  const codeInput = document.querySelector('input[placeholder="请输入验证码"]');
  check("A5b phone + code inputs render", !!phoneInput && !!codeInput,
    "phone=" + !!phoneInput + " code=" + !!codeInput);
  const sendBtn = Array.from(document.querySelectorAll("button")).find(
    (b) => (b.textContent || "").trim() === "发送验证码"
  );
  check("A5b send-code button present", !!sendBtn, "button=" + !!sendBtn);
  // 填手机号 → 点发送 → 打桩记录 payload（不带 sync_creator）
  const setValue = (input, value) => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value"
    ).set;
    setter.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  setValue(phoneInput, "13800138000");
  await waitFor(modalEl);
  if (sendBtn) sendBtn.click();
  await waitFor(modalEl);
  const sent = log.phone[0] || {};
  check("A5b send-code submits phone without sync_creator",
    sent.phone === "13800138000" && !("sync_creator" in sent),
    JSON.stringify(sent));
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A6 — 补登弹窗千帆档（§18.16）：二维码 / 邮箱密码两档，默认扫码，无粘贴入口。 */
async function scenarioA6() {
  const log = reset();
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(RenewLoginModal, {
      target: {
        account: { id: 8, nickname: "观隅好物" },
        channel: "ark",
        state: "expired"
      },
      onClose: function () {},
      onRenewed: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  const text = modalText();
  check("A6 qianfan renew offers 二维码 / 邮箱密码 (same matrix as binding)",
    text.includes("二维码") && text.includes("邮箱密码"), "text=" + text.slice(0, 200));
  // §18.15.4 第 3 条（Cookie 粘贴）被 §18.16 取代：绑定弹窗千帆档没有粘贴，
  // 「功能相同」要求补登档也没有。
  check("A6 qianfan renew has no Cookie paste entry",
    !text.includes("Cookie") && !text.includes("粘贴"),
    "hasCookie=" + text.includes("Cookie") + " hasPaste=" + text.includes("粘贴"));
  await waitFor(() => document.querySelector(".ant-modal img"), { timeout: 1500 });
  check("A6 qianfan renew shows a QR by default", !!document.querySelector(".ant-modal img"), "img=" + !!document.querySelector(".ant-modal img"));
  // 扫码会话带 target（续期只写这一个账号，不建新行）
  const qrCall = log.qr[0] || {};
  check("A6 qianfan renew QR session carries target_account_id",
    (qrCall.payload || {}).target_account_id === 8, JSON.stringify(qrCall));
  const switched = clickByText(".ant-segmented-item", "邮箱密码");
  check("A6 can switch to the email method", switched, "switched=" + switched);
  await waitFor(modalEl);
  const emailInput = document.querySelector('input[placeholder*="example.com"]');
  check("A6 email form renders in renew mode (target kept)", !!emailInput, "email=" + !!emailInput);
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A7 — 补登弹窗网页版档（§18.16）：二维码 / 手机验证码，默认扫码；切到手机
 * 验证码渲染表单，且发码 payload 带 target_account_id（续期语义）。 */
async function scenarioA7() {
  const log = reset();
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(RenewLoginModal, {
      target: {
        account: { id: 8, nickname: "观隅好物" },
        channel: "www",
        state: "expired"
      },
      onClose: function () {},
      onRenewed: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  const text = modalText();
  check("A7 www renew offers 二维码 / 手机验证码 (same matrix as binding)",
    text.includes("二维码") && text.includes("手机验证码"), "text=" + text.slice(0, 200));
  await waitFor(() => document.querySelector(".ant-modal img"), { timeout: 1500 });
  check("A7 www renew shows a QR by default", !!document.querySelector(".ant-modal img"), "img=" + !!document.querySelector(".ant-modal img"));
  const qrCall = log.qr[0] || {};
  check("A7 www renew QR session carries target_account_id",
    (qrCall.payload || {}).target_account_id === 8, JSON.stringify(qrCall));
  const switched = clickByText(".ant-segmented-item", "手机验证码");
  check("A7 www renew can switch to the phone method", switched, "switched=" + switched);
  await waitFor(modalEl);
  const phoneInput = document.querySelector('input[placeholder="请输入手机号"]');
  const codeInput = document.querySelector('input[placeholder="请输入验证码"]');
  check("A7 phone + code inputs render in renew mode", !!phoneInput && !!codeInput,
    "phone=" + !!phoneInput + " code=" + !!codeInput);
  check("A7 renew notice says only this account is updated",
    (modalText() || "").includes("只更新这一个账号"), "text=" + (modalText() || "").slice(0, 120));
  // 发码接线：填手机号 → 点发送 → payload 带 target_account_id
  // （面板没渲染出来时后续检查自然变红，不让 harness 崩在半路）
  const setValue = (input, value) => {
    if (!input) return;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value"
    ).set;
    setter.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  setValue(phoneInput, "13800138000");
  await waitFor(modalEl);
  const sendBtn = Array.from(document.querySelectorAll("button")).find(
    (b) => (b.textContent || "").trim() === "发送验证码"
  );
  if (sendBtn) sendBtn.click();
  await waitFor(modalEl);
  const sent = log.phone[0] || {};
  check("A7 send-code submits target_account_id in renew mode",
    sent.phone === "13800138000" && sent.target_account_id === 8, JSON.stringify(sent));
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A8 — 补登弹窗创作者档（§18.16）：二维码 / 手机验证码，扫码走 creator 通道。 */
async function scenarioA8() {
  const log = reset();
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(RenewLoginModal, {
      target: {
        account: { id: 9, nickname: "创作者账号" },
        channel: "creator",
        state: "missing"
      },
      onClose: function () {},
      onRenewed: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  const text = modalText();
  check("A8 creator renew offers 二维码 / 手机验证码",
    text.includes("二维码") && text.includes("手机验证码"), "text=" + text.slice(0, 200));
  await waitFor(() => document.querySelector(".ant-modal img"), { timeout: 1500 });
  check("A8 creator renew shows a QR by default", !!document.querySelector(".ant-modal img"), "img=" + !!document.querySelector(".ant-modal img"));
  const qrCall = log.qr[0] || {};
  check("A8 creator renew QR session uses the creator channel with target",
    qrCall.kind === "creator" && (qrCall.payload || {}).target_account_id === 9,
    JSON.stringify(qrCall));
  const switched = clickByText(".ant-segmented-item", "手机验证码");
  check("A8 creator renew can switch to the phone method", switched, "switched=" + switched);
  await waitFor(modalEl);
  const phoneInput = document.querySelector('input[placeholder="请输入手机号"]');
  check("A8 phone input renders for creator renew", !!phoneInput, "phone=" + !!phoneInput);
  const setValue = (input, value) => {
    if (!input) return;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value"
    ).set;
    setter.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  setValue(phoneInput, "13800138000");
  await waitFor(modalEl);
  const sendBtn = Array.from(document.querySelectorAll("button")).find(
    (b) => (b.textContent || "").trim() === "发送验证码"
  );
  if (sendBtn) sendBtn.click();
  await waitFor(modalEl);
  const sent = log.phone[0] || {};
  check("A8 creator send-code submits target_account_id",
    sent.phone === "13800138000" && sent.target_account_id === 9, JSON.stringify(sent));
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A4c — 邮箱 / 密码输入框**默认必须为空**（用户口径；安全属性）。
 *
 * 为什么值得钉一条守卫：预填邮箱/密码需要把它们存到某个地方
 * （localStorage / URL / 后端），与「后端不保留邮箱和密码」直接冲突。
 * 空的输入框不是缺陷，是这条硬约束在界面上的样子。
 *
 * 同场加钉「不邀请浏览器自动填充」：用户实测 Chrome 会把在千帆后台保存的
 * 凭据自动填进来（autoComplete=username/current-password 正是邀请语）——
 * 初始 state 为空挡不住浏览器，属性上就必须关门。
 */
async function scenarioA4c() {
  const { el, root } = mount();
  root.render(
    React.createElement(StrictMode, null, React.createElement(AddAccountDrawer, {
      open: true, onClose: function () {}, onBound: function () {}
    }))
  );
  await waitFor(() => (modalEl() && (modalEl().textContent || "").length > 20));
  clickByText(".ant-segmented-item", "千帆");
  await waitFor(modalEl);
  clickByText(".ant-segmented-item", "邮箱密码");
  await waitFor(modalEl);
  const emailInput = document.querySelector('input[placeholder*="example.com"]');
  const pwdInput = document.querySelector('input[type="password"]');
  check("A4c email input starts empty", !!emailInput && emailInput.value === "",
    "value=" + JSON.stringify(emailInput && emailInput.value));
  check("A4c password input starts empty", !!pwdInput && pwdInput.value === "",
    "value=" + JSON.stringify(pwdInput && pwdInput.value));
  check("A4c email input does not invite autofill",
    emailInput && emailInput.getAttribute("autocomplete") === "off",
    "autocomplete=" + (emailInput && emailInput.getAttribute("autocomplete")));
  check("A4c password input does not invite autofill",
    pwdInput && pwdInput.getAttribute("autocomplete") === "new-password",
    "autocomplete=" + (pwdInput && pwdInput.getAttribute("autocomplete")));
  root.unmount();
  el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

(async () => {
  await scenarioA1();
  await scenarioA2A3();
  await scenarioA4();
  await scenarioA4b();
  await scenarioA4c();
  await scenarioA5();
  await scenarioA5b();
  await scenarioA6();
  await scenarioA7();
  await scenarioA8();
  console.log("@@JSON@@" + JSON.stringify(results));
})();
`;

// --- bundle + run -----------------------------------------------------------
const origLog = console.log;

// §18.16 补登弹窗契约的变异开关：每个开关证明对应守卫承重
// （锚点未命中直接抛错 —— 静默不生效比变红更危险）。
// mutate 由 runOnce 参数传入（单进程多变异，2026-09-25 P0-4 提速）。
const RENEW_RESERVED = {
  // 默认方式从「二维码」改成「手机验证码」
  default: 'const DEFAULT_METHOD: RenewMethod = "qr";',
  // www / creator 两档的「手机验证码」换成「邮箱密码」（矩阵被改）
  matrix: '{ label: "手机验证码", value: "phone" },',
  // 二维码面板不再带 targetAccountId（续期退化成建号）
  notarget: "targetAccountId={target.account.id}",
};
const RENEW_MUTATE_TO = {
  default: 'const DEFAULT_METHOD: RenewMethod = "phone";',
  matrix: '{ label: "邮箱密码", value: "email" },',
  notarget: "targetAccountId={undefined}",
};

// 跑一轮：modalMutate/renewMutate 为空串跑契约，否则跑对应变异。
// 返回 { results, failed, timeout }；不碰 process.exit（单进程多变异靠这个可重入性）。
async function runOnce(modalMutate, renewMutate) {
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
        // Mutation mode: swap the Modal back to a Drawer-ish width so the
        // harness proves A1 is load-bearing. Nothing on disk is touched.
        name: "mutate-modal",
        setup(build) {
          const mutate = modalMutate === "drawer";
          build.onLoad({ filter: /add-account-drawer\.tsx$/ }, async (args) => {
            const fs = require("fs");
            let contents = fs.readFileSync(args.path, "utf8");
            if (mutate) {
              const before = contents;
              // 两个都要动：只改宽不改高，A1 的高度断言照样能抓到（各变各的）
              contents = contents
                .replace('width="50vw"', 'width={420}')
                .replace('height: "75vh"', 'height: "50vh"');
              if (contents === before) {
                throw new Error("mutation matched nothing; size literals were renamed");
              }
            }
            return { contents, loader: "tsx" };
          });
        }
      },
      {
        // §18.16 变异：只动内存里的 bundle，不落盘。三个锚点都住在
        // renew-login-modal.tsx（矩阵 / 默认方式 / target 接线）。
        name: "mutate-renew",
        setup(build) {
          build.onLoad({ filter: /renew-login-modal\.tsx$/ }, async (args) => {
            const fs = require("fs");
            // ⚠️ 行尾归一化：源码可能是 CRLF，锚点失配会让变异静默不生效。
            let contents = fs.readFileSync(args.path, "utf8").replace(/\r\n/g, "\n");
            if (renewMutate) {
              const needle = RENEW_RESERVED[renewMutate];
              if (!needle) throw new Error(`未知变异：${renewMutate}`);
              if (!contents.includes(needle)) {
                throw new Error(`变异锚点未命中（实现已改？）：${needle}`);
              }
              contents = contents.split(needle).join(RENEW_MUTATE_TO[renewMutate]);
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

  return await new Promise((resolve, reject) => {
    const restore = () => {
      clearTimeout(timer);
      console.log = origLog;
    };
    const timer = setTimeout(() => {
      restore();
      origLog("@@TIMEOUT@@ no result produced");
      resolve({ results: [], failed: -1, timeout: true });
    }, 60000);

    console.log = (...args) => {
      const line = args.join(" ");
      if (line.startsWith("@@JSON@@")) {
        restore();
        const results = JSON.parse(line.slice("@@JSON@@".length));
        let failed = 0;
        for (const r of results) {
          if (!r.ok) failed += 1;
          origLog(`${r.ok ? "PASS" : "FAIL"}  ${r.name}   [${r.detail}]`);
        }
        origLog(failed === 0 ? "\nALL DIALOG CONTRACT HOLDS" : `\n${failed} CHECK(S) FAILED`);
        resolve({ results, failed, timeout: false });
        return;
      }
      origLog(...args);
    };

    try {
      new Function(code)();
    } catch (err) {
      restore();
      reject(err);
    }
  });
}

//: self-check 依次跑的 variant：none 契约（必须全 PASS），其余必须至少一个 FAIL
const MODAL_VARIANTS = [
  { name: "none", modal: "", renew: "" },
  { name: "drawer", modal: "drawer", renew: "" },
  { name: "default", modal: "", renew: "default" },
  { name: "matrix", modal: "", renew: "matrix" },
  { name: "notarget", modal: "", renew: "notarget" },
];

async function selfCheck() {
  let broken = 0;
  for (const v of MODAL_VARIANTS) {
    let r;
    try {
      r = await runOnce(v.modal, v.renew);
    } catch (err) {
      // 打包失败 / 锚点未命中都算 BROKEN —— 静默不生效比变红更危险
      origLog(`SELF ${v.name} BROKEN (throw: ${err && err.message ? err.message : err})`);
      broken += 1;
      continue;
    }
    const expectFail = v.name !== "none";
    const ok = expectFail ? r.failed > 0 : r.failed === 0 && !r.timeout;
    if (!ok) broken += 1;
    origLog(`SELF ${v.name} ${ok ? "OK" : "BROKEN"} ${r.results.length - Math.max(r.failed, 0)}/${r.results.length}`);
  }
  return broken === 0 ? 0 : 1;
}

if (process.argv.includes("--self-check")) {
  selfCheck()
    .then((code) => process.exit(code))
    .catch((err) => {
      origLog("HARNESS ERROR:", err && err.stack ? err.stack : err);
      process.exit(3);
    });
} else {
  runOnce(process.env.MODAL_MUTATE || "", process.env.RENEW_MUTATE || "")
    .then((r) => process.exit(r.failed === 0 && !r.timeout ? 0 : 1))
    .catch((err) => {
      origLog("HARNESS ERROR:", err && err.stack ? err.stack : err);
      process.exit(3);
    });
}
