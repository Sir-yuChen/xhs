import { Modal, Segmented, Typography } from "antd";
import { useState } from "react";

import { loginChannelLabel } from "../../lib/login-state";
import type { PlatformAccount } from "../../types";
import { PhoneLoginPanel } from "./phone-login-panel";
import { QianfanEmailLoginPanel } from "./qianfan-email-login-panel";
import { QrLoginPanel } from "./qr-login-panel";

const { Text } = Typography;

/**
 * 续期 / 补齐某个账号某个平台的登录令牌（设计 13 §18 / §18.14；2026-09-21 拆分）。
 *
 * ## 2026-09-23（§18.16）：补登 = 绑定账号弹窗的登录方式矩阵
 *
 * 用户口径：「账号信息卡中的补登应与绑定账号弹窗中的功能相同，不同平台提供不同的
 * 登录方式，默认使用扫码登录，支持切换补登方式」。因此本弹窗与
 * `AddAccountDrawer` 共用同一套「按平台给方式 + Segmented 切换 + 默认扫码」形态：
 *
 * | 平台（channel） | 补登方式（默认扫码） |
 * |---|---|
 * | `www`（网页版） | 二维码 / 手机验证码 |
 * | `creator`（创作者平台） | 二维码 / 手机验证码 |
 * | `ark`（千帆） | 二维码 / 邮箱密码 |
 *
 * 三个面板（`QrLoginPanel` / `PhoneLoginPanel` / `QianfanEmailLoginPanel`）与绑定
 * 弹窗**同一批组件**，续期模式下都带 `targetAccountId` —— 只把这一个账号的对应
 * 通道换掉，不走 upsert、不建新行（§18.8 决定 ③A）。
 *
 * **Cookie 粘贴入口已不在本弹窗**（§18.15.4 第 3 条被 §18.16 取代，原句在该节
 * 保留备查）：绑定弹窗的千帆档本来就没有粘贴（§18.15.2「Cookie 入口隐藏不再
 * 展示」），「功能相同」要求两边一致。后端 `/accounts/{id}/qianfan-cookie`
 * 端点与 `renewXhsQianfanCookie` 封装**保留不动**（能力不撤、契约不动），
 * 撤的只是弹窗里的入口 —— 与 §18.15.2 同一处理先例。
 *
 * 弹窗**只放二维码 / 表单**，为什么还不算「少说了话」：身份不符会失败且不写库，
 * 三个面板在续期模式下**本来就各有一行**说明（「本次登录只更新这一个账号的对应
 * 令牌，不会新建账号；xxx 后若不是该账号，会报错且不保存任何内容」），失败时
 * 还会渲染后端给的真原因。这里再写一遍就是**同一语义两份实现**（改一处漏一处）。
 */
export type RenewTarget = {
  account: PlatformAccount;
  /** `www`（网页版）/ `creator`（创作者平台）/ `ark`（千帆）。 */
  channel: string;
  /** 该平台当前状态，决定标题是「补齐」还是「重新登录」。 */
  state: string;
};

type RenewLoginModalProps = {
  target: RenewTarget | null;
  onClose: () => void;
  onRenewed: (accountId: number) => void;
};

type RenewMethod = "qr" | "phone" | "email";

/**
 * 补登登录方式按平台给（§18.16，与 `AddAccountDrawer.loginMethodOptions` 同矩阵）：
 * * www / creator：二维码 / 手机验证码；
 * * ark：二维码 / 邮箱密码（千帆登录页没有手机验证码入口，不做）。
 */
const renewMethodOptions: Record<string, { label: string; value: RenewMethod }[]> = {
  www: [
    { label: "二维码", value: "qr" },
    { label: "手机验证码", value: "phone" },
  ],
  creator: [
    { label: "二维码", value: "qr" },
    { label: "手机验证码", value: "phone" },
  ],
  ark: [
    { label: "二维码", value: "qr" },
    { label: "邮箱密码", value: "email" },
  ],
};

/** 默认一律扫码（用户口径「默认使用扫码登录」）。 */
const DEFAULT_METHOD: RenewMethod = "qr";

/** channel → 二维码面板的账号类型（与绑定弹窗同名映射一致）。 */
function qrAccountType(channel: string): "pc" | "creator" | "qianfan" {
  if (channel === "creator") return "creator";
  if (channel === "ark") return "qianfan";
  return "pc";
}

export function RenewLoginModal({ target, onClose, onRenewed }: RenewLoginModalProps) {
  const [method, setMethod] = useState<RenewMethod>(DEFAULT_METHOD);
  const channelText = target ? loginChannelLabel(target.channel) : "";
  const isFirstTime = target?.state === "missing";
  const accountName = target ? target.account.nickname || `账号 ${target.account.id}` : "";
  // 方式清单按平台给；目标渠道不在表里（不该发生）时退化成「只有扫码」。
  const methodOptions = (target && renewMethodOptions[target.channel]) || [
    { label: "二维码", value: DEFAULT_METHOD },
  ];
  // 当前方式不在这个平台的清单里（如带着 phone 切到 ark）⇒ 退回清单第一项
  // （永远是「二维码」），不渲染「千帆 + 手机验证码」这种没有面板可组合的组合
  // （同绑定弹窗口径）。退化成「取第一项」而不是写死 qr：哪天某个平台的清单
  // 换了顺序或首位，这里不需要跟着改。
  const effectiveMethod = methodOptions.some((option) => option.value === method)
    ? method
    : methodOptions[0].value;

  /**
   * 补登成功：刷新卡片标签 **+ 关掉弹窗**（2026-09-26 用户口径
   * 「补齐登录令牌补登成功之后……应自动关闭弹窗」）。
   *
   * 三个面板只在**真正成功**时才回调（失败走各自的 inline 说明 / toast，
   * `renew_error` 分支更是直接 return）—— 所以这里不必再判一次成败，
   * 判两次等于两处各自以为对方在判。
   */
  function handleRenewed(accountId: number) {
    onRenewed(accountId);
    onClose();
  }

  return (
    <Modal
      open={target !== null}
      title={
        <span style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
          <span>{isFirstTime ? "补齐登录令牌" : "重新登录"}</span>
          <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            {accountName} · {channelText}
          </Text>
        </span>
      }
      footer={null}
      width={440}
      onCancel={onClose}
      destroyOnClose
      styles={{
        header: { background: "#1f1f1f", borderBottom: "1px solid #303030" },
        body: { background: "#141414", padding: 20 },
      }}
    >
      {target ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {/*
            登录方式切换（§18.16）：与绑定弹窗同一个 Segmented 形态，
            默认落在「二维码」。渠道本身由卡片 tag 决定，不在这里选。
          */}
          <Segmented
            block
            value={effectiveMethod}
            options={methodOptions}
            onChange={(val) => setMethod(val as RenewMethod)}
          />
          {effectiveMethod === "qr" ? (
            <QrLoginPanel
              accountType={qrAccountType(target.channel)}
              targetAccountId={target.account.id}
              onConfirmed={() => undefined}
              // 成功与否都由面板自己说（它会渲染「已把新令牌写入…」或后端给的真原因）；
              // 这里负责让卡片标签刷新 + 关窗。
              onRenewed={(account) => handleRenewed(account?.id ?? target.account.id)}
            />
          ) : effectiveMethod === "phone" ? (
            <PhoneLoginPanel
              accountType={target.channel === "creator" ? "creator" : "pc"}
              targetAccountId={target.account.id}
              onConfirmed={() => undefined}
              onRenewed={(account) => onRenewed(account?.id ?? target.account.id)}
            />
          ) : (
            <QianfanEmailLoginPanel
              targetAccountId={target.account.id}
              onConfirmed={() => undefined}
              onRenewed={(id) => handleRenewed(id)}
            />
          )}
        </div>
      ) : null}
    </Modal>
  );
}
