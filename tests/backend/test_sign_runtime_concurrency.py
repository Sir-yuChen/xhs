"""签名运行时并发隔离（DEF-101，2026-10-09）。

真机缺陷：`XHS_Qianfan_Apis._sign` 把签名入参写进**固定文件** `_CORE_JS_DIR/.sign_input.json`
再 `node sign.js @.sign_input.json`。前端并行拉「运费模板 + 物流模板」两个签名请求
（都是 `_signed_post`）⇒ 两个线程同时写/删同一文件 ⇒ `node` 读到空/半截输入，
报 `cookie required` / `Unexpected end of JSON input`（尾巴被截成 `Node.js v22.23.2`）
⇒ **两个下拉恒空**。实测 16 并发 15 失败。

守卫：并发调用 `_sign`，用假 `subprocess.run` 模拟 `sign.js`（**读文件 → 比对**），
每个调用必须拿回**自己那份**入参算出的签名。共用一个临时文件 ⇒ 交叉污染，本用例必红。

变异：把 `tempfile.mkstemp(...)` 换回固定 `.sign_input.json` ⇒ 本用例必红。
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from apis.xhs_qianfan_apis import XHS_Qianfan_Apis


class _FakeRun:
    """`subprocess.run` 替身：读 `@file` 里的入参，按 `data.tag` 回一个签名。

    ⚠️ 读文件**前** sleep 一下，把「写 → 被覆盖/删除 → 读」的竞态窗口拉开；
    文件被别的线程覆盖或删掉时，读到的是**别人的 tag** 或直接 `FileNotFoundError`
    ⇒ 该调用的签名就对不上自己的 tag。
    """

    def __init__(self) -> None:
        self.barrier = threading.Barrier(2, timeout=10)

    def __call__(self, cmd, **kwargs):
        #: cmd = ["node", "sign.js", "@.sign_input_xxx.json"]（相对名，cwd=_JS_DIR）
        from xhs_utils.xhs_core.runtime import _JS_DIR
        import os

        assert cmd[0] == "node" and cmd[1] == "sign.js", cmd
        ref = cmd[2]
        assert ref.startswith("@"), ref
        #: 真实 `subprocess.run(cwd=_JS_DIR)` 下 `@<相对名>` 按 _JS_DIR 解析。
        path = os.path.join(_JS_DIR, os.path.basename(ref[1:]))
        #: 两线程先在此汇合，确保都在「文件已写、node 待读」的窗口内。
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            pass
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            #: 复刻真机失败形态（node 的报错尾巴）。
            return subprocess.CompletedProcess(
                cmd, 1, stdout="",
                stderr=f"Error: {type(error).__name__}\n    at Object.<anonymous> (sign.js:68:28)\n"
                       f"    at node:internal/main/run_main_module:36:49\n\n"
                       f"Node.js v22.23.2\n",
            )
        tag = (data.get("data") or {}).get("tag")
        line = json.dumps({"xs": f"tag-{tag}", "xt": "t", "x3": 1})
        return subprocess.CompletedProcess(cmd, 0, stdout=line + "\n", stderr="")


@pytest.fixture()
def _stub_profile(monkeypatch):
    """把设备 profile 与会话态上下文的取数打桩（签名材料不是本用例的被测面）。"""
    from xhs_utils.xhs_pc import state as state_mod

    ctx = {
        "now": 1700000000000, "version": "1", "loadts": 1, "seq": 1,
        "envConst": "EC", "envFpTail": "FP", "webBuild": "1.0.0",
    }
    fake_profile = SimpleNamespace(
        cookie_map={"a1": "a1val"}, next_sign_context=lambda api: dict(ctx),
        current_b1=lambda now: "b1val",
    )
    monkeypatch.setattr(state_mod, "PcDeviceProfile", lambda **kw: fake_profile)
    monkeypatch.setattr(
        "apis.xhs_qianfan_apis._dsl_pair_or_empty", lambda: ["", ""], raising=False
    )


def test_sign_temp_file_is_isolated_across_concurrent_calls(monkeypatch, _stub_profile):
    """⚠️ DEF-101：并发 `_sign` 必须各用各的临时文件 —— 每个调用拿回自己的签名。"""
    fake_run = _FakeRun()
    monkeypatch.setattr(subprocess, "run", fake_run)
    #: `_sign` 内部 `import subprocess` 后再 `subprocess.run` —— 打桩模块属性即可。

    api = XHS_Qianfan_Apis("a1=abc; web_session=x")
    results: dict[int, str] = {}
    errors: list[str] = []

    def worker(tag: int) -> None:
        headers, err, signed = api._sign("api/some/path", {"tag": tag}, "/ref")
        if err or not headers:
            errors.append(f"tag={tag} err={err}")
        else:
            results[tag] = (signed or {}).get("xs", "")

    threads = [threading.Thread(target=worker, args=(t,)) for t in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"并发签名失败（临时文件未隔离）：{errors}"
    #: 每个调用必须拿回**自己** tag 的签名 —— 共用一个文件时这里必然串味。
    assert results.get(0) == "tag-0", results
    assert results.get(1) == "tag-1", results


def test_sign_cleans_up_its_temp_file(monkeypatch, _stub_profile):
    """临时文件含 Cookie（凭据）⇒ 请求结束**必须**删掉（硬约束②：不落盘）。"""
    from xhs_utils.xhs_core.runtime import _JS_DIR

    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        import os
        from xhs_utils.xhs_core.runtime import _JS_DIR

        abs_path = os.path.join(_JS_DIR, os.path.basename(cmd[2][1:]))
        seen.append(abs_path)
        assert os.path.exists(abs_path), "node 执行时文件应在"
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"xs": "s", "x3": 1}) + "\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    api = XHS_Qianfan_Apis("a1=abc; web_session=x")
    ok, err, _ = api._sign("api/x", {"tag": 1}, "/ref")
    assert ok and err is None
    assert len(seen) == 1
    import os

    assert not os.path.exists(seen[0]), "签名临时文件未清理（凭据落盘）"
    #: 且不应留下固定名 `.sign_input.json`（那是旧实现的名字）。
    assert not os.path.exists(os.path.join(_JS_DIR, ".sign_input.json"))
