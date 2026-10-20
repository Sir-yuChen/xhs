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
import { useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { Typography } from "antd";

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
  /**
   * 蒙版色调。
   * - `light`（默认）：白 55% 底 + 白卡片（正文/列表/弹窗内常规等待）；
   * - `dark`：黑 55% 底 + **深色文字**（用于原本就是深色遮罩的场景，
   *   如登录提交、生成弹窗 —— 白底卡会与暗色背景撞色）。
   */
  tone?: "light" | "dark";
  /**
   * 是否绘制**半透明遮罩底**（同时**拦住点击**）。
   *
   * - `true`（默认）：遮罩 + 居中卡片，**挡交互** —— 等待期间用户点不到下面的按钮
   *   （用户口径 2026-10-20：「全部没有蒙版了，用户可以操作非等待或进度弹窗下的按钮功能」
   *   ⇒ 等待必须阻断操作，否则会在等结果时又点出别的动作）。
   * - `false`：只有居中卡片、不画底、不拦点击 —— 仅用于**纯提示**且不需要阻断的场景。
   *
   * ⚠️ 视觉上仍然只有**一层**：原先叠层的原因是 `Spin`/`Table` 各自又画了一层，
   * 那两处已全部移除，现在全站只有 `WaitMask` 这一层底。
   */
  mask?: boolean;
  /**
   * **最短可见时长（毫秒）** —— 默认 1000。
   *
   * 用户口径 2026-10-20：「所有最少 1 秒后再关闭，否则容易出现闪现问题」。
   * 本地查询常常几十毫秒就返回，蒙版一闪而过看着像「闪屏」；
   * 这里保证从**出现到消失至少 1 秒**，超过 1 秒的等待不受影响、按真实耗时消失。
   * 传 `0` 可关闭（仅用于测试）。
   */
  minVisibleMs?: number;
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
  tone = "light",
  mask: withMask = true,
  minVisibleMs = 1000,
  maskStyle,
  style,
}: WaitMaskProps) {
  //: **最短显示 1 秒**（用户口径 2026-10-20「否则容易出现闪现问题」）。
  //: 逻辑：`open` 变 true 时记开始时间；变 false 时若还不满 `minVisibleMs`，
  //: 先保持可见、到点再隐藏。这样「几十毫秒就返回」的本地查询也不会闪。
  //: ⚠️ 用 state 驱动隐藏（不是直接读 ref）—— 必须触发一次重渲染才会真的消失。
  const [visible, setVisible] = useState(open);
  //: 每次「变可见」的时刻；0 = 还没显示过。
  const shownAtRef = useRef<number>(open ? Date.now() : 0);
  useEffect(() => {
    if (open) {
      //: 变为可见：记起点，并立刻显示（清掉可能挂着的关闭定时器）。
      shownAtRef.current = Date.now();
      setVisible(true);
      return;
    }
    if (!shownAtRef.current) {
      //: 从未显示过 ⇒ 无需求最短时长。
      setVisible(false);
      return;
    }
    //: 关闭请求：够 `minVisibleMs` 就直接关，不够就等到点（避免一闪而过）。
    const remain = minVisibleMs - (Date.now() - shownAtRef.current);
    if (remain <= 0) {
      setVisible(false);
      shownAtRef.current = 0;
      return;
    }
    const timer = window.setTimeout(() => {
      setVisible(false);
      shownAtRef.current = 0;
    }, remain);
    return () => window.clearTimeout(timer);
  }, [open, minVisibleMs]);

  //: ⚠️ **关闭请求后内容会被父组件清空**（`open` 变 false 时父组件往往同时把
  //: `title`/`elapsed` 也重置，如 `endWait()` 里 `setWaitLabel(null)`）。
  //: 若最短 1 秒期间直接用当前 props，卡片会从「加载商品列表… 已耗时 0s」
  //: **跳成空白标题**（用户实测两张截图：同一张卡先有文案、后只剩「已耗时 0s」）。
  //: ⇒ 这里冻结「最后一次可见时的快照」，最短显示期内用它渲染，文案不再跳。
  const snapshotRef = useRef<{ title: ReactNode; elapsed: number | null; sub: ReactNode; extra: ReactNode }>({
    title,
    elapsed: elapsed ?? null,
    sub,
    extra,
  });
  if (open) {
    snapshotRef.current = { title, elapsed: elapsed ?? null, sub, extra };
  }
  const snap = snapshotRef.current;

  //: ⚠️ 最短 1 秒保持期内父组件的计时器已经停了（`endWait` 会清 interval），
  //: 若直接用冻结的 `elapsed` 数值，用户会看到「耗时一直不变的 0s」。
  //: ⇒ 保持期内**自己走表**：每 250ms 重算 `(now - 显示起点)`，秒数照常增长。
  //: 显示期间**每 250ms 重算一次**（首次显示 + 最短保持期都要走表）。
  const [, forceTick] = useState(0);
  useEffect(() => {
    if (!visible) return;
    const id = window.setInterval(() => forceTick((n) => n + 1), 250);
    return () => window.clearInterval(id);
  }, [visible]);
  //: **自走表优先**：从「本次显示起点」算到现在的秒数。
  //: 父组件传的 `elapsed` 只在自走表尚未起步（起点为 0）时兜底。
  const shownElapsed = shownAtRef.current
    ? Math.floor((Date.now() - shownAtRef.current) / 1000)
    : snap.elapsed;

  if (!visible) return null;

  const mask: CSSProperties = {
    ...MASK_BASE,
    ...(tone === "dark" ? { background: "rgba(0, 0, 0, 0.55)" } : null),
    ...(scope === "page" ? { position: "fixed", inset: 0 } : { position: "absolute", inset: 0 }),
    //: `mask={false}` ⇒ 不画底、不拦点击，只留居中的白卡片提示。
    ...(withMask ? null : { background: "transparent", backdropFilter: "none", pointerEvents: "none" }),
    ...maskStyle,
  };
  const cardStyle: CSSProperties =
    tone === "dark"
      ? {
          ...CARD_BASE,
          //: 深色底上不放白卡（会刺眼），改为「透明卡片 + 深色文字」，与原来黑底遮罩观感一致。
          background: "transparent",
          boxShadow: "none",
          color: "rgba(255,255,255,0.92)",
        }
      : CARD_BASE;

  const hasElapsed = typeof elapsed === "number" && Number.isFinite(elapsed);
  //: ⚠️ 不用 antd `<Spin>` 画这里的转圈（2026-10-20 实测）：
  //: 无子内容时它渲染出 `.ant-spin-nested-loading`（高度塌成 0），内部图标是
  //: **绝对定位** ⇒ 脱离文档流、压在文案上（用户截图：圈盖在「加载商品列表」中间）。
  //: 改成纯 CSS 圆环，天然占位、永远与文案纵向排列、不依赖 antd 内部结构。
  const ring: CSSProperties = {
    width: 28,
    height: 28,
    borderRadius: "50%",
    border: `3px solid ${tone === "dark" ? "rgba(255,255,255,0.28)" : "var(--ant-color-primary-border, #91caff)"}`,
    borderTopColor: tone === "dark" ? "rgba(255,255,255,0.92)" : "var(--ant-color-primary, #1677ff)",
    animation: "wait-mask-spin 0.9s linear infinite",
    flex: "0 0 auto",
  };

  return (
    <div style={mask} role="status" aria-live="polite" aria-busy="true">
      <style>{"@keyframes wait-mask-spin{to{transform:rotate(360deg)}}"}</style>
      <div style={{ ...cardStyle, ...style }}>
        {children ?? (
          <>
            <div style={ring} aria-hidden="true" />
            {(snap.title || hasElapsed) && (
              <div
                style={{
                  fontSize: 14,
                  color:
                    tone === "dark"
                      ? "rgba(255,255,255,0.92)"
                      : "var(--ant-color-text, rgba(0,0,0,0.88))",
                }}
              >
                {snap.title}
                {hasElapsed ? `${snap.title ? "… " : ""}已耗时 ${Math.floor(shownElapsed as number)}s` : ""}
              </div>
            )}
            {snap.extra}
            {snap.sub && (
              <Text
                type="secondary"
                style={{ fontSize: 12, lineHeight: "18px", textAlign: "center", maxWidth: 360 }}
              >
                {snap.sub}
              </Text>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default WaitMask;
