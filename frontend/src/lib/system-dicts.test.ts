import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  dictLabel,
  dictListClass,
  dictGroup,
  dictsLoaded,
  ensureDicts,
  mergeDictOptions,
  refreshDicts,
  resetDictStoreForTest,
  setDictFetcher,
  type DictOption,
} from "./system-dicts-core";

/**
 * 统一字典层的**纯逻辑**测试（设计 63 §2.2）。
 *
 * 不发真实请求：fetcher 由 `setDictFetcher` 注入假实现 —— 这同时复现了
 * 行为脚本单文件打包 core 时的「无 fetcher」路径（一切走回退常量）。
 */

const TASK_STATUS_DICT: DictOption[] = [
  { value: "pending", label: "字典待处理", list_class: "default", sort_order: 10 },
  { value: "running", label: "字典进行中", list_class: "processing", sort_order: 20 },
];

beforeEach(() => {
  resetDictStoreForTest();
});

afterEach(() => {
  // 恢复「无 fetcher」干净态，避免用例间串味（core 是模块级单例 store）
  resetDictStoreForTest();
});

describe("无 fetcher（行为脚本/未接线环境）⇒ 一切走回退", () => {
  it("dictLabel 用回退常量，表外值原样返回", () => {
    expect(dictLabel("sys_task_status", "pending", { pending: "待处理" })).toBe("待处理");
    expect(dictLabel("sys_task_status", "new_future_value")).toBe("new_future_value");
  });

  it("dictLabel 空值原样返回（缺值口径：空串不造假；null 归一为空串）", () => {
    expect(dictLabel("sys_task_status", "", { pending: "待处理" })).toBe("");
    expect(dictLabel("sys_task_status", null)).toBe("");
    expect(dictLabel("sys_task_status", undefined)).toBe("");
  });

  it("dictListClass 无回退 ⇒ default", () => {
    expect(dictListClass("sys_task_status", "pending")).toBe("default");
    expect(dictListClass("sys_task_status", "running", { running: "processing" })).toBe(
      "processing"
    );
    expect(dictListClass("sys_task_status", null, { running: "processing" })).toBe("default");
  });

  it("ensureDicts 无 fetcher 时不炸、不置 loaded", () => {
    expect(() => ensureDicts()).not.toThrow();
    expect(dictsLoaded()).toBe(false);
  });
});

describe("有 fetcher ⇒ 字典赢", () => {
  it("refreshDicts 后 dictLabel/dictListClass 以字典为准", async () => {
    setDictFetcher(async () => ({ items: { sys_task_status: TASK_STATUS_DICT } }));
    await refreshDicts();
    expect(dictsLoaded()).toBe(true);
    expect(dictLabel("sys_task_status", "pending", { pending: "待处理" })).toBe("字典待处理");
    expect(dictListClass("sys_task_status", "running", { running: "processing" })).toBe(
      "processing"
    );
    // 表外值仍原样返回（不造假）
    expect(dictLabel("sys_task_status", "whatever", { pending: "待处理" })).toBe("whatever");
    // 未加载的组仍走回退
    expect(dictLabel("sys_job_status", "active", { active: "运行中" })).toBe("运行中");
  });

  it("refreshDicts 抛错 ⇒ 静默回退、不置 loaded（下次重试）", async () => {
    setDictFetcher(async () => {
      throw new Error("network down");
    });
    await expect(refreshDicts()).rejects.toThrow("network down");
    ensureDicts(); // 不炸
    expect(dictLabel("sys_task_status", "pending", { pending: "待处理" })).toBe("待处理");
  });

  it("ensureDicts 并发只发一次（幂等）", async () => {
    let calls = 0;
    setDictFetcher(async () => {
      calls += 1;
      return { items: {} };
    });
    ensureDicts();
    ensureDicts();
    ensureDicts();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toBe(1);
  });
});

describe("mergeDictOptions（下拉选项合并）", () => {
  const fallback = [
    { value: "pending", label: "待处理" },
    { value: "all", label: "全部" },
  ];

  it("字典未加载 ⇒ 纯回退（list_class 缺省 default）", () => {
    expect(mergeDictOptions(null, fallback)).toEqual([
      { value: "pending", label: "待处理", list_class: "default", sort_order: 0 },
      { value: "all", label: "全部", list_class: "default", sort_order: 0 },
    ]);
  });

  it("字典赢 + 回退独有的 value 补位（「全部」档不丢）", () => {
    const merged = mergeDictOptions(TASK_STATUS_DICT, fallback);
    expect(merged.map((item) => item.value)).toEqual(["pending", "running", "all"]);
    expect(merged[0].label).toBe("字典待处理");
    expect(merged[2].label).toBe("全部");
  });

  it("空组 + 空回退 ⇒ 空数组（不造假）", () => {
    expect(mergeDictOptions([], [])).toEqual([]);
  });
});

describe("dictGroup 读取", () => {
  it("未加载的组 ⇒ null（调用方走回退）", () => {
    expect(dictGroup("no_such_group")).toBeNull();
  });

  it("加载后按 code 取组", async () => {
    setDictFetcher(async () => ({ items: { sys_task_status: TASK_STATUS_DICT } }));
    await refreshDicts();
    expect(dictGroup("sys_task_status")).toEqual(TASK_STATUS_DICT);
  });
});
