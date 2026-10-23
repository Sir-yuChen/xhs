/*
 * 个人资料页（v1.1.0 / 设计 49 §模块七）。
 *
 * ## 所有人可用，不需要权限码
 *
 * 这一页只有两件事：**看自己的账号信息** + **改自己的密码**。
 * 给它挂任何权限码都会导致「某个角色的用户永远改不了自己密码」，
 * 而 `must_change_password=true` 时那等于把人永久锁死。
 *
 * ## 为什么分成两块而不是一个长表单
 *
 * 改密会**让当前令牌失效**（后端抬 `token_version`），所以它必须是一个
 * 独立的、有明确后果的动作 —— 和「看信息」混在一起会让用户以为
 * 「改完还能继续用」。用两个 Card 分开，改密那块单独写清「改完要重新登录」。
 *
 * ## 可见字段的口径
 *
 * 手机号 / 邮箱是**自己的**，全量显示（列表接口才脱敏 —— 那个是给别人看的）。
 */

import { IdcardOutlined, LockOutlined, UserOutlined } from "@ant-design/icons";
import { App, Button, Card, Col, Descriptions, Form, Input, Row, Space, Tag, Typography } from "antd";
import { useState } from "react";

import { useAuth } from "../../hooks/use-auth";
import { usePermission } from "../../hooks/use-permission";
import { changeMyPassword } from "../../lib/api";

const { Text } = Typography;

export function ProfilePage() {
  const auth = useAuth();
  const { role, isAdmin, codes } = usePermission();
  const { message, modal } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  const user = auth.user;

  return (
    <Row gutter={16}>
      <Col xs={24} lg={12}>
        <Card size="small" title={<Space><UserOutlined />账号信息</Space>}>
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="用户名">{user?.username ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="姓名">{user?.real_name || "-"}</Descriptions.Item>
            <Descriptions.Item label="邮箱">{user?.email || "-"}</Descriptions.Item>
            <Descriptions.Item label="角色">
              <Tag color={isAdmin ? "red" : "blue"}>{role || "未知"}</Tag>
              {isAdmin && (
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                  拥有全部权限
                </Text>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="状态">
              {user?.status === "normal" ? (
                <Tag color="success">正常</Tag>
              ) : user?.status === "pending" ? (
                <Tag color="warning">待审核</Tag>
              ) : (
                <Tag>{(user?.status ?? "-") as string}</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="我的权限码">
              <Text type="secondary">
                {isAdmin ? "全部（超级管理员）" : `${codes.size} 条`}
              </Text>
            </Descriptions.Item>
          </Descriptions>
        </Card>
      </Col>

      <Col xs={24} lg={12}>
        <Card size="small" title={<Space><LockOutlined />修改密码</Space>}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            修改成功后当前登录会失效，需要用新密码重新登录。
          </Text>
          <Form
            form={form}
            layout="vertical"
            style={{ marginTop: 12 }}
            onFinish={async (values) => {
              modal.confirm({
                title: "确认修改密码？",
                content: "改完会立即退出登录，需要用新密码重新登录。",
                okText: "确认修改",
                onOk: async () => {
                  setSaving(true);
                  try {
                    await changeMyPassword(values.old_password, values.new_password);
                    message.success("密码已修改，正在退出…");
                    window.location.href = "/login";
                  } catch (error) {
                    const detail = (
                      error as { response?: { data?: { detail?: { message?: string } } } }
                    ).response?.data?.detail;
                    message.error(detail?.message ?? "修改失败");
                  } finally {
                    setSaving(false);
                  }
                },
              });
            }}
          >
            <Form.Item
              name="old_password"
              label="当前密码"
              rules={[{ required: true, message: "请输入当前密码" }]}
            >
              <Input.Password autoComplete="current-password" />
            </Form.Item>
            <Form.Item
              name="new_password"
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
              dependencies={["new_password"]}
              rules={[
                { required: true, message: "请再次输入" },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    if (!value || getFieldValue("new_password") === value) {
                      return Promise.resolve();
                    }
                    return Promise.reject(new Error("两次输入不一致"));
                  },
                }),
              ]}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={saving} icon={<IdcardOutlined />}>
              修改密码
            </Button>
          </Form>
        </Card>
      </Col>
    </Row>
  );
}
