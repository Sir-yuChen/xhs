import { App, Button, Form, Input, Space, Typography } from "antd";
import { KeyOutlined } from "@ant-design/icons";
import { useState } from "react";

import { detailMessage, loginXhsQianfanByEmail } from "../../lib/api";
import type { PlatformAccount } from "../../types";

const { Text } = Typography;

type QianfanEmailLoginPanelProps = {
  /**
   * **续期模式**（设计 13 §18 / §18.15.4）：带目标账号 ⇒ 把新令牌写进这一个账号。
   * 不带 ⇒ 绑定新账号（`sub_type=qianfan`，昵称取店铺名）。
   */
  targetAccountId?: number;
  onConfirmed: (account: PlatformAccount) => void;
  /** 续期成功回调（与 QrLoginPanel 同形，让卡片标签跟着刷新）。 */
  onRenewed?: (accountId: number) => void;
};

/**
 * 千帆邮箱密码登录面板（2026-09-23，设计 13 §18.15.3；真机抓包 + 服务端实证）。
 *
 * 一次请求跑完登录全链（后端同步端点），**不需要轮询**：邮箱密码没有
 * 「等用户扫码」的中间态。
 *
 * ## 邮箱与密码不保留
 *
 * 它们只在组件 state 里活到「登录请求返回」为止 —— 不写 localStorage、
 * 不进 URL、不进日志；后端同样只用它们换令牌（不落库、不回显）。
 * 用户离开这个弹窗，凭据随组件卸载消失。
 */
export function QianfanEmailLoginPanel({
  targetAccountId,
  onConfirmed,
  onRenewed,
}: QianfanEmailLoginPanelProps) {
  const [email, setEmail] = useState("");
  const [pwd, setPwd] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径：
  // 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）—— 登录失败
  // 不再渲染 inline Alert 占版面。
  const { message } = App.useApp();

  const renewing = typeof targetAccountId === "number";

  async function handleLogin() {
    if (!email.trim() || !pwd || isSubmitting) {
      return;
    }
    setIsSubmitting(true);
    try {
      const result = await loginXhsQianfanByEmail({
        email: email.trim(),
        pwd,
        // 无目标（绑定新账号）时**不带这个键** —— 带上 `undefined` 虽然会被
        // JSON 序列化丢掉，但对象形状应该是「意图明确」的：建号 vs 续期。
        ...(targetAccountId === undefined ? {} : { target_account_id: targetAccountId }),
      });
      // 凭据使命完成，立刻从 state 里清掉（不留着过夜）。
      setPwd("");
      setDone(true);
      if (result.account) {
        if (renewing) {
          onRenewed?.(result.account.id);
        } else {
          onConfirmed(result.account);
        }
      }
    } catch (caught) {
      const detail = (caught as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
      message.error(
        detailMessage(detail) || "登录失败，请确认邮箱与密码，或改用扫码登录。"
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  if (done) {
    return (
      <Text style={{ color: "#52c41a", fontSize: 13 }}>
        {renewing ? "千帆令牌已更新，其它平台未改动。" : "千帆账号已加入账号矩阵。"}
      </Text>
    );
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Text type="secondary" style={{ fontSize: 13 }}>
        使用千帆商家后台的账号密码登录。邮箱与密码仅用于本次登录，不会保存。
      </Text>
      {/*
        ⚠️ **autoComplete 必须关掉**（用户 2026-09-23 实测）：浏览器会把用户在
        千帆后台保存的凭据**自动填进**这两个框（当时填的是用户真手机号）。
        「默认应为空」不只是初始 state 为空 —— 还要防浏览器自动填充：
        email 用 `off` + 非语义 name；密码用 `new-password`（Chrome 对
        `current-password` / `username` 会主动填充，对 `new-password` 不会）。
      */}
      <Form layout="vertical" autoComplete="off">
        <Form.Item label={<span style={{ color: "rgba(255,255,255,0.88)" }}>邮箱</span>}>
          <Input
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="name@example.com"
            name="qianfan-login-email"
            autoComplete="off"
            style={{ background: "#1f1f1f", borderColor: "#303030", color: "rgba(255,255,255,0.88)" }}
          />
        </Form.Item>
        <Form.Item label={<span style={{ color: "rgba(255,255,255,0.88)" }}>密码</span>}>
          <Input.Password
            value={pwd}
            onChange={(event) => setPwd(event.target.value)}
            placeholder="千帆商家后台密码"
            name="qianfan-login-password"
            autoComplete="new-password"
            style={{ background: "#1f1f1f", borderColor: "#303030", color: "rgba(255,255,255,0.88)" }}
          />
        </Form.Item>
      </Form>

      <Button
        type="primary"
        block
        icon={<KeyOutlined />}
        onClick={() => void handleLogin()}
        loading={isSubmitting}
        disabled={!email.trim() || !pwd}
      >
        {isSubmitting ? "登录中..." : "登录"}
      </Button>
    </Space>
  );
}
