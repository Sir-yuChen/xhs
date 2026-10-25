"""系统更新端点（2026-10-13 需求 R1，设计 36 §5）。

| 方法 | 路径 | 语义 |
|---|---|---|
| `GET`  | `/api/system/version` | 当前版本 + 更新源是否已配置（**免鉴权**：登录页也能显示版本） |
| `GET`  | `/api/system/update/check` | 查最新版（`has_update` / `notes` / `download_url`） |
| `POST` | `/api/system/update/download` | 下载安装包到 `data/updates/` |
| `POST` | `/api/system/update/install` | 触发静默安装（重启到新版） |

前缀 `/api/system`（工作平台公共维度，与 `/api/llm` 同属非平台前缀）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.deps import get_current_user
from backend.app.core.version import current_version, version_info
from backend.app.models import User
from backend.app.services import system_update_service
from backend.app.schemas.system_ops import (
    AppVersionResponse,
    UpdateCheckResponse,
    UpdateDownloadResponse,
    UpdateInstallResponse,
)

router = APIRouter(prefix="/system", tags=["system"])


class UpdateDownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    version: str = Field(default="", max_length=64)

    model_config = ConfigDict(extra="forbid")


class UpdateInstallRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)

    model_config = ConfigDict(extra="forbid")


@router.get("/version", response_model=AppVersionResponse)
def get_version() -> dict:
    """当前应用版本 + 打包时间 + **版本升级历史** + 更新源配置状态（免鉴权）。"""
    source = system_update_service.load_update_source()
    info = version_info()
    return {
        "version": current_version(),
        #: 打包时间 / 提交（用户口径 2026-10-13「本地记录当前版本和打包等时间相关信息」）。
        #: dev 态未打包 ⇒ 空串（**不臆造**）。
        "built_at": info["built_at"],
        "commit": info["commit"],
        "version_source": info["source"],
        #: 版本升级历史（用户口径 2026-10-13「version.json 应记录的是版本升级历史」）——
        #: 前端弹窗按版本折叠展示。最新在前。
        "history": info["history"],
        "source_configured": source.configured,
        "platform": "xhs",
    }


@router.get("/update/check", response_model=UpdateCheckResponse)
def check_update(_: User = Depends(get_current_user)) -> dict:
    """查最新版本（**绝不抛异常**：未配置更新源 ⇒ 返回 `has_update=False`）。"""
    return system_update_service.check_for_update()


@router.post("/update/download", response_model=UpdateDownloadResponse)
def download_update(
    payload: UpdateDownloadRequest,
    _: User = Depends(get_current_user),
) -> dict:
    """下载安装包到 `data/updates/`（同步；URL 必须是 http/https）。"""
    result = system_update_service.download_installer(payload.url, version=payload.version)
    if not result["ok"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UPDATE_DOWNLOAD_FAILED", "message": result["message"]},
        )
    return result


@router.post("/update/install", response_model=UpdateInstallResponse)
def install_update(
    payload: UpdateInstallRequest,
    _: User = Depends(get_current_user),
) -> dict:
    """触发静默安装（仅打包版 Windows；dev 态返回 400 + 明确原因）。"""
    result = system_update_service.launch_silent_installer(payload.path)
    if not result["ok"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UPDATE_INSTALL_FAILED", "message": result["message"]},
        )
    return result
