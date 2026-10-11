"""千帆包结构守卫（2026-10-11 拆包）。

`backend/app/api/platforms/xhs/qianfan.py`（6057 行 / 65 路由，全仓最大文件）
拆成同名的包。**拆包最大的风险不是搬错代码，是 URL 悄悄变了** ——
API 层没有编译期保护，路由前缀写错一个字符就是「前端整块 404 而测试全绿」。

所以本文件钉三件事：

① **URL 逐条一致**：`/api/openapi.json` 里 `/xhs/qianfan*` 的 (路径, 方法) 集合
   与拆包前录的基线**逐条相等**（不是数量相等 —— 少一个多一个都必须红）；
② **兼容转发面在**：`product_notes.py` / `note_sync_service.py` / 三条测试
   仍走 `from ...xhs.qianfan import X`，包必须继续导出那些名字
   （**含下划线私有名** —— `from .x import *` 拿不到它们，这是实测踩过的坑）；
③ **没有裸路径读旧文件**：`qianfan.py` 已不存在，任何源码/测试里再出现
   `Path(...	"qianfan.py")` 都是会 `FileNotFoundError` 或（更糟）断言范围缩小的隐患。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "backend/app/api/platforms/xhs/qianfan"
BASELINE = Path(__file__).resolve().parent / "_data" / "qianfan_openapi_baseline.json"

#: 拆包后的预期文件集合。**加文件是允许的**（新域），删文件必须同步改这里
#: —— 少了 `_shared.py` 之类会让依赖分析静默失效。
EXPECTED_FILES = {"__init__.py", "_shared.py", "goods.py", "orders.py", "picture_space.py", "publish.py"}


def _pkg_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(PKG.rglob("*.py")))


class TestPackageShape:
    def test_package_files_exist(self):
        got = {p.name for p in PKG.glob("*.py")}
        assert EXPECTED_FILES <= got, f"缺文件：{EXPECTED_FILES - got}"

    def test_prefix_and_tags_are_unchanged(self):
        """前缀 / tags 与拆分前**逐字**一致 —— 改这里就是改对外 URL。"""
        init = (PKG / "__init__.py").read_text(encoding="utf-8")
        assert 'APIRouter(prefix="/xhs/qianfan", tags=["xhs-qianfan"])' in init, (
            "父 router 的前缀/tags 变了 —— 对外 URL 会整体移位"
        )
        #: 子 router 不得各自带 prefix（否则路径会叠成 /xhs/qianfan/xhs/qianfan/...）。
        for f in ("goods.py", "orders.py", "picture_space.py", "publish.py"):
            src = (PKG / f).read_text(encoding="utf-8")
            assert re.search(r"^router = APIRouter\(\)\s*$", src, re.M), (
                f"{f} 的子 router 应是不带 prefix 的 `APIRouter()`"
            )
            assert "APIRouter(prefix=" not in src, f"{f} 不得再带 prefix（会与父前缀叠）"

    def test_old_module_is_gone(self):
        assert not (ROOT / "backend/app/api/platforms/xhs/qianfan.py").exists(), (
            "旧模块还在 —— 与包同名会让 import 解析不确定（哪个赢取决于路径顺序）"
        )


class TestCompatibilityReexports:
    """包必须继续导出**外部实际在用的名字**（尤其私有名）。"""

    #: (名字, 为什么它在用) —— 只登记真实调用点，不臆造。
    REQUIRED = {
        "_owned_account": "product_notes.py 直接 import",
        "_ark_adapter_for": "test_picture_space* 两条 monkeypatch 锚点",
        "MAX_EXPORT_ROWS": "test_product_edit.py 的 monkeypatch + 直接 import",
        "get_qianfan_api_adapter_factory": "note_sync_service.py / 多条测试",
        "MATERIAL_ORDER_FIELDS": "test_picture_space.py 白名单守卫",
        "MAX_BATCH_DELETE": "test_shop_items.py / test_page_optimizations.py",
        "ShopItemsRequest": "test_shop_items_query_contract.py",
        "ItemsExportRequest": "test_shop_items_query_contract.py",
        "ItemPublishRequest": "test_item_publish.py",
        "ItemDraftCreateRequest": "test_item_publish.py / test_product_draft.py",
        "ItemDraftSku": "test_item_publish.py",
    }

    def test_required_names_are_reexported(self):
        init = (PKG / "__init__.py").read_text(encoding="utf-8")
        missing = [n for n in self.REQUIRED if n not in init]
        assert not missing, (
            f"包不再导出这些名字：{missing} —— 调用点会 ImportError。"
            "⚠️ 别用 `from .goods import *` 顶替：`*` 不导出下划线名。"
        )

    def test_private_names_really_importable(self):
        """真去 import 一遍 —— 源码里有那个字符串 ≠ 真的能导入。"""
        import importlib

        mod = importlib.import_module("backend.app.api.platforms.xhs.qianfan")
        for name in self.REQUIRED:
            assert hasattr(mod, name), f"`qianfan.{name}` 导入不到"


class TestUrlBaseline:
    def test_openapi_paths_match_baseline_exactly(self):
        """`/xhs/qianfan*` 的 (路径, 方法序) 与拆包前基线**逐条相等**。

        ⚠️ 基线文件是**录下来**的（拆包前从真实 app 取的 openapi），
        不是手抄 —— 手抄的基线会把当时的错一起固化。
        """
        from backend.app.main import create_app

        assert BASELINE.exists(), f"缺 URL 基线：{BASELINE}"
        base: dict[str, list[str]] = json.loads(BASELINE.read_text(encoding="utf-8"))

        spec = create_app().openapi()
        now = {p: sorted(v) for p, v in spec["paths"].items() if "/qianfan" in p}

        missing = sorted(set(base) - set(now))
        extra = sorted(set(now) - set(base))
        changed = sorted(p for p in base if p in now and base[p] != sorted(now[p]))

        assert not missing, f"拆包后丢失的路径：{missing}"
        assert not extra, f"拆包后多出的路径：{extra}"
        assert not changed, (
            f"这些路径的 HTTP 方法变了：{[{p: (base[p], sorted(now[p]))} for p in changed]}"
        )

    def test_baseline_is_not_empty(self):
        """基线自身不许退化 —— 空基线会让上一条恒绿。"""
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        assert len(base) >= 55, f"基线只有 {len(base)} 条，明显不完整"
        total_ops = sum(len(v) for v in base.values())
        assert total_ops >= 55, f"基线只有 {total_ops} 个操作"


class TestNoStalePathReads:
    def test_no_source_reads_old_qianfan_py(self):
        """没有任何 Python 源码再按路径读 `qianfan.py`（已不存在）。

        ⚠️ 这类残留的表现是 `FileNotFoundError` 或（更隐蔽）**断言范围缩小**：
        把路径改成某个子模块后守卫恒绿。统一走
        `tests.backend.conftest.read_qianfan_source()`（读整包）。
        """
        offenders: list[str] = []
        for base in (ROOT / "backend", ROOT / "tests"):
            for p in base.rglob("*.py"):
                if "__pycache__" in p.parts or p == Path(__file__):
                    continue
                src = p.read_text(encoding="utf-8")
                #: 只认**真的读文件**的形态：`"....qianfan.py").read_text(` 或 `open(...)`
                for m in re.finditer(r'["\'][^"\']*qianfan\.py["\']', src):
                    tail = src[m.end():m.end() + 40]
                    if "read_text" in tail or "read_bytes" in tail or "open(" in tail:
                        line = src[:m.start()].count("\n") + 1
                        offenders.append(f"{p.relative_to(ROOT)}:{line}")
        assert offenders == [], (
            f"这些地方还在按路径读已不存在的 qianfan.py：{offenders} —— "
            "改用 tests.backend.conftest.read_qianfan_source()"
        )


class TestDomainSeparation:
    """拆分要真的按域分开，不是「一个大文件切成五个随机块」。"""

    def test_each_domain_has_its_own_router_and_routes(self):
        for f, expect_prefix in (
            ("goods.py", "/items"),
            ("orders.py", "/orders"),
            ("picture_space.py", "/picture-space"),
            ("publish.py", "/item-drafts"),
        ):
            tree = ast.parse((PKG / f).read_text(encoding="utf-8"))
            paths = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in node.decorator_list:
                        if (
                            isinstance(d, ast.Call)
                            and isinstance(d.func, ast.Attribute)
                            and d.func.attr in ("get", "post", "put", "patch", "delete")
                            and d.args
                            and isinstance(d.args[0], ast.Constant)
                        ):
                            paths.append(d.args[0].value)
            assert paths, f"{f} 里一条路由都没有（域分错了？）"
            assert any(p.startswith(expect_prefix) for p in paths), (
                f"{f} 里没有 `{expect_prefix}` 开头的路由 —— 域归属可能漂了"
            )

    def test_shared_holds_no_routes(self):
        """`_shared.py` 只放常量与辅助函数 —— 有路由说明域切分出了问题。"""
        tree = ast.parse((PKG / "_shared.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    assert not (
                        isinstance(d, ast.Call)
                        and isinstance(d.func, ast.Attribute)
                        and d.func.attr in ("get", "post", "put", "patch", "delete")
                    ), f"_shared.py 出现了路由 {node.name} —— 应按域搬走"
