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
    # OCR PowerShell 脚本（单图 + 批量）—— `image_ocr_service` 用
    # `Path(__file__).with_name(...)` 定位；frozen 下必须显式收进包，否则
    # 「违规词检测」整条链路在安装版上 `OCR_SCRIPT_MISSING`。
    _data("backend/app/services/win_ocr_json.ps1", "backend/app/services"),
    _data("backend/app/services/win_ocr_batch.ps1", "backend/app/services"),
]

#: ⚠️⚠️ **RapidOCR 必须显式收进包**（2026-10-20 实测漏了）
#: 违规词检测的**主引擎**是 RapidOCR（PP-OCRv4 ONNX，纯 CPU）；
#: PyInstaller **不会**自动收集它的 3 个 `.onnx` 模型（它们是包内数据文件，
#: 不是被 import 进来的），也不一定跟到 `rapidocr_onnxruntime` 的隐式导入。
#: 漏了的后果：本地开发环境一切正常（模型在 site-packages 里），
#: 而**装好的客户端**一点「预检」就 500 —— 现场报「检查违规词异常 预检失败」。
#: ⇒ 把整个包目录（含 `models/*.onnx`）按原相对路径拷进 `_internal`，
#: 让 `rapidocr_onnxruntime` 在 frozen 下能按 `__file__` 找到模型。


def _rapidocr_data():
    """把已安装的 `rapidocr_onnxruntime` 整包收进 `_internal`（含 onnx 模型）。"""
    try:
        import rapidocr_onnxruntime as _rapid  # noqa: PLC0415 - 构建期探测
    except ImportError:
        return []
    pkg_dir = os.path.dirname(_rapid.__file__)
    out = []
    for root, _dirs, files in os.walk(pkg_dir):
        for name in files:
            src = os.path.join(root, name)
            rel = os.path.relpath(src, pkg_dir)
            out.append((src, os.path.join("rapidocr_onnxruntime", os.path.dirname(rel)) if os.path.dirname(rel) else "rapidocr_onnxruntime"))
    return out


datas.extend(_rapidocr_data())

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
        #: ⚠️ `rapidocr_onnxruntime` 在 `rapid_ocr_service` 里是**函数内延迟导入**
        #: （`from rapidocr_onnxruntime import RapidOCR` 写在 try 块中），
        #: PyInstaller 的静态分析跟不到 ⇒ 必须显式声明，否则装包版
        #: 「违规词检测」拿不到引擎（本地开发正常，因为 site-packages 里有）。
        + collect_submodules("rapidocr_onnxruntime")
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pytest 只服务于测试链，进包纯粹占体积（约 10MB+）；
    # IPython / notebook 与桌面运行时无关（防钩子误收）。
    # ⚠️ **不能排除 tkinter**：`package_precheck._ask_directory` 用它弹
    # 「选择文件夹」对话框，排除后装包用户点选择文件夹只会拿到「没有可用的图形界面」
    # （2026-10-20 实测：先报错，因为 import 失败就直接抛 NO_DESKTOP）。
    excludes=["pytest", "tests", "IPython", "notebook"],
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
