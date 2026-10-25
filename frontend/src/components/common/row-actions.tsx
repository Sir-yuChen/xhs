/**
 * 操作列的统一容器（全站共用，2026-10-25 用户口径）。
 *
 * ## 为什么不各页自己写
 *
 * 用户口径：
 * > 「操作列的操作按钮自适应，不要溢出页面，**所有操作列均不允许溢出页面**」
 *
 * 溢出不是「某一页写宽了」，而是**每个页面各自决定放几个按钮**这种做法的必然结果：
 * 用户管理放 5 个、任务中心放 4 个，加上权限判断后按钮数量还会变（有的角色只看得到 2 个），
 * 任何固定宽度都会在某个角色/某个分辨率下溢出。
 *
 * ## 做法：常用 N 个直接显示，其余收进「更多」
 *
 * ```
 * [编辑] [重置密码] [下线] [更多 ▾]
 *                        └─ 删除 / 通过审核 / …
 * ```
 *
 * * `primary` 数量的上限 = `maxInline`（默认 3）—— 三个短按钮 + 「更多」在 240px 内放得下；
 * * 每项都可带 `permission`，**无权限直接不出现**（不是置灰）；
 * * 全部无权限时整列渲染 `-`（缺值口径，不显示空按钮组）。
 *
 * ## 与 antd `Space` 的区别
 *
 * `Space` 不会换行也不会收敛，按钮多了就顶破列宽。这里用 `flex-wrap: nowrap`
 * + `overflow: hidden` 兜底：**即使 maxInline 被配大，也不会顶破表格**。
 */

import type { ReactNode } from "react";
import { Button, Dropdown, Space, Typography } from "antd";
import type { MenuProps } from "antd";
import { MoreOutlined } from "@ant-design/icons";

import { hasPermission } from "../../hooks/use-permission";

const { Text } = Typography;

export type ActionItem = {
  /** 按钮文案（短：2-4 字，「立即执行」这类长文案请改短或放「更多」里） */
  label: string;
  onClick: () => void;
  /** 需要的权限码；不传 = 不做权限判断（如「详情」这类纯读） */
  permission?: string;
  danger?: boolean;
  disabled?: boolean;
  loading?: boolean;
  /** 进「更多」时的补充说明（可选） */
  hint?: string;
};

type Props = {
  items: ActionItem[];
  /** 直接显示几个（默认 3）。超出的进「更多」。 */
  maxInline?: number;
  /** 列宽（px）；给定时按它算能放几个，否则用 maxInline */
  width?: number;
};

/**
 * 估算「能直接放几个」：每个短按钮约 56px，「更多」约 44px，间距 4px。
 *
 * 不追求精确 —— 真正的兜底是 `overflow: hidden` + `nowrap`：
 * 算多了也不会顶破表格，只会把最后那个截掉半个字，比整列溢出好得多。
 */
function inlineCount(maxInline: number, width?: number): number {
  if (!width) return maxInline;
  const usable = width - 16; // 列内边距
  const per = 60; // 一个短按钮 + 间距
  const more = 44; // 「更多」按钮
  const fit = Math.floor((usable - more) / per);
  return Math.max(1, Math.min(maxInline, fit));
}

export function RowActions({ items, maxInline = 3, width }: Props) {
  //: 无权限的项直接**不出现**（用户口径「没有对应权限则不展示」）
  const visible = items.filter((it) => !it.permission || hasPermission(it.permission));
  if (!visible.length) return <Text type="secondary">-</Text>;

  const n = inlineCount(maxInline, width);
  const head = visible.slice(0, n);
  const tail = visible.slice(n);

  const moreItems: MenuProps["items"] = tail.map((it, i) => ({
    key: `${i}`,
    danger: it.danger,
    disabled: it.disabled,
    label: it.hint ? `${it.label}（${it.hint}）` : it.label,
    onClick: it.onClick,
  }));

  return (
    <Space
      size={4}
      //: 兜底：即使算多了也不顶破表格列宽
      style={{ flexWrap: "nowrap", overflow: "hidden", maxWidth: "100%" }}
    >
      {head.map((it, i) => (
        <Button
          key={`${it.label}-${i}`}
          type="link"
          size="small"
          danger={it.danger}
          disabled={it.disabled}
          loading={it.loading}
          style={{ padding: 0, flexShrink: 0 }}
          onClick={it.onClick}
        >
          {it.label}
        </Button>
      ))}
      {tail.length > 0 && (
        <Dropdown menu={{ items: moreItems }} trigger={["click"]}>
          <Button type="link" size="small" style={{ padding: 0, flexShrink: 0 }}>
            更多 <MoreOutlined />
          </Button>
        </Dropdown>
      )}
    </Space>
  );
}

/** 操作列的通用 `column` 定义（宽度、fixed、不换行）。 */
export function actionColumn(
  title: string,
  render: (row: never) => ReactNode,
  width = 200
) {
  return {
    title,
    key: "__actions__",
    width,
    fixed: "right" as const,
    render,
  };
}
