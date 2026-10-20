import { WaitMask } from "../../components/ui/wait-mask";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  App,
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { PlusOutlined, ReloadOutlined, SettingOutlined } from "@ant-design/icons";

import {
  createSystemSetting,
  deleteSystemSetting,
  fetchSystemSettings,
  updateSystemSetting,
} from "../../lib/api";
import {
  CATEGORY_LABELS,
  VALUE_TYPE_OPTIONS,
  categoryLabel,
  categoryOptions,
  emptyFormValues,
  filterByCategory,
  formValuesToCreatePayload,
  formValuesToUpdatePayload,
  liveBadge,
  renderSettingValue,
  settingToFormValues,
  type SettingFormValues,
} from "../../lib/settings-view";
import type { SystemSetting } from "../../types";

const { Paragraph, Text } = Typography;

const CATEGORY_OPTIONS = Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label }));

/** 表单值类型别名（唯一实现在 `lib/settings-view.ts`，页面只引用）。 */
type FormValues = SettingFormValues;

export function SettingsPage() {
  // 消息提醒一律走 `App.useApp()` 的实例（项目口径：禁静态 `import { message }`）。
  const { message } = App.useApp();
  const [form] = Form.useForm<FormValues>();

  const [settings, setSettings] = useState<SystemSetting[]>([]);
  const [loading, setLoading] = useState(false);
  // 失败三分法：取不到 ⇒ 一行安静说明（不铺满红块）；真异常才带原因上红块。
  const [loadError, setLoadError] = useState("");
  const [category, setCategory] = useState<string>("all");

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<SystemSetting | null>(null);
  const [saving, setSaving] = useState(false);
  // 弹窗内的值控件跟随所选类型切换 —— 用一个受控 state 而不是 `Form.useWatch`
  // （后者在 Modal 首次挂载前拿不到值，会出现「切了类型控件不变」的中间态）。
  const [valueType, setValueType] = useState<string>("int");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchSystemSettings();
      setSettings(data.items);
      setLoadError("");
    } catch (error) {
      // 错误消息由 `lib/api.ts` 的全局拦截器统一弹出（非 `_silent`），
      // 这里只负责把「页面处于取不到数据」这件事**显示出来**（否则是空白表格）。
      setLoadError(error instanceof Error ? error.message : "配置加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const categories = useMemo(() => categoryOptions(settings), [settings]);

  const rows = useMemo(() => filterByCategory(settings, category), [settings, category]);

  const openCreate = () => {
    setEditing(null);
    setModalOpen(true);
  };

  const openEdit = (setting: SystemSetting) => {
    setEditing(setting);
    setModalOpen(true);
  };

  // ⚠️ **灌值必须在弹窗打开之后**（真机实测踩到的坑，2026-09-27）：
  // 弹窗带 `destroyOnClose` ⇒ **关闭时 Form 整个被卸载**，打开前它还没挂载。
  // 在打开前调 `form.setFieldsValue()`，值虽然写进了 FormStore，但 Form 挂载时
  // 会被丢掉 ⇒ 表现是「标题对（说明 `editing` 生效了）、**字段全空**」。
  // 真机复现（CDP 读 DOM）：`编辑配置 · xhs_sync_account_gap_seconds` + 6 个字段全空。
  // 正确做法 = 等打开后的这次 commit（Form 已挂载、字段已注册）再灌。
  useEffect(() => {
    if (!modalOpen) return;
    const initial = editing ? settingToFormValues(editing) : emptyFormValues();
    setValueType(initial.value_type);
    form.setFieldsValue(initial);
  }, [modalOpen, editing, form]);

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (editing) {
        // 编辑：不带 key（后端改键要删旧建新，界面也不给改）
        await updateSystemSetting(editing.key, formValuesToUpdatePayload(values));
        message.success(`${editing.key} 已更新${editing.is_live ? "（已实时生效）" : ""}`);
      } else {
        await createSystemSetting(formValuesToCreatePayload(values));
        message.success(`${values.key.trim()} 已新增`);
      }
      setModalOpen(false);
      await load();
    } catch {
      // 失败消息由全局拦截器弹出（含后端给的结构化原因），此处不重复报。
    } finally {
      setSaving(false);
    }
  };

  const remove = async (setting: SystemSetting) => {
    try {
      const result = await deleteSystemSetting(setting.key);
      const fallback = result.effective_value;
      message.success(
        `${setting.key} 已删除，回落为 ${
          fallback === null || fallback === undefined ? "无（无运行时读点）" : String(fallback)
        }`
      );
      await load();
    } catch {
      // 同上：失败由全局拦截器报。
    }
  };

  const columns: ColumnsType<SystemSetting> = [
    {
      title: "配置键",
      dataIndex: "key",
      width: 240,
      render: (key: string, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{key}</Text>
          {record.label && record.label !== key ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {record.label}
            </Text>
          ) : null}
        </Space>
      ),
    },
    {
      title: "当前值",
      dataIndex: "value",
      width: 120,
      render: (_value, record) => <Text code>{renderSettingValue(record)}</Text>,
    },
    {
      title: "类型",
      dataIndex: "value_type",
      width: 80,
      render: (type: string) => <Text type="secondary">{type}</Text>,
    },
    {
      title: "分组",
      dataIndex: "category",
      width: 120,
      render: (value: string) => <Tag>{categoryLabel(value)}</Tag>,
    },
    {
      title: "说明",
      dataIndex: "description",
      render: (description: string) =>
        description ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {description}
          </Text>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
    {
      title: "生效性",
      dataIndex: "is_live",
      width: 130,
      render: (_isLive: boolean, record) => {
        const badge = liveBadge(record);
        return (
          <Tooltip title={badge.tip}>
            <Tag color={record.is_live ? "green" : undefined}>{badge.label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 170,
      render: (value: string | null) =>
        value ? <Text type="secondary">{value.replace("T", " ").slice(0, 19)}</Text> : <Text type="secondary">-</Text>,
    },
    {
      title: "操作",
      key: "actions",
      width: 140,
      render: (_value, record) => (
        <Space size="small">
          <Button type="link" size="small" onClick={() => openEdit(record)}>
            编辑
          </Button>
          <Popconfirm
            title="删除该配置项？"
            description={
              record.is_live
                ? "删除后该键会回落到默认值（配置文件 / 内置默认），并立即生效。"
                : "删除后该键会从列表消失。"
            }
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => remove(record)}
          >
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      {/* 2026-10-05 用户口径「移除所有页面页面头」：原「Workspace / 设置 / 一句描述」
          三行整块移除（与侧栏菜单项重复）；本页第一张卡「系统参数」自带标题与刷新入口。 */}

      {/*
        2026-09-26 用户口径「设置页面移除项目声明」：原第二张「项目声明」卡
        （开源警告 Alert + 四条禁止条款）**整卡移除**，`WarningOutlined` / `Alert`
        导入随之下线。
        2026-09-27（设计 10 §7）：本页新增「系统参数」卡 —— 运行时配置中心的
        列表 + 增删改查；「安全边界」保留为第二张卡。
        2026-10-05 用户口径「设置页的安全边界模块移除」：该卡**整块删除**
        （正文那句「双重归属校验 / Cookie 与模型 Key 统一加密存储」是**实现声明**，
        不是可配置项；清单类说明不该占版面）⇒ 本页现在只剩「系统参数」一张卡。
        原句保留备查，不静默删除。
      */}
      <Row gutter={[16, 16]}>
        <Col xs={24}>
          <Card
            title={
              <span>
                <SettingOutlined style={{ marginRight: 8 }} />
                系统参数
              </span>
            }
            extra={
              <Space>
                <Segmented
                  size="small"
                  value={category}
                  onChange={(value) => setCategory(String(value))}
                  options={categories}
                />
                <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
                  刷新
                </Button>
                <Button size="small" type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                  新增
                </Button>
              </Space>
            }
          >
            <Paragraph type="secondary" style={{ marginBottom: 12 }}>
              标记「实时生效」的参数改完立即作用于运行中的任务（无需重启）；
              标记「仅存档」的键没有运行时读点，改它不影响程序行为。
              限流额度调大 = 同步更快，但平台风控是真实的，建议逐步调整并观察。
            </Paragraph>

            {loadError ? (
              <Text type="danger" style={{ display: "block", marginBottom: 12 }}>
                配置加载失败：{loadError}
              </Text>
            ) : null}

            {/* 2026-10-20 统一：表格等待由公共 WaitMask 表达（Table 自带 loading 会渲染

                antd Spin 覆盖，与 WaitMask 叠成两层 ⇒ 已移除）。 */}

            <WaitMask open={loading} title="正在加载系统参数" />

            <Table<SystemSetting>
              rowKey="id"
              size="small"
          //: ⚠️ 2026-10-20 统一：移除 Table 自带 loading（它渲染 antd Spin，会与 WaitMask 叠两层）。
              columns={columns}
              dataSource={rows}
              pagination={false}
              locale={{ emptyText: loadError ? "取不到配置（见上方说明）" : "还没有配置项" }}
            />
          </Card>
        </Col>
      </Row>

      <Modal
                centered
                open={modalOpen}
        title={editing ? `编辑配置 · ${editing.key}` : "新增配置"}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onOk={() => void submit()}
        onCancel={() => setModalOpen(false)}
        destroyOnClose
      >
        {/*
          ⚠️ 刻意**不写** `preserve={false}`：它会在字段卸载时清掉 store 里的值，
          与「打开后再灌值」的时序叠在一起只会让「值什么时候还在」更难判断。
          弹窗本来就有 `destroyOnClose`（每次打开都是新的），不需要再加一层。
        */}
        <Form form={form} layout="vertical">
          <Form.Item
            name="key"
            label="配置键"
            rules={[{ required: true, message: "请填写配置键" }]}
            extra={editing ? "配置键不可修改（改键 = 删旧建新）。" : "英文小写 + 下划线，例如 xhs_sync_account_parallelism"}
          >
            <Input disabled={Boolean(editing)} placeholder="crawl_rate_limit_per_minute" />
          </Form.Item>

          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="value_type" label="值类型" rules={[{ required: true }]}>
                <Select
                  options={VALUE_TYPE_OPTIONS}
                  onChange={(value) => setValueType(String(value))}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="category" label="分组">
                <Select options={CATEGORY_OPTIONS} />
              </Form.Item>
            </Col>
          </Row>

          {valueType === "bool" ? (
            <Form.Item name="bool_value" label="值" valuePropName="checked">
              <Switch checkedChildren="开" unCheckedChildren="关" />
            </Form.Item>
          ) : valueType === "int" || valueType === "float" ? (
            <Form.Item
              name="number_value"
              label="值"
              rules={[{ required: true, message: "请填写数值" }]}
              extra={
                editing?.is_live
                  ? "该键已接入运行时读点，保存后立即生效。"
                  : undefined
              }
            >
              <InputNumber style={{ width: "100%" }} precision={valueType === "int" ? 0 : undefined} />
            </Form.Item>
          ) : (
            <Form.Item
              name="text_value"
              label="值"
              rules={
                valueType === "json"
                  ? [
                      {
                        validator: async (_rule, value: string) => {
                          if (!value) return;
                          try {
                            JSON.parse(value);
                          } catch {
                            throw new Error("不是合法 JSON");
                          }
                        },
                      },
                    ]
                  : undefined
              }
            >
              <Input.TextArea rows={valueType === "json" ? 4 : 2} />
            </Form.Item>
          )}

          <Form.Item name="label" label="中文名">
            <Input placeholder="平台请求限流额度（次/分钟）" />
          </Form.Item>

          <Form.Item name="description" label="说明">
            <Input.TextArea rows={2} placeholder="改了会怎样 —— 这句话是给下次排障的人看的" />
          </Form.Item>

          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="requires_restart" label="需重启生效" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="is_dangerous" label="危险项（保存前二次确认）" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>
    </div>
  );
}
