import { WaitMask } from "../ui/wait-mask";

/**
 * 登录提交蒙版（2026-09-28 用户口径：「所有登录窗口提交后加蒙版，登录成功后
 * 自动关闭蒙版和窗口」）。
 *
 * ## 为什么抽成共享件
 *
 * 三个登录面板（二维码 / 手机验证码 / 千帆邮箱）× 两个入口（绑定账号弹窗 /
 * 补登弹窗）= 6 处要盖同一层东西。各写一份必然漂移 —— 真发生过的形态就是
 * 「三个面板里只有一个记得关窗」（2026-09-28 用户实测：手机号登录成功后
 * 补登窗口不关）。所以蒙版**只有这一份实现**（DEF-044 口径）。
 *
 * ## 用法
 *
 * 面板根节点是 `position: relative` 的容器，本件以 `position: absolute; inset: 0`
 * 铺满它：
 *
 * ```tsx
 * <div style={{ position: "relative" }}>
 *   …表单…
 *   <LoginSubmitMask active={isSubmitting} />
 * </div>
 * ```
 *
 * ⚠️ **不改布局尺寸**（`inset: 0` 而不是撑高容器）：提交瞬间面板不会跳一下。
 * ⚠️ 蒙版本身**不负责关窗**：关窗是「成功回调」的职责（面板 `onRenewed` /
 * `onConfirmed`），蒙版只随「提交中」这个布尔量消失 —— 两者都靠同一个 state
 * 收敛，不互相依赖。
 */
export function LoginSubmitMask({
  active,
  text = "正在登录，请稍候…",
}: {
  /** 是否正在提交（`true` 才渲染蒙版；`false` 直接返回 null，不留空节点）。 */
  active: boolean;
  /** 蒙版上的说明文字（各面板可按自己的动作措辞，如「已扫码，正在确认登录…」）。 */
  text?: string;
}) {
  //: ⚠️ 2026-10-20 用户口径「项目中所有等待，蒙版全部应走统一的公共组件」：
  //: 原来是自建黑底 div + antd `<Spin spinning={false}>` ⇒ 现委托 `WaitMask tone="dark" scope="panel"`。
  //: 对外接口（`active` / `text` / `data-testid`）保持不变，6 个调用点零改动。
  return (
    <span data-testid="login-submit-mask" style={{ display: "contents" }}>
      <WaitMask open={active} mask scope="panel" tone="dark" title={text} />
    </span>
  );
}
