// ── 「业务点 → 服务商 / 模型」下拉候选的纯逻辑（2026-10-02） ──
//
// 抽成独立模块的理由与 `llm-gateway/gateway-tree.ts` 相同：**只有纯函数才能被单测钉住**。
// 这里的规则此前只存在于 JSX 的 `render` 里，没有任何自动化覆盖 —— 而它恰好承载了
// 一条用户口径（v2.3「服务商与具体模型两者都必选」），规则错了会直接把用户引进死胡同。
//
// 用户曾报「还是报错」：把目录为空的服务商放进候选 ⇒ 用户在 ② 步永远选不出模型 ⇒
// 卡在「缺具体模型」且**无法自救**。本模块把「空目录不可选」这条规则显式化、可测。

/** 服务商候选下拉所需的最小字段（避免依赖整个 `LlmGatewayConfig`）。 */
export type ProviderCandidate = {
  provider_key: string;
  name: string;
  enabled: boolean;
  /**
   * ⚠️ **v2.7/v2.8**：这里是服务商的**已配置模型行**（结构数组），
   * 不再是上游候选池 —— 候选池搬到了 `catalog: string[]`。
   * 「无模型目录」判据改看它（服务商下没有可用模型行 = ② 步选不出模型）。
   *
   * ⚠️ **v2.9**：`platform` 由单值改为多值。
   * ⚠️ **v2.28**：再改为**引用计数** `Record<string, number>`（`{"xhs": 2}`）。
   * 本模块只用 `model_name` / `enabled` 判「有无可用模型」，平台**不参与判定** ——
   * 但类型要与后端出参（`LlmGatewayModelRow`）对齐，否则传进来就类型不兼容。
   */
  models:
    | {
        model_name: string;
        model_type?: string;
        platform?: Record<string, number> | null;
        enabled?: boolean;
      }[]
    | null
    | undefined;
};

/** 模型行（本模块用到的最小字段，也是级联候选的叶子）。 */
export type ModelCandidate = {
  model_name: string;
  model_type?: string;
  enabled?: boolean;
};

/**
 * 业务点能力需求 `kind` 是否被模型声明的 `model_type` 满足。
 *
 * ⚠️ **必须与后端 `services/llm_gateway_client._type_satisfies` 同判**（§13.2.11.4，v2.9）。
 * 两处不同判会出现最难查的形态：**前端筛出来的候选一选就被后端 400 拒**（或反之，
 * 后端放行的模型前端压根不显示）。
 *
 * | 业务点 kind | 可被哪些 model_type 满足 |
 * |---|---|
 * | `text` | `text` ✅ · `multimodal` ✅ · `image` ❌ · `video` ❌ |
 * | `image` | `image` ✅ · `multimodal` ✅ · `text` ❌ · `video` ❌ |
 * | `video` | `video` ✅ |
 *
 * ⚠️ **v2.11 修**：本函数此前是内联的 `(m.model_type || "text") === kind` **严格相等**，
 * 会把 `multimodal` 模型从 text/image 业务点的候选里**静默滤掉** —— 而后端是放行的
 * （多态模型打 `/chat/completions` 或 `/images/generations` 都可用）⇒ 用户看得见的
 * 候选比可用的少，且没有任何提示。放宽后与后端一致。
 *
 * ⚠️ **`image` 不认 `text`**：文本模型打 `/images/generations` 必失败，方向性不兼容必须拦住。
 * ⚠️ 空类型 / 未知类型 ⇒ 不满足，**不猜**（DEF-057）。
 */
export function typeSatisfies(kind: string, modelType: string | undefined | null): boolean {
  const t = modelType || "";
  if (!t) return false;
  if (t === kind) return true;
  // 多态模型同时可服务 text 与 image 两类业务点
  if (t === "multimodal" && (kind === "text" || kind === "image")) return true;
  return false;
}

/**
 * 某业务点 `kind` 下**可用于级联叶子**的模型行。
 *
 * 过滤两件事（缺一不可）：
 * - **能力匹配** ⇒ `typeSatisfies`（与后端同判，放宽 multimodal）
 * - **可用** ⇒ `enabled !== false` 且 `model_name` 非空（停用/占位行选下去调用必失败）
 *
 * 结果**保序**（沿用后端 `_load_models` 的 `id asc` 顺序 = 用户配置顺序）。
 */
export function usableModels(
  models: ProviderCandidate["models"],
  kind: string,
): ModelCandidate[] {
  return (models ?? []).filter(
    (m): m is ModelCandidate =>
      !!m?.model_name && m?.enabled !== false && typeSatisfies(kind, m?.model_type),
  );
}

/**
 * 该服务商在某业务点 `kind` 下**是否没有任何可用模型**。
 *
 * ⚠️ v2.7/v2.8：判据只看**启用的**模型行 —— 全被停用的服务商与无模型等价
 * （② 步同样选不出可用模型），必须一起归入「无目录」并置灰，否则又是死胡同。
 * ⚠️ v2.11：类型判据改走 `typeSatisfies`（不再严格相等），与后端同判。
 */
export function hasNoCatalog(provider: ProviderCandidate, kind: string): boolean {
  return usableModels(provider.models, kind).length === 0;
}

/**
 * 一条下拉选项（`label` 已带状态后缀，`disabled` 已按规则算好）。
 *
 * `disabledReason` 让调用方能区分「为什么不可选」—— 两种原因的**补救动作完全不同**：
 * - `disabled`（已停用）：用户先去服务商页启用
 * - `no_catalog`（无模型目录）：用户先去服务商页拉目录
 *
 * 更关键的一点：**当前行已经绑着一个 `no_catalog` / 停用服务商时，用户必须能把它清掉**
 * （否则那一行永远停在报错态，且任何合法值都选不了 ⇒ 又回到「报错但不可自救」）。
 * 所以置灰只对「新选」生效，见 `isSelectableForRow`。
 */
export type ProviderOption = {
  value: string;
  label: string;
  disabled: boolean;
  disabledReason: "disabled" | "no_catalog" | null;
};

/**
 * 服务商候选 → 下拉选项。
 *
 * 两种「不可选」各自标注真实原因，**不混为一谈**：
 * - 已停用 ⇒ `（已停用）`
 * - 无模型目录 ⇒ `（无模型目录）`
 *
 * 二者都 `disabled`：停用无法调用、空目录选完必然卡在 ② 步。**仍在列表可见**
 * （不隐藏）—— 用户看得见「它存在但还差什么」，才知道去补目录（同「缺值不造假」）。
 *
 * @param providers 候选服务商
 * @param kind      该行业务点的能力需求（决定「有无可用模型」的判据，v2.11 新增）
 */
export function buildProviderOptions(
  providers: ProviderCandidate[],
  kind: string,
): ProviderOption[] {
  return providers.map((p) => {
    const noCatalog = hasNoCatalog(p, kind);
    return {
      value: p.provider_key,
      label: !p.enabled
        ? `${p.name}（已停用）`
        : noCatalog
          ? `${p.name}（无模型目录）`
          : p.name,
      disabled: !p.enabled || noCatalog,
      disabledReason: !p.enabled ? "disabled" : noCatalog ? "no_catalog" : null,
    };
  });
}

/**
 * 某服务商在某业务点下**可用于级联叶子的模型选项**（label = 模型名，值 = 模型名）。
 *
 * 级联的第二级（叶子）用它；`kind` 决定能力过滤（v2.11 起走 `typeSatisfies`，
 * 与后端同判，`multimodal` 会出现在 text / image 两类业务点下）。
 */
export function buildModelOptions(
  provider: ProviderCandidate | undefined,
  kind: string,
): { value: string; label: string }[] {
  return usableModels(provider?.models, kind).map((m) => ({
    value: m.model_name,
    label: m.model_name,
  }));
}

/**
 * 某个下拉选项**对该行**是否可选。
 *
 * 默认照 `option.disabled` 走；唯一例外：**该行当前正绑着这个选项**。
 * 此时若仍置灰，用户会卡死在报错态 —— 合法值全灰、连「取消选择」都点不了。
 * 于是已绑定的那一项永远可选（选中它 = 没有变化；`allowClear` 清空则走 `onSwitch("")`，
 * 是合法请求，后端 `provider_key=""` 会连带清空 `model_name`）。
 *
 * @param option        候选选项（来自 `buildProviderOptions`）
 * @param currentValue  该行当前已绑定的 `provider_key`（空串 = 未绑定）
 */
export function isSelectableForRow(
  option: ProviderOption,
  currentValue: string,
): boolean {
  if (!option.disabled) return true;
  return !!currentValue && option.value === currentValue;
}

/**
 * 该类型的全部服务商**都没有可用模型**（v2.11：按 `kind` 判，不再只看有无模型行）。
 *
 * 用于把 ① 步下拉整体置灰并给一行说明：此时不是「某个服务商有问题」，
 * 而是**该类型一个能用的都没有** ⇒ 应引导用户去服务商页拉目录，
 * 而不是让他逐个点开、逐个失望。
 */
export function isAllCatalogEmpty(providers: ProviderCandidate[], kind: string): boolean {
  return providers.length > 0 && providers.every((p) => hasNoCatalog(p, kind));
}

/**
 * 某服务商在当前业务点下**是否缺失所选模型**（用于回显校验）。
 *
 * 场景：库里存着 `model_name=X`，但 X 已被停用 / 改了类型 / 不在该服务商下 ⇒
 * 级联的叶子会显示不出来。此时**不能静默清空**该值（用户没动它），
 * 而应让页面据此给出「该模型已不可用」的安静说明 —— 由本函数判定。
 *
 * @param providerKey 生效服务商
 * @param modelName   该行当前所选模型
 * @param selected    该行的候选服务商（`kind` 过滤后的那批）
 */
export function isModelMissing(
  providerKey: string,
  modelName: string,
  selected: ProviderCandidate[],
  kind: string,
): boolean {
  if (!providerKey || !modelName) return false;
  const p = selected.find((x) => x.provider_key === providerKey);
  if (!p) return false;
  return !usableModels(p.models, kind).some((m) => m.model_name === modelName);
}

/**
 * ① 步（选服务商）是否应**整体置灰**。
 *
 * 「该类型全都没目录」只在**本行还没绑服务商**时才该把 ① 步锁死 —— 此时确实无路可走，
 * 给出「去拉目录」的引导即可。
 *
 * ⚠️ 本行**已经绑着**一个服务商时，纵然该类型「全都没目录」，① 步也必须保持可用：
 * 否则用户被锁死在报错态（合法值全灰、② 步必然空、连清空都点不了）—— 这正是
 * 用户二次反馈「还是选不了模型」的成因。放行后，已绑定项由 `isSelectableForRow`
 * 可选，用户得以清空或改选，从而自救。
 */
export function isStepOneLocked(
  allCatalogEmpty: boolean,
  currentValue: string,
): boolean {
  return allCatalogEmpty && !currentValue;
}

/**
 * 两段式写入的**提交时机**判定（用户报「选了服务商就报错、模型永远选不了」的根因守卫）。
 *
 * 后端契约：`provider_key` 与 `model_name` **两者都非空**才接受写入，否则 400
 * `LLM_BUSINESS_MODEL_REQUIRED`。而 ① 步（选服务商）天然只决定服务商、还没选模型 ——
 * **此刻绝不能落库**，否则请求必被拒 ⇒ 库内 `provider_key` 仍为空 ⇒ ② 步因
 * `!provider_key` 恒 `disabled` ⇒ 模型下拉永远打不开 ⇒ 死循环。
 *
 * 故：
 * - ① 步选中非空服务商 ⇒ `hold`（只存本地暂存，等 ② 步）
 * - ① 步清空 ⇒ `commit`（`provider_key=""` 是合法请求，后端会连带清空模型）
 * - ② 步选中模型 ⇒ `commit`（服务商 + 模型齐全）
 *
 * @param providerKey 生效服务商（暂存优先，否则库内值）
 * @param modelName   选中的模型（② 步；① 步场景传空串）
 */
export function commitDecision(
  providerKey: string,
  modelName: string,
): "commit" | "hold" {
  // 服务商非空但模型为空 = 中间态，必须暂存而不是落库（这就是那个死循环的根因）
  if (providerKey && !modelName) return "hold";
  return "commit";
}
