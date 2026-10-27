import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 前端单测：Vitest（与 Vite 共用同一条转换管线，无需额外 babel/ts-node 配置）。
// 当前只覆盖**纯逻辑模块**（如 `pages/llm-gateway/gateway-tree.ts` 的父子树构建），
// 故测试环境用默认的 `node` 即可 —— 不引入 jsdom / @testing-library/react，
// 保持依赖面最小。日后要测 React 组件再补 `environment: "jsdom"` + 对应依赖。
export default defineConfig({
  plugins: [react()],
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "node",
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    /**
     * ⚠️ **端口被占用时不许悄悄换端口**（`strictPort`）。
     *
     * 默认行为是「5173 被占就依次试 5174、5175…」，而 launcher、浏览器书签、文档、
     * 反向代理全部硬编码 5173 ⇒ 换端口后**用户看到的仍是旧页面**（8000 的 dist 产物），
     * 会以为「改前端不生效」。实测踩过：三个 vite 实例互抢端口，最后一个跑到 **5224**，
     * `vite.log` 里只有一串 `Port 52xx is in use`，症状却是「前端改动不生效」。
     * ⇒ 宁可**启动失败**（日志里一眼看到端口被占），也不要「起来了但没人访问得到」。
     */
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            if (proxyRes.headers["content-type"]?.includes("text/event-stream")) {
              proxyRes.headers["cache-control"] = "no-cache";
              proxyRes.headers["x-accel-buffering"] = "no";
            }
          });
        },
      },
    },
  },
});
