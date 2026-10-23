"""端点注册一致性守卫 —— 「函数体写好了，但丢了 @router 装饰器」。

## 为什么需要这条守卫

2026-10-07 实测：`backend/app/api/notes.py` 的 `batch_create_drafts` 有**完整函数体**、
请求模型 `BatchCreateDraftsRequest`、序列化器 `_serialize_draft`，**唯独没有
`@router.post("/batch-create-drafts")`** —— 因此该路由从未存在。

这类缺陷有三个「看不见」的特征，普通手段全都会放行：

1. **端点数口径照旧**（设计 05 登记了它、AGENTS 端点数也是对的）—— 因为它压根没进 OpenAPI，
   端点数只统计已注册的，少一个「本该有」的端点不会让计数失衡；
2. **`openapi.json` 里查不到**，但「查不到」与「这条路不该有」在表面上一样；
3. 唯一会红的是**直接打这个端点的契约用例**，而它表现为 `405`，极易被读成
   「测试写错了 / 环境问题」——本项目它就这么红了**很多轮**没人动。

根因是「注释掉装饰器调试完忘了还原」「重构挪函数时装饰器留在原地」这类纯手误，
**代码本身没有任何语法或类型错误**，编译、lint、`tsc` 全绿。

## 判据

遍历 `backend/app/api/**/*.py` 的**模块顶层函数**，若：

* 函数名**不以 `_` 开头**（私有辅助不是端点；如 `drafts._build_publish_options`
  与 `login_sessions._confirm_phone_login` 就故意不带装饰器），且
* 第一个参数**带类型注解**，且该注解名是**本模块定义的类**、且以 `Request` 结尾
  （= 本模块的 pydantic 请求体），

则它**必须**被 `@router.<verb>(...)` 装饰。否则报红，并把「哪个文件第几行哪个函数」打出来。

⚠️ 判据刻意保守：只认「本模块自己定义的 `*Request` 类」。跨模块导入的请求模型、
用 `Annotated[...]` / `Body(...)` 直接声明的端点不会被误判 —— **宁可漏报不可误报**，
误报会逼人加白名单，白名单一多守卫就死了。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest



#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.redline

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "backend" / "app" / "api"


def _unregistered_endpoints(source: str) -> list[tuple[int, str, str]]:
    """返回 [(行号, 函数名, 请求模型名)]，即「该注册却没注册」的端点。"""
    tree = ast.parse(source)
    request_models = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.endswith("Request")
    }
    found: list[tuple[int, str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        # 私有辅助函数不是端点（如实测的 drafts._build_publish_options）。
        if node.name.startswith("_"):
            continue
        args = node.args.posonlyargs + node.args.args
        if not args:
            continue
        annotation = args[0].annotation
        model_name = annotation.id if isinstance(annotation, ast.Name) else None
        if model_name is None or model_name not in request_models:
            continue
        decorated = any("router." in ast.unparse(d) for d in node.decorator_list)
        if not decorated:
            found.append((node.lineno, node.name, model_name))
    return found


def test_request_bodied_endpoints_are_all_registered_on_a_router():
    """每个「以本模块 `*Request` 为请求体」的公开函数都必须挂路由。"""
    offenders: list[str] = []
    scanned = 0
    for path in sorted(API_DIR.rglob("*.py")):
        scanned += 1
        for lineno, func_name, model_name in _unregistered_endpoints(
            path.read_text(encoding="utf-8")
        ):
            offenders.append(
                f"{path.relative_to(ROOT).as_posix()}:{lineno} "
                f"def {func_name}(payload: {model_name}) —— 缺 @router.<verb>(...) 装饰器"
            )
    assert scanned > 20, f"扫描到的 API 模块过少（{scanned}）—— 目录对不对？"
    assert not offenders, (
        "以下端点有完整实现与请求模型，却没有挂到路由上（外部访问会 404/405）：\n  "
        + "\n  ".join(offenders)
    )


def test_guard_catches_a_missing_decorator():
    """守卫自检：喂一份「丢了装饰器」的样本，必须能抓出来（防止守卫空转）。"""
    sample = (
        "from pydantic import BaseModel\n"
        "class FooRequest(BaseModel):\n"
        "    x: int = 1\n"
        "# 没有装饰器 —— 正是 batch_create_drafts 的形态\n"
        "def do_foo(payload: FooRequest):\n"
        "    return payload\n"
    )
    caught = _unregistered_endpoints(sample)
    assert caught == [(5, "do_foo", "FooRequest")], caught


def test_guard_ignores_private_helpers_and_unrelated_models():
    """守卫自检：私有辅助 / 非 `*Request` 模型 / 已装饰的端点都不得报红。"""
    sample = (
        "from pydantic import BaseModel\n"
        "class FooRequest(BaseModel):\n"
        "    x: int = 1\n"
        "class Thing(BaseModel):\n"
        "    y: int = 1\n"
        "def _build_options(payload: FooRequest):\n"
        "    return payload\n"
        "def not_an_endpoint(data: Thing):\n"
        "    return data\n"
        "@router.post('/foo')\n"
        "def do_foo(payload: FooRequest):\n"
        "    return payload\n"
    )
    assert _unregistered_endpoints(sample) == []


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    pytest.main([__file__, "-q", "-n0"])
