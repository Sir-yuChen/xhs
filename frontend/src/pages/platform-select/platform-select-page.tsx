import { ApiOutlined, ArrowRightOutlined, LogoutOutlined } from "@ant-design/icons";
import { Button, Card, Space, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { PlatformSelector } from "../../components/layout/platform-selector";
import { useAuth } from "../../hooks/use-auth";
import { fetchPlatforms } from "../../lib/api";
import { fallbackPlatforms } from "../../lib/platforms";
import type { PlatformMeta } from "../../types";

const { Title, Text } = Typography;

export function PlatformSelectPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [platforms, setPlatforms] = useState<PlatformMeta[]>(fallbackPlatforms);

  useEffect(() => {
    fetchPlatforms().then(setPlatforms);
  }, []);

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#0a0a0a",
        padding: "48px 40px",
      }}
    >
      <div
        style={{
          maxWidth: 960,
          margin: "0 auto",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            marginBottom: 40,
          }}
        >
          <div>
            <Text
              type="secondary"
              style={{
                fontSize: 12,
                textTransform: "uppercase",
                letterSpacing: 1,
                display: "block",
                marginBottom: 4,
              }}
            >
              Choose Workspace
            </Text>
            <Title level={2} style={{ margin: "0 0 8px" }}>
              选择平台工作区
            </Title>
            <Text type="secondary">
              小红书已开放，其它平台保留扩展入口。
            </Text>
          </div>
          <Button
            icon={<LogoutOutlined />}
            onClick={() => void auth.logout()}
          >
            退出登录
          </Button>
        </div>

        {/* 2026-09-29 设计 17 §7.1 v1.5：大模型网关 = 工作平台级公共能力，入口为平台选择页的独立卡片（不在任何平台内） */}
        <PlatformSelector
          platforms={platforms}
          extra={
            <Card
              hoverable
              style={{ borderColor: "#303030", cursor: "pointer" }}
              styles={{ body: { padding: "20px 24px" } }}
              onClick={() => navigate("/llm-gateway")}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                <div
                  style={{
                    width: 44,
                    height: 44,
                    borderRadius: 12,
                    background: "#722ed1",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 20,
                    color: "#fff",
                    flexShrink: 0,
                  }}
                >
                  <ApiOutlined />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Title level={5} style={{ margin: 0, fontSize: 16 }}>
                    大模型网关
                  </Title>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    LLM Gateway
                  </Text>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Tag color="purple" style={{ margin: 0, borderRadius: 4 }}>
                    公共
                  </Tag>
                  <ArrowRightOutlined style={{ color: "rgba(255,255,255,0.45)", fontSize: 14 }} />
                </div>
              </div>
            </Card>
          }
        />
      </div>
    </div>
  );
}
