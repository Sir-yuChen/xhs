"""系统运维域响应模型：依赖探测 / 下载进度（设计 61 §A6 第二期）。

字段逐字段抄自 `core/env_bootstrap.py`：
* `DependencyItem` ← `dependency_status()`（:807-827）
* `DependencyStatusResponse` ← `all_status()`（:830-842）
* `DependencyProgressItem` ← `api/system_deps.py::dependency_progress` 的 `snapshot` 条目
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DependencyItem(BaseModel):
    """一个可下载三方依赖的当前状态。"""

    key: str = Field(description="依赖键，如 `window_host` / `ocr_model`")
    name: str
    #: 体积提示（给人看的字符串，如 \"180 MB\"；不是数值）。
    size_hint: str = ""
    detail: str = ""
    #: 落盘目录绝对路径（诊断用）。
    dir: str = ""
    #: 本地是否可用（`probe` 的结果）。
    ready: bool = False
    #: `ready` / `missing`
    status: str = "missing"
    error: str = ""
    #: 能否「清空重下」。
    resettable: bool = False
    #: 必需依赖缺失 ⇒ 主流程不可用（如窗口内核）。
    required: bool = False


class DependencyStatusResponse(BaseModel):
    """`GET /api/system/deps` —— 全部依赖状态 + 汇总。"""

    items: list[DependencyItem] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    required_missing: list[str] = Field(default_factory=list)
    #: 全部就绪（含非必需项）。
    ready: bool = False
    #: 依赖根目录（开发态 `.hermes/deps`，安装态 `<安装目录>/deps`）。
    root: str = ""
    bundled_note: str = ""


class DependencyProgressItem(BaseModel):
    """单个依赖的下载进度。

    ⚠️ `status` 是前端**判断下载到哪一步的唯一依据**：
    `idle` 未开始 / `running` 下载中 / `done` 完成 / `failed` 失败。
    下载刚完成但 `probe` 还没跑完的一瞬间，「`ready` 为假」不代表失败 ——
    那时要以本字段为准（见端点 docstring）。
    """

    key: str
    status: str = "idle"
    percent: int = Field(default=0, description="0–100")
    error: str = ""


class DependencyProgressResponse(BaseModel):
    """`GET /api/system/deps/progress` —— 进度快照 + 当前状态。"""

    progress: dict[str, DependencyProgressItem] = Field(default_factory=dict)
    deps: DependencyStatusResponse
