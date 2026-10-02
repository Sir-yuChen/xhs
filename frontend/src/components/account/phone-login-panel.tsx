import { App, Button, Form, Input, Space, Typography } from "antd";
import { MessageOutlined, CheckCircleOutlined } from "@ant-design/icons";
import { useEffect, useRef, useState } from "react";

import { confirmXhsPhoneLogin, detailMessage, sendXhsPhoneCode } from "../../lib/api";
import { loginChannelLabel, loginConfirmed } from "../../lib/login-state";
import { useThemePalette } from "../../app/providers";
import type { PlatformAccount } from "../../types";
import { LoginSubmitMask } from "./login-submit-mask";

const { Text } = Typography;

type PhoneLoginPanelProps = {
  accountType: "pc" | "creator";
  onConfirmed: (account: PlatformAccount) => void;
  /**
   * **续期模式**（设计 13 §18 / §18.16）：带目标账号 ⇒ 把验证码登录拿到的新令牌
   * 写进**这一个**账号的对应通道，而不是按身份建号（建号路径会 upsert 出行，
   * 「登成别的号」时静默新建一行 —— §18.8 决定 ③A）。
   */
  targetAccountId?: number;
  /** 续期成功回调（与 QrLoginPanel 同形：`channel` 是令牌实际落进的那条通道）。 */
  onRenewed?: (account: PlatformAccount | null, channel: string) => void;
};

const PHONE_CODE_COOLDOWN_SECONDS = 120;

/**
 * 手机验证码登录面板（设计 13 §18；2026-09-23 §18.16 起支持补登续期）。
 *
 * ⚠️ 2026-09-23（§18.15.2）：「登录 PC 后同步 Creator 账号」勾选**整条移除** ——
 * 语义与三平台拆分后的「一次登录只更新自己那个平台」方向相反（同 QrLoginPanel）。
 * 后端 `sync_creator` 参数保留（契约不动），前端不再发送。
 *
 * ⚠️ 2026-09-23（§18.16）：**续期模式**。带 `targetAccountId` 时，发码与确认
 * 都带上 `target_account_id`，后端只把这一个账号的对应通道换掉（身份不符 ⇒
 * 400 + 结构化原因，**一个字节都没写**）。面板文案随之切到续期语义，
 * 与扫码续期（`QrLoginPanel`）同一套口径：写入了哪个平台、其它平台没动、
 * 失败时「本次没有保存任何令牌」。
 */
export function PhoneLoginPanel({
  accountType,
  onConfirmed,
  targetAccountId,
  onRenewed,
}: PhoneLoginPanelProps) {
  const p = useThemePalette();
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [statusText, setStatusText] = useState("输入手机号后发送验证码");
  const [isSending, setIsSending] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);
  const [cooldownSeconds, setCooldownSeconds] = useState(0);
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径：
  // 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）—— 面板内的
  // notice / error 两条 inline Alert 收口成 message；`statusText` 是**实时
  // 状态行**，不是一次性提醒，保留。
  const { message } = App.useApp();
  const isCoolingDown = cooldownSeconds > 0;
  const renewing = typeof targetAccountId === "number";

  // 回调走 ref：续期模式在**整个挂载期不变**，但避免闭包读到旧 props。
  const onRenewedRef = useRef(onRenewed);
  onRenewedRef.current = onRenewed;

  useEffect(() => {
    if (cooldownSeconds <= 0) {
      return;
    }

    const timer = window.setTimeout(() => {
      setCooldownSeconds((current) => Math.max(0, current - 1));
    }, 1000);

    return () => window.clearTimeout(timer);
  }, [cooldownSeconds]);

  async function handleSendCode() {
    if (isCoolingDown) {
      return;
    }
    if (phone.trim().length < 6) {
      message.warning("请输入有效手机号。");
      return;
    }

    setIsSending(true);
    try {
      const result = await sendXhsPhoneCode({
        sub_type: accountType,
        phone: phone.trim(),
        // 续期模式带上目标账号（§18.16）：无目标（绑定新账号）时**不带这个键**，
        // 对象形状保持「意图明确」—— 建号 vs 续期（同 QianfanEmailLoginPanel）。
        ...(targetAccountId === undefined ? {} : { target_account_id: targetAccountId }),
      });
      setSessionId(result.session_id);
      setStatusText("验证码已发送，请查看手机短信");
      setCooldownSeconds(PHONE_CODE_COOLDOWN_SECONDS);
    } catch {
      message.error("验证码发送失败。");
    } finally {
      setIsSending(false);
    }
  }

  async function handleConfirm() {
    if (!sessionId) {
      message.warning("请先发送验证码。");
      return;
    }
    if (code.trim().length < 4) {
      message.warning("请输入短信验证码。");
      return;
    }

    setIsConfirming(true);
    try {
      const result = await confirmXhsPhoneLogin({
        sub_type: accountType,
        session_id: sessionId,
        phone: phone.trim(),
        code: code.trim(),
        ...(targetAccountId === undefined ? {} : { target_account_id: targetAccountId }),
      });
      /*
        ⚠️ **关窗/落态的唯一判据 = 后端结论**（2026-09-28 用户口径「根据后端接口返回的
        成功与否进行判断是否关闭弹窗」）。HTTP 200 **不等于**登录成功：后端可能回
        `status="pending"`（平台还没确认）。旧实现不判这一条 ⇒ 未成功也回调、也关窗，
        用户看到窗口关了却什么都没更新。判据走唯一实现 `loginConfirmed`。
      */
      if (!loginConfirmed(result)) {
        const reason = detailMessage((result as { renew_error?: unknown }).renew_error);
        setStatusText("登录未完成");
        message.error(reason || "后端未确认登录成功，本次没有更新任何令牌。");
        return;
      }
      if (renewing) {
        /*
          续期分支（§18.16）：登录成功但续期失败时后端返回 **400 + 结构化
          `{code, message}`**（同千帆邮箱密码端点），在下面的 catch 里收口；
          能走到这里说明令牌已写进目标账号的对应通道。
          通道名走 `loginChannelLabel()`（唯一实现），不在面板里再写一份。
        */
        const channel = result.renewed_channel || "";
        const channelText = channel ? loginChannelLabel(channel) : "对应";
        setStatusText("登录令牌已更新");
        message.success(`已把新令牌写入该账号的「${channelText}」。其它平台的令牌未被改动。`);
        onRenewedRef.current?.(result.account ?? null, channel);
        return;
      }
      if (result.account) {
        onConfirmed(result.account);
      }
      setStatusText("账号绑定成功");
    } catch (caught) {
      const detail = (caught as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
      const reason = detailMessage(detail);
      if (renewing) {
        // 续期失败：后端**一个字节都没写**，文案必须说清「什么都没改」。
        setStatusText("续期未完成");
        message.error(reason || "续期失败，本次没有保存任何令牌。");
      } else {
        message.error("验证码校验失败或已过期。");
      }
    } finally {
      setIsConfirming(false);
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%", position: "relative" }}>
      <Text style={{ color: p.textSecondary }}>{statusText}</Text>

      <Form layout="vertical">
        <Form.Item label={<span style={{ color: p.text }}>手机号</span>}>
          <Input
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="请输入手机号"
          />
        </Form.Item>
      </Form>

      <Button
        block
        icon={<MessageOutlined />}
        onClick={handleSendCode}
        disabled={isSending || isCoolingDown}
        loading={isSending}
      >
        {isSending ? "发送中..." : isCoolingDown ? `${cooldownSeconds} 秒后重发` : "发送验证码"}
      </Button>

      <Form layout="vertical">
        <Form.Item label={<span style={{ color: p.text }}>验证码</span>}>
          <Input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="请输入验证码"
          />
        </Form.Item>
      </Form>

      {renewing ? (
        <Text type="secondary" style={{ fontSize: 11 }}>
          本次登录只更新这一个账号的对应令牌，不会新建账号；登录后若不是该账号，会报错且不保存任何内容。
        </Text>
      ) : null}

      <Button
        type="primary"
        block
        icon={<CheckCircleOutlined />}
        onClick={handleConfirm}
        loading={isConfirming}
      >
        {isConfirming ? "验证中..." : renewing ? "确认并更新令牌" : "确认绑定"}
      </Button>

      {/*
        提交蒙版（2026-09-28 用户口径）：点「确认并更新令牌」之后盖住表单 ——
        既让用户看到「正在提交」，也挡住重复点击。成功时面板回调关窗、`isConfirming`
        随之归位，蒙版与窗口一起消失。
      */}
      <LoginSubmitMask active={isConfirming} />
    </Space>
  );
}
