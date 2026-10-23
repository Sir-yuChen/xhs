/*
 * 按钮级权限判据（v1.1.0 / 设计 49 §2.2b / 50 §4.2）。
 *
 * ## 唯一的用法
 *
 * ```tsx
 * const can = useCan();
 * <Button disabled={!can("xhs:item:edit")} onClick={...}>编辑</Button>
 * ```
 *
 * 也可以整块隐藏：
 *
 * ```tsx
 * {can("xhs:item:delete") && <Button danger>删除</Button>}
 * ```
 *
 * ## 判据三条（与后端 `require_perm` **严格对应**，别只改一处）
 *
 * 1. `admin` 恒 `true`（后端也是代码派生 `ALL_PERMS`，不查授权表）；
 * 2. 权限未加载 ⇒ `false`（**fail-closed**：宁可先隐藏，也不先显示后消失）；
 * 3. 未登记的码 ⇒ `false`（写错字母当没权限，不 fail-open）。
 *
 * ## ⚠️ 前端判据**不是安全边界**
 *
 * 它只决定「显示不显示」。真正的拦截在后端 —— 任何只在前端隐藏的按钮，
 * 直接调 API 仍会 403。这是设计前提（见 `design 50 §1`）。
 *
 * ## 为什么「禁用」和「隐藏」要分开
 *
 * 设计 49 §2.2b 的判据：
 *  * **写操作**（改了会落库）⇒ 无权限时**隐藏**按钮（不给用户「点了才知道不行」）；
 *  * **读操作**（看明细、翻页）⇒ 无权限时**禁用 + Tooltip 说明**（让用户知道
 *    「这里有东西，但你看不到」，而不是以为功能不存在）。
 *
 * 两种都用 `can()` 判，区别只在渲染方式。
 */

import { usePermission } from "../hooks/use-permission";

export type Can = (code: string | null | undefined) => boolean;

/** 取一个「判某个权限码」的函数。组件里每次渲染都会重算（权限变化即重渲染）。 */
export function useCan(): Can {
  const { has } = usePermission();
  return has;
}

/**
 * 给 antd `Button` 之类拿「无权限时的额外 props」。
 *
 * ```tsx
 * <Button {...guard("xhs:item:delete")} onClick={onDelete}>删除</Button>
 * ```
 * 有权限 ⇒ 原样；无权限 ⇒ `disabled + title`（Tooltip 由 antd `title` 兜底，
 * 不额外包 Tooltip 组件 —— 那个会把按钮的 `disabled` 态搞得很难点中，
 * 反而没法看到提示）。
 */
export function useGuard(): (
  code: string,
  reason?: string
) => { disabled?: boolean; title?: string } {
  const can = useCan();
  return (code: string, reason = "没有操作权限") =>
    can(code) ? {} : { disabled: true, title: reason };
}
