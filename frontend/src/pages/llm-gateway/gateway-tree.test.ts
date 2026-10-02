import { describe, expect, it } from "vitest";

import { buildGatewayTree, isExpandableProvider, type GatewayTreeRow, type GatewayTreeSource } from "./gateway-tree";

/** 造一条最小服务商（只填树构建用得到的字段）。 */
function gw(id: number, name: string, models: string[] | null) {
  return { id, name, models };
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
  it("父节点 = 服务商，子节点 = 该服务商目录内的每个模型", () => {
    const tree = buildGatewayTree([
      gw(1, "DeepSeek 官方", ["deepseek-v4.1-flash", "deepseek-v4.1-pro"]),
      gw(2, "智谱", ["glm-4.6"]),
    ]);

    expect(tree).toHaveLength(2);

    // 父节点承载服务商本体
    const p0 = asProvider(tree[0]);
    expect(p0.gateway.name).toBe("DeepSeek 官方");
    expect(p0.children).toHaveLength(2);

    // 子节点只承载模型名 + 回指父级的 id/名称
    expect(p0.children[0]).toMatchObject({
      kind: "model",
      model: "deepseek-v4.1-flash",
      parentId: 1,
      parentName: "DeepSeek 官方",
    });
    expect(asProvider(tree[1]).children.map((c) => asModel(c).model)).toEqual(["glm-4.6"]);
  });

  it("key 全局唯一 —— 不同服务商的同名模型不得撞 key", () => {
    // 这是父子树最容易踩的坑：两个服务商都提供 `gpt-4o`，
    // 若子 key 只用模型名（不带父 id），antd Table 的 rowKey 会重复 ⇒
    // React 报 duplicate key、展开/选中错行。key 必须带父 id 前缀。
    const tree = buildGatewayTree([
      gw(7, "中转站 A", ["gpt-4o", "claude-4"]),
      gw(9, "中转站 B", ["gpt-4o"]),
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
    // 服务商可能尚未拉取模型目录（没配令牌 / 没点过测试）⇒ `models` 为空。
    // 后端此时下发 `null` 或缺字段，前端必须能承受（缺值不造假 ⇒ 空目录不是错误）。
    const tree = buildGatewayTree([gw(4, "未探测", null), { id: 5, name: "缺字段" }]);
    expect(asProvider(tree[0]).children).toEqual([]);
    expect(asProvider(tree[1]).children).toEqual([]);
  });

  it("空列表 ⇒ 空树（由表格 locale.emptyText 呈现空态）", () => {
    expect(buildGatewayTree([])).toEqual([]);
  });

  it("顺序保真：父按入参顺序、子按 models 目录顺序（不做二次排序）", () => {
    const tree = buildGatewayTree([
      gw(1, "B", ["z-model", "a-model"]),
      gw(2, "A", ["m"]),
    ]);
    expect(tree.map((p) => asProvider(p).gateway.name)).toEqual(["B", "A"]);
    expect(asProvider(tree[0]).children.map((c) => asModel(c).model)).toEqual(["z-model", "a-model"]);
  });
});

describe("isExpandableProvider —— 展开箭头只给含模型的父行", () => {
  it("含模型的父行可展开", () => {
    const [p] = buildGatewayTree([gw(1, "有模型", ["m1"])]);
    expect(isExpandableProvider(p)).toBe(true);
  });

  it("模型行本身不可展开（模型下没有第三层）", () => {
    const [p] = buildGatewayTree([gw(1, "有模型", ["m1"])]);
    expect(isExpandableProvider(asProvider(p).children[0])).toBe(false);
  });

  it("空目录的父行不可展开（避免「点了没反应」的空箭头）", () => {
    const [p] = buildGatewayTree([gw(1, "空目录", [])]);
    expect(isExpandableProvider(p)).toBe(false);
  });
});
