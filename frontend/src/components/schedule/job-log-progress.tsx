/**
 * 任务日志「任务进度」的**唯一渲染实现**（列表列 + 详情弹窗共用，DEF-044）。
 *
 * ## 为什么要抽成一个组件（2026-09-28 用户报障，设计 13 §43）
 *
 * 用户原话：「同步商品/笔记任务，任务日志的**任务进度列的进度条自动达到了 100%** 但是此时
 * **任务刚刚开始**，点击查看详情发现**详情弹窗中进度条 0%**」。
 *
 * 真身：**同一行日志，两处各画各的**。列表列在「在跑且 `progress` 为 0」时渲染
 * `percent={100}` 的「不定量条」—— 满格条与「100% 完成」**视觉同形**（只少一个数字）；
 * 详情弹窗读的是同一个 `log.progress`（0）⇒ 0%。两个读数同时摆在用户面前，
 * 无论信哪个都是错的（取消后 `status` 变 `failed`，列表列又回落成真实值 0% ⇒
 * 用户看到「100% → 取消 → 0%」的第三次矛盾）。
 *
 * ⇒ 两处**共用本组件**：比例已知画真比例，比例未知画**不定量滑动条**（没有固定填充比例，
 * 既不冒充 100%，也不是 0%），两个界面从此不可能再出现两个读数。
 *
 * ## 「比例未知」的判据
 *
 * `running` 且 `progress` 为 0 —— 后端进度泵（`_ProgressPump`）只在
 * `percent > 现值` 时写库、**永不写 0**，所以「在跑 + 0」就是「一次都没上报过」，
 * 而不是「跑到了 0%」（设计 13 §35.18.6：未知与 0% 是两件事，0% 会被读成卡死）。
 */
import { Progress, Tooltip, theme } from "antd";

import type { ScheduledJobLog } from "../../types";

/** 未知比例的说明文案（唯一实现：列表列与详情弹窗共用一句，不各写一份）。 */
export const LOG_PROGRESS_UNKNOWN_TIP =
  "增量 / 时间区间同步会提前结束，总量不可知，故不显示百分比";

type LogProgressInput = Pick<ScheduledJobLog, "status" | "progress">;

/** 「比例未知」= 在跑、但后端没上报过任何百分比（0 即「未上报」，缺值口径）。 */
export function isLogProgressUnknown(log: LogProgressInput): boolean {
  return log.status === "running" && !log.progress;
}

export type JobLogProgressProps = {
  log: LogProgressInput;
  /** 与 antd `Progress` 同参：列表列用 `small`（6px），弹窗用默认（8px）。 */
  size?: "small" | "default";
  /** 进度条宽度上限（详情弹窗的 `Descriptions` 单元格会撑满，需限宽）。 */
  maxWidth?: number;
};

export function JobLogProgress({ log, size = "small", maxWidth }: JobLogProgressProps) {
  // 主题色从 antd token 取（本项目 dark / light 双主题都靠 token 表达），
  // 不硬编码颜色 —— 轨道色与 antd `Progress` 的 `remainingColor` 同源（`colorFillSecondary`）。
  const { token } = theme.useToken();

  if (isLogProgressUnknown(log)) {
    const height = size === "small" ? 6 : 8;
    return (
      <Tooltip title={LOG_PROGRESS_UNKNOWN_TIP}>
        {/* 不定量条：轨道 + 一个**滑动**的色块。⚠️ 刻意不设宽度百分比 ——
            「满格」会被读成「已完成」，那正是本次要修的那件事。 */}
        <span
          className="log-progress-unknown"
          style={{ height, background: token.colorFillSecondary, maxWidth }}
          role="progressbar"
          aria-label="任务进度"
          aria-valuetext="执行中，总量未知"
        >
          <span
            className="log-progress-unknown-bar"
            style={{ background: token.colorPrimary }}
          />
        </span>
      </Tooltip>
    );
  }

  return (
    <Progress
      percent={Math.max(0, Math.min(100, log.progress ?? 0))}
      size={size}
      /* ⚠️ 失败时**不切 `status="exception"`**（2026-09-27 用户口径「已执行的进度
         百分比保留」）：antd 该态会把百分比文字**换成 ✗ 图标**，用户就看不到
         「跑到多少了」；失败语义改由红色条 + 下方红色原因行表达。 */
      strokeColor={log.status === "failed" ? "#ff4d4f" : undefined}
      status={log.status === "running" ? "active" : "normal"}
      style={maxWidth ? { maxWidth } : undefined}
    />
  );
}
