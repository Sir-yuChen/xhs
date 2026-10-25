"""系统运维域响应模型：版本 / 更新检查 / 下载 / 安装（设计 61 §A6 第二期）。

字段逐字段抄自实现：
* `AppVersionResponse` ← `api/system.py::get_version`（:43-56）
* `UpdateCheckResponse` ← `system_update_service.check_for_update`（:198-205 初始形状）
* `UpdateDownloadResponse` / `UpdateInstallResponse` ← 两个 POST 的 return
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class VersionHistoryEntry(BaseModel):
    """一条版本记录（`version.json` 的 history 项）。

    ⚠️ 字段宽松（`Any`/可选）：`version.json` 是**随包发布的数据文件**，
    新增字段不该因为后端模型没跟上就 500（那是把数据文件的灵活性换掉）。
    """

    version: str = ""
    released_at: Optional[str] = None
    notes: str = ""
    changes: Optional[list[Any]] = None


class AppVersionResponse(BaseModel):
    """`GET /api/system/version`（**免鉴权**：登录页要显示版本号）。"""

    version: str
    #: 打包时间 / 提交（dev 态未打包 ⇒ 空串，**不臆造**）。
    built_at: str = ""
    commit: str = ""
    #: 版本号来源：`packaged` / `source` 等（诊断用）。
    version_source: str = ""
    #: 版本升级历史，最新在前。
    history: list[VersionHistoryEntry] = Field(default_factory=list)
    #: 更新源是否已配置（未配置 ⇒ 检查更新恒 `has_update=False`）。
    source_configured: bool = False
    platform: str = "xhs"


class UpdateCheckResponse(BaseModel):
    """`GET /api/system/update/check`（**绝不抛异常**，未配置源也返回本形状）。"""

    current: str
    latest: str = ""
    has_update: bool = False
    notes: str = ""
    download_url: str = ""
    #: 字节数（未知 = 0，不臆造）。
    download_size: int = 0
    source_configured: bool = False


class UpdateDownloadResponse(BaseModel):
    """`POST /api/system/update/download`。

    ⚠️ `ok=False` 时端点会转成 400（`UPDATE_DOWNLOAD_FAILED`）并带上 `message`，
    所以 200 响应里 `ok` 恒 True —— 但字段保留，前端不必分两条路解析。
    """

    ok: bool = True
    path: str = Field(default="", description="下载到本地的安装包绝对路径")
    size: int = 0
    message: str = ""


class UpdateInstallResponse(BaseModel):
    """`POST /api/system/update/install`。

    ⚠️ 字段名是 `launching`（不是 `launched`）—— 抄自
    `launch_silent_installer` 的实际返回，别按「更自然的英文」改名：
    改了会让前端 `result.launching` 恒 `undefined`（静默失效，不是报错）。
    """

    ok: bool = True
    launching: bool = False
    message: str = ""
