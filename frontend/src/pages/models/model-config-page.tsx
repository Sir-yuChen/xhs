import { ReloadOutlined } from "@ant-design/icons";
import { Alert, App, Card, Select, Space, Spin, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import { PageHeader } from "../../components/layout/app-shell";
import {
  fetchBusinessProviderKeys,
  fetchBusinessProviders,
  fetchGatewayConfigs,
  fetchGatewayPlatforms,
  upsertBusinessProvider,
} from "../../lib/api";
import type {
  BusinessKeyOption,
  BusinessProviderRow,
  LlmGatewayConfig,
  LlmGatewayPlatformOption,
} from "../../types";

const { Text, Paragraph } = Typography;

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
  const [platforms, setPlatforms] = useState<LlmGatewayPlatformOption[]>([]);
  const [platform, setPlatform] = useState<string>("xhs");
  const [isLoading, setIsLoading] = useState(true);
  const [savingKey, setSavingKey] = useState<string | null>(null);

  const load = useCallback(
    async (target?: string) => {
      setIsLoading(true);
      try {
        const scope = target ?? platform;
        const [mapping, keyList, providerList, platformList] = await Promise.all([
          fetchBusinessProviders(scope),
          fetchBusinessProviderKeys(),
          fetchGatewayConfigs(),
          fetchGatewayPlatforms(),
        ]);
        setRows(mapping.items);
        setKeys(keyList.items);
        setProviders(providerList.items);
        setPlatforms(platformList.items ?? []);
      } catch {
        // 失败由全局拦截器报（401/400 都带结构化原因）
      } finally {
        setIsLoading(false);
      }
    },
    [platform],
  );

  useEffect(() => {
    void load();
    // 仅首屏拉一次；换平台走下方 Select 的 onChange
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 某业务点的下拉候选 = 本用户的**同 kind** 服务商（图像业务点不列文本服务商） */
  const candidatesFor = useCallback(
    (kind: string) => providers.filter((p) => p.model_type === kind),
    [providers],
  );

  const onSwitch = useCallback(
    async (row: BusinessProviderRow, providerKey: string) => {
      setSavingKey(row.business_key);
      try {
        await upsertBusinessProvider({
          platform: row.platform,
          business_key: row.business_key,
          provider_key: providerKey,
        });
        message.success(providerKey ? "已切换服务商" : "已清空该业务点的服务商");
        await load();
      } catch {
        // 失败由全局拦截器报（类型不匹配 / 服务商不存在都带结构化原因）
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
      title: "当前服务商",
      dataIndex: "provider_key",
      render: (_v, record) => {
        const options = candidatesFor(record.kind);
        const empty = options.length === 0;
        return (
          <Space direction="vertical" size={2} style={{ minWidth: 320 }}>
            <Select
              style={{ width: 320 }}
              value={record.provider_key || undefined}
              placeholder={empty ? "暂无可用的该类型服务商" : "选择服务商（选中即生效）"}
              disabled={empty || savingKey === record.business_key}
              loading={savingKey === record.business_key}
              allowClear
              onChange={(value) => void onSwitch(record, value ?? "")}
              options={options.map((p) => ({
                value: p.provider_key,
                label: p.enabled ? p.name : `${p.name}（已停用）`,
                disabled: !p.enabled,
              }))}
            />
            {empty && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                还没有{record.kind === "image" ? "图像" : "文本"}类型的服务商，请先到
                「服务商配置」新增。
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
        if (!record.configured) return <Tag>未配置</Tag>;
        if (record.provider_enabled === false) return <Tag color="orange">服务商已停用</Tag>;
        // 类型不匹配 = 「配置过、之后又改了服务商类型」的后果；必须显出来，不许静默
        if (record.provider_model_type && record.provider_model_type !== record.kind) {
          return <Tag color="red">类型不匹配</Tag>;
        }
        return <Tag color="green">已配置</Tag>;
      },
    },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Model Routing"
        title="模型配置"
        description="为每个平台的每个业务点选择服务商；选中即生效，无需保存。"
        action={
          <Space>
            <Select
              style={{ width: 160 }}
              value={platform}
              onChange={(value) => {
                setPlatform(value);
                void load(value);
              }}
              options={platforms.map((p) => ({ value: p.value, label: p.label }))}
            />
            <ReloadOutlined onClick={() => void load()} style={{ cursor: "pointer" }} />
          </Space>
        }
      />
      <Card>
        <Paragraph type="secondary" style={{ marginBottom: 16 }}>
          服务商在「服务商配置」页单独维护（名称 / 地址 / 令牌 / 模型类型）。本页只负责把
          业务点指向某个服务商 —— 业务代码按固定 key 取用，改这一行即刻生效。
        </Paragraph>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="未配置的业务点在业务调用时会报「该业务点还没配服务商」，不会回落到其它服务商。"
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
