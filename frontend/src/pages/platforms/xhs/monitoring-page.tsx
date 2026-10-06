import {
  DeleteOutlined,
  PauseOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  List,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useEffect, useState } from "react";

import { useThemePalette } from "../../../app/providers";
import {
  createMonitoringTarget,
  deleteMonitoringTarget,
  fetchMonitoringSnapshots,
  fetchMonitoringTargetNotes,
  fetchMonitoringTargets,
  refreshMonitoringTarget,
  setMonitoringTargetStatus,
} from "../../../lib/api";
import { formatShanghaiTime } from "../../../lib/time";
import type {
  MonitoringNote,
  MonitoringSnapshot,
  MonitoringTarget,
  MonitoringTargetPayload,
} from "../../../types";

const { Text } = Typography;

const targetTypeLabels: Record<string, string> = {
  keyword: "关键词",
  account: "账号",
  brand: "品牌",
  note_url: "笔记 URL",
};

const targetTypeOptions = [
  { label: "关键词", value: "keyword" },
  { label: "账号", value: "account" },
  { label: "品牌", value: "brand" },
  { label: "笔记 URL", value: "note_url" },
];

function formatTime(value?: string | null): string {
  return formatShanghaiTime(value);
}

function snapshotMetric(
  snapshot?: MonitoringSnapshot,
  key?: "matched_count" | "total_engagement"
): number {
  const value = key ? snapshot?.payload?.[key] : undefined;
  return typeof value === "number" ? value : 0;
}

export function XhsMonitoringPage() {
  const p = useThemePalette();
  const [targets, setTargets] = useState<MonitoringTarget[]>([]);
  const [snapshotsByTarget, setSnapshotsByTarget] = useState<
    Record<number, MonitoringSnapshot>
  >({});
  const [notesByTarget, setNotesByTarget] = useState<
    Record<number, MonitoringNote[]>
  >({});
  const [targetType, setTargetType] =
    useState<MonitoringTargetPayload["target_type"]>("note_url");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isWorking, setIsWorking] = useState(false);
  // 全站消息提醒统一走 antd message（设计 04 §3.6，2026-09-25 用户口径）：
  // 操作结果反馈不再渲染 inline Alert 占版面。
  const { message } = App.useApp();

  async function loadTargets() {
    setIsLoading(true);
    try {
      const result = await fetchMonitoringTargets();
      setTargets(result.items);
      const snapshotResults = await Promise.all(
        result.items.map(async (target) => {
          try {
            const snapshots = await fetchMonitoringSnapshots(target.id);
            return [target.id, snapshots.items[0]] as const;
          } catch {
            return [target.id, undefined] as const;
          }
        })
      );
      setSnapshotsByTarget(
        Object.fromEntries(
          snapshotResults.filter(
            (entry): entry is readonly [number, MonitoringSnapshot] =>
              Boolean(entry[1])
          )
        )
      );
      const noteResults = await Promise.all(
        result.items.map(async (target) => {
          try {
            const notes = await fetchMonitoringTargetNotes(target.id);
            return [target.id, notes.items] as const;
          } catch {
            return [target.id, []] as const;
          }
        })
      );
      setNotesByTarget(Object.fromEntries(noteResults));
    } catch {
      message.error("监控目标加载失败。");
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    void loadTargets();
  }, []);

  async function createTarget() {
    const trimmedValue = value.trim();
    if (!trimmedValue) {
      message.warning("请输入监控值。");
      return;
    }
    setIsWorking(true);
    try {
      const created = await createMonitoringTarget({
        target_type: targetType,
        name: name.trim() || trimmedValue,
        value: trimmedValue,
        status: "active",
        config: {},
      });
      setTargets((currentTargets) => [created, ...currentTargets]);
      setName("");
      setValue("");
      message.success("监控目标已创建。");
    } catch {
      message.error("监控目标创建失败。");
    } finally {
      setIsWorking(false);
    }
  }

  async function refreshTarget(targetId: number) {
    setIsWorking(true);
    try {
      const result = await refreshMonitoringTarget(targetId);
      setTargets((currentTargets) =>
        currentTargets.map((target) =>
          target.id === result.target.id ? result.target : target
        )
      );
      setSnapshotsByTarget((currentSnapshots) => ({
        ...currentSnapshots,
        [result.target.id]: result.snapshot,
      }));
      const notes = await fetchMonitoringTargetNotes(result.target.id);
      setNotesByTarget((currentNotes) => ({
        ...currentNotes,
        [result.target.id]: notes.items,
      }));
      message.success(`刷新任务已创建：#${result.task.id}`);
    } catch {
      message.error("刷新任务创建失败。");
    } finally {
      setIsWorking(false);
    }
  }

  async function removeTarget(targetId: number) {
    setIsWorking(true);
    try {
      await deleteMonitoringTarget(targetId);
      setTargets((currentTargets) =>
        currentTargets.filter((target) => target.id !== targetId)
      );
      setSnapshotsByTarget((currentSnapshots) => {
        const nextSnapshots = { ...currentSnapshots };
        delete nextSnapshots[targetId];
        return nextSnapshots;
      });
      setNotesByTarget((currentNotes) => {
        const nextNotes = { ...currentNotes };
        delete nextNotes[targetId];
        return nextNotes;
      });
      message.success("监控目标已删除。");
    } catch {
      message.error("监控目标删除失败。");
    } finally {
      setIsWorking(false);
    }
  }

  /**
   * 暂停 / 恢复监控目标（DEF-012 / T-112）。
   *
   * ⚠️ 暂停后巡检**跳过**它（`monitoring_crawl_service` 只取 `status == active`），
   * 但**历史快照与命中笔记都保留** —— 恢复后接得上，不是删除。
   */
  async function toggleTargetStatus(target: MonitoringTarget) {
    const nextStatus = target.status === "active" ? "paused" : "active";
    setIsWorking(true);
    try {
      const updated = await setMonitoringTargetStatus(target.id, nextStatus);
      setTargets((currentTargets) =>
        currentTargets.map((item) => (item.id === updated.id ? updated : item))
      );
      message.success(nextStatus === "paused" ? "已暂停监控。" : "已恢复监控。");
    } catch {
      message.error(nextStatus === "paused" ? "暂停失败。" : "恢复失败。");
    } finally {
      setIsWorking(false);
    }
  }

  return (
    <div>
      {/* 2026-10-05 用户口径「移除所有页面页面头」：原「XHS Monitoring / 竞品监控 /
          一句话说明」三行整块移除（页面标题与侧栏「监控中心」语义重复，合并后本页
          本就覆盖 4 种目标类型）；「刷新」收进下方新增目标卡右上角。 */}
      <Card
        style={{ background: p.cardBg, borderColor: p.border, marginBottom: 24 }}
        title="新增监控目标"
        extra={
          <Button
            size="small"
            icon={<ReloadOutlined />}
            onClick={loadTargets}
            loading={isLoading}
          >
            刷新
          </Button>
        }
      >
        <Form layout="inline" style={{ flexWrap: "wrap", gap: 8 }}>
          <Form.Item>
            <Select
              value={targetType}
              onChange={(val) =>
                setTargetType(val as MonitoringTargetPayload["target_type"])
              }
              options={targetTypeOptions}
              disabled={isLoading}
              style={{ width: 120 }}
            />
          </Form.Item>
          <Form.Item>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="目标名称，可选"
              disabled={isLoading}
              style={{ width: 180 }}
            />
          </Form.Item>
          <Form.Item>
            <Input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="关键词、账号 ID、品牌或笔记 URL"
              disabled={isLoading}
              style={{ width: 280 }}
            />
          </Form.Item>
          <Form.Item>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => void createTarget()}
              disabled={isWorking}
            >
              添加目标
            </Button>
          </Form.Item>
        </Form>
      </Card>

      {isLoading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin tip="正在加载监控目标..." />
        </div>
      ) : targets.length === 0 ? (
        <Card style={{ background: p.cardBg, borderColor: p.border }}>
          <Empty description="暂无监控目标。" />
        </Card>
      ) : (
        <Row gutter={[16, 16]}>
          {targets.map((target) => (
            <Col xs={24} md={12} key={target.id}>
              <Card
                title={
                  <Space>
                    <Text strong>{target.name || target.value}</Text>
                    <Tag color={target.status === "active" ? "green" : "default"}>
                      {target.status}
                    </Tag>
                  </Space>
                }
                style={{ background: p.cardBg, borderColor: p.border }}
              >
                <Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
                  {target.value}
                </Text>
                <div style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ marginRight: 16 }}>
                    类型：{targetTypeLabels[target.target_type] ?? target.target_type}
                  </Text>
                  <Text type="secondary" style={{ marginRight: 16 }}>
                    最近刷新：{formatTime(target.last_refreshed_at)}
                  </Text>
                  <Text type="secondary">
                    创建时间：{formatTime(target.created_at)}
                  </Text>
                </div>

                {snapshotsByTarget[target.id] && (
                  <Row gutter={16} style={{ marginBottom: 16 }}>
                    <Col span={8}>
                      <Statistic
                        title="匹配"
                        value={snapshotMetric(snapshotsByTarget[target.id], "matched_count")}
                        suffix="条"
                        valueStyle={{ fontSize: 16 }}
                      />
                    </Col>
                    <Col span={8}>
                      <Statistic
                        title="互动"
                        value={snapshotMetric(snapshotsByTarget[target.id], "total_engagement")}
                        valueStyle={{ fontSize: 16 }}
                      />
                    </Col>
                    <Col span={8}>
                      <Statistic
                        title="快照时间"
                        value={formatTime(snapshotsByTarget[target.id].created_at)}
                        valueStyle={{ fontSize: 12 }}
                      />
                    </Col>
                  </Row>
                )}

                {notesByTarget[target.id]?.length ? (
                  <List
                    size="small"
                    dataSource={notesByTarget[target.id].slice(0, 3)}
                    style={{ marginBottom: 16 }}
                    renderItem={(note) => (
                      <List.Item>
                        <List.Item.Meta
                          title={note.title || note.note_id}
                          description={`${note.author_name || "未知作者"} · 互动 ${note.engagement}`}
                        />
                      </List.Item>
                    )}
                  />
                ) : null}

                <Space>
                  <Button
                    icon={<SyncOutlined />}
                    onClick={() => void refreshTarget(target.id)}
                    disabled={isWorking}
                  >
                    手动刷新
                  </Button>
                  {/* DEF-012 / T-112：暂停 / 恢复（后端 `status` 早就支持，缺的是入口） */}
                  <Tooltip
                    title={
                      target.status === "active"
                        ? "暂停后巡检跳过该目标，历史快照与命中笔记保留"
                        : "恢复后巡检重新纳入该目标"
                    }
                  >
                    <Button
                      icon={target.status === "active" ? <PauseOutlined /> : <PlayCircleOutlined />}
                      onClick={() => void toggleTargetStatus(target)}
                      disabled={isWorking}
                    >
                      {target.status === "active" ? "暂停" : "恢复"}
                    </Button>
                  </Tooltip>
                  <Button
                    icon={<DeleteOutlined />}
                    danger
                    onClick={() => void removeTarget(target.id)}
                    disabled={isWorking}
                  >
                    删除
                  </Button>
                </Space>
              </Card>
            </Col>
          ))}
        </Row>
      )}
    </div>
  );
}
