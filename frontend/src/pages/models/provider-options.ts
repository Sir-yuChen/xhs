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
  models: string[] | null | undefined;
};

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
 * 该服务商是否**没有模型目录**。
 *
 * `null` / `undefined` / 空数组一律视为空目录 —— 三者对用户是同一件事
 * （② 步选不出模型），分开处理只会漏掉一种形态。
 */
export function hasNoCatalog(provider: ProviderCandidate): boolean {
  return (provider.models ?? []).length === 0;
}

/**
 * 服务商候选 → 下拉选项。
 *
 * 两种「不可选」各自标注真实原因，**不混为一谈**：
 * - 已停用 ⇒ `（已停用）`
 * - 无模型目录 ⇒ `（无模型目录）`
 *
 * 二者都 `disabled`：停用无法调用、空目录选完必然卡在 ② 步。**仍在列表可见**
 * （不隐藏）—— 用户看得见「它存在但还差什么」，才知道去补目录（同「缺值不造假」）。
 */
export function buildProviderOptions(
  providers: ProviderCandidate[],
): ProviderOption[] {
  return providers.map((p) => {
    const noCatalog = hasNoCatalog(p);
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
 * 该类型的全部服务商**都没有模型目录**。
 *
 * 用于把 ① 步下拉整体置灰并给一行说明：此时不是「某个服务商有问题」，
 * 而是**该类型一个能用的都没有** ⇒ 应引导用户去服务商页拉目录，
 * 而不是让他逐个点开、逐个失望。
 */
export function isAllCatalogEmpty(providers: ProviderCandidate[]): boolean {
  return providers.length > 0 && providers.every(hasNoCatalog);
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
