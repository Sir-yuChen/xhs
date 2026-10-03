import { describe, expect, it } from "vitest";

import {
  buildGatewayTree,
  isExpandableProvider,
  modelRowCount,
  type GatewayModelRow,
  type GatewayTreeRow,
  type GatewayTreeSource,
} from "./gateway-tree";

/** 造一条最小服务商（只填树构建用得到的字段）。 */
function gw(id: number, name: string, models: GatewayModelRow[] | null) {
  return { id, name, models };
}

/** 造一条模型行（v2.7：类型 / 平台是模型的属性；v2.28：platform 是**引用计数**）。 */
function m(
  name: string,
  modelType = "text",
  platform: Record<string, number> = { xhs: 1 },
  id?: number,
): GatewayModelRow {
  return { id, model_name: name, model_type: modelType, platform, enabled: true };
}

/** 断言并收窄成父节点（`kind === "provider"`），返回带完整字段类型的节点。 */
function asProvider<S extends GatewayTreeSource>(row: GatewayTreeRow<S>) {
  if (row.kind !== "provider") throw new Error(`期望父节点，实得 ${row.kind}`);
  return row;
}

/** 断言并收窄成子节点（`kind === "model"`）。 */
function asModel<S extends GatewayTreeSource>(row: GatewayTreeRow<S>) {
  if (row.kind !== "model") throw new Error(`期望模型子节点，实得 ${row.kind}`);
  return row;
}

describe("buildGatewayTree —— 服务商列表父子树", () => {
  it("父节点 = 服务商，子节点 = 该服务商的每条模型行（含类型 / 平台）", () => {
    const tree = buildGatewayTree([
      gw(1, "DeepSeek 官方", [
        m("deepseek-v4.1-flash"),
        m("deepseek-v4.1-pro", "image", { douyin: 1 }),
      ]),
      gw(2, "智谱", [m("glm-4.6")]),
    ]);

    expect(tree).toHaveLength(2);

    // 父节点承载服务商本体
    const p0 = asProvider(tree[0]);
    expect(p0.gateway.name).toBe("DeepSeek 官方");
    expect(p0.children).toHaveLength(2);

    // 子节点承载模型名 + 类型 + 平台 + 回指父级的 id/名称
    expect(p0.children[0]).toMatchObject({
      kind: "model",
      model: "deepseek-v4.1-flash",
      modelType: "text",
      platforms: ["xhs"],
      enabled: true,
      parentId: 1,
      parentName: "DeepSeek 官方",
    });
    expect(asModel(p0.children[1])).toMatchObject({
      model: "deepseek-v4.1-pro",
      modelType: "image",
      platforms: ["douyin"],
    });
    expect(asProvider(tree[1]).children.map((c) => asModel(c).model)).toEqual(["glm-4.6"]);
  });

  it("v2.9：platform 多值保真（一个模型被多个平台共用）", () => {
    const [p] = buildGatewayTree([gw(1, "共用行", [m("gpt-4o", "multimodal", { xhs: 1, douyin: 1, kuaishou: 1 })])]);
    const child = asModel(asProvider(p).children[0]);
    expect(child.platforms).toEqual(["xhs", "douyin", "kuaishou"]);
  });

  it("v2.9：platform 数组里的空值被剥掉（缺值不造假 ⇒ 不留空标签）", () => {
    const [p] = buildGatewayTree([
      gw(1, "脏数据", [{ model_name: "m", platform: { xhs: 1, "": 1, douyin: 1 } }]),
    ]);
    expect(asModel(asProvider(p).children[0]).platforms).toEqual(["xhs", "douyin"]);
  });

  it("模型行缺 type/platform 字段 ⇒ 空串 / 空数组（缺值不造假，不猜类型）", () => {
    const [p] = buildGatewayTree([gw(1, "手写行", [{ model_name: "m" }])]);
    const child = asModel(asProvider(p).children[0]);
    expect(child.modelType).toBe("");
    expect(child.platforms).toEqual([]);
    expect(child.enabled).toBe(true); // 缺省视为启用
    expect(child.remark).toBe(""); // v2.9：备注缺省空串（子表按缺值口径渲染 -）
  });

  it("v2.10：模型行缺健康字段 ⇒ false + null（缺值不造假，不猜成「正常」）", () => {
    const [p] = buildGatewayTree([gw(1, "未探测", [{ model_name: "m" }])]);
    const child = asModel(asProvider(p).children[0]);
    // ⚠️ 判据是 `lastHealthAt !== null && healthy`，两半缺一即「未探测」——
    // 缺省必须给 false + null，否则会把从未探测的行误渲染成「异常」。
    expect(child.healthy).toBe(false);
    expect(child.lastHealthAt).toBeNull();
  });

  it("v2.10：模型行带健康字段 ⇒ 原样带进节点（子表健康列展示）", () => {
    const [p] = buildGatewayTree([
      gw(1, "探测过", [
        { model_name: "m", healthy: true, last_health_at: "2026-10-02T20:00:00" },
      ]),
    ]);
    const child = asModel(asProvider(p).children[0]);
    expect(child.healthy).toBe(true);
    expect(child.lastHealthAt).toBe("2026-10-02T20:00:00");
  });

  it("v2.10：探测过但不通 ⇒ healthy=false 且 lastHealthAt 有值（与「未探测」区分开）", () => {
    const [p] = buildGatewayTree([
      gw(1, "探测失败", [{ model_name: "m", healthy: false, last_health_at: "2026-10-02T20:00:00" }]),
    ]);
    const child = asModel(asProvider(p).children[0]);
    // 这是三态里最容易被写错的一态：`healthy=false` 不等于「未探测」，
    // 要看 `lastHealthAt` 有没有值来分流 ⇒ 有值 = 探测过不通（异常）。
    expect(child.healthy).toBe(false);
    expect(child.lastHealthAt).not.toBeNull();
  });

  it("v2.10：模型被业务点使用时 ⇒ usages 带出中文名（前端据此置灰「启用」开关）", () => {
    const [p] = buildGatewayTree([
      gw(1, "在用", [
        {
          model_name: "m",
          usages: [
            { business_key: "generate_note", label: "生成笔记", kind: "text", platform: "xhs" },
            { business_key: "rewrite_note", label: "改写笔记", kind: "text", platform: "xhs" },
          ],
        },
      ]),
    ]);
    const child = asModel(asProvider(p).children[0]);
    // ⚠️ 置灰判据 = `usages.length > 0` ⇒ 这里必须**两条都在**（少一条会漏判）
    expect(child.usages).toEqual(["生成笔记", "改写笔记"]);
  });

  it("v2.10：`usages` 缺字段（老后端/无映射）⇒ 空数组（缺值不造假 ⇒ 不拦停用）", () => {
    const [p] = buildGatewayTree([gw(1, "无映射", [{ model_name: "m" }])]);
    // 后端没下发 = 没人用 ⇒ 开关可点。与后端「无映射即放行」同向，
    // 若这里回落成「有值」会让所有老接口的模型都点不动（假死）。
    expect(asModel(asProvider(p).children[0]).usages).toEqual([]);
  });

  it("v2.10：`usages` 里 label 缺失 ⇒ 回落 business_key（不编中文名，但也不能变空串）", () => {
    const [p] = buildGatewayTree([
      gw(1, "无 label", [{ model_name: "m", usages: [{ business_key: "generate_note", label: "" }] }]),
    ]);
    // 回落 key 而不是丢掉这条 —— 丢掉会让置灰判据误判成「没人用」
    expect(asModel(asProvider(p).children[0]).usages).toEqual(["generate_note"]);
  });

  it("v2.9：模型行带 remark ⇒ 原样带进节点（子表备注列展示）", () => {
    const [p] = buildGatewayTree([
      gw(1, "带备注", [{ model_name: "m", remark: "限流 8 并发" }]),
    ]);
    expect(asModel(asProvider(p).children[0]).remark).toBe("限流 8 并发");
  });

  it("v2.9：带 id 的模型行把 id 带进节点（类型下拉就地改类型要用）", () => {
    const [p] = buildGatewayTree([gw(1, "有 id", [m("m", "text", { xhs: 1 }, 42)])]);
    const child = asModel(asProvider(p).children[0]);
    expect(child.modelId).toBe(42);
    // key 也带 id —— 模型改名后 React 不复用错行
    expect(child.key).toBe("gw-1-model-42");
  });

  it("v2.9：无 id 的模型行 key 回落模型名（不造 id）", () => {
    const [p] = buildGatewayTree([gw(1, "无 id", [m("m")])]);
    const child = asModel(asProvider(p).children[0]);
    expect(child.modelId).toBeNull();
    expect(child.key).toBe("gw-1-model-m");
  });

  it("key 全局唯一 —— 不同服务商的同名模型不得撞 key", () => {
    // 这是父子树最容易踩的坑：两个服务商都提供 `gpt-4o`，
    // 若子 key 只用模型名（不带父 id），antd Table 的 rowKey 会重复 ⇒
    // React 报 duplicate key、展开/选中错行。key 必须带父 id 前缀。
    const tree = buildGatewayTree([
      gw(7, "中转站 A", [m("gpt-4o"), m("claude-4")]),
      gw(9, "中转站 B", [m("gpt-4o")]),
    ]);

    const allKeys = tree.flatMap((p) => {
      const parent = asProvider(p);
      return [parent.key, ...parent.children.map((c) => c.key)];
    });
    expect(new Set(allKeys).size).toBe(allKeys.length);

    const a = asModel(asProvider(tree[0]).children[0]);
    const b = asModel(asProvider(tree[1]).children[0]);
    expect(a.key).toBe("gw-7-model-gpt-4o");
    expect(b.key).toBe("gw-9-model-gpt-4o");
    expect(a.key).not.toBe(b.key);
  });

  it("models 为空数组 ⇒ 父节点 children 为空（不产生幽灵子行）", () => {
    const [p] = buildGatewayTree([gw(3, "刚建的", [])]);
    expect(asProvider(p).children).toEqual([]);
    expect(isExpandableProvider(p)).toBe(false);
  });

  it("models 为 null/undefined ⇒ 按空目录处理，不抛异常", () => {
    // 服务商可能尚未配置任何模型行 ⇒ `models` 为空。
    // 后端此时下发 `null` 或缺字段，前端必须能承受（缺值不造假 ⇒ 空不是错误）。
    const tree = buildGatewayTree([gw(4, "未配置", null), { id: 5, name: "缺字段" }]);
    expect(asProvider(tree[0]).children).toEqual([]);
    expect(asProvider(tree[1]).children).toEqual([]);
  });

  it("空列表 ⇒ 空树（由表格 locale.emptyText 呈现空态）", () => {
    expect(buildGatewayTree([])).toEqual([]);
  });

  it("顺序保真：父按入参顺序、子按 models 顺序（不做二次排序）", () => {
    const tree = buildGatewayTree([
      gw(1, "B", [m("z-model"), m("a-model")]),
      gw(2, "A", [m("m")]),
    ]);
    expect(tree.map((p) => asProvider(p).gateway.name)).toEqual(["B", "A"]);
    expect(asProvider(tree[0]).children.map((c) => asModel(c).model)).toEqual(["z-model", "a-model"]);
  });
});

describe("isExpandableProvider —— 展开箭头只给含模型的父行", () => {
  it("含模型的父行可展开", () => {
    const [p] = buildGatewayTree([gw(1, "有模型", [m("m1")])]);
    expect(isExpandableProvider(p)).toBe(true);
  });

  it("模型行本身不可展开（模型下没有第三层）", () => {
    const [p] = buildGatewayTree([gw(1, "有模型", [m("m1")])]);
    expect(isExpandableProvider(asProvider(p).children[0])).toBe(false);
  });

  it("空目录的父行不可展开（避免「点了没反应」的空箭头）", () => {
    const [p] = buildGatewayTree([gw(1, "空目录", [])]);
    expect(isExpandableProvider(p)).toBe(false);
  });
});

describe("modelRowCount —— 子表行数（父行 = 模型数；模型行恒 0）", () => {
  it("父行返回其模型行数", () => {
    const [p] = buildGatewayTree([gw(1, "两个模型", [m("m1"), m("m2")])]);
    expect(modelRowCount(p)).toBe(2);
  });

  it("空目录父行 = 0（与 isExpandableProvider=false 一致：不展开）", () => {
    const [p] = buildGatewayTree([gw(1, "空目录", [])]);
    expect(modelRowCount(p)).toBe(0);
    expect(isExpandableProvider(p)).toBe(false);
  });

  it("模型子行恒 0（模型下没有第三层，不存在「子表的子表」）", () => {
    const [p] = buildGatewayTree([gw(1, "有模型", [m("m1")])]);
    expect(modelRowCount(asProvider(p).children[0])).toBe(0);
  });
});
