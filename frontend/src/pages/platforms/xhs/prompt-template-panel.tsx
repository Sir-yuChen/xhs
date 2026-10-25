/**
 * 提示词模板面板（T9 / 设计 17 §7.2、§5.1）。
 *
 * 两层结构：**模块**（可复用片段：风格 / 语气 / 受众 / 结构 / 禁忌）+ **模板**
 * （骨架 `{插槽}` + 引用的模块 id 列表）。
 *
 * ⚠️ 三条纪律：
 *   - **插槽白名单在后端**：这里只做输入框，不本地校验插槽合法性（两份真相源必分叉）；
 *   - **默认模板唯一**：`is_default` 写时清旧由后端保证，这里只展示结果；
 *   - **渲染预览先于保存**：改了骨架先点「预览」看最终提示词 —— 别等生成失败才发现插槽写错。
 */
import {
  App,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography, AutoComplete } from "antd";
import type { ColumnsType } from "antd/es/table";
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useCallback, useEffect, useState } from "react";

import {
  createPromptModule,
  createPromptTemplate,
  deletePromptModule,
  deletePromptTemplate,
  fetchPromptModules,
  fetchPromptTemplates,
  previewPromptTemplate,
  updatePromptModule,
  updatePromptTemplate,
} from "../../../lib/api";
import type { PromptModule, PromptTemplate } from "../../../types";

import { WaitMask } from "../../../components/ui/wait-mask";
import { dictLabel } from "../../../lib/system-dicts";
const { Text, Paragraph } = Typography;
const { TextArea } = Input;

/** 任务类型（与后端 `PROMPT_TASK_TYPES` 同源；中文名只在这里给）。 */
const TASK_TYPE_LABELS: Record<string, string> = {
  text: "文案生成",
  image: "图片生成",
  video: "视频生成",
  cover: "封面生成",
};
const TASK_TYPE_OPTIONS = Object.entries(TASK_TYPE_LABELS).map(([value, label]) => ({
  value,
  label: `${label}（${value}）`,
}));

/** 模块类别建议集（后端只给建议、不做白名单校验，故用自由输入 + 建议）。 */
const CATEGORY_OPTIONS = [
  { value: "style", label: "style（风格）" },
  { value: "tone", label: "tone（语气）" },
  { value: "audience", label: "audience（受众）" },
  { value: "structure", label: "structure（结构）" },
  { value: "taboo", label: "taboo（禁忌）" },
];

export function PromptTemplatePanel() {
  const { message } = App.useApp();
  const [modules, setModules] = useState<PromptModule[]>([]);
  const [templates, setTemplates] = useState<PromptTemplate[]>([]);
  const [loading, setLoading] = useState(false);

  const [moduleForm] = Form.useForm();
  const [templateForm] = Form.useForm();
  const [editingModule, setEditingModule] = useState<PromptModule | null>(null);
  const [editingTemplate, setEditingTemplate] = useState<PromptTemplate | null>(null);
  const [moduleModalOpen, setModuleModalOpen] = useState(false);
  const [templateModalOpen, setTemplateModalOpen] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewText, setPreviewText] = useState("");
  const [previewMissing, setPreviewMissing] = useState<string[]>([]);
  const [previewing, setPreviewing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [modulePage, templatePage] = await Promise.all([
        fetchPromptModules({ pageSize: 100 }),
        fetchPromptTemplates({ pageSize: 100 }),
      ]);
      setModules(modulePage.items);
      setTemplates(templatePage.items);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "提示词数据加载失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function openModuleModal(target: PromptModule | null) {
    setEditingModule(target);
    moduleForm.setFieldsValue(
      target
        ? {
            name: target.name,
            category: target.category,
            content: target.content,
            sort_order: target.sort_order,
            enabled: target.enabled,
          }
        : { name: "", category: "style", content: "", sort_order: 0, enabled: true }
    );
    setModuleModalOpen(true);
  }

  async function submitModule() {
    try {
      const values = await moduleForm.validateFields();
      if (editingModule) {
        await updatePromptModule(editingModule.id, values);
        message.success("模块已更新");
      } else {
        await createPromptModule(values);
        message.success("模块已创建");
      }
      setModuleModalOpen(false);
      await load();
    } catch (error) {
      if (error instanceof Error) message.error(error.message);
      // 表单校验失败（非 Error）静默 —— antd 已在字段下渲染原因
    }
  }

  function openTemplateModal(target: PromptTemplate | null) {
    setEditingTemplate(target);
    templateForm.setFieldsValue(
      target
        ? {
            name: target.name,
            task_type: target.task_type,
            prompt_skeleton: target.prompt_skeleton,
            module_ids: target.module_ids,
            is_default: target.is_default,
          }
        : {
            name: "",
            task_type: "text",
            prompt_skeleton: "",
            module_ids: [],
            is_default: false,
          }
    );
    setTemplateModalOpen(true);
  }

  async function submitTemplate() {
    try {
      const values = await templateForm.validateFields();
      if (editingTemplate) {
        await updatePromptTemplate(editingTemplate.id, values);
        message.success("模板已更新");
      } else {
        await createPromptTemplate(values);
        message.success("模板已创建");
      }
      setTemplateModalOpen(false);
      await load();
    } catch (error) {
      if (error instanceof Error) message.error(error.message);
    }
  }

  async function handlePreview() {
    if (!editingTemplate) {
      message.info("先保存模板，再预览渲染结果");
      return;
    }
    setPreviewing(true);
    try {
      const result = await previewPromptTemplate(editingTemplate.id);
      setPreviewText(result.rendered || "");
      setPreviewMissing(result.missing_slots || []);
      setPreviewOpen(true);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "渲染预览失败");
    } finally {
      setPreviewing(false);
    }
  }

  const moduleColumns: ColumnsType<PromptModule> = [
    { title: "名称", dataIndex: "name", width: 160 },
    {
      title: "类别",
      dataIndex: "category",
      width: 110,
      render: (value: string) => <Tag>{value || "-"}</Tag>,
    },
    {
      title: "内容",
      dataIndex: "content",
      render: (value: string) => (
        <Text type="secondary" ellipsis style={{ maxWidth: 360 }}>
          {value}
        </Text>
      ),
    },
    { title: "排序", dataIndex: "sort_order", width: 70 },
    {
      title: "启用",
      dataIndex: "enabled",
      width: 70,
      render: (value: boolean) => (value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
    },
    {
      title: "操作",
      key: "action",
      width: 130,
      render: (_v, row) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={() => openModuleModal(row)}>
            编辑
          </Button>
          <Popconfirm
            title="删除这个模块？"
            description="引用它的模板会失去这段内容（模板本身不删）。"
            okText="删除"
            cancelText="取消"
            onConfirm={async () => {
              try {
                await deletePromptModule(row.id);
                message.success("已删除");
                await load();
              } catch (error) {
                message.error(error instanceof Error ? error.message : "删除失败");
              }
            }}
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const templateColumns: ColumnsType<PromptTemplate> = [
    { title: "模板名", dataIndex: "name", width: 180 },
    {
      title: "任务类型",
      dataIndex: "task_type",
      width: 140,
      render: (value: string) => (
        <Tag color="blue">{dictLabel("sys_prompt_task_type", value, TASK_TYPE_LABELS) || "-"}</Tag>
      ),
    },
    {
      title: "引用模块",
      dataIndex: "modules",
      render: (_v, row) => {
        const names = (row.modules || []).map((m) => m.name);
        if (names.length === 0) return <Text type="secondary">-</Text>;
        return (
          <Space size={4} wrap>
            {names.map((name) => (
              <Tag key={name}>{name}</Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: "默认",
      dataIndex: "is_default",
      width: 80,
      render: (value: boolean) => (value ? <Tag color="gold">默认</Tag> : "-"),
    },
    {
      title: "操作",
      key: "action",
      width: 130,
      render: (_v, row) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={() => openTemplateModal(row)}>
            编辑
          </Button>
          <Popconfirm
            title="删除这个模板？"
            okText="删除"
            cancelText="取消"
            onConfirm={async () => {
              try {
                await deletePromptTemplate(row.id);
                message.success("已删除");
                await load();
              } catch (error) {
                message.error(error instanceof Error ? error.message : "删除失败");
              }
            }}
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <>
    <Spin spinning={false} indicator={<></>}>
      <Tabs
        items={[
          {
            key: "templates",
            label: "提示词模板",
            children: (
              <Card
                size="small"
                extra={
                  <Space size={8}>
                    <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
                      刷新
                    </Button>
                    <Button
                      type="primary"
                      size="small"
                      icon={<PlusOutlined />}
                      onClick={() => openTemplateModal(null)}
                    >
                      新建模板
                    </Button>
                  </Space>
                }
              >
                {templates.length === 0 ? (
                  <Empty description="还没有提示词模板（模板 = 骨架 + 引用的模块）" />
                ) : (
                  <Table<PromptTemplate>
                    rowKey="id"
                    size="small"
                    columns={templateColumns}
                    dataSource={templates}
                    pagination={false}
                  />
                )}
              </Card>
            ),
          },
          {
            key: "modules",
            label: "可复用模块",
            children: (
              <Card
                size="small"
                extra={
                  <Space size={8}>
                    <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
                      刷新
                    </Button>
                    <Button
                      type="primary"
                      size="small"
                      icon={<PlusOutlined />}
                      onClick={() => openModuleModal(null)}
                    >
                      新建模块
                    </Button>
                  </Space>
                }
              >
                {modules.length === 0 ? (
                  <Empty description="还没有模块。模块是可复用的提示词片段，多个模板都能引用。" />
                ) : (
                  <Table<PromptModule>
                    rowKey="id"
                    size="small"
                    columns={moduleColumns}
                    dataSource={modules}
                    pagination={false}
                  />
                )}
              </Card>
            ),
          },
        ]}
      />

      <Modal
                centered
                open={moduleModalOpen}
        title={editingModule ? "编辑模块" : "新建模块"}
        onCancel={() => setModuleModalOpen(false)}
        onOk={() => void submitModule()}
        okText="保存"
      >
        <Form form={moduleForm} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]}>
            <Input maxLength={128} placeholder="例如：口语化语气" />
          </Form.Item>
          {/* 类别：下拉选已有 / 直接输入新增（与系统配置、违禁词分组同一形态）。
              ⚠️ 原来只开 `showSearch` —— 那是「搜已有项」，placeholder 却写
              「选择或输入」，实际输入不了新值。 */}
          <Form.Item name="category" label="类别">
            <AutoComplete
              allowClear
              placeholder="选择或输入类别"
              options={CATEGORY_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
              filterOption={(input, option) =>
                String(option?.label ?? option?.value ?? "")
                  .toLowerCase()
                  .includes(input.toLowerCase())
              }
            />
          </Form.Item>
          <Form.Item
            name="content"
            label="内容（会插进模板的 {模块} 位置）"
            rules={[{ required: true, message: "请填写内容" }]}
          >
            <TextArea rows={5} maxLength={4000} showCount placeholder="例如：用口语化、轻松的语气，多用短句" />
          </Form.Item>
          <Space size={16}>
            <Form.Item name="sort_order" label="排序" style={{ marginBottom: 0 }}>
              <Input type="number" style={{ width: 100 }} />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked" style={{ marginBottom: 0 }}>
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      <Modal
                centered
                open={templateModalOpen}
        title={editingTemplate ? "编辑模板" : "新建模板"}
        width={680}
        onCancel={() => setTemplateModalOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setTemplateModalOpen(false)}>取消</Button>
            <Button loading={previewing} onClick={() => void handlePreview()}>
              渲染预览
            </Button>
            <Button type="primary" onClick={() => void submitTemplate()}>
              保存
            </Button>
          </Space>
        }
      >
        <Form form={templateForm} layout="vertical">
          <Form.Item name="name" label="模板名" rules={[{ required: true, message: "请填写模板名" }]}>
            <Input maxLength={128} placeholder="例如：小红书商品种草文案" />
          </Form.Item>
          <Form.Item
            name="task_type"
            label="任务类型"
            rules={[{ required: true, message: "请选择任务类型" }]}
          >
            <Select options={TASK_TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="prompt_skeleton"
            label="骨架（用 {字段} 占位；可用 {模块} 引用所选模块）"
            rules={[{ required: true, message: "请填写骨架" }]}
          >
            <TextArea rows={8} placeholder="例如：为商品「{商品标题}」写一篇小红书笔记。{模块}" />
          </Form.Item>
          <Form.Item name="module_ids" label="引用模块（按顺序拼接）">
            <Select
              mode="multiple"
              allowClear
              placeholder="选择要引用的模块（可留空）"
              options={modules.map((m) => ({ value: m.id, label: `${m.name}（${m.category}）` }))}
            />
          </Form.Item>
          <Form.Item name="is_default" label="设为该任务类型的默认模板" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Text type="secondary" style={{ fontSize: 12 }}>
            同一任务类型只有一个默认模板（保存时会自动清掉旧的）。
          </Text>
        </Form>
      </Modal>

      <Modal
                centered
                open={previewOpen}
        title="渲染预览"
        width={680}
        onCancel={() => setPreviewOpen(false)}
        footer={<Button onClick={() => setPreviewOpen(false)}>关闭</Button>}
      >
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          {previewMissing.length > 0 && (
            <Text type="warning" style={{ fontSize: 12 }}>
              未识别的插槽：{previewMissing.join("、")}（这些位置渲染后会原样留空）
            </Text>
          )}
          <Paragraph
            style={{
              whiteSpace: "pre-wrap",
              background: "rgba(0,0,0,0.03)",
              padding: 12,
              borderRadius: 4,
              maxHeight: 420,
              overflow: "auto",
              marginBottom: 0,
            }}
          >
            {previewText || "（渲染结果为空）"}
          </Paragraph>
        </Space>
      </Modal>
    </Spin>
<WaitMask open={loading} title="正在加载提示词模板" />
    </>
  );
}
