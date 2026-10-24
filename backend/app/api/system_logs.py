"""系统日志 + 当前用户菜单/权限端点（设计 55 §6 / §7）。

| 方法 | 路径 | 权限码 |
|---|---|---|
| `GET` | `/api/system/logs/app` | `system:log:view` |
| `GET` | `/api/system/logs/audit` | `system:log:view` |
| `GET` | `/api/system/logs/audit/{log_id}` | `system:log:view` |
| `GET` | `/api/system/menus/mine` | **登录即可**（无权限码，见下） |
| `GET` | `/api/system/permissions/mine` | **登录即可** |

## 为什么 `/menus/mine` 与 `/permissions/mine` 不挂权限码

它们是**登录后第一件事**：前端拿它渲染侧栏与按钮可见性。
如果给它们挂 `system:menu:view`，那么一个只有 `xhs:item:view` 的用户
一登录就 403，连自己能看的页面都渲染不出来 —— 那是死锁。

它们的「过滤」就是权限判定本身：返回的集合已经是该用户有权看到的部分。

## 菜单树的过滤判据（三个条件同时成立才显示）

1. `menus.status = 'normal'`（功能已上线）
2. 该用户的授权集合包含它（`role_menus`，模板行 ∪ 用户行）
3. `menus.is_visible = true`（前端要显示它）

第 3 条与第 2 条的区别：`is_visible=false` 的菜单**授权了但不显示**
（例如某个按钮级的 F 行、或一个只给 API 用的 C 行）。
管理页要看到全部（含隐藏），侧栏只看可见的 —— 所以树接口与 mine 接口是两套过滤。

## 父节点补齐

授权是按行给的，但**树要能连通**：如果只授权了子页而没授权父目录，
侧栏渲染时那个页面挂不上去（用户看到空白菜单但直输 URL 却能进）。

处理：`mine` 接口返回的集合 = 授权行 ∪ 它们的**祖先链**（仅当祖先也在
`status='normal'` 且 `is_visible=true` 时补）。这比「要求管理员必须连父一起勾选」
更符合直觉，也避免管理页里「勾了子项忘了父项」这类操作事故。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.perm_deps import require_perm
from backend.app.models.user import User
from backend.app.schemas.system import (
    AppLogListOut,
    AuditLogDetailOut,
    AuditLogListOut,
)

router = APIRouter(prefix="/system", tags=["system"])


# --------------------------------------------------------------------------- #
# 日志（需要 system:log:view）
# --------------------------------------------------------------------------- #
@router.get("/logs/app", response_model=AppLogListOut)
def list_app_logs(
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:log:view")),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> dict:
    """应用日志列表。

    ⚠️ 本项目**没有 `app_logs` 表**（日志走文件 `.hermes/logs/*.log`）。
    本端点返回**文件日志的尾部**（按 `logs` 目录里最新的文件），
    而不是从库里查 —— 库里的 `api_logs` 是 API 审计日志（见下个端点）。

    缺值口径：目录不存在或没有日志文件 ⇒ `items: []` + `note` 说明，
    **不造空行**（硬约束⑤）。
    """
    from backend.app.core.paths import app_root

    log_dir = app_root() / ".hermes" / "logs"
    if not log_dir.is_dir():
        return {
            "items": [],
            "total": 0,
            "note": f"日志目录不存在：{log_dir}",
        }

    files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"items": [], "total": 0, "note": "还没有日志文件"}

    target = files[0]
    try:
        # 只读尾部，不整读（生产日志可能上百 MB）
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:  # noqa: BLE001
        return {"items": [], "total": 0, "note": f"日志不可读：{exc}"}

    tail = lines[-2000:]
    start = max(0, len(tail) - page_size * page)
    chunk = tail[start : start + page_size]
    return {
        "items": [
            {"line_no": start + i + 1, "text": text}
            for i, text in enumerate(chunk)
        ],
        "total": len(tail),
        "file": target.name,
        "page": page,
        "page_size": page_size,
        "note": "来自文件日志的尾部，不是数据库",
    }


@router.get("/logs/audit", response_model=AuditLogListOut)
def list_audit_logs(
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:log:view")),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    endpoint: Optional[str] = None,
    status_code: Optional[int] = None,
) -> dict:
    """API 审计日志（`api_logs` 表，DEF-019 的 API 审计中间件写入）。

    ⚠️ 表里**只有 `endpoint`**（形如 `POST /api/xhs/items`），没有独立的
    `method` / `path` 两列 —— 方法与路径合存在一列里。筛「按方法」要
    `endpoint LIKE 'POST %'`，所以这里的 `endpoint` 参数是**模糊匹配整串**。
    """
    from backend.app.models.api_log import ApiLog

    stmt = select(ApiLog)
    if endpoint:
        stmt = stmt.where(ApiLog.endpoint.ilike(f"%{endpoint.strip()}%"))
    if status_code is not None and hasattr(ApiLog, "status_code"):
        stmt = stmt.where(ApiLog.status_code == status_code)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(ApiLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    return {
        "items": [
            {
                "id": r.id,
                "user_id": getattr(r, "user_id", None),
                # 表里只有 endpoint（"POST /api/..."），没有独立 method / path 列
                "endpoint": getattr(r, "endpoint", ""),
                "platform": getattr(r, "platform", None),
                "status": getattr(r, "status", None),
                "status_code": getattr(r, "status_code", None),
                "created_at": r.created_at.isoformat() if getattr(r, "created_at", None) else None,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/logs/audit/{log_id}", response_model=AuditLogDetailOut)
def get_audit_log(
    log_id: int,
    *,
    db: Session = Depends(get_db),
    _: User = Depends(require_perm("system:log:view")),
) -> dict:
    """审计日志详情。

    ⚠️ **不返回 `query` / `body`**：DEF-019 的审计中间件**刻意不落这两项**
    （可能含密码 / 令牌）。这里也不能凭空造，只给落了的字段。
    """
    from backend.app.models.api_log import ApiLog

    row = db.get(ApiLog, log_id)
    if row is None:
        raise HTTPException(
            404, detail={"code": "AUDIT_LOG_NOT_FOUND", "message": "日志不存在"}
        )
    return {
        "id": row.id,
        "user_id": getattr(row, "user_id", None),
        "endpoint": getattr(row, "endpoint", ""),
        "platform": getattr(row, "platform", None),
        "status": getattr(row, "status", None),
        "status_code": getattr(row, "status_code", None),
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
        "meta": getattr(row, "meta", None),
        "note": "query / body 不落库（可能含敏感字段），故此处不返回",
    }
