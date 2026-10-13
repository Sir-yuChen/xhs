"""backfill gateway provider seeds for existing users (2026-10-13 用户口径)

用户报障（原文）：「模型服务商依旧未配置」。

## 根因

种子服务商（DeepSeek / 阿里通义千问 / 火山方舟 / 智谱GLM / 月之暗面Kimi / MiniMax）
原实现**只在 `register()` 里播种** ⇒ **存量用户永远拿不到这 6 个**。
用户口径原文是「**用户安装登录后**只需要配置 key 并检测后即可使用」——
「安装登录后」既包括新注册，也包括**已在用的老账号**。
实测：老账号 uid=2 名下只有其早期自建的 `discovery` / `qianwenaiapi`，
6 个种子全落在我 E2E 测试账号 uid=25 名下 ⇒ 需求没落地。

## 本迁移做什么

对**每个已有用户**补种服务商 + 默认业务点映射，**只补缺失、绝不覆盖**：
- 复用**同一对幂等函数**（`seed_gateway_providers_for_user` /
  `seed_default_business_map_for_user`）—— 不在迁移里重写一遍播种逻辑（DEF-044）。
  两者本身按「服务商名」/「(user_id, platform, business_key)」判重 ⇒
  用户改过的 base_url、配过的 KEY、自建的同名服务商、选过的业务点映射**一律原样保留**
  （用户口径「已经配置好的参数不带改动」）。
- 与 `b1e4d7c9a2f3` / `j3k4l5m6n7o8` 等既有迁移同纪律：**迁移可 import 应用服务层**
  （本仓既有先例）。

## 与「启动补种」的关系（**两者都做**，用户 2026-10-13 拍板）

本迁移解决**存量**（一次性跑完即生效，不必等重启）；
`main.py::lifespan → seed_gateway_providers_for_all_users` 解决**将来**（每次启动幂等补，
防「新加的种子对老用户不生效」再漂一次）。两条都走同一对幂等函数，重复执行无副作用。

## downgrade

**不删任何行** —— 播进去的是**应用初始化数据**（服务商/映射的正当内容），
且无法区分「本迁移播的」与「用户自己建的/注册时播的」。
降级回到的代码版本没有这批种子，删掉反而会让老用户少了服务商（同既有种子迁移纪律）。

Revision ID: c2d3e4f5a6b8
Revises: b1c2d3e4f5a7
Create Date: 2026-10-13
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect, text

revision: str = "c2d3e4f5a6b8"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _table_names()
    # 表还没建（极小夹具/早期 revision）⇒ 无事可做（种子播种依赖这两张表）。
    if "llm_gateway_configs" not in tables or "users" not in tables:
        logger.info("gateway provider seed backfill: 目标表不存在，跳过")
        return

    # 用**应用自己的 Session**（与服务层同库、同 ORM 映射）——迁移自带 bind 是
    # alembic 的裸连接，服务层函数要 ORM Session。
    from backend.app.core.database import SessionLocal

    # 惰性 import 服务层（迁移 import 应用服务，本仓既有先例：见模块 docstring）。
    from backend.app.models.user import User
    from backend.app.services.llm_gateway_seed_service import (
        DEFAULT_SEED_PLATFORM,
        seed_default_business_map_for_user,
        seed_gateway_providers_for_user,
    )

    db = SessionLocal()
    try:
        user_ids = [row[0] for row in db.execute(text("SELECT id FROM users")).all()]
        providers_total = 0
        for uid in user_ids:
            providers_total += seed_gateway_providers_for_user(db, uid)
            seed_default_business_map_for_user(db, uid, platform=DEFAULT_SEED_PLATFORM)
        db.commit()
        logger.info(
            "gateway provider seed backfill: %s 个用户，新增 %s 个服务商种子",
            len(user_ids), providers_total,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def downgrade() -> None:
    """**不删任何行**（见模块 docstring：播的是应用初始化数据，且无法区分来源）。"""
    return
