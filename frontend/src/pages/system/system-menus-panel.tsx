/*
 * 菜单管理页（v1.1.0 / 设计 55 §4 / 49 模块三）。
 *
 * ## 这一页管三件事，别混
 *
 * | 管什么 | 在哪管 | 影响 |
 * |---|---|---|
 * | **菜单启停**（`status`） | 本页「状态」列的开关 | 全站功能上线 / 下线，**不踢人** |
 * | **谁能看见**（授权） | 角色管理 → 分配菜单 | **踢人**（旧令牌失效） |
 * | **新增平台**（抖音 / 闲鱼） | 本页把 `status` 切 `normal` 即可 | 侧栏立刻出现 |
 *
 * 用户口径（设计 46 §1.5）：「抖音 / 闲鱼已写进种子但 `status=disabled`，
 * 接入平台后启动菜单即可」—— 就是这一列的开关。
 *
 * ## 为什么启停不踢人
 *
 * 菜单启停是**全站功能开关**，不是「谁有权限」。平台上线这种低频操作
 * 让所有人掉线是不可接受的。所以只清权限缓存，`token_version` 不动。
 *
 * ## 删除保护
 *
 * 有子菜单 / 有授权引用 ⇒ 后端 409。页面上把原因显示出来，而不是「点了报错」。
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
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";

import { useCan } from "../../hooks/use-can";
import {
  createSystemMenu,
  deleteSystemMenu,
  fetchSystemMenuTree,
  updateSystemMenu,
  type SystemMenuNode,
} from "../../lib/api";

const { Text } = Typography;

const TYPE_META: Record<string, { text: string; color: string }> = {
  M: { text: "目录", color: "purple" },
  C: { text: "页面", color: "blue" },
  F: { text: "按钮", color: "default" },
};

/** 扁平化（表格展示用；树形缩进靠 `depth`）。 */
interface FlatRow extends Omit<SystemMenuNode, "child_count" | "children"> {
  /** 子节点数（`flatten` 里已归一成必填，故此处覆盖父类型的可选声明）。 */
  child_count: number;
  depth: number;
}

function flatten(nodes: SystemMenuNode[], depth = 0, out: FlatRow[] = []): FlatRow[] {
  for (const node of nodes) {
    //: `child_count` 后端有给（`_serialize(child_count=...)`），但树接口里
    //: 父节点的 children 已经是完整子树 ⇒ 缺值时用 children.length 兜底，
    //: 避免「有没有子」这个判断在某条路径上恒为 0。
    //:
    //: ⚠️ **必须剥掉 children**（2026-10-25 实测）：`...node` 展开会把 children
    //    原样带进扁平行 —— antd Table 对「带 children 的行」按树渲染一遍、
    //    递归追加的扁平行又渲染一遍 ⇒ 监控中心/自动运营等全部出现两次
    //    （菜单管理页树形混乱的直接根因）。
    const { children: _stripped, ...rest } = node;
    out.push({
      ...rest,
      child_count: node.child_count ?? node.children?.length ?? 0,
      depth,
    });
    if (node.children?.length) flatten(node.children, depth + 1, out);
  }
  return out;
}

export function SystemMenusPanel() {
  const can = useCan();
  const { message, modal } = App.useApp();

  const [tree, setTree] = useState<SystemMenuNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [workspace, setWorkspace] = useState<string | undefined>(undefined);
  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<SystemMenuNode | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchSystemMenuTree(workspace);
      setTree(res.items);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载菜单失败");
    } finally {
      setLoading(false);
    }
  }, [workspace, message]);

  useEffect(() => {
    void load();
  }, [load]);

  const rows = flatten(tree);
  const workspaces = Array.from(
    new Set(rows.map((r) => r.workspace).filter(Boolean))
  ).sort();

  const columns: ColumnsType<FlatRow> = [
    {
      title: "名称",
      dataIndex: "name",
      width: 240,
      render: (v: string, row) => (
        //: 缩进表达层级：M / C / F 三级用 20px 递进，不额外画树线（表格里树线很吵）
        <span style={{ paddingLeft: row.depth * 20 }}>{v}</span>
      ),
    },
    {
      title: "类型",
      dataIndex: "menu_type",
      width: 80,
      render: (v: string) => {
        const meta = TYPE_META[v] ?? { text: v, color: "default" };
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: "权限码",
      dataIndex: "perms",
      width: 220,
      render: (v: string) =>
        v ? <Text code style={{ fontSize: 12 }}>{v}</Text> : <Text type="secondary">-</Text>,
    },
    {
      title: "路由",
      dataIndex: "path",
      width: 240,
      render: (v: string) =>
        v ? <Text style={{ fontSize: 12 }}>{v}</Text> : <Text type="secondary">-</Text>,
    },
    {
      title: "工作区",
      dataIndex: "workspace",
      width: 100,
      render: (v: string) => v || "-",
    },
    {
      title: "排序",
      dataIndex: "sort_order",
      width: 70,
      align: "right",
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (v: string, row) => {
        //: ⚠️ 启停 ⇒ **只清缓存不踢人**（后端 `update_menu` 的口径）。
        //: 抖音 / 闲鱼接入时把这里打开即可，不必改代码。
        const on = v === "normal";
        return can("system:menu:edit") ? (
          <Tooltip title="启停是功能开关，不影响谁有权限（那些人不会掉线）">
            <Switch
              size="small"
              checked={on}
              checkedChildren="启用"
              unCheckedChildren="停用"
              onChange={async (next) => {
                try {
                  await updateSystemMenu(row.id, { status: next ? "normal" : "disabled" });
                  message.success(next ? "已启用" : "已停用");
                  void load();
                } catch (error) {
                  message.error(error instanceof Error ? error.message : "操作失败");
                }
              }}
            />
          </Tooltip>
        ) : on ? (
          <Tag color="success">启用</Tag>
        ) : (
          <Tag>停用</Tag>
        );
      },
    },
    {
      title: "操作",
      key: "actions",
      width: 180,
      fixed: "right",
      render: (_: unknown, row) => (
        <Space size={0}>
          {can("system:menu:edit") && (
            <Button
              size="small"
              type="link"
              icon={<EditOutlined />}
              onClick={() => setEditTarget({ ...row, children: [] })}
            >
              编辑
            </Button>
          )}
          {can("system:menu:delete") && (
            <Tooltip
              title={
                row.child_count > 0
                  ? `还有 ${row.child_count} 个子菜单，不能删`
                  : "有角色授权引用时也不能删"
              }
            >
              <Button
                size="small"
                type="link"
                danger
                icon={<DeleteOutlined />}
                onClick={() => {
                  modal.confirm({
                    title: `删除菜单「${row.name}」？`,
                    content: "若有子菜单或角色授权引用，后端会拒绝并说明原因。",
                    okButtonProps: { danger: true },
                    onOk: async () => {
                      try {
                        await deleteSystemMenu(row.id);
                        message.success("已删除");
                        void load();
                      } catch (error) {
                        //: 把后端的 409 原因原样显示（MENU_HAS_CHILDREN / MENU_IN_USE）
                        const detail = (error as { response?: { data?: { detail?: { message?: string } } } })
                          .response?.data?.detail;
                        message.error(detail?.message ?? "删除失败");
                      }
                    },
                  });
                }}
              >
                删除
              </Button>
            </Tooltip>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Card
      size="small"
      title={`菜单与权限码（${rows.length} 行）`}
      extra={
        <Space>
          <Select
            placeholder="全部工作区"
            allowClear
            style={{ width: 160 }}
            value={workspace}
            onChange={setWorkspace}
            options={workspaces.map((w) => ({ value: w, label: w }))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          {can("system:menu:create") && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              新建菜单
            </Button>
          )}
        </Space>
      }
    >
      <Table<FlatRow>
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={false}
        scroll={{ x: 1300, y: 560 }}
      />

      <MenuFormModal
        open={createOpen}
        allRows={rows}
        onClose={() => setCreateOpen(false)}
        onSaved={() => {
          setCreateOpen(false);
          void load();
        }}
      />
      <MenuFormModal
        open={Boolean(editTarget)}
        allRows={rows}
        editing={editTarget}
        onClose={() => setEditTarget(null)}
        onSaved={() => {
          setEditTarget(null);
          void load();
        }}
      />
    </Card>
  );
}

function MenuFormModal({
  open,
  editing,
  allRows,
  onClose,
  onSaved,
}: {
  open: boolean;
  editing?: SystemMenuNode | null;
  allRows: FlatRow[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const isEdit = Boolean(editing);

  useEffect(() => {
    if (!open) return;
    if (editing) {
      form.setFieldsValue({
        parent_id: editing.parent_id,
        name: editing.name,
        menu_type: editing.menu_type,
        perms: editing.perms,
        path: editing.path,
        component: editing.component,
        icon: editing.icon,
        workspace: editing.workspace,
        sort_order: editing.sort_order,
        is_visible: editing.is_visible,
        status: editing.status,
      });
    } else {
      form.resetFields();
      form.setFieldsValue({ parent_id: 0, menu_type: "C", is_visible: true, status: "normal", sort_order: 0 });
    }
  }, [open, editing, form]);

  return (
    <Modal
      centered
      open={open}
      title={isEdit ? `编辑菜单 · ${editing?.name ?? ""}` : "新建菜单"}
      onCancel={onClose}
      width="42vw"
      onOk={async () => {
        try {
          const values = await form.validateFields();
          if (isEdit && editing) {
            //: 类型不可改（M→C 会让已有的授权行语义错位）
            const { menu_type: _ignored, ...rest } = values;
            await updateSystemMenu(editing.id, rest);
          } else {
            await createSystemMenu(values);
          }
          message.success("已保存");
          onSaved();
        } catch (error) {
          if ((error as { errorFields?: unknown }).errorFields) return;
          const detail = (error as { response?: { data?: { detail?: { message?: string } } } })
            .response?.data?.detail;
          message.error(detail?.message ?? (error instanceof Error ? error.message : "保存失败"));
        }
      }}
    >
      <Form form={form} layout="vertical">
        <Form.Item name="parent_id" label="上级菜单">
          <Select
            options={[
              { value: 0, label: "（顶级）" },
              ...allRows
                .filter((r) => r.menu_type !== "F" && r.id !== editing?.id)
                .map((r) => ({
                  value: r.id,
                  label: `${"　".repeat(r.depth)}${r.name}`,
                })),
            ]}
          />
        </Form.Item>
        <Form.Item name="name" label="名称" rules={[{ required: true, max: 64 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="menu_type" label="类型" rules={[{ required: true }]}>
          <Radio.Group disabled={isEdit}>
            <Radio.Button value="M">目录 M</Radio.Button>
            <Radio.Button value="C">页面 C</Radio.Button>
            <Radio.Button value="F">按钮 F</Radio.Button>
          </Radio.Group>
        </Form.Item>
        <Form.Item
          name="perms"
          label="权限码"
          extra="必须已在 core/perms_registry.py 登记；页面（C）必须带 <域>:<资源>:view"
        >
          <Input placeholder="例如 xhs:item:edit" />
        </Form.Item>
        <Form.Item name="path" label="路由（页面必填）">
          <Input placeholder="例如 /system/users" />
        </Form.Item>
        <Form.Item name="component" label="组件">
          <Input />
        </Form.Item>
        <Form.Item name="icon" label="图标（antd 组件名）">
          <Input placeholder="例如 UserOutlined" />
        </Form.Item>
        <Form.Item name="workspace" label="工作区">
          <Input placeholder="xhs / ai / system" />
        </Form.Item>
        <Form.Item name="sort_order" label="排序">
          <InputNumber style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="is_visible" label="在侧栏显示" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select
            options={[
              { value: "normal", label: "启用" },
              { value: "disabled", label: "停用" },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
