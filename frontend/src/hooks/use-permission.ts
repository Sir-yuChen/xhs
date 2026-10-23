/*
 * 权限 store —— 前端「按钮能不能点」的唯一判据（设计 50 §4 / 56 §2）。
 *
 * ## 为什么是 `useSyncExternalStore` 而不是 Context
 *
 * 权限要在**任意组件**里读（每个按钮都读一次）。Context 会让「任何权限变化
 * 重渲染整棵子树」；external store 只在订阅者里触发，且能在**模块顶层**读
 * （`hasPermission()` 不依赖 hook，可以在事件回调 / 工具函数里用）。
 *
 * 与 `use-auth.ts` 同一套模式 —— 那个文件已经这么做了一年多，直接对齐。
 *
 * ## 判据三条（与后端 `require_perm` 严格对应）
 *
 * 1. **admin 恒 true**：`is_admin` 由后端下发（后端也是代码派生 `ALL_PERMS`），
 *    前端不自己判断 `role === "admin"` —— 那样两处各有一份判断，迟早不一致；
 * 2. **未登记 / 空码 恒 false**：写错码就当没权限（与后端 `has_perm` 一致），
 *    **不 fail-open**；
 * 3. **权限未加载时恒 false**：加载中就把按钮藏起来，而不是先显示再消失
 *    （后者会让用户点到一半突然消失）。
 *
 * ## ⚠️ 前端判据**不是安全边界**
 *
 * 它只决定「显示不显示」，真正的拦截在后端 `require_perm`。
 * 任何「只在前端隐藏」的按钮，直接调 API 仍会被 403 —— 这是设计前提，
 * 不是缺陷。反过来：**前端也不能替代后端**，隐藏只是体验，不是防护。
 */

import { useSyncExternalStore } from "react";

import { fetchMyPermissions } from "../lib/api";

export interface PermissionSnapshot {
  status: "idle" | "loading" | "ready" | "error";
  role: string;
  isAdmin: boolean;
  codes: ReadonlySet<string>;
  error: string | null;
}

const EMPTY: ReadonlySet<string> = new Set();

let snapshot: PermissionSnapshot = {
  status: "idle",
  role: "",
  isAdmin: false,
  codes: EMPTY,
  error: null
};

const listeners = new Set<() => void>();

function emit(next: PermissionSnapshot): void {
  snapshot = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): PermissionSnapshot {
  return snapshot;
}

/** 拉一次当前用户的权限码（登录后 / 权限变更后调用）。 */
async function load(): Promise<void> {
  if (snapshot.status === "loading") return;
  emit({ ...snapshot, status: "loading", error: null });
  try {
    const payload = await fetchMyPermissions();
    emit({
      status: "ready",
      role: payload.role,
      isAdmin: Boolean(payload.is_admin),
      codes: new Set(payload.permissions ?? []),
      error: null
    });
  } catch (error) {
    emit({
      status: "error",
      role: "",
      isAdmin: false,
      codes: EMPTY,
      error: error instanceof Error ? error.message : "权限加载失败"
    });
  }
}

/** 清空（登出时调用）—— 不清会让下一个登录的人短暂拿到上一个人的权限。 */
function reset(): void {
  emit({ status: "idle", role: "", isAdmin: false, codes: EMPTY, error: null });
}

/**
 * 判据实现（模块级函数，可在事件回调 / 非组件代码里直接用）。
 *
 * ⚠️ 与后端 `core/perms.py:has_perm` 的三条判据一一对应，别只改一处。
 */
export function hasPermission(code: string | null | undefined): boolean {
  if (!code) return false;
  if (snapshot.isAdmin) return true;
  if (snapshot.status !== "ready") return false;
  return snapshot.codes.has(code);
}

/** 任一条即可（「批量」与「行内」共用一码时用不上，但多语义按钮会用）。 */
export function hasAnyPermission(codes: readonly string[]): boolean {
  if (snapshot.isAdmin) return true;
  if (snapshot.status !== "ready") return false;
  return codes.some((code) => snapshot.codes.has(code));
}

/** 全部都要。 */
export function hasAllPermissions(codes: readonly string[]): boolean {
  if (snapshot.isAdmin) return true;
  if (snapshot.status !== "ready") return false;
  return codes.every((code) => snapshot.codes.has(code));
}

export function usePermission() {
  const state = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return {
    ...state,
    isReady: state.status === "ready",
    has: hasPermission,
    hasAny: hasAnyPermission,
    hasAll: hasAllPermissions,
    reload: load,
    reset
  };
}

/** 供 `use-auth` 在登录 / 登出时调用（避免循环 import，不走 hook）。 */
export const permissionStore = { load, reset, getSnapshot };
