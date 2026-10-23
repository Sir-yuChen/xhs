/*
 * 强制改密页（v1.1.0 / 设计 55 §1.4）。
 *
 * ## 为什么必须有这一页
 *
 * 出厂管理员是 `admin` / `admin123`（迁移里插的种子，`must_change_password=true`）。
 * 如果登录后不强制改密，这个口令就永远有效 —— 而它是**公开在文档里**的。
 * 所以：`must_change_password=true` 的用户登录后，除了这一页和登出，
 * **任何页面都进不去**（`ProtectedRoute` 里拦）。
 *
 * ## 为什么改完要跳登录页
 *
 * 改密会抬 `token_version` ⇒ 手上这个令牌**已经失效**。继续用它发请求会 401，
 * 前端拦截器又要刷新、刷新也会失败 —— 表现为「改完密码页面卡住」。
 * 所以改完直接 `logout()` 回登录页，用新密码重新登录，路径最干净。
 *
 * ## 不许「稍后再说」
 *
 * 刻意**不提供**「取消 / 跳过」按钮：一旦允许跳过，用户会永远跳过。
 * 唯一出口是「登出」（换一个账号登录）。
 */

import { LockOutlined } from "@ant-design/icons";
import { App, Button, Card, Form, Input, Result, Space, Typography } from "antd";
import { useState } from "react";

import { changeMyPassword } from "../../lib/api";

const { Text } = Typography;

export function ForcePasswordPage() {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <Card style={{ width: 460, maxWidth: "100%" }}>
        <Result
          status="warning"
          icon={<LockOutlined />}
          title="请先修改初始密码"
          subTitle={
            <Text type="secondary">
              当前账号仍在使用初始口令。为了账号安全，修改后才能进入系统。
            </Text>
          }
          style={{ padding: "0 0 16px" }}
        />
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values) => {
            setSaving(true);
            try {
              await changeMyPassword(values.old_password, values.new_password);
              message.success("密码已修改，请用新密码重新登录");
              //: 旧令牌已失效（后端抬了 token_version）⇒ 必须回登录页重新拿令牌。
              //: 用整页跳转而不是 SPA 导航：确保内存里的旧令牌 / 权限缓存都被丢掉。
              window.location.href = "/login";
            } catch (error) {
              const detail = (error as { response?: { data?: { detail?: { message?: string } } } })
                .response?.data?.detail;
              message.error(detail?.message ?? "修改失败，请重试");
            } finally {
              setSaving(false);
            }
          }}
        >
          <Form.Item
            name="old_password"
            label="当前密码（初始密码）"
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
              { required: true, message: "请再次输入新密码" },
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
          <Space style={{ width: "100%", justifyContent: "space-between" }}>
            <Button
              type="link"
              onClick={() => {
                //: 唯一出口：换账号（不能跳过改密）
                window.location.href = "/login";
              }}
            >
              换一个账号登录
            </Button>
            <Button type="primary" htmlType="submit" loading={saving}>
              修改并重新登录
            </Button>
          </Space>
        </Form>
      </Card>
    </div>
  );
}
