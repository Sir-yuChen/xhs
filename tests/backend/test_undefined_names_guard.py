# encoding: utf-8
"""未定义名字守卫（DEF-080 的落地，2026-09-27）。

**缺陷形态**：`sync_task_service.close_running_logs_for_task` 用了 `shanghai_now()`
却没 import ⇒ 被调用时抛 `NameError`。它编译期抓不到（`python -m compileall` 只查语法）、
测试也抓不到（原有用例都没造 `running` 日志行 ⇒ 函数返回 0 直接跳过，那行从未被执行）
⇒ 只能靠静态检查。本文件守护 `.hermes/scripts/check_undefined_names.py`。

**两个后果都很坏**（这就是它值得一个常驻守卫的原因）：
* `tasks.py::cancel_task` 没兜底 ⇒ 接口 500，而取消在它之前就已 commit
  ⇒ 用户看到「请求失败」但任务真取消了（错误归因）；
* `recover_account_tasks` 用 `except BaseException` 兜底 ⇒ 启动看起来完全正常，
  而孤儿日志永远收不了口（静默失败）。

⚠️ `test_guard_catches_undefined_name` 是**能力验证**：只跑生产代码不报错的用例是
**恒真的假防线**（把脚本逻辑全删掉它照样 PASS）。必须造一个真·未定义名字的文件，
断言守卫能抓到它 —— 否则守卫本身没有被验证过。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".hermes" / "scripts" / "check_undefined_names.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )


def test_backend_app_has_no_undefined_names():
    """生产代码零未定义名字（扫描 `backend/app/**/*.py`）。"""
    result = _run()
    assert result.returncode == 0, (
        "发现未定义名字（运行时必抛 NameError）：\n" f"{result.stdout}\n{result.stderr}"
    )


def test_guard_catches_undefined_name(tmp_path):
    """**能力验证**：真·未定义名字必须被抓到。"""
    (tmp_path / "broken.py").write_text(
        "def f():\n    return shanghai_now()\n", encoding="utf-8"
    )

    result = _run("--scan-root", str(tmp_path))

    assert result.returncode == 1, "守卫抓不到未定义名字：它没有在守护任何东西"
    assert "shanghai_now" in result.stdout


def test_guard_accepts_imported_name(tmp_path):
    """防误报：import 过的名字合法（DEF-080 的修法本身不能被报）。"""
    (tmp_path / "ok.py").write_text(
        "from backend.app.core.time import shanghai_now\n"
        "\n"
        "def f():\n"
        "    return shanghai_now()\n",
        encoding="utf-8",
    )

    result = _run("--scan-root", str(tmp_path))

    assert result.returncode == 0, result.stdout


def test_guard_accepts_scope_local_names(tmp_path):
    """防误报：形参 / 局部变量 / 循环变量 / 推导式 / with / except / 类属性都不算未定义。"""
    (tmp_path / "scoped.py").write_text(
        "import os\n"
        "\n"
        "def f(items, *, limit=1):\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        total += item\n"
        "    doubled = [x * 2 for x in items]\n"
        "    with open(os.devnull) as handle:\n"
        "        handle.read()\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        return str(exc), doubled, limit\n"
        "    return total\n"
        "\n"
        "class C:\n"
        "    attr = 1\n"
        "\n"
        "    def method(self):\n"
        "        return self.attr\n",
        encoding="utf-8",
    )

    result = _run("--scan-root", str(tmp_path))

    assert result.returncode == 0, result.stdout


def test_guard_skips_star_import(tmp_path):
    """防误报：`from x import *` 的模块无法判断名字来源 ⇒ 整体跳过。"""
    (tmp_path / "star.py").write_text(
        "from os.path import *\n"
        "\n"
        "def f():\n"
        "    return join('a', 'b')\n",
        encoding="utf-8",
    )

    result = _run("--scan-root", str(tmp_path))

    assert result.returncode == 0, result.stdout
