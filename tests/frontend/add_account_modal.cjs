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
 *   A9  续期成功后弹窗**自动关闭**（三种方式各验一次；2026-09-28 用户实测
 *      「手机号登录成功后重登窗口未自动关闭」补的守卫）
 *   A10 提交后出现蒙版、成功后蒙版随窗口消失（三种方式各验一次）
 *   A11 关窗判据 = 后端返回成功（回 pending 时必须**不关窗**）
 *   A12 登录窗口页面级等待上限 = 5 分钟
 *
 * Run:
 *   <node> tests/frontend/add_account_modal.cjs                        # expect all PASS
 *   <node> tests/frontend/add_account_modal.cjs --self-check           # 契约 only（pytest 默认用这个）
 *   <node> tests/frontend/add_account_modal.cjs --self-check --full    # 契约 + 全部变异
 *   MODAL_MUTATE=drawer <node> tests/frontend/add_account_modal.cjs    # expect A1 FAIL
 *   RENEW_MUTATE=default  <node> tests/frontend/add_account_modal.cjs  # expect A6/A7/A8 FAIL
 *   RENEW_MUTATE=matrix   <node> tests/frontend/add_account_modal.cjs  # expect A7/A8 FAIL
 *   RENEW_MUTATE=notarget <node> tests/frontend/add_account_modal.cjs  # expect A7/A8 FAIL
 *   MASK_MUTATE=nomask   <node> tests/frontend/add_account_modal.cjs  # expect A10 FAIL
 *   LOGIN_MUTATE=loginconfirm <node> tests/frontend/add_account_modal.cjs  # expect A11 FAIL
 *   LOGIN_MUTATE=shortwait <node> tests/frontend/add_account_modal.cjs  # expect A12 FAIL
 *
 * `--self-check` (2026-09-25 P0-4 speedup, see investigation report): contract + all
 * mutations run sequentially in ONE node process and print
 * `SELF <variant> OK|BROKEN <p>/<f>` summary lines for pytest to parse. Before this,
 * each mutation spawned its own process (5x jsdom boot + esbuild); the related cases
 * cost ~100s of the full regression, now ~1 run.
 *
 * ⚠️ **2026-10-14 拆分（用户口径「拆」）**：实测契约 **38s**、每个变异 **11s** ⇒
 * 旧的全跑模式固定花 ~126s，且它在 pytest 的模块级 fixture 里**串行**执行
 * （xdist 帮不上忙），是全量回归长期 10+ 分钟的主因之一。
 * 现在默认**只跑契约**（38s），变异走 `--full`（改本脚本 / 改弹窗时跑）。
 * ⚠️ 拆的是「跑哪些」不是「判什么」：`login_state.cjs` 同理已拆分。
 * 守卫 `test_frontend_scripts_expose_a_full_mutation_mode` 钉住两件事：
 * 两个脚本都支持 `--full`，且**变异清单非空**（否则拆分可能把变异悄悄丢光）。
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
// 可控延迟（默认 0）：A10 要断言「提交后出现蒙版」，而蒙版只在请求在途时存在
// —— 桩立即 resolve 的话蒙版一帧就没了，测不到（假绿）。置位后所有提交类接口
// 都慢一拍，让蒙版可观察。
const wait = () => new Promise((r) => setTimeout(r, G.__MODAL_DELAY_MS__ || 0));
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
  await wait();
  // A9（续期成功必须关窗）要驱动「扫码确认」这一格：默认仍是 pending（其它用例
  // 依赖它），只有显式置位才回 confirmed。**默认关闭**，不改变既有用例的行为。
  if (G.__MODAL_QR_CONFIRM__) {
    return {
      session_id: sessionId,
      status: "confirmed",
      account: { id: 79, nickname: "扫码账号", sub_type: "pc", action: "renewed" },
      renewed_channel: "www",
    };
  }
  // A10 要观察「已扫码、正在确认」这一段的蒙版：置位后先回 scanned，等用例把它
  // 关掉再回 confirmed（模拟真机「扫了 → 手机端点确认」两步）。
  if (G.__MODAL_QR_SCANNED__) {
    return { session_id: sessionId, status: "scanned" };
  }
  return { session_id: sessionId, status: "pending", qr_url: "u" };
}
export async function sendXhsPhoneCode(payload) {
  G.__MODAL_LOG__.phone.push(payload);
  return { session_id: 9100, status: "pending", message: "sent" };
}
export async function confirmXhsPhoneLogin(payload) {
  G.__MODAL_LOG__.phone.push(payload);
  await wait();
  // A11 要验「后端未确认成功 ⇒ 不关窗」：__MODAL_CONFIRM_STATUS__ 可把结论改成
  // pending 之类。默认 confirmed（其它用例依赖成功路径）。
  return {
    session_id: payload.session_id,
    status: G.__MODAL_CONFIRM_STATUS__ || "confirmed",
    account: { id: 78, nickname: "手机号账号", sub_type: "pc", action: "created" },
  };
}
export async function loginXhsQianfanByEmail(payload) {
  G.__MODAL_LOG__.email.push(payload);
  await wait();
  return {
    session_id: null,
    status: G.__MODAL_CONFIRM_STATUS__ || "confirmed",
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
import { LOGIN_WAIT_LIMIT_MS } from "./lib/login-state";

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
  G.__MODAL_QR_CONFIRM__ = false;
  G.__MODAL_QR_SCANNED__ = false;
  G.__MODAL_DELAY_MS__ = 0;
  G.__MODAL_CONFIRM_STATUS__ = "";
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

/* A9 / A10 共用的表单操作与挂载辅助。
 * ⚠️ 本段住在 ENTRY 模板串里 ⇒ 注释与代码都不许出现反引号。 */
function setInputValue(input, value) {
  if (!input) return;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, "value"
  ).set;
  setter.call(input, value);
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
}
function findButton(text) {
  return Array.from(document.querySelectorAll("button")).find(
    (b) => (b.textContent || "").trim() === text
  );
}
/**
 * 挂载补登弹窗，并把 onClose 记进 closed 数组。
 *
 * ⚠️ onClose 里**把 target 置回 null 重新渲染** —— 真实父组件就是这么关窗的
 * （open={target !== null}）。少了这一步，harness 里的弹窗永远开着，
 * 「成功后蒙版随窗口消失」这条根本测不到（组件没被卸载 ⇒ 蒙版还在）。
 */
function mountRenewModal(target) {
  const closed = [];
  const { el, root } = mount();
  const render = (current) => root.render(
    React.createElement(StrictMode, null, React.createElement(RenewLoginModal, {
      target: current,
      onClose: function () { closed.push(true); render(null); },
      onRenewed: function () {}
    }))
  );
  render(target);
  return { el, root, closed };
}
const maskEl = () => document.querySelector('[data-testid="login-submit-mask"]');

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

/* A9 — 续期成功后弹窗必须**自动关闭**（2026-09-26 用户口径；2026-09-28 用户实测
 * 「手机号登录成功后重登窗口未自动关闭」补的守卫）。
 *
 * 覆盖手机验证码 / 千帆邮箱两档；**二维码那一格由 A10 覆盖**（它同时断言 onClose
 * 与蒙版，不必再等一轮 2000ms 轮询）。
 *
 * 为什么这条必须钉住：三种登录方式（二维码 / 手机验证码 / 千帆邮箱）各自有**独立**
 * 的 onRenewed 接线，少接一处就只有那一种方式不关窗 —— 而 A6/A7/A8 只验「渲染出
 * 了哪些档」，对「成功后关不关窗」一无所知。用户就是这么踩到的：手机档直连
 * onRenewed 而绕过了带 onClose() 的 handleRenewed。
 *
 * 判据是 **onClose 真的被调到**，不是「DOM 里没有 .ant-modal」—— 后者在组件
 * 根本没挂上时也成立，等于没判（假绿）。
 *
 * ⚠️ 本段住在 ENTRY 模板串里 ⇒ **注释与代码都不许出现反引号**（会提前终止模板串，
 * 报 SyntaxError: Unexpected identifier）。
 */
async function scenarioA9() {
  // 操作辅助与挂载器已提到 ENTRY 顶层（A9 / A10 共用），这里只取别名。
  const setValue = setInputValue;
  const mountRenew = mountRenewModal;
  const WWW = { account: { id: 8, nickname: "观隅好物" }, channel: "www", state: "expired" };
  const ARK = { account: { id: 8, nickname: "观隅好物" }, channel: "ark", state: "expired" };

  /* ① 手机验证码 */
  reset();
  let h = mountRenew(WWW);
  await waitFor(modalEl);
  clickByText(".ant-segmented-item", "手机验证码");
  await waitFor(() => document.querySelector('input[placeholder="请输入手机号"]'));
  setValue(document.querySelector('input[placeholder="请输入手机号"]'), "13800138000");
  await waitFor(() => findButton("发送验证码"));
  const sendBtn = findButton("发送验证码");
  if (sendBtn) sendBtn.click();
  // ⚠️ 必须等**发码真的回来**（session_id 落进 state）再点确认 —— 否则
  // handleConfirm 走 if (!sessionId) { warning("请先发送验证码。"); return; }
  // 直接返回，onClose 永远为 0，用例会误判成「关窗逻辑坏了」（假红）。
  // DOM 信号：发码成功后状态行变「验证码已发送，请查看手机短信」。
  await waitFor(() => (modalText() || "").includes("验证码已发送"), { timeout: 3000 });
  setValue(document.querySelector('input[placeholder="请输入验证码"]'), "123456");
  await waitFor(() => findButton("确认并更新令牌"));
  const phoneConfirm = findButton("确认并更新令牌");
  if (phoneConfirm) phoneConfirm.click();
  await waitFor(() => h.closed.length > 0, { timeout: 3000 });
  check("A9 手机验证码续期成功后自动关闭弹窗", h.closed.length > 0,
    "onClose=" + h.closed.length + " phoneLog=" + JSON.stringify(G.__MODAL_LOG__.phone)
    + " tail=" + (modalText() || "").slice(-80));
  h.root.unmount();
  h.el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });

  /* ② 千帆邮箱密码 */
  reset();
  h = mountRenew(ARK);
  await waitFor(modalEl);
  clickByText(".ant-segmented-item", "邮箱密码");
  await waitFor(() => document.querySelector('input[placeholder="name@example.com"]'));
  setValue(document.querySelector('input[placeholder="name@example.com"]'), "ops@example.com");
  setValue(document.querySelector('input[placeholder="千帆商家后台密码"]'), "secret123");
  // 按钮在两个输入框都非空前是 disabled 的（React state 异步 ⇒ 等它可用）
  await waitFor(() => { const b = findButton("登录"); return b && !b.disabled; }, { timeout: 1500 });
  const emailBtn = findButton("登录");
  if (emailBtn) emailBtn.click();
  await waitFor(() => h.closed.length > 0, { timeout: 3000 });
  check("A9 千帆邮箱续期成功后自动关闭弹窗", h.closed.length > 0, "onClose=" + h.closed.length);
  h.root.unmount();
  h.el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });

}

/* A10 — 提交后必须出现蒙版，成功后蒙版随窗口一起消失（2026-09-28 用户口径
 * 「所有登录窗口提交后加蒙版，登录成功后自动关闭蒙版和窗口」）。
 *
 * 三种登录方式各验一次：蒙版本身是共享件（login-submit-mask.tsx），但**触发条件
 * 各写各的**（phone 的 isConfirming / email 的 isSubmitting / QR 的 status=scanned）
 * —— 漏接一处就只有那一种方式没有蒙版，而 A9 只看「关不关窗」，对这种漏接一无所知。
 *
 * ⚠️ 桩要慢一拍（__MODAL_DELAY_MS__）才观察得到蒙版：桩立即 resolve 的话蒙版一帧
 * 就没了，断言恒绿（假绿）。承重变异：MASK_MUTATE=nomask（让共享件永远返回 null）。
 */
async function scenarioA10() {
  const WWW = { account: { id: 8, nickname: "观隅好物" }, channel: "www", state: "expired" };
  const ARK = { account: { id: 8, nickname: "观隅好物" }, channel: "ark", state: "expired" };

  /* 1 手机验证码 */
  reset();
  G.__MODAL_DELAY_MS__ = 400;
  let h = mountRenewModal(WWW);
  await waitFor(modalEl);
  clickByText(".ant-segmented-item", "手机验证码");
  await waitFor(() => document.querySelector('input[placeholder="请输入手机号"]'));
  setInputValue(document.querySelector('input[placeholder="请输入手机号"]'), "13800138000");
  await waitFor(() => findButton("发送验证码"));
  const sendBtnA9 = findButton("发送验证码");
  if (sendBtnA9) sendBtnA9.click();
  await waitFor(() => (modalText() || "").includes("验证码已发送"), { timeout: 3000 });
  setInputValue(document.querySelector('input[placeholder="请输入验证码"]'), "123456");
  await waitFor(() => findButton("确认并更新令牌"));
  const confirmBtn = findButton("确认并更新令牌");
  if (confirmBtn) confirmBtn.click();
  const phoneMask = await waitFor(maskEl, { timeout: 1500 });
  check("A10 手机验证码提交后出现蒙版", phoneMask, "mask=" + !!maskEl());
  await waitFor(() => h.closed.length > 0, { timeout: 3000 });
  await waitFor(() => !maskEl(), { timeout: 1500 });
  check("A10 手机验证码成功后蒙版随窗口消失",
    h.closed.length > 0 && !maskEl(), "onClose=" + h.closed.length + " mask=" + !!maskEl());
  h.root.unmount();
  h.el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });

  /* 2 千帆邮箱密码 */
  reset();
  G.__MODAL_DELAY_MS__ = 400;
  h = mountRenewModal(ARK);
  await waitFor(modalEl);
  clickByText(".ant-segmented-item", "邮箱密码");
  await waitFor(() => document.querySelector('input[placeholder="name@example.com"]'));
  setInputValue(document.querySelector('input[placeholder="name@example.com"]'), "ops@example.com");
  setInputValue(document.querySelector('input[placeholder="千帆商家后台密码"]'), "secret123");
  await waitFor(() => { const b = findButton("登录"); return b && !b.disabled; }, { timeout: 1500 });
  const emailBtn = findButton("登录");
  if (emailBtn) emailBtn.click();
  const emailMask = await waitFor(maskEl, { timeout: 1500 });
  check("A10 千帆邮箱提交后出现蒙版", emailMask, "mask=" + !!maskEl());
  await waitFor(() => h.closed.length > 0, { timeout: 3000 });
  await waitFor(() => !maskEl(), { timeout: 1500 });
  check("A10 千帆邮箱成功后蒙版随窗口消失",
    h.closed.length > 0 && !maskEl(), "onClose=" + h.closed.length + " mask=" + !!maskEl());
  h.root.unmount();
  h.el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });

  /* 3 二维码：已扫码等手机端确认这一段必须盖住（轮询 2000ms，超时给 10s） */
  reset();
  G.__MODAL_QR_SCANNED__ = true;
  h = mountRenewModal(WWW);
  await waitFor(modalEl);
  // ⚠️ 显式切到「二维码」：不依赖 DEFAULT_METHOD（否则 default 变异会把本用例
  // 变成「因为默认方式变了才红」，与它要守的东西无关，信号被搅浑）。
  clickByText(".ant-segmented-item", "二维码");
  const qrMask = await waitFor(maskEl, { timeout: 10000 });
  check("A10 二维码已扫码等待确认时出现蒙版", qrMask, "mask=" + !!maskEl());
  G.__MODAL_QR_SCANNED__ = false;
  G.__MODAL_QR_CONFIRM__ = true;
  await waitFor(() => h.closed.length > 0, { timeout: 10000 });
  await waitFor(() => !maskEl(), { timeout: 1500 });
  check("A10 二维码登录成功后蒙版随窗口消失",
    h.closed.length > 0 && !maskEl(), "onClose=" + h.closed.length + " mask=" + !!maskEl());
  G.__MODAL_QR_CONFIRM__ = false;
  h.root.unmount();
  h.el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A11 — 关窗判据 = **后端返回成功**（2026-09-28 用户口径「根据后端接口返回的成功与否
 * 进行判断是否关闭弹窗」）。
 *
 * 为什么单独立一条：A9 只验「成功后关窗」，验不出**失败也关窗**这种反向缺陷 ——
 * 手机验证码档原来根本不看 status（HTTP 200 就当成功），千帆邮箱档看的是
 * result.account 这个派生字段。本用例把桩的结论改成 pending（HTTP 仍是 200），
 * 断言弹窗**必须留着**，且不回调 onRenewed。
 *
 * 承重变异：LOGIN_MUTATE=loginconfirm（让 loginConfirmed 恒真）。
 */
async function scenarioA11() {
  const WWW = { account: { id: 8, nickname: "观隅好物" }, channel: "www", state: "expired" };
  const ARK = { account: { id: 8, nickname: "观隅好物" }, channel: "ark", state: "expired" };

  /* 1 手机验证码：后端回 pending */
  reset();
  G.__MODAL_CONFIRM_STATUS__ = "pending";
  let h = mountRenewModal(WWW);
  await waitFor(modalEl);
  clickByText(".ant-segmented-item", "手机验证码");
  await waitFor(() => document.querySelector('input[placeholder="请输入手机号"]'));
  setInputValue(document.querySelector('input[placeholder="请输入手机号"]'), "13800138000");
  await waitFor(() => findButton("发送验证码"));
  const sendBtn11 = findButton("发送验证码");
  if (sendBtn11) sendBtn11.click();
  await waitFor(() => (modalText() || "").includes("验证码已发送"), { timeout: 3000 });
  setInputValue(document.querySelector('input[placeholder="请输入验证码"]'), "123456");
  await waitFor(() => findButton("确认并更新令牌"));
  const confirmBtn11 = findButton("确认并更新令牌");
  if (confirmBtn11) confirmBtn11.click();
  // 等到请求真的回来（桩里记了 confirm 那次调用）再判「关没关」
  await waitFor(() => (G.__MODAL_LOG__.phone || []).length >= 2, { timeout: 3000 });
  await waitFor(() => false, { timeout: 300 });
  check("A11 手机验证码：后端回 pending 时不关窗",
    h.closed.length === 0 && !!maskEl() === false && !!modalEl(),
    "onClose=" + h.closed.length + " modal=" + !!modalEl());
  h.root.unmount();
  h.el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });

  /* 2 千帆邮箱：后端回 pending */
  reset();
  G.__MODAL_CONFIRM_STATUS__ = "pending";
  h = mountRenewModal(ARK);
  await waitFor(modalEl);
  clickByText(".ant-segmented-item", "邮箱密码");
  await waitFor(() => document.querySelector('input[placeholder="name@example.com"]'));
  setInputValue(document.querySelector('input[placeholder="name@example.com"]'), "ops@example.com");
  setInputValue(document.querySelector('input[placeholder="千帆商家后台密码"]'), "secret123");
  await waitFor(() => { const b = findButton("登录"); return b && !b.disabled; }, { timeout: 1500 });
  const emailBtn11 = findButton("登录");
  if (emailBtn11) emailBtn11.click();
  await waitFor(() => (G.__MODAL_LOG__.email || []).length >= 1, { timeout: 3000 });
  await waitFor(() => false, { timeout: 300 });
  check("A11 千帆邮箱：后端回 pending 时不关窗",
    h.closed.length === 0 && !!modalEl(),
    "onClose=" + h.closed.length + " modal=" + !!modalEl());
  h.root.unmount();
  h.el.remove();
  await waitFor(() => document.querySelectorAll('.ant-modal').length === 0, { timeout: 500 });
}

/* A12 — 页面最多等待 5 分钟（2026-09-28 用户口径）。
 *
 * 数值就是口径本身，所以直接断言这个数：扫码轮询是唯一会无限等下去的路径
 * （手机 / 邮箱两条各由请求层 2 分钟超时兜住），本常量是它的收口闸。
 * 承重变异：LOGIN_MUTATE=shortwait（把 5 分钟改成 300ms）。
 */
function scenarioA12() {
  check("A12 登录窗口页面级等待上限 = 5 分钟",
    LOGIN_WAIT_LIMIT_MS === 5 * 60 * 1000, "limit=" + LOGIN_WAIT_LIMIT_MS);
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
  await scenarioA9();
  await scenarioA10();
  await scenarioA11();
  scenarioA12();
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
  // A9 的承重变异：手机档绕过带 `onClose()` 的 `handleRenewed`，直连 `onRenewed`
  // —— 正是 2026-09-28 用户实测的那个缺陷（手机号登录成功、窗口不关）。
  // ⚠️ 锚点必须带**收尾上下文** `) : (`：二维码档的接线行与手机档**逐字相同**，
  // 只锚那一行会打到二维码档上（变异打偏 ⇒ A9 的二维码那格变红、手机那格照样绿）。
  phoneclose:
    '              onRenewed={(account) => handleRenewed(account?.id ?? target.account.id)}\n            />\n          ) : (',
};
const RENEW_MUTATE_TO = {
  default: 'const DEFAULT_METHOD: RenewMethod = "phone";',
  matrix: '{ label: "邮箱密码", value: "email" },',
  notarget: "targetAccountId={undefined}",
  phoneclose:
    '              onRenewed={(account) => onRenewed(account?.id ?? target.account.id)}\n            />\n          ) : (',
};

// 跑一轮：modalMutate/renewMutate 为空串跑契约，否则跑对应变异。
// 返回 { results, failed, timeout }；不碰 process.exit（单进程多变异靠这个可重入性）。
async function runOnce(modalMutate, renewMutate, maskMutate, loginMutate) {
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
        // A10 的承重变异：让共享蒙版永远返回 null —— 三种登录方式都该因此变红。
        // 锚点住在 login-submit-mask.tsx（共享件），改的是内存里的 bundle，不落盘。
        name: "mutate-mask",
        setup(build) {
          build.onLoad({ filter: /login-submit-mask\.tsx$/ }, async (args) => {
            const fs = require("fs");
            let contents = fs.readFileSync(args.path, "utf8").replace(/\r\n/g, "\n");
            if (maskMutate === "nomask") {
              const needle = "  if (!active) {";
              if (!contents.includes(needle)) {
                throw new Error("蒙版变异锚点未命中（实现已改？）：" + needle);
              }
              contents = contents.replace(needle, "  if (true) {");
            }
            return { contents, loader: "tsx" };
          });
        }
      },
      {
        // A11 的承重变异：让 loginConfirmed 恒真 —— 「后端没确认也关窗」的老缺陷。
        // 锚点住在 lib/login-state.ts（判据的唯一实现），只改内存里的 bundle。
        name: "mutate-login",
        setup(build) {
          build.onLoad({ filter: /login-state\.ts$/ }, async (args) => {
            const fs = require("fs");
            let contents = fs.readFileSync(args.path, "utf8").replace(/\r\n/g, "\n");
            if (loginMutate === "loginconfirm") {
              const needle = 'return String(result?.status ?? "") === "confirmed";';
              if (!contents.includes(needle)) {
                throw new Error("登录判据变异锚点未命中（实现已改？）：" + needle);
              }
              contents = contents.replace(needle, "return true;");
            } else if (loginMutate === "shortwait") {
              // A12 的承重变异：把 5 分钟压成 300ms（口径被改）。
              const needle = "5 * 60 * 1000";
              if (!contents.includes(needle)) {
                throw new Error("等待上限变异锚点未命中（实现已改？）：" + needle);
              }
              contents = contents.replace(needle, "300");
            }
            return { contents, loader: "ts" };
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
  { name: "none", modal: "", renew: "", mask: "", login: "" },
  { name: "drawer", modal: "drawer", renew: "", mask: "", login: "" },
  { name: "default", modal: "", renew: "default", mask: "", login: "" },
  { name: "matrix", modal: "", renew: "matrix", mask: "", login: "" },
  { name: "notarget", modal: "", renew: "notarget", mask: "", login: "" },
  { name: "phoneclose", modal: "", renew: "phoneclose", mask: "", login: "" },
  { name: "nomask", modal: "", renew: "", mask: "nomask", login: "" },
  { name: "loginconfirm", modal: "", renew: "", mask: "", login: "loginconfirm" },
  { name: "shortwait", modal: "", renew: "", mask: "", login: "shortwait" },
];

//: ⚠️ **契约与变异分批**（2026-10-14 用户口径「拆」）：
//:
//: 实测耗时：契约（none）**38s**、每个变异约 **11s** ⇒ `--self-check` 跑满 9 个
//: variant 要 **~126s**，而 pytest 是模块级 fixture 串行跑两个行为脚本
//: （另一个是 `login_state.cjs`）⇒ **每轮全量回归固定多花 2 分钟以上**，
//: 且它跑在单线程里，xdist 的 6 个 worker 谁也帮不上忙（这是全量回归
//: 长期 10+ 分钟的主要来源之一）。
//:
//: 拆分口径：
//: * **默认（`--self-check`）只跑契约** —— 守卫「契约必须全 PASS」这条日常判据；
//: * **`--self-check --full` 才跑全部变异** —— 变异是「证明守卫承重」的判据，
//:   属于**改动本脚本或改弹窗代码时**才需要跑的重活。
//:
//: ⚠️ 拆的是「跑哪些」，不是「判什么」：两种模式下**契约都必须全 PASS**，
//: 且 `--full` 下每个变异仍必须至少让一条断言变红。判据一条不减。
const CONTRACT_VARIANT = MODAL_VARIANTS[0];
const MUTATION_VARIANTS = MODAL_VARIANTS.slice(1);

async function selfCheck({ full = false } = {}) {
  const variants = full ? MODAL_VARIANTS : [CONTRACT_VARIANT];
  let broken = 0;
  for (const v of variants) {
    let r;
    try {
      r = await runOnce(v.modal, v.renew, v.mask, v.login);
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
  //: `--full` 才跑全部变异；默认只跑契约（见上面 CONTRACT_VARIANT 的耗时说明）。
  selfCheck({ full: process.argv.includes("--full") })
    .then((code) => process.exit(code))
    .catch((err) => {
      origLog("HARNESS ERROR:", err && err.stack ? err.stack : err);
      process.exit(3);
    });
} else {
  runOnce(process.env.MODAL_MUTATE || "", process.env.RENEW_MUTATE || "", process.env.MASK_MUTATE || "", process.env.LOGIN_MUTATE || "")
    .then((r) => process.exit(r.failed === 0 && !r.timeout ? 0 : 1))
    .catch((err) => {
      origLog("HARNESS ERROR:", err && err.stack ? err.stack : err);
      process.exit(3);
    });
}
