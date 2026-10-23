# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：SpiderXHS 桌面版（设计 14）。

用法（一般走 packaging/build.ps1，不必手跑）：
    <build-venv>/python -m PyInstaller --noconfirm --clean \
        --distpath packaging/dist --workpath packaging/build packaging/SpiderXHS.spec

产物：packaging/dist/SpiderXHS/（onedir；build.ps1 再往里组装
frontend/dist、runtime/node、config/default.yaml）。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# PyInstaller 6.x 的 spec 命名空间只注入 SPECPATH（spec 所在目录）等变量，
# 没有 __file__（用了会 NameError）。
SPEC_DIR = SPECPATH
REPO_ROOT = os.path.dirname(SPEC_DIR)


def _data(src: str, dest: str) -> tuple[str, str]:
    return (os.path.join(REPO_ROOT, src), dest)


def _safe_submodules(name: str) -> list[str]:
    """收集子模块，**包没装就返回空**（不中断构建）。

    为什么需要：有些依赖是**可选的**（如 `psutil` —— `singleton.py` 用
    try/except 兜底，`requirements.txt` 里也没有它）。PyInstaller 的
    `collect_submodules` 对不存在的包会抛异常，直接让整个构建失败。
    """
    try:
        return collect_submodules(name)
    except Exception:  # noqa: BLE001 - 可选依赖未安装
        return []


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
#: 违规词检测的**主引擎**是 RapidOCR（现为 `rapidocr` 3.x / PP-OCRv5 ONNX，纯 CPU）；
#: PyInstaller **不会**自动收集它的 `.onnx` 模型（它们是包内数据文件，
#: 不是被 import 进来的），也不一定跟到 `rapidocr` 的隐式导入。
#: 漏了的后果：本地开发环境一切正常（模型在 site-packages 里），
#: 而**装好的客户端**一点「预检」就 500 —— 现场报「检查违规词异常 预检失败」。
#: ⇒ 把整个包目录（含 `models/*.onnx`、`config.yaml`、`default_models.yaml`）按原
#: 相对路径拷进 `_internal`，让 `rapidocr` 在 frozen 下能按 `__file__` 找到模型。
#:
#: ⚠️ **3.x 的模型是首次使用时自动下载到 `models/` 的**。构建机（本机）跑过一次
#: 预检后 `models/` 里就有 3 个 onnx；若构建机上没有，`_rapidocr_data` 只会收到
#: 空目录 ⇒ 装包版第一次预检要联网下载（离线机器直接失败）。构建自检
#: （`build.ps1` / 测试）要断言模型文件确实进了包。

_OCR_PKG_NAME = "rapidocr"


#: ⚠️⚠️ **onnx 模型不再进包**（用户口径 2026-10-23）：
#:
#: 「用户不安装 py 环境，只下载能下载的三方依赖，减少打包后的体积」。
#: 三个模型实测 30.4 MB（`PP-OCRv6_rec_small` 20.3 + `det_small` 9.5 + `cls` 0.6），
#: 改为**首次启动下载**到 `deps/ocr_models/`（见 `env_bootstrap_service`）。
#:
#: ⚠️ 只排除 `.onnx`，**rapidocr 的 python 代码与 yaml 配置仍然进包** ——
#: 那是程序逻辑不是资源，外置会让「用户要装 py 环境」这个前提成立。
_MODEL_EXT = ".onnx"


def _rapidocr_data():
    """把已安装的 `rapidocr` 整包收进 `_internal`（**不含 onnx 模型**，见上方说明）。"""
    try:
        import importlib

        _rapid = importlib.import_module(_OCR_PKG_NAME)  # noqa: PLC0415 - 构建期探测
    except ImportError:
        return []
    pkg_dir = os.path.dirname(_rapid.__file__)
    out = []
    for root, _dirs, files in os.walk(pkg_dir):
        for name in files:
            #: 模型外置（用户安装时下载）——只跳过 .onnx，代码与 yaml 照收
            if name.endswith(_MODEL_EXT):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, pkg_dir)
            out.append(
                (
                    src,
                    os.path.join(_OCR_PKG_NAME, os.path.dirname(rel))
                    if os.path.dirname(rel)
                    else _OCR_PKG_NAME,
                )
            )
    return out


datas.extend(_rapidocr_data())


#: ⚠️⚠️ **ffmpeg / ffprobe 必须进包**（2026-10-23 实测踩到）：
#:
#: 现象：1.0.5 安装版「点击预检后视频**没有勾选**」+「勾选后自动上传仍提示
#:   『数据包中没有视频』」，而开发机（源码 + 5173）完全正常。
#:
#: 根因：`image_ocr_service.scan_video()` 抽帧依赖 `shutil.which("ffmpeg")`。
#:   开发机上 ffmpeg 装在 **PATH**（winget），抽帧成功 ⇒ `ocr_ok=True` ⇒ 默认勾选；
#:   但**安装包进程不继承开发者的 PATH** ⇒ `which()` 返回 None
#:   ⇒ `ocr_ok=None` + `unverified_reason="本机没有可用的 ffmpeg"`
#:   ⇒ `passedMedia` 按 `ocr_ok === true` 过滤 ⇒ **视频被整个剔除**
#:   ⇒ `videoPaths` 恒空 ⇒ 自动加载跳过视频、提交体不带 `videos[]`。
#:   用户手动勾选也救不回来（勾选状态不参与 `passedMedia` 的过滤）。
#:
#: 做法：构建期从 PATH 找到真实文件（`which()` 给的是 WinGet 的 **符号链接**，
#:   必须 `os.path.realpath` 解到实体，否则 PyInstaller 收进去的是个 0 字节链接）。
#: 目标目录 `bin/` —— `image_ocr_service._bundled_bin_dir()` 在 frozen 下按
#:   `sys._MEIPASS/bin` 找，与这里一致。
#: ⚠️⚠️ **ffmpeg 不进包**（2026-10-23 用户口径：能下载的三方依赖改为首次启动下载）。
#:
#: 曾经这里有一段 `_ffmpeg_data()` 把构建机 PATH 里的 ffmpeg/ffprobe 收进 `bin/`，
#: 那是「先让装包版能用视频抽帧」的临时做法。实测 full build 的
#: **ffmpeg.exe 222 MB + ffprobe.exe 222 MB = 444 MB** —— 装包会从 126 MB 涨到 400+ MB，
#: 与「减小安装包体积」的口径直接冲突。
#:
#: 现行做法：`env_bootstrap_service` 在首次启动时从 npmmirror 下载
#: 静态构建（约 28 MB）到 `deps/ffmpeg/`，`image_ocr_service._bundled_bin_dir()`
#: 优先读那里。ffprobe 缺失时 `probe_duration` 回落估算，不影响抽帧。
#:
#: ⇒ 这段**刻意留空**而不是删掉整段：留着注释是为了让「这里曾有个函数、为什么没了」
#:   可追溯（同 `precheck-zones.ts` 里被删的默认映射函数的处理方式）。

#: ⚠️ **第三方包的数据文件也要收**（模块在 ≠ 数据在）：
#:   · openpyxl：模板/样式等随包数据（缺了读某些 xlsx 会莫名失败）
#:   · alembic ：`script.py.mako` 模板 + 自身资源（它生成迁移脚本时要用）
#:   · onnxruntime：ONNX Runtime 的许可/图谱优化库等运行时资源
#: 注意：本项目的迁移脚本（backend/alembic）已由上面 `_data(...)` 显式收，
#: 这里收的是 **alembic 包自身**的那份，两者互不覆盖。
for _pkg in ("openpyxl", "alembic"):
    try:
        datas.extend(collect_data_files(_pkg))
    except Exception:  # noqa: BLE001 - 构建期探测失败不阻断（模块已由 hiddenimports 保证）
        pass

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
        #: ⚠️ 以下全是**函数内延迟 import** 的第三方包 —— PyInstaller 的静态分析
        #: 只看模块顶层，写进 try/函数体的导入**跟不到**，于是包里没有它们，
        #: 而开发环境一切正常（site-packages 里有）。2026-10-20 体检逐个发现：
        #:   · rapidocr            —— 违规词检测主引擎（装包版点预检 500）
        #:   · openpyxl             —— 数据包 xlsx 导入（缺则「缺少 openpyxl 依赖」）
        #:   · alembic              —— 启动时跑迁移（缺则静默退化成 create_all，
        #:                             老库不升级表结构 / 迁移里的数据修正不执行）
        #:   · psutil               —— 多实例租约的 PID 存活探测（缺则走兜底）
        #:   · send2trash           —— 素材文件删除进回收站（缺则抛错不删）
        #:   · xlrd                —— 老式 .xls 数据包（2026-10-21「常用表格格式」）
        #:   · pyxlsb              —— .xlsb 数据包（同为延迟 import）
        + _safe_submodules(_OCR_PKG_NAME)
        + _safe_submodules("openpyxl")
        + _safe_submodules("alembic")
        + _safe_submodules("send2trash")
        + _safe_submodules("psutil")
        + _safe_submodules("xlrd")
        + _safe_submodules("pyxlsb")
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
