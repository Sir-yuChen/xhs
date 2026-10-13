/**
 * 系统更新弹窗（设计 36，2026-10-13 用户口径）。
 *
 * ## 两种入口（2026-10-13 用户追加口径）
 *
 * > 「再切换白天和夜间模式图标旁边新增检查版本更新入口图标，点击检查当前版本，
 * > 存在新版本则弹窗提示更新或不更新，**只有在每次软件打开时的入口才要求强制更新**」
 *
 * * **启动检查**（`<UpdateDialog />`，挂 `WorkspaceShell` 打开软件即查）= **强制**：
 *   弹出即不可关闭（无 X、`maskClosable=false`、无「稍后」/「暂不更新」）。
 *   原口径：「存在新版本则强制安装最新版本软件」。
 * * **手动检查**（顶栏「检查版本更新」图标）= **可选**：弹窗可关，
 *   footer 给「暂不更新」+「下载」两条路 —— 用户主动点的检查，理应能选择不更新。
 *
 * 两者共用同一套「检查 / 下载 / 安装」状态机（`useUpdateFlow`）与弹窗主体
 * （`UpdateModalBody`）——**一份实现，两种强制语义**（DEF-044：不留两份）。
 *
 * **唯一让步（两种入口一致）**：下载/安装失败时转可关闭的错误态并给「重试 / 关闭」——
 * 强制的对象是**版本**，不是把用户锁死在装不上的界面上。
 */
import { CheckCircleOutlined, CloudDownloadOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Collapse, Modal, Progress, Space, Tag, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  checkSystemUpdate,
  downloadSystemUpdate,
  fetchAppVersion,
  installSystemUpdate,
} from "../../lib/api";
import type { SystemUpdateInfo, SystemVersionRelease } from "../../types";

const { Text, Paragraph } = Typography;

export type UpdatePhase =
  | "idle"
  | "checking"
  | "available"
  | "downloading"
  | "downloaded"
  | "installing"
  | "error";

export type UpdateFlow = {
  info: SystemUpdateInfo | null;
  /** 版本升级历史（来自 `GET /api/system/version`，**最新在前**）。 */
  history: SystemVersionRelease[];
  phase: UpdatePhase;
  error: string;
  progress: number;
  runCheck: () => Promise<void>;
  handleDownload: () => Promise<void>;
  handleInstall: () => Promise<void>;
  reset: () => void;
  /** 检查完成且无新版本（手动入口据此弹「已是最新」提示）。 */
  upToDate: boolean;
};

/**
 * 「检查 / 下载 / 安装」状态机（两种入口共用）。
 *
 * @param autoCheckOnMount 挂载即自动检查一次（**启动入口**用；手动入口由点击触发）。
 */
export function useUpdateFlow(autoCheckOnMount = false): UpdateFlow {
  const [info, setInfo] = useState<SystemUpdateInfo | null>(null);
  const [history, setHistory] = useState<SystemVersionRelease[]>([]);
  const [phase, setPhase] = useState<UpdatePhase>("idle");
  const [error, setError] = useState<string>("");
  const [installerPath, setInstallerPath] = useState<string>("");
  const [progress, setProgress] = useState(0);
  const [upToDate, setUpToDate] = useState(false);
  const checkedRef = useRef(false);

  const runCheck = useCallback(async () => {
    setPhase("checking");
    setError("");
    setUpToDate(false);
    try {
      const result = await checkSystemUpdate();
      setInfo(result);
      if (result.has_update) {
        setPhase("available");
      } else {
        // 无新版本：手动入口要能告诉用户「已是最新」；启动入口静默即可。
        setPhase("idle");
        setUpToDate(true);
      }
    } catch (exc) {
      // 检查失败**不静默**给手动入口用（用户主动点的，得知道为什么没结果）；
      // 是否提示由调用方决定 —— 这里只如实置错误态。
      setError("检查更新失败，请检查网络后重试。");
      setPhase("error");
      throw exc;
    }
  }, []);

  useEffect(() => {
    if (!autoCheckOnMount) return;
    // 每次打开软件（挂载）检查一次；用 ref 防 React 严格模式的重复调用。
    if (checkedRef.current) return;
    checkedRef.current = true;
    void runCheck().catch(() => {
      // 启动检查失败**静默**（网络抖动不该打扰用户；也不该挡登录后的正常使用）。
      setPhase("idle");
      setError("");
    });
  }, [autoCheckOnMount, runCheck]);

  // 版本升级历史（弹窗按版本折叠展示，用户口径 2026-10-13）—— 挂载时读一次。
  // 读不到就退化成「只有远端 notes 一条」，不影响「检查 / 下载 / 安装」主流程。
  useEffect(() => {
    void fetchAppVersion()
      .then((snapshot) => setHistory(snapshot.history ?? []))
      .catch(() => setHistory([]));
  }, []);

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

  const reset = useCallback(() => {
    setPhase("idle");
    setError("");
  }, []);

  return {
    info,
    history,
    phase,
    error,
    progress,
    runCheck,
    handleDownload,
    handleInstall,
    reset,
    upToDate,
  };
}

/** 弹窗主体（版本号 + **版本升级历史折叠** + 进度 + 错误）——两种入口共用一份。 */
function UpdateModalBody({ flow }: { flow: UpdateFlow }) {
  const { info, history, phase, error, progress } = flow;

  //: 折叠面板数据：优先用**本地版本历史**（`config/version.json` 的 versions[]，
  //: 用户口径「记录的是版本升级历史」）；本地为空时退化成「远端 notes 一条」。
  const panels = (() => {
    if (history.length > 0) {
      return history.map((item) => ({
        key: item.version,
        version: item.version,
        built_at: item.built_at,
        summary: item.summary,
        notes: item.notes,
      }));
    }
    if (info?.notes) {
      return [
        {
          key: info.latest || "latest",
          version: info.latest || "最新版本",
          built_at: "",
          summary: "",
          notes: info.notes,
        },
      ];
    }
    return [];
  })();

  //: 有新版本时，默认展开**最新那条**（用户一眼看到这次更新了什么）。
  const [activeKeys, setActiveKeys] = useState<string[]>([]);
  useEffect(() => {
    setActiveKeys(panels.length > 0 ? [panels[0].key] : []);
  }, [panels.length]);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Space>
        <Text>当前版本</Text>
        <Tag>{info?.current || "-"}</Tag>
        <Text>最新版本</Text>
        <Tag color="blue">{info?.latest || "-"}</Tag>
      </Space>

      {panels.length > 0 ? (
        <div style={{ maxHeight: "32vh", overflowY: "auto" }}>
          <Text type="secondary">版本更新记录：</Text>
          <Collapse
            ghost
            size="small"
            style={{ marginTop: 4 }}
            activeKey={activeKeys}
            onChange={(keys) =>
              setActiveKeys(Array.isArray(keys) ? keys.map(String) : [String(keys)])
            }
            items={panels.map((panel) => ({
              key: panel.key,
              label: (
                <Space size={8} wrap>
                  <Text strong>{`v${panel.version}`}</Text>
                  {panel.built_at ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {panel.built_at}
                    </Text>
                  ) : null}
                  {panel.summary ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {panel.summary}
                    </Text>
                  ) : null}
                </Space>
              ),
              children: (
                <Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
                  {panel.notes || "（无更新说明）"}
                </Paragraph>
              ),
            }))}
          />
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
  );
}

/**
 * 启动检查入口（**强制**）：打开软件即查；有新版本弹窗**不可关闭**。
 *
 * 用户口径「存在新版本则强制安装最新版本软件」。
 */
export function UpdateDialog() {
  const flow = useUpdateFlow(true);
  const { phase } = flow;
  const open =
    phase === "available" ||
    phase === "downloading" ||
    phase === "downloaded" ||
    phase === "installing" ||
    phase === "error";
  if (!open) return null;

  // ⚠️ 强制更新：正常路径不可关闭；**错误态**才给关闭出口（否则用户既装不上也退不出）。
  const closable = phase === "error";

  return (
    <Modal
      open
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
      onCancel={closable ? flow.reset : undefined}
      footer={
        phase === "error"
          ? [
              <Button key="retry" icon={<ReloadOutlined />} onClick={() => void flow.runCheck()}>
                重试
              </Button>,
              <Button key="close" onClick={flow.reset}>
                关闭
              </Button>,
            ]
          : [
              phase === "available" && (
                <Button key="dl" type="primary" onClick={() => void flow.handleDownload()}>
                  下载
                </Button>
              ),
              phase === "downloaded" && (
                <Button key="install" type="primary" onClick={() => void flow.handleInstall()}>
                  立即安装并重启
                </Button>
              ),
            ].filter(Boolean)
      }
      //: 宽高都是**视口百分比**（不是固定像素），各种屏幕比例一致。
      //: 用户口径 2026-10-13「检查版本更新 固定全屏的 40%」⇒ 宽 40vw + 高 40vh。
      width="40vw"
      styles={{ body: { height: "40vh", maxHeight: "40vh", overflowY: "auto" } }}
    >
      <UpdateModalBody flow={flow} />
    </Modal>
  );
}

/**
 * 手动检查入口（**不强制**）：顶栏「检查版本更新」图标点出来的弹窗。
 *
 * 用户口径「点击检查当前版本，存在新版本则弹窗提示更新或不更新」——
 * 故本入口的弹窗**可关闭**，且 footer 明给「暂不更新」。
 */
export function ManualUpdateDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const flow = useUpdateFlow(false);
  const { phase } = flow;

  // 打开时自动查一次（用户点图标 = 想看结果，不该再让他点第二下）。
  const openedRef = useRef(false);
  useEffect(() => {
    if (!open) {
      openedRef.current = false;
      return;
    }
    if (openedRef.current) return;
    openedRef.current = true;
    void flow.runCheck().catch(() => {
      /* 错误态已由 flow 置好，弹窗自己会显示 */
    });
  }, [open, flow]);

  useEffect(() => {
    if (!open) flow.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const hasUpdate = phase === "available" || phase === "downloading" || phase === "downloaded" || phase === "installing";

  return (
    <Modal
      open
      centered
      title={
        <Space>
          <CloudDownloadOutlined />
          <span>{hasUpdate ? "发现新版本" : "检查版本更新"}</span>
        </Space>
      }
      closable
      maskClosable
      onCancel={onClose}
      footer={
        phase === "error"
          ? [
              <Button key="retry" icon={<ReloadOutlined />} onClick={() => void flow.runCheck()}>
                重试
              </Button>,
              <Button key="close" onClick={onClose}>
                关闭
              </Button>,
            ]
          : [
              // 「暂不更新」= 手动入口特有的出口（启动入口没有它）。
              <Button key="skip" onClick={onClose}>
                暂不更新
              </Button>,
              phase === "available" && (
                <Button key="dl" type="primary" onClick={() => void flow.handleDownload()}>
                  下载
                </Button>
              ),
              phase === "downloaded" && (
                <Button key="install" type="primary" onClick={() => void flow.handleInstall()}>
                  立即安装并重启
                </Button>
              ),
            ].filter(Boolean)
      }
      //: 宽高都是**视口百分比**（不是固定像素），各种屏幕比例一致。
      //: 用户口径 2026-10-13「检查版本更新 固定全屏的 40%」⇒ 宽 40vw + 高 40vh。
      width="40vw"
      styles={{ body: { height: "40vh", maxHeight: "40vh", overflowY: "auto" } }}
    >
      {phase === "checking" ? (
        <Space>
          <Text type="secondary">正在检查最新版本……</Text>
        </Space>
      ) : flow.upToDate ? (
        <Space>
          <CheckCircleOutlined style={{ color: "#52c41a" }} />
          <Text>
            当前已是最新版本
            {flow.info?.current ? `（${flow.info.current}）` : ""}。
          </Text>
        </Space>
      ) : (
        <UpdateModalBody flow={flow} />
      )}
    </Modal>
  );
}
