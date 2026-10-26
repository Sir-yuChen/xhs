/*
 * 用户管理页（v1.1.0 / 设计 55 §2 / 49 模块一）。
 *
 * ## 页面结构判据
 *
 * 一条主表 + 5 个弹窗（新建 / 编辑 / 重置密码 / 详情 / 审批）。
 * 交互口径全部对齐 dataPanel（用户口径「与 dataPanel 一致」）：
 *
 * | 交互 | 口径 |
 * |---|---|
 * | 编辑弹窗 | `username` / `role` **disabled**（改名 / 换角色会踢人，走后门更清晰） |
 * | `password` | **只在新建态**出现（编辑走「重置密码」独立入口） |
 * | 状态列 | `normal` / `pending`（待审）/ `disabled`（停用），颜色由状态派生 |
 * | 在线列 | `online` 是**近似值**（5 分钟内有无请求），tooltip 必须说明 |
 * | 删除 | 二次确认，提示「可恢复」（软删） |
 *
 * ## 权限判据
 *
 * 每个操作按钮都用 `can()` 判：无权限**不渲染**（写操作）或 `disabled`（读操作）。
 * 后端 `require_perm` 是真正的拦截，这里只是体验。
 *
 * ## 手机号脱敏
 *
 * 列表接口后端已脱敏（`138****2857`），详情接口给全。页面**不再自己脱敏** ——
 * 两处各做一次必然不一致（DEF-044）。
 */

import {
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  KeyOutlined,
  LogoutOutlined,
  PlusOutlined,
  ReloadOutlined,
  UserOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useCan } from "../../hooks/use-can";
import { RowActions } from "../../components/common/row-actions";
import {
  approveSystemUser,
  batchApproveUsers,
  batchDeleteUsers,
  batchForceLogoutUsers,
  batchResetUserPassword,
  createSystemUser,
  deleteSystemUser,
  fetchSystemRoles,
  fetchSystemUsers,
  forceLogoutSystemUser,
  resetSystemUserPassword,
  updateSystemUser,
  type SystemRole,
  type SystemUser,
} from "../../lib/api";
import { dictLabel, dictListClass } from "../../lib/system-dicts";

const { Text } = Typography;

const STATUS_META: Record<string, { text: string; color: string }> = {
  normal: { text: "正常", color: "success" },
  pending: { text: "待审核", color: "warning" },
  disabled: { text: "已停用", color: "default" },
};

function userStatusTexts(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(STATUS_META).map(([key, item]) => [key, item.text])
  );
}

function userStatusColors(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(STATUS_META).map(([key, item]) => [key, item.color])
  );
}

export function SystemUsersPanel() {
  const can = useCan();
  const { message, modal } = App.useApp();

  const [rows, setRows] = useState<SystemUser[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const [roles, setRoles] = useState<SystemRole[]>([]);

  //: 筛选条件（提交式：只有点「查询」或翻页才生效，输入框变化不触发请求）
  const [filter, setFilter] = useState<{ username?: string; status?: string; role?: string }>({});
  const [draft, setDraft] = useState<{ username?: string; status?: string; role?: string }>({});

  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<SystemUser | null>(null);
  const [pwdTarget, setPwdTarget] = useState<SystemUser | null>(null);
  const [detail, setDetail] = useState<SystemUser | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchSystemUsers({
        page,
        page_size: pageSize,
        ...filter,
      });
      setRows(res.items);
      setTotal(res.total);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载用户失败");
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, filter, message]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    fetchSystemRoles()
      .then((res) => setRoles(res.items))
      .catch(() => setRoles([]));
  }, []);

  //: 2026-10-26 用户口径「超级管理员只能有一个，系统种子数据 admin 账号」：
  //: 角色下拉**按场景给不同选项** ——
  //:  * 建号：不提供 admin（后端也会拒）；
  //:  * 编辑别人：不提供 admin（不能把人提升成第二个超管）；
  //:  * 编辑种子超管本身：提供 admin（它是唯一合法的持有者）。
  //: 后端是最后一道闸，前端这里是为了让用户根本选不到无效项。
  const roleOptions = useMemo(
    () => roles.map((r) => ({ value: r.code, label: `${r.name}（${r.code}）` })),
    [roles]
  );
  const assignableRoleOptions = useMemo(
    () => roleOptions.filter((o) => o.value !== "admin"),
    [roleOptions]
  );
  const roleOptionsFor = (username?: string) =>
    username === "admin" ? roleOptions : assignableRoleOptions;

  const handleDelete = (row: SystemUser) => {
    modal.confirm({
      title: `删除用户「${row.username}」？`,
      content:
        "这是软删除：账号立即无法登录，但业务数据保留，之后可以由管理员恢复。",
      okText: "删除",
      okButtonProps: { danger: true },
      onOk: async () => {
        await deleteSystemUser(row.id);
        message.success("已删除（可恢复）");
        void load();
      },
    });
  };

  const handleForceLogout = (row: SystemUser) => {
    modal.confirm({
      title: `强制「${row.username}」下线？`,
      content: "他的所有登录令牌会立即失效，需要重新登录。",
      onOk: async () => {
        await forceLogoutSystemUser(row.id);
        message.success("已强制下线");
        void load();
      },
    });
  };

  //: 批量操作（2026-10-25 用户口径「新增用户管理批量相关操作」）：
  //: 勾选后才出现操作条 —— 未勾选时不占任何布局，也不显示禁用按钮
  //: （用户口径「没有对应权限则不展示」，空选择同理）。
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [batchBusy, setBatchBusy] = useState<"" | "delete" | "reset" | "kick" | "approve">("");
  const [batchPwdOpen, setBatchPwdOpen] = useState(false);
  const [batchPwd, setBatchPwd] = useState("");

  /** 批量操作统一出口：调后端 → 用回执报账（**不自己拼「成功 N 个」**）。 */
  const runBatch = useCallback(
    async (kind: "delete" | "reset" | "kick" | "approve", pwd?: string) => {
      if (!selectedIds.length) return;
      if (kind === "delete") {
        const sure = await new Promise<boolean>((resolve) => {
          modal.confirm({
            title: `删除 ${selectedIds.length} 个用户？`,
            content: "软删除：业务数据保留，旧登录令牌立即失效。",
            okButtonProps: { danger: true },
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!sure) return;
      }
      setBatchBusy(kind);
      try {
        const res =
          kind === "delete"
            ? await batchDeleteUsers(selectedIds)
            : kind === "reset"
              ? await batchResetUserPassword(selectedIds, pwd ?? "", true)
              : kind === "kick"
                ? await batchForceLogoutUsers(selectedIds)
                : await batchApproveUsers(selectedIds);
        //: 回执四态逐条报，有跳过/失败就说清楚（缺值口径：不假装全成功）
        const parts = [`成功 ${res.ok} 个`];
        if (res.skipped) parts.push(`跳过 ${res.skipped} 个`);
        if (res.failed) parts.push(`失败 ${res.failed} 个`);
        if (res.not_found) parts.push(`不存在 ${res.not_found} 个`);
        const text = parts.join("，");
        if (res.failed || res.skipped) {
          const detail = res.results
            .filter((r) => r.status !== "ok")
            .slice(0, 5)
            .map((r) => `#${r.user_id} ${r.reason || r.status}`)
            .join("；");
          message.warning(`${text}。${detail}`);
        } else {
          message.success(text);
        }
        setSelectedIds([]);
        void load();
      } catch {
        message.error("批量操作失败");
      } finally {
        setBatchBusy("");
      }
    },
    [selectedIds, load]
  );

  const columns: ColumnsType<SystemUser> = [
    {
      title: "用户名",
      dataIndex: "username",
      fixed: "left",
      width: 180,
      render: (_: unknown, row) => (
        <Space size={6}>
          <UserOutlined />
          <a onClick={() => setDetail(row)}>{row.username}</a>
          {row.must_change_password && (
            <Tooltip title="仍在用初始密码，下次登录会被要求改密">
              <Tag color="orange">默认口令</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    { title: "姓名", dataIndex: "real_name", width: 120, render: (v: string) => v || "-" },
    { title: "邮箱", dataIndex: "email", width: 180, render: (v: string) => v || "-" },
    {
      title: "手机",
      dataIndex: "phone",
      width: 130,
      //: 后端列表已脱敏；这里直接显 —— 页面不再自己脱一次（DEF-044）
      render: (v: string) => v || "-",
    },
    {
      title: "角色",
      dataIndex: "role",
      width: 130,
      render: (v: string, row) => (
        <Tooltip title={v}>
          <Tag color={v === "admin" ? "red" : "blue"}>{row.role_name || v}</Tag>
        </Tooltip>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (v: string) => {
        // 设计 63：用户状态走字典 `sys_user_status`（STATUS_META 为镜像兜底）。
        const meta = {
          text: dictLabel("sys_user_status", v, userStatusTexts()),
          color: dictListClass("sys_user_status", v, userStatusColors()),
        };
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: "在线",
      dataIndex: "online",
      width: 80,
      render: (v: boolean) => (
        <Tooltip title="判据：5 分钟内是否有过请求（不是浏览器是否开着）">
          <Tag color={v ? "green" : "default"}>{v ? "在线" : "离线"}</Tag>
        </Tooltip>
      ),
    },
    {
      title: "最后登录",
      dataIndex: "last_login_at",
      width: 170,
      render: (v: string | null, row) =>
        v ? (
          <Tooltip title={row.last_login_ip || "-"}>
            <Text style={{ fontSize: 12 }}>{v.replace("T", " ").slice(0, 19)}</Text>
          </Tooltip>
        ) : (
          //: 缺值渲染 `-` 而不是 0 / 空字符串（硬约束⑤ 不造假）
          <Text type="secondary">从未登录</Text>
        ),
    },
    {
      title: "操作",
      key: "actions",
      fixed: "right",
      width: 260,
      render: (_: unknown, row) => {
        const actions = [];
        if (row.status === "pending" && can("system:user:approve")) {
          actions.push(
            <Button
              key="approve"
              size="small"
              type="link"
              icon={<CheckCircleOutlined />}
              onClick={async () => {
                await approveSystemUser(row.id);
                message.success("已通过审核");
                void load();
              }}
            >
              通过
            </Button>
          );
        }
        //: 写操作无权限 ⇒ **不渲染**（与「读操作 disabled」区分，见 use-can.ts 注释）
        if (can("system:user:edit")) {
          actions.push(
            <Button
              key="edit"
              size="small"
              type="link"
              icon={<EditOutlined />}
              onClick={() => setEditTarget(row)}
            >
              编辑
            </Button>
          );
        }
        if (can("system:user:resetPassword")) {
          actions.push(
            <Button
              key="pwd"
              size="small"
              type="link"
              icon={<KeyOutlined />}
              onClick={() => setPwdTarget(row)}
            >
              重置密码
            </Button>
          );
        }
        if (can("system:user:forceLogout")) {
          actions.push(
            <Button
              key="kick"
              size="small"
              type="link"
              icon={<LogoutOutlined />}
              onClick={() => handleForceLogout(row)}
            >
              下线
            </Button>
          );
        }
        if (can("system:user:delete")) {
          actions.push(
            <Button
              key="del"
              size="small"
              type="link"
              danger
              icon={<DeleteOutlined />}
              onClick={() => handleDelete(row)}
            >
              删除
            </Button>
          );
        }
        return actions.length ? <Space size={0}>{actions}</Space> : <Text type="secondary">-</Text>;
      },
    },
  ];

  // 设计 63 §6（2026-10-25 用户截图「分页未固定在底部」）：三区布局 —— 卡头/筛选
  // 固定，表格体内部滚动，分页钉在卡底（共享类 `.fill-table`，与商品台账同一套）。
  // 旧写法整卡 `overflow:auto` ⇒ 分页随内容滚出屏外（截图里最后一行被截断同源）。
  return (
    <div style={{ height: "calc(100vh - 87px)", display: "flex", flexDirection: "column" }}>
    <Card
      size="small"
      style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
      styles={{ body: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" } }}
      title={`用户（共 ${total}）`}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          {can("system:user:create") && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              新建用户
            </Button>
          )}
        </Space>
      }
    >
      {selectedIds.length > 0 && (
        <div
          style={{
            marginBottom: 12,
            padding: "6px 12px",
            background: "#fafafa",
            borderRadius: 6,
            display: "flex",
            alignItems: "center",
            gap: 12,
          }}
        >
          <Text>已选 {selectedIds.length} 项</Text>
          <Space size={4} wrap>
            {can("system:user:batchApprove") && (
              <Button size="small" loading={batchBusy === "approve"} onClick={() => void runBatch("approve")}>
                通过审核
              </Button>
            )}
            {can("system:user:batchForceLogout") && (
              <Button size="small" loading={batchBusy === "kick"} onClick={() => void runBatch("kick")}>
                批量下线
              </Button>
            )}
            {can("system:user:batchResetPassword") && (
              <Button size="small" onClick={() => setBatchPwdOpen(true)}>
                重置密码
              </Button>
            )}
            {can("system:user:batchDelete") && (
              <Button size="small" danger loading={batchBusy === "delete"} onClick={() => void runBatch("delete")}>
                批量删除
              </Button>
            )}
          </Space>
          <Button type="link" size="small" onClick={() => setSelectedIds([])}>
            清空选择
          </Button>
        </div>
      )}
      <Space style={{ marginBottom: 12 }} wrap>
        <Input
          placeholder="用户名（模糊）"
          allowClear
          style={{ width: 180 }}
          value={draft.username ?? ""}
          onChange={(e) => setDraft((d) => ({ ...d, username: e.target.value }))}
        />
        <Select
          placeholder="状态"
          allowClear
          style={{ width: 130 }}
          value={draft.status}
          onChange={(v) => setDraft((d) => ({ ...d, status: v }))}
          options={[
            { value: "normal", label: "正常" },
            { value: "pending", label: "待审核" },
            { value: "disabled", label: "已停用" },
          ]}
        />
        <Select
          placeholder="角色"
          allowClear
          style={{ width: 180 }}
          value={draft.role}
          onChange={(v) => setDraft((d) => ({ ...d, role: v }))}
          options={roleOptions}
        />
        <Button
          type="primary"
          onClick={() => {
            setFilter(draft);
            setPage(1);
          }}
        >
          查询
        </Button>
        <Button
          onClick={() => {
            setDraft({});
            setFilter({});
            setPage(1);
          }}
        >
          重置
        </Button>
      </Space>

      <div className="fill-table">
      <Table<SystemUser>
        rowKey="id"
        size="small"
        rowSelection={{
          selectedRowKeys: selectedIds,
          onChange: (keys) => setSelectedIds(keys as number[]),
          //: 自己不能批量操作自己（后端也会拦，前端先不给选，避免必然失败的勾选）
          getCheckboxProps: (row) => ({ disabled: row.username === "admin" }),
        }}
        loading={loading}
        columns={columns}
        dataSource={rows}
        scroll={{ x: 1400, y: "100%" }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          showTotal: (n) => `共 ${n} 条`,
          onChange: (p, ps) => {
            setPage(p);
            setPageSize(ps);
          },
        }}
      />
      </div>

      <UserCreateModal
        open={createOpen}
        roleOptions={assignableRoleOptions}
        onClose={() => setCreateOpen(false)}
        onSaved={() => {
          setCreateOpen(false);
          void load();
        }}
      />
      <UserEditModal
        user={editTarget}
        roleOptions={roleOptionsFor(editTarget?.username)}
        onClose={() => setEditTarget(null)}
        onSaved={() => {
          setEditTarget(null);
          void load();
        }}
      />
      <ResetPasswordModal
        user={pwdTarget}
        onClose={() => setPwdTarget(null)}
      />
      <Modal
        centered
        open={Boolean(detail)}
        title={`用户详情 · ${detail?.username ?? ""}`}
        footer={null}
        onCancel={() => setDetail(null)}
      >
        {detail && (
          <Space direction="vertical" style={{ width: "100%" }} size={6}>
            <Text>ID：{detail.id}</Text>
            <Text>用户名：{detail.username}</Text>
            <Text>姓名：{detail.real_name || "-"}</Text>
            <Text>邮箱：{detail.email || "-"}</Text>
            {/* 详情给全手机号（列表脱敏） */}
            <Text>手机：{detail.phone || "-"}</Text>
            <Text>
              角色：{detail.role_name || detail.role}（{detail.role}）
            </Text>
            <Text>状态：{dictLabel("sys_user_status", detail.status, userStatusTexts()) || detail.status}</Text>
            <Text>最后登录：{detail.last_login_at ? detail.last_login_at.replace("T", " ").slice(0, 19) : "从未登录"}</Text>
            <Text>登录 IP：{detail.last_login_ip || "-"}</Text>
            <Text>创建时间：{detail.created_at ? detail.created_at.replace("T", " ").slice(0, 19) : "-"}</Text>
          </Space>
        )}
      </Modal>
      <Modal
        title="批量重置密码"
        open={batchPwdOpen}
        centered
        onCancel={() => setBatchPwdOpen(false)}
        onOk={async () => {
          if (!batchPwd || batchPwd.length < 6) {
            message.warning("密码至少 6 位");
            return;
          }
          setBatchPwdOpen(false);
          await runBatch("reset", batchPwd);
          setBatchPwd("");
        }}
        okText={`重置 ${selectedIds.length} 个用户`}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text type="secondary">所有选中用户设为同一个密码，下次登录需修改。</Text>
          <Input.Password
            placeholder="新密码（至少 6 位）"
            value={batchPwd}
            onChange={(e) => setBatchPwd(e.target.value)}
          />
        </Space>
      </Modal>
    </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ 弹窗 */

function UserCreateModal({
  open,
  roleOptions,
  onClose,
  onSaved,
}: {
  open: boolean;
  roleOptions: { value: string; label: string }[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  return (
    <Modal
      centered
      open={open}
      title="新建用户"
      confirmLoading={saving}
      onCancel={onClose}
      onOk={async () => {
        try {
          const values = await form.validateFields();
          setSaving(true);
          await createSystemUser(values);
          message.success("已创建");
          form.resetFields();
          onSaved();
        } catch (error) {
          if ((error as { errorFields?: unknown }).errorFields) return;
          message.error(error instanceof Error ? error.message : "创建失败");
        } finally {
          setSaving(false);
        }
      }}
    >
      <Form form={form} layout="vertical" initialValues={{ role: "user", status: "normal" }}>
        <Form.Item
          name="username"
          label="用户名"
          rules={[
            { required: true, message: "请输入用户名" },
            { min: 3, max: 80, message: "3–80 个字符" },
            { pattern: /^[A-Za-z0-9_]+$/, message: "只能字母 / 数字 / 下划线" },
          ]}
        >
          <Input placeholder="登录名" autoComplete="off" />
        </Form.Item>
        <Form.Item
          name="password"
          label="初始密码"
          rules={[
            { required: true, message: "请输入密码" },
            { min: 6, message: "至少 6 位" },
          ]}
        >
          <Input.Password placeholder="至少 6 位" autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="real_name" label="姓名">
          <Input placeholder="可选" />
        </Form.Item>
        <Form.Item
          name="email"
          label="邮箱"
          rules={[{ type: "email", message: "邮箱格式不正确" }]}
        >
          <Input placeholder="可选（用于邮箱验证码登录）" autoComplete="off" />
        </Form.Item>
        <Form.Item name="phone" label="手机号">
          <Input placeholder="可选" />
        </Form.Item>
        <Form.Item name="role" label="角色">
          <Select options={roleOptions} />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select
            options={[
              { value: "normal", label: "正常" },
              { value: "disabled", label: "停用" },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function UserEditModal({
  user,
  roleOptions,
  onClose,
  onSaved,
}: {
  user: SystemUser | null;
  roleOptions: { value: string; label: string }[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!user) return;
    form.setFieldsValue({
      real_name: user.real_name,
      email: user.email,
      phone: user.phone,
      role: user.role,
      status: user.status,
    });
  }, [user, form]);

  return (
    <Modal
      centered
      open={Boolean(user)}
      title={`编辑用户 · ${user?.username ?? ""}`}
      confirmLoading={saving}
      onCancel={onClose}
      onOk={async () => {
        if (!user) return;
        try {
          const values = await form.validateFields();
          setSaving(true);
          await updateSystemUser(user.id, values);
          message.success("已保存");
          onSaved();
        } catch (error) {
          if ((error as { errorFields?: unknown }).errorFields) return;
          message.error(error instanceof Error ? error.message : "保存失败");
        } finally {
          setSaving(false);
        }
      }}
    >
      <Form form={form} layout="vertical">
        {/* 用户名不可改：改了等于换一个人，历史审计会断（与 dataPanel 一致） */}
        <Form.Item label="用户名">
          <Input value={user?.username} disabled />
        </Form.Item>
        <Form.Item name="real_name" label="姓名">
          <Input />
        </Form.Item>
        <Form.Item name="email" label="邮箱" rules={[{ type: "email", message: "邮箱格式不正确" }]}>
          <Input />
        </Form.Item>
        <Form.Item name="phone" label="手机号">
          <Input />
        </Form.Item>
        <Form.Item
          name="role"
          label="角色"
          extra="改角色会让该用户立即重新登录（旧令牌失效）"
        >
          <Select options={roleOptions} />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select
            options={[
              { value: "normal", label: "正常" },
              { value: "pending", label: "待审核" },
              { value: "disabled", label: "停用" },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function ResetPasswordModal({
  user,
  onClose,
}: {
  user: SystemUser | null;
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  return (
    <Modal
      centered
      open={Boolean(user)}
      title={`重置密码 · ${user?.username ?? ""}`}
      confirmLoading={saving}
      onCancel={onClose}
      onOk={async () => {
        if (!user) return;
        try {
          const values = await form.validateFields();
          setSaving(true);
          await resetSystemUserPassword(user.id, values.password, values.must_change ?? true);
          message.success("已重置，该用户的旧令牌已失效");
          form.resetFields();
          onClose();
        } catch (error) {
          if ((error as { errorFields?: unknown }).errorFields) return;
          message.error(error instanceof Error ? error.message : "重置失败");
        } finally {
          setSaving(false);
        }
      }}
    >
      <Form form={form} layout="vertical" initialValues={{ must_change: true }}>
        <Form.Item
          name="password"
          label="新密码"
          rules={[
            { required: true, message: "请输入新密码" },
            { min: 6, message: "至少 6 位" },
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认新密码"
          dependencies={["password"]}
          rules={[
            { required: true, message: "请再次输入" },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue("password") === value) return Promise.resolve();
                return Promise.reject(new Error("两次输入不一致"));
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="must_change" label="下次登录强制改密" valuePropName="checked">
          <Select
            options={[
              { value: true, label: "是（推荐）" },
              { value: false, label: "否" },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
