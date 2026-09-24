# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：SpiderXHS 桌面版（设计 14）。

用法（一般走 packaging/build.ps1，不必手跑）：
    <build-venv>/python -m PyInstaller --noconfirm --clean \
        --distpath packaging/dist --workpath packaging/build packaging/SpiderXHS.spec

产物：packaging/dist/SpiderXHS/（onedir；build.ps1 再往里组装
frontend/dist、runtime/node、config/default.yaml）。
"""

import os

from PyInstaller.utils.hooks import collect_submodules

# PyInstaller 6.x 的 spec 命名空间只注入 SPECPATH（spec 所在目录）等变量，
# 没有 __file__（用了会 NameError）。
SPEC_DIR = SPECPATH
REPO_ROOT = os.path.dirname(SPEC_DIR)


def _data(src: str, dest: str) -> tuple[str, str]:
    return (os.path.join(REPO_ROOT, src), dest)


datas = [
    # 配置：安装目录里的可编辑副本由 build.ps1 放置；_internal 这份是
    # config.py 的 __file__ 相对定位兜底（设计 14 §4）。
    _data("config/default.yaml", "config"),
    # 应用初始化数据（设计 15 §4.5）：安装根的可编辑副本由 build.ps1 放；
    # _internal 这份是 config.py `project_root` 相对定位在 frozen 下的兜底
    # （缺它 ⇒ 只剩安装根一条路；打包漏拷时任务中心就空了）。
    _data("config/initial_data", "config/initial_data"),
    # alembic：ini + script_location 整棵树（env.py / script.py.mako /
    # versions/ 都不是 import 进来的，必须显式收）。
    _data("backend/alembic.ini", "backend"),
    _data("backend/alembic", "backend/alembic"),
    # 签名 JS 与运行期数据（mns.js 用 __dirname 读 keystream json）。
    _data("xhs_utils/xhs_core/js", "xhs_utils/xhs_core/js"),
    _data("xhs_utils/xhs_pc/js", "xhs_utils/xhs_pc/js"),
    _data("xhs_utils/xhs_creator/js", "xhs_utils/xhs_creator/js"),
]

# 托盘图标要进 _internal/assets/（launcher.asset_path 在 frozen 下从
# sys._MEIPASS/assets 读）；只打 exe 图标不够 —— 少了它托盘起不来，
# 用户就只剩任务管理器一条退路。build.ps1 会先生成它；手动跑 spec 时
# 不存在则跳过（那时 exe 也不带图标，见下方 _icon 的同类保护）。
_icon = os.path.join(SPEC_DIR, "assets", "app.ico")
if os.path.exists(_icon):
    datas.append((_icon, "assets"))

_version_file = os.path.join(SPEC_DIR, "version_info.txt")
if not os.path.exists(_icon):
    _icon = None
if not os.path.exists(_version_file):
    _version_file = None

a = Analysis(
    [os.path.join(SPEC_DIR, "launcher.py")],
    pathex=[REPO_ROOT],
    binaries=[],
    datas=datas,
    # router 虽是静态 import，services / 适配层可能有延迟 import ⇒ 整包收集。
    hiddenimports=(
        collect_submodules("backend")
        + collect_submodules("apis")
        + collect_submodules("xhs_utils")
        + collect_submodules("pystray")
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pytest 只服务于测试链，进包纯粹占体积（约 10MB+）；
    # tkinter / IPython / notebook 与桌面运行时无关（防钩子误收）。
    excludes=["pytest", "tests", "tkinter", "IPython", "notebook"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SpiderXHS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
    version=_version_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SpiderXHS",
)
