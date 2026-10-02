import { describe, expect, it } from "vitest";

import {
  buildProviderOptions,
  commitDecision,
  hasNoCatalog,
  isAllCatalogEmpty,
  isSelectableForRow,
  isStepOneLocked,
  type ProviderCandidate,
  type ProviderOption,
} from "./provider-options";

/** 造一个最小服务商候选。 */
function prov(
  provider_key: string,
  name: string,
  models: string[] | null | undefined,
  enabled = true,
): ProviderCandidate {
  return { provider_key, name, enabled, models };
}

describe("hasNoCatalog", () => {
  it("空数组 / null / undefined 都算没有目录（三者对用户是同一件事）", () => {
    expect(hasNoCatalog(prov("A", "a", []))).toBe(true);
    expect(hasNoCatalog(prov("B", "b", null))).toBe(true);
    expect(hasNoCatalog(prov("C", "c", undefined))).toBe(true);
  });

  it("有模型时不算空目录", () => {
    expect(hasNoCatalog(prov("D", "d", ["gpt-4o"]))).toBe(false);
  });
});

describe("buildProviderOptions", () => {
  it("正常服务商：label 就是名称，可选", () => {
    const [opt] = buildProviderOptions([prov("DISCOVERY", "discovery", ["m1"])]);
    expect(opt).toEqual({
      value: "DISCOVERY",
      label: "discovery",
      disabled: false,
      disabledReason: null,
    });
  });

  it("空目录 ⇒ label 标注「无模型目录」且 disabled（核心修正）", () => {
    // 用户曾报「还是报错」：空目录服务商若可选，用户选完在 ② 步永远选不出模型
    // ⇒ 卡在「缺具体模型」且无法自救。这条断言就是那个 bug 的守卫。
    const [opt] = buildProviderOptions([prov("TEST-TEXT", "test-text", [])]);
    expect(opt.label).toBe("test-text（无模型目录）");
    expect(opt.disabled).toBe(true);
    expect(opt.disabledReason).toBe("no_catalog");
  });

  it("已停用 ⇒ label 标注「已停用」且 disabled，且与空目录原因不混淆", () => {
    const [opt] = buildProviderOptions([prov("STOPPED", "stopped", ["m1"], false)]);
    expect(opt.label).toBe("stopped（已停用）");
    expect(opt.disabled).toBe(true);
    expect(opt.disabledReason).toBe("disabled");
  });

  it("「已停用 + 空目录」时优先显示停用（停用是更靠前的原因）", () => {
    const [opt] = buildProviderOptions([prov("X", "x", [], false)]);
    expect(opt.label).toBe("x（已停用）");
    expect(opt.disabled).toBe(true);
  });

  it("不可选的服务商**仍在列表里**（不隐藏），只是灰掉", () => {
    // 「缺值不造假」：用户该看得见「它存在但还差目录」，而不是凭空消失。
    const opts = buildProviderOptions([
      prov("OK", "ok", ["m1"]),
      prov("NOCAT", "nocat", []),
      prov("OFF", "off", ["m2"], false),
    ]);
    expect(opts.map((o) => o.value)).toEqual(["OK", "NOCAT", "OFF"]);
    expect(opts.map((o) => o.disabled)).toEqual([false, true, true]);
  });

  it("空列表 ⇒ 空选项（不造幽灵选项）", () => {
    expect(buildProviderOptions([])).toEqual([]);
  });

  it("顺序保真", () => {
    const opts = buildProviderOptions([
      prov("B", "b", ["m1"]),
      prov("A", "a", ["m2"]),
    ]);
    expect(opts.map((o) => o.value)).toEqual(["B", "A"]);
  });
});

describe("isAllCatalogEmpty", () => {
  it("该类型全部服务商都没目录 ⇒ true（① 步整体置灰 + 一行说明）", () => {
    expect(isAllCatalogEmpty([prov("A", "a", []), prov("B", "b", null)])).toBe(true);
  });

  it("只要有一个有目录 ⇒ false", () => {
    expect(isAllCatalogEmpty([prov("A", "a", []), prov("B", "b", ["m1"])])).toBe(false);
  });

  it("空列表 ⇒ false（此时讲的是「没有服务商」，不是「都没目录」，两种提示不同）", () => {
    expect(isAllCatalogEmpty([])).toBe(false);
  });

  it("停用的服务商有目录时，也不算「都没目录」", () => {
    // 停用是另一码事（用户在列表里看得见、知道去启用）；不能和空目录混成一句提示。
    expect(isAllCatalogEmpty([prov("A", "a", ["m1"], false)])).toBe(false);
  });
});

describe("isStepOneLocked（② 步之前，① 步该不该整体置灰）", () => {
  it("全都没目录 + 本行没绑 ⇒ 锁死（确实无路可走，引导去拉目录）", () => {
    expect(isStepOneLocked(true, "")).toBe(true);
  });

  it("全都没目录 + 本行已绑 ⇒ **不锁**（否则用户被锁死在报错态、无法自救）", () => {
    // 这是用户二次反馈「还是选不了模型」的直接成因。
    expect(isStepOneLocked(true, "TEST-TEXT")).toBe(false);
  });

  it("不是全都没目录 ⇒ 从不锁", () => {
    expect(isStepOneLocked(false, "")).toBe(false);
    expect(isStepOneLocked(false, "DISCOVERY")).toBe(false);
  });
});

describe("commitDecision（选服务商阶段不许落库 —— 死循环根因守卫）", () => {
  it("① 步选了服务商、还没选模型 ⇒ hold（此刻 PUT 必 400 ⇒ ② 步永远打不开）", () => {
    // 这是用户报「选了服务商 -> 报必选模型的错 -> 模型却选不了」的直接根因。
    expect(commitDecision("DISCOVERY-EUSK", "")).toBe("hold");
  });

  it("② 步服务商 + 模型齐全 ⇒ commit（一次性落库，合法）", () => {
    expect(commitDecision("DISCOVERY-EUSK", "deepseek-v4-flash-0731")).toBe("commit");
  });

  it("清空服务商 ⇒ commit（`provider_key=\"\"` 是合法请求，后端连带清空模型）", () => {
    expect(commitDecision("", "")).toBe("commit");
  });

  it("只清模型、服务商保留 ⇒ hold（回到中间态，应由 ① 步清空或 ② 步重选收口）", () => {
    expect(commitDecision("DISCOVERY-EUSK", "")).toBe("hold");
  });
});

describe("isSelectableForRow（用户二次反馈「还是选不了模型」的守卫）", () => {
  // 场景：本行已绑 `TEST-TEXT`（无目录），而该类型**只有** `TEST-TEXT`。
  //   若 ① 步整体置灰 + 空目录项一律 disabled ⇒ 用户被锁死：合法值选不了（② 步必然空），
  //   连「清空」也点不了 ⇒ 报错但不可自救。这条规则保证已绑定项永远可选。
  const bound = prov("TEST-TEXT", "test-text", []);
  const option: ProviderOption = buildProviderOptions([bound])[0];

  it("已绑定项即使 disabled 也必须对该行可选（否则无法清空/改选 ⇒ 锁死）", () => {
    expect(option.disabled).toBe(true); // 规则本身仍不可选
    expect(isSelectableForRow(option, "TEST-TEXT")).toBe(true); // 但对本行放行
  });

  it("未绑定时，空目录项照旧不可选（新选仍被拦住，不放进死胡同）", () => {
    expect(isSelectableForRow(option, "")).toBe(false);
  });

  it("绑定的是别的服务商时，空目录项不可选（不能新选它）", () => {
    expect(isSelectableForRow(option, "OTHER")).toBe(false);
  });

  it("正常可选的服务商任何情况都可选", () => {
    const ok = buildProviderOptions([prov("DISCOVERY", "discovery", ["m1"])])[0];
    expect(isSelectableForRow(ok, "")).toBe(true);
    expect(isSelectableForRow(ok, "DISCOVERY")).toBe(true);
    expect(isSelectableForRow(ok, "OTHER")).toBe(true);
  });

  it("已停用且已绑定 ⇒ 仍可对该行选中（用户得能重新选它或清空）", () => {
    const off = buildProviderOptions([prov("STOPPED", "stopped", ["m1"], false)])[0];
    expect(isSelectableForRow(off, "STOPPED")).toBe(true);
    expect(isSelectableForRow(off, "")).toBe(false);
  });
});
