/*
 * 平台工作区纯函数（设计 62）—— 路由 ↔ workspace 映射与卡片落点。
 *
 * `workspaceOf` 是 AppShell 决定「向后端要哪一片菜单」的唯一判据
 * （`useDynamicNav(workspace)` → `fetchMyMenus(workspace)`），
 * 映射错了 = 侧栏把别的工作区的菜单混进来（正是设计 62 要消灭的形状），
 * 故四个分支逐一钉死。
 */

import { describe, expect, it } from "vitest";

import { workspaceEntryPath, workspaceOf } from "./workspaces";

describe("workspaceOf", () => {
  it("系统管理工作区：/system 前缀 → system", () => {
    expect(workspaceOf("/system")).toBe("system");
    expect(workspaceOf("/system/users")).toBe("system");
    expect(workspaceOf("/system/users?tab=x".split("?")[0])).toBe("system");
  });

  it("平台工作区：/platforms/:id → 该 id", () => {
    expect(workspaceOf("/platforms/xhs")).toBe("xhs");
    expect(workspaceOf("/platforms/xhs/library")).toBe("xhs");
    expect(workspaceOf("/platforms/system")).toBe("system");
    expect(workspaceOf("/platforms/douyin")).toBe("douyin");
  });

  it("footer 工具页与 403 归小红书工作区", () => {
    expect(workspaceOf("/tasks")).toBe("xhs");
    expect(workspaceOf("/task-records")).toBe("xhs");
    expect(workspaceOf("/models")).toBe("xhs");
    expect(workspaceOf("/settings")).toBe("xhs");
    expect(workspaceOf("/403")).toBe("xhs");
  });

  it("其余（含根路径）兜底 xhs", () => {
    expect(workspaceOf("/")).toBe("xhs");
    expect(workspaceOf("/platform-select")).toBe("xhs");
    expect(workspaceOf("/anywhere-else")).toBe("xhs");
  });

  it("不把 /systemx 之类的前缀误判成 /system", () => {
    expect(workspaceOf("/systemx")).toBe("xhs");
    expect(workspaceOf("/platforms/xhsfoo")).toBe("xhsfoo");
  });
});

describe("workspaceEntryPath", () => {
  it("ai 走独立公共模块 /llm-gateway", () => {
    expect(workspaceEntryPath("ai")).toBe("/llm-gateway");
  });

  it("其余工作区按 /platforms/<workspace> 落点", () => {
    expect(workspaceEntryPath("xhs")).toBe("/platforms/xhs");
    expect(workspaceEntryPath("system")).toBe("/platforms/system");
    expect(workspaceEntryPath("douyin")).toBe("/platforms/douyin");
  });
});

// 设计 62 回归守卫（2026-10-25 实测两个断点）：
// ① agent 重写 router 时丢了 /platform-select 路由声明（React Router 匹配不到
//    ⇒ 渲染空白 = 用户看到的「黑屏」）；② PlatformRootRoute 只特判了 system，
//    xhs 工作区卡片点进来落在 ComingSoon。两条都钉在路由源码上（vitest 是
//    node 环境，不引整个 router 组件树）。
// ⚠️ 前端 tsconfig 没有 @types/node —— 读源码用 Vite 的 `?raw` 导入（vitest 同样支持）。
import routerSrc from "../app/router.tsx?raw";

const ROUTER_SRC = routerSrc;

describe("router 平台分发回归守卫（设计 62）", () => {
  it("必须声明 /platform-select 路由（丢了 = 登录后全屏空白）", () => {
    expect(ROUTER_SRC).toContain('path="/platform-select"');
  });

  it("PlatformRootRoute 必须分发 xhs（丢了 = 工作区卡片点进来落在 ComingSoon）", () => {
    const body = ROUTER_SRC.split("function PlatformRootRoute", 2)[1] ?? "";
    expect(body).toContain('"xhs"');
  });
});
