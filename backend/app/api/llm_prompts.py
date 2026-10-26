"""提示词模板 API（设计 17 §7.2，T2）：公共模块 4 端点 + 模板 4 端点 = 8。

挂 `/api/xhs/llm/` 前缀（§7 契约「生成链路端点挂在 /api/xhs/product-notes/ 与
/api/xhs/llm/ 下」）。

## 门禁与纪律

- **越权 404**：所有单行操作先校验 `user_id`（DEF-066）；「不存在」与「不是你的」
  一律回 404，不泄露存在性。
- **删除被引用 ⇒ 400 不级联**：`PROMPT_MODULE_IN_USE` / `PROMPT_TEMPLATE_IN_USE`
  （列出引用方名称）—— 静默改坏别人的模板比报错危险得多。
- **`is_default` 写时清旧**：同 (user_id, task_type) 只有一个默认（业务不变量，
  不是 DB 唯一约束；同类型允许多模板）。
- **`version` 只升不降**：仅当骨架 / 模块引用变更时 +1（改名不算）。
- **未知插槽渲染即报错**：`prompt_renderer` 单点负责，本层只做错误码映射。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal, get_db
from backend.app.core.deps import get_current_user

logger = logging.getLogger(__name__)
from backend.app.models import (
    GenerationRecord,
    PromptModule,
    PromptTemplate,
    User,
)
from backend.app.models.prompt import PROMPT_TASK_TYPES
from backend.app.schemas.common import paginated
from backend.app.services.prompt_renderer import (
    PromptRenderError,
    render_prompt,
)

router = APIRouter(prefix="/xhs/llm", tags=["xhs-llm-prompts"])


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class PromptModuleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1)
    sort_order: int = 0
    enabled: bool = True

    model_config = ConfigDict(extra="forbid")


class PromptModuleUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    category: Optional[str] = Field(default=None, min_length=1, max_length=64)
    content: Optional[str] = None
    sort_order: Optional[int] = None
    enabled: Optional[bool] = None

    model_config = ConfigDict(extra="forbid")


class PromptTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    task_type: str = Field(min_length=1, max_length=32)
    prompt_skeleton: str = Field(min_length=1)
    module_ids: list[int] = Field(default_factory=list)
    is_default: bool = False

    model_config = ConfigDict(extra="forbid")


class PromptTemplateUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    task_type: Optional[str] = Field(default=None, min_length=1, max_length=32)
    prompt_skeleton: Optional[str] = None
    module_ids: Optional[list[int]] = None
    is_default: Optional[bool] = None

    model_config = ConfigDict(extra="forbid")


class RenderPreviewRequest(BaseModel):
    template_id: int
    #: ⚠️ 设计 75 S5（2026-10-27）：商品台账表（`shop_products`）已 DROP，本地不再有
    #: 「按主键取商品」这条路 ⇒ 该字段退场。前端实测**从未传过它**（只传 `template_id`），
    #: 所以删除不影响任何入口；保留 `extra="forbid"`，老客户端传了会得到 422 而不是被静默忽略。

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def _serialize_module(module: PromptModule) -> dict[str, Any]:
    return {
        "id": module.id,
        "name": module.name,
        "category": module.category,
        "content": module.content,
        "sort_order": module.sort_order,
        "enabled": module.enabled,
        "created_at": module.created_at.isoformat() if module.created_at else None,
        "updated_at": module.updated_at.isoformat() if module.updated_at else None,
    }


def _serialize_template(template: PromptTemplate, modules: list[PromptModule] | None = None) -> dict[str, Any]:
    """模板序列化；`modules` 存在时展开引用的模块（name/content/sort_order）。"""
    payload: dict[str, Any] = {
        "id": template.id,
        "name": template.name,
        "task_type": template.task_type,
        "prompt_skeleton": template.prompt_skeleton,
        "module_ids": list(template.module_ids or []),
        "is_default": template.is_default,
        "version": template.version,
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }
    if modules is not None:
        payload["modules"] = [
            {
                "id": m.id,
                "name": m.name,
                "content": m.content,
                "sort_order": m.sort_order,
                "enabled": m.enabled,
            }
            for m in modules
        ]
    return payload


def _load_modules_by_ids(db: Session, user_id: int, module_ids: list[int]) -> list[PromptModule]:
    """按 id 列表取模块（保持**传入顺序**，§3.2「顺序即拼接序」）。"""
    if not module_ids:
        return []
    rows = db.scalars(
        select(PromptModule).where(
            PromptModule.user_id == user_id, PromptModule.id.in_(module_ids)
        )
    ).all()
    by_id = {row.id: row for row in rows}
    return [by_id[mid] for mid in module_ids if mid in by_id]


def _get_owned_module(db: Session, current_user: User, module_id: int) -> PromptModule:
    module = db.get(PromptModule, module_id)
    if module is None or module.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PROMPT_MODULE_NOT_FOUND", "message": "提示词模块不存在"},
        )
    return module


def _get_owned_template(db: Session, current_user: User, template_id: int) -> PromptTemplate:
    template = db.get(PromptTemplate, template_id)
    if template is None or template.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PROMPT_TEMPLATE_NOT_FOUND", "message": "提示词模板不存在"},
        )
    return template


def _clear_default_for_task_type(db: Session, user_id: int, task_type: str) -> None:
    """同 (user, task_type) 旧默认清零（§7.2 is_default 写时清旧）。

    与一期 `model_configs._clear_default_for_type` 同款语义；此处按 `task_type`
    而非 `model_type` 分桶。
    """
    rows = db.scalars(
        select(PromptTemplate).where(
            PromptTemplate.user_id == user_id,
            PromptTemplate.task_type == task_type,
            PromptTemplate.is_default.is_(True),
        )
    ).all()
    for row in rows:
        row.is_default = False


def _validate_task_type(task_type: str) -> None:
    if task_type not in PROMPT_TASK_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "LLM_TASK_TYPE_UNSUPPORTED",
                "message": f"不支持的任务类型：{task_type}（合法：{'/'.join(PROMPT_TASK_TYPES)}）",
            },
        )


# --------------------------------------------------------------------------- #
# 公共模块（4 端点）
# --------------------------------------------------------------------------- #
@router.get("/prompt-modules")
def list_prompt_modules(
    category: Optional[str] = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(PromptModule).where(PromptModule.user_id == current_user.id)
    if category:
        statement = statement.where(PromptModule.category == category)
    rows = db.scalars(
        statement.order_by(PromptModule.sort_order.asc(), PromptModule.id.asc())
    ).all()
    return paginated([_serialize_module(row) for row in rows], page, page_size)


@router.post("/prompt-modules", status_code=status.HTTP_201_CREATED)
def create_prompt_module(
    payload: PromptModuleCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    exists = db.scalar(
        select(PromptModule).where(
            PromptModule.user_id == current_user.id, PromptModule.name == payload.name
        )
    )
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "PROMPT_MODULE_NAME_EXISTS", "message": "模块名已存在"},
        )

    module = PromptModule(
        user_id=current_user.id,
        name=payload.name,
        category=payload.category,
        content=payload.content,
        sort_order=payload.sort_order,
        enabled=payload.enabled,
    )
    db.add(module)
    db.commit()
    db.refresh(module)
    return _serialize_module(module)


@router.patch("/prompt-modules/{module_id}")
def update_prompt_module(
    module_id: int,
    payload: PromptModuleUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    module = _get_owned_module(db, current_user, module_id)

    if payload.name is not None and payload.name != module.name:
        conflict = db.scalar(
            select(PromptModule).where(
                PromptModule.user_id == current_user.id,
                PromptModule.name == payload.name,
                PromptModule.id != module.id,
            )
        )
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "PROMPT_MODULE_NAME_EXISTS", "message": "模块名已存在"},
            )
        module.name = payload.name
    if payload.category is not None:
        module.category = payload.category
    if payload.content is not None:
        module.content = payload.content
    if payload.sort_order is not None:
        module.sort_order = payload.sort_order
    if payload.enabled is not None:
        module.enabled = payload.enabled

    db.commit()
    db.refresh(module)
    return _serialize_module(module)


@router.delete("/prompt-modules/{module_id}")
def delete_prompt_module(
    module_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除模块；被模板引用 ⇒ 400（**不级联**，列出引用模板名）。

    `module_ids` 是 JSON 列，不能用 SQL 精确判成员 ⇒ 在 Python 侧过滤
    （用户内模板数量有限，可接受；且**只扫本用户**，不越权）。
    """
    module = _get_owned_module(db, current_user, module_id)

    referencing = [
        template
        for template in db.scalars(
            select(PromptTemplate).where(PromptTemplate.user_id == current_user.id)
        ).all()
        if module_id in list(template.module_ids or [])
    ]
    if referencing:
        names = "、".join(t.name for t in referencing[:5])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PROMPT_MODULE_IN_USE",
                "message": f"模块被 {len(referencing)} 个模板引用，无法删除：{names}",
            },
        )

    db.delete(module)
    db.commit()
    return {"id": module_id, "status": "deleted"}


# --------------------------------------------------------------------------- #
# 模板（4 端点）
# --------------------------------------------------------------------------- #
@router.get("/prompt-templates")
def list_prompt_templates(
    task_type: Optional[str] = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(PromptTemplate).where(PromptTemplate.user_id == current_user.id)
    if task_type:
        statement = statement.where(PromptTemplate.task_type == task_type)
    templates = db.scalars(
        statement.order_by(PromptTemplate.id.desc())
    ).all()

    serialized = []
    for template in templates:
        modules = _load_modules_by_ids(db, current_user.id, list(template.module_ids or []))
        serialized.append(_serialize_template(template, modules))
    return paginated(serialized, page, page_size)


@router.post("/prompt-templates", status_code=status.HTTP_201_CREATED)
def create_prompt_template(
    payload: PromptTemplateCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _validate_task_type(payload.task_type)
    if payload.is_default:
        _clear_default_for_task_type(db, current_user.id, payload.task_type)

    template = PromptTemplate(
        user_id=current_user.id,
        name=payload.name,
        task_type=payload.task_type,
        prompt_skeleton=payload.prompt_skeleton,
        module_ids=list(payload.module_ids or []),
        is_default=payload.is_default,
        version=1,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    modules = _load_modules_by_ids(db, current_user.id, list(template.module_ids or []))
    return _serialize_template(template, modules)


@router.patch("/prompt-templates/{template_id}")
def update_prompt_template(
    template_id: int,
    payload: PromptTemplateUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    template = _get_owned_template(db, current_user, template_id)

    if payload.task_type is not None and payload.task_type != template.task_type:
        _validate_task_type(payload.task_type)
        # 换类型时，旧类型的默认标记不再成立 —— 摘掉，避免跨类型假默认
        if template.is_default:
            template.is_default = False
        template.task_type = payload.task_type

    if payload.name is not None:
        template.name = payload.name

    # version 只升不降：仅骨架 / 模块引用变化时 +1
    if payload.prompt_skeleton is not None and payload.prompt_skeleton != template.prompt_skeleton:
        template.prompt_skeleton = payload.prompt_skeleton
        template.version = int(template.version or 1) + 1
    if payload.module_ids is not None and list(payload.module_ids) != list(template.module_ids or []):
        template.module_ids = list(payload.module_ids)
        template.version = int(template.version or 1) + 1

    if payload.is_default is not None:
        if payload.is_default:
            _clear_default_for_task_type(db, current_user.id, template.task_type)
        template.is_default = payload.is_default

    db.commit()
    db.refresh(template)
    modules = _load_modules_by_ids(db, current_user.id, list(template.module_ids or []))
    return _serialize_template(template, modules)


@router.delete("/prompt-templates/{template_id}")
def delete_prompt_template(
    template_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除模板；被 `generation_records.template_id` 引用 ⇒ 400（保留审计可追）。"""
    template = _get_owned_template(db, current_user, template_id)

    used = db.scalar(
        select(GenerationRecord.id)
        .where(
            GenerationRecord.user_id == current_user.id,
            GenerationRecord.template_id == template.id,
        )
        .limit(1)
    )
    if used is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PROMPT_TEMPLATE_IN_USE",
                "message": "模板已被生成记录引用，无法删除（保留审计可追溯）",
            },
        )

    db.delete(template)
    db.commit()
    return {"id": template_id, "status": "deleted"}


def preview_product_facts() -> dict[str, Any]:
    """渲染预览用的**商品插槽示例值**（**唯一构造点**）。

    为什么是示例值而不是真实商品：预览回答的是「插槽名写对了吗 / 拼出来的提示词长什么样」，
    不是「这件商品的真实提示词」（那由生成链路负责，且它按平台**当下**数据渲染）。
    真商品在改造后需要账号 + 一次平台请求，会让预览多出网络失败面 —— 不值。

    ⚠️ 键集合必须与 `product_note_service.PRODUCT_FIELDS` 一致：渲染器按它报缺槽，
    这里少一个键就会把「模板写错」误报成「模板缺插槽」。
    """
    from backend.app.services.product_note_service import PRODUCT_FIELDS

    labels = {
        "item_id": "0000000000000000000000",
        "title": "（示例）商品标题",
        "article_no": "SKU-DEMO",
        "price_min_fen": "9900",
        "price_max_fen": "9900",
        "stock": "100",
        "sku_count": "1",
        "on_sale_sku_count": "1",
        "category": "（示例）分类",
        "sales_total": "10",
    }
    #: 缺键不猜：`PRODUCT_FIELDS` 新增而这里忘补时，值退化成占位串（渲染照常，
    #: 但一眼能看出「这是没配的示例值」），不会静默渲染成空 —— 空值会让
    #: `product_payload` 把它过滤掉，表现为「插槽没填」而非「示例值缺定义」。
    return {key: labels.get(key, f"（示例）{key}") for key in PRODUCT_FIELDS}


@router.post("/prompt-templates/render-preview")
def render_template_preview(
    payload: RenderPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """预览最终 Prompt（**纯本地渲染，不调供应商**，§5.2）。

    商品字段走 `product_payload` 白名单提取（与生成链路同口径，只有一处实现）；
    未选到此商品 / 不是本用户的 ⇒ 404。
    """
    template = _get_owned_template(db, current_user, payload.template_id)

    modules = _load_modules_by_ids(db, current_user.id, list(template.module_ids or []))
    active_modules = [m for m in modules if m.enabled]
    #: ⚠️ 设计 75 S5：商品插槽**用示例值渲染**，不再取真实商品。
    #: 原实现要 `shop_products` 台账行（表已 DROP），而平台实时查商品需要账号 +
    #: 一次平台请求 —— 预览是「保存模板前看插槽写没写错」，不该因此产生网络依赖与
    #: 失败面（没绑千帆的用户也要能预览）。插槽键名仍由 `PRODUCT_FIELDS` 决定，
    #: 渲染器据此报缺槽，判据不受影响。
    facts = preview_product_facts()

    try:
        prompt = render_prompt(
            skeleton=template.prompt_skeleton,
            modules=active_modules,
            product=facts,
        )
    except PromptRenderError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": error.message},
        ) from error

    return {"id": template.id, "name": template.name, "prompt": prompt}


def seed_builtin_prompt_templates(db: Session, *, user_id: int) -> int:
    """为某用户播种内置提示词模块与模板（v2.24，设计 17 §13.5.4）。返回**新建行数**。

    **幂等**：按 `(user_id, name)` 判重 —— 用户改过 / 删过的行**不重建、不覆盖**。
    这点与「代码常量」有本质区别：种子是**起点**，库里的是**用户的**。

    调用时机：应用启动时为**已有用户**播种（`main.py::lifespan`），以及**注册新用户**
    后播种（否则新用户第一次打开出图弹窗仍然没有模板可选 —— 那正是本轮的缺口）。

    ⚠️ **模块先于模板**：模板靠 `module_names` 引用模块 id，顺序反了会解析不到。
    """
    from backend.app.core.initial_data import load_initial_prompt_templates

    payload = load_initial_prompt_templates()
    modules_spec = payload.get("modules") or []
    templates_spec = payload.get("templates") or []
    if not modules_spec and not templates_spec:
        return 0

    created = 0

    # ---- ① 模块（按 name 判重，拿到 name → id 供模板引用） ----
    name_to_module: dict[str, PromptModule] = {}
    if modules_spec:
        existing_modules = {
            row.name: row
            for row in db.scalars(
                select(PromptModule).where(PromptModule.user_id == user_id)
            )
        }
        for spec in modules_spec:
            row = existing_modules.get(spec["name"])
            if row is None:
                row = PromptModule(
                    user_id=user_id,
                    name=spec["name"],
                    category=spec["category"],
                    content=spec["content"],
                    sort_order=spec["sort_order"],
                    enabled=True,
                )
                db.add(row)
                created += 1
            name_to_module[spec["name"]] = row
        db.flush()  # 拿到新行的 id

    # ---- ② 模板（按 name 判重；module_names → module_ids） ----
    if templates_spec:
        existing_templates = {
            row.name
            for row in db.scalars(
                select(PromptTemplate).where(PromptTemplate.user_id == user_id)
            )
        }
        for spec in templates_spec:
            if spec["name"] in existing_templates:
                continue
            module_ids = [
                name_to_module[name].id
                for name in spec["module_names"]
                if name in name_to_module
            ]
            # 默认模板按 (user_id, task_type) 唯一：先清旧的，避免撞唯一约束
            if spec["is_default"]:
                _clear_default_for_task_type(db, user_id, spec["task_type"])
            db.add(
                PromptTemplate(
                    user_id=user_id,
                    name=spec["name"],
                    task_type=spec["task_type"],
                    prompt_skeleton=spec["prompt_skeleton"],
                    module_ids=module_ids,
                    is_default=spec["is_default"],
                    version=1,
                )
            )
            created += 1

    return created


def seed_builtin_prompt_templates_for_all_users() -> int:
    """应用启动时给**每个已有用户**播种（与 `seed_builtin_settings_from_initial_data`
    同型：失败只记 warning 不抛 —— 起服务不能被子系统问题拖死）。

    ⚠️ 用**独立 Session**（:启动阶段没有请求级 db 依赖）。
    """
    from backend.app.models.user import User

    # ⚠️ `SessionLocal` 在**模块层**持有（不是函数内 import）——
    # `conftest.patch_service_sessions` 按模块属性打桩，函数级 import 打不到 ⇒
    # 测试会**默默写生产库**（2026-09-27 已发生两次，见该守卫的 docstring）。
    db = SessionLocal()
    try:
        user_ids = list(db.scalars(select(User.id)))
        total = 0
        for uid in user_ids:
            total += seed_builtin_prompt_templates(db, user_id=uid)
        if total:
            db.commit()
            logger.info("应用初始化数据：为 %s 个用户补种 %s 行提示词模块/模板", len(user_ids), total)
        return total
    except Exception as exc:  # noqa: BLE001 - 起服务不能被子系统问题拖死
        db.rollback()
        logger.warning("提示词模板播种失败（已跳过，可在模板页手动新建）：%s", exc)
        return 0
    finally:
        db.close()
