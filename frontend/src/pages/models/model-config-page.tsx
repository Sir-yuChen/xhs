import {
  ApiOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  FileImageOutlined,
  KeyOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  StarOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Col,
  Empty,
  Form,
  Input,
  Popconfirm,
  Row,
  Segmented,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import type { FormEvent } from "react";
import { useEffect, useMemo, useState } from "react";

import { PageHeader } from "../../components/layout/app-shell";
import {
  createModelConfig,
  deleteModelConfig,
  fetchModelConfigs,
  setDefaultModelConfig,
  testModelConfig,
  updateModelConfig,
} from "../../lib/api";
import type { ModelConfig, ModelConfigPayload, ModelType } from "../../types";

const { Text } = Typography;

const emptyForm: ModelConfigPayload = {
  name: "",
  model_type: "text",
  provider: "openai-compatible",
  model_name: "gpt-5.4",
  base_url: "",
  api_key: "",
  is_default: true,
};

function defaultModelName(type: ModelType): string {
  return type === "text" ? "gpt-5.4" : "";
}

function typeLabel(type: ModelType): string {
  return type === "text" ? "文本模型" : "图片模型";
}

function ModelTypeIcon({ type }: { type: ModelType }) {
  return type === "text" ? <RobotOutlined /> : <FileImageOutlined />;
}

const cardStyle = { background: "#1f1f1f", borderColor: "#303030" };

export function ModelConfigPage() {
  const [configs, setConfigs] = useState<ModelConfig[]>([]);
  const [form, setForm] = useState<ModelConfigPayload>(emptyForm);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径：
  // 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）—— 操作结果反馈
  // 不再渲染 inline Alert 占版面；`App.useApp()` 的实例继承主题与 locale。
  const { message } = App.useApp();
  const [testingId, setTestingId] = useState<number | null>(null);
  const [testResults, setTestResults] = useState<Record<number, { status: string; message: string }>>({});

  const grouped = useMemo(
    () => ({
      text: configs.filter((config) => config.model_type === "text"),
      image: configs.filter((config) => config.model_type === "image"),
    }),
    [configs]
  );

  async function loadConfigs() {
    setIsLoading(true);
    try {
      const result = await fetchModelConfigs();
      setConfigs(result.items);
    } catch {
      message.error("模型配置加载失败。");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.name.trim() || !form.model_name.trim()) {
      message.warning("请填写配置名称和模型名称。");
      return;
    }

    // DEF-032: 纵深防御——Base URL 必须是以 http(s):// 开头的合法网址。
    // 即使浏览器的自动填充没被上面的 autoComplete 声明拦下（例如把用户名填进本字段），
    // 脏值也无法入库。显示问题可以容忍，错误值入库不可逆。
    const baseUrl = form.base_url.trim();
    if (baseUrl) {
      let isValidUrl = false;
      try {
        const parsed = new URL(baseUrl);
        isValidUrl = parsed.protocol === "http:" || parsed.protocol === "https:";
      } catch {
        isValidUrl = false;
      }
      if (!isValidUrl) {
        message.warning("Base URL 需为合法网址（以 http:// 或 https:// 开头），请检查输入内容。");
        return;
      }
    }

    setIsSaving(true);
    const payload = {
      ...form,
      name: form.name.trim(),
      model_name: form.model_name.trim(),
      provider: form.provider.trim(),
      base_url: form.base_url.trim(),
      api_key: form.api_key.trim(),
    };
    try {
      if (editingId) {
        const updated = await updateModelConfig(editingId, payload);
        setConfigs((current) => current.map((c) => c.id === updated.id ? updated : (updated.is_default && c.model_type === updated.model_type ? { ...c, is_default: false } : c)));
        message.success(`${typeLabel(updated.model_type)}配置已更新。`);
        setEditingId(null);
      } else {
        const created = await createModelConfig(payload);
        setConfigs((current) => {
          const withoutOldDefault = created.is_default
            ? current.map((config) => config.model_type === created.model_type ? { ...config, is_default: false } : config)
            : current;
          return [created, ...withoutOldDefault];
        });
        message.success(`${typeLabel(created.model_type)}配置已保存。`);
      }
      setForm({ ...emptyForm, model_type: form.model_type });
    } catch {
      message.error("模型配置保存失败。");
    } finally {
      setIsSaving(false);
    }
  }

  function handleEdit(config: ModelConfig) {
    setEditingId(config.id);
    setForm({
      name: config.name,
      model_type: config.model_type,
      provider: config.provider,
      model_name: config.model_name,
      base_url: config.base_url,
      api_key: "",
      is_default: config.is_default,
    });
  }

  function handleCancelEdit() {
    setEditingId(null);
    setForm({ ...emptyForm, model_type: form.model_type });
  }

  async function handleDelete(configId: number) {
    try {
      await deleteModelConfig(configId);
      setConfigs((current) => current.filter((c) => c.id !== configId));
      if (editingId === configId) { setEditingId(null); setForm({ ...emptyForm, model_type: form.model_type }); }
      message.success("配置已删除。");
    } catch {
      message.error("配置删除失败。");
    }
  }

  async function handleTest(configId: number) {
    setTestingId(configId);
    try {
      const result = await testModelConfig(configId);
      setTestResults((prev) => ({ ...prev, [configId]: { status: result.status, message: result.message } }));
    } catch {
      setTestResults((prev) => ({ ...prev, [configId]: { status: "error", message: "检查请求失败" } }));
    } finally {
      setTestingId(null);
    }
  }

  async function handleSetDefault(config: ModelConfig) {
    try {
      const updated = await setDefaultModelConfig(config.id);
      setConfigs((current) =>
        current.map((item) =>
          item.model_type === updated.model_type
            ? { ...item, is_default: item.id === updated.id }
            : item
        )
      );
      message.success(
        `${updated.name} 已设为默认${typeLabel(updated.model_type)}。`
      );
    } catch {
      message.error("默认模型切换失败。");
    }
  }

  useEffect(() => {
    void loadConfigs();
  }, []);

  return (
    <div>
      <PageHeader
        eyebrow="Model Routing"
        title="模型配置"
        description="为改写、生成、封面和图片处理配置用户级文本与图片模型，后续 AI 任务会从默认配置解析调用参数。"
        action={
          <Button
            icon={<ReloadOutlined />}
            onClick={loadConfigs}
            loading={isLoading}
          >
            刷新
          </Button>
        }
      />

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="推荐的 OpenAI 兼容 API 服务"
        description={<>
          <Typography.Link href="https://api.openai-next.com/" target="_blank" rel="noreferrer">api.openai-next.com</Typography.Link> — OpenAI 中转，Base URL: <Typography.Text code>https://api.openai-next.com/v1</Typography.Text><br />
          <Typography.Link href="https://www.volcengine.com/product/doubao" target="_blank" rel="noreferrer">火山引擎（豆包）</Typography.Link> — 字节跳动大模型平台，Base URL: <Typography.Text code>https://ark.cn-beijing.volces.com/api/v3</Typography.Text><br />
          <Typography.Link href="https://bailian.console.aliyun.com/" target="_blank" rel="noreferrer">阿里云百炼</Typography.Link> — 通义千问系列，Base URL: <Typography.Text code>https://dashscope.aliyuncs.com/compatible-mode/v1</Typography.Text>
        </>}
      />

      <Row gutter={[24, 24]}>
        <Col xs={24} lg={8}>
          <Card
            title={
              <Space>
                {editingId ? <EditOutlined /> : <PlusOutlined />}
                <span>{editingId ? "编辑模型" : "新增模型"}</span>
              </Space>
            }
            extra={editingId ? <Button size="small" onClick={handleCancelEdit}>取消编辑</Button> : undefined}
            style={cardStyle}
          >
            <Segmented
              value={form.model_type}
              options={[
                { label: "文本模型", value: "text" },
                { label: "图片模型", value: "image" },
              ]}
              onChange={(val) =>
                setForm((current) => ({
                  ...current,
                  model_type: val as ModelType,
                  model_name: defaultModelName(val as ModelType),
                }))
              }
              block
              style={{ marginBottom: 20 }}
            />

            {/* DEF-032: 该表单含密码型输入框，浏览器会据「登录表单启发式」自动填充
                已保存的本站凭据（Base URL 被填成用户名、API Key 被填成密码）。
                下面的 autoComplete 声明用于让浏览器放弃该启发式；
                同时 handleSubmit 对 base_url 做格式校验，确保脏值无法入库。 */}
            <form onSubmit={handleSubmit} autoComplete="off">
              <Form layout="vertical" component="div">
                <Form.Item label="配置名称">
                  <Input
                    name="model-config-name"
                    autoComplete="off"
                    value={form.name}
                    onChange={(e) =>
                      setForm((current) => ({
                        ...current,
                        name: e.target.value,
                      }))
                    }
                    placeholder="例如：默认文本模型"
                  />
                </Form.Item>
                <Alert message="所有模型需兼容 OpenAI 接口规范" type="info" style={{ marginBottom: 16, fontSize: 12 }} />
                <Form.Item label="模型名称">
                  <Input
                    name="model-model-name"
                    autoComplete="off"
                    value={form.model_name}
                    onChange={(e) =>
                      setForm((current) => ({
                        ...current,
                        model_name: e.target.value,
                      }))
                    }
                    placeholder={
                      form.model_type === "text" ? "gpt-4o-mini" : "gpt-image-1"
                    }
                  />
                </Form.Item>
                <Form.Item label="Base URL">
                  <Input
                    name="model-base-url"
                    autoComplete="url"
                    value={form.base_url}
                    onChange={(e) =>
                      setForm((current) => ({
                        ...current,
                        base_url: e.target.value,
                      }))
                    }
                    placeholder="https://api.example.com/v1"
                  />
                </Form.Item>
                <Form.Item label="API Key">
                  <Input.Password
                    name="model-api-key"
                    autoComplete="new-password"
                    value={form.api_key}
                    onChange={(e) =>
                      setForm((current) => ({
                        ...current,
                        api_key: e.target.value,
                      }))
                    }
                    placeholder="保存后只显示是否已配置"
                  />
                </Form.Item>
                <Form.Item>
                  <Checkbox
                    checked={form.is_default}
                    onChange={(e) =>
                      setForm((current) => ({
                        ...current,
                        is_default: e.target.checked,
                      }))
                    }
                  >
                    设为该类型默认模型
                  </Checkbox>
                </Form.Item>
                <Button
                  type="primary"
                  htmlType="submit"
                  icon={<KeyOutlined />}
                  loading={isSaving}
                  block
                >
                  {isSaving ? "保存中..." : editingId ? "更新配置" : "保存配置"}
                </Button>
              </Form>
            </form>
          </Card>
        </Col>

        <Col xs={24} lg={16}>
          <Row gutter={[16, 16]}>
            {(["text", "image"] as ModelType[]).map((type) => (
              <Col xs={24} md={12} key={type}>
                <Card
                  title={
                    <Space>
                      <ModelTypeIcon type={type} />
                      <span>{typeLabel(type)}</span>
                    </Space>
                  }
                  style={cardStyle}
                >
                  {isLoading ? (
                    <div style={{ textAlign: "center", padding: 24 }}>
                      <Spin tip="正在加载配置..." />
                    </div>
                  ) : grouped[type].length === 0 ? (
                    <Empty
                      image={
                        <ModelTypeIcon type={type} />
                      }
                      description={
                        <div>
                          <Text strong>暂无{typeLabel(type)}</Text>
                          <br />
                          <Text type="secondary">
                            保存一个配置后，AI 改写和生成流程就能读取默认模型。
                          </Text>
                        </div>
                      }
                    />
                  ) : (
                    <Space
                      direction="vertical"
                      style={{ width: "100%" }}
                      size="middle"
                    >
                      {grouped[type].map((config) => (
                        <Card
                          key={config.id}
                          size="small"
                          style={{
                            background: "#262626",
                            borderColor: "#303030",
                          }}
                        >
                          <Space
                            style={{
                              width: "100%",
                              justifyContent: "space-between",
                            }}
                          >
                            <Text strong>{config.name}</Text>
                            {config.is_default && (
                              <Tag
                                icon={<StarOutlined />}
                                color="blue"
                              >
                                默认
                              </Tag>
                            )}
                          </Space>
                          <div style={{ marginTop: 4, marginBottom: 4 }}>
                            <Text>{config.model_name || "未填写模型名称"}</Text>
                          </div>
                          <div
                            style={{
                              marginTop: 8,
                              marginBottom: 8,
                            }}
                          >
                            <Text
                              type="secondary"
                              style={{ fontSize: 12, marginRight: 12 }}
                            >
                              {config.base_url || "未配置 Base URL"}
                            </Text>
                            <Text
                              type="secondary"
                              style={{ fontSize: 12 }}
                            >
                              {config.has_api_key
                                ? "已保存 API Key"
                                : "未保存 API Key"}
                            </Text>
                          </div>
                          <Space size={4} wrap>
                            <Button
                              size="small"
                              icon={<CheckCircleOutlined />}
                              disabled={config.is_default}
                              onClick={() => handleSetDefault(config)}
                            >
                              {config.is_default ? "当前默认" : "设为默认"}
                            </Button>
                            <Button
                              size="small"
                              icon={<ApiOutlined />}
                              loading={testingId === config.id}
                              onClick={() => void handleTest(config.id)}
                            >
                              检查
                            </Button>
                            <Button
                              size="small"
                              icon={<EditOutlined />}
                              onClick={() => handleEdit(config)}
                            >
                              编辑
                            </Button>
                            <Popconfirm title="确定删除此模型配置？" onConfirm={() => void handleDelete(config.id)}>
                              <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
                            </Popconfirm>
                          </Space>
                          {testResults[config.id] && (
                            <div style={{ marginTop: 6 }}>
                              <Tag color={testResults[config.id].status === "ok" ? "success" : "error"}>
                                {testResults[config.id].status === "ok" ? "连接正常" : "连接失败"}
                              </Tag>
                              <Text type="secondary" style={{ fontSize: 11 }}>{testResults[config.id].message}</Text>
                            </div>
                          )}
                        </Card>
                      ))}
                    </Space>
                  )}
                </Card>
              </Col>
            ))}
          </Row>
        </Col>
      </Row>
    </div>
  );
}
