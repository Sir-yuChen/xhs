/**
 * 工作流纯逻辑用例（设计 19 / M5）。
 *
 * 五组判据各有守卫，且都能被变异抓住：
 * 1. **表外值原样显示**（不造名，DEF-057 同型）；
 * 2. **未结束 ⇒ 耗时 `-`**（不补 0，缺值口径）；
 * 3. `paused` = 待人工审核（§3.3 硬约束：不自动放行）；
 * 4. 只有活跃 run 可取消 / 失败或取消可重跑；
 * 5. **同 `seq` 次序稳定**（否则刷新一次链就跳）。
 */

import { describe, expect, it } from "vitest";

import {
  canCancelRun,
  canRerunNode,
  chainText,
  durationText,
  needsHumanReview,
  nodeLabel,
  runStatusColor,
  runStatusText,
  selectableNodeTypes,
  sortChain,
  triggerText,
  workflowStatusText,
} from "./workflow-format";

describe("节点标签", () => {
  it("认识的类型给中文名", () => {
    expect(nodeLabel("trigger")).toBe("触发器");
    expect(nodeLabel("review")).toBe("人工审核");
    expect(nodeLabel("monitor")).toBe("监控回填");
  });

  it("⚠️ 表外值【原样返回】，不造名", () => {
    // 变异守护：把兜底改成「未知节点」⇒ 本用例必红。
    // 理由：用户看到 foo_bar 比看到「未知」有用 —— 后者把
    // 「后端加了新类型」伪装成「前端有 bug」。
    expect(nodeLabel("brand_new_node")).toBe("brand_new_node");
    expect(nodeLabel("")).toBe("");
  });

  it("trigger 已存在时不再出现在可选项里（链首唯一，§3.1）", () => {
    expect(selectableNodeTypes([])).toContain("trigger");
    expect(selectableNodeTypes(["trigger", "topic"])).not.toContain("trigger");
    // 其它类型不受影响
    expect(selectableNodeTypes(["trigger"])).toContain("generate");
  });
});

describe("状态文案", () => {
  it("认识的状态给中文", () => {
    expect(workflowStatusText("enabled")).toBe("已启用");
    expect(runStatusText("paused")).toBe("待审核");
    expect(runStatusText("succeeded")).toBe("成功");
  });

  it("⚠️ 表外状态原样显示", () => {
    expect(runStatusText("weird_state")).toBe("weird_state");
    expect(workflowStatusText("archived")).toBe("archived");
  });

  it("配色是纯展示，不认识的走 default", () => {
    expect(runStatusColor("succeeded")).toBe("success");
    expect(runStatusColor("failed")).toBe("error");
    expect(runStatusColor("nope")).toBe("default");
  });
});

describe("可操作性判据", () => {
  it("只有活跃 run 可取消", () => {
    expect(canCancelRun("pending")).toBe(true);
    expect(canCancelRun("running")).toBe(true);
    expect(canCancelRun("paused")).toBe(true);
    for (const done of ["succeeded", "failed", "cancelled"]) {
      expect(canCancelRun(done)).toBe(false);
    }
  });

  it("⚠️ paused = 待人工审核（§3.3：默认人工、不自动放行）", () => {
    // 变异守护：把 needsHumanReview 改成 always false ⇒ 本用例必红。
    expect(needsHumanReview("paused")).toBe(true);
    for (const s of ["running", "succeeded", "failed", "pending", "cancelled"]) {
      expect(needsHumanReview(s)).toBe(false);
    }
  });

  it("只有失败 / 已取消的 run 可重跑节点", () => {
    expect(canRerunNode("failed")).toBe(true);
    expect(canRerunNode("cancelled")).toBe(true);
    expect(canRerunNode("succeeded")).toBe(false);
    expect(canRerunNode("running")).toBe(false);
  });
});

describe("触发器文案", () => {
  it("三种触发器各有文案", () => {
    expect(triggerText("cron", "0 0 9 * * *")).toBe("定时 0 0 9 * * *");
    expect(triggerText("cron", "")).toBe("定时");
    expect(triggerText("at", "")).toBe("一次性");
    expect(triggerText("manual", "")).toBe("手动");
  });

  it("⚠️ 表外触发器原样显示", () => {
    expect(triggerText("webhook", "")).toBe("webhook");
  });
});

describe("耗时", () => {
  it("正常区间给秒 / 分", () => {
    expect(durationText("2026-10-04T10:00:00", "2026-10-04T10:00:42")).toBe("42 秒");
    expect(durationText("2026-10-04T10:00:00", "2026-10-04T10:02:05")).toBe("2 分 5 秒");
    expect(durationText("2026-10-04T10:00:00", "2026-10-04T10:03:00")).toBe("3 分");
  });

  it("⚠️ 未结束 ⇒ `-`，【不补 0】（缺值口径）", () => {
    // 变异守护：把 startedAt 有值但 finishedAt 为空的分支改成返回 "0 秒" ⇒ 必红。
    // 「还在跑」与「跑了 0 秒」是两件事。
    expect(durationText("2026-10-04T10:00:00", null)).toBe("-");
    expect(durationText(null, "2026-10-04T10:00:00")).toBe("-");
    expect(durationText(null, null)).toBe("-");
  });

  it("坏时间戳 / 倒序 ⇒ `-`（不抛异常）", () => {
    expect(durationText("不是时间", "2026-10-04T10:00:00")).toBe("-");
    expect(durationText("2026-10-04T10:00:05", "2026-10-04T10:00:00")).toBe("-");
  });
});

describe("节点链", () => {
  const mk = (key: string, seq: number, name = "") => ({
    node_key: key,
    node_type: key,
    node_name: name,
    enabled: true,
    seq,
  });

  it("按 seq 排序并补中文名", () => {
    const chain = sortChain([mk("publish", 3), mk("trigger", 0), mk("generate", 2)]);
    expect(chain.map((n) => n.node_type)).toEqual(["trigger", "generate", "publish"]);
    expect(chain[0].node_name).toBe("触发器");
    expect(chain[2].node_name).toBe("发布");
  });

  it("⚠️ 同 seq 次序【稳定】（node_key 兜底）", () => {
    // 变异守护：去掉 `|| a.node_key.localeCompare(...)` ⇒ 本用例必红。
    // 只按 seq 排时同序号项顺序由后端返回顺序决定，刷新一次链就跳。
    const a = sortChain([mk("bbb", 1), mk("aaa", 1), mk("ccc", 1)]);
    const b = sortChain([mk("ccc", 1), mk("bbb", 1), mk("aaa", 1)]);
    expect(a.map((n) => n.node_key)).toEqual(b.map((n) => n.node_key));
    expect(a.map((n) => n.node_key)).toEqual(["aaa", "bbb", "ccc"]);
  });

  it("链文本用 ` → ` 连接；停用节点标注；空链有兜底文案", () => {
    expect(chainText(sortChain([mk("trigger", 0), mk("topic", 1)]))).toBe("触发器 → 选题");
    const withRule = sortChain([{ ...mk("trigger", 0), enabled: false }, mk("topic", 1)]);
    expect(chainText(withRule)).toBe("触发器(停用) → 选题");
    expect(chainText([])).toBe("（无节点）");
  });

  it("不改动入参数组（纯函数）", () => {
    const input = [mk("b", 2), mk("a", 1)];
    const before = input.map((n) => n.node_key);
    sortChain(input);
    expect(input.map((n) => n.node_key)).toEqual(before);
  });
});
