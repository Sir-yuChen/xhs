import { ReloadOutlined } from "@ant-design/icons";
import { Alert, App, Card, Select, Space, Spin, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import { PageHeader } from "../../components/layout/app-shell";
import {
  buildProviderOptions,
  commitDecision,
  isAllCatalogEmpty,
  isSelectableForRow,
  isStepOneLocked,
} from "./provider-options";
import {
  fetchBusinessProviderKeys,
  fetchBusinessProviders,
  fetchGatewayConfigs,
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
      const [mapping, keyList, providerList] = await Promise.all([
        fetchBusinessProviders(XHS_PLATFORM),
        fetchBusinessProviderKeys(),
        fetchGatewayConfigs(),
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

  /** 某业务点的下拉候选 = 本用户的**同 kind** 服务商（图像业务点不列文本服务商） */
  const candidatesFor = useCallback(
    (kind: string) => providers.filter((p) => p.model_type === kind),
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
      render: (_v, record) => {
        const options = candidatesFor(record.kind);
        const empty = options.length === 0;
        const saving = savingKey === record.business_key;
        // 「生效服务商」= 暂存选择（用户刚在 ① 步点过）优先，否则库内值。
        const chosenKey = effectiveProviderKey(record);
        // 模型候选 = **生效服务商**的目录：暂存选择也要能立刻出模型，否则 ② 步还是空的
        const modelOptions = (
          chosenKey
            ? (options.find((p) => p.provider_key === chosenKey)?.models ?? [])
            : []
        ).map((m) => ({ value: m, label: m }));
        const noModelCatalog = !!chosenKey && modelOptions.length === 0;
        // v2.3 修正：模型目录为空的服务商**不可选**（灰掉 + 说明）。
        //   原因：模型是必选项，而空目录 ⇒ 第 ② 步下拉永远是空的 ⇒ 用户选完只能停在
        //   「缺具体模型」的报错态、且**自己无法补救**（唯一出路是去服务商页拉取目录）。
        //   放它可选 = 把用户引进死胡同，正是「报错但不可自救」。规则已抽到
        //   `provider-options.ts` 并有单测钉住（见该文件顶部注释）。
        const providerOptions = buildProviderOptions(options);
        const allCatalogEmpty = isAllCatalogEmpty(options);
        // ⚠️ 收紧置灰条件（用户第二次反馈「还是选不了模型」）：
        //   `allCatalogEmpty` 只该在**本行还没绑任何服务商**时才整体禁用 ① 步。
        //   若本行已经绑着一个空目录服务商（脏配置，如 `TEST-TEXT`），再整体禁用
        //   就把用户锁死在报错态：合法服务商选不了（② 步必然空），连清空也点不了。
        //   此时 ① 步必须可选 —— 已绑定项由 `isSelectableForRow` 放行，用户得以
        //   换服务商或清空来自救。
        const lockedByAllEmpty = isStepOneLocked(allCatalogEmpty, chosenKey);
        const selectableOptions = providerOptions.map((o) => ({
          ...o,
          disabled: !isSelectableForRow(o, chosenKey),
        }));
        // 已绑定项若仍不可选 ⇒ 说明是「已停用」而非「无目录」（无目录已被上面放行）
        const boundOption = providerOptions.find((o) => o.value === chosenKey);
        const boundDisabled = boundOption?.disabledReason === "disabled";
        return (
          <Space direction="vertical" size={4} style={{ minWidth: 340 }}>
            <Select
              style={{ width: 340 }}
              value={chosenKey || undefined}
              placeholder={
                empty
                  ? "暂无可用的该类型服务商"
                  : lockedByAllEmpty
                    ? "该类型服务商都还没有模型目录"
                    : "① 选择服务商"
              }
              disabled={empty || lockedByAllEmpty || saving}
              loading={saving}
              allowClear
              onChange={(value) => {
                // ① 步：`commitDecision` 说 hold 就只存本地暂存，绝不落库
                //    （此刻 PUT 必 400 ⇒ provider_key 存不下 ⇒ ② 步永远打不开）。
                //    清空时是 commit（`provider_key=""` 合法，后端连带清空模型）。
                const next = value ?? "";
                if (commitDecision(next, "") === "commit") {
                  void onSwitch(record, next, "");
                  return;
                }
                setPendingProvider((prev) => ({ ...prev, [record.business_key]: next }));
              }}
              options={selectableOptions}
            />
            <Select
              style={{ width: 340 }}
              value={record.model_name || undefined}
              placeholder={
                !chosenKey
                  ? "② 先选服务商"
                  : noModelCatalog
                    ? "该服务商暂无模型目录"
                    : "② 选择具体模型（必选，选中即保存）"
              }
              disabled={!chosenKey || noModelCatalog || saving}
              loading={saving}
              allowClear
              onChange={(value) => {
                // ② 步：服务商 + 模型齐全 ⇒ commit，一次性落库（唯一写点）。
                //    这里恒为 commit（providerKey 非空、modelName 非空）。
                const nextModel = value ?? "";
                if (commitDecision(chosenKey, nextModel) === "commit") {
                  void onSwitch(record, chosenKey, nextModel);
                }
              }}
              options={modelOptions}
              showSearch
            />
            {empty && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                还没有{record.kind === "image" ? "图像" : "文本"}类型的服务商，请先到
                「服务商配置」新增。
              </Text>
            )}
            {allCatalogEmpty && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                该类型的 {options.length} 个服务商都还没有模型目录；模型是必选项，
                请先到「服务商配置」对目标服务商执行「测试」以拉取模型目录。
              </Text>
            )}
            {noModelCatalog && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                该服务商还没有模型目录（目录为空时选不出模型）。请到「服务商配置」对它
                执行「测试」拉取模型目录；或先把本行服务商<b>清空</b>，再改选其它有目录的服务商。
              </Text>
            )}
            {boundDisabled && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                本行绑定的服务商<b>已停用</b>。请到「服务商配置」启用它，或先把本行服务商
                <b>清空</b>再改选其它服务商。
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
        // 类型不匹配 = 「配置过、之后又改了服务商类型」的后果；必须显出来，不许静默
        if (record.provider_model_type && record.provider_model_type !== record.kind) {
          return <Tag color="red">类型不匹配</Tag>;
        }
        if (!record.model_name) return <Tag color="orange">缺具体模型</Tag>;
        return <Tag color="green">已配置</Tag>;
      },
    },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Model Routing"
        title="模型配置"
        description="为小红书各业务点选择服务商；选中即生效，无需保存。"
        action={
          <ReloadOutlined onClick={() => void load()} style={{ cursor: "pointer" }} />
        }
      />
      <Card>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="服务商与具体模型都必选：先选服务商，再选它下面的具体模型；选中模型即保存。只选服务商不选模型，业务调用会报「该业务点还没配模型」，不会回落到服务商的默认模型，也不会回落到其它服务商。"
        />
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
