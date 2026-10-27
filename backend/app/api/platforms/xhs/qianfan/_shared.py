"""千帆 shared —— 跨域共用的常量、辅助函数与**转发面 import**。

由 `qianfan.py`（6057 行 / 65 路由）按域拆分而来，**URL 与前缀一条未改**。
路由前缀由 `__init__.py` 的 `router` 统一挂载（仍是 `/xhs/qianfan`）。

## ⚠️ 这个文件里的 import **大多不是给自己用的**

拆包把「一个文件的 import」变成「五个文件的 import」。原来大家同处一模块，
`get_db` / `BaseModel` / 各 model / 各 service 谁用谁取；拆开后若让每个子模块
各自重复 import 一遍，会出现 **5 份同样的 import 清单**（改一处漏四处）。

所以这里统一收口：本文件 import 一次 → 子模块 `from ._shared import (...)`。
代价是本文件的 `Any` / `quote` / `BaseModel` 等**在本文件里确实没被引用**——
它们是**给子模块的转发面**，不是死 import。

`test_no_unused_imports.py` 只认「本文件是否用到」，故整段 import 标 `# noqa`
并写明原因（守卫要求的正规做法）。**不要**因为守卫报红就把它们删掉——
删了子模块会成片 `ImportError`。
"""
from __future__ import annotations  # noqa: F401  # 转发面：本文件不直接用，子模块用


from __future__ import annotations  # noqa: F401  # 转发面：本文件不直接用，子模块用

import json  # noqa: F401  # 转发面：本文件不直接用，子模块用

import logging  # noqa: F401  # 转发面：本文件不直接用，子模块用

import mimetypes  # noqa: F401  # 转发面：本文件不直接用，子模块用

import shutil  # noqa: F401  # 转发面：本文件不直接用，子模块用

from datetime import datetime  # noqa: F401  # 转发面：本文件不直接用，子模块用

from urllib.parse import quote  # noqa: F401  # 转发面：本文件不直接用，子模块用

from pathlib import Path  # noqa: F401  # 转发面：本文件不直接用，子模块用

from types import SimpleNamespace  # noqa: F401  # 转发面：本文件不直接用，子模块用

from typing import Any, Optional  # noqa: F401  # 转发面：本文件不直接用，子模块用

import requests  # noqa: F401  # 转发面：本文件不直接用，子模块用

from fastapi import APIRouter, Body, Depends, HTTPException, status  # noqa: F401  # 转发面：本文件不直接用，子模块用

from pydantic import BaseModel, ConfigDict, Field  # noqa: F401  # 转发面：本文件不直接用，子模块用

from sqlalchemy import func, select  # noqa: F401  # 转发面：本文件不直接用，子模块用

from sqlalchemy.orm import Session  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.adapters.xhs.qianfan_api_adapter import (
    get_qianfan_api_adapter_factory,  # noqa: F401  重导出：`from ...xhs.qianfan import ...` 与测试打桩继续有效
)

from backend.app.core.database import get_db  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.core.deps import get_current_user, get_owned_active_account  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.core.time import shanghai_now  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.api.ai import get_image_ai_client, get_text_ai_client  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.ai_service import ImageAiClient, OpenAICompatibleTextClient  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.core.business_provider_keys import (
    BUSINESS_KEY_PRODUCT_DETAIL_IMAGE,
    BusinessKey,
)  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services import detail_image_prompt, image_prompt  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.llm_gateway_client import _type_satisfies  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.models.material import MaterialFolder  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.material_scan_service import is_safe_material_path  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.media_generation import _media_dir  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.models.platform_account import PlatformAccount  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.models.note import Note, NoteAsset  # noqa: F401  # 转发面：本文件不直接用，子模块用



from backend.app.models.task import Task  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.shop_health_service import check_account_health  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services import scheduled_job_runner, sync_task_service  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services import order_sync_service  # noqa: F401  # 转发面：本文件不直接用，子模块用


from backend.app.services import shop_products_service  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services import shop_item_operate_service as operate_service  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services import shop_draft_package as draft_package  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services import shop_draft_service as draft_service  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.job_registry import (
    JOB_KEY_SHOP_GOODS_SYNC,
)  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.picture_space_service import (
    MATERIAL_ORDER_FIELDS as MATERIAL_ORDER_FIELDS,
)  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.note_queue_service import (
    SOURCE_ACCOUNT_CARD_GOODS_SYNC,
)  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.sync_task_service import (
    ABSOLUTE_PAGE_CAP,
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SHOP_GOODS_SYNC_TASK_TYPE,
    SYNC_MODE_AUTO,
    SYNC_SCOPE_FULL,
)  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.note_cleanup_service import CandidateQuery, list_candidates  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.services.shop_binding_service import (
    get_shop_state,
    has_ark_session,
)  # noqa: F401  # 转发面：本文件不直接用，子模块用

from backend.app.models.user import User  # noqa: F401  # 转发面：本文件不直接用，子模块用

#: 售后写链路扫订单的上限（平台没有「按售后单查」的口子，只能顺着最近订单找）。
SCAN_ORDERS = 50

logger = logging.getLogger(__name__)

def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""

def _owned_account(db: Session, current_user: User, account_id: int) -> PlatformAccount:
    """（薄别名）**唯一实现见 `core/deps.py::get_owned_active_account`**（DEF-055）。

    本函数曾是同一语义的 **4 份拷贝之一，且漏了软删闸** —— 而它一家就有 **11 个调用点**
    （商品列表 / 商品同步 / 绑定 / 解绑 / 巡检 / item-notes / 商品挂链笔记…），
    等于把「已删除的账号仍能发起平台操作」这个口子放大了 11 倍。
    """
    return get_owned_active_account(db, current_user, account_id, platform="xhs")

def _fmt_state_time(value: Any) -> str:
    """`shop_state` 里的 ISO 时间 → 展示用 `YYYY-MM-DD HH:MM`；空 / 坏值 → 空串。"""
    from backend.app.services.shop_binding_service import parse_state_time

    parsed = parse_state_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed is not None else ""

def _account_shop_summary(db: Session, account: PlatformAccount) -> dict[str, Any]:
    """账号的千帆能力概览（**纯本地**，不打千帆）。

    ## `bound` 字段名保留，语义已改（ADR-021 / 设计 18，2026-09-30）

    用户 2026-09-30 口径：*「商品页走的本地数据库数据，不应该校验是否存在千帆店铺，
    所有校验应该校验的是该小红书账号下是否存在千帆可用的会话」*。

    ⇒ **`bound` 现在回答的是「本账号有没有可用的 `ark` 会话」**，而不是
    「有没有绑店」。字段名刻意不变（省前端连带改），但语义写在这里，
    前端提示语也必须跟着改（旧文案「请先绑定千帆店铺」现在是错的）。

    * `shop_id_fk` 字段**保留但恒为 `None`** —— 店铺实体已退场。留着是给老前端
      一个明确的空值，而不是让它 `KeyError`；新前端不该再读它。
    * `shop_user_id` / `shop_name` 改从 `account.seller_id` / 账号昵称派生
      （`seller_id` 是账号属性了，店铺名就是账号的千帆卖家名）。

    ⚠️ **归属（seller_id）与能力（ark 令牌）是两件事**：有 `seller_id` 没有令牌
    ⇒ 仍然 `bound=False`（调不了千帆）；有令牌没有 `seller_id` ⇒ `bound=True`
    （能调，`seller_id` 等首次真调千帆时回填）。账号 9 就是后者。
    """
    state = get_shop_state(account)
    has_ark = has_ark_session(db, account.id)
    return {
        "platform_account_id": account.id,
        #: ⚠️ **语义已改为「有可用的 ark 会话」**（不是「已绑店」）。见 docstring。
        "bound": has_ark,
        #: 已退场字段（店铺实体不存在了）。恒 `None`，只为老前端不 `KeyError`。
        "shop_id_fk": None,
        "shop_user_id": account.seller_id or "",
        "shop_name": _shop_display_name(db, account),
        "status": str(state["status"]),
        "status_message": str(state["status_message"]),
        "goods_note_total": int(state["goods_note_total"] or 0),
        #: ⛔ `synced_notes` / `goods_synced_at` **已删除**（2026-10-27）：笔记与商品
        #: 都改平台实时查询后，「本地同步了多少条」这个概念不存在了 —— 那两个值一个
        #: 恒 0、一个恒空串（`shop_note_goods` 已 DROP），留着只会让前端以为有覆盖率。
        #: 最近一次千帆登录态巡检时间（T-31）；空 = 从未巡检。
        "health_checked_at": _fmt_state_time(state["health_checked_at"]),
    }

def _shop_display_name(db: Session, account: PlatformAccount) -> str:
    """店铺展示名（**委托给服务层唯一实现**，不在 API 层再写一份）。

    见 `shop_binding_service.shop_display_name` —— 商品页与订单页共用同一份。
    """
    from backend.app.services.shop_binding_service import shop_display_name

    return shop_display_name(account)

def _datetime_from(value: str | None, *, end_of_day: bool) -> datetime | None:
    """发布时间入参 → `datetime`（东八区）。

    支持日期 `YYYY-MM-DD`（`end_of_day=True` 取当天 23:59:59）、
    完整时刻 `YYYY-MM-DD HH:MM:SS`、以及**毫秒 / 秒**时间戳整数
    （千帆 `publish_time` 原文是毫秒）。无法解析 → `ValueError`（调用方转 400）。
    """
    from datetime import datetime, timedelta

    from backend.app.core.time import SHANGHAI_TZ

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        stamp = int(text)
        #: 纯数字按千帆口径当**毫秒**；小于 10 位（看起来是秒）自动按秒处理，
        #: 否则 1970 年附近的毫秒值会把整段区间筛成空集。
        if stamp < 10_000_000_000:
            stamp = stamp * 1000
        return datetime.fromtimestamp(stamp / 1000.0, tz=SHANGHAI_TZ)

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(
            f"无法解析的发布时间：{text}（支持 YYYY-MM-DD / YYYY-MM-DD HH:MM:SS / 时间戳）"
        )
    if fmt == "%Y-%m-%d" and end_of_day:
        parsed = parsed + timedelta(hours=23, minutes=59, seconds=59)
    return parsed.replace(tzinfo=SHANGHAI_TZ)

def _price_fen(value: float | None) -> int | None:
    """元 → 分（千帆口径）。**全链路只有这一处做换算**。"""
    if value is None:
        return None
    return int(round(float(value) * 100))

def _ts_text(value: Any) -> str:
    """毫秒时间戳 → `YYYY-MM-DD HH:MM`（千帆列表给的是毫秒整数）。

    与 `shop_products_service._ts_text` 同形：本模块自用（item-notes 的
    `publish_time`），契约函数只有 `normalize_item_from_raw` 那一份。
    """
    try:
        stamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    from datetime import datetime, timezone, timedelta

    return (datetime.fromtimestamp(stamp / 1000, tz=timezone(timedelta(hours=8)))).strftime(
        "%Y-%m-%d %H:%M"
    )

def _epoch_millis(value: str | int | None, *, end_of_day: bool) -> int | None:
    """创建时间入参 → Unix **毫秒**（v1.32 起本地筛选用，设计 13 §30.3.2）。

    改造前这个函数返回**秒**（千帆 `search_item_v2` 只认秒，传毫秒被 ES 500）；
    现在列表查本地 `shop_products.create_time_ms`（平台原文是毫秒），所以同一套
    解析、另一个单位。单位换算**仍然只有这一处** —— 「有的地方乘了 1000」
    这类错值不会报错，只会静默筛出空集。

    - `YYYY-MM-DD`：`end_of_day=False` 取 00:00:00.000；`end_of_day=True` 取
      23:59:59.999（用户选「截止 7-16」的自然含义是**含当天**）。
    - `YYYY-MM-DD HH:MM:SS`：按给定时刻取。
    - 整数/数字字符串：按 Unix **秒**透传并换算成毫秒（契约口径不变）。
    - 空值：`None`。无法解析：抛 `ValueError`（由调用方转成 400，不静默忽略）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text) * 1000

    from datetime import datetime, timedelta

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"无法解析的时间：{text}（支持 YYYY-MM-DD / YYYY-MM-DD HH:MM:SS / Unix 秒）")

    if fmt == "%Y-%m-%d" and end_of_day:
        parsed = parsed + timedelta(hours=23, minutes=59, seconds=59, milliseconds=999)
    # 平台按东八区理解这些时刻（本系统所有展示时间也统一东八区）
    from backend.app.core.time import SHANGHAI_TZ

    return int(parsed.replace(tzinfo=SHANGHAI_TZ).timestamp() * 1000)

def _resolve_style_reference(
    db: Session,
    current_user: User,
    *,
    file_id: int | None,
) -> tuple[str, str]:
    """把**风格参考图**（素材库文件 ID）解析成上游可取的 URL + 提示词说明（§13.7）。

    返回 `(url, note)`；不传 ⇒ `("", "")`（可选增强，零回归）。

    ⚠️ **只收文件 ID，不收 URL**：出图端点已有 SSRF 防线（商品参考图必须过台账
    白名单），若风格图收 URL 就等于开了第二个「下载任意地址」的口子。
    文件 ID 的归属校验走既有唯一实现（与素材端点同口径）。

    ⚠️ **归属不符 ⇒ 404，不静默忽略**：静默忽略的观感是「出图成功了」，
    但用户精心挑的风格图**根本没参与**（与 §13.6.3「不静默回落」同一纪律）。
    """
    if not file_id:
        return "", ""

    from backend.app.models.material import MaterialFile

    row = db.scalar(
        select(MaterialFile).where(
            MaterialFile.id == file_id,
            MaterialFile.user_id == current_user.id,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "STYLE_REFERENCE_NOT_FOUND",
                "message": "风格参考图不存在或不属于当前用户",
            },
        )
    if row.file_type != "image":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "STYLE_REFERENCE_NOT_IMAGE",
                "message": "风格参考图必须是图片文件",
            },
        )
    # 复用素材端点同一个取字节 URL（`/api/xhs/materials/files/{id}/file`）——
    # `ai_service._resolve_image_ref` 认这个前缀并转 base64 送上游。
    return f"/api/xhs/materials/files/{row.id}/file", detail_image_prompt.style_reference_note()

def _render_detail_image_template(
    db: Session,
    current_user: User,
    *,
    template_id: int,
    item: dict[str, Any],
    skus: list[dict[str, Any]],
    extra_prompt: str = "",
) -> tuple[str, str]:
    """用公共提示词模板渲染详情图提示词（v2.24，设计 17 §13.5.4）。

    返回 `(提示词, 说明)`；说明供响应 `prompt_source` 如实标注用了哪个模板。

    ⚠️ **不静默回落**：模板不存在 / 不属于本用户 ⇒ 404；骨架含未知插槽 ⇒ 400。
    既然用户**显式选了**模板，「以为用了、其实没用」比报错更坏。
    """
    from backend.app.models.prompt import PromptModule, PromptTemplate
    from backend.app.services import prompt_renderer

    template = db.scalar(
        select(PromptTemplate).where(
            PromptTemplate.id == template_id,
            PromptTemplate.user_id == current_user.id,
        )
    )
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PROMPT_TEMPLATE_NOT_FOUND",
                "message": f"提示词模板 #{template_id} 不存在或不属于当前用户",
            },
        )

    module_ids = [int(mid) for mid in (template.module_ids or []) if isinstance(mid, int)]
    modules: list[PromptModule] = []
    if module_ids:
        modules = list(
            db.scalars(
                select(PromptModule).where(
                    PromptModule.user_id == current_user.id,
                    PromptModule.id.in_(module_ids),
                    PromptModule.enabled.is_(True),
                )
            )
        )

    try:
        rendered = detail_image_prompt.render_template_prompt(
            template=template,
            modules=modules,
            item=item,
            skus=skus,
            extra_prompt=extra_prompt,
        )
    except prompt_renderer.PromptRenderError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": error.code, "message": error.message},
        ) from error

    return rendered, f"模板「{template.name}」"

def _default_detail_image_prompt(item: dict[str, Any], skus: list[dict[str, Any]]) -> str:
    """按商品信息拼默认提示词（用户没填 `prompt` 时用）。

    只用**台账里真实存在**的字段（标题 / 规格名 / 价格带），缺值就不写进提示词 ——
    编造卖点等于替用户宣称商品没有的属性。
    """
    title = str(item.get("title") or "").strip()
    parts = [f"电商商品详情图，商品：{title}" if title else "电商商品详情图"]
    variant_names = [
        str(sku.get("variant_name") or "").strip()
        for sku in skus
        if str(sku.get("variant_name") or "").strip()
    ]
    if variant_names:
        # 去重保序，最多取 4 个（再多提示词会失控）
        seen: list[str] = []
        for name in variant_names:
            if name not in seen:
                seen.append(name)
        parts.append("可选规格：" + "、".join(seen[:4]))
    parts.append("竖版长图构图，干净背景，突出商品主体与卖点排版")
    # v2.21（§13.3.7.7）：**一致性锁** —— 回落路径也带上公共约束库的防漂移三条。
    # ⚠️ 这段是**直接送出图模型的最终 prompt**（不经文本模型），故用公共库的
    # 防漂移与合规约束；构图那句上面已写，不重复。
    parts.extend(image_prompt.GENERATION_SAFETY_LINES)
    parts.extend(image_prompt.PLATFORM_SAFE_LINES)
    return "；".join(parts)

def _save_to_material_folder(
    db: Session,
    *,
    user_id: int,
    folder_id: int,
    url: str,
    filename_hint: str,
) -> dict[str, Any]:
    """把出图**下载到用户指定的素材库文件夹**（设计 17 §13.3.7，v2.19）。

    用户口径：「图片支持下载到本地指定的素材库中也就是本地指定文件夹下」。

    ⚠️ **与 `_download_media` 的区别**：那个写 `storage/media/`（本项目的托管目录，
    给「素材库面板的 media 来源」用）；本函数写**用户自己的绝对路径目录**
    （`material_folders.path`）—— 那是用户在素材库页配置的真实文件夹。

    ⚠️ **只落文件，不建 `material_files` 索引行**：索引由用户点「扫描」时按真实文件
    生成（`material_scan_service.scan_folder`）。在这里插索引行 = 与扫描抢同一份真相源，
    且会与「只索引不搬移」的既有口径打架（§3.4）。
    """
    folder = db.scalar(
        select(MaterialFolder).where(
            MaterialFolder.id == folder_id, MaterialFolder.user_id == user_id
        )
    )
    if folder is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="指定的素材库文件夹不存在（或不属于当前用户）",
        )

    safe, reason = is_safe_material_path(folder.path)
    if not safe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"素材库文件夹路径不可写：{reason}",
        )

    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        # 本地 media URL ⇒ 直接复制（已在磁盘上）。
        if url.startswith("/api/files/media/"):
            source = _media_dir() / url.split("/")[-1]
            if not source.is_file():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="出图本地文件不存在，无法存入素材库",
                )
            target_dir = Path(folder.path)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{filename_hint}{source.suffix or '.png'}"
            shutil.copyfile(source, target)
            return {"path": str(target), "folder_id": folder.id, "filename": target.name}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无法识别的出图地址：{str(url)[:64]}",
        )

    try:
        response = requests.get(url, timeout=120)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"出图下载失败，无法存入素材库：{exc}",
        ) from exc

    content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"上游返回的不是图片（Content-Type={content_type}），未存入素材库",
        )
    extension = mimetypes.guess_extension(content_type or "") or ".png"
    if extension == ".jpe":
        extension = ".jpg"

    target_dir = Path(folder.path)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{filename_hint}{extension}"
    target.write_bytes(response.content)
    return {"path": str(target), "folder_id": folder.id, "filename": target.name}

def _text_model_context(
    db: Session, user: User, business_key: BusinessKey
) -> tuple[Any | None, Any | None, str]:
    """取**文本/多态模型**上下文（详情图提示词整理用，v2.19）。

    ⚠️ **与 `_business_model_context` 的关键区别：缺配置不抛异常，返回 `(None, None, "")`**
    —— 整理是可选的增强层，缺失时由调用方**回落本地拼接**。
    用同一个业务点 key 解析：用户可能给该业务点挂的是**多态**模型（既能整理又能出图），
    也可能只挂了 image 类 ⇒ 那时类型不符 ⇒ 同样回落（不报错）。

    ⚠️ **不复用 `_business_model_context`**：那个按业务点 `kind` 校验（image 类业务点
    只接受 image/multimodal 模型，且失败即 400）—— 我们要的是「拿得到就用、拿不到就算」，
    语义不同。但**解析入口仍是同一份** `resolve_business_provider`（不另写一份查找）。
    """
    try:
        from backend.app.services.llm_gateway_client import resolve_business_provider

        ctx = resolve_business_provider(db, user, business_key.key)
    except Exception as exc:  # noqa: BLE001 - 未配 / 停用 / 类型不符都回落
        logger.debug("详情图提示词整理：文本模型不可用（%s）", exc)
        return None, None, ""

    model_type = str(getattr(ctx, "model_type", "") or "")
    if not _type_satisfies(kind="text", model_type=model_type):
        # 挂的是纯 image 模型（打不了 /chat/completions）⇒ 回落，不报错。
        return None, None, ""

    config = SimpleNamespace(
        base_url=ctx.base_url,
        model_name=ctx.model_name,
    )
    return OpenAICompatibleTextClient(), config, ctx.api_key

_DRAFT_MAX_IMAGES = draft_service.MAX_DRAFT_IMAGES

_DRAFT_MAX_SKUS = draft_service.MAX_DRAFT_SKUS

_DRAFT_LIMIT_PER_MINUTE = draft_service.DRAFT_LIMIT_PER_MINUTE

def payload_metrics_account_id(account: PlatformAccount) -> int:
    return account.id

def _order_write_error_to_http(error: Any) -> HTTPException:
    """`OrderWriteError` → HTTP（结构化）。门禁类 400，找不到类 404。"""
    from backend.app.services.order_write_service import OrderWriteError

    if not isinstance(error, OrderWriteError):  # pragma: no cover
        return HTTPException(status_code=500, detail={"code": "INTERNAL", "message": str(error)})
    not_found = {"ORDER_NOT_FOUND", "ORDER_AFTERSALE_NOT_FOUND"}
    code = status.HTTP_404_NOT_FOUND if error.code in not_found else status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=code, detail={"code": error.code, "message": str(error), **error.extra})

class OrderRef:
    """订单的**最小引用**（发货链路只需要这两个字段，见 `ship_confirm_material`）。

    ⚠️ 设计 75 S5（2026-10-27）：原来 `_owned_order` 返回 `orders` 表的一整行。订单域
    改**平台实时查询**后本地没有行了 —— 而发货 / 售后的 payload 只需要
    `order_no` + `package_id`，用一个两字段的小对象代替整行，调用方 `.order_no` /
    `.package_id` 的写法零改动。
    """

    __slots__ = ("order_no", "package_id")

    def __init__(self, order_no: str, package_id: str) -> None:
        self.order_no = order_no
        self.package_id = package_id

    def __repr__(self) -> str:  # pragma: no cover - 排障用
        return f"OrderRef(order_no={self.order_no!r}, package_id={self.package_id!r})"


def _owned_order(db: Session, current_user: User, account_id: int, order_no: str) -> "OrderRef":
    """按订单号取**本账号**的一笔订单（越权 / 不存在 ⇒ 404，不区分二者）。

    ⚠️ 设计 75 S5：改为**实时查平台**（`get_order_page` 按订单号定位 `package_id`）。
    「归属」由账号保证：只在该账号自己的千帆令牌下查询，查不到就是查不到。
    """
    from backend.app.services import realtime_platform_query_service as rt
    from backend.app.services.order_write_service import OrderWriteError

    wanted = str(order_no or "").strip()
    if not wanted:
        raise OrderWriteError("缺少订单号", code="ORDER_NOT_FOUND")
    account = db.get(PlatformAccount, int(account_id))
    if account is None:
        raise OrderWriteError(f"订单不存在或不属于当前账号：{wanted}", code="ORDER_NOT_FOUND")
    try:
        from backend.app.services.shop_binding_service import qianfan_cookies_for_account

        cookies = qianfan_cookies_for_account(db, account.id)
        if not cookies:
            raise OrderWriteError(
                "该账号没有可用的千帆（ark）登录态，无法读取订单。", code="ARK_SESSION_REQUIRED"
            )
        adapter = get_qianfan_api_adapter_factory()(cookies)
        #: 平台必须给**有界**时间窗口（无界会挂 15 秒）⇒ `list_orders` 自己兜底
        #: 「今天往前 365 天 ~ 现在」，这里显式不传窗口就是调它的兜底路径。
        page = rt.list_orders(adapter, page=1, page_size=50)
    except OrderWriteError:
        raise
    except Exception as exc:  # noqa: BLE001 - 平台失败统一成结构化错误
        raise OrderWriteError(
            f"读取订单失败：{exc}", code="ORDER_QUERY_FAILED"
        ) from exc
    for row in page.get("orders") or []:
        if str(row.get("order_no") or "") == wanted:
            return OrderRef(wanted, str(row.get("package_id") or ""))
    raise OrderWriteError(f"订单不存在或不属于当前账号：{wanted}", code="ORDER_NOT_FOUND")


def _owned_after_sale(db: Session, current_user: User, account_id: int, after_sale_id: str) -> dict:
    """按售后单 ID 取**本账号**的售后单（越权 / 不存在 ⇒ 404）。

    ⚠️ 设计 75 S5：改为**实时查平台**（订单详情里的 `after_sale_list`）。
    平台没有「按售后单 ID 直接查」的口子 ⇒ 必须扫最近的订单详情找它。
    ⚠️ 因此本函数只用于**售后处理**这种低频写动作（一次扫 `SCAN_ORDERS` 笔），
    不要放进列表链路。
    """
    from backend.app.services import realtime_platform_query_service as rt
    from backend.app.services.order_write_service import OrderWriteError

    wanted = str(after_sale_id or "").strip()
    if not wanted:
        raise OrderWriteError("缺少售后单号", code="ORDER_AFTERSALE_NOT_FOUND")
    account = db.get(PlatformAccount, int(account_id))
    if account is None:
        raise OrderWriteError(
            f"售后单不存在或不属于当前账号：{wanted}", code="ORDER_AFTERSALE_NOT_FOUND"
        )
    try:
        from backend.app.services.shop_binding_service import qianfan_cookies_for_account

        cookies = qianfan_cookies_for_account(db, account.id)
        if not cookies:
            raise OrderWriteError(
                "该账号没有可用的千帆（ark）登录态，无法读取售后单。", code="ARK_SESSION_REQUIRED"
            )
        adapter = get_qianfan_api_adapter_factory()(cookies)
        page = rt.list_orders(adapter, page=1, page_size=SCAN_ORDERS)
        for row in page.get("orders") or []:
            package_id = str(row.get("package_id") or "")
            if not package_id:
                continue
            detail = rt.get_order_detail(adapter, package_id=package_id)
            for after_sale in (detail.get("item") or {}).get("after_sales") or []:
                if str(after_sale.get("after_sale_id") or "") == wanted:
                    return dict(after_sale)
    except OrderWriteError:
        raise
    except Exception as exc:  # noqa: BLE001 - 平台失败统一成结构化错误
        raise OrderWriteError(
            f"读取售后单失败：{exc}", code="ORDER_QUERY_FAILED"
        ) from exc
    raise OrderWriteError(
        f"售后单不存在或不属于当前账号：{wanted}", code="ORDER_AFTERSALE_NOT_FOUND"
    )


def realtime_query_error_to_http(error: Any, *, fallback_status: int = 502):
    """`RealtimeQueryError` → `HTTPException`（**结构化原因，不猜**）。

    * 凭据失效（`QIANFAN_CREDENTIAL_EXPIRED`）⇒ **400**：用户能自救（去重登千帆）；
    * 无界 / 反向时间窗口（`ORDER_WINDOW_REQUIRED`）⇒ **400**：是调用方的入参问题，
      **平台请求一个都没发**；
    * 其余平台失败（网络 / 结构异常 / 服务端错）⇒ **502**（我们没错，平台没答上来）。

    与商品域 `_realtime_query_error_detail` **同一判据**（那处是商品页的私有实现，
    本函数是跨域共用面；两处都按 `code` 分流，不按文案）。
    """
    code = getattr(error, "code", "")
    detail = (
        error.as_dict()
        if hasattr(error, "as_dict")
        else {"code": "QIANFAN_QUERY_FAILED", "message": str(error)}
    )
    client_error = code in ("QIANFAN_CREDENTIAL_EXPIRED", "ORDER_WINDOW_REQUIRED")
    return HTTPException(status_code=400 if client_error else fallback_status, detail=detail)


def _ark_adapter_for(db: Session, account: PlatformAccount, factory: Any) -> Any:
    """按账号取 ark 适配器（**不借会话**：拿不到令牌 ⇒ 结构化 400）。

    ⚠️ 工厂返回的是**适配器类**，构造只吃 `cookies` 一个参数（与
    `shop_health_service` 的 `factory(cookies)` 同口径）。传 `account` 会在
    构造处 `TypeError` ⇒ 所有走本函数的端点（发货 / 售后 / 图片空间…）整体 500。
    """
    from backend.app.services.shop_binding_service import qianfan_cookies_for_account

    cookies = qianfan_cookies_for_account(db, account.id)
    if not cookies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARK_SESSION_REQUIRED",
                "message": "该账号没有可用的千帆（ark）登录态，无法执行该操作。"
                "先在账号矩阵导入其千帆登录态。",
            },
        )
    return factory(cookies)

def _material_confirm_token(account_id: int, action: str, item_ids: list) -> str:
    """删除的二次确认指纹（`account + action + 排序后的 id 集`）。

    与订单写链路的 `confirm_token` 同一思路：把「这次要对什么动手」编码进 token，
    前端预览时拿到、确认时原样回传 —— 中间被改（多删 / 换账号）就不匹配。
    """
    import hashlib

    payload = json.dumps(
        {"account": int(account_id), "action": action, "ids": sorted(str(i) for i in item_ids)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

_PERMIT_BATCH = 14

_PERMIT_CRED: dict[str, tuple[str, str]] = {}
