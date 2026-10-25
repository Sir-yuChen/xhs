/*
 * 角色管理页（v1.1.0 / 设计 55 §3 / 49 模块二）。
 *
 * ## 核心交互：两棵树
 *
 * 一条主表（角色）+ 两个抽屉：
 *  * **分配菜单** —— antd `Tree` + `checkable`，勾选即授权。写操作按钮
 *    「保存」会把**整个勾选集合**提交（`PUT /roles/{id}/menus` 是整体替换语义）。
 *  * **管理用户** —— 列出持有该角色的用户（`GET /roles/{id}/users`）。
 *
 * ## 内置角色（`is_system`）
 *
 * 后端拦截三条：不许删、不许停用、不许改 `code`。页面对应的按钮**不渲染**，
 * 并且把原因写进 tooltip —— 不给用户「点了才知道不行」。
 * 允许改的只有显示名 / 描述 / 排序（那是文案，不是语义）。
 *
 * ## 保存后会踢人
 *
 * 授权变更 ⇒ 该角色的所有用户 `token_version+1` ⇒ **他们会被踢下线**。
 * 保存成功后必须明确提示这一点，否则管理员会以为「保存没生效」
 * （用户还是看不到新菜单，因为要重新登录）。
 */

import {
  ApartmentOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Card,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Table,
  Tag,
  Tooltip,
  Tree,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { hasPermission } from "../../hooks/use-permission";
import type { DataNode } from "antd/es/tree";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useCan } from "../../hooks/use-can";
import { RowActions } from "../../components/common/row-actions";
import {
  createSystemRole,
  deleteSystemRole,
  fetchRoleMenus,
  fetchRoleUsers,
  fetchSystemMenuTree,
  fetchSystemRoles,
  setRoleMenus,
  updateSystemRole,
  type SystemMenuNode,
  type SystemRole,
} from "../../lib/api";

const { Text } = Typography;

export function SystemRolesPanel() {
  const can = useCan();
  const { message, modal } = App.useApp();

  const [rows, setRows] = useState<SystemRole[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<SystemRole | null>(null);
  const [menusTarget, setMenusTarget] = useState<SystemRole | null>(null);
  const [usersTarget, setUsersTarget] = useState<SystemRole | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchSystemRoles();
      setRows(res.items);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载角色失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: ColumnsType<SystemRole> = [
    {
      title: "角色名",
      dataIndex: "name",
      width: 160,
      render: (v: string, row) => (
        <Space size={6}>
          {v}
          {row.is_system && (
            <Tooltip title="内置角色：不许删除 / 停用 / 改编码">
              <Tag color="gold">内置</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    { title: "编码", dataIndex: "code", width: 140 },
    { title: "描述", dataIndex: "description", render: (v: string) => v || "-" },
    {
      title: "用户数",
      dataIndex: "user_count",
      width: 90,
      align: "right",
    },
    {
      title: "已授权菜单",
      dataIndex: "menu_count",
      width: 110,
      align: "right",
      //: ⚠️ 这个数**只数模板行**（后端 `_counts` 的口径）。曾经的 522 = 174 × 3
      //: 是把「模板 + 每个持有者的用户行」都算进去了，界面上会被读成「授了 522 个菜单」。
      render: (v: number) => <Tooltip title="该角色默认被授权的菜单数">{v}</Tooltip>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (v: string) =>
        v === "normal" ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      fixed: "right",
      //: 2026-10-25 用户口径「所有操作列均不允许溢出页面」：
      //: 原 320px 手写 4 个带图标按钮 ⇒ 窄屏被裁。改用统一容器：
      //: 常用 3 个直显、其余进「更多」，去掉图标省宽度（图标在 240px 里挤不下）。
      width: 240,
      render: (_: unknown, row) => (
        <RowActions
          width={240}
          maxInline={3}
          items={[
            {
              label: "分配菜单",
              permission: "system:role:assignMenu",
              onClick: () => setMenusTarget(row),
            },
            {
              label: "查看用户",
              onClick: () => setUsersTarget(row),
            },
            {
              label: "编辑",
              permission: "system:role:edit",
              onClick: () => setEditTarget(row),
            },
            //: 系统内置角色不可删 —— 业务规则而非权限：直接不给这一项
            ...(row.is_system
              ? []
              : [
                  {
                    label: "删除",
                    permission: "system:role:delete",
                    danger: true,
                    onClick: () => {
                      modal.confirm({
                        title: `删除角色「${row.name}」？`,
                        content:
                          row.user_count > 0
                            ? `还有 ${row.user_count} 个用户使用该角色，需先调整他们的角色。`
                            : "删除后该角色的所有授权一起消失。",
                        okButtonProps: { danger: true, disabled: row.user_count > 0 },
                        onOk: async () => {
                          await deleteSystemRole(row.id);
                          message.success("已删除");
                          void load();
                        },
                      });
                    },
                  },
                ]),
          ]}
        />
      ),
    },
  ];

  // 设计 63 §6：三区布局（同用户管理）—— 表格体内部滚动，卡底不再留白/溢出。
  return (
    <div style={{ height: "calc(100vh - 87px)", display: "flex", flexDirection: "column" }}>
    <Card
      size="small"
      style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
      styles={{ body: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" } }}
      title={`角色（共 ${rows.length}）`}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          {can("system:role:create") && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              新建角色
            </Button>
          )}
        </Space>
      }
    >
      <div className="fill-table">
      <Table<SystemRole>
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={false}
        scroll={{ x: 1100, y: "100%" }}
      />
      </div>

      <RoleCreateModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSaved={() => {
          setCreateOpen(false);
          void load();
        }}
      />
      <RoleEditModal
        role={editTarget}
        onClose={() => setEditTarget(null)}
        onSaved={() => {
          setEditTarget(null);
          void load();
        }}
      />
      <AssignMenusDrawer
        role={menusTarget}
        onClose={() => setMenusTarget(null)}
        onSaved={() => {
          void load();
        }}
      />
      <RoleUsersDrawer role={usersTarget} onClose={() => setUsersTarget(null)} />
    </Card>
    </div>
  );
}

function RoleCreateModal({
  open,
  onClose,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();

  return (
    <Modal
      centered
      open={open}
      title="新建角色"
      onCancel={onClose}
      onOk={async () => {
        try {
          const values = await form.validateFields();
          if (!hasPermission("system:role:create")) {
            message.warning("没有新建角色的权限");
            return;
          }
          await createSystemRole(values);
          message.success("已创建");
          form.resetFields();
          onSaved();
        } catch (error) {
          if ((error as { errorFields?: unknown }).errorFields) return;
          message.error(error instanceof Error ? error.message : "创建失败");
        }
      }}
    >
      <Form form={form} layout="vertical">
        <Form.Item name="name" label="角色名" rules={[{ required: true, min: 2, max: 64 }]}>
          <Input />
        </Form.Item>
        <Form.Item
          name="code"
          label="编码"
          rules={[
            { required: true },
            { pattern: /^[a-z][a-z0-9_]{1,31}$/, message: "小写字母开头，2–32 位" },
          ]}
          extra="编码一旦创建不可修改（代码按它引用）"
        >
          <Input placeholder="例如 ops_admin" />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input.TextArea rows={2} />
        </Form.Item>
        <Form.Item name="sort_order" label="排序" initialValue={0}>
          <InputNumber style={{ width: "100%" }} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function RoleEditModal({
  role,
  onClose,
  onSaved,
}: {
  role: SystemRole | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();

  useEffect(() => {
    if (!role) return;
    form.setFieldsValue({
      name: role.name,
      description: role.description,
      status: role.status,
      sort_order: role.sort_order,
    });
  }, [role, form]);

  return (
    <Modal
      centered
      open={Boolean(role)}
      title={`编辑角色 · ${role?.name ?? ""}`}
      onCancel={onClose}
      onOk={async () => {
        if (!role) return;
        try {
          const values = await form.validateFields();
          if (!hasPermission("system:role:edit")) {
            message.warning("没有编辑角色的权限");
            return;
          }
          await updateSystemRole(role.id, values);
          message.success("已保存");
          onSaved();
        } catch (error) {
          if ((error as { errorFields?: unknown }).errorFields) return;
          message.error(error instanceof Error ? error.message : "保存失败");
        }
      }}
    >
      <Form form={form} layout="vertical">
        <Form.Item label="编码">
          <Input value={role?.code} disabled />
        </Form.Item>
        <Form.Item name="name" label="角色名" rules={[{ required: true, min: 2, max: 64 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input.TextArea rows={2} />
        </Form.Item>
        <Form.Item
          name="status"
          label="状态"
          extra={role?.is_system ? "内置角色不允许停用（会把一整类用户锁在门外）" : undefined}
        >
          <Input
            disabled={Boolean(role?.is_system)}
            onChange={(e) => form.setFieldValue("status", e.target.value)}
            value={form.getFieldValue("status")}
          />
        </Form.Item>
        <Form.Item name="sort_order" label="排序">
          <InputNumber style={{ width: "100%" }} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

/** 后端菜单树 → antd Tree 节点（只展示 M/C，F 行（按钮权限）也展示但默认不展开）。 */
function toTreeData(nodes: SystemMenuNode[]): DataNode[] {
  return nodes.map((node) => ({
    key: node.id,
    title: (
      <Space size={6}>
        <span>{node.name}</span>
        {node.perms && (
          <Text type="secondary" style={{ fontSize: 11 }}>
            {node.perms}
          </Text>
        )}
        {node.status !== "normal" && <Tag color="default">停用</Tag>}
      </Space>
    ),
    children: node.children?.length ? toTreeData(node.children) : undefined,
  }));
}

/** 收集所有节点的 key（「全选」与「展开全部」用）。 */
function allKeys(nodes: SystemMenuNode[]): number[] {
  const out: number[] = [];
  const walk = (list: SystemMenuNode[]) => {
    for (const n of list) {
      out.push(n.id);
      if (n.children?.length) walk(n.children);
    }
  };
  walk(nodes);
  return out;
}

function AssignMenusDrawer({
  role,
  onClose,
  onSaved,
}: {
  role: SystemRole | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [tree, setTree] = useState<SystemMenuNode[]>([]);
  const [checked, setChecked] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [expanded, setExpanded] = useState<number[]>([]);

  useEffect(() => {
    if (!role) return;
    setLoading(true);
    Promise.all([fetchSystemMenuTree(), fetchRoleMenus(role.id)])
      .then(([treeRes, menuRes]) => {
        setTree(treeRes.items);
        setChecked(menuRes.menu_ids);
        setExpanded(treeRes.items.map((n) => n.id));
      })
      .catch((error) => message.error(error instanceof Error ? error.message : "加载失败"))
      .finally(() => setLoading(false));
  }, [role, message]);

  const treeData = useMemo(() => toTreeData(tree), [tree]);
  const keys = useMemo(() => allKeys(tree), [tree]);

  return (
    <Drawer
      open={Boolean(role)}
      width="46vw"
      title={`分配菜单 · ${role?.name ?? ""}`}
      onClose={onClose}
      extra={
        <Space>
          <Button onClick={() => setExpanded(expanded.length ? [] : keys.map(Number))}>
            {expanded.length ? "收起" : "展开"}
          </Button>
          <Button onClick={() => setChecked([])}>清空</Button>
          <Button
            type="primary"
            loading={saving}
            onClick={async () => {
              if (!role) return;
              setSaving(true);
              try {
                const res = await setRoleMenus(role.id, checked);
                //: ⚠️ 必须把「踢了多少人」说出来 —— 否则管理员会以为「保存没生效」
                //: （用户仍看不到新菜单，因为要重新登录）。
                message.success(
                  res.kicked_users > 0
                    ? `已保存（授权 ${res.granted} 项）。该角色的 ${res.kicked_users} 个用户需重新登录才能看到新菜单。`
                    : `已保存（授权 ${res.granted} 项）`
                );
                onSaved();
                onClose();
              } catch (error) {
                message.error(error instanceof Error ? error.message : "保存失败");
              } finally {
                setSaving(false);
              }
            }}
          >
            保存
          </Button>
        </Space>
      }
    >
      {role?.code === "admin" && (
        <Text type="warning" style={{ display: "block", marginBottom: 12 }}>
          超级管理员的权限由代码派生（拥有全部权限），改这里的勾选不会影响他的实际权限。
        </Text>
      )}
      <Tree
        checkable
        selectable={false}
        treeData={treeData}
        checkedKeys={checked}
        onCheck={(keys) => setChecked(keys as number[])}
        expandedKeys={expanded}
        onExpand={(keys) => setExpanded(keys as number[])}
        checkStrictly={false}
      />
    </Drawer>
  );
}

function RoleUsersDrawer({
  role,
  onClose,
}: {
  role: SystemRole | null;
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const [items, setItems] = useState<
    { id: number; username: string; real_name: string; status: string }[]
  >([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!role) return;
    setLoading(true);
    fetchRoleUsers(role.id)
      .then((res) => setItems(res.items))
      .catch((error) => message.error(error instanceof Error ? error.message : "加载失败"))
      .finally(() => setLoading(false));
  }, [role, message]);

  return (
    <Drawer
      open={Boolean(role)}
      width="36vw"
      title={`持有「${role?.name ?? ""}」的用户（${items.length}）`}
      onClose={onClose}
    >
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={items}
        pagination={false}
        columns={[
          { title: "ID", dataIndex: "id", width: 70 },
          { title: "用户名", dataIndex: "username" },
          { title: "姓名", dataIndex: "real_name", render: (v: string) => v || "-" },
          {
            title: "状态",
            dataIndex: "status",
            width: 90,
            render: (v: string) =>
              v === "normal" ? <Tag color="success">正常</Tag> : <Tag>{v}</Tag>,
          },
        ]}
      />
    </Drawer>
  );
}
