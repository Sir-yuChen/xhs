// 大模型网关 · 服务商列表 → 父子树
//
// 领域模型（2026-10-02 用户口径）：大模型网关是一个**模块**，里面对接不同**服务商**，
// 每个服务商下有多个**具体大模型**。因此服务商列表用**父子树**呈现：
//   父节点 = 服务商；子节点 = 该服务商 `models_json` 目录里的每个具体模型。
//
// 形态选型：用 antd `Table` 的**可展开子行**（每行挂 `children`）而不是 antd `Tree` —— 原因有三：
//   ① 服务商行的列信息（服务编码 / 使用平台 / 类型 / 地址 / 默认模型 / 调用量 / 令牌 /
//      健康 / 启停开关 / 测试·编辑操作）**必须保留**，`Tree` 只有「标题 + 图标」一个展示位，
//      塞不进 10 个字段；表格的列布局直接复用，父子行共享同一套列定义。
//   ② 父行（服务商）上的操作（测试 / 编辑 / 启停）需要**行内可交互**，表格天然支持。
//   ③ 语义上仍是「一行一条服务商」，只是每条服务商**展开**出它的模型目录 —— 与
//      「服务商下有多个具体模型」的领域模型同构。
//
// 子行（模型）**只承载模型名**，其余列留空（用 `-` 占位）—— 模型不是独立实体，
// 它没有服务编码 / 平台 / 地址 / 令牌这些属性（那些属于父级服务商），
// **不复制父行值到子行**：复制会让「这一格到底描述谁」变得含混（同「缺值不造假」纪律）。

/** 服务商行的最小形状（只取树构建所需字段，便于单测构造假数据）。 */
export interface GatewayTreeSource {
  id: number;
  name: string;
  models?: string[] | null;
}

/** 树节点：父 = 服务商（`kind: "provider"`），子 = 具体模型（`kind: "model"`）。 */
export type GatewayTreeRow<S extends GatewayTreeSource = GatewayTreeSource> =
  | { kind: "provider"; key: string; gateway: S; children: GatewayTreeRow<S>[] }
  | { kind: "model"; key: string; model: string; parentId: number; parentName: string };

/** 服务商列表 → 父子树节点（父 = 服务商；子 = `models` 里的每个模型名）。 */
export function buildGatewayTree<S extends GatewayTreeSource>(gateways: S[]): GatewayTreeRow<S>[] {
  return gateways.map((g) => ({
    kind: "provider" as const,
    key: `gw-${g.id}`,
    gateway: g,
    children: (g.models ?? []).map((model) => ({
      kind: "model" as const,
      key: `gw-${g.id}-model-${model}`,
      model,
      parentId: g.id,
      parentName: g.name,
    })),
  }));
}

/** antd `Table` 的展开箭头只该出现在「含模型的服务商行」上（空目录不显示箭头）。 */
export function isExpandableProvider<S extends GatewayTreeSource>(
  row: GatewayTreeRow<S>,
): boolean {
  return row.kind === "provider" && row.children.length > 0;
}
