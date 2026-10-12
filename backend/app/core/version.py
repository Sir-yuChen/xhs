"""应用版本 = **单一真相源**（2026-10-13 需求 R1，设计 36）。

## 为什么要有这个文件

用户口径：「新增系统更新逻辑……每次打开软件后需要检查版本是否需要更新」。

版本号此前**没有运行时来源** —— 只有打包脚本 `build.ps1` 传 `-Version` 给
`version_info.txt`（exe 文件属性）与 `installer.iss`（安装器版本）。运行时
（后端 / launcher / 前端）拿不到「我现在是哪个版本」⇒ 无从比对。

本模块给出**唯一读取点**：`current_version()`。

## 值从哪来（优先级）

1. 环境变量 `SPIDER_XHS_VERSION`（打包/测试可显式注入）；
2. 随包分发的 `config/version.json`（`build.ps1` 打包时写入，`{"version": "x.y.z"}`）；
3. 同目录 `_version.txt` 兜底（简单文本）；
4. 都没有 ⇒ `0.0.0`（dev 态，**不臆造版本**）。

⚠️ **不许在别处再写一份版本常量**（DEF-044）：打包脚本、后端、前端都读这里
（前端另经 `GET /api/system/version` 拿，不重复实现）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: dev / 未知态版本（**不臆造**：缺失即 0.0.0）。
UNKNOWN_VERSION = "0.0.0"


def _project_root() -> Path:
    """与 `core/config.py::_load_yaml_config()` 同口径（frozen 下 = `_internal/`）。"""
    return Path(__file__).resolve().parent.parent.parent.parent


def _candidate_files() -> list[Path]:
    candidates: list[Path] = []
    config_file = os.environ.get("CONFIG_FILE")
    if config_file:
        config_path = Path(config_file)
        if not config_path.is_absolute():
            config_path = _project_root() / config_path
        candidates.append(config_path.parent / "version.json")
        candidates.append(config_path.parent / "_version.txt")
    root = _project_root()
    candidates.append(root / "config" / "version.json")
    candidates.append(root / "config" / "_version.txt")
    return candidates


def current_version() -> str:
    """当前应用版本（见模块 docstring 的优先级；都取不到 ⇒ `0.0.0`）。"""
    env = (os.environ.get("SPIDER_XHS_VERSION") or "").strip()
    if env:
        return env.lstrip("vV")

    for path in _candidate_files():
        if not path.is_file():
            continue
        try:
            if path.suffix == ".json":
                with open(path, "r", encoding="utf-8-sig") as handle:
                    payload = json.load(handle)
                value = str(payload.get("version") or "").strip()
            else:
                value = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("version: 读取 %s 失败（%s），跳过", path, exc)
            continue
        if value:
            return value.lstrip("vV")
    return UNKNOWN_VERSION


def compare_versions(a: str, b: str) -> int:
    """语义化版本比较：`a < b` ⇒ -1，`a == b` ⇒ 0，`a > b` ⇒ 1。

    ⚠️ **绝不用字符串比较**：`"1.10.0" < "1.9.0"`（字典序）会把新版本判成旧版本。
    规则：
    1. 剥前缀 `v`/`V`、去空白；
    2. 按 `.` 切段，逐段转 int（**非数字段视为 0**，外部输入不抛错）；
    3. 段数不等时短方补 0（`1.2 == 1.2.0`）；
    4. 逐段数值比较。
    """

    def _parts(value: str) -> list[int]:
        text = (value or "").strip().lstrip("vV")
        out: list[int] = []
        for chunk in text.split("."):
            chunk = chunk.strip()
            out.append(int(chunk) if chunk.isdigit() else 0)
        return out

    pa, pb = _parts(a), _parts(b)
    width = max(len(pa), len(pb))
    pa += [0] * (width - len(pa))
    pb += [0] * (width - len(pb))
    for x, y in zip(pa, pb):
        if x < y:
            return -1
        if x > y:
            return 1
    return 0
