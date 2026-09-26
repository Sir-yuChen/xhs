import { App, Button, Card, Space, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useEffect, useRef, useState } from "react";

import {
  createXhsCreatorQrLoginSession,
  createXhsPcQrLoginSession,
  createXhsQianfanQrLoginSession,
  detailMessage,
  pollXhsLoginSession
} from "../../lib/api";
import { loginChannelLabel } from "../../lib/login-state";
import type { PlatformAccount, XhsQrLoginSession } from "../../types";

const { Text, Link: AntLink } = Typography;

type QrLoginPanelProps = {
  accountType: "pc" | "creator" | "qianfan";
  onConfirmed: (account: PlatformAccount) => void;
  /**
   * **续期模式**（设计 13 §18）：把扫出来的新令牌写进这一个账号，而不是按身份建号。
   *
   * 续期模式下**不给**「同步 Creator 账号」勾选框 —— 那个选项会派生出一套新令牌并
   * upsert 出账号行，而续期的语义是「只改我点开的这一个」（§18.8 决定 ③A）。
   * 两个语义混在同一个弹窗里，用户没法知道自己点一下到底会改几个账号。
   *
   * ⚠️ 2026-09-23（设计 13 §18.15.2）：**「同步 Creator 账号」勾选已整条移除** ——
   * 不再有 `syncCreator` 状态、不再随创建/轮询发送 `sync_creator`。原因：它的语义
   * （一次登录派生两套令牌、upsert 出第二个账号行）与拆分后「一次登录只更新自己
   * 那个平台」的模型方向相反；PC 与创作者平台本就是两个入口，要哪个登哪个。
   * 后端 `sync_creator` 参数保留（契约不动），只是前端不再发送。
   */
  targetAccountId?: number;
  /** 续期成功：`channel` 是令牌实际落进的那条通道（`www` / `ark`）。 */
  onRenewed?: (account: PlatformAccount | null, channel: string) => void;
};

export function QrLoginPanel({ accountType, onConfirmed, targetAccountId, onRenewed }: QrLoginPanelProps) {
  const [session, setSession] = useState<XhsQrLoginSession | null>(null);
  const [statusText, setStatusText] = useState("准备生成二维码");
  const [isLoading, setIsLoading] = useState(false);
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径：
  // 「所有消息提醒参考 antd 成熟组件，不要在页面暂用空间」）—— 面板内的
  // notice / error 两条 inline Alert 收口成 message；`statusText` 是**实时
  // 状态行**（扫码进度），不是一次性提醒，保留。
  const { message } = App.useApp();
  const confirmedRef = useRef(false);
  const renewing = typeof targetAccountId === "number";

  const onConfirmedRef = useRef(onConfirmed);
  onConfirmedRef.current = onConfirmed;
  const onRenewedRef = useRef(onRenewed);
  onRenewedRef.current = onRenewed;
  /** 续期模式在**整个挂载期不变**，但走 ref 与上面两个同形，避免闭包读到旧值。 */
  const renewingRef = useRef(renewing);
  renewingRef.current = renewing;

  /**
   * Which session the QR on screen belongs to. **A poll response is only
   * allowed to touch state when it reports this same id.**
   *
   * Without that gate the panel can end up showing one QR while polling
   * another, and then the user's phone confirmation is credited to a session
   * nobody is watching — the page simply never advances. Two things used to
   * make that happen together:
   *
   * 1. **Two sessions per mount.** React StrictMode invokes effects twice in
   *    dev, and `startSession` had no guard, so a single drawer open minted
   *    two login sessions ~2s apart (the second POST is serialised behind the
   *    first by the signature layer). Only one of them was ever polled.
   * 2. **A stale poll response overwriting the live session.** The poll
   *    callback used to write `{...polled, qr_image_data_url: <current image>}`
   *    unconditionally. If a response for the *previous* session landed after
   *    the state had already moved on to the new one, `session_id` jumped back
   *    to the old session while `qr_image_data_url` kept the **new** QR image:
   *    the screen showed QR-B, the poller watched session-A, and the scan was
   *    never observed.
   *
   * Verified against the live DB: of 18 PC sessions, 8 had been polled (the
   * webprofile `gid` cookie is injected on the first poll) and 10 had not —
   * i.e. roughly half of every pair was minted and then abandoned.
   */
  const activeSessionIdRef = useRef<number | null>(null);
  /** Monotonic id per `startSession` call; a superseded creation never wins. */
  const createGenerationRef = useRef(0);
  /** Guards the mount effect so StrictMode's second invoke is a no-op. */
  const initialisedForRef = useRef<"pc" | "creator" | "qianfan" | null>(null);

  /**
   * 失败原因解析**只有一份实现**：`lib/api.ts::detailMessage()`。
   *
   * 这里曾经自带一份「只认字符串」的 detail 解析，外加一句硬编码的通用兜底 ——
   * 于是 DEF-058 修好全站 toast 之后，**这个弹窗是唯一没吃到修复的地方**：
   * 后端给的结构化 `{code, message}` 到这里照样变成一句通用文案，而且那句兜底
   * 对「二维码服务不可达」是**做不到的建议**（设计 13 §18.7②）。
   * 兜底只在**确实没有真原因**时出现，且必须指向用户真能做的事。
   */
  function errorMessage(error: unknown): string {
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return detailMessage(detail) || "二维码生成失败，请检查网络或平台登录态后重试。";
  }

  async function startSession() {
    const generation = createGenerationRef.current + 1;
    createGenerationRef.current = generation;
    setIsLoading(true);
    confirmedRef.current = false;
    // Nothing is active while we mint: any in-flight poll for the previous
    // session must not be able to write itself back into state.
    activeSessionIdRef.current = null;
    try {
      const nextSession =
        accountType === "pc"
          ? await createXhsPcQrLoginSession({
              target_account_id: targetAccountId
            })
          : accountType === "qianfan"
            // 千帆扫码（2026-09-21）：与创作者同一套 customer 单点登录，service 指向 ark。
            // 2026-09-23（§18.15.3）：target 可选 —— 不带 = 建号。
            ? await createXhsQianfanQrLoginSession({ target_account_id: targetAccountId as number })
            : await createXhsCreatorQrLoginSession({ target_account_id: targetAccountId });
      if (generation !== createGenerationRef.current) {
        // Superseded by a newer creation (StrictMode's second invoke, or a
        // refresh click). Drop it — adopting it late is exactly what put a
        // QR on screen that no one was polling.
        return;
      }
      activeSessionIdRef.current = nextSession.session_id;
      setSession(nextSession);
      setStatusText(
        accountType === "pc"
          ? "请使用小红书 App 扫描二维码"
          : accountType === "qianfan"
            ? "请使用小红书 App 扫描千帆登录二维码"
            : "请使用小红书 App 扫描 Creator 二维码"
      );
    } catch (caught) {
      if (generation !== createGenerationRef.current) {
        return;
      }
      message.error(errorMessage(caught));
    } finally {
      if (generation === createGenerationRef.current) {
        setIsLoading(false);
      }
    }
  }

  // Deliberately NOT keyed on any preference — the panel has no user-tunable
  // options left (2026-09-23 §18.15.2 removed the sync-Creator checkbox).
  //
  // The `initialisedForRef` gate makes StrictMode's double effect invocation
  // mint exactly one session. Keyed on the value rather than a boolean so a
  // real PC <-> Creator switch still gets its own session.
  useEffect(() => {
    if (initialisedForRef.current === accountType) {
      return;
    }
    initialisedForRef.current = accountType;
    void startSession();
  }, [accountType]);

  useEffect(() => {
    const sessionId = session?.session_id;
    if (!sessionId || session.status === "confirmed" || session.status === "expired") {
      return;
    }

    const interval = window.setInterval(async () => {
      try {
        const polled = await pollXhsLoginSession(sessionId);
        // A response that belongs to a session no longer on screen must be
        // dropped, not merged — see activeSessionIdRef above.
        if (polled.session_id !== activeSessionIdRef.current) {
          return;
        }
        setSession((current) =>
          current && current.session_id === sessionId
            ? { ...polled, qr_image_data_url: current.qr_image_data_url ?? polled.qr_image_data_url }
            : current
        );
        if (polled.status === "scanned") {
          setStatusText("已扫码，请在手机端确认登录");
        } else if (polled.status === "expired") {
          setStatusText("二维码已过期，请刷新");
        } else if (polled.status === "confirmed" && !confirmedRef.current) {
          confirmedRef.current = true;
          if (renewingRef.current) {
            /*
              续期分支：**成败都在这里收口**，且失败时后端**一个字节都没写**
              （扫成别的账号 / 目标账号不可用，§18.8 决定 ③A）。
              所以失败文案必须说清「什么都没改」，否则用户会以为已经改坏了。
            */
            if (polled.renew_error) {
              setStatusText("续期未完成");
              message.error(detailMessage(polled.renew_error) || "续期失败，本次没有保存任何令牌。");
              return;
            }
            const channel = polled.renewed_channel || "";
            /*
              通道名走 `loginChannelLabel()`（**唯一实现**），不在面板里再写一份
              三元表达式 —— 否则后端改通道名时，「卡片 tag 用新名、面板提示用旧名」
              会在同一屏上同时出现。
            */
            const channelText = channel ? loginChannelLabel(channel) : "对应";
            setStatusText("登录令牌已更新");
            /*
              ⚠️ 2026-09-21 拆分后**不再有**「千帆与创作者共用一份令牌」这句话 ——
              真机实测推翻了它（创作者 jar 打千帆一律 -13000）。这里只说事实：
              本次写入了哪个平台、其它平台没动。
            */
            message.success(`已把新令牌写入该账号的「${channelText}」。其它平台的令牌未被改动。`);
            // 令牌写入成功但**店铺关系没绑上**（1:1 冲突等）时必须说出来：
            // 否则用户以为「登录好了，商品也能看了」，而商品区仍显示未绑定店铺。
            const renewBindError = detailMessage(polled.shop_bind_error);
            if (renewBindError) {
              message.warning(renewBindError);
            }
            onRenewedRef.current?.(polled.account ?? null, channel);
            return;
          }
          if (!polled.account) {
            return;
          }
          setStatusText("账号绑定成功");
          // 同上：绑定成功、但店铺关系没绑上时照实说明（弹窗马上会关，靠 toast 承载）。
          const bindError = detailMessage(polled.shop_bind_error);
          if (bindError) {
            message.warning(bindError);
          }
          onConfirmedRef.current(polled.account);
        }
      } catch {
        if (sessionId === activeSessionIdRef.current) {
          // 轮询每 2s 一次：用**同一个 key**，连续失败只更新同一条 toast，
          // 不会每 2 秒叠一条（antd message 的成熟用法）。
          message.error({ content: "轮询登录状态失败，正在等待下一次尝试。", key: "qr-login-poll" });
        }
      }
    }, 2000);

    return () => window.clearInterval(interval);
  }, [accountType, session?.session_id, session?.status]);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card
        styles={{
          body: {
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 24,
            minHeight: 220,
            background: "#1f1f1f",
          },
        }}
        style={{ borderColor: "#303030" }}
      >
        {session?.qr_image_data_url ? (
          <img
            src={session.qr_image_data_url}
            alt="小红书登录二维码"
            style={{ width: 180, height: 180, borderRadius: 8, background: "#fff", padding: 8 }}
          />
        ) : (
          <div
            style={{
              width: 180,
              height: 180,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              background: "#262626",
              borderRadius: 8,
              color: "rgba(255,255,255,0.3)",
              fontSize: 28,
              fontWeight: 700,
            }}
          >
            QR
          </div>
        )}
      </Card>

      <div style={{ textAlign: "center" }}>
        <Text strong style={{ display: "block", marginBottom: 4, color: "rgba(255,255,255,0.88)" }}>
          {statusText}
        </Text>
        {session?.qr_url ? (
          <AntLink href={session.qr_url} target="_blank" rel="noreferrer">
            打开二维码链接
          </AntLink>
        ) : null}
      </div>

      {renewing ? (
        <Text type="secondary" style={{ fontSize: 11 }}>
          本次登录只更新这一个账号的对应令牌，不会新建账号；扫到的若不是该账号，会报错且不保存任何内容。
        </Text>
      ) : null}

      <Button
        block
        icon={<ReloadOutlined />}
        onClick={startSession}
        loading={isLoading}
      >
        {isLoading ? "生成中..." : "刷新二维码"}
      </Button>
    </Space>
  );
}
