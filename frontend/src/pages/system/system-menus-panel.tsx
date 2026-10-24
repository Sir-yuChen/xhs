/*
 * 菜单管理页（v1.1.0 / 设计 55 §4 / 49 模块三；2026-10-25 用户口径重排）。
 *
 * ## 这一页管三件事，别混
 *
 * | 管什么 | 在哪管 | 影响 |
 * |---|---|---|
 * | **菜单启停**（`status`） | 本页「状态」列的开关 | 全站功能上线 / 下线，**不踢人** |
 * | **谁能看见**（授权） | 角色管理 → 分配菜单 | **踢人**（旧令牌失效） |
 * | **新增平台**（抖音 / 闲鱼） | 本页把 `status` 切 `normal` 即可 | 侧栏立刻出现 |
 *
 * ## 展示形态（2026-10-25 用户口径）
 *
 * * **antd 父子树表格**：dataSource 直接用后端的树（`children` 交给 Table
 *   原生展开），不再前端拍平 —— 拍平 + 展开字段残留曾是「行出现两次」的根因。
 * * **四级类型**：工作区（顶级 M）→ 目录（M）→ 页面（C）→ 按钮（F）。
 *   类型列与编辑表单都按四级表达；新建时**可选类型由上级菜单决定**
 *   （工作区下 → 目录/页面；目录下 → 页面；页面下 → 按钮；根级 → 工作区）。
 * * **编辑弹窗紧凑**（640px、短字段双列），列表卡片撑满内容区高度，
 *   底部不再留大片空白。
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
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Row,
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

/** 四级类型（用户口径）：M 且顶级 = 工作区，其余 M = 目录，C = 页面，F = 按钮。 */
function levelOf(node: Pick<SystemMenuNode, "menu_type" | "parent_id">): {
  text: string;
  color: string;
} {
  if (node.menu_type === "M") {
    return node.parent_id === 0
      ? { text: "工作区", color: "gold" }
      : { text: "目录", color: "purple" };
  }
  if (node.menu_type === "C") return { text: "页面", color: "blue" };
  return { text: "按钮", color: "default" };
}

function levelLabelOf(node: Pick<SystemMenuNode, "menu_type" | "parent_id">): string {
  return levelOf(node).text;
}

/** 树 → 上级菜单下拉的选项（带层级缩进；按钮不能当上级）。 */
function parentOptions(
  nodes: SystemMenuNode[],
  depth = 0,
  out: { value: number; label: string }[] = [],
) {
  for (const node of nodes) {
    if (node.menu_type !== "F") {
      out.push({ value: node.id, label: `${"　".repeat(depth)}${node.name}` });
    }
    if (node.children?.length) parentOptions(node.children, depth + 1, out);
  }
  return out;
}

/** 按上级节点算「允许新建的类型」。上级为空（顶级）只允许工作区。 */
function allowedTypes(parent: SystemMenuNode | null): { value: string; label: string }[] {
  if (!parent) return [{ value: "M", label: "工作区 M" }];
  if (parent.menu_type === "C") return [{ value: "F", label: "按钮 F" }];
  const isWorkspaceRoot = parent.menu_type === "M" && parent.parent_id === 0;
  return isWorkspaceRoot
    ? [
        { value: "M", label: "目录 M" },
        { value: "C", label: "页面 C" },
      ]
    : [{ value: "C", label: "页面 C" }];
}

function countNodes(nodes: SystemMenuNode[]): number {
  return nodes.reduce((sum, n) => sum + 1 + countNodes(n.children ?? []), 0);
}

function findNode(nodes: SystemMenuNode[], id: number): SystemMenuNode | null {
  for (const node of nodes) {
    if (node.id === id) return node;
    const hit = findNode(node.children ?? [], id);
    if (hit) return hit;
  }
  return null;
}

export function SystemMenusPanel() {
  const can = useCan();
  const { message, modal } = App.useApp();

  const [tree, setTree] = useState<SystemMenuNode[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [workspace, setWorkspace] = useState<string | undefined>(undefined);
  const [createOpen, setCreateOpen] = useState(false);
  const [createParent, setCreateParent] = useState<SystemMenuNode | null>(null);
  const [editTarget, setEditTarget] = useState<SystemMenuNode | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchSystemMenuTree(workspace);
      setTree(res.items);
      setTotal(res.total);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载菜单失败");
    } finally {
      setLoading(false);
    }
  }, [workspace, message]);

  useEffect(() => {
    void load();
  }, [load]);

  const wsSet = new Set<string>(
    (function collect(nodes: SystemMenuNode[]): string[] {
      return nodes.flatMap((n) => [n.workspace, ...collect(n.children ?? [])]);
    })(tree)
  );
  wsSet.delete("");
  const workspaces = Array.from(wsSet).sort();

  const columns: ColumnsType<SystemMenuNode> = [
    {
      title: "名称",
      dataIndex: "name",
      width: 260,
    },
    {
      title: "类型",
      key: "level",
      width: 90,
      render: (_: unknown, row) => {
        const meta = levelOf(row);
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
      width: 90,
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
      width: 220,
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
          {can("system:menu:create") && row.menu_type !== "F" && (
            <Button
              size="small"
              type="link"
              icon={<PlusOutlined />}
              onClick={() => {
                setCreateParent(row);
                setCreateOpen(true);
              }}
            >
              子菜单
            </Button>
          )}
          {can("system:menu:delete") && (
            <Tooltip
              title={
                (row.children?.length ?? 0) > 0
                  ? `还有 ${row.children?.length ?? 0} 个子菜单，不能删`
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
      title={`菜单与权限码（${total} 行）`}
      style={{ height: "100%" }}
      styles={{ body: { height: "calc(100% - 40px)", overflow: "auto" } }}
      extra={
        <Space>
          <Select
            placeholder="全部工作区"
            allowClear
            style={{ width: 160 }}
            value={workspace}
            onChange={(v) => setWorkspace(v || undefined)}
            options={workspaces.map((w) => ({ value: w, label: w }))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          {can("system:menu:create") && (
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                setCreateParent(null);
                setCreateOpen(true);
              }}
            >
              新建菜单
            </Button>
          )}
        </Space>
      }
    >
      {/* 父子树表格：dataSource 即后端树（children 交给 Table 原生展开 ——
          不再前端拍平；拍平 + children 残留曾是「行出现两次」的根因）。 */}
      <Table<SystemMenuNode>
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={tree}
        pagination={false}
        scroll={{ x: 1300 }}
      />

      <MenuFormModal
        open={createOpen}
        parent={createParent}
        tree={tree}
        onClose={() => setCreateOpen(false)}
        onSaved={() => {
          setCreateOpen(false);
          void load();
        }}
      />
      <MenuFormModal
        open={Boolean(editTarget)}
        editing={editTarget}
        tree={tree}
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
  parent,
  tree,
  onClose,
  onSaved,
}: {
  open: boolean;
  editing?: SystemMenuNode | null;
  /** 新建时预选的上级（行级「子菜单」入口带入；顶部按钮 = null = 顶级）。 */
  parent?: SystemMenuNode | null;
  tree: SystemMenuNode[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const isEdit = Boolean(editing);

  const parentNode = parent ?? (editing ? findNode(tree, editing.parent_id) : null);
  const typeOptions = isEdit
    ? [
        {
          value: editing?.menu_type ?? "C",
          label: levelLabelOf(editing ?? { menu_type: "C", parent_id: 0 }),
        },
      ]
    : allowedTypes(editing ? null : parentNode);

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
      form.setFieldsValue({
        parent_id: parent?.id ?? 0,
        menu_type: allowedTypes(parent ?? null)[0]?.value ?? "C",
        is_visible: true,
        status: "normal",
        sort_order: 0,
        workspace: parent?.workspace ?? "",
      });
    }
  }, [open, editing, parent, form]);

  return (
    <Modal
      centered
      open={open}
      title={
        isEdit
          ? `编辑菜单 · ${editing?.name ?? ""}`
          : parent
            ? `新建子菜单 · ${parent.name}`
            : "新建菜单（工作区）"
      }
      onCancel={onClose}
      width={640}
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
      <Form form={form} layout="vertical" style={{ maxHeight: "60vh", overflowY: "auto", paddingRight: 4 }}>
        <Form.Item name="parent_id" label="上级菜单" hidden>
          <InputNumber />
        </Form.Item>
        <Text type="secondary" style={{ fontSize: 12 }}>
          上级菜单：{parentNode ? `${parentNode.name}（${levelLabelOf(parentNode)}）` : "（顶级 = 新工作区）"}
        </Text>
        <Form.Item name="name" label="名称" rules={[{ required: true, max: 64 }]} style={{ marginTop: 12 }}>
          <Input />
        </Form.Item>
        <Form.Item name="menu_type" label="类型" rules={[{ required: true }]}>
          <Radio.Group disabled={isEdit} options={typeOptions} optionType="button" buttonStyle="solid" />
        </Form.Item>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item
              name="perms"
              label="权限码"
              tooltip={{
                title:
                  "必须已在 core/perms_registry.py 登记过，未登记的码会被启动守卫拒绝；" +
                  "页面（C）必须带 <域>:<资源>:view 形态的查看码，按钮（F）挂操作码。悬停标签可随时查看。",
              }}
            >
              <Input placeholder="例如 xhs:item:edit" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="path" label="路由（页面必填）">
              <Input placeholder="例如 /system/users" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="component" label="组件">
              <Input />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="icon" label="图标（antd 组件名）">
              <Input placeholder="例如 UserOutlined" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="workspace" label="工作区">
              <Input placeholder="xhs / ai / system" />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item name="sort_order" label="排序">
              <InputNumber style={{ width: "100%" }} />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item name="is_visible" label="在侧栏显示" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="status" label="状态">
          <Select
            style={{ width: 160 }}
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
