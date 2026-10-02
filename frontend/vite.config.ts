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
