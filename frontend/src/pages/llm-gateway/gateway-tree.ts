// 大模型网关 · 服务商列表 → 父子树
//
// 领域模型（2026-10-02 用户口径）：大模型网关是一个**模块**，里面对接不同**服务商**，
// 每个服务商下有多个**具体大模型**。因此服务商列表用**父子树**呈现：
//   父节点 = 服务商；子节点 = 该服务商 `models` 数组里的每条**模型行**。
//
// 形态选型：用 antd `Table` 的**可展开子行**（每行挂 `children`）而不是 antd `Tree` —— 原因有三：
//   ① 服务商行的列信息（服务编码 / 地址 / 调用量 / 令牌 / 健康 / 启停开关 / 测试·编辑操作）
//      **必须保留**，`Tree` 只有「标题 + 图标」一个展示位，塞不进这么多字段；
//      表格的列布局直接复用，父子行共享同一套列定义。
//   ② 父行（服务商）上的操作（测试 / 编辑 / 启停）需要**行内可交互**，表格天然支持。
//   ③ 语义上仍是「一行一条服务商」，只是每条服务商**展开**出它的模型行 —— 与
//      「服务商下有多个具体模型」的领域模型同构。
//
// 子行（模型）承载**模型名 + 类型 + 平台**（v2.7：这三者都是模型的属性）；
// 服务商级字段（服务编码 / 地址 / 令牌 / 健康 / 启停）不属于模型 ⇒ 子行留 `-` 占位，
// **不复制父行值到子行**：复制会让「这一格到底描述谁」变得含混（同「缺值不造假」纪律）。
//
// ⚠️ **v2.7/v2.8**：`models` 从「字符串数组（目录候选）」升级为**结构数组（模型行）**；
// 原目录候选池改名为 `catalog`。类型与平台不再是服务商级字段。
// 见设计 17 §13.2.9 / §13.2.10。
//
// ⚠️ **v2.9**：模型类型扩四值（text / image / multimodal / video）；
// **`platform` 由单值改为多值数组**（一个模型可被多个平台共用）；
// 模型行带 `id` —— 「类型」列改为**就地可编辑下拉**要按 id 调 PATCH 落库。
// 见设计 17 §13.2.11。

/** 模型行的最小形状（只取树构建所需字段，便于单测构造假数据）。 */
export interface GatewayModelRow {
  /** 模型行主键 —— 「类型」列就地改类型时按它调 `PATCH /{config_id}/models/{id}`。 */
  id?: number;
  model_name: string;
  model_type?: string;
  /**
   * v2.28：**引用计数**（`{"xhs": 2}` = xhs 下有 2 个业务点在用这个模型）。
   * 取代 v2.9 的 `string[]`；后端 `_parse_platforms` 宽入（旧数组 ⇒ `{"xhs": 1}`）。
   */
  platform?: Record<string, number> | null;
  /** v2.28：`platform` 的键列表（后端同步下发，渲染直接用）。 */
  platforms?: string[] | null;
  enabled?: boolean;
  /** 备注（后端字段早已存在；子表「备注」列展示，缺值不造假 ⇒ 空串）。 */
  remark?: string;
  /** v2.10：模型级健康（探测过且通 = true）；从未探测时后端给 false。 */
  healthy?: boolean;
  /** v2.10：模型级最近探测时间（`null` = 从未探测）。判据见 `lastHealthAt`。 */
  last_health_at?: string | null;
  /**
   * v2.10：**正在使用该模型的业务点**（后端下发；空数组 = 没人用）。
   * 用户口径「已在使用的模型无法停用」⇒ 非空时前端把「启用」开关置灰，
   * 后端 PATCH 也会 400 拦（前端置灰只是体验，判据在后端）。
   */
  usages?: { business_key: string; label: string; kind?: string; platform?: string }[] | null;
}

/** 服务商行的最小形状（只取树构建所需字段，便于单测构造假数据）。 */
export interface GatewayTreeSource {
  id: number;
  name: string;
  models?: GatewayModelRow[] | null;
}

/** 树节点：父 = 服务商（`kind: "provider"`），子 = 具体模型（`kind: "model"`）。 */
export type GatewayTreeRow<S extends GatewayTreeSource = GatewayTreeSource> =
  | { kind: "provider"; key: string; gateway: S; children: GatewayTreeRow<S>[] }
  | {
      kind: "model";
      key: string;
      /** 模型行主键（就地改类型要按它发 PATCH）；后端未给 id 时为 `null`。 */
      modelId: number | null;
      model: string;
      modelType: string;
      /** v2.28：平台标识列表（已剥空值；**计数为 0 的已由后端丢弃**）。 */
      platforms: string[];
      /**
       * v2.28：平台 → **使用次数**（`{"xhs": 2}` = xhs 下 2 个业务点在用）。
       * 渲染「xhs × 2」用；缺值 = `{}`（不造假）。
       */
      platformCounts: Record<string, number>;
      enabled: boolean;
      /** 备注（空串 = 无；子表按缺值口径渲染 `-`）。 */
      remark: string;
      /**
       * v2.10：模型级健康（探测过且通 = true）。
       * ⚠️ **不能只看它** —— 判「正常」必须同时满足 `lastHealthAt !== null`；
       * 从未探测时 `healthy` 也是 false，单看会误报成「异常」。
       */
      healthy: boolean;
      /** v2.10：模型级最近探测时间；`null` = **从未探测**（判据的另一半）。 */
      lastHealthAt: string | null;
      /**
       * v2.10：使用该模型的**业务点中文名列表**（空数组 = 无业务点在用）。
       * ⚠️ 只取 `label` 供交互提示用 —— 「已在使用的模型无法停用」的置灰判据
       * 就是 `usages.length > 0`；完整结构（key/kind/platform）留给需要时再取。
       */
      usages: string[];
      parentId: number;
      parentName: string;
    };

/** 服务商列表 → 父子树节点（父 = 服务商；子 = `models` 里的每条模型行）。 */
export function buildGatewayTree<S extends GatewayTreeSource>(gateways: S[]): GatewayTreeRow<S>[] {
  return gateways.map((g) => ({
    kind: "provider" as const,
    key: `gw-${g.id}`,
    gateway: g,
    children: (g.models ?? []).map((m) => ({
      kind: "model" as const,
      // ⚠️ key 里带上行 id：模型可被**改名**（同步时上游目录变了），只用 model_name 做 key
      // 会让 React 在改名后复用错行（类型下拉的本地态串行）。id 缺失时回落模型名。
      key: `gw-${g.id}-model-${m.id ?? m.model_name}`,
      modelId: m.id ?? null,
      model: m.model_name,
      modelType: m.model_type ?? "",
      // v2.28 宽入（§13.2.9.7c）：`platform` 是**引用计数** `{"xhs": 2}`；旧形态
      // 是数组 `["xhs"]` ⇒ 一律归一到「只保留计数 > 0 的键」。
      // ⚠️ **不能只 `.filter(Boolean)`**：对象形态下那会把整个计数对象当成一个真值元素
      // （渲染出一个 `[object Object]` 标签）—— 类型变更后最典型的静默退化。
      ...(() => {
        const raw: unknown = m.platform;
        const counts: Record<string, number> = {};
        if (Array.isArray(raw)) {
          for (const item of raw) if (item) counts[String(item)] = 1;
        } else if (raw && typeof raw === "object") {
          for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
            const n = Number(value);
            if (key && Number.isFinite(n) && n > 0) counts[key] = n;
          }
        }
        return { platforms: Object.keys(counts), platformCounts: counts };
      })(),
      enabled: m.enabled ?? true,
      remark: m.remark ?? "",
      // v2.10：模型级健康两字段**原样带回**（缺值不造假：未探测就是 false + null，
      // 前端据此渲染「未探测」而不是「异常」）。
      healthy: m.healthy ?? false,
      lastHealthAt: m.last_health_at ?? null,
      // v2.10：正在使用该模型的业务点中文名（空数组 = 没人用 ⇒ 可自由停用）。
      // 缺字段按空数组（缺值不造假：没说「有人用」就是不拦，与后端「无映射即放行」同向）。
      usages: (m.usages ?? []).map((u) => u.label || u.business_key),
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

/**
 * 父行（服务商）与其子表（模型）的**列数**关系 —— 布局用。
 *
 * 父子各用一套列，宽度天然对不齐；父表用 `tableLayout: "fixed"` 固定分配列宽，
 * 子表放在展开区里会**自己排布**（不继承父表列宽）。这里只暴露「子表数据行是否为空」，
 * 供 UI 决定是否需要为展开区留白（空目录的父行不展开，故一般用不到）。
 */
export function modelRowCount<S extends GatewayTreeSource>(row: GatewayTreeRow<S>): number {
  return row.kind === "provider" ? row.children.length : 0;
}

/** 模型子节点（`kind: "model"` 的分支类型）—— 子表列定义按它书写。 */
export type GatewayModelNode<S extends GatewayTreeSource = GatewayTreeSource> = Extract<
  GatewayTreeRow<S>,
  { kind: "model" }
>;

/** 服务商父节点（`kind: "provider"` 的分支类型）—— 父表列定义按它书写。 */
export type GatewayProviderNode<S extends GatewayTreeSource = GatewayTreeSource> = Extract<
  GatewayTreeRow<S>,
  { kind: "provider" }
>;
