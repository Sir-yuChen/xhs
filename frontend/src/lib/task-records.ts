/**
 * 队列记录（设计 13 §35.11，2026-09-25 用户口径「队列记录中，记录的是队列中每一条
 * 记录的详情以及执行情况数据」；§31.10 起新增商品生成队列）的**纯逻辑**：类型中文名、
 * 状态映射、可取消 / 可重试判据、缺值口径、来源、**批次父子树的呈现判据**、
 * **账号列判据**（2026-09-26 用户口径「批次记录执行的账号」）。
 *
 * ⚠️ **这个页只收「队列」**（`QUEUE_TASK_TYPES`：三个队列各自的**队列项** + **批次
 * 父行**）—— 账号级全量同步（`xhs_note_sync`）、店铺商品同步、AI 生成、抓取 / 监控
 * **不是队列**，它们的执行情况在账号卡片 / 任务中心的任务日志看。混进来就会把
 * 这个页变成「另一个任务日志」（2026-09-25 用户当场纠正）。
 *
 * ⚠️ **批次形态（2026-09-26 两轮口径，后者覆盖前者）**：
 * 早间口径「不需要队列批次，队列是常驻，只需要队列详情记录即可」把批次层删了；
 * 当日晚间口径「**将队列执行详情表使用批次+详情父子树风格展示，批次逻辑不再移除，
 * 生成唯一批次号**」把批次层请回来 —— 形态是**一次入队动作 = 一个批次**（一次点击
 * / 一次 API 调用一个号），批次行是父节点、队列项是它的子节点。
 * 所以本文件的队列类型白名单**必须同时收批次类型**，且「这一行是不是批次」的判据
 * 走 `batch_no`（数据判据）而不是类型清单 —— 新队列上线后端自动生成批次号，
 * 前端零改动。**不做存量兼容**（2026-09-26 用户口径「不做兼容逻辑，数据已清空」）：
 * 列表顶级行只有批次行，明细一律挂在父行的 `children` 里（父子树，不摊平）。
 *
 * 为什么单独一个文件：页面（`pages/tasks/task-records-page.tsx`）要渲染这些，
 * 行为脚本（`tests/frontend/task_records.cjs`）要**打包这一个文件**跑真实断言 ——
 * 页面 tsx 里塞逻辑就没办法在不渲染的情况下验证（DEF-044：同一语义不留两份实现）。
 */

/** 队列**项**类型（一行 = 一篇笔记 / 一个商品；worker 认领只认这三个）。 */
export const QUEUE_ITEM_TASK_TYPES: readonly string[] = [
  "xhs_note_sync_item",
  "xhs_note_publish_item",
  "xhs_note_gen_item",
];

/**
 * 队列**批次父行**类型（一次入队动作 = 一个唯一批次号；批次行**永不被执行**）。
 *
 * ⚠️ 2026-09-27：详情补齐队列（`xhs_note_hydrate_item` / `xhs_note_hydrate_batch`）
 * **整条移除**（用户口径「账号信息卡中移除补齐详情逻辑……不需要再点击补齐详情」）——
 * 补齐能力并入同步任务本身（设计 13 §35.18），后端 `QUEUE_REGISTRY` 只剩三个队列。
 */
export const QUEUE_BATCH_TASK_TYPES: readonly string[] = [
  "xhs_note_sync_batch",
  "xhs_note_publish_batch",
  "xhs_note_gen_batch",
];

/** 队列记录页认的全部类型（队列项 + 批次行）。 */
export const QUEUE_TASK_TYPES: readonly string[] = [
  ...QUEUE_ITEM_TASK_TYPES,
  ...QUEUE_BATCH_TASK_TYPES,
];

/** 商品生成队列的队列项类型（生成前笔记不存在 ⇒ 显示商品而不是拉笔记标题）。 */
export const GEN_ITEM_TASK_TYPE = "xhs_note_gen_item";

/*
 * ⚠️ **`ACCOUNT_TASK_TYPES` 已删除**（2026-09-27 用户口径「任务中心移除账号级任务 (1) tab」）：
 * 它是「账号级任务」页签的筛选白名单（`GET /tasks?task_types=…`），页签移除后没有调用方。
 * 后端仍保留 `ACCOUNT_TASK_TYPES`（账号任务 worker 的认领 / 复位 / 保留期用它），
 * 账号级任务行仍可在队列记录页查看与中断（`QUEUE_RECORD_TYPES` 含它们）。
 */

/** 是不是**队列**项（最细粒度的一行；批次父行不是）。 */
export function isQueueItemTask(taskType: string): boolean {
  return QUEUE_ITEM_TASK_TYPES.includes(taskType);
}

/** 是不是队列**批次父行**（一次入队动作的记账行）。 */
export function isQueueBatchTask(taskType: string): boolean {
  return QUEUE_BATCH_TASK_TYPES.includes(taskType);
}

/** 是不是队列记录页该显示的类型（队列项 + 批次行）。 */
export function isQueueTaskType(taskType: string): boolean {
  return QUEUE_TASK_TYPES.includes(taskType);
}

/**
 * 这一行是不是**批次父行**：判据是 `batch_no` 有值（**不是**类型清单）。
 *
 * 为什么用数据判据：批次号由后端按注册表生成，新队列上线后前端不必改一行就能把
 * 它的批次行渲染成父节点（写死类型清单 = 新队列的批次行被当成普通行，父子树塌成
 * 平铺）。明细行（队列项）**不携带批次号** ⇒ 判 false，于是「批次号」与
 * 「批次创建时间」两列只对父节点渲染（2026-09-26 用户口径「批次号 / 记录列只展示
 * 批次行」）；列表顶级行也只有批次行（后端只下发批次行，不做存量兼容）。
 */
export function isBatchRow(row: { batch_no?: string | null } | null | undefined): boolean {
  return Boolean(String(row?.batch_no ?? "").trim());
}

/** 任务类型中文名（**不猜**：表里没有的类型原样显示，别造一个好看的名字）。 */
export const TASK_TYPE_LABELS: Record<string, string> = {
  // 笔记队列（`services/note_queue_service.py`，持久化底座）
  xhs_note_sync_item: "同步笔记",
  xhs_note_publish_item: "发布笔记",
  xhs_note_gen_item: "生成商品笔记",
  // 队列**批次父行**（一次入队动作 = 一个唯一批次号；2026-09-26 批次层）
  xhs_note_sync_batch: "同步批次",
  xhs_note_publish_batch: "发布批次",
  xhs_note_gen_batch: "生成批次",
  // 账号级全量同步 / 商品同步
  xhs_note_sync: "账号全量同步",
  xhs_shop_goods_sync: "店铺商品同步",
  // 采集 / 监控
  crawl: "数据抓取",
  monitoring_crawl: "监控抓取",
  monitoring_refresh: "监控刷新",
  xhs_no_traffic_cleanup: "零流量清理",
  // 发布链路（定时发布）
  creator_publish: "创作者发布",
  creator_publish_scheduler: "定时发布",
  // AI 生成
  ai_generate_note: "AI 生成笔记",
  ai_generate_tags: "AI 生成标签",
  ai_generate_title: "AI 生成标题",
  ai_polish_text: "AI 润色正文",
  ai_rewrite: "AI 改写",
  ai_image_describe: "AI 图片描述",
  ai_image_generate: "AI 生成图片",
  ai_image_generate_cover: "AI 生成封面",
};

/** 任务记录分组（筛选下拉用；与任务类型一一对应，不是新概念）。 */
export type TaskRecordGroup = "sync" | "publish" | "ai" | "crawl" | "other";

export const TASK_RECORD_GROUP_LABELS: Record<TaskRecordGroup, string> = {
  sync: "同步",
  publish: "发布",
  ai: "AI 生成",
  crawl: "抓取 / 监控",
  other: "其它",
};

/** 任务状态中文名（`tasks.status` 的既有取值，不新增状态）。 */
export const TASK_STATUS_LABELS: Record<string, string> = {
  pending: "待处理",
  running: "进行中",
  done: "成功",
  failed: "失败",
  cancelled: "已取消",
  exhausted: "重试用尽",
};

/** 任务状态颜色（antd 预设色名；失败 / 用尽都是红，取消是橙）。 */
export const TASK_STATUS_COLORS: Record<string, string> = {
  pending: "default",
  running: "processing",
  done: "success",
  failed: "error",
  cancelled: "warning",
  exhausted: "error",
};

/**
 * 任务**来源**中文名（`tasks.payload.source`，既有字段，不是新概念）。
 *
 * 为什么必须显示：定时任务触发的那一层（`system.note_sync` / 商品同步 / 监控刷新）
 * **同时**写两处 —— 调度器写一条 `scheduled_job_logs`（任务中心的「任务日志」），
 * 业务层给每个账号写一条 `tasks` 行（本页）。不显示来源，用户看到自己没点过的记录
 * 会以为系统在偷偷跑（2026-09-25 用户口径「显示来源，使用 tip 展示说明」）。
 *
 * ⚠️ 2026-09-26 用户两次口径：「来源列不能是未知，你排查所有来源后给每个来源固定值」
 * +「来源不能是简单的手动或自动，应具体到按钮」⇒ 取值**具体到触发它的那个按钮**，
 * 与后端 `note_queue_service.QUEUE_SOURCES` 一一对应（新入口两边一起加，不许留空）：
 *
 * | 值 | 中文名 | 入口 |
 * |---|---|---|
 * | `note_sync_single` | 内容库-同步按钮 | 内容库单条「同步」 |
 * | `note_sync_batch` | 内容库-批量同步按钮 | 内容库「批量同步」 |
 * | `note_publish_single` | 内容库-发布按钮 | 内容库单条「发布」 |
 * | `note_publish_batch` | 内容库-批量发布按钮 | 内容库「批量发布」 |
 * | `product_note_generate_single` | 商品列表-生成笔记按钮 | 商品列表单条「生成笔记」 |
 * | `product_note_generate_batch` | 商品列表-批量生成笔记按钮 | 商品列表「批量生成智能笔记」 |
 * | `account_card_note_sync` | 账号卡-同步笔记按钮 | 账号卡「同步笔记」（等价手动跑一次定时任务） |
 * | `account_card_goods_note_sync` | 账号卡-同步商品笔记按钮 | 账号卡「**同步商品笔记**」（§44 拆出的独立任务；与下一行**不是一回事**） |
 * | `account_card_goods_sync` | 账号卡-同步商品按钮 | 账号卡「同步商品」（店铺商品台账，同上） |
 * | `retry` | 队列记录-重试按钮 | 本页点「重试」 |
 * | `recovery` | 应用重启恢复 | 启动恢复自动续跑的断点行 |
 * | `scheduled` | 定时任务 | 预留（队列侧暂无入口） |
 * | `manual` | 页面按钮（早期数据） | **遗留值**：按钮级细分之前入队的行（迁移
 *   `d3e4f5a6b7c8` 回填的），代码不再写它；显示时说清是早期数据，别冒充具体按钮 |
 *
 * ⚠️ **「一一对应」由守卫强制**（2026-09-28）：`tests/backend/test_note_queue_service.py::
 * test_all_queue_sources_have_frontend_labels` 会**遍历 `QUEUE_SOURCES`** 逐项检查本表
 * 与 `TASK_SOURCE_TIPS` 都有键。加后端来源而忘加这里 ⇒ 用例直接红。
 * （此前该守卫手写清单，漏了上面两个账号卡来源 —— 页面显示英文原值而无人察觉。）
 */
export const TASK_SOURCE_LABELS: Record<string, string> = {
  note_sync_single: "内容库-同步按钮",
  note_sync_batch: "内容库-批量同步按钮",
  note_publish_single: "内容库-发布按钮",
  note_publish_batch: "内容库-批量发布按钮",
  product_note_generate_single: "商品列表-生成笔记按钮",
  product_note_generate_batch: "商品列表-批量生成笔记按钮",
  product_note_generate_llm_single: "商品列表-LLM生成笔记按钮",
  product_note_generate_llm_batch: "商品列表-批量LLM生成笔记按钮",
  llm_generate_retry: "生成记录-重试按钮",
  account_card_note_sync: "账号卡-同步笔记按钮",
  account_card_goods_note_sync: "账号卡-同步商品笔记按钮",
  account_card_goods_sync: "账号卡-同步商品按钮",
  retry: "队列记录-重试按钮",
  recovery: "应用重启恢复",
  scheduled: "定时任务",
  // 遗留值（迁移 d3e4f5a6b7c8 回填的存量行）：按钮级细分之前的「手动」，
  // 代码不再写它。说清是早期数据，不冒充某个具体按钮。
  manual: "页面按钮（早期数据）",
};

/**
 * 来源的**说明**（Tooltip 用，不占版面 —— 设计 04 §3.6）。
 * 顺带把两页分工讲清：本页 = 「跑完了什么」，任务中心 = 「什么时候跑」。
 */
export const TASK_SOURCE_TIPS: Record<string, string> = {
  note_sync_single: "你在内容库点单条「同步」按钮入队的。",
  note_sync_batch: "你在内容库点「批量同步」入队的（一次多篇）。",
  note_publish_single: "你在内容库点单条「发布」按钮入队的。",
  note_publish_batch: "你在内容库点「批量发布」入队的（一次多篇）。",
  product_note_generate_single: "你在商品列表点「生成笔记」按钮入队的（一个商品 = 一条记录）。",
  product_note_generate_batch: "你在商品列表点「批量生成智能笔记」入队的（一次多个商品）。",
  product_note_generate_llm_single:
    "你在商品列表点「LLM 生成笔记」按钮入队的（走提示词模板 + 大模型生成，一个商品 = 一条记录）。",
  product_note_generate_llm_batch:
    "你在商品列表点「批量 LLM 生成笔记」入队的（走提示词模板 + 大模型生成，一次多个商品）。",
  llm_generate_retry:
    "你在「生成记录」里点「重试」重新入队的（这一跑来自重试，不是当初入队那次）。",
  account_card_note_sync: "你在账号卡上点「同步笔记」触发的（等价于手动跑一次定时任务，不与笔记队列抢平台额度）。",
  account_card_goods_note_sync:
    "你在账号卡上点「同步商品笔记」触发的（千帆挂链笔记，等价于手动跑一次 system.goods_note_sync）。"
    + "⚠️ 与「同步商品」（店铺商品台账 / SKU 价格）是两件事。",
  account_card_goods_sync: "你在账号卡上点「同步商品」触发的（等价于手动跑一次定时任务）。",
  retry: "你在队列记录里点「重试」重新入队的（这一跑来自重试，不是当初入队那次）。",
  recovery: "应用重启后由启动恢复自动续跑的断点（上次退出时没跑完的那些，不是新任务）。",
  scheduled:
    "定时任务触发 —— 同一次执行在「任务中心 · 任务日志」里还有一条调度记录（那边记「什么时候跑」，本页记「跑完了什么」）。",
  manual:
    "早期数据（来源按按钮细分之前入队的）：确实是从页面上点出来的，但那时还不记录具体是哪个按钮。",
};

/**
 * 来源中文名（表外原样返回，同 `taskTypeLabel` 的口径：不造名）。
 *
 * ⚠️ **空值兜底也不是「未知」**（2026-09-26 用户口径）：来源按按钮细分之前入队的
 * 存量行没有 `source`（与迁移回填的 `manual` 同一类）⇒ 归到「页面按钮（早期数据）」，
 * 靠 Tooltip 说明「早期数据」—— 显示「未知」等于把已知的事说成不知道。
 */
export function taskSourceLabel(source?: string | null): string {
  const key = String(source ?? "").trim();
  if (!key) return TASK_SOURCE_LABELS.manual;
  return TASK_SOURCE_LABELS[key] || key;
}

/** 来源说明（Tooltip 文案；表外给一句通用说明，不返回空串）。 */
export function taskSourceTip(source?: string | null): string {
  const key = String(source ?? "").trim() || "manual";
  return TASK_SOURCE_TIPS[key] || `来源代码：${key}`;
}

/** 未终态：可取消（后端 `POST /tasks/{id}/cancel` 的条件更新只动这两态）。 */
export const TASK_OPEN_STATUSES = ["pending", "running"] as const;

/** 可重试：后端只把 `failed` / `exhausted` 复位成 `pending`。 */
export const TASK_RETRYABLE_STATUSES = ["failed", "exhausted"] as const;

/**
 * **可手动清理**的状态（2026-09-26 用户口径「新增手动批量清理……清理逻辑不包含
 * 待重试和进行中状态的记录，前端不允许被选中」）。
 *
 * 与后端 `note_queue_service.CLEANABLE_STATUSES` 一一对应（测试钉死防漂移）：
 * 只有 `done` / `cancelled` 能清 —— 待重试（`failed` / `exhausted`，带重试按钮）
 * 与进行中（`pending` / `running`，活的队列项）删了就是夺用户的自救路径 / 丢任务。
 *
 * ⚠️ **清理有两个维度**（2026-09-26 用户口径「支持按照批次清理记录……批次中存在
 * 正在执行或待执行得记录，则不支持该批次全部清理……分为两个维度，批次维度和批次内
 * 的操作」）：
 *
 * * **批次维度**（点名批次行）＝ **原子**整批清理 ⇒ 批内只要有「正在执行 / 待执行」
 *   或「待重试」的明细就**不支持**（原因文案走 `batchCleanBlockReason`，与后端
 *   `batch_clean_blockers` 同判据）。批量/整批**不允许半清**：清一半会在父子树里
 *   留下一个残缺批次，用户看不懂；
 * * **批次内维度**（点名明细行）＝ 单条清，判据就是这一个函数。
 */
export const CLEANABLE_STATUSES = ["done", "cancelled"] as const;

/** 这条记录能不能被手动清理（复选框禁用 / 清理按钮灰掉的判据）。 */
export function canCleanTask(status: string): boolean {
  return (CLEANABLE_STATUSES as readonly string[]).includes(status);
}

/**
 * **批次维度**清理的阻塞原因：`null` = 可以整批清理；字符串 = 原因（直接用作按钮
 * 禁用时的 Tooltip 文案）。
 *
 * 计数一律取后端 `summary`（`batch_rollup` 唯一实现），前端不重算。`failed` 里已并入
 * `exhausted`（后端 `batch_summary` 的算法），所以这里只看 `failed` 一个键。
 *
 * ⚠️ 与后端 `note_queue_service.batch_clean_blockers` **同一个判据**：
 * `pending` / `running` = 正在执行 / 待执行（用户点名的两类）；`failed` / `exhausted`
 * = 待重试（删了用户失去自救路径）。前端禁用是体验，后端拒绝是边界（DEF-048）。
 */
export function batchCleanBlockReason(batch: {
  summary?: QueueBatchSummaryShape | null;
}): string | null {
  const summary = batch.summary ?? {};
  const active = (Number(summary.pending) || 0) + (Number(summary.running) || 0);
  const retryable = Number(summary.failed) || 0;
  const parts: string[] = [];
  if (active) parts.push(`${active} 条正在执行 / 待执行`);
  if (retryable) parts.push(`${retryable} 条待重试`);
  if (!parts.length) return null;
  return `本批还有 ${parts.join(" · ")}的明细，不能整批清理；可展开明细逐条清理已完成的记录。`;
}

/** 手动清理接口的返回（`POST /tasks/clean` / `clean-all`，分两个维度报账）。 */
export type CleanResultShape = {
  cleaned?: number;
  skipped?: number;
  requested?: number;
  batches?: { cleaned?: number; blocked?: number } | null;
  items?: { cleaned?: number; skipped?: number } | null;
  blocked?: { id?: number; batch_no?: string | null; active?: number; retryable?: number }[] | null;
};

/**
 * 清理结果的用户提示（2026-09-26 用户口径「分为两个维度」）：**如实分层报账** ——
 * 清了多少（批次几个 / 明细几条）、多少因**批次维度被挡**、多少因**状态不可清被跳过**。
 * 三种都要说：只说「已清理 3 条」会让用户以为剩下的 2 条清掉了。
 *
 * 返回 `level`（页面据此选 `message.success/warning/info`）+ `text`。
 */
export function cleanOutcomeText(result: CleanResultShape): {
  level: "success" | "warning" | "info";
  text: string;
} {
  const cleaned = Number(result.cleaned) || 0;
  const batchCount = Number(result.batches?.cleaned) || 0;
  const itemCount = Number(result.items?.cleaned) || 0;
  const blockedBatches = Number(result.batches?.blocked) || 0;
  const skipped = Number(result.skipped) || 0;
  const detail = cleaned ? `（批次 ${batchCount} 个 · 明细 ${itemCount} 条）` : "";
  const why = blockedBatches
    ? `；${blockedBatches} 个批次因明细还在执行中 / 待重试，整批未清理`
    : skipped > 0
      ? `；${skipped} 条因状态不可清被跳过`
      : "";
  if (cleaned > 0) {
    return {
      level: blockedBatches || skipped ? "warning" : "success",
      text: `已清理 ${cleaned} 条记录${detail}${why}。`,
    };
  }
  if (blockedBatches || skipped) {
    return { level: "warning", text: `没有可清理的记录${why}。` };
  }
  return { level: "info", text: "没有可清理的记录。" };
}

/**
 * 类型中文名。**未知类型原样返回**（`taskTypeLabel("foo") === "foo"`）——
 * 造一个好看的名字会让用户把没见过的类型当成已知类型排查。
 */
export function taskTypeLabel(taskType: string): string {
  return TASK_TYPE_LABELS[taskType] || taskType || "未知类型";
}

/** 类型分组（筛选用）。 */
export function taskTypeGroup(taskType: string): TaskRecordGroup {
  if (taskType.startsWith("xhs_note_sync") || taskType === "xhs_shop_goods_sync") return "sync";
  if (taskType.startsWith("xhs_note_publish") || taskType.startsWith("creator_publish")) return "publish";
  if (taskType.startsWith("xhs_note_gen") || taskType.startsWith("ai_")) return "ai";
  if (taskType === "crawl" || taskType.startsWith("monitoring_") || taskType === "xhs_no_traffic_cleanup") {
    return "crawl";
  }
  return "other";
}

/** 状态中文名（未知状态原样返回，同 `taskTypeLabel` 的口径）。 */
export function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] || status || "未知状态";
}

/** 状态颜色（未知状态按 default，不红也不绿）。 */
export function taskStatusColor(status: string): string {
  return TASK_STATUS_COLORS[status] || "default";
}

/** 这条任务能不能取消（只有未终态能）。 */
export function canCancelTask(status: string): boolean {
  return (TASK_OPEN_STATUSES as readonly string[]).includes(status);
}

/** 这条任务能不能重试（只有失败 / 重试用尽能）。 */
export function canRetryTask(status: string): boolean {
  return (TASK_RETRYABLE_STATUSES as readonly string[]).includes(status);
}

/**
 * 队列项的**主体**是不是商品（商品生成队列：生成前笔记不存在 ⇒ 详情抽屉显示
 * 商品名 / 商品 ID，不去拉笔记标题 —— 拉不到的标题只会显示成 `#0`）。
 */
export function isProductGenItem(taskType: string): boolean {
  return taskType === GEN_ITEM_TASK_TYPE;
}

/**
 * 商品生成队列项的商品文案：`item_name` 优先（入队时从台账快照），缺省退
 * `item_id`，都没有 `-`（缺值口径：不猜名字）。
 */
export function genItemProductText(payload: {
  item_id?: string;
  item_name?: string;
} | null | undefined): string {
  const name = String(payload?.item_name ?? "").trim();
  if (name) return name;
  const itemId = String(payload?.item_id ?? "").trim();
  return itemId || "-";
}

/**
 * 商品生成队列项**终态后**的「生成了哪篇笔记」（worker 成功后写进行体）。
 * 没写（排队中 / 失败 / 早期数据）⇒ 空串，调用方不渲染那一行。
 */
export function genItemNoteText(payload: {
  note_title?: string;
  note_id?: string;
} | null | undefined): string {
  const title = String(payload?.note_title ?? "").trim();
  if (title) return title;
  return String(payload?.note_id ?? "").trim();
}

/**
 * 耗时渲染：毫秒 → 「1.2s / 3m 05s / 2h 14m」。**缺值一律 `-`**（缺值口径：
 * 没跑完 / 没开始的计时不是 0，别把未知说成已知）。
 */
export function formatDuration(durationMs?: number | null): string {
  if (durationMs === null || durationMs === undefined) return "-";
  const ms = Number(durationMs);
  if (!Number.isFinite(ms) || ms < 0) return "-";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(Math.floor(seconds % 60)).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** 队列项（子行）的结果摘要：成功 / 失败 + 真实原因。同步与发布同一个口径。 */
export function itemOutcomeText(payload: {
  ok?: boolean;
  reason?: string;
  code?: string;
}): string {
  if (payload.ok) return "成功";
  const reason = String(payload.reason || "").trim();
  const code = String(payload.code || "").trim();
  if (reason && code) return `${reason}（${code}）`;
  return reason || code || "失败（平台未给原因）";
}

/**
 * 列表行要不要按「有子行」渲染（`GET /tasks?tree=1` 的 `children`）。
 *
 * 2026-09-26 批次层：批次父行的 `children` 可能非空（一次动作 N 条明细）⇒ true；
 * **队列项行永远是 false**（它是最细粒度的一行，即便脏数据给了 children 也不该
 * 被当成父节点渲染 —— 那会把一条明细显示成一个可展开的批次）。
 */
export function hasChildren(taskType: string, children?: unknown[]): boolean {
  return !isQueueItemTask(taskType) && Array.isArray(children) && children.length > 0;
}

/**
 * 队列清单里本模块用到的最小结构（`QueueSummary` 的子集，纯逻辑不依赖前端类型）。
 *
 * `label` 是**队列中文名**（后端 `QUEUE_REGISTRY.label`，如「笔记同步队列」）——
 * 列表的「类型」列直接用它展示（见 `queueLabelForTask`）。
 */
export type QueueRef = {
  key: string;
  label: string;
  item_task_type: string;
  batch_task_type?: string;
};

/**
 * 这一行属于哪个队列（`null` = 不属于任何已注册队列）。
 *
 * 用后端下发的清单（`QueueSummary`：`item_task_type` + `batch_task_type`）反查，
 * 前端**不写死类型清单** ⇒ 新队列上线时筛选器自动包含它的队列项与批次行。
 * 批次行也要能落回队列，否则按队列筛选时批次行会消失（父子树只剩明细）。
 */
export function queueKeyForTask(
  taskType: string,
  queues: readonly QueueRef[]
): string | null {
  for (const queue of queues) {
    if (queue.item_task_type === taskType || queue.batch_task_type === taskType) {
      return queue.key;
    }
  }
  return null;
}

/**
 * 「类型」列对用户展示的**执行队列中文名**（2026-09-26 第二轮用户口径
 * 「队列执行记录列表的类型展示为执行队列中文展示」）。
 *
 * 与队列卡片、队列筛选器**同一份来源**：后端 `/tasks/queues` 下发的 `label`
 * （`QUEUE_REGISTRY`）—— 前端不写死队列清单，新队列上线时本列自动跟上。
 *
 * ⚠️ **不属于任何已注册队列的类型**（账号级全量同步 `xhs_note_sync`、采集 / 监控…）
 * 回退 `taskTypeLabel()`：宁可显示「账号全量同步」这类中文类型名，也不留空、
 * 不编一个并不存在的队列名（硬约束⑤：不造假）。
 */
export function queueLabelForTask(taskType: string, queues: readonly QueueRef[]): string {
  const key = queueKeyForTask(taskType, queues);
  const hit = queues.find((queue) => queue.key === key);
  return hit?.label || taskTypeLabel(taskType);
}

/**
 * **批次行筛选**（2026-09-26 用户口径「将队列执行记录表中的状态筛选放到批次内，
 * 父子树表不使用同一个表头，在表头状态列加筛选条件」）：队列 + 状态两个条件，
 * **只作用于顶级（批次）行**。
 *
 * ⚠️ 明细行**从不**参与筛选：命中批次 ⇒ 它的一整批明细原样展示。把明细再按状态
 * 筛一遍会让「共 N 条明细」变成「只显示 1 条」，与后端 `summary` 的计数对不上 ——
 * 「父子树不使用同一个表头」指的就是这件事：筛选条件是**批次（父）行**的条件。
 *
 * 唯一实现：页面的表头筛选（受控 `filteredValue`）只写回状态，真过滤在这里 ——
 * 所以页面列上**不写 `onFilter`**（写了就是同一判据两个实现，DEF-044）。
 *
 * 泛型保住调用方的行类型（页面传 `TaskRecord[]`，拿回 `TaskRecord[]`）。
 */
export function filterBatchRows<T extends QueueRecordRow>(
  rows: readonly T[],
  options: {
    queueKey?: string;
    queues?: readonly QueueRef[];
    status?: string;
  }
): T[] {
  const queueKey = String(options.queueKey ?? "all");
  const status = String(options.status ?? "all");
  const queues = options.queues ?? [];
  return rows.filter((row) => {
    const queueMatched =
      queueKey === "all" || queueKeyForTask(row.task_type, queues) === queueKey;
    const statusMatched = status === "all" || row.status === status;
    return queueMatched && statusMatched;
  });
}

// --------------------------------------------------------------------------- #
// 列表呈现（2026-09-26 用户口径）
// --------------------------------------------------------------------------- #

/** 队列记录行的最小结构（`types.TaskRecord` 的子集，纯逻辑不依赖前端类型）。 */
export type QueueRecordRow = {
  task_type: string;
  status: string;
  payload?: Record<string, unknown> | null;
};

/**
 * 「执行详情」列的多行文案（2026-09-26 用户口径「执行详情，使用多行简单列出
 * 执行具体内容」）：**简单、多行、只列事实**。
 *
 * * 第一行主体：商品生成 = 商品名（`item_name` 优先）；同步 / 发布 = 入队时快照的
 *   笔记标题（缺省退 `#note_pk`，早期数据不编标题）；
 * * 第二行结果：成功 / 失败**带真实原因** / 未终态 / 已取消 —— 同步与发布失败的
 *   原因只在行上给一次（DEF-058）。
 *
 * ⚠️ 只用于**队列项行**（最细粒度）：批次行的文案走
 * :func:`batchExecutionDetailLines`（它回答的是「这一批整体怎么样」）。
 *
 * 返回空数组 = 没什么可列的（调用方渲染 `-`，缺值口径：不造假）。
 */
export function executionDetailLines(row: QueueRecordRow): string[] {
  const payload = (row.payload ?? {}) as Record<string, unknown>;

  if (isProductGenItem(row.task_type)) {
    const lines = [genItemProductText(payload as { item_id?: string; item_name?: string })];
    lines.push(_itemOutcomeLine(row, payload));
    return lines;
  }

  // 同步 / 发布队列项：主体 = 入队快照的笔记标题（§35.9 入队即完整落库）
  const title = String(payload.note_title ?? "").trim();
  const notePk = Number(payload.note_pk) || 0;
  const subject = title ? `《${title}》` : notePk ? `笔记 #${notePk}` : "";
  const lines = subject ? [subject] : [];
  lines.push(_itemOutcomeLine(row, payload));
  return lines;
}

/** 批次行 `summary` 的最小结构（后端按子行现算；前端只渲染不重算）。 */
export type QueueBatchSummaryShape = {
  total?: number;
  done?: number;
  failed?: number;
  cancelled?: number;
  running?: number;
  pending?: number;
};

/**
 * **批次行**的「执行详情」多行文案（2026-09-26 用户口径「批次+详情父子树」）。
 *
 * 与队列项那份的分工：这一份回答「**这一批整体怎么样**」，明细逐条的成败在展开
 * 后的子行里看。计数**一律取后端 `summary`**（`batch_rollup` 唯一实现），
 * 前端不重算 —— 重算就是第二份判据（DEF-044）。
 *
 * * 第一行：`共 N 条明细`；
 * * 第二行：非零计数明细（`成功 3 · 失败 1 · 进行中 1`）；全终态且零计数
 *   （空批次）⇒ 说明「这次动作没有入队成功的项」，不硬凑一个 `成功 0 · 失败 0`；
 * * 缺值口径：总计缺省按 0 报（计数缺值渲染 0 是既有口径，非计数才用 `-`）。
 */
export function batchExecutionDetailLines(batch: {
  summary?: QueueBatchSummaryShape | null;
}): string[] {
  const summary = batch.summary ?? {};
  const total = Number(summary.total) || 0;
  const parts = [
    ["成功", Number(summary.done) || 0],
    ["失败", Number(summary.failed) || 0],
    ["取消", Number(summary.cancelled) || 0],
    ["进行中", Number(summary.running) || 0],
    ["待处理", Number(summary.pending) || 0],
  ]
    .filter(([, count]) => Number(count) > 0)
    .map(([label, count]) => `${label} ${count}`);
  return [
    `共 ${total} 条明细`,
    parts.length ? parts.join(" · ") : "这次动作没有入队成功的项",
  ];
}

// --------------------------------------------------------------------------- #
// 账号（2026-09-26 用户口径「批次记录执行的账号」）
// --------------------------------------------------------------------------- #

/** 账号名映射所需的最小结构（`PlatformAccount` 的子集；纯逻辑不依赖前端类型）。 */
export type AccountNameRow = { id: number; nickname?: string | null };

/**
 * `id → 账号名` 查表（:func:`accountNameMap` 的返回类型）。
 *
 * 为什么导出这个别名而不是让调用方各自写 `Record<string, string>`：页面里有既有
 * 守卫「页面不许出现 `Record<string, string>`」（防的是**复制类型中文名映射表**）——
 * 账号名映射与那张表无关，但字面量长得一样；用 lib 导出的具名类型既能通过守卫，
 * 也让「这个查表是 lib 的定义」在页面上一眼可读。
 */
export type AccountNameMap = Record<string, string>;

/**
 * `id → 账号名` 映射（`fetchAccounts()` 的返回整理成查表）。
 *
 * ⚠️ 只放**账号名**（`nickname`），不带店铺 / 类型 / `(#id)` 后缀 —— 与账号下拉
 * 「只展示账号名称」同一条口径（设计 13 §30；`accountLabel` 同形）。
 * 名字缺失退 `账号 #id`（**可辨认的兜底**，不是造名字 —— 与 `笔记 #pk` 同型）；
 * `id` 非正整数直接跳过（那种行不该出现在映射里）。
 */
export function accountNameMap(accounts: readonly AccountNameRow[]): AccountNameMap {
  const map: AccountNameMap = {};
  for (const account of accounts) {
    const id = Number(account?.id) || 0;
    if (id <= 0) continue;
    const name = String(account.nickname ?? "").trim();
    map[String(id)] = name || `账号 #${id}`;
  }
  return map;
}

/**
 * 批次行**用到哪些账号**（后端 `batch_account_ids` 下发的 id 列表，去重保序）。
 *
 * ⚠️ 批次级账号是**派生值**（后端按子行 `payload.account_id` 现算）⇒ 前端**不重算**
 * （重算就是第二份判据，DEF-044）；后端没给 / 给了空数组 ⇒ 空数组（渲染 `-`）。
 * 明细行的账号看它**自己**的 `payload.account_id`，不走这里。
 */
export function batchAccountIds(batch: { accounts?: number[] | null }): number[] {
  if (!Array.isArray(batch?.accounts)) return [];
  return batch.accounts.map((id) => Number(id) || 0).filter((id) => id > 0);
}

/** 一行记录的最小结构（「账号」列判据只需要这三样）。 */
export type AccountRowLike = {
  batch_no?: string | null;
  accounts?: number[] | null;
  payload?: Record<string, unknown> | null;
};

/**
 * 「**账号**」列的文案（批次行与明细行**同一列**，判据不同）：
 *
 * * **批次行** → 取后端派生的 `accounts`：0 个 ⇒ `-`（不知道是谁跑的就不编）、
 *   1 个 ⇒ 那个账号名、多个 ⇒ `N 个账号`（逐行列出走 :func:`taskAccountTip`）；
 * * **明细行** → 取自己 `payload.account_id`（入队时的账号快照）：取不到 ⇒ `-`，
 *   取到但账号已删 / 改名 ⇒ `账号 #id`（与 `笔记 #pk` 同型兜底）。
 *
 * 为什么批次行不显示单个账号名：一次批量可以**跨账号勾选** ⇒ 批次级根本不存在
 * 「唯一账号」这回事，硬报一个就是报错的（用 `N 个账号` 才与明细对得上）。
 */
export function taskAccountText(row: AccountRowLike, names: AccountNameMap): string {
  if (isBatchRow(row)) {
    const ids = batchAccountIds(row);
    if (!ids.length) return "-";
    if (ids.length === 1) return names[String(ids[0])] || `账号 #${ids[0]}`;
    return `${ids.length} 个账号`;
  }
  const payload = (row.payload ?? {}) as Record<string, unknown>;
  const id = Number(payload.account_id) || 0;
  if (id <= 0) return "-";
  return names[String(id)] || `账号 #${id}`;
}

/**
 * 「账号」列的 Tooltip：**只有多账号批次**才有内容（其余返回空串 ⇒ 调用方不套
 * Tooltip，不给单账号 / 明细行加一个点开只有一行同样文字的浮层）。
 */
export function taskAccountTip(row: AccountRowLike, names: AccountNameMap): string {
  if (!isBatchRow(row)) return "";
  const ids = batchAccountIds(row);
  if (ids.length <= 1) return "";
  return ids.map((id) => names[String(id)] || `账号 #${id}`).join("、");
}

/** 队列项的**结果行**（未终态 / 已取消 / 成功 / 失败带真实原因，一个口径）。 */
function _itemOutcomeLine(row: QueueRecordRow, payload: Record<string, unknown>): string {
  if (row.status === "pending") return "待处理";
  if (row.status === "running") return "进行中";
  if (row.status === "cancelled") return "已取消";
  if (row.status === "done") {
    if (isProductGenItem(row.task_type)) {
      const noteText = genItemNoteText(payload as { note_title?: string; note_id?: string });
      return noteText ? `已生成《${noteText}》` : "已生成";
    }
    return itemOutcomeText(payload as { ok?: boolean; reason?: string; code?: string });
  }
  return itemOutcomeText(payload as { ok?: boolean; reason?: string; code?: string });
}
