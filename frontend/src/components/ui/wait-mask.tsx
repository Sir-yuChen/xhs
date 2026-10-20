/**
 * 全站统一的「等待 / 进度蒙版」。
 *
 * 用户口径 2026-10-20：*「扫描前端所有蒙版，按照当前抽取统一蒙版样式，进度/等待弹窗居中，
 * 保存全项目蒙版样式一样，内容各个组件使用时可自定义内容」*。
 *
 * 为什么要抽这一个组件（而不是各页继续写 `Spin tip`）：
 * ① **`Spin` 的 `tip` 会跟着内容滚动跑出可视区** —— 页面一滚，「已耗时 Ns」就看不见了，
 *    用户以为卡死。本组件用 `position: absolute/fixed` + flex 居中，标签与滚动无关。
 * ② 全站此前是三种形态并存（整页 `Spin spinning`／自建 `.publish-wait-mask`／弹窗内裸 `Spin`），
 *    同一个「正在跑」在不同页面长相不同。这里收敛成**一套视觉**。
 * ③ 内容必须可自定义：详情页要说「加载商品详情」、发布要说「正在发布商品（保持在售）」、
 *    导入要说「读取数据包…」。因此 `title` / `sub` 是 slot，`extra` 允许放进度条或自定义节点。
 *
 * 用法：
 *
 * ```tsx
 * // ① 整页蒙版（铺满视口，盖住整个页面）
 * <WaitMask open={loading} title="加载商品列表…" elapsed={sec} />
 *
 * // ② 弹窗内蒙版（父元素需 `position: relative`，只盖住弹窗）
 * <WaitMask open={busy} scope="panel" title="正在生成…" elapsed={sec}
 *           sub="请勿关闭本窗口" extra={<Progress percent={p} />} />
 *
 * // ③ 完全自定义内容
 * <WaitMask open={busy} scope="panel">
 *   <MyOwnContent />
 * </WaitMask>
 * ```
 *
 * ⚠️ `scope="panel"` 时父元素**必须** `position: relative`，否则它会往上找到最近的定位祖先
 * （通常是 body），变成整页蒙版 —— 表现是「蒙版盖到页面上了，不是盖在这个弹窗里」。
 */
import type { CSSProperties, ReactNode } from "react";
import { Spin, Typography } from "antd";

const { Text } = Typography;

export type WaitMaskScope = "page" | "panel";

export interface WaitMaskProps {
  /** 是否显示。false 时不渲染任何节点（不占布局）。 */
  open: boolean;
  /**
   * - `page`（默认）：`position: fixed` 铺满视口，盖住整个页面；
   * - `panel`：`position: absolute` 铺满最近的定位祖先，用于**弹窗内**等待。
   */
  scope?: WaitMaskScope;
  /** 主文案，如「正在发布商品（保持在售）…」。 */
  title?: ReactNode;
  /** 已耗时秒数。给了就在主文案后补「已耗时 Ns」；不给就不显示（短等待不必计时）。 */
  elapsed?: number | null;
  /** 次要说明（12px 灰字，如「请勿关闭本窗口或重复点击」）。 */
  sub?: ReactNode;
  /** 自定义附加内容，渲染在主文案与 `sub` 之间（进度条 / 第 N/M 步 / 自定义节点）。 */
  extra?: ReactNode;
  /** 完全自定义卡片内容 —— 给了它就不渲染默认的 Spin + 文案结构。 */
  children?: ReactNode;
  /** 蒙版透明度/背景等微调（透传到蒙版层）。 */
  maskStyle?: CSSProperties;
  /** 卡片本身的微调（透传到白卡片）。 */
  style?: CSSProperties;
}

/** 蒙版的统一底色：白 55% + 1px 模糊（与 `.publish-wait-mask` 一致）。 */
const MASK_BASE: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "rgba(255, 255, 255, 0.55)",
  backdropFilter: "blur(1px)",
  zIndex: 2000,
};

/** 白卡片的统一形态：圆角 12、阴影、纵向居中。 */
const CARD_BASE: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  gap: 14,
  padding: "24px 32px",
  borderRadius: 12,
  background: "var(--ant-color-bg-container, #fff)",
  boxShadow: "0 6px 24px rgba(0, 0, 0, 0.14)",
  maxWidth: "min(420px, 80vw)",
};

export function WaitMask({
  open,
  scope = "page",
  title,
  elapsed,
  sub,
  extra,
  children,
  maskStyle,
  style,
}: WaitMaskProps) {
  if (!open) return null;

  const mask: CSSProperties = {
    ...MASK_BASE,
    ...(scope === "page" ? { position: "fixed", inset: 0 } : { position: "absolute", inset: 0 }),
    ...maskStyle,
  };

  const hasElapsed = typeof elapsed === "number" && Number.isFinite(elapsed);

  return (
    <div style={mask} role="status" aria-live="polite" aria-busy="true">
      <div style={{ ...CARD_BASE, ...style }}>
        {children ?? (
          <>
            <Spin size="large" />
            {(title || hasElapsed) && (
              <div style={{ fontSize: 14, color: "var(--ant-color-text, rgba(0,0,0,0.88))" }}>
                {title}
                {hasElapsed ? `${title ? "… " : ""}已耗时 ${Math.floor(elapsed as number)}s` : ""}
              </div>
            )}
            {extra}
            {sub && (
              <Text
                type="secondary"
                style={{ fontSize: 12, lineHeight: "18px", textAlign: "center", maxWidth: 360 }}
              >
                {sub}
              </Text>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default WaitMask;
