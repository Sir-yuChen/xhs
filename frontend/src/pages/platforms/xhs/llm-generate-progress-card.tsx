/**
 * LLM 生成进度卡（T9 / 设计 17 §7.4、§13.4.5）。
 *
 * 复用一期队列卡骨架的**同一套口径**（§31.10 进度卡的纪律）：
 *   - 5s 轮询 `generate-llm-queue`，键名 `generated`（不是底座通用的 `synced`）；
 *   - **停刷判据 = `total > 0 && 待处理 === 0`**（+ 空转兜底），不是「轮询 N 轮就停」；
 *   - 显示**当日三数**（成功 / 失败 / 待处理），不是本页累计；
 *   - ⚠️ 轮询摘除**禁写 `finally`**：写 `finally` 会在「取数失败但队列仍在跑」时停掉轮询，
 *     队列永远停在那一刻（本项目专门有一条守卫 H9 钉这个）。
 */
import { Progress, Space, Statistic, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import { fetchLlmGenerateQueue } from "../../../lib/api";
import type { LlmGenerateQueueSnapshot } from "../../../types";

const { Text } = Typography;

const POLL_INTERVAL_MS = 5000;
/** 空转兜底：连续这么多轮「待处理 0」且 total 为 0 就停（防「从没跑过」永远轮询）。 */
const IDLE_ROUNDS_LIMIT = 6;

const EMPTY_SNAPSHOT: LlmGenerateQueueSnapshot = {
  generated: 0,
  failed: 0,
  total: 0,
  running: 0,
  queued: 0,
  finished: [],
};

export function LlmGenerateProgressCard({
  /** 外部（操作列）触发一次后登记：让卡片从「空闲」切到「轮询」。 */
  activeItemIds,
  onFinishedEntry,
}: {
  activeItemIds: Set<string>;
  /** 每条终态回调一次（宿主据此给操作列按钮复位 loading、报失败原因）。 */
  onFinishedEntry?: (entry: LlmGenerateQueueSnapshot["finished"][number]) => void;
}) {
  const [snapshot, setSnapshot] = useState<LlmGenerateQueueSnapshot>(EMPTY_SNAPSHOT);
  const [polling, setPolling] = useState(false);
  const timerRef = useRef<number | null>(null);
  const idleRoundsRef = useRef(0);
  const seenRef = useRef<Set<string>>(new Set());

  const stop = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setPolling(false);
  }, []);

  const tick = useCallback(async () => {
    let next: LlmGenerateQueueSnapshot;
    try {
      next = await fetchLlmGenerateQueue();
    } catch {
      // ⚠️ 取数失败**不停轮询**（不是 finally）：可能是瞬时网络问题，
      // 队列还在跑；停了会让卡片永远停在失败那一刻。
      return;
    }
    setSnapshot(next);

    // 终态回调（每条只报一次）
    for (const entry of next.finished || []) {
      const key = `${entry.item_id}:${entry.status}`;
      if (!seenRef.current.has(key)) {
        seenRef.current.add(key);
        onFinishedEntry?.(entry);
      }
    }

    const pending = (next.running || 0) + (next.queued || 0);
    if (next.total > 0 && pending === 0) {
      stop();
      return;
    }
    if (next.total === 0) {
      idleRoundsRef.current += 1;
      if (idleRoundsRef.current >= IDLE_ROUNDS_LIMIT) stop();
    } else {
      idleRoundsRef.current = 0;
    }
  }, [onFinishedEntry, stop]);

  // 外部登记了新项 ⇒ 开轮询（已在轮询则不重复开）
  useEffect(() => {
    if (activeItemIds.size === 0) return;
    if (timerRef.current !== null) return;
    idleRoundsRef.current = 0;
    setPolling(true);
    void tick();
    timerRef.current = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeItemIds, tick]);

  useEffect(() => stop, [stop]);

  if (!polling && snapshot.total === 0) {
    // 空闲且从没跑过 ⇒ 不占版面（空态不渲染一个「0 0 0」的卡片）
    return null;
  }

  const today = snapshot.today;
  const pending = (snapshot.running || 0) + (snapshot.queued || 0);
  const done = today ? today.ok : snapshot.generated;
  const failed = today ? today.failed : snapshot.failed;
  const total = done + failed + pending;
  const percent = total > 0 ? Math.round(((done + failed) / total) * 100) : 0;

  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      <Space size={24}>
        <Statistic title="生成成功" value={done} valueStyle={{ fontSize: 18 }} />
        <Statistic title="失败" value={failed} valueStyle={{ fontSize: 18 }} />
        <Statistic title="待处理" value={pending} valueStyle={{ fontSize: 18 }} />
        {polling && <Text type="secondary" style={{ fontSize: 12 }}>生成中…</Text>}
      </Space>
      <Progress percent={percent} status={polling ? "active" : "normal"} size="small" />
      <Text type="secondary" style={{ fontSize: 11 }}>
        仅当日数据 · 队列每 5 秒刷新
      </Text>
    </Space>
  );
}
