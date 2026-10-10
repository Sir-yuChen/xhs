import main


def test_parse_args_defaults_to_backend_only():
    args = main.parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.with_frontend is False


def test_parse_args_accepts_frontend_flag():
    args = main.parse_args(["--with-frontend", "--port", "8010"])
    assert args.with_frontend is True
    assert args.port == 8010


def test_build_frontend_command_uses_resolved_npm_executable():
    command = main.build_frontend_command(5174, npm_executable="C:/node/npm.cmd")
    assert command == ["C:/node/npm.cmd", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5174"]


def test_static_assets_get_cache_headers(tmp_path, monkeypatch):
    """静态产物的**缓存头**（2026-10-10 实测补的响应侧契约）。

    根因：Starlette `StaticFiles` **不发 `Cache-Control`** ⇒ 浏览器走启发式缓存，
    `index.html` 被缓存后不再回源校验，于是长期指向**旧的** `index-<hash>.js`
    —— 这就是用户多轮报「依旧」的真根因（服务端已是新包，页面却加载旧 hash）。

    口径：带 hash 的 `assets/*` 可长缓存（`immutable`）；**SPA 入口必须回源**（`no-cache`）。
    """
    import importlib

    (tmp_path / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")

    monkeypatch.setenv("FRONTEND_SERVE_STATIC", "true")
    monkeypatch.setenv("FRONTEND_BUILD_DIR", str(tmp_path))
    import backend.app.core.config as cfg

    importlib.reload(cfg)
    monkeypatch.setattr(cfg, "get_settings", lambda: cfg.Settings(frontend_serve_static=True, frontend_build_dir=str(tmp_path)))

    import backend.app.main as m

    importlib.reload(m)
    try:
        app = m.create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app)
        #: 入口 / 客户端路由 —— 必须回源
        root = client.get("/")
        assert root.status_code == 200
        assert "no-cache" in (root.headers.get("cache-control") or ""), (
            "index.html 必须 no-cache（否则浏览器缓存入口 ⇒ 长期指向旧 JS hash）"
        )
        #: 带 hash 的产物 —— 可长缓存
        asset = client.get("/assets/index-abc123.js")
        assert asset.status_code == 200
        cc = asset.headers.get("cache-control") or ""
        assert "immutable" in cc and "max-age=" in cc, "带 hash 的 assets 应长缓存"
    finally:
        #: ⚠️ `importlib.reload` 是**全局副作用**：本 worker 里后续用例会拿到被改过的
        #: settings ⇒ 必须还原（env 由 monkeypatch 撤销，模块要手动 reload 回默认态）。
        importlib.reload(cfg)
        importlib.reload(m)
