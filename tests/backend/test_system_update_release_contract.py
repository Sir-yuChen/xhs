"""系统更新发布链路契约（设计 36 §1.1 / §2.1，2026-10-13 R1-D）—— **静态守卫**。

用户口径的「静态更新 + 保留历史数据」有两个**结构性**前提，靠静态断言钉住
（真机双装冒烟另有 `packaging/smoke-test.ps1` 的「卸载后 data\\spider_xhs.db 仍在」判据）：

1. `packaging/installer.iss` **不得**有 `[UninstallDelete]` / `[InstallDelete]` 段
   —— 一旦出现并包含 `data\\*` / `logs\\*`，升级或卸载就会删用户历史数据（本需求最高风险点）。
   现状：安装清单只有 `[Files] Source: dist\\SpiderXHS\\*`（只覆盖程序文件），
   数据目录是运行期在 `{app}\\data` 生成、不在安装记录里 ⇒ 升级/卸载自然保留。
2. 发布物命名与更新源解析对齐：`OutputBaseFilename=spider-xhs-setup-{#AppVersion}`
   与设计 36 §2.1 约定的 `spider-xhs-setup-<ver>.exe` 一致（更新源按该名找 assets）。

⚠️ **变异守护**：往 `installer.iss` 加一段 `[UninstallDelete] Type: filesandordirs; Name: "{app}\\data"`
⇒ 本文件必须变红。
"""

from __future__ import annotations

from pathlib import Path

import pytest




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]
INSTALLER_ISS = ROOT / "packaging" / "installer.iss"
BUILD_PS1 = ROOT / "packaging" / "build.ps1"
SMOKE_PS1 = ROOT / "packaging" / "smoke-test.ps1"


def _read(path: Path) -> str:
    # installer.iss / *.ps1 可能是 UTF-8 BOM（build.ps1 是 BOM + CRLF），读取侧一律宽容。
    return path.read_text(encoding="utf-8-sig")


def test_installer_never_deletes_user_data():
    """安装器**不得**声明删除 data/ / logs/ / storage/（静态更新保留历史数据）。"""
    text = _read(INSTALLER_ISS)
    for section in ("[UninstallDelete]", "[InstallDelete]"):
        assert section not in text, (
            f"installer.iss 出现了 {section} —— 升级/卸载可能删除用户数据（本需求最高风险点）"
        )


def test_installer_files_source_is_program_only():
    """`[Files]` 只从 PyInstaller 产物目录安装（程序文件），不含 data/。"""
    text = _read(INSTALLER_ISS)
    assert "dist\\SpiderXHS\\*" in text, "安装清单不再从程序产物目录取文件"
    # 数据目录名不得出现在任何 Source 行里（出现即意味着把用户数据当安装文件处理）。
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Source:") and "data\\" in stripped:
            raise AssertionError(f"安装清单把数据目录当成了安装文件：{line}")


def test_release_asset_name_matches_the_update_source_contract():
    """发布物命名 = `spider-xhs-setup-<ver>.exe`（更新源按该名找 Release assets）。"""
    text = _read(INSTALLER_ISS)
    assert "OutputBaseFilename=spider-xhs-setup-{#AppVersion}" in text, (
        "安装器产物命名与更新源契约（spider-xhs-setup-<ver>.exe）不一致"
    )


def test_build_writes_runtime_version_file():
    """打包必须写 `config/version.json`（运行时**唯一**版本真相源，设计 36 §5.1）。"""
    build = _read(BUILD_PS1)
    assert "config\\version.json" in build, "build.ps1 没有写 config/version.json"
    # 正向断言：清单里也校验它存在（缺了 = 版本恒 0.0.0 ⇒ 更新检查失效）
    assert '"config\\version.json"' in build, "build.ps1 的自检清单没有包含 config/version.json"


def test_smoke_keeps_the_data_preservation_assertion():
    """冒烟测试必须保留「卸载后 data/spider_xhs.db 仍在」判据（数据保留的真机口径）。"""
    smoke = _read(SMOKE_PS1)
    assert "卸载后数据目录被删" in smoke, "冒烟缺少「卸载后数据保留」失败判据"
    assert "unread-count" not in smoke  # sanity: 这条与通知无关，防止误贴
