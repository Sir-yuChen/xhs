import { describe, expect, it } from "vitest";

import {
  buildModelOptions,
  buildProviderOptions,
  commitDecision,
  hasNoCatalog,
  isAllCatalogEmpty,
  isModelMissing,
  isSelectableForRow,
  isStepOneLocked,
  typeSatisfies,
  usableModels,
  type ProviderCandidate,
  type ProviderOption,
} from "./provider-options";

/** 造一个模型行（v2.7/v2.8：服务商下的模型是结构数组，不是字符串数组；v2.9：platform 多值）。 */
function row(model_name: string, model_type = "text", enabled = true) {
  return { model_name, model_type, platform: { xhs: 1 }, enabled };
}

/**
 * 造一个最小服务商候选。
 *
 * ⚠️ v2.7/v2.8：第三个参数收的是**模型行结构数组**（老签名是 `string[]`）。
 * 传字符串会被 `row()` 包一层，让老用例的 `["m1"]` 读法继续成立、少改噪音。
 */
function prov(
  provider_key: string,
  name: string,
  models: (string | ReturnType<typeof row>)[] | null | undefined,
  enabled = true,
): ProviderCandidate {
  return {
    provider_key,
    name,
    enabled,
    models: models?.map((m) => (typeof m === "string" ? row(m) : m)),
  };
}

describe("typeSatisfies（与后端 _type_satisfies 同判，v2.11）", () => {
  it("同类型满足", () => {
    expect(typeSatisfies("text", "text")).toBe(true);
    expect(typeSatisfies("image", "image")).toBe(true);
    expect(typeSatisfies("video", "video")).toBe(true);
  });

  it("multimodal 同时满足 text 与 image（核心放宽 —— 老代码严格相等会漏掉）", () => {
    // v2.11 修：老代码 `(m.model_type||"text")===kind` 会把多态模型从 text/image 候选里
    // 静默滤掉，而后端放行 ⇒ 用户看得见的候选比可用的少且无提示。
    expect(typeSatisfies("text", "multimodal")).toBe(true);
    expect(typeSatisfies("image", "multimodal")).toBe(true);
  });

  it("方向性不兼容必须拦（文本模型不能出图）", () => {
    expect(typeSatisfies("image", "text")).toBe(false);
    expect(typeSatisfies("text", "image")).toBe(false);
    expect(typeSatisfies("text", "video")).toBe(false);
    expect(typeSatisfies("video", "multimodal")).toBe(false);
  });

  it("空类型 / 未知类型 ⇒ 不满足，不猜（DEF-057）", () => {
    expect(typeSatisfies("text", "")).toBe(false);
    expect(typeSatisfies("text", undefined)).toBe(false);
    expect(typeSatisfies("text", null)).toBe(false);
    expect(typeSatisfies("text", "unknown-type")).toBe(false);
  });
});

describe("usableModels（级联叶子候选）", () => {
  it("按 kind 过滤 + 保序", () => {
    const models = [row("a"), row("b", "image"), row("c")];
    expect(usableModels(models, "text").map((m) => m.model_name)).toEqual(["a", "c"]);
  });

  it("multimodal 同时进 text / image 两类候选", () => {
    const models = [row("mm", "multimodal"), row("t", "text"), row("i", "image")];
    expect(usableModels(models, "text").map((m) => m.model_name)).toEqual(["mm", "t"]);
    expect(usableModels(models, "image").map((m) => m.model_name)).toEqual(["mm", "i"]);
  });

  it("停用行 / 空名行被剔除（选下去调用必失败）", () => {
    const models = [row("ok"), row("off", "text", false), row("")];
    expect(usableModels(models, "text").map((m) => m.model_name)).toEqual(["ok"]);
  });

  it("null / undefined 等价空数组", () => {
    expect(usableModels(null, "text")).toEqual([]);
    expect(usableModels(undefined, "text")).toEqual([]);
  });
});

describe("hasNoCatalog", () => {
  it("空数组 / null / undefined 都算没有目录（三者对用户是同一件事）", () => {
    expect(hasNoCatalog(prov("A", "a", []), "text")).toBe(true);
    expect(hasNoCatalog(prov("B", "b", null), "text")).toBe(true);
    expect(hasNoCatalog(prov("C", "c", undefined), "text")).toBe(true);
  });

  it("有模型时不算空目录", () => {
    expect(hasNoCatalog(prov("D", "d", ["gpt-4o"]), "text")).toBe(false);
  });

  it("v2.7：模型行**全被停用** ⇒ 等同于没有目录（② 步同样选不出可用模型）", () => {
    expect(hasNoCatalog(prov("E", "e", [row("m1", "text", false)]), "text")).toBe(true);
    // 只要有一条启用 ⇒ 不算空
    expect(hasNoCatalog(prov("F", "f", [row("m1", "text", false), row("m2")]), "text")).toBe(false);
  });

  it("v2.7：模型行名为空的占位行不算数", () => {
    expect(hasNoCatalog(prov("G", "g", [row("")]), "text")).toBe(true);
  });

  it("v2.11：**按 kind 判** —— 只有图像模型的服务器对文本业务点算「无目录」", () => {
    const p = prov("H", "h", [row("img", "image")]);
    expect(hasNoCatalog(p, "text")).toBe(true);
    expect(hasNoCatalog(p, "image")).toBe(false);
  });

  it("v2.11：multimodal 模型对 text / image 都算有目录", () => {
    const p = prov("I", "i", [row("mm", "multimodal")]);
    expect(hasNoCatalog(p, "text")).toBe(false);
    expect(hasNoCatalog(p, "image")).toBe(false);
  });
});

describe("buildProviderOptions", () => {
  it("正常服务商：label 就是名称，可选", () => {
    const [opt] = buildProviderOptions([prov("DISCOVERY", "discovery", ["m1"])], "text");
    expect(opt).toEqual({
      value: "DISCOVERY",
      label: "discovery",
      disabled: false,
      disabledReason: null,
    });
  });

  it("无该类型模型 ⇒ label 标注「无模型目录」且 disabled（核心修正）", () => {
    // 用户曾报「还是报错」：空目录服务商若可选，用户选完在 ② 步永远选不出模型
    // ⇒ 卡在「缺具体模型」且无法自救。这条断言就是那个 bug 的守卫。
    const [opt] = buildProviderOptions([prov("TEST-TEXT", "test-text", [])], "text");
    expect(opt.label).toBe("test-text（无模型目录）");
    expect(opt.disabled).toBe(true);
    expect(opt.disabledReason).toBe("no_catalog");
  });

  it("已停用 ⇒ label 标注「已停用」且 disabled，且与空目录原因不混淆", () => {
    const [opt] = buildProviderOptions(
      [prov("STOPPED", "stopped", ["m1"], false)],
      "text",
    );
    expect(opt.label).toBe("stopped（已停用）");
    expect(opt.disabled).toBe(true);
    expect(opt.disabledReason).toBe("disabled");
  });

  it("「已停用 + 空目录」时优先显示停用（停用是更靠前的原因）", () => {
    const [opt] = buildProviderOptions([prov("X", "x", [], false)], "text");
    expect(opt.label).toBe("x（已停用）");
    expect(opt.disabled).toBe(true);
  });

  it("不可选的服务商**仍在列表里**（不隐藏），只是灰掉", () => {
    // 「缺值不造假」：用户该看得见「它存在但还差目录」，而不是凭空消失。
    const opts = buildProviderOptions(
      [prov("OK", "ok", ["m1"]), prov("NOCAT", "nocat", []), prov("OFF", "off", ["m2"], false)],
      "text",
    );
    expect(opts.map((o) => o.value)).toEqual(["OK", "NOCAT", "OFF"]);
    expect(opts.map((o) => o.disabled)).toEqual([false, true, true]);
  });

  it("空列表 ⇒ 空选项（不造幽灵选项）", () => {
    expect(buildProviderOptions([], "text")).toEqual([]);
  });

  it("顺序保真", () => {
    const opts = buildProviderOptions(
      [prov("B", "b", ["m1"]), prov("A", "a", ["m2"])],
      "text",
    );
    expect(opts.map((o) => o.value)).toEqual(["B", "A"]);
  });

  it("v2.11：候选池**保留全部服务商**（含无该类型模型的），由 disabled 表达不可选", () => {
    // 老代码 `.filter(p => p.models.length > 0)` 会把它们整条删掉 ⇒ 用户看不见也修不了。
    const opts = buildProviderOptions(
      [prov("TEXT-ONLY", "t", ["m"]), prov("IMAGE-ONLY", "i", [row("img", "image")])],
      "text",
    );
    expect(opts.map((o) => o.value)).toEqual(["TEXT-ONLY", "IMAGE-ONLY"]);
    expect(opts.map((o) => o.disabled)).toEqual([false, true]);
  });
});

describe("buildModelOptions（级联二级 = 叶子候选）", () => {
  it("取该服务商在 kind 下的可用模型，保序", () => {
    const p = prov("P", "p", [row("b"), row("img", "image"), row("a")]);
    expect(buildModelOptions(p, "text").map((o) => o.value)).toEqual(["b", "a"]);
  });

  it("服务商不存在（undefined）⇒ 空叶子，不抛错", () => {
    expect(buildModelOptions(undefined, "text")).toEqual([]);
  });

  it("leaf label = 模型名（displayRender 直接可读）", () => {
    expect(buildModelOptions(prov("P", "p", ["m1"]), "text")).toEqual([
      { value: "m1", label: "m1" },
    ]);
  });
});

describe("isAllCatalogEmpty", () => {
  it("该类型全部服务商都没模型 ⇒ true（① 级整体置灰 + 一行说明）", () => {
    expect(isAllCatalogEmpty([prov("A", "a", []), prov("B", "b", null)], "text")).toBe(true);
  });

  it("只要有一个有模型 ⇒ false", () => {
    expect(isAllCatalogEmpty([prov("A", "a", []), prov("B", "b", ["m1"])], "text")).toBe(false);
  });

  it("空列表 ⇒ false（此时讲的是「没有服务商」，不是「都没目录」，两种提示不同）", () => {
    expect(isAllCatalogEmpty([], "text")).toBe(false);
  });

  it("停用的服务商有该类型模型时，也不算「都没目录」", () => {
    // 停用是另一码事（用户在列表里看得见、知道去启用）；不能和空目录混成一句提示。
    expect(isAllCatalogEmpty([prov("A", "a", ["m1"], false)], "text")).toBe(false);
  });

  it("v2.11：按 kind 判 —— 全是图像服务商对文本业务点算「都没目录」", () => {
    expect(isAllCatalogEmpty([prov("A", "a", [row("img", "image")])], "text")).toBe(true);
    expect(isAllCatalogEmpty([prov("A", "a", [row("img", "image")])], "image")).toBe(false);
  });
});

describe("isStepOneLocked（② 级之前，① 级该不该整体置灰）", () => {
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
  it("① 级选了服务商、还没选模型 ⇒ hold（此刻 PUT 必 400 ⇒ ② 级永远打不开）", () => {
    // 这是用户报「选了服务商 -> 报必选模型的错 -> 模型却选不了」的直接根因。
    expect(commitDecision("DISCOVERY-EUSK", "")).toBe("hold");
  });

  it("② 级服务商 + 模型齐全 ⇒ commit（一次性落库，合法）", () => {
    expect(commitDecision("DISCOVERY-EUSK", "deepseek-v4-flash-0731")).toBe("commit");
  });

  it("清空服务商 ⇒ commit（`provider_key=\"\"` 是合法请求，后端连带清空模型）", () => {
    expect(commitDecision("", "")).toBe("commit");
  });

  it("只清模型、服务商保留 ⇒ hold（回到中间态，应由 ① 级清空或 ② 级重选收口）", () => {
    expect(commitDecision("DISCOVERY-EUSK", "")).toBe("hold");
  });
});

describe("isSelectableForRow（用户二次反馈「还是选不了模型」的守卫）", () => {
  // 场景：本行已绑 `TEST-TEXT`（无目录），而该类型**只有** `TEST-TEXT`。
  //   若 ① 级整体置灰 + 空目录项一律 disabled ⇒ 用户被锁死：合法值选不了（② 级必然空），
  //   连「清空」也点不了 ⇒ 报错但不可自救。这条规则保证已绑定项永远可选。
  const bound = prov("TEST-TEXT", "test-text", []);
  const option: ProviderOption = buildProviderOptions([bound], "text")[0];

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
    const ok = buildProviderOptions([prov("DISCOVERY", "discovery", ["m1"])], "text")[0];
    expect(isSelectableForRow(ok, "")).toBe(true);
    expect(isSelectableForRow(ok, "DISCOVERY")).toBe(true);
    expect(isSelectableForRow(ok, "OTHER")).toBe(true);
  });

  it("已停用且已绑定 ⇒ 仍可对该行选中（用户得能重新选它或清空）", () => {
    const off = buildProviderOptions([prov("STOPPED", "stopped", ["m1"], false)], "text")[0];
    expect(isSelectableForRow(off, "STOPPED")).toBe(true);
    expect(isSelectableForRow(off, "")).toBe(false);
  });
});

describe("isModelMissing（库里存的模型已不可用 ⇒ 不静默清空，如实提示）", () => {
  const candidates = [prov("P", "p", [row("a"), row("img", "image"), row("off", "text", false)])];

  it("存的模型可用 ⇒ 不算缺失", () => {
    expect(isModelMissing("P", "a", candidates, "text")).toBe(false);
  });

  it("存的模型被停用 ⇒ 缺失", () => {
    expect(isModelMissing("P", "off", candidates, "text")).toBe(true);
  });

  it("存的模型类型不匹配该业务点 ⇒ 缺失", () => {
    expect(isModelMissing("P", "img", candidates, "text")).toBe(true);
  });

  it("存的模型已不在该服务商下 ⇒ 缺失", () => {
    expect(isModelMissing("P", "ghost", candidates, "text")).toBe(true);
  });

  it("没绑服务商 / 没存模型 ⇒ 不算缺失（是空态不是异常）", () => {
    expect(isModelMissing("", "a", candidates, "text")).toBe(false);
    expect(isModelMissing("P", "", candidates, "text")).toBe(false);
  });

  it("绑的服务商不在候选里（已删/跨用户）⇒ 不判缺失（交由状态列表达）", () => {
    expect(isModelMissing("GONE", "a", candidates, "text")).toBe(false);
  });
});
