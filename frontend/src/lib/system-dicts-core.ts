/**
 * 系统字典统一出入口 —— **core**（设计 63 §2.2；无 `./api` 依赖）。
 *
 * 为什么拆两个文件：`lib/task-records.ts` 等纯逻辑模块被行为脚本
 * （`tests/frontend/task_records.cjs`）**esbuild 单文件打包**跑真实断言，
 * 不能把 axios/全量端点层卷进 bundle。本文件只依赖 react（可打包），
 * 取数函数由 `system-dicts.ts` 用 `setDictFetcher()` 注入 —— 行为脚本
 * 单独打包 core 时没有 fetcher ⇒ `ensureDicts()` 空转 ⇒ 全部走回退常量，
 * 恰好就是「离线地板」路径。
 *
 * 唯一取数点 = `GET /api/system/dict/options`（登录即可、无权限码，一次全量）。
 * 所有「code → 中文文案 / 颜色 / 下拉选项」的消费方都从本模块读，**不再各自
 * 直连字典接口**；字典管理页改完调用 `refreshDicts()`，全站订阅者即时重渲染。
 *
 * 回退口径（缺值不造假，也不裸奔）：
 * - 字典未加载 / 该组不存在 / 该 value 不在字典里 ⇒ 用各枚举自带的**镜像
 *   常量**（与后端 Python 常量逐个一致，由 `test_dict_seed_mirrors_python_constants`
 *   钉住）兜底；
 * - 字典里查到 ⇒ 字典赢（用户改过文案就显示改过的 —— 「动态实时可变」的落点）。
 *
 * 加载时机：首次有任何消费方读字典时惰性拉一次（`ensureDicts()` 幂等），
 * 登录后首个带字典文案的渲染自然触发；未登录（无 refresh token）不发起。
 */

import { useSyncExternalStore } from "react";

export type DictOption = {
  value: string;
  label: string;
  list_class: string;
  sort_order: number;
};

/** 回退条目（镜像常量的形状；`list_class` 缺省 default）。 */
export type DictFallbackEntry = {
  value: string;
  label: string;
  list_class?: string;
};

/** 统一读出口的取数函数形状（由接线层注入，core 不 import api）。 */
export type DictFetcher = () => Promise<{ items: Record<string, DictOption[]> }>;

let fetcher: DictFetcher | null = null;

/** 接线层注入取数函数（`system-dicts.ts` 调；行为脚本环境不调 ⇒ 纯回退）。 */
export function setDictFetcher(fn: DictFetcher): void {
  fetcher = fn;
}

/** 测试专用：清空模块级 store（core 是单例，用例间不复位会串味）。 */
export function resetDictStoreForTest(): void {
  groups = {};
  loadedOnce = false;
  inflight = null;
  version = 0;
  fetcher = null;
  listeners.clear();
}

/** 由镜像常量构造回退条目（`Record<value, label>` 或 `Record<value, {label,color}>`）。 */
export function dictFallbackEntries(
  source: Record<string, string> | Record<string, { label: string; color?: string }>
): DictFallbackEntry[] {
  return Object.entries(source).map(([value, item]) =>
    typeof item === "string"
      ? { value, label: item }
      : { value, label: item.label, list_class: item.color }
  );
}

// --------------------------------------------------------------------------- //
// 模块级 store（同步读 + 订阅）
// --------------------------------------------------------------------------- //
let groups: Record<string, DictOption[]> = {};
let loadedOnce = false;
let inflight: Promise<void> | null = null;
let version = 0;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((listener) => listener());
}

/** 拉一次全量字典并广播（字典管理页改完也调它 ⇒ 全站实时更新）。 */
export async function refreshDicts(): Promise<void> {
  if (!fetcher) return;
  const res = await fetcher();
  groups = res.items;
  loadedOnce = true;
  version += 1;
  emit();
}

/** 惰性加载（幂等；并发只发一次）。 */
export function ensureDicts(): void {
  if (loadedOnce || inflight || !fetcher) return;
  inflight = refreshDicts()
    .catch(() => {
      /* 拉取失败静默走回退常量；下次订阅者读时重试 */
    })
    .finally(() => {
      inflight = null;
    });
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** 该组当前字典选项；未加载到 ⇒ `null`（调用方走回退）。 */
export function dictGroup(code: string): DictOption[] | null {
  ensureDicts();
  return groups[code] ?? null;
}

/** 字典已加载过吗（测试与页面判断「是否显示字典态」用）。 */
export function dictsLoaded(): boolean {
  return loadedOnce;
}

// --------------------------------------------------------------------------- //
// 同步读（lib 层格式化函数用；渲染层建议用下面的 hook 才能吃到实时刷新）
// ---------------------------------------------------------------------------

/** code → 中文文案：字典赢，镜像常量兜底，都没有原样返回 value。 */
export function dictLabel(
  code: string,
  value: string | null | undefined,
  fallback?: Record<string, string>
): string {
  if (value === null || value === undefined || value === "") return value ?? "";
  const group = dictGroup(code);
  const fromDict = group?.find((item) => item.value === value)?.label;
  if (fromDict !== undefined) return fromDict;
  const fromFallback = fallback ? fallback[String(value)] : undefined;
  return fromFallback ?? value;
}

/** code+value → antd Tag 颜色档位。字典赢；回退表兜底；都没有 `default`。 */
export function dictListClass(
  code: string,
  value: string | null | undefined,
  fallback?: Record<string, string>
): string {
  if (value === null || value === undefined) return "default";
  const group = dictGroup(code);
  const fromDict = group?.find((item) => item.value === value)?.list_class;
  if (fromDict) return fromDict;
  return fallback?.[value] ?? "default";
}

// --------------------------------------------------------------------------- //
// React hook（订阅 store ⇒ 字典管理页改完这里实时重渲染）
// ---------------------------------------------------------------------------

/**
 * 订阅字典版本：字典一刷新就递增。挂在**应用外壳**（workspace-shell）一层 ——
 * 外壳重渲染 ⇒ 全部页面重渲染 ⇒ 表格里走**同步读**（`dictLabel`，非组件上下文
 * 上不了 hook）的文案也跟着字典即时更新，不用每张表单独接 hook。
 */
export function useDictVersion(): number {
  ensureDicts();
  return useSyncExternalStore(subscribe, () => version);
}

/** 回退合并（纯函数）：字典组赢、按字典 sort_order 排；字典缺的 value 由回退补位。 */
export function mergeDictOptions(
  group: DictOption[] | null,
  fallback: DictFallbackEntry[] = []
): DictOption[] {
  if (!group) {
    return fallback.map((entry) => ({
      value: entry.value,
      label: entry.label,
      list_class: entry.list_class ?? "default",
      sort_order: 0,
    }));
  }
  const dictValues = new Set(group.map((item) => item.value));
  const merged: DictOption[] = group.map((item) => ({ ...item }));
  for (const entry of fallback) {
    if (!dictValues.has(entry.value)) {
      merged.push({
        value: entry.value,
        label: entry.label,
        list_class: entry.list_class ?? "default",
        sort_order: Number.MAX_SAFE_INTEGER,
      });
    }
  }
  return merged;
}

/**
 * 取某组的下拉选项：字典加载后字典赢（按字典 sort_order 排），字典缺的
 * value 用回退条目补位；回退里独有的也保留 —— 保证下拉永远有得选。
 */
export function useDictOptions(
  code: string,
  fallback: DictFallbackEntry[] = []
): DictOption[] {
  ensureDicts();
  const group = useSyncExternalStore(subscribe, () => groups[code] ?? null);
  return mergeDictOptions(group, fallback);
}

/** 响应式单值文案（渲染层用；lib 层同步场景用 `dictLabel`）。 */
export function useDictLabel(
  code: string,
  value: string | null | undefined,
  fallback?: Record<string, string>
): string {
  ensureDicts();
  useSyncExternalStore(subscribe, () => groups[code] ?? null);
  return dictLabel(code, value, fallback);
}
