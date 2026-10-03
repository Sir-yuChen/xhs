"""素材存储抽象（v2.31，设计 17 §13.3.12）。

## 为什么现在抽（用户口径 2026-10-03）

> 「目前设计的素材库均为本地，**后期可能为会上云存储**」

**当前只实现 `LocalStorage`，零行为变化**。抽的是**接口**，不是加功能 ——
目的是让「上云」将来只写一个新实现类，**不动业务代码**。

## 抽象边界的选取（为什么是这四个方法）

按**业务真正需要的能力**切，不是照搬 SDK：

| 方法 | 业务需要它做什么 | 上云后对应 |
|---|---|---|
| `validate_root` | 拒掉「扫全盘 / 系统目录」这类危险根 | 换成 bucket 前缀 + 权限校验 |
| `make_dir` | 一次生成一个子目录（用户口径「每次生成放不同子目录」） | 云上无目录 ⇒ **空实现**（key 前缀天然如此） |
| `write` | 把结果字节落进去 | `put_object` |
| `resolve` | 缩略图/扫描服务需要**真实路径**读文件 | ⚠️ 云上**没有本地路径** ⇒ 这条是本地实现专有 |

⚠️ 刻意**不做**的：
* **不加 `MaterialStorage` 的工厂/配置项** —— 用哪家云还没定，加了就是空配置；
* **不改表结构**（不加 `storage_type` 列）—— `rel_path` 在本地实现下是相对路径，
  在云实现下天然就是 object key，**语义兼容**，等定了再加；
* **不引云 SDK** —— 本地用不上，白加依赖（用户成本意识：不提前付费/加依赖）。

## 边界：本轮**只覆盖写路径**

`material_save_service`（保存生成结果）是**唯一写素材库的地方**，先抽它。
`material_scan_service`（扫描用户已有目录）是**只读 + 遍历本地文件系统**，
上云后语义完全不同（没有「扫描用户的目录」这回事），**本轮不动**，
留到真上云时按云的能力重新设计（避免现在猜一个用不上的形状）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class MaterialStorageError(RuntimeError):
    """存储层失败（结构化原因，不吞）。"""


class MaterialStorage(Protocol):
    """素材存储后端（**当前唯一实现 = `LocalStorage`**）。"""

    def validate_root(self, root: str) -> tuple[bool, str]:
        """校验根位置可用性，返回 `(ok, reason)`。"""

    def make_dir(self, root: str, relative: str) -> str:
        """按需建目录，返回该目录的**根相对标识**（本地 = 相对路径）。

        ⚠️ 云存储上「目录」是**概念**不是实体 ⇒ 实现可以是空操作，直接返回 `relative`。
        """

    def write(self, root: str, relative: str, data: bytes) -> None:
        """把字节写到「根 + 相对标识」处。"""

    def write_stream(self, root: str, relative: str, chunks: Any) -> int:
        """把字节流写到目标处，返回写入字节数（避免整张图进内存）。"""

    def resolve(self, root: str, relative: str) -> Path:
        """**本地实现专有**：把相对标识解析成本地绝对路径。

        ⚠️ 云实现**不该**实现它 —— 那时调用方要改用 `url_for()` 之类的取址方式。
        保留它是因为**扫描服务与缩略图服务**（本地）确实需要真实路径。
        """


class LocalStorage:
    """本地文件系统实现（**当前唯一实现**）。

    行为与 v2.30 之前**逐字一致** —— 抽接口不改变任何可见行为：
    同样的目录结构、同样的路径校验、同样的写入顺序。
    """

    def validate_root(self, root: str) -> tuple[bool, str]:
        from backend.app.services.material_scan_service import is_safe_material_path

        return is_safe_material_path(str(root))

    def make_dir(self, root: str, relative: str) -> str:
        target = Path(root) / relative
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MaterialStorageError(f"无法创建目录 {target}：{exc}") from exc
        return relative

    def write(self, root: str, relative: str, data: bytes) -> None:
        target = Path(root) / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise MaterialStorageError(f"写入失败 {target}：{exc}") from exc

    def write_stream(self, root: str, relative: str, chunks: Any) -> int:
        target = Path(root) / relative
        written = 0
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                for chunk in chunks:
                    if chunk:
                        handle.write(chunk)
                        written += len(chunk)
        except OSError as exc:
            raise MaterialStorageError(f"写入失败 {target}：{exc}") from exc
        return written

    def resolve(self, root: str, relative: str) -> Path:
        return Path(root) / relative


def get_material_storage() -> MaterialStorage:
    """取当前素材存储后端。

    ⚠️ **现在恒返回 `LocalStorage`**，没有配置分支 ——
    加「按配置选实现」等于现在就为一家还没定的云写死一个空开关
    （用户成本意识：不提前加用不上的东西）。
    将来上云时，这里改成读配置 + 返回 `S3Storage`，**调用方一行不改**。
    """
    return LocalStorage()
