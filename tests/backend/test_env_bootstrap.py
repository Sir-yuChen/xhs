"""初始化环境依赖下载（设计 24 §27；用户口径 2026-10-23）。

背景（1.0.5 实机报障）：安装包缺 ffmpeg ⇒ 视频抽帧失败 ⇒ `ocr_ok=None`
⇒ `passedMedia` 按 `ocr_ok is True` 过滤 ⇒ 视频被整个剔除 ⇒ 用户勾了也传不上去，
还提示「数据包中没有视频」。根因是**依赖没随包**。

口径：「用户不安装 py 环境，只下载能下载的三方依赖，减少打包后的体积」
+「新增初始化环境弹窗，展示下载进度，失败支持清空本地重新下载」
+「下载失败自动重试 3 次」+「使用开源公共地址，准备两套」。
"""

from __future__ import annotations

import pathlib

from backend.app.services import env_bootstrap_service as env


def test_bundled_binaries_are_not_downloadable():
    """可下载清单**只含资源类**，不含编译进 exe 的二进制库。

    `cv2` / `onnxruntime` / `numpy` 是 PyInstaller 编译进程序的 `.pyd/.dll`，
    改成下载要把打包形态从单文件 exe 换成「用户装 Python 环境」——用户明确否决。
    """
    keys = {s.key for s in env.DEPENDENCIES}
    assert keys == {"node", "ffmpeg", "ocr_models"}, f"依赖清单被改过：{keys}"
    for banned in ("cv2", "opencv", "onnxruntime", "numpy"):
        assert banned not in keys, f"{banned} 不能出现在下载清单（它是运行库不是资源）"
    #: 说明文字**已按用户口径移除**（2026-10-23）——
    #: 常量保留为空串，前端 `bundled_note` 契约不破（旧前端读不到内容自然不渲染）。
    assert env.BUNDLED_ONLY_NOTE == "", (
        "说明文字被加回来了（用户口径：移除这段文本）"
    )


def test_every_dependency_has_two_public_sources():
    """**每项都要两套公共源**（用户口径：「准备两套」）。

    ⚠️ 实测过的不可用源一个都不许回退：
      · `github.com` / `api.github.com` → 超时 22s、0 字节（被墙）
      · `huggingface.co` / `hf-mirror.com` → 连不上
    """
    assert len(env.FFMPEG_URLS) >= 2, "ffmpeg 只有一套源"
    assert len(env.NODE_URLS) >= 2, "node 只有一套源"
    for name, urls in env.OCR_MODEL_SOURCES:
        assert len(urls) >= 2, f"{name} 只有一套源"

    all_urls = list(env.FFMPEG_URLS) + list(env.NODE_URLS)
    for _n, urls in env.OCR_MODEL_SOURCES:
        all_urls.extend(urls)
    for url in all_urls:
        assert "github.com" not in url, f"用了被墙的 GitHub：{url}"
        assert "huggingface" not in url, f"用了连不上的 HuggingFace：{url}"
        assert "hf-mirror" not in url, f"用了连不上的 hf-mirror：{url}"
        #: 必须是 http(s) 公网地址
        assert url.startswith("https://"), f"非 https：{url}"


def test_download_retries_three_times_before_failing(tmp_path):
    """下载失败**自动重试 3 次**；全部失败才抛，且异常里带每次的原因。"""
    calls: list[int] = []

    def always_fail(_target: pathlib.Path) -> None:
        calls.append(1)
        raise RuntimeError(f"boom-{len(calls)}")

    import pytest

    with pytest.raises(RuntimeError) as ei:
        env._with_retries(always_fail, tmp_path, "测试项")
    assert len(calls) == env.DOWNLOAD_RETRIES == 3, f"重试次数不是 3：{len(calls)}"
    assert "boom-3" in str(ei.value), "异常里没有最后一次的真实原因"
    assert "重试 3 次仍失败" in str(ei.value)


def test_download_retry_succeeds_and_stops_early(tmp_path):
    """第一次成功就不该再试（省时间、别重复下载 83 MB）。"""
    calls: list[int] = []

    def ok_on_second(_target: pathlib.Path) -> None:
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("first fails")

    env._with_retries(ok_on_second, tmp_path, "测试项")
    assert len(calls) == 2, f"应只试 2 次，实试 {len(calls)} 次"


def test_retry_clears_partial_files_between_attempts(tmp_path):
    """每次重试前清掉 `*.part` —— 否则第二次会撞上第一次留下的半截文件。"""
    seen_parts: list[bool] = []

    def check_then_fail(target: pathlib.Path) -> None:
        #: 记录「进函数时是否还残留 .part」（第一次调用时不存在）
        seen_parts.append(any(target.glob("*.part")))
        (target / "x.part").write_bytes(b"half")
        raise RuntimeError("fail")

    import pytest

    with pytest.raises(RuntimeError):
        env._with_retries(check_then_fail, tmp_path, "测试项")
    #: 第 2、3 次进函数时，上一次的 .part 必须已被清掉
    assert seen_parts[1:] == [False, False], f"重试没清半截文件：{seen_parts}"


def test_multisource_falls_back_to_second_url(monkeypatch):
    """主源失败 ⇒ **自动切备源**，不让用户手动换。"""
    tried: list[str] = []

    def fake_open(url: str, timeout: int = 0):
        tried.append(url)

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                if len(tried) == 1:
                    raise OSError("primary down")
                return b"x" * 4096

        return _Resp()

    monkeypatch.setattr(env.urllib.request, "urlopen", fake_open)
    data = env._download_first_ok(("https://a.test/f", "https://b.test/f"), "测试")
    assert data == b"x" * 4096
    assert tried == ["https://a.test/f", "https://b.test/f"], "没切到备源"


def test_multisource_reports_every_source_error():
    """全部源失败 ⇒ 异常里**列出每个源的真实错误**（用户要能判断是不是网络问题）。"""
    import pytest

    def fake_open(url: str, timeout: int = 0):
        raise OSError(f"cannot reach {url}")

    env.urllib.request.urlopen = fake_open  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            env._download_first_ok(("https://a.test/f", "https://b.test/f"), "测试")
        msg = str(ei.value)
        assert "a.test" in msg and "b.test" in msg, f"没列出所有源：{msg}"
        assert "所有下载源均失败" in msg
    finally:
        import importlib

        importlib.reload(env.urllib.request) if False else None


def test_reset_really_deletes_local_files(tmp_path, monkeypatch):
    """「清空本地重新下载」必须**真的删目录**（不是只把状态改成 missing）。"""
    monkeypatch.setattr(env, "deps_root", lambda: tmp_path)
    target = tmp_path / "ffmpeg"
    target.mkdir()
    (target / "ffmpeg.exe").write_bytes(b"broken")
    assert target.exists()

    result = env.reset_dependency("ffmpeg")
    assert result["removed"] is True
    assert not target.exists(), "reset 没有真的删掉本地文件"


def test_reset_unknown_key_raises():
    import pytest

    with pytest.raises(KeyError):
        env.reset_dependency("not-a-dep")


def test_probe_rejects_half_downloaded_files(tmp_path):
    """**半截文件必须判不可用** —— 否则用户永远看不到「清空重下」按钮。

    ⚠️ 判据要**分别钉住两条**（2026-10-23 变异 M6 ESCAPED 暴露）：
      · ① 文件不存在 / 太小；
      · ② **能执行但体积不对**。
    只测 ① 抓不住「删掉体积检查」的变异 —— 假文件跑不起来，
    去掉体积判据后 `subprocess` 照样抛异常 ⇒ 仍然返回 False ⇒ 假绿。
    ⇒ ② 用**构建机真实的 ffmpeg** 复制一份「合法但被截断」的文件来测。
    """
    #: ① 假文件（跑不起来）
    (tmp_path / "ffmpeg.exe").write_bytes(b"not really ffmpeg")
    assert env.probe_ffmpeg(tmp_path) is False
    (tmp_path / "node.exe").write_bytes(b"x" * 100)
    assert env.probe_node(tmp_path) is False
    (tmp_path / "PP-OCRv6_det_tiny.onnx").write_bytes(b"x" * 2_000_000)
    assert env.probe_ocr_models(tmp_path) is False

    #: ② **体积判据必须独立生效**：源码里要有体积门槛（不是只判 is_file）
    import pathlib as _p

    src = _p.Path("backend/app/services/env_bootstrap_service.py").read_text(encoding="utf-8")
    i = src.index("def probe_ffmpeg(")
    body = src[i : i + 900]
    assert "exe.stat().st_size < 1_000_000" in body, (
        "probe_ffmpeg 没有体积门槛 —— 半截 exe（下了一半）会被当成可用"
    )
    j = src.index("def probe_node(")
    body2 = src[j : j + 900]
    assert "exe.stat().st_size < 10_000_000" in body2, "probe_node 没有体积门槛"

    #: ③ OCR 模型的阈值必须与**实际体积**同量级（估大了会造成「下对了却不可用」）
    k = src.index("def probe_ocr_models(")
    body3 = src[k : k + 1200]
    assert "1_600_000" in body3 and "4_000_000" in body3, (
        "OCR 阈值不是实测口径（tiny 档 det 1.83MB / rec 4.49MB），估大了会恒判不可用"
    )


def test_node_is_the_only_required_dependency():
    """node 是**必装**（签名全靠它，缺了软件不可用）；ffmpeg/OCR 缺失只退化功能。"""
    required = {s.key for s in env.DEPENDENCIES if s.required}
    assert required == {"node"}, f"必装项不对：{required}"


def test_dialog_defaults_to_open_because_it_is_conditionally_mounted():
    """依赖检查弹窗**挂载即打开**（用户报障 2026-10-23）。

    原文：「点击检查依赖，没有弹窗展示检查情况」。
    真因：`EnvBootstrapDialog` 被**条件挂载**（`{depCheckOpen ? <Dialog/> : null}`），
    挂载即代表「要显示」；但初版写的是 `useState(false)` 且没有任何地方置 true
    ⇒ 点按钮只让组件挂载、弹窗永远不出现（表现为「点了没反应」）。

    ⚠️ 这类 bug **类型检查和纯单测都抓不住**（组件确实渲染了，只是 Modal 没开），
    所以判据必须钉在源码上。
    """
    import pathlib as _p

    src = _p.Path(
        "frontend/src/components/system/env-bootstrap-dialog.tsx"
    ).read_text(encoding="utf-8")

    #: ① 主弹窗的 open 初值必须是 true
    i = src.index("export function EnvBootstrapDialog(")
    body = src[i : i + 3000]
    assert "const [open, setOpen] = useState(true);" in body, (
        "EnvBootstrapDialog 的 open 初值是 false —— 条件挂载后弹窗不会出现"
    )
    assert "const [open, setOpen] = useState(false);" not in body, (
        "open 初值退回 false（点按钮将没有任何反应）"
    )

    #: ② 关闭时必须通知父级卸载，否则下次点按钮不会重新探测
    assert "onDone?.();\n          setOpen(false);" in src, (
        "onOk 完成时没通知父级卸载 —— 组件留在 DOM 里，再次点击不会重新探测"
    )
    assert "onCancel={() => {\n        onDone?.();\n        setOpen(false);\n      }}" in src, (
        "onCancel 没通知父级卸载"
    )

    #: ③ 启动入口（Gate）仍然**探测后才打开**（不能一进软件就无条件弹）
    j = src.index("export function EnvBootstrapGate()")
    gate = src[j:]
    assert "const [open, setOpen] = useState(false);" in gate, (
        "Gate 不该默认打开 —— 它要在探测到缺失后才弹"
    )
    assert "if ((data.deps.missing ?? []).length > 0) setOpen(true);" in gate


def test_refresh_button_uses_shared_wait_mask_and_reports_result():
    """「刷新状态」必须用**公共等待组件**并给出结果反馈（用户报障 2026-10-23）。

    原文：「点击刷新状态按钮无反应，加公共等待组件」。

    真因两条：
      ① 探测几十毫秒返回、界面毫无变化 ⇒ 看着像没响应；
      ② 失败时静默 `catch(() => undefined)` ⇒ 网络断了也没有任何提示。

    修法：`WaitMask`（本仓统一等待组件，用户口径 2026-10-20「全项目蒙版样式一样」）
    + 结果 message（就绪 / 逐项报缺失 / 失败原因）。
    """
    import pathlib as _p

    src = _p.Path(
        "frontend/src/components/system/env-bootstrap-dialog.tsx"
    ).read_text(encoding="utf-8")

    #: ① 用公共组件，不许自己写 Spin 蒙版
    assert 'import { WaitMask } from "../ui/wait-mask";' in src, "没用公共等待组件 WaitMask"
    assert "<WaitMask open={refreshing}" in src, "刷新时没显示等待蒙版"
    assert "正在检查本地依赖" in src, "蒙版没说清在做什么"

    #: ② 刷新有独立状态（不能与下载 busy 混用）
    assert "const [refreshing, setRefreshing] = useState(false);" in src
    assert "loading={refreshing}" in src, "按钮没有 loading 态"

    #: ③ 结果必须有反馈（就绪 / 缺失 / 失败三类）
    assert "依赖检查完成：全部就绪" in src, "成功后无提示"
    assert "项缺失（" in src, "缺失时没逐项报出来"
    assert "检查失败：" in src, "失败被静默吞掉"

    #: ④ 刷新走独立函数（不是裸 refresh）
    assert "const manualRefresh = useCallback(" in src
    assert "onClick={() => void manualRefresh()}" in src, "按钮仍直接调 refresh（无反馈）"
