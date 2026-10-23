/**
 * **初始化环境**弹窗（设计 24 §27；用户口径 2026-10-23）。
 *
 * 原文：「新增一个初始化环境的弹窗，展示各个依赖下载的进度，如果下载失败支持
 * 点击清空本地重新下载」+「下载失败自动重试 3 次，依旧失败，支持用户触发重复下载」。
 *
 * 为什么必须有这个弹窗
 * --------------------
 * 1.0.5 安装包实测：**装包版没有 ffmpeg** ⇒ 视频抽帧失败 ⇒ `ocr_ok=None`
 * ⇒ 视频被 `passedMedia` 过滤掉 ⇒ 用户勾了视频也传不上去，还提示「数据包中没有视频」。
 * 根因是「依赖没随包」，而修法是「让用户在首次启动时把能下载的依赖下下来」
 * （node 83 MB + OCR 模型 30 MB + ffmpeg 6 MB ≈ 119 MB 从安装包里拿出来）。
 *
 * 交互口径
 * --------
 * * 打开即探测（`GET /system/deps`）—— **只报状态，不自动开始下载**
 *   （用户要看到清单再决定，避免一进软件就闷头拉 83 MB）。
 * * 点「开始下载」→ 每个依赖各自 `POST /system/deps/download` → 轮询 `progress`。
 * * 失败态显示原因 + **「清空本地重新下载」**按钮（`POST /system/deps/reset` 再下载）。
 * * `required` 项（node）没下成功 ⇒ **不能关闭弹窗**（软件不可用），
 *   非 required 项允许「跳过」。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircleOutlined,
  CloudDownloadOutlined,
  ExclamationCircleOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { Alert, App, Button, Modal, Progress, Space, Tag, Tooltip, Typography } from "antd";
import { WaitMask } from "../ui/wait-mask";
import {
  fetchDepsStatus,
  startDepDownload,
  resetDep,
  type DepItem,
  type DepProgress,
} from "../../lib/api";

const { Text } = Typography;

/** 轮询间隔（ms）。下载 83 MB 属于慢操作，1.2s 一刷足够且不压后端。 */
const POLL_MS = 1200;

type Row = DepItem & { progress?: DepProgress };

export function EnvBootstrapDialog({
  onDone,
  autoStart = false,
}: {
  onDone?: () => void;
  /** 打开即**自动触发下载**（用户口径 2026-10-23：手动入口「不完整则自动触发下载」）。
   *  ⚠️ 启动入口（`EnvBootstrapGate`）**不自动下载** —— 一进软件就闷头拉 83 MB
   *  会把用户吓一跳，启动入口只弹清单让用户点「开始下载」。 */
  autoStart?: boolean;
}) {
  //: ⚠️ 禁止静态 `message`（本仓守卫：静态方法不吃 ConfigProvider 主题，
  //: antd 官方也不推荐）。统一走 `App.useApp()`。
  const { message } = App.useApp();
  //: ⚠️⚠️ **默认打开**（2026-10-23 用户报障「点击检查依赖，没有弹窗展示检查情况」）：
  //: 本组件是**被条件挂载**的（`{depCheckOpen ? <EnvBootstrapDialog/> : null}`），
  //: 挂载即代表「要显示」。初版写成 `useState(false)` 且没有任何地方置 true
  //: ⇒ 点按钮只让组件挂载、弹窗永远不出现（点了没反应）。
  const [open, setOpen] = useState(true);
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  //: 「刷新状态」的独立等待态（用户口径 2026-10-23：「点击刷新状态按钮无反应，
  //: 加公共等待组件」）。⚠️ 不与 `busy` 合并：`busy` 管下载动作，
  //: 刷新只是**重新探测**，共用一个会让下载中的刷新按钮点亮整个蒙版。
  const [refreshing, setRefreshing] = useState(false);
  const [requiredMissing, setRequiredMissing] = useState<string[]>([]);
  const timer = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  }, []);

  /** 拉一次状态 + 进度（合并成一行数据）。 */
  const refresh = useCallback(async () => {
    const data = await fetchDepsStatus();
    const progress = data.progress ?? {};
    setRows(
      (data.deps.items ?? []).map((item: DepItem) => ({
        ...item,
        progress: progress[item.key],
      })),
    );
    setRequiredMissing(data.deps.required_missing ?? []);
    return data;
  }, []);

  //: 打开时探测一次；`autoStart` 时**发现缺失立刻开下**（手动入口口径）
  useEffect(() => {
    if (!open) return;
    void refresh()
      .then((data) => {
        if (!autoStart) return;
        const missing = (data.deps.items ?? []).filter((i: DepItem) => !i.ready);
        if (missing.length === 0) return;
        //: 逐个起下载（不 await 全部 —— 让进度条先动起来）
        for (const item of missing) {
          void startDepDownload(item.key).catch(() => undefined);
        }
        window.setTimeout(() => void refresh().catch(() => undefined), 400);
      })
      .catch(() => undefined);
    return stopPolling;
  }, [autoStart, open, refresh, stopPolling]);

  const anyRunning = rows.some((r) => r.progress?.status === "running");

  //: 有下载在跑 ⇒ 开轮询；全停下来 ⇒ 关轮询（不留常驻定时器）
  useEffect(() => {
    if (!open || !anyRunning) {
      stopPolling();
      return;
    }
    stopPolling();
    timer.current = window.setInterval(() => {
      void refresh().catch(() => undefined);
    }, POLL_MS);
    return stopPolling;
  }, [anyRunning, open, refresh, stopPolling]);

  const downloadAll = useCallback(async () => {
    setBusy(true);
    try {
      const todo = rows.filter((r) => !r.ready);
      if (todo.length === 0) {
        void message.success("所有依赖已就绪");
        return;
      }
      //: 逐个起下载（后端各自起线程；**并发下载**会让慢网下的进度互相拖累）
      for (const row of todo) {
        await startDepDownload(row.key).catch((err: unknown) => {
          void message.error(`${row.name} 启动失败：${String(err)}`);
        });
      }
      await refresh();
    } finally {
      setBusy(false);
    }
  }, [refresh, rows]);

  const redo = useCallback(
    async (row: Row) => {
      setBusy(true);
      try {
        //: **先清空本地**（删目录）——否则半截文件会让重下又失败一次
        await resetDep(row.key);
        await startDepDownload(row.key);
        await refresh();
        void message.success(`${row.name} 已清空，开始重新下载`);
      } catch (err: unknown) {
        void message.error(`清空失败：${String(err)}`);
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  /** 「刷新状态」：重新探测本地依赖（**带公共等待蒙版**）。
   *
   *  ⚠️ 用户报障 2026-10-23「点击刷新状态按钮无反应」的两个原因：
   *    ① 探测通常几十毫秒就返回、界面毫无变化 ⇒ 用户以为没响应；
   *    ② 失败时静默（`catch(() => undefined)`）⇒ 网络断了也看不到任何提示。
   *  ⇒ 用 `WaitMask`（本仓统一等待组件）+ 结果 message（就绪/缺失逐项报）。
   */
  const manualRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const data = await refresh();
      const missing = (data.deps.items ?? []).filter((i: DepItem) => !i.ready);
      if (missing.length === 0) {
        void message.success("依赖检查完成：全部就绪");
      } else {
        void message.warning(
          `检查完成：${missing.length} 项缺失（${missing.map((i: DepItem) => i.name).join("、")}）`,
        );
      }
    } catch (err: unknown) {
      void message.error(`检查失败：${String(err)}`);
    } finally {
      setRefreshing(false);
    }
  }, [message, refresh]);

  const allReady = rows.length > 0 && rows.every((r) => r.ready);
  //: **必装项没下成功 ⇒ 不许关**（node 缺失则签名全挂，软件等于不可用）
  const blocked = requiredMissing.length > 0;

  return (
    <Modal
      open={open}
      //: ⚠️ `centered` 必须放在 `title` **之前**：本仓守卫从 `<Modal` 向下找
      //: 「行尾 `>`」来定块边界，而 `title` 里的 `<CloudDownloadOutlined />`
      //: 那一行以 `/>` 结尾 ⇒ 会被误判成 Modal 提前结束、看不到后面的 `centered`。
      centered
      title={
        <Space>
          <CloudDownloadOutlined />
          初始化环境
        </Space>
      }
      width="min(760px, 86vw)"
      maskClosable={false}
      closable={!blocked}
      keyboard={!blocked}
      okText={allReady ? "完成" : "开始下载"}
      onOk={async () => {
        if (allReady) {
          //: 必须**通知父级卸载** —— 本组件是条件挂载的，
          //: 只 `setOpen(false)` 会让它留在 DOM 里（下次再点按钮不会重新探测）。
          onDone?.();
          setOpen(false);
          return;
        }
        await downloadAll();
      }}
      okButtonProps={{ loading: busy, disabled: anyRunning }}
      cancelButtonProps={{ style: { display: blocked ? "none" : undefined } }}
      onCancel={() => {
        onDone?.();
        setOpen(false);
      }}
      footer={(_, { OkBtn, CancelBtn }) => (
        <Space>
          <Button
            onClick={() => void manualRefresh()}
            icon={<ReloadOutlined />}
            loading={refreshing}
            disabled={busy}
          >
            刷新状态
          </Button>
          <CancelBtn />
          <OkBtn />
        </Space>
      )}
    >
      {/* 公共等待蒙版（本仓统一组件 `WaitMask`）：刷新状态时盖住整页并显示耗时，
          避免「点了没反应」的观感。
          ⚠️ 用 `scope="page"`（fixed 铺满视口）而不是 `panel`：`panel` 要求父元素
          `position: relative`，而 antd Modal 的 body 默认**不是**定位容器 ⇒ 会往上
          找到 body 变成整页蒙版（表现与 page 相同但行为不可控）。既然本弹窗是
          「独占式检查」——刷新时用户不该去点别处——直接用 page 语义最准确。 */}
      <WaitMask open={refreshing} title="正在检查本地依赖…" />

      <Alert
        type={blocked ? "error" : "info"}
        showIcon
        style={{ marginBottom: 12 }}
        message={
          blocked
            ? "必装依赖未就绪，软件暂时无法使用（下载完成前不能关闭本窗口）"
            : "首次使用需下载下列组件（约 340 MB，其中独立窗口内核约 220 MB；已就绪的会跳过）"
        }
      />

      <Space direction="vertical" size={10} style={{ width: "100%" }}>
        {rows.map((row) => {
          const p = row.progress;
          const running = p?.status === "running";
          const failed = p?.status === "failed";
          const percent = row.ready ? 100 : (p?.percent ?? 0);
          return (
            <div
              key={row.key}
              style={{ border: "1px solid var(--ant-color-border-secondary, #f0f0f0)", borderRadius: 8, padding: "10px 12px" }}
            >
              <Space style={{ width: "100%", justifyContent: "space-between" }} align="start">
                <Space direction="vertical" size={2}>
                  <Space size={6}>
                    <Text strong>{row.name}</Text>
                    <Tag>{row.size_hint}</Tag>
                    {row.required ? <Tag color="red">必装</Tag> : null}
                    {row.ready ? (
                      <Tag color="green" icon={<CheckCircleOutlined />}>
                        已就绪
                      </Tag>
                    ) : failed ? (
                      <Tag color="red" icon={<ExclamationCircleOutlined />}>
                        下载失败
                      </Tag>
                    ) : running ? (
                      <Tag color="blue">下载中</Tag>
                    ) : (
                      <Tag>待下载</Tag>
                    )}
                  </Space>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {row.detail}
                  </Text>
                </Space>
                {failed || (row.resettable && !row.ready && !running) ? (
                  <Tooltip title="清空本地文件后重新下载">
                    <Button
                      size="small"
                      icon={<ReloadOutlined />}
                      onClick={() => void redo(row)}
                      disabled={busy || running}
                    >
                      清空本地重新下载
                    </Button>
                  </Tooltip>
                ) : null}
              </Space>

              <Progress
                percent={percent}
                size="small"
                //: 下载中把**实时量级**写进 format（如「42.8 / 109.6 MB」）。
                //: 只看百分比时,220 MB 的内核下载在一段时间里"数字不动"更像是卡死;
                //: 带上已下载/总量,用户能自己判断「在慢」还是「断了」（用户口径
                //: 2026-10-23「下载进度需要反馈给用户」）。
                format={(v) =>
                  running && p?.note ? <span style={{ fontSize: 12 }}>{p.note}</span> : `${v ?? 0}%`
                }
                status={failed ? "exception" : row.ready ? "success" : running ? "active" : "normal"}
                style={{ marginTop: 8, marginBottom: 0 }}
              />

              {failed && p?.error ? (
                <Text type="danger" style={{ fontSize: 12 }}>
                  {p.error}
                </Text>
              ) : null}
            </div>
          );
        })}
      </Space>

    </Modal>
  );
}

/** 启动入口：挂在 `WorkspaceShell`，**打开软件即探测一次**（不自动下载）。 */
export function EnvBootstrapGate() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void fetchDepsStatus()
      .then((data: Awaited<ReturnType<typeof fetchDepsStatus>>) => {
        if (cancelled) return;
        //: 有任何一个缺失就弹（哪怕是可选依赖 —— 缺 ffmpeg 会让视频无法检测）
        if ((data.deps.missing ?? []).length > 0) setOpen(true);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  if (!open) return null;
  return <EnvBootstrapDialog onDone={() => setOpen(false)} />;
}

export default EnvBootstrapGate;
