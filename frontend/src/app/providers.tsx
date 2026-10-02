import { App, ConfigProvider, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

type ThemeMode = "dark" | "light";

const ThemeContext = createContext<{ mode: ThemeMode; toggle: () => void }>({
  mode: "dark",
  toggle: () => {},
});

export function useThemeMode() {
  return useContext(ThemeContext);
}

/**
 * 主题感知的常用色板（2026-10-02 修复「切主题后一半黑一半白」）。
 * 早期业务页面把卡片底 `#1f1f1f`、边框 `#303030`、白字 `rgba(255,255,255,…)` 写死在内联样式里，
 * 浅色模式下 antd 已经切到白底，这些硬编码却不动，导致内容区一片黑卡片、周围白底。
 * 这里统一提供一份随 `mode` 派生的色板，业务页面把硬编码替换为 `palette.*` 即可双主题自洽。
 */
export function useThemePalette() {
  const { mode } = useThemeMode();
  const dark = mode === "dark";
  return {
    // 页面根背景：暗色近黑 / 浅色浅灰（比卡片背景更暗/更浅一层，形成层次）
    pageBg: dark ? "#0a0a0a" : "#f5f5f5",
    // 卡片 / 容器背景：暗色下沿用手写值，浅色下交给 antd 白底
    cardBg: dark ? "#1f1f1f" : "#ffffff",
    cardBgElevated: dark ? "#262626" : "#ffffff",
    // 边框：暗色 `#303030` / 浅色 `#f0f0f0`
    border: dark ? "#303030" : "#f0f0f0",
    borderSoft: dark ? "#262626" : "#e5e5e5",
    borderStrong: dark ? "#434343" : "#d9d9d9",
    // 文字：暗色白字 / 浅色黑字（三级对比度）
    text: dark ? "rgba(255,255,255,.88)" : "rgba(0,0,0,.88)",
    textSecondary: dark ? "rgba(255,255,255,.65)" : "rgba(0,0,0,.65)",
    textTertiary: dark ? "rgba(255,255,255,.45)" : "rgba(0,0,0,.45)",
    textMuted: dark ? "rgba(255,255,255,.35)" : "rgba(0,0,0,.35)",
    // 弱底（hover / 占位块）：暗色白 4% / 浅色黑 4%
    fillWeak: dark ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.04)",
    fillIcon: dark ? "rgba(255,255,255,0.25)" : "rgba(0,0,0,0.25)",
    // 图片占位 / 缩略图容器底（暗色近黑、浅色浅灰）
    mediaBg: dark ? "#141414" : "#f0f0f0",
  };
}

const darkTokens = {
  colorPrimary: "#1668dc",
  colorBgBase: "#141414",
  colorBgContainer: "#1f1f1f",
  colorBgElevated: "#262626",
  colorBorder: "#303030",
  colorBorderSecondary: "#262626",
  borderRadius: 6,
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
};

const lightTokens = {
  colorPrimary: "#1668dc",
  colorBgBase: "#ffffff",
  colorBgContainer: "#ffffff",
  colorBgElevated: "#ffffff",
  colorBorder: "#e8e8e8",
  colorBorderSecondary: "#f0f0f0",
  borderRadius: 6,
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
};

const darkComponents = {
  Layout: { siderBg: "#141414", headerBg: "#141414", bodyBg: "#0a0a0a" },
  Menu: { darkItemBg: "#141414", darkSubMenuItemBg: "#141414", darkItemSelectedBg: "#1668dc20", darkItemSelectedColor: "#1668dc" },
  Card: { colorBgContainer: "#1a1a1a" },
  Table: { colorBgContainer: "#1a1a1a", headerBg: "#1f1f1f", rowHoverBg: "rgba(255,255,255,0.06)" },
};

const lightComponents = {
  Layout: { siderBg: "#ffffff", headerBg: "#ffffff", bodyBg: "#f5f5f5" },
  Menu: { itemBg: "#ffffff", subMenuItemBg: "#ffffff", itemSelectedBg: "#1668dc14", itemSelectedColor: "#1668dc", itemColor: "rgba(0,0,0,.85)" },
  Card: {},
  Table: { rowHoverBg: "rgba(0,0,0,0.04)" },
};

type AppProvidersProps = { children: ReactNode };

export function AppProviders({ children }: AppProvidersProps) {
  const [mode, setMode] = useState<ThemeMode>(() => {
    const saved = localStorage.getItem("theme-mode");
    return saved === "light" ? "light" : "dark";
  });

  useEffect(() => {
    localStorage.setItem("theme-mode", mode);
    document.body.style.background = mode === "dark" ? "#141414" : "#f5f5f5";
  }, [mode]);

  const toggle = () => setMode((m) => (m === "dark" ? "light" : "dark"));
  const isDark = mode === "dark";

  return (
    <ThemeContext.Provider value={{ mode, toggle }}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
          token: isDark ? darkTokens : lightTokens,
          components: isDark ? darkComponents : lightComponents,
        }}
      >
        <App>{children}</App>
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}
