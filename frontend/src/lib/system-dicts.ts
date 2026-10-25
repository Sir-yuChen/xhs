/**
 * 系统字典统一出入口 —— **接线层**（设计 63 §2.2）。
 *
 * core（`./system-dicts-core`）无 `./api` 依赖（行为脚本要单文件打包它）；
 * 本文件把统一读出口 `fetchSystemDictOptions` 注入 core 并整体再导出。
 * **业务代码一律 `from "./system-dicts"`（或 `lib/system-dicts`）**，
 * core 只被本文件与被打包的纯逻辑模块直接引用。
 */

import { fetchSystemDictOptions, hasRefreshToken } from "./api";
import { setDictFetcher } from "./system-dicts-core";

setDictFetcher(async () => {
  // 未登录（无 refresh token）不发起 —— 401 噪声与无谓的刷新循环都不给它。
  if (!hasRefreshToken()) return { items: {} };
  return fetchSystemDictOptions();
});

export * from "./system-dicts-core";
