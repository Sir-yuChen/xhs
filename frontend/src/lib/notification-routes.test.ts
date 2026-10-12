import { describe, expect, it } from "vitest";

import { notificationTargetPath } from "./notification-routes";

// 站内消息 → 目标路由映射（R2-C 2026-10-13）。
// 盯三件事：① 有落地页的来源映射到正确路径；② 无落地页的来源返回 null（不猜页面）；
// ③ 缺失 source_type 返回 null（不崩）。

describe("notificationTargetPath", () => {
  it("maps llm gateway alerts to the gateway page", () => {
    expect(notificationTargetPath("llm_gateway_alert", 7)).toBe("/llm-gateway");
  });

  it("maps account / target / task sources to their pages", () => {
    expect(notificationTargetPath("account", 3)).toBe("/platforms/xhs/accounts");
    expect(notificationTargetPath("target", 5)).toBe("/platforms/xhs/monitoring");
    expect(notificationTargetPath("task", 9)).toBe("/tasks");
  });

  it("returns null for unknown sources (no guessing a page)", () => {
    expect(notificationTargetPath("something_new", 1)).toBeNull();
  });

  it("returns null when source_type is missing", () => {
    expect(notificationTargetPath(null, 1)).toBeNull();
    expect(notificationTargetPath(undefined, undefined)).toBeNull();
    expect(notificationTargetPath("", 1)).toBeNull();
  });
});
