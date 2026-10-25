import { ArrowLeftOutlined } from "@ant-design/icons";
import { Button, Result, Typography } from "antd";
import { useNavigate, useParams } from "react-router-dom";

import { useThemePalette } from "../../app/providers";
import { getPlatform } from "../../lib/platforms";

const { Paragraph } = Typography;

export function ComingSoonPage() {
  const { platformId } = useParams();
  const platform = getPlatform(platformId);
  const navigate = useNavigate();
  const p = useThemePalette();

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "100vh",
        background: p.pageBg,
      }}
    >
      <Result
        icon={
          <div
            style={{
              width: 72,
              height: 72,
              borderRadius: 18,
              background: platform.accent_color,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 800,
              fontSize: 36,
              color: "#fff",
              margin: "0 auto 8px",
            }}
          >
            {platform.name_cn.slice(0, 1)}
          </div>
        }
        title={`${platform.name_cn} 工作区即将开放`}
        subTitle={`${platform.name_en} 已进入平台注册表。`}
        extra={
          <Button
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate("/platform-select")}
          >
            返回平台选择
          </Button>
        }
      >
        <Paragraph type="secondary" style={{ textAlign: "center", maxWidth: 480, margin: "0 auto" }}>
          该平台尚未开放。
        </Paragraph>
      </Result>
    </div>
  );
}
