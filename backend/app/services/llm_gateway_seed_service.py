"""LLM 网关服务商种子播种服务（2026-10-13 用户口径）。

## 用户口径原文

> 「大模型网关中的服务商中，默认将 deepseek、千问、豆包、火山、智谱 等等 —— 调研一下将
> 国内常见的 6 个服务商作为种子数据配置到服务列表中，用户安装登录后只需要配置 key
> 并检测后即可使用。」

## 语义

- **播种时机 = ① 用户注册时**（`api/auth.py::register`，与 `seed_builtin_jobs_for_user` /
  `seed_settings_for_user` / `seed_builtin_prompt_templates` 并列）**+ ② 应用启动时补存量**
  （`main.py::lifespan` → `seed_gateway_providers_for_all_users`，2026-10-13 补：原实现只播
  新注册，存量老账号永远拿不到这 6 个 ⇒ 用户报障「模型服务商依旧未配置」）。
  两处都走同一对幂等函数，**绝无覆盖**。
- **幂等**：按 `(user_id, name)` 判重 —— 已存在的服务商**一律不覆盖**
  （用户改过 base_url、配过 KEY、删过模型的行，绝不还魂）。
- **不含 KEY**：`access_token=""`（用户登录后自填 + 点「测试连接」）。
- **模型行**：按种子登记（含 `model_type` / `platform=["xhs"]`），`model_key` 用
  `_next_model_key` 自然序号（与服务商内其它模型连续，不跳号、不与手工建的撞）。
- 失败**不阻塞注册**（调用方 try/except + warning，同既有三个种子）。

## 与 §13.2.9.7 的关系

`provider_key` 复用 `api/llm_gateway.py::_generate_provider_key`（**唯一生成实现**，
不在本模块重抄一份 —— KEY 一经生成即不可变，两处算法分叉会让种子 KEY 与手工 KEY 风格不一致）。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.models import LlmGatewayConfig, LlmGatewayModel

logger = logging.getLogger(__name__)

#: 种子服务商模型的默认使用平台（一期只服务小红书）。
DEFAULT_SEED_PLATFORM = "xhs"


def seed_gateway_providers_for_user(db: Session, user_id: int) -> int:
    """给某用户播种服务商种子（幂等）。返回新建的服务商条数。

    ⚠️ **不 commit** —— 由调用方（注册流程）统一提交，与其余三个种子的写法一致
    （`seed_builtin_jobs_for_user` 等也只 flush/add）。本函数只负责 add。
    """
    from backend.app.core.initial_data import load_initial_gateway_providers
    # 惰性 import：避免 services ↔ api 的模块级导入环（`_generate_provider_key` 在 api 层）。
    from backend.app.api.llm_gateway import (
        _dump_platforms,
        _generate_provider_key,
        _next_model_key,
    )

    providers = load_initial_gateway_providers()
    if not providers:
        return 0

    # 该用户已存在的服务商名（判重键；一次性查出，不在循环里 N+1）
    existing_names = set(
        db.scalars(
            select(LlmGatewayConfig.name).where(LlmGatewayConfig.user_id == user_id)
        ).all()
    )

    created = 0
    for spec in providers:
        name = spec["name"]
        if name in existing_names:
            continue  # 用户已有同名服务商 ⇒ 不覆盖（含用户改过的）

        provider_key = _generate_provider_key(db, user_id, name)
        config = LlmGatewayConfig(
            user_id=user_id,
            provider_key=provider_key,
            name=name,
            base_url=spec["base_url"],
            access_token="",  # ⚠️ 种子不含 KEY
            enabled=True,
            healthy=False,
        )
        db.add(config)
        db.flush()  # 需要 config.provider_key 落定后再建模型行（同一次 flush 内可见）

        # 模型行（按种子登记；model_key 走自然序号）
        for model_spec in spec.get("models") or []:
            model_key = _next_model_key(db, user_id, provider_key)
            db.add(
                LlmGatewayModel(
                    user_id=user_id,
                    provider_key=provider_key,
                    model_key=model_key,
                    model_name=model_spec["model_name"],
                    model_type=model_spec["model_type"],
                    platform=_dump_platforms({DEFAULT_SEED_PLATFORM: 1}),
                    enabled=True,
                    remark="",
                )
            )
        created += 1
        existing_names.add(name)

    if created:
        logger.info("seed_gateway_providers: 为 user %s 播种 %s 个服务商种子", user_id, created)
    return created


#: 默认业务点映射（用户口径「小红书页的大模型配置页也需要默认指定一个服务商的模型」）。
#: 形状 `(business_key, model_type, 首选服务商名, 该服务商下的模型名)`。
#:
#: 决策依据（2026-10-13）：
#:  * 文本类业务点 → **DeepSeek / deepseek-chat**（六家里最通用的对话模型，成本低）；
#:  * 图片类业务点 → **阿里通义千问 / wanx2.1-t2i-turbo**（六家里唯一带了出图模型的）；
#:  * 视频类业务点（`generate_video`）**不映射** —— 六家种子里没有任何视频模型，
#:    硬映射到文本模型会在调用时炸在类型校验上（`MODEL_TYPE` 不匹配），
#:    如实留空（用户自行配视频服务商，同「不猜类型」纪律）。
#:
#: ⚠️ 只映射**能映射的**；映射失败（服务商/模型不在种子里）静默跳过该条，不报错。
DEFAULT_BUSINESS_MAP: tuple[tuple[str, str, str, str], ...] = (
    ("generate_note", "text", "DeepSeek", "deepseek-chat"),
    ("rewrite_note", "text", "DeepSeek", "deepseek-chat"),
    ("generate_title", "text", "DeepSeek", "deepseek-chat"),
    ("generate_tags", "text", "DeepSeek", "deepseek-chat"),
    ("polish_text", "text", "DeepSeek", "deepseek-chat"),
    ("polish_product_title", "text", "DeepSeek", "deepseek-chat"),
    ("generate_selling_point", "text", "DeepSeek", "deepseek-chat"),
    ("generate_cover", "image", "阿里通义千问", "wanx2.1-t2i-turbo"),
    ("generate_image", "image", "阿里通义千问", "wanx2.1-t2i-turbo"),
    ("describe_image", "image", "阿里通义千问", "wanx2.1-t2i-turbo"),
    ("generate_product_detail_image", "image", "阿里通义千问", "wanx2.1-t2i-turbo"),
)


def seed_default_business_map_for_user(db: Session, user_id: int, *, platform: str = "xhs") -> int:
    """给某用户播种**默认业务点映射**（幂等）。返回新建条数。

    幂等键 = `(user_id, platform, business_key)`（与 `llm_business_providers` 的唯一约束同形）：
    该业务点**已有映射就不碰**（用户选过的服务商绝不还魂）。
    只映射种子里**真实存在**的服务商 + 模型（缺一即跳过该条，不臆造）。
    """
    from backend.app.models import LlmBusinessProvider

    # 该用户名下 服务商名 → (provider_key, {模型名: model_type})，一次性查完（避免 N+1）
    configs = db.scalars(
        select(LlmGatewayConfig).where(LlmGatewayConfig.user_id == user_id)
    ).all()
    name_to_config = {c.name: c for c in configs}
    if not name_to_config:
        return 0

    models_by_provider: dict[str, dict[str, str]] = {}
    for c in configs:
        rows = db.scalars(
            select(LlmGatewayModel).where(
                LlmGatewayModel.user_id == user_id,
                LlmGatewayModel.provider_key == c.provider_key,
            )
        ).all()
        models_by_provider[c.provider_key] = {m.model_name: m.model_type for m in rows}

    existing_keys = set(
        db.scalars(
            select(LlmBusinessProvider.business_key).where(
                LlmBusinessProvider.user_id == user_id,
                LlmBusinessProvider.platform == platform,
            )
        ).all()
    )

    created = 0
    for business_key, want_type, provider_name, model_name in DEFAULT_BUSINESS_MAP:
        if business_key in existing_keys:
            continue
        config = name_to_config.get(provider_name)
        if config is None:
            continue  # 该服务商不在种子/已被用户删 ⇒ 跳过（不臆造）
        actual_type = models_by_provider.get(config.provider_key, {}).get(model_name)
        if actual_type is None or actual_type != want_type:
            # 模型不存在或类型不符 ⇒ 跳过（类型不符硬映射会在调用时炸在类型校验上）
            continue
        db.add(
            LlmBusinessProvider(
                user_id=user_id,
                platform=platform,
                business_key=business_key,
                provider_key=config.provider_key,
                model_name=model_name,
            )
        )
        existing_keys.add(business_key)
        created += 1

    if created:
        logger.info(
            "seed_gateway_providers: 为 user %s 播种 %s 条默认业务点映射（platform=%s）",
            user_id, created, platform,
        )
    return created


def seed_gateway_providers_for_all_users() -> int:
    """**应用启动时**给每个已有用户补种服务商 + 默认业务点映射（幂等）。返回新建服务商条数。

    为什么要「启动时补存量」而不只在注册时播（2026-10-13 用户报障「模型服务商依旧未配置」）：

    用户口径原文「**用户安装登录后**只需要配置 key 并检测后即可使用」—— 这里的
    「安装登录后」既包括**新注册**，也包括**已在用的老账号**。而原实现只在
    `register()` 里播种 ⇒ **存量用户永远拿不到这 6 个种子服务商**（用户实测：
    uid=2 名下只有他早期自建的 `discovery` / `qianwenaiapi`，6 个种子全在
    我的 E2E 测试账号 uid=25 名下）—— 需求没落地。本函数补上这一半。

    ⚠️ **绝不覆盖**（用户口径「已经配置好的参数不带改动」）：两个子函数本身已是
    非破坏性的 —— `seed_gateway_providers_for_user` 按**服务商名**判重（同名skip）、
    `seed_default_business_map_for_user` 按 `(user_id, platform, business_key)` 判重
    （已有映射不碰）。故对存量用户只会**新增**缺失的种子服务商、**补齐**未映射的
    业务点，用户改过的配置一律原样保留。

    ⚠️ 与 `seed_builtin_prompt_templates_for_all_users` 同型：**独立 Session**、
    **失败只记 warning 不抛**（起服务不能被子系统问题拖死）。

    ⚠️ `SessionLocal` 必须在**模块层**持有：`conftest.patch_service_sessions` 按模块
    属性打桩，函数内 import 打不到 ⇒ 测试会默默写生产库。
    """
    from backend.app.models.user import User

    db = SessionLocal()
    try:
        user_ids = list(db.scalars(select(User.id)))
        providers_total = 0
        for uid in user_ids:
            providers_total += seed_gateway_providers_for_user(db, uid)
            seed_default_business_map_for_user(db, uid, platform=DEFAULT_SEED_PLATFORM)
        if providers_total:
            db.commit()
            logger.info(
                "应用初始化数据：为 %s 个用户补种 %s 个网关服务商种子",
                len(user_ids), providers_total,
            )
        else:
            db.rollback()
        return providers_total
    except Exception as exc:  # noqa: BLE001 - 起服务不能被子系统问题拖死
        db.rollback()
        logger.warning("网关服务商种子补种失败（已跳过，可在网关页手动新建）：%s", exc)
        return 0
    finally:
        db.close()
