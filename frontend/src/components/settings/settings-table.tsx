/**
 * 三级配置通用表格（设计 64 §1.6）：`scope` 决定读/写哪一级，
 * 三处挂载点共用 ——
 * ① 系统管理 → 配置管理（`scope="system"`，admin）；
 * ② 头像下拉「用户配置」弹窗 + `/settings` 页（`scope="user"`，本人）；
 * ③ WorkspaceShell 顶栏齿轮「工作区配置」弹窗（`scope="workspace"`）。
 *
 * 读写契约（设计 64 §1.5）：用户级/工作级配置**按需创建** ——
 * 界面上没有行的键点「编辑/覆盖」走 POST（建覆盖），已有行走 PUT；
 * 删除 = 回落到下一级（弹窗文案随 scope 变）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  App,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
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
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";

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
import { useDictOptions } from "../../lib/system-dicts";

const { Text } = Typography;

const CATEGORY_OPTIONS = Object.entries(CATEGORY_LABELS).map(([value, label]) => ({
  value,
  label,
}));

type FormValues = SettingFormValues;

export type SettingsTableProps = {
  scope: "user" | "system" | "workspace";
  /** `scope="workspace"` 必传；其余级忽略。 */
  workspace?: string;
  /** 卡头标题（弹窗/面板各自给）。 */
  title: string;
  /** 是否显示写按钮（后端仍有 admin 收口 —— 前端只做显隐）。 */
  canEdit?: boolean;
};

export function SettingsTable({ scope, workspace, title, canEdit = true }: SettingsTableProps) {
  const valueTypeOptions = useDictOptions("sys_setting_value_type", VALUE_TYPE_OPTIONS);
  const { message, modal } = App.useApp();
  const [form] = Form.useForm<FormValues>();

  const [settings, setSettings] = useState<SystemSetting[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [category, setCategory] = useState<string>("all");

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<SystemSetting | null>(null);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [valueType, setValueType] = useState<string>("int");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchSystemSettings({ scope, workspace });
      setSettings(data.items);
      setLoadError("");
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "配置加载失败");
    } finally {
      setLoading(false);
    }
  }, [scope, workspace]);

  useEffect(() => {
    void load();
  }, [load]);

  const categories = useMemo(() => categoryOptions(settings), [settings]);
  const rows = useMemo(() => filterByCategory(settings, category), [settings, category]);

  const openCreate = () => {
    setEditing(null);
    setCreating(true);
    setModalOpen(true);
  };

  const openEdit = (setting: SystemSetting) => {
    setEditing(setting);
    setCreating(false);
    setModalOpen(true);
  };

  useEffect(() => {
    if (!modalOpen) return;
    const initial = editing && !creating ? settingToFormValues(editing) : emptyFormValues();
    setValueType(initial.value_type);
    form.setFieldsValue(initial);
  }, [modalOpen, editing, creating, form]);

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (editing && !creating) {
        await updateSystemSetting(editing.key, {
          ...formValuesToUpdatePayload(values),
          scope,
          workspace,
        });
        message.success(`${editing.key} 已更新（${scopeLabel(scope)}）`);
      } else {
        await createSystemSetting({
          ...formValuesToCreatePayload(values),
          scope,
          workspace,
        });
        message.success(`${values.key.trim()} 已新增到${scopeLabel(scope)}配置`);
      }
      setModalOpen(false);
      await load();
    } catch {
      // 失败消息由全局拦截器弹出（含后端结构化原因）。
    } finally {
      setSaving(false);
    }
  };

  const remove = async (setting: SystemSetting) => {
    try {
      const result = await deleteSystemSetting(setting.key, { scope, workspace });
      const fallback = result.effective_value;
      message.success(
        `${setting.key} 已删除，回落为 ${
          fallback === null || fallback === undefined ? "无（无运行时读点）" : String(fallback)
        }`
      );
      await load();
    } catch {
      // 同上。
    }
  };

  const confirmRemove = (setting: SystemSetting) => {
    modal.confirm({
      title: `删除${scopeLabel(scope)}配置「${setting.key}」？`,
      content: DELETE_HINTS[scope],
      okButtonProps: { danger: true },
      okText: "删除",
      cancelText: "取消",
      onOk: () => remove(setting),
    });
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
      width: 110,
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
      width: 110,
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
      width: 150,
      render: (value: string | null) =>
        value ? (
          <Text type="secondary">{value.replace("T", " ").slice(0, 19)}</Text>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
    ...(canEdit
      ? [
          {
            title: "操作",
            key: "actions",
            width: 130,
            render: (_value: unknown, record: SystemSetting) => (
              <Space size="small">
                <Button type="link" size="small" onClick={() => openEdit(record)}>
                  编辑
                </Button>
                <Button type="link" size="small" danger onClick={() => confirmRemove(record)}>
                  删除
                </Button>
              </Space>
            ),
          },
        ]
      : []),
  ];

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 12,
          flexWrap: "wrap",
          gap: 8,
        }}
      >
        <Segmented
          size="small"
          value={category}
          onChange={(value) => setCategory(String(value))}
          options={categories}
        />
        <Space>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
            刷新
          </Button>
          {canEdit && (
            <Button size="small" type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              {CREATE_LABELS[scope]}
            </Button>
          )}
        </Space>
      </div>

      <Text type="secondary" style={{ display: "block", marginBottom: 12 }}>
        {HINTS[scope]}
      </Text>

      {loadError ? (
        <Text type="danger" style={{ display: "block", marginBottom: 12 }}>
          配置加载失败：{loadError}
        </Text>
      ) : null}

      <Table<SystemSetting>
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={rows}
        pagination={false}
        scroll={{ x: 900, y: "100%" }}
        rootClassName="fill-table"
        locale={{ emptyText: loadError ? "取不到配置（见上方说明）" : EMPTY_HINTS[scope] }}
      />

      <Modal
        centered
        open={modalOpen}
        title={editing && !creating ? `编辑配置 · ${editing.key}` : CREATE_LABELS[scope]}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onOk={() => void submit()}
        onCancel={() => setModalOpen(false)}
        destroyOnClose
        width="46vw"
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="key"
            label="配置键"
            rules={[{ required: true, message: "请填写配置键" }]}
            extra={editing && !creating ? "配置键不可修改（改键 = 删旧建新）。" : "英文小写 + 下划线"}
          >
            <Input disabled={Boolean(editing) && !creating} placeholder="crawl_rate_limit_per_minute" />
          </Form.Item>

          <div style={{ display: "flex", gap: 12 }}>
            <Form.Item name="value_type" label="值类型" rules={[{ required: true }]} style={{ flex: 1 }}>
              <Select options={valueTypeOptions} onChange={(value) => setValueType(String(value))} />
            </Form.Item>
            <Form.Item name="category" label="分组" style={{ flex: 1 }}>
              <Select options={CATEGORY_OPTIONS} />
            </Form.Item>
          </div>

          {valueType === "bool" ? (
            <Form.Item name="bool_value" label="值" valuePropName="checked">
              <Switch checkedChildren="开" unCheckedChildren="关" />
            </Form.Item>
          ) : valueType === "int" || valueType === "float" ? (
            <Form.Item
              name="number_value"
              label="值"
              rules={[{ required: true, message: "请填写数值" }]}
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

          <div style={{ display: "flex", gap: 12 }}>
            <Form.Item name="requires_restart" label="需重启生效" valuePropName="checked" style={{ flex: 1 }}>
              <Switch />
            </Form.Item>
            <Form.Item name="is_dangerous" label="危险项（保存前二次确认）" valuePropName="checked" style={{ flex: 1 }}>
              <Switch />
            </Form.Item>
          </div>
        </Form>
      </Modal>
    </div>
  );
}

function scopeLabel(scope: SettingsTableProps["scope"]): string {
  return scope === "system" ? "系统级" : scope === "workspace" ? "工作区级" : "用户级";
}

const CREATE_LABELS: Record<SettingsTableProps["scope"], string> = {
  user: "新增个人覆盖",
  system: "新增系统级配置",
  workspace: "新增工作区配置",
};

const EMPTY_HINTS: Record<SettingsTableProps["scope"], string> = {
  user: "还没有个人覆盖 —— 在「新增个人覆盖」里建一个键，它会对你的账号优先生效",
  system: "还没有系统级配置 —— 新增的键对全站所有用户生效",
  workspace: "当前工作区还没有配置 —— 新增的键在该工作区内生效（优先于用户/系统级）",
};

const HINTS: Record<SettingsTableProps["scope"], string> = {
  user: "个人覆盖：对**你的账号**优先于工作区级与系统级生效；删除 = 回落到工作区/系统级。",
  system: "系统级：对**全站所有用户**生效（用户级/工作区级覆盖仍可各自优先）。改完立即生效，无需重启。",
  workspace: "工作区级：只对**当前工作区**生效，优先于用户级与系统级。改完立即生效。",
};

const DELETE_HINTS: Record<SettingsTableProps["scope"], string> = {
  user: "删除后回落到工作区级 / 系统级 / 内置默认，并立即生效。",
  system: "删除后全站回落到配置文件 / 内置默认，立即生效。",
  workspace: "删除后当前工作区回落到用户级 / 系统级。",
};
