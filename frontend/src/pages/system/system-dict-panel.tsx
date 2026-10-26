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
  SearchOutlined,
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
import { hasPermission } from "../../hooks/use-permission";
import { useCallback, useEffect, useState } from "react";

import { RowActions } from "../../components/common/row-actions";
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
  //: 左栏 43 种类型只能滚动找（用户截图里已经在滚，底部类型被切掉）⇒ 加搜索。
  const [typesFilter, setTypesFilter] = useState("");

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

  //: ⚠️ 2026-10-26 用户口径（字典布局截图）：「一些数据无法展示完成，一些没有数据
  //: 占用大量空间」⇒ 两处根因都是**列宽写死**：
  //:  * 左栏 `编码` 写死 170px，而真实编码（`sys_queue_source`）不需要那么宽，
  //:    于是「项」列被挤成单字表头；
  //:  * 右栏 `值` 写死 140px，`note_publish_single` 这类长值被折成两行（截图实证），
  //:    而 `备注` 列吃 flex 剩余宽度、17 行全是 `-` —— 空列占走大量空间。
  //: 判据：**内容决定宽度，空列不占位**。
  //:  * 类型/编码/标签/值都是**定长内容** ⇒ 给足宽度且 `whiteSpace:nowrap`；
  //:  * `备注` 几乎全空 ⇒ 没有内容时整列不渲染（不留空列）。
  const hasAnyRemark = items.some((it) => (it.remark || "").trim() !== "");
  const typesQ = typesFilter.trim().toLowerCase();
  const filteredTypes = typesQ
    ? types.filter(
        (t) =>
          t.name.toLowerCase().includes(typesQ) || t.code.toLowerCase().includes(typesQ)
      )
    : types;

  const typeColumns: ColumnsType<SystemDictType> = [
    {
      title: "类型",
      dataIndex: "name",
      ellipsis: true,
      render: (v: string, row) => (
        <Space size={4}>
          <a onClick={() => setActiveId(row.id)} title={v}>
            {v}
          </a>
          {row.is_system && (
            <Tag color="gold" style={{ marginInlineEnd: 0 }}>
              内置
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: "编码",
      dataIndex: "code",
      //: 定长内容：按**实测最长编码**定宽（`xhs_violation_punish_status` 27 字符
      //: ≈ 242px @12px monospace）⇒ 给 250，再长才省略号 + tooltip 兜全。
      //: 旧值 170 是「挤走相邻列」的直接原因（截图里「项」表头被压成单字）。
      width: 250,
      render: (v: string) => (
        <Text style={{ fontSize: 12, fontFamily: "monospace" }} ellipsis={{ tooltip: v }}>
          {v}
        </Text>
      ),
    },
    {
      title: "项数",
      dataIndex: "item_count",
      width: 64,
      align: "right",
    },
    {
      title: "",
      key: "ops",
      width: 72,
      align: "right",
      render: (_: unknown, row) => (
        <Space size={0}>
          {can("system:dict:edit") && (
            <Button
              size="small"
              type="text"
              icon={<EditOutlined />}
              onClick={(e) => {
                e.stopPropagation();
                setTypeModal({ mode: "edit", row });
              }}
            />
          )}
          {can("system:dict:delete") && !row.is_system && (
            <Button
              size="small"
              type="text"
              danger
              icon={<DeleteOutlined />}
              onClick={(e) => {
                e.stopPropagation();
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
    { title: "标签", dataIndex: "label", width: 200, ellipsis: true },
    {
      title: "值",
      dataIndex: "value",
      //: ⚠️ 截图的直接病根：140px 装不下 `note_publish_single` ⇒ 折成两行。
      //: 宽度按**实测最长值**定：`product_note_generate_llm_single` 32 字符
      //: ≈ 278px @12px monospace ⇒ 给 300，再长才省略号 + tooltip。
      width: 300,
      render: (v: string) => (
        <Text code style={{ whiteSpace: "nowrap" }} ellipsis={{ tooltip: v }}>
          {v}
        </Text>
      ),
    },
    {
      title: "样式",
      dataIndex: "list_class",
      width: 110,
      render: (v: string) => (
        <Tag color={v} style={{ marginInlineEnd: 0 }}>
          {v}
        </Tag>
      ),
    },
    { title: "排序", dataIndex: "sort_order", width: 72, align: "right" },
    {
      title: "状态",
      dataIndex: "status",
      width: 80,
      render: (v: string) =>
        v === "normal" ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>,
    },
    //: 备注只在**这一组里有非空值**时才出现 —— 17 行全 `-` 的空列不再吃掉
    //: 一整列横向空间（用户口径「没有数据占用大量空间」）。
    ...(hasAnyRemark
      ? ([
          {
            title: "备注",
            dataIndex: "remark",
            ellipsis: true,
            render: (v: string) => v || "-",
          },
        ] as ColumnsType<SystemDictItem>)
      : []),
    {
      title: "操作",
      key: "ops",
      //: 2026-10-25 用户口径「所有操作列均不允许溢出页面」：
      //: 原 120px 手写按钮组，改用统一容器（无权限不渲染 / 超出进「更多」）。
      width: 140,
      render: (_: unknown, row) => (
        <RowActions
          width={140}
          maxInline={2}
          items={[
            {
              label: "编辑",
              permission: "system:dict:edit",
              onClick: () => setItemModal({ mode: "edit", row }),
            },
            {
              label: "删除",
              permission: "system:dict:delete",
              danger: true,
              onClick: () => {
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
              },
            },
          ]}
        />
      ),
    },
  ];

  // 设计 63 §6（2026-10-25 用户截图「底部空白」）：双卡撑满视口高度，两张表
  // 都在**表体内部**滚动（`.fill-table` 共享类）—— 旧写法固定 `scroll.y=520`
  // ⇒ 卡片到视口底之间留一大块空白。
  return (
    <div style={{ height: "calc(100vh - 87px)", display: "flex", flexDirection: "column" }}>
    <Row gutter={12} style={{ flex: 1, minHeight: 0 }}>
      <Col span={8} style={{ height: "100%", display: "flex", flexDirection: "column" }}>
        <Card
          size="small"
          style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
          styles={{ body: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden", paddingBottom: 8 } }}
          title={`字典类型（${filteredTypes.length}${typesQ ? ` / ${types.length}` : ""}）`}
          extra={
            <Space size={8}>
              <Input
                allowClear
                size="small"
                prefix={<SearchOutlined style={{ color: "#bbb" }} />}
                placeholder="搜类型名 / 编码"
                value={typesFilter}
                onChange={(e) => setTypesFilter(e.target.value)}
                style={{ width: 170 }}
              />
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
            dataSource={filteredTypes}
            pagination={false}
            //: ⚠️ 横向也给 `max-content`：`编码 200 + 项数 64 + 操作 72` 在小屏
            //: 可能超过栏宽 ⇒ 表体横向滚动，而不是把列挤成竖排文字。
            scroll={{ x: "max-content", y: "100%" }}
            rowClassName={(row) => (row.id === activeId ? "ant-table-row-selected" : "")}
            onRow={(row) => ({ onClick: () => setActiveId(row.id) })}
          />
          </div>
        </Card>
      </Col>
      <Col span={16} style={{ height: "100%", display: "flex", flexDirection: "column" }}>
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
            //: 右栏同样给横向滚动：标签 200 + 值 260 + 样式 110 + 排序 72 +
            //: 状态 80 + 备注(flex) + 操作 140 ⇒ 窄屏时不挤压、不折行。
            scroll={{ x: "max-content", y: "100%" }}
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
          if (!hasPermission("system:dict:edit")) { message.warning("没有编辑字典类型的权限"); return; }
      if (editing) await updateSystemDictType(editing.id, values);
          else {
        if (!hasPermission("system:dict:create")) { message.warning("没有新建字典类型的权限"); return; }
        await createSystemDictType(values);
      }
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
          if (!hasPermission("system:dict:edit")) { message.warning("没有编辑字典项的权限"); return; }
      if (editing) await updateSystemDictItem(editing.id, values);
          else if (typeId) {
        if (!hasPermission("system:dict:create")) { message.warning("没有新建字典项的权限"); return; }
        await createSystemDictItem({ type_id: typeId, ...values });
      }
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
