"""全局路径的唯一真相源（修设计 53 的 A2）。

## 为什么必须集中

`Settings.storage_dir` 原来写 `Path("backend/app/storage")` —— **相对 CWD**。
本机从仓库根跑 uvicorn 时看着没问题，但服务器上 `systemd` 的
`WorkingDirectory=` / Docker 的 `WORKDIR` / 从任意目录起 uvicorn，
**任何一处 CWD 不同，媒体文件立刻「全部丢失」**：

* 读不到 **且不报错**（只是列表 0 条，最难查的那类故障）；
* 不同入口还会写到**不同目录**（`/opt/spider-xhs/backend/app/storage`
  vs `/backend/app/storage`），同一份数据被劈成两半。

`frontend_build_dir: "./frontend/dist"` 是同一个病：CWD 不对 ⇒ **整个前端 404**。

## 判据（唯一）

本模块按**文件自身位置**推根目录，**永不看 CWD** ——
`Path(__file__).resolve()` 得到的是 `.../backend/app/core/paths.py`，
`parents[2]` 就是仓库根（或安装目录根）。

守卫：`test_perms_core.py::test_no_cwd_relative_storage_paths` —— 除本文件外，
`backend/app` 出现 `Path("backend/` 或 `Path("./` 即红。
"""

from __future__ import annotations

from pathlib import Path


def app_root() -> Path:
    """项目根（含 `backend/` 与 `frontend/` 的那一层）。

    `core/paths.py` ⇒ `parents[0]` 是 `core/`、`parents[1]` 是 `app/`、
    **`parents[2]` 是 `backend/`**、`parents[3]` 才是仓库/安装目录根。
    ⚠️ 少算一层是**静默错**：`storage_dir()` 会得到
    `<repo>/backend/backend/app/storage` —— 目录能建、文件能写，只是媒体「全部丢失」，
    而且不报错。守卫 `test_paths_are_absolute_and_do_not_depend_on_cwd` 用
    「根下必须有 `backend/app` 与 `frontend`」把它钉住。

    打包成 exe 后 `__file__` 指向 PyInstaller 的解包路径，层级相同，不会退回 CWD。
    """
    return Path(__file__).resolve().parents[3]


def resolve_from_root(value: str | Path) -> Path:
    """把配置里的**相对**路径按 :func:`app_root` 解析成绝对路径。

    绝对路径原样返回（部署时有人写 `/data/spider-xhs/storage` 是完全合理的）。
    空串视为未配置，由调用方决定兜底 —— 这里不猜。
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("path value is empty")
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    return (app_root() / candidate).resolve()


def storage_dir() -> Path:
    """媒体 / 导出 / 暂存的根目录（`<root>/backend/app/storage`）。

    ⚠️ **不在这里 `mkdir`**：有的调用点在只读场景（导出前先探测目录）。
    需要写的调用方自己建（`package_staging_service` 已有）。
    """
    return app_root() / "backend" / "app" / "storage"


def frontend_dist() -> Path:
    """前端构建产物目录（`<root>/frontend/dist`）。`main.py` 静态托管用它。"""
    return app_root() / "frontend" / "dist"


def logs_dir() -> Path:
    """日志目录（`<root>/logs`）—— 与打包自检 / 日志查看页同一口径。"""
    return app_root() / "logs"
