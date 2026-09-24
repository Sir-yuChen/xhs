/**
 * 登录态的**呈现映射**（设计 13 §18.5 / §18.8；2026-09-21 三平台拆分）。
 *
 * 为什么单独成一个模块：这里藏着一条**必须被测到**的规则 ——
 * **`unknown` 绝不能渲染成「已失效」**。用「没检查」冒充「不可用」会让用户去做一次
 * 根本不需要的重登（DEF-057 的同型红线）。而这类「不许出现某种映射」的规则
 * **源码断言证明不了**（把 unknown 的 label 改成「已失效」，源码里照样能找到
 * `unknown` 这三个字母）—— 所以映射必须能被真实驱动，见 `tests/frontend/login_state.cjs`。
 *
 * 状态→颜色/文案是**呈现**；状态本身（谁可用、谁失效）是**业务判断**，由后端给
 * （`backend/app/services/login_state_service.py`）。前端不重新判一次。
 *
 * ## 2026-09-21 拆分（用户拍板）
 *
 * 千帆与创作者平台**不再是同一份令牌**（真机实测：创作者 jar 打千帆一律
 * `-13000 缺失登录凭证`），所以这里是**三个**标签：千帆 / 创作者 / 网页版。
 */

export type LoginStateValue = "missing" | "guest" | "valid" | "expired" | "unknown";

export type LoginStateAction = "" | "login";

export type LoginStateView = {
  /** 归一化后的状态（未识别的取值一律落到 `unknown`，见 `normalizeLoginState`）。 */
  state: LoginStateValue;
  /** 状态点颜色。 */
  color: string;
  /** 状态短标签（≤4 字，卡片窄列放得下）。 */
  label: string;
  /**
   * 该状态允许的动作：`""` 无 / `login` 去登录或续期。
   *
   * ⚠️ **只有「失效 / 未登录」可点**（用户 2026-09-21 口径）：`valid` 点了没意义，
   * `unknown`（未检测）也不给点 —— 进入页面会自动探活（v1.30），真探不出结论时
   * 刷新页面会再探一次（v1.31 起「检查」按钮已移除，不再有第二个手工入口）。
   */
  action: LoginStateAction;
  /** 动作文案（`action` 为空时无意义）。 */
  actionLabel: string;
};

/** 五种状态各一份视图。**五份必须互不相同**（有测试钉住）。 */
const VIEWS: Record<LoginStateValue, LoginStateView> = {
  missing: { state: "missing", color: "#8c8c8c", label: "未登录", action: "login", actionLabel: "登录" },
  guest: { state: "guest", color: "#faad14", label: "非登录态", action: "login", actionLabel: "重新登录" },
  valid: { state: "valid", color: "#52c41a", label: "可用", action: "", actionLabel: "" },
  expired: { state: "expired", color: "#ff4d4f", label: "已失效", action: "login", actionLabel: "重新登录" },
  unknown: { state: "unknown", color: "#8c8c8c", label: "未检测", action: "", actionLabel: "" },
};

/**
 * 把后端给的取值收敛到五种之一。
 *
 * **未识别的取值落到 `unknown`，绝不落到 `expired`**：后端将来新增一个状态时，
 * 最坏的后果应该是「界面说未确认」，而不是「界面替平台宣布令牌已死」。
 * 这是本模块唯一一条**方向性**约定，所以写成显式白名单而不是 `||` 兜底。
 */
export function normalizeLoginState(value: unknown): LoginStateValue {
  if (value === "missing" || value === "guest" || value === "valid" || value === "expired") {
    return value;
  }
  return "unknown";
}

export function loginStateView(value: unknown): LoginStateView {
  return VIEWS[normalizeLoginState(value)];
}

/**
 * 平台的展示顺序：**千帆（ark）在前，创作者（creator）次之，网页版（www）在后**。
 *
 * 用户要的是「千帆登录状态 + PC 登录状态」，先说千帆；同时**顺序在这里写死**
 * 而不是靠对象键的插入顺序（那取决于后端字典的构造顺序，改一处就静默换位）。
 */
export const LOGIN_CHANNEL_ORDER: string[] = ["ark", "creator", "www"];

/** 平台 → 回退展示名（后端会下发 `label`，这里只用于字典缺项时）。
 *
 * ⚠️ 必须与后端 `login_state_service.CHANNEL_LABELS` **逐个相等**
 * （`tests/backend/test_login_state.py` 有一条守卫钉住），且**严格长于**下面的短名
 * （`login_state.cjs` F2 钉住）—— 相等就等于 tag 里塞的是全名，昵称会被挤出卡片。
 */
export const LOGIN_CHANNEL_FALLBACK_LABELS: Record<string, string> = {
  ark: "千帆商家后台",
  creator: "创作者平台",
  www: "网页版（PC）",
};

/**
 * 平台 → 展示名（**唯一实现**）。
 *
 * 原先这个映射在**三个文件**里各有一份：卡片 `loginChannelRows` 的回退、
 * 续期弹窗的 `CHANNEL_TEXT`、登录面板成功提示里的三元表达式 ——
 * 后端给平台改名时，同一屏上会出现两个名字（tag 用新的、弹窗标题用旧的）。
 * 收敛到这里之后只剩一份；`tests/backend/test_login_state.py` 另有一条守卫
 * 断言它**等于**后端 `login_state_service.CHANNEL_LABELS`（回退副本不得与真值漂移）。
 */
export function loginChannelLabel(channel: string): string {
  return LOGIN_CHANNEL_FALLBACK_LABELS[channel] || channel;
}

/**
 * 平台 → **标签里放的短名**（账号名称后面那几个 tag 用的就是它）。
 *
 * 为什么不复用 `LOGIN_CHANNEL_FALLBACK_LABELS`：那些是**全名**（「创作者平台」
 * = 5 字），放在名称后面会把昵称挤没。短名必须**严格短于全名**，且各平台**互不相同**
 * —— 否则两个 tag 长得一样，用户分不清哪个是千帆（两条都断言在
 * `tests/frontend/login_state.cjs` 的 F1/F2，并各有变异开关证明守卫承重）。
 */
export const LOGIN_CHANNEL_SHORT_LABELS: Record<string, string> = {
  ark: "千帆",
  creator: "创作者",
  www: "网页版",
};

export type LoginChannelRow = {
  channel: string;
  /** 平台**全名**（后端下发；弹窗标题用）。 */
  label: string;
  /** 平台**短名**（放进 tag；见 `LOGIN_CHANNEL_SHORT_LABELS`）。 */
  shortLabel: string;
  view: LoginStateView;
  /** 后端给的原因全文（当前 tip 不用它，保留给将来的详情展示）。 */
  reason: string;
  probed: boolean;
};

/**
 * 把一次响应里的 `channels` 摊成**有序、完整**的行。
 *
 * 「完整」是指：**三个平台永远都在** —— 后端没给某个平台时合成 `missing`
 * （而不是不渲染）。缺项消失会让「这个账号没有这个平台的令牌」和「后端没算这个平台」
 * 长得一模一样，而前者才是事实。
 */
export function loginChannelRows(channels: unknown): LoginChannelRow[] {
  const map = (channels && typeof channels === "object" ? channels : {}) as Record<
    string,
    { state?: unknown; reason?: unknown; probed?: unknown; label?: unknown }
  >;
  // 后端将来新增的平台**也保留**（放最后），只是顺序在已知平台之后 ——
  // 静默吞掉一个平台等于让它的令牌状态永远看不见。
  const extra = Object.keys(map).filter((channel) => !LOGIN_CHANNEL_ORDER.includes(channel));
  return [...LOGIN_CHANNEL_ORDER, ...extra].map((channel) => {
    const entry = map[channel];
    const label = String(entry?.label || loginChannelLabel(channel));
    // 短名只用于 tag。未收录的平台（后端新增）退回用平台名本身 —— 它一定短于全名，
    // 也一定和其它平台不同，两个不变量仍然成立。
    const shortLabel = LOGIN_CHANNEL_SHORT_LABELS[channel] || channel;
    if (!entry) {
      // 后端没给这个平台 ⇒ 事实是「没有」，不是「未检测」。
      return {
        channel,
        label,
        shortLabel,
        view: VIEWS.missing,
        reason: "",
        probed: false,
      };
    }
    return {
      channel,
      label,
      shortLabel,
      view: loginStateView(entry.state),
      reason: typeof entry.reason === "string" ? entry.reason : "",
      probed: Boolean(entry.probed),
    };
  });
}

/**
 * 登录态标签的悬停说明（设计 13 §18.14，2026-09-21 精简）。
 *
 * **只留核心**：哪个平台、什么状态、能做什么。用户口径「tip 太长了」——
 * 初版把通道全名 + 后端长原因 + 动作四段全塞进去，一个气泡半屏高。
 * 后端原因（`row.reason`）是给「检测没得到结论」那种场景准备的全文，
 * 标签上放不下也不该放 —— 状态短标签已经把结论说完了。
 *
 * 例：`千帆令牌可用` / `千帆令牌已失效，点击重新登录` / `网页版令牌未登录，点击登录`。
 */
export function loginTagTip(row: LoginChannelRow): string {
  const head = `${row.shortLabel}令牌${row.view.label}`;
  if (row.view.action === "") {
    return head;
  }
  return `${head}，点击${row.view.actionLabel}`;
}

/** 至少有一个平台需要用户动手（用于给卡片加一处汇总提示，可选）。 */
export function needsAttention(channels: unknown): boolean {
  return loginChannelRows(channels).some((row) => row.view.state !== "valid");
}
