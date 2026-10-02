"""⚠️ **v2.0 整模块退场**（设计 17 §13.2.4 / §13.2.6）—— 一期「模型配置」6 端点
全部改为 **410 墓碑**，数据层 `model_configs` 表已被迁移 `f8a9b0c1d2e3` drop。

## 为什么是 410 墓碑而不是删掉路由

删掉路由会让旧端点回 **404**，而 404 无法区分「这个端点从来没存在过」与
「功能已退场」—— 排障时会把两者混为一谈（同 ADR-021 / `llm_gateway.py`
的 DELETE 410 退场惯例）。保留路由 + `include_in_schema=False`：

* 旧端点回 **410 Gone** + 结构化 `{code, message}`，指向替代路径（模型配置页）；
* **不计入 OpenAPI**（paths / operations 都不算），前端看不到死端点。

## 退场后业务怎么走（§13.2.4 调用链）

```
业务端点（ai.py 8 个 + product_notes.py + note_gen_llm_worker）
  │  用常量调用 resolve_business_provider(db, user, BUSINESS_KEY_*, platform)
  ▼
llm_business_providers ──provider_key──► llm_gateway_configs
  ▼
ResolvedModelContext(base_url, api_key, model_name, provider_key)
  ▼
OpenAICompatibleTextClient._complete() / OpenAICompatibleImageClient.generate_image()
```

⚠️ 本模块**只保留墓碑**：不再 import `ModelConfig`（表已 drop，引用会变成
对不存在表的 ORM 映射），不做任何库读写。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.deps import get_current_user
from backend.app.models import User

router = APIRouter(prefix="/model-configs", tags=["model-configs"])

#: 退场码（响应体 `code`，供前端按 code 给「去模型配置页」的指向性提示）。
MODEL_CONFIGS_GONE_CODE = "MODEL_CONFIGS_GONE"

_GONE_MESSAGE = (
    "模型配置已退场：请到「模型配置页」为业务点选择服务商"
    "（不同平台和不同的业务可以实时切换服务商）"
)


def _gone() -> HTTPException:
    """统一的 410 墓碑响应（结构化，同 `llm_gateway.py` 的退场口径）。"""
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={"code": MODEL_CONFIGS_GONE_CODE, "message": _GONE_MESSAGE},
    )


# ⚠️ 六个端点的**方法 + 路径**逐一保留（缺一个，旧端点就退化成无信息的 404）。
# `include_in_schema=False` ⇒ 全部不计入 OpenAPI（§13.2.6 端点回数口径）。
# 依赖（`get_current_user` / `get_db`）保持声明：未认证请求仍然先被 401 拦下，
# 与退场前的鉴权语义一致（不因退场而放松鉴权）。

@router.get("", include_in_schema=False)
def get_model_configs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raise _gone()


@router.post("", include_in_schema=False)
def create_model_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raise _gone()


@router.post("/{config_id}/test", include_in_schema=False)
def test_model_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raise _gone()


@router.patch("/{config_id}", include_in_schema=False)
def update_model_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raise _gone()


@router.delete("/{config_id}", include_in_schema=False)
def delete_model_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raise _gone()


@router.post("/{config_id}/set-default", include_in_schema=False)
def set_default_model_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raise _gone()
