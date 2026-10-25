/*
 * 字典管理页（v1.1.0 / 设计 55 §5 / 49 模块四）。
 *
 * ## 布局：左类型 / 右项
 *
 * 与 dataPanel 的 `t_dict_type` + `t_dict_data` 一致：左边选类型，右边管它的项。
 * 不分两个 Tab —— 两者是父子关系，分开会让「改类型编码后项去哪了」难以追踪。
 *
 * ## 三条口径
 *
 * 1. **改类型 `code` ⇒ 项的 `type_code` 同步**（后端做），页面上给出提示 ——
 *    否则前端按 `code` 取项会一批取不到。
 * 2. **删类型 ⇒ 级联删项**。确认框里必须写出**会连带删几项**
 *    （后端响应里有 `deleted_items`，但确认框要在**点之前**就告诉用户）。
 * 3. **内置类型不许删、不许改 code**（后端拦），页面把按钮不渲染。
 */

import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";

import { useCan } from "../../hooks/use-can";
import {
  createSystemDictItem,
  createSystemDictType,
  deleteSystemDictItem,
  deleteSystemDictType,
  fetchSystemDictItems,
  fetchSystemDictTypes,
  updateSystemDictItem,
  updateSystemDictType,
  type SystemDictItem,
  type SystemDictType,
} from "../../lib/api";
import { refreshDicts } from "../../lib/system-dicts";

const { Text } = Typography;

const LIST_CLASS_OPTIONS = [
  { value: "default", label: "默认（灰）" },
  { value: "success", label: "成功（绿）" },
  { value: "warning", label: "警告（橙）" },
  { value: "danger", label: "危险（红）" },
  { value: "info", label: "信息（蓝）" },
  { value: "processing", label: "进行中（蓝）" },
  { value: "error", label: "错误（红）" },
  { value: "red", label: "红" },
  { value: "volcano", label: "火红" },
  { value: "orange", label: "橙" },
  { value: "gold", label: "金" },
  { value: "yellow", label: "黄" },
  { value: "lime", label: "青柠" },
  { value: "green", label: "绿" },
  { value: "cyan", label: "青" },
  { value: "blue", label: "蓝" },
  { value: "geekblue", label: "极客蓝" },
  { value: "purple", label: "紫" },
  { value: "magenta", label: "洋红" },
  { value: "pink", label: "粉" },
];

export function SystemDictPanel() {
  const can = useCan();
  const { message, modal } = App.useApp();

  const [types, setTypes] = useState<SystemDictType[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [items, setItems] = useState<SystemDictItem[]>([]);
  const [loadingTypes, setLoadingTypes] = useState(false);
  const [loadingItems, setLoadingItems] = useState(false);

  const [typeModal, setTypeModal] = useState<{ mode: "create" | "edit"; row?: SystemDictType } | null>(
    null
  );
  const [itemModal, setItemModal] = useState<{ mode: "create" | "edit"; row?: SystemDictItem } | null>(
    null
  );

  const loadTypes = useCallback(async () => {
    setLoadingTypes(true);
    try {
      const res = await fetchSystemDictTypes();
      setTypes(res.items);
      setActiveId((prev) => {
        if (prev && res.items.some((t) => t.id === prev)) return prev;
        return res.items[0]?.id ?? null;
      });
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载字典类型失败");
    } finally {
      setLoadingTypes(false);
    }
  }, [message]);

  const loadItems = useCallback(
    async (typeId: number | null) => {
      if (!typeId) {
        setItems([]);
        return;
      }
      setLoadingItems(true);
      try {
        const res = await fetchSystemDictItems({ type_id: typeId });
        setItems(res.items);
      } catch (error) {
        message.error(error instanceof Error ? error.message : "加载字典项失败");
      } finally {
        setLoadingItems(false);
      }
    },
    [message]
  );

  useEffect(() => {
    void loadTypes();
  }, [loadTypes]);

  useEffect(() => {
    void loadItems(activeId);
  }, [activeId, loadItems]);

  const activeType = types.find((t) => t.id === activeId) ?? null;

  const typeColumns: ColumnsType<SystemDictType> = [
    {
      title: "类型",
      dataIndex: "name",
      render: (v: string, row) => (
        <Space size={4}>
          <a onClick={() => setActiveId(row.id)}>{v}</a>
          {row.is_system && <Tag color="gold">内置</Tag>}
        </Space>
      ),
    },
    {
      title: "编码",
      dataIndex: "code",
      width: 170,
      render: (v: string) => <Text style={{ fontSize: 12 }}>{v}</Text>,
    },
    { title: "项", dataIndex: "item_count", width: 50, align: "right" },
    {
      title: "",
      key: "ops",
      width: 90,
      render: (_: unknown, row) => (
        <Space size={0}>
          {can("system:dict:edit") && (
            <Button
              size="small"
              type="text"
              icon={<EditOutlined />}
              onClick={() => setTypeModal({ mode: "edit", row })}
            />
          )}
          {can("system:dict:delete") && !row.is_system && (
            <Button
              size="small"
              type="text"
              danger
              icon={<DeleteOutlined />}
              onClick={() => {
                modal.confirm({
                  title: `删除字典类型「${row.name}」？`,
                  content: `将同时删除它的 ${row.item_count} 个字典项（不可恢复）。`,
                  okButtonProps: { danger: true },
                  onOk: async () => {
                    const res = await deleteSystemDictType(row.id);
                    message.success(`已删除（连带 ${res.deleted_items} 项）`);
                    void loadTypes();
                    void refreshDicts();
                  },
                });
              }}
            />
          )}
        </Space>
      ),
    },
  ];

  const itemColumns: ColumnsType<SystemDictItem> = [
    { title: "标签", dataIndex: "label", width: 160 },
    {
      title: "值",
      dataIndex: "value",
      width: 140,
      render: (v: string) => <Text code>{v}</Text>,
    },
    {
      title: "样式",
      dataIndex: "list_class",
      width: 100,
      render: (v: string) => <Tag color={v}>{v}</Tag>,
    },
    { title: "排序", dataIndex: "sort_order", width: 70, align: "right" },
    {
      title: "状态",
      dataIndex: "status",
      width: 80,
      render: (v: string) =>
        v === "normal" ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>,
    },
    { title: "备注", dataIndex: "remark", render: (v: string) => v || "-" },
    {
      title: "操作",
      key: "ops",
      width: 120,
      render: (_: unknown, row) => (
        <Space size={0}>
          {can("system:dict:edit") && (
            <Button
              size="small"
              type="link"
              icon={<EditOutlined />}
              onClick={() => setItemModal({ mode: "edit", row })}
            >
              编辑
            </Button>
          )}
          {can("system:dict:delete") && (
            <Button
              size="small"
              type="link"
              danger
              icon={<DeleteOutlined />}
              onClick={() => {
                modal.confirm({
                  title: `删除字典项「${row.label}」？`,
                  okButtonProps: { danger: true },
                  onOk: async () => {
                    await deleteSystemDictItem(row.id);
                    message.success("已删除");
                    void loadItems(activeId);
                    void loadTypes();
                    void refreshDicts();
                  },
                });
              }}
            >
              删除
            </Button>
          )}
        </Space>
      ),
    },
  ];

  // 设计 63 §6（2026-10-25 用户截图「底部空白」）：双卡撑满视口高度，两张表
  // 都在**表体内部**滚动（`.fill-table` 共享类）—— 旧写法固定 `scroll.y=520`
  // ⇒ 卡片到视口底之间留一大块空白。
  return (
    <div style={{ height: "calc(100vh - 87px)", display: "flex", flexDirection: "column" }}>
    <Row gutter={12} style={{ flex: 1, minHeight: 0 }}>
      <Col span={9} style={{ height: "100%", display: "flex", flexDirection: "column" }}>
        <Card
          size="small"
          style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
          styles={{ body: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" } }}
          title={`字典类型（${types.length}）`}
          extra={
            <Space>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => void loadTypes()}
                loading={loadingTypes}
              />
              {can("system:dict:create") && (
                <Button
                  size="small"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setTypeModal({ mode: "create" })}
                >
                  新建
                </Button>
              )}
            </Space>
          }
        >
          <div className="fill-table">
          <Table<SystemDictType>
            rowKey="id"
            size="small"
            loading={loadingTypes}
            columns={typeColumns}
            dataSource={types}
            pagination={false}
            scroll={{ y: "100%" }}
            rowClassName={(row) => (row.id === activeId ? "ant-table-row-selected" : "")}
            onRow={(row) => ({ onClick: () => setActiveId(row.id) })}
          />
          </div>
        </Card>
      </Col>
      <Col span={15} style={{ height: "100%", display: "flex", flexDirection: "column" }}>
        <Card
          size="small"
          style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
          styles={{ body: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" } }}
          title={
            activeType
              ? `「${activeType.name}」的字典项（${items.length}）`
              : "字典项（请先选一个类型）"
          }
          extra={
            <Space>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => void loadItems(activeId)}
                loading={loadingItems}
              />
              {can("system:dict:create") && activeType && (
                <Button
                  size="small"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setItemModal({ mode: "create" })}
                >
                  新建项
                </Button>
              )}
            </Space>
          }
        >
          <div className="fill-table">
          <Table<SystemDictItem>
            rowKey="id"
            size="small"
            loading={loadingItems}
            columns={itemColumns}
            dataSource={items}
            pagination={false}
            scroll={{ y: "100%" }}
          />
          </div>
        </Card>
      </Col>

      <DictTypeModal
        state={typeModal}
        onClose={() => setTypeModal(null)}
        onSaved={() => {
          setTypeModal(null);
          void loadTypes();
          void refreshDicts();
        }}
      />
      <DictItemModal
        state={itemModal}
        typeId={activeId}
        onClose={() => setItemModal(null)}
        onSaved={() => {
          setItemModal(null);
          void loadItems(activeId);
          void loadTypes();
          void refreshDicts();
        }}
      />
    </Row>
    </div>
  );
}

function DictTypeModal({
  state,
  onClose,
  onSaved,
}: {
  state: { mode: "create" | "edit"; row?: SystemDictType } | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const editing = state?.mode === "edit" ? state.row : undefined;

  useEffect(() => {
    if (!state) return;
    if (editing) {
      form.setFieldsValue({
        code: editing.code,
        name: editing.name,
        status: editing.status,
        remark: editing.remark,
      });
    } else {
      form.resetFields();
      form.setFieldsValue({ status: "normal" });
    }
  }, [state, editing, form]);

  return (
    <Modal
      centered
      open={Boolean(state)}
      title={editing ? `编辑类型 · ${editing.name}` : "新建字典类型"}
      onCancel={onClose}
      onOk={async () => {
        try {
          const values = await form.validateFields();
          if (editing) await updateSystemDictType(editing.id, values);
          else await createSystemDictType(values);
          message.success("已保存");
          onSaved();
        } catch (error) {
          if ((error as { errorFields?: unknown }).errorFields) return;
          message.error(error instanceof Error ? error.message : "保存失败");
        }
      }}
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="code"
          label="编码"
          rules={[{ required: true }, { pattern: /^[a-z][a-z0-9_]{1,63}$/, message: "小写字母开头" }]}
          extra={
            editing?.is_system
              ? "内置类型的编码不允许修改"
              : editing
                ? "改编码会同步更新所有项（否则按 code 取不到项）"
                : "建好后前端按它取项"
          }
        >
          <Input disabled={Boolean(editing?.is_system)} />
        </Form.Item>
        <Form.Item name="name" label="名称" rules={[{ required: true, max: 64 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select
            options={[
              { value: "normal", label: "启用" },
              { value: "disabled", label: "停用" },
            ]}
          />
        </Form.Item>
        <Form.Item name="remark" label="备注">
          <Input.TextArea rows={2} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function DictItemModal({
  state,
  typeId,
  onClose,
  onSaved,
}: {
  state: { mode: "create" | "edit"; row?: SystemDictItem } | null;
  typeId: number | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const editing = state?.mode === "edit" ? state.row : undefined;

  useEffect(() => {
    if (!state) return;
    if (editing) {
      form.setFieldsValue({
        label: editing.label,
        value: editing.value,
        sort_order: editing.sort_order,
        list_class: editing.list_class,
        status: editing.status,
        remark: editing.remark,
      });
    } else {
      form.resetFields();
      form.setFieldsValue({ status: "normal", list_class: "default", sort_order: 0 });
    }
  }, [state, editing, form]);

  return (
    <Modal
      centered
      open={Boolean(state)}
      title={editing ? `编辑字典项 · ${editing.label}` : "新建字典项"}
      onCancel={onClose}
      onOk={async () => {
        try {
          const values = await form.validateFields();
          if (editing) await updateSystemDictItem(editing.id, values);
          else if (typeId) await createSystemDictItem({ type_id: typeId, ...values });
          message.success("已保存");
          onSaved();
        } catch (error) {
          if ((error as { errorFields?: unknown }).errorFields) return;
          message.error(error instanceof Error ? error.message : "保存失败");
        }
      }}
    >
      <Form form={form} layout="vertical">
        <Form.Item name="label" label="标签（显示给人看）" rules={[{ required: true, max: 64 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="value" label="值（存进库的）" rules={[{ required: true, max: 64 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="list_class" label="样式">
          <Select options={LIST_CLASS_OPTIONS} />
        </Form.Item>
        <Form.Item name="sort_order" label="排序">
          <InputNumber style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select
            options={[
              { value: "normal", label: "启用" },
              { value: "disabled", label: "停用" },
            ]}
          />
        </Form.Item>
        <Form.Item name="remark" label="备注">
          <Input.TextArea rows={2} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
