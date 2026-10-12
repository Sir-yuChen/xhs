/**
 * 系统更新弹窗（设计 36，2026-10-13 用户口径）。
 *
 * 用户口径原文：
 * > 「每次打开软件后需要检查版本是否需要更新，存在新版本则弹窗提示更新，展示更新内容，
 * > 提供下载按钮，下载成功点击安装按钮触发软件的按钮（安装），实现静默安装保证历史数据；
 * > **存在新版本则强制安装最新版本软件**。」
 *
 * 「强制」的落地（设计 36 §4）：弹出即**不可关闭**（无 X、`maskClosable=false`、
 * 无「稍后」）；下载 → 安装是唯一路径。**唯一让步**：下载/安装失败时转为可关闭的错误态
 * 并给「重试 / 关闭」出口 —— 强制的对象是**版本**，不是把用户锁死在装不上的界面上。
 *
 * 挂载点：`WorkspaceShell`（登录后即检查；打开软件即触发）。
 */
import { CloudDownloadOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Modal, Progress, Space, Tag, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import { checkSystemUpdate, downloadSystemUpdate, installSystemUpdate } from "../../lib/api";
import type { SystemUpdateInfo } from "../../types";

const { Text, Paragraph } = Typography;

type Phase = "idle" | "checking" | "available" | "downloading" | "downloaded" | "installing" | "error";

export function UpdateDialog() {
  const [info, setInfo] = useState<SystemUpdateInfo | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string>("");
  const [installerPath, setInstallerPath] = useState<string>("");
  const [progress, setProgress] = useState(0);
  const checkedRef = useRef(false);

  const runCheck = useCallback(async () => {
    setPhase("checking");
    setError("");
    try {
      const result = await checkSystemUpdate();
      setInfo(result);
      setPhase(result.has_update ? "available" : "idle");
    } catch {
      // 检查失败**静默**（网络抖动不该打扰用户；也不该挡登录后的正常使用）
      setPhase("idle");
    }
  }, []);

  useEffect(() => {
    // 每次打开软件（挂载）检查一次；用 ref 防 React 严格模式的重复调用。
    if (checkedRef.current) return;
    checkedRef.current = true;
    void runCheck();
  }, [runCheck]);

  const handleDownload = useCallback(async () => {
    if (!info?.download_url) {
      setError("更新源未提供下载地址。");
      setPhase("error");
      return;
    }
    setPhase("downloading");
    setProgress(0);
    // 进度用定时器模拟（后端为同步下载，无真实分片进度）——到 90% 停，成功后跳 100。
    const timer = setInterval(() => setProgress((p) => (p < 90 ? p + 7 : p)), 300);
    try {
      const result = await downloadSystemUpdate({
        url: info.download_url,
        version: info.latest,
      });
      clearInterval(timer);
      if (result.ok) {
        setProgress(100);
        setInstallerPath(result.path);
        setPhase("downloaded");
      } else {
        setError(result.message || "下载失败");
        setPhase("error");
      }
    } catch {
      clearInterval(timer);
      setError("下载失败，请检查网络后重试。");
      setPhase("error");
    }
  }, [info]);

  const handleInstall = useCallback(async () => {
    if (!installerPath) return;
    setPhase("installing");
    try {
      const result = await installSystemUpdate(installerPath);
      if (!result.ok) {
        setError(result.message || "启动安装失败");
        setPhase("error");
      }
      // 成功：安装器会重启应用，本页面即将失效 —— 不额外提示。
    } catch {
      setError("启动安装失败，请重试。");
      setPhase("error");
    }
  }, [installerPath]);

  const open = phase === "available" || phase === "downloading" || phase === "downloaded" || phase === "installing" || phase === "error";
  if (!open) return null;

  // ⚠️ 强制更新：正常路径不可关闭；**错误态**才给关闭出口（否则用户既装不上也退不出）。
  const closable = phase === "error";

  return (
    <Modal
      open={open}
      centered
      title={
        <Space>
          <CloudDownloadOutlined />
          <span>发现新版本</span>
        </Space>
      }
      closable={closable}
      maskClosable={false}
      keyboard={closable}
      onCancel={closable ? () => setPhase("idle") : undefined}
      footer={
        phase === "error"
          ? [
              <Button key="retry" icon={<ReloadOutlined />} onClick={() => void runCheck()}>
                重试
              </Button>,
              <Button key="close" onClick={() => setPhase("idle")}>
                关闭
              </Button>,
            ]
          : [
              phase === "available" && (
                <Button key="dl" type="primary" onClick={() => void handleDownload()}>
                  下载
                </Button>
              ),
              phase === "downloaded" && (
                <Button key="install" type="primary" onClick={() => void handleInstall()}>
                  立即安装并重启
                </Button>
              ),
            ].filter(Boolean)
      }
      width="56vw"
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Space>
          <Text>当前版本</Text>
          <Tag>{info?.current || "-"}</Tag>
          <Text>最新版本</Text>
          <Tag color="blue">{info?.latest || "-"}</Tag>
        </Space>

        {info?.notes ? (
          <div style={{ maxHeight: "32vh", overflowY: "auto" }}>
            <Text type="secondary">更新内容：</Text>
            <Paragraph style={{ whiteSpace: "pre-wrap", marginTop: 4 }}>{info.notes}</Paragraph>
          </div>
        ) : null}

        {phase === "downloading" && <Progress percent={progress} status="active" />}
        {phase === "downloaded" && <Text type="success">下载完成，点击「立即安装并重启」更新。</Text>}
        {phase === "installing" && <Text type="secondary">正在启动安装程序，应用将自动重启……</Text>}
        {phase === "error" && <Alert type="error" showIcon message={error || "更新失败"} />}

        <Text type="secondary" style={{ fontSize: 12 }}>
          更新仅替换程序文件，账号、笔记与历史数据全部保留。
        </Text>
      </Space>
    </Modal>
  );
}
