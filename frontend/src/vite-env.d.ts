/// <reference types="vite/client" />

/**
 * Vite 的 `?raw` 导入声明（本项目新增 2026-10-21）。
 *
 * ⚠️ 项目里原本没有 `vite-env.d.ts`（`tsconfig.json` 也没引 `vite/client` 类型），
 * 而 `.tsx` 不被 vitest 直接导入、测试要读**真实数据包夹具**时就只能：
 *   ① `import { readFileSync } from "node:fs"` —— tsconfig 没有 `node` 类型 ⇒ TS2307，
 *      且会让 `npm run build` 一起失败；
 *   ② 把 CSV 抄成 TS 字符串常量 —— 456 行会撑爆源文件，且改夹具要动代码。
 * ⇒ 用 Vite 原生支持的 `?raw`（构建期转成字符串），这里补上声明让 `tsc` 认得。
 *
 * ⚠️ `?raw` 用于**测试夹具**，不进产物（无人 import 的测试文件不会被打包）。
 */
declare module "*?raw" {
  const content: string;
  export default content;
}
