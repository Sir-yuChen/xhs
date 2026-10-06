import { ReloadOutlined } from "@ant-design/icons";
import { App, Button, Card, Cascader, Space, Spin, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  buildModelOptions,
  buildProviderOptions,
  commitDecision,
  isAllCatalogEmpty,
  isModelMissing,
  isSelectableForRow,
  isStepOneLocked,
} from "./provider-options";
import {
  fetchBusinessProviderKeys,
  fetchBusinessProviders,
  fetchGatewayModelsTree,
  upsertBusinessProvider,
} from "../../lib/api";
import type {
  BusinessKeyOption,
  BusinessProviderRow,
  LlmGatewayConfig,
} from "../../types";

const { Text } = Typography;

/**
 * **本页固定的业务平台**（v2.6，设计 17 §13.2.8）。
 *
 * 用户口径（2026-10-02）：「小红书模型配置页面**只能配置小红书相关业务使用的模型**，
 * 把右上角的下拉移除，**不支持切换到其它平台**。」
 *
 * 为什么写死 `xhs` 而不是保留一个「当前平台」态：本项目**只有一个业务平台**，
 * 业务代码的调用点**全部**传 `'xhs'`（`resolve_business_provider(db, user, BUSINESS_KEY_*, "xhs")`）。
 * 页面上原那个 6 平台下拉（`xhs/douyin/...`）**指向 5 个没有任何业务代码会读的映射** ——
 * 用户切到「抖音」配置一通，业务侧**永远读不到**（调用写死 `xhs`）⇒ 典型的
 * 「**能改但不生效**」静默坑。删掉它 = 删**假入口**，不是删后端能力：
 * 后端的 `PLATFORM_ENUM` 与跨平台隔离（唯一键 `(user_id, platform, business_key)`）
 * **完整保留**，将来真接抖音时后端零改动，只需把这里换回真实平台态。
 */
const XHS_PLATFORM = "xhs";

/**
 * 模型配置页（v2.0，设计 17 §13.2.1）。
 *
 * ## 这一页在改什么
 *
 * 一期这一页管的是「本地模型配置表」（`model_configs`）：用户填死一套
 * 文本/图像模型 + 地址 + key，业务代码按「默认模型」取用。**v2.0 已整体退场**
 * —— 服务商（`LlmGatewayConfig`）单独在「服务商配置」页配好，业务改由
 * **固定 key** 映射到服务商（`llm_business_providers`）。
 *
 * 用户口径：「服务商为单独配置，业务中的使用点映射成固定 key，新增一个模型配置页，
 * 不同平台和不同的业务可以**实时切换服务商**」。
 *
 * ## 三条不能丢的纪律
 *
 * ① **业务点清单从后端拉**（`fetchBusinessProviderKeys`）：清单真相源在后端常量
 *    `services/business_provider_keys.py`，前端不许就地写死（写死 ⇒ 新增业务点要改
 *    两处，前端那份会静默过期，DEF-044）。
 * ② **选中即生效**：`onChange` 直接 `PUT`，**没有「保存」按钮** —— 用户要的是实时，
 *    多一个保存按钮就多一个「改了但没点保存」的静默坑。
 * ③ **业务点 key 只读**：用户只能选服务商，不能改 key（key 是代码里的调用契约）。
 *
 * ## 空态
 *
 * 下拉候选**按 kind 过滤**（图像业务点只列 image 类型服务商）。库内服务商若全是
 * 文本类 ⇒ 图像业务点整行为空，这是**正常空态**，页面要给明确文案而不是一个空下拉
 * （否则用户无从判断是「坏了」还是「没配」）。
 */
export function ModelConfigPage() {
  const { message } = App.useApp();
  const [rows, setRows] = useState<BusinessProviderRow[]>([]);
  const [keys, setKeys] = useState<BusinessKeyOption[]>([]);
  const [providers, setProviders] = useState<LlmGatewayConfig[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [savingKey, setSavingKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      // 平台固定 `xhs`（v2.6，§13.2.8）⇒ 不再拉平台枚举，也不再传可变 scope。
      //
      // ⚠️ **v2.11 纠错（2026-10-02 用户报「全部无法修改服务商和模型」的根因）**：
      //   这里原本调 `fetchGatewayConfigs()` —— 那个端点的每项 `models` **恒为空数组**
      //   （后端 `GET /llm/gateway-configs` 走 `_serialize_config(c)` 不传 `models`，
      //   形参默认 `None ⇒ []`）。而下方 `candidatesFor()` 会
      //   `.filter((p) => p.models.length > 0)` ⇒ **所有服务商被过滤掉** ⇒ `options` 恒空
      //   ⇒ `empty = true` ⇒ 两个 `Select` 的 `disabled` 恒真 ⇒ 服务商与模型**都改不了**
      //   （且不报错，是「能看不能改」的静默坑）。
      //   正解 = 换 `fetchGatewayModelsTree()`（`GET /llm/gateway-configs/models-tree`），
      //   它与 `GET /` 的过滤/排序/序列化**同一个函数**，只是每项的 `models` 真的装了模型行。
      //   `usage-dashboard-panel.tsx` 早已踩过同一个坑并留了注释，本页是漏改的一处。
      const [mapping, keyList, providerList] = await Promise.all([
        fetchBusinessProviders(XHS_PLATFORM),
        fetchBusinessProviderKeys(),
        fetchGatewayModelsTree(),
      ]);
      setRows(mapping.items);
      setKeys(keyList.items);
      setProviders(providerList.items);
    } catch {
      // 失败由全局拦截器报（401/400 都带结构化原因）
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * 某业务点的候选服务商 = **`kind` 下有可用模型行**的服务商。
   *
   * ⚠️ **v2.7**：`model_type` 已从服务商下沉到**模型行** —— 读 `p.models` 而不是
   * 服务商级的 `p.model_type`（该字段已不存在）。
   *
   * ⚠️ **v2.11**：判据全交给 `provider-options.ts` 的 `usableModels` / `typeSatisfies`
   * （**与后端 `_type_satisfies` 同判**）。老代码这里内联了 `(m.model_type||"text")===kind`
   * 的**严格相等** ⇒ 把 `multimodal` 模型从 text/image 业务点的候选里静默滤掉了
   * （后端其实放行）⇒ 用户看得见的候选比可用的少。
   *
   * 为什么不用 `catalog`（上游候选池）：那是**未配置**的候选，没有类型信息，
   * 且 v2.7 起调用时后端只认模型行 ⇒ 候选池里的名字选下去会 400
   * `LLM_BUSINESS_MODEL_NOT_CONFIGURED`（§13.2.9.4 分工）。
   *
   * ⚠️ **不再预过滤空目录服务商**：v2.11 起候选池保留**全部**服务商，由
   * `buildProviderOptions` 把「无该类型模型」的置灰并标注原因 —— 「仍可见不隐藏」
   * 才能让用户知道去补目录（同「缺值不造假」）。老代码 `.filter(p => p.models.length>0)`
   * 会把它们**整条删掉**，用户既看不见也修不了。
   */
  const candidatesFor = useCallback(
    (_kind: string) => providers,
    [providers],
  );

  // ⚠️ ① 步（服务商）的**暂存**选择：不立刻落库。
  //
  // 为什么必须暂存（这是用户报「选了服务商就报错、模型永远选不了」的根因）：
  //   后端契约是「服务商 + 模型**两者都必选**」，`provider_key` 非空而 `model_name`
  //   为空 ⇒ 400 `LLM_BUSINESS_MODEL_REQUIRED`。若 ① 步 onChange 立刻 PUT
  //   （带 `model_name=""`），这次写入**必然被拒** ⇒ 该行 `provider_key` 仍是空 ⇒
  //   ② 步 `disabled` 条件 `!record.provider_key` 恒真 ⇒ 模型下拉永远打不开。
  //   结果：用户「选了服务商 → 弹必选模型的错 → 模型却选不了」的死循环。
  //
  // 正解：① 步只改本地态（② 步据此启用并显示该服务商的模型目录），
  //   **② 步选中模型时才一次性 PUT（服务商 + 模型齐全）** —— 单次请求即合法。
  const [pendingProvider, setPendingProvider] = useState<Record<string, string>>({});
  /** 该行「生效的」服务商 = 暂存值（若用户正在选）否则库内值。 */
  const effectiveProviderKey = (record: BusinessProviderRow): string =>
    pendingProvider[record.business_key] ?? record.provider_key;

  const onSwitch = useCallback(
    async (row: BusinessProviderRow, providerKey: string, modelName: string) => {
      setSavingKey(row.business_key);
      try {
        await upsertBusinessProvider({
          platform: row.platform,
          business_key: row.business_key,
          provider_key: providerKey,
          model_name: modelName,
        });
        // 落库成功 ⇒ 清掉该行暂存，改以服务端返回为准
        setPendingProvider((prev) => {
          if (!(row.business_key in prev)) return prev;
          const next = { ...prev };
          delete next[row.business_key];
          return next;
        });
        if (!providerKey) {
          message.success("已清空该业务点的服务商");
        } else {
          message.success("已切换服务商与模型");
        }
        await load();
      } catch {
        // 失败由全局拦截器报（类型不匹配 / 服务商不存在 / 模型不在目录内都带结构化原因）
      } finally {
        setSavingKey(null);
      }
    },
    [load, message],
  );

  const rowsWithKey = useMemo(
    () => rows.map((r) => ({ ...r, _key: `${r.platform}:${r.business_key}` })),
    [rows],
  );

  const columns: ColumnsType<BusinessProviderRow & { _key: string }> = [
    {
      title: "业务点",
      dataIndex: "business_key",
      width: 220,
      render: (_v, record) => {
        // 中文名由后端随行下发（常量 `BusinessKey.label`），页面不再拿 `keys` 反查。
        return (
          <Space direction="vertical" size={0}>
            <Text>{record.label || record.business_key}</Text>
            {/* 业务点 key 只读展示：它是代码里的调用契约，用户改不了 */}
            <Text type="secondary" style={{ fontSize: 12 }}>
              {record.business_key}
            </Text>
          </Space>
        );
      },
    },
    {
      title: "触发源",
      dataIndex: "triggers",
      width: 240,
      render: (_v, record) => {
        // **只读**：触发源是常量元数据（`BusinessKey.triggers`），由后端随行下发，
        // 页面只渲染、不提供编辑入口 —— 它镜像的是代码里的调用点，用户改不了。
        // 空数组 = 只由 API 直接调用、界面无入口：**如实说明**，不编一个入口出来
        // （同「缺值不造假」纪律）。
        const triggers = record.triggers ?? [];
        if (triggers.length === 0) {
          return (
            <Text type="secondary" style={{ fontSize: 12 }}>
              无界面入口（仅 API 调用）
            </Text>
          );
        }
        return (
          <Space direction="vertical" size={2}>
            {triggers.map((t) => (
              <Tag key={t} style={{ margin: 0 }}>
                {t}
              </Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: "类型",
      dataIndex: "kind",
      width: 90,
      render: (value: string) => (
        <Tag color={value === "image" ? "purple" : "blue"}>
          {value === "image" ? "图像" : "文本"}
        </Tag>
      ),
    },
    {
      title: "当前服务商 / 模型",
      dataIndex: "provider_key",
      width: 420,
      render: (_v, record) => {
        const options = candidatesFor(record.kind);
        const empty = options.length === 0;
        const saving = savingKey === record.business_key;
        // 「生效服务商」= 暂存选择（用户刚在 ① 级点过）优先，否则库内值。
        const chosenKey = effectiveProviderKey(record);
        // ⚠️ v2.11：服务商与模型合并为**一级联组件**（用户口径「应使用二级联动组件展示」）。
        //   规则仍在 `provider-options.ts`（纯函数、有单测钉住），这里只做渲染与提交。
        const providerOptions = buildProviderOptions(options, record.kind);
        const allCatalogEmpty = isAllCatalogEmpty(options, record.kind);
        // 只在该行**还没绑服务商**时才整体锁死：已绑行必须能改选/清空以自救（v2.5 结论）。
        const lockedByAllEmpty = isStepOneLocked(allCatalogEmpty, chosenKey);
        // 级联数据：一级 = 服务商（无该类型模型的置灰并标注原因，仍可见不隐藏）；
        //   二级 = 该服务商在该业务点下的可用模型（后端 `_type_satisfies` 同判）。
        const cascaderOptions = providerOptions.map((o) => ({
          value: o.value,
          label: o.label,
          disabled: !isSelectableForRow(o, chosenKey),
          children: buildModelOptions(
            options.find((p) => p.provider_key === o.value),
            record.kind,
          ),
        }));
        // 已绑定项若仍不可选 ⇒ 说明是「已停用」而非「无目录」（无目录已被上面放行）
        const boundOption = providerOptions.find((o) => o.value === chosenKey);
        const boundDisabled = boundOption?.disabledReason === "disabled";
        // 该服务商在该业务点下没有可用模型（先看候选，再按生效服务商判）
        const noModelCatalog =
          !!chosenKey &&
          buildModelOptions(
            options.find((p) => p.provider_key === chosenKey),
            record.kind,
          ).length === 0;
        // ⚠️ 库里存的模型已不可用（被停用 / 改类型 / 删了）：**不静默清空**，
        //   如实提示 —— 用户没动它，悄悄抹掉才是最难查的那种（同「缺值不造假」）。
        const modelMissing = isModelMissing(chosenKey, record.model_name, options, record.kind);
        // 级联当前值：服务商选好但模型还没选 = 只给一级（中间态）；齐全才给 [服务商, 模型]。
        const cascaderValue: string[] = chosenKey
          ? record.model_name && !modelMissing
            ? [chosenKey, record.model_name]
            : [chosenKey]
          : [];
        return (
          <Space direction="vertical" size={4} style={{ minWidth: 380 }}>
            <Cascader
              style={{ width: 380 }}
              value={cascaderValue.length ? cascaderValue : undefined}
              placeholder={
                empty
                  ? "暂无可用的该类型服务商"
                  : lockedByAllEmpty
                    ? "该类型服务商都还没有可用模型"
                    : "选择服务商 — 模型（选中即保存）"
              }
              disabled={empty || lockedByAllEmpty || saving}
              options={cascaderOptions}
              // 允许只选到一级（服务商）：此刻只落本地暂存，不落库（中间态见 commitDecision）
              changeOnSelect
              allowClear
              showSearch={{
                filter: (input, path) =>
                  path.some((o) => String(o.label ?? "").toLowerCase().includes(input.toLowerCase())),
              }}
              displayRender={(labels) => labels.join(" — ")}
              onChange={(value) => {
                // `changeOnSelect` 下 value 可能只有一级（选了服务商）或两级（服务商+模型）
                const nextProvider = (value?.[0] as string) ?? "";
                const nextModel = (value?.[1] as string) ?? "";
                // 清空（点了 allowClear）⇒ 一次 PUT 清空该行（provider_key="" 合法）
                if (commitDecision(nextProvider, nextModel) === "commit") {
                  void onSwitch(record, nextProvider, nextModel);
                  return;
                }
                // 中间态（选了服务商、还没选模型）⇒ **只在本地暂存**，绝不落库：
                //   此刻 PUT 必 400（后端要求服务商+模型都非空）⇒ provider_key 存不下
                //   ⇒ 级联二级永远打不开 ⇒ 死循环（v2.5 根因，`commitDecision` 守卫）。
                setPendingProvider((prev) => ({ ...prev, [record.business_key]: nextProvider }));
              }}
            />
            {empty && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                还没有{record.kind === "image" ? "图像" : "文本"}类型的服务商，请先到
                「服务商配置」新增。
              </Text>
            )}
            {allCatalogEmpty && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                该类型的 {options.length} 个服务商都还没有可用模型；模型是必选项，
                请先到「服务商配置」对目标服务商执行「测试」以拉取模型目录。
              </Text>
            )}
            {noModelCatalog && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                该服务商在该业务点下没有可用模型。请到「服务商配置」为它补
                {record.kind === "image" ? "图像" : "文本"}类模型；或先把本行服务商
                <b>清空</b>，再改选其它服务商。
              </Text>
            )}
            {boundDisabled && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                本行绑定的服务商<b>已停用</b>。请到「服务商配置」启用它，或先把本行服务商
                <b>清空</b>再改选其它服务商。
              </Text>
            )}
            {modelMissing && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                已存的模型「{record.model_name}」在该业务点下<b>已不可用</b>
                （被停用、改了类型或已删除）。请重新选择模型；或先把本行服务商
                <b>清空</b>再改选。
              </Text>
            )}
          </Space>
        );
      },
    },
    {
      title: "状态",
      dataIndex: "configured",
      width: 150,
      render: (_v, record) => {
        // v2.3：服务商与模型**都**选了才算配好；只选服务商 ⇒ 未配置
        if (!record.provider_key) return <Tag>未配置</Tag>;
        if (record.provider_enabled === false) return <Tag color="orange">服务商已停用</Tag>;
        // v2.17：**类型判据改读后端下发的 `type_satisfied`**（多态模型可服务 text/image）。
        // ⚠️ 原实现自己写 `provider_model_type !== record.kind` 严格相等 —— 那是判据的
        // 第二份实现，与后端 `_type_satisfies` 漂移 ⇒ 多模态服务商被**误报「类型不匹配」**
        // （后端写入校验明明允许）。判据唯一源在后端，前端只渲染（DEF-044）。
        if (record.model_name && record.type_satisfied === false) {
          return <Tag color="red">类型不匹配</Tag>;
        }
        if (!record.model_name) return <Tag color="orange">缺具体模型</Tag>;
        return <Tag color="green">已配置</Tag>;
      },
    },
  ];

  return (
    <>
      {/* 2026-10-05 用户口径「移除所有页面页面头」：原「Model Routing / 模型配置 /
          一句话说明」三行整块移除；刷新图标收进下方服务商表卡片右上角。 */}

      <Card
        title="模型配置"
        extra={
          <Button size="small" icon={<ReloadOutlined />} loading={isLoading} onClick={() => void load()}>
            刷新
          </Button>
        }
      >
        <Table
          rowKey="_key"
          loading={isLoading}
          columns={columns}
          dataSource={rowsWithKey}
          pagination={false}
          size="middle"
        />
      </Card>
    </>
  );
}
