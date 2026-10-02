import { VideoCameraOutlined } from "@ant-design/icons";
import { Card, Empty, Typography } from "antd";

import { useThemePalette } from "../../../app/providers";

const { Text } = Typography;

/**
 * 视频工坊**面板**（2026-09-25 §36：原 `video-studio-page.tsx` 独立页面，现为
 * 「笔记工坊 → 视频工坊」页签的内容）。
 *
 * 视频能力属于**范围外**（需求规格说明书「视觉工坊」条）：这里只保留
 * 「即将上线」占位，与合并前的空壳页逐字一致 —— 不借合并之机把范围外功能
 * 悄悄做实，也不把占位撤掉让用户以为曾有的功能丢了。
 */
export function VideoStudioPanel() {
  const p = useThemePalette();
  return (
    <Card
      style={{ background: p.cardBg, borderRadius: 8, border: `1px solid ${p.border}` }}
      styles={{ body: { padding: 48 } }}
    >
      <Empty
        image={<VideoCameraOutlined style={{ fontSize: 64, color: p.textTertiary }} />}
        imageStyle={{ height: 80 }}
        description={
          <div>
            <Text strong style={{ fontSize: 16, display: "block", marginBottom: 8 }}>视频工坊即将上线</Text>
            <Text type="secondary">
              视频剪辑、封面提取、视频转码、AI 视频润色等功能正在开发中，敬请期待。
            </Text>
          </div>
        }
      />
    </Card>
  );
}
