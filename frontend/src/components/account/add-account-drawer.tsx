import { App, Modal, Segmented } from "antd";
import { useState } from "react";

import { useThemePalette } from "../../app/providers";
import type { PlatformAccount } from "../../types";
import { PhoneLoginPanel } from "./phone-login-panel";
import { QianfanEmailLoginPanel } from "./qianfan-email-login-panel";
import { QrLoginPanel } from "./qr-login-panel";

type AddAccountDrawerProps = {
  open: boolean;
  onClose: () => void;
  onBound: () => void;
};

type AccountType = "pc" | "creator" | "qianfan";
type LoginMethod = "qr" | "phone" | "email";

const accountTypeOptions = [
  { label: "PC", value: "pc" as const },
  // 2026-09-23（设计 13 §18.15.2，用户口径）：「Creator」→「创作者平台」。
  { label: "创作者平台", value: "creator" as const },
  // 2026-09-23（§18.15.3）：新增千帆平台登录（扫码 + 邮箱密码）。
  { label: "千帆", value: "qianfan" as const },
];

/**
 * 登录方式按账号类型给（§18.15.2）：
 * * PC / 创作者平台：二维码 / 手机验证码；
 * * 千帆：二维码 / 邮箱密码（千帆登录页没有「我们的」手机验证码入口，不做）。
 *
 * ⚠️ **Cookie 入口已整档移除**（用户口径「Cookie 入口隐藏不再展示」）——
 * 后端 `/accounts/import-cookie` 端点与 `importXhsCookieAccount` 封装保留
 * （能力不撤、契约不动），撤的只是这个弹窗里的入口。
 */
const loginMethodOptions: Record<AccountType, { label: string; value: LoginMethod }[]> = {
  pc: [
    { label: "二维码", value: "qr" },
    { label: "手机验证码", value: "phone" },
  ],
  creator: [
    { label: "二维码", value: "qr" },
    { label: "手机验证码", value: "phone" },
  ],
  qianfan: [
    { label: "二维码", value: "qr" },
    { label: "邮箱密码", value: "email" },
  ],
};

export function AddAccountDrawer({ open, onClose, onBound }: AddAccountDrawerProps) {
  const p = useThemePalette();
  const [accountType, setAccountType] = useState<AccountType>("pc");
  const [method, setMethod] = useState<LoginMethod>("qr");
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径：
  // 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）—— `App.useApp()`
  // 替代静态 `message` 导入，实例继承 ConfigProvider 的主题与 locale。
  const { message } = App.useApp();

  function handleConfirmed(account: PlatformAccount) {
    const actionText = account.action === "updated" ? "已更新到账号矩阵" : "已加入账号矩阵";
    message.success(`${account.nickname || "账号"} ${actionText}`);
    onBound();
    // 绑定成功即收工：**弹窗连同蒙版一起关掉**（2026-09-26 用户口径
    // 「账号绑定成功后应关闭……绑定账号弹窗」）。之前只刷新列表不关窗，
    // 用户看到的是「二维码还挂在屏幕上」——分不清是没登成还是没关。
    onClose();
  }

  /**
   * 换账号类型时，若当前登录方式不在新类型的清单里（如千帆下选了手机验证码），
   * 退回二维码 —— 否则会出现「千帆 + 手机验证码」这种没有面板可渲染的组合。
   */
  function handleAccountTypeChange(value: AccountType) {
    setAccountType(value);
    if (!loginMethodOptions[value].some((option) => option.value === method)) {
      setMethod("qr");
    }
  }

  return (
    <Modal
            centered
            title={
        <div>
          <div style={{ fontSize: 12, color: p.textTertiary, marginBottom: 4, textTransform: "uppercase", letterSpacing: 1 }}>
            XHS Account
          </div>
          <div style={{ fontSize: 18, fontWeight: 600, color: p.text }}>添加小红书账号</div>
        </div>
      }
      open={open}
      onCancel={onClose}
      footer={null}
      destroyOnClose
      // 蒙版（用户口径「绑定时添加页面蒙版」，2026-09-26）：登录期间窗口是**模态**的
      // —— 点蒙版不关窗（`maskClosable={false}`：扫码扫到一半误点空白会把会话丢掉），
      // 且蒙版本身要**看得见**（见下方 `styles.mask`）。
      maskClosable={false}
      /**
       * 2026-09-23（设计 13 §18.15.2，用户口径「使用弹窗展示全屏的50%」，
       * 当日修正：高为全屏的 **75%**）：Drawer（右侧 420）→ Modal **50vw × 75vh**。
       * body `flex:1 + overflow:auto`：固定高度下窄视口两栏折行时右栏不被裁掉
       * （§33.10 挂链笔记弹窗 `hidden` 裁右栏的教训）。
       */
      width="50vw"
      styles={{
        // antd 6 Modal 的语义样式的「容器」= 对话框本体（无 content 键）；
        // 固定 75vh + flex 列布局：header 自适应、body 吃剩余高度并可滚动。
        container: { height: "75vh", display: "flex", flexDirection: "column", background: p.cardBg },
        header: { background: p.cardBg, borderBottom: `1px solid ${p.border}` },
        body: { background: p.cardBg, padding: 24, flex: 1, overflowY: "auto" },
        // 页面底色本身就是深色（#141414），antd 默认的 `rgba(0,0,0,0.45)` 蒙版
        // 在这上面**几乎看不出来**（用户口径「绑定时添加页面蒙版」）。
        // 显式给一层看得见的遮罩：加不透明度 + 轻微模糊，让「弹窗外的页面被压住」
        // 这件事在深色主题下也成立。
        mask: {
          background: "rgba(0, 0, 0, 0.75)",
          backdropFilter: "blur(2px)",
        },
      }}
    >
      <div style={{ marginBottom: 20 }}>
        <Segmented
          block
          value={accountType}
          options={accountTypeOptions}
          onChange={(val) => handleAccountTypeChange(val as AccountType)}
        />
      </div>

      <div style={{ marginBottom: 24 }}>
        <Segmented
          block
          value={method}
          options={loginMethodOptions[accountType]}
          onChange={(val) => setMethod(val as LoginMethod)}
        />
      </div>

      {/*
        先按账号类型分流：千帆只有 二维码 / 邮箱密码 两种面板；
        pc / creator 才有手机验证码（TS 据此把 accountType 收窄，不会出现
        「千帆 + 手机验证码」这种渲染不出面板的组合）。
      */}
      {accountType === "qianfan" ? (
        method === "email" ? (
          <QianfanEmailLoginPanel onConfirmed={handleConfirmed} />
        ) : (
          <QrLoginPanel accountType="qianfan" onConfirmed={handleConfirmed} />
        )
      ) : method === "phone" ? (
        <PhoneLoginPanel accountType={accountType} onConfirmed={handleConfirmed} />
      ) : (
        <QrLoginPanel accountType={accountType} onConfirmed={handleConfirmed} />
      )}
    </Modal>
  );
}
