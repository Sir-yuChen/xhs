"""部署配置守卫 —— 防止「部署文件看起来对、实际起不来」（v1.1.0 / S9）。

## 为什么这些必须被守卫

部署配置是**只在服务器上才执行**的东西，本机开发永远碰不到。它的错误形态
不是「测试红」，而是「上线后发现起不来 / 数据丢了 / 被打穿」。本项目实测过的
同类事故有三条（都写进过 AGENTS.md）：

* `main.py --with-frontend` 当常驻服务 ⇒ 父进程退出后 uvicorn 变**孤儿**
  （端口仍持着但 502）；
* `secret_key` 用默认值 ⇒ 任何人都能伪造 JWT；
* `storage_dir` 相对路径 ⇒ systemd/Docker 换 CWD 就**静默丢文件**。

这些在本机跑测试时**全都测不出来**，所以判据只能落在配置文件内容上。

## 判据风格

每条都断言**具体配置项存在且取值正确**，不断言「文件存在」。文件存在是
最弱的一类守卫 —— 它连「文件被清空」都抓不到。
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover
    pytest.skip("需要 PyYAML", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
NGINX = ROOT / "deploy/nginx.conf"
SERVICE = ROOT / "deploy/spider-xhs.service"
ENV_EXAMPLE = ROOT / "deploy/.env.example"

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE.is_file(), f"缺少 {COMPOSE}"
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# ① 三个服务与它们的依赖顺序
# --------------------------------------------------------------------------- #
def test_compose_has_app_db_and_nginx(compose):
    services = compose.get("services") or {}
    for name in ("app", "db", "nginx"):
        assert name in services, f"compose 缺服务 {name}"


def test_app_waits_for_a_healthy_database(compose):
    """`app` 必须等 `db` 健康才启动。

    ⚠️ 若只写 `depends_on: [db]`（列表形态 = 只等容器**启动**，不等 PG 接受连接），
    app 会在 PG 还在初始化时开始跑迁移 ⇒ `connection refused` ⇒ 崩 ⇒ 无限重启。
    判据必须是 **dict 形态 + `condition: service_healthy`**。
    """
    depends = (compose["services"]["app"].get("depends_on") or {})
    assert isinstance(depends, dict), (
        "app.depends_on 必须是 dict 形态（列表形态只等容器启动，不等 PG ready）"
    )
    assert depends.get("db", {}).get("condition") == "service_healthy", (
        "app 必须等 db healthy 才启动，否则迁移会在 PG 未就绪时失败"
    )


def test_database_has_a_real_healthcheck(compose):
    """`db` 必须有 `pg_isready` 探针 —— 没有它 `service_healthy` 永远不成立。"""
    hc = compose["services"]["db"].get("healthcheck") or {}
    test = hc.get("test")
    assert test, "db 缺 healthcheck（app 会一直等不到 healthy）"
    joined = " ".join(test) if isinstance(test, list) else str(test)
    assert "pg_isready" in joined, f"db healthcheck 应使用 pg_isready，实际 {test}"


# --------------------------------------------------------------------------- #
# ② 密钥与密码必须「不填就起不来」
# --------------------------------------------------------------------------- #
def test_secret_key_and_password_are_required(compose):
    """`SECRET_KEY` / `POSTGRES_PASSWORD` 必须用 compose 的 `:?` 语法强制必填。

    ⚠️ 用 `${VAR:-默认值}` 会让「忘了设」变成「用默认值静默跑起来」——
    正是「默认密钥能起」这个事故的形态。
    """
    app_env = compose["services"]["app"].get("environment") or {}
    db_env = compose["services"]["db"].get("environment") or {}

    #: compose 的 environment 允许 list 或 dict 形态
    def flat(env) -> dict:
        if isinstance(env, dict):
            return {str(k): str(v) for k, v in env.items()}
        out = {}
        for item in env or []:
            if "=" in str(item):
                k, v = str(item).split("=", 1)
                out[k] = v
        return out

    app_flat = flat(app_env)
    db_flat = flat(db_env)

    assert ":?" in app_flat.get("SECRET_KEY", ""), (
        "SK 提示: SECRET_KEY 必须写成 ${SECRET_KEY:?...}，否则没设也会静默启动"
    )
    assert ":?" in app_flat.get("DATABASE_POSTGRES_PASSWORD", ""), (
        "DATABASE_POSTGRES_PASSWORD 必须强制必填"
    )
    assert ":?" in db_flat.get("POSTGRES_PASSWORD", ""), (
        "db 的 POSTGRES_PASSWORD 必须强制必填"
    )


def test_app_uses_postgres_not_sqlite(compose):
    """服务器态必须用 PG（SQLite 只是历史源 / 离线测试）。"""
    env = compose["services"]["app"].get("environment") or {}
    flat = env if isinstance(env, dict) else {
        str(i).split("=")[0]: str(i).split("=", 1)[1]
        for i in env if "=" in str(i)
    }
    assert flat.get("DATABASE_TYPE") == "postgres", (
        f"服务器部署必须 DATABASE_TYPE=postgres，实际 {flat.get('DATABASE_TYPE')!r}"
    )
    assert flat.get("DATABASE_POSTGRES_HOST") == "db", "PG 主机应指向 compose 的 db 服务"


# --------------------------------------------------------------------------- #
# ③ 数据必须落在宿主机卷上
# --------------------------------------------------------------------------- #
def test_business_data_is_on_a_host_volume(compose):
    """`/app/data` 必须是 bind mount。

    ⚠️ 没有它，`docker compose down` 会连媒体 / 导出 / 上传的安装包一起删掉
    —— 那是不可恢复的数据丢失，而且是**最容易被漏掉**的一条。
    """
    volumes = compose["services"]["app"].get("volumes") or []
    joined = " ".join(str(v) for v in volumes)
    assert "/app/data" in joined, "业务数据目录 /app/data 必须挂到宿主机"
    assert "pg_data" in (compose.get("volumes") or {}), "PG 数据卷未声明"


def test_database_port_is_not_published_to_host(compose):
    """PG 的 5432 **不要**映射到宿主机（暴露到公网是常见事故源）。

    判据：db 服务只能用 `expose`（容器间可见），不能用 `ports`（宿主可见）。
    """
    db = compose["services"]["db"]
    assert not db.get("ports"), (
        f"db 不该用 ports 暴露到宿主机，应用 expose；实际 {db.get('ports')}"
    )
    assert db.get("expose"), "db 需要用 expose 让 app 能连"


# --------------------------------------------------------------------------- #
# ④ nginx：SPA 回退 + 上传体积 + 超时
# --------------------------------------------------------------------------- #
def test_nginx_has_spa_fallback():
    """前端路由（`/system/roles`）在磁盘上没有文件 ⇒ 必须回退 index.html。

    没有它，用户**刷新**任意子路由就 404。

    ⚠️ 判据必须查**回退目标本身**，不能只查 `try_files` 与 `/index.html`
    同时出现 —— 实测变异 `try_files $uri $uri/ =404;`（没有回退）
    在「只查两者出现」的写法下**照样绿**（ESCAPED）：
    那个文件里 `/index.html` 在别的 location 块里也出现过。
    """
    text = NGINX.read_text(encoding="utf-8")
    import re

    #: 找 `location /` 块里的 try_files，断言它的**最后一个参数**是 /index.html
    match = re.search(
        r"location\s*/\s*\{[^}]*?try_files\s+([^;]+);", text, re.DOTALL
    )
    assert match, "nginx 的 `location /` 块里没有 try_files"
    parts = match.group(1).split()
    assert parts[-1].rstrip(";") == "/index.html", (
        f"SPA 回退的目标必须是 /index.html，实际 try_files 末尾是 {parts[-1]!r}"
        "（写成 =404 时子路由刷新会 404）"
    )


def test_nginx_allows_large_uploads():
    """默认 `client_max_body_size` 是 1M ⇒ 数据包上传必然 413。

    这是本项目部署后「上传失败」的最常见原因，必须显式放大。
    """
    text = NGINX.read_text(encoding="utf-8")
    assert "client_max_body_size" in text, "nginx 未设置上传体积上限（默认 1M 会 413）"
    #: 解析出具体值，断言它 ≥ 100m（数据包常见上百张图）
    import re

    match = re.search(r"client_max_body_size\s+(\d+)([mMgGkK]?)\s*;", text)
    assert match, "client_max_body_size 写法无法解析"
    amount, unit = int(match.group(1)), match.group(2).lower()
    megabytes = amount * {"": 1 / 1024 / 1024, "k": 1 / 1024, "m": 1, "g": 1024}[unit]
    assert megabytes >= 100, f"上传上限只有 {megabytes:.0f}M，数据包会 413"


def test_nginx_proxy_timeouts_cover_slow_endpoints():
    """数据包预检（OCR 上百张图）/ 商品同步是**长耗时**请求。

    nginx 默认 `proxy_read_timeout 60s` ⇒ 那些请求会在网关层被切断，
    而用户看到的是「预检没有结果」（不像报错，更像卡住）。
    """
    text = NGINX.read_text(encoding="utf-8")
    import re

    match = re.search(r"proxy_read_timeout\s+(\d+)s", text)
    assert match, "nginx 未设置 proxy_read_timeout"
    assert int(match.group(1)) >= 300, (
        f"proxy_read_timeout 只有 {match.group(1)}s，长耗时接口会被网关切断"
    )


def test_nginx_does_not_cache_index_html():
    """`index.html` 必须 no-store。

    ⚠️ 缓存了旧的 index.html ⇒ 它引用已删除的 hash 文件名 ⇒ 发版后用户**白屏**，
    而且「刷新」也没用（还是拿缓存）。
    """
    text = NGINX.read_text(encoding="utf-8")
    assert "index.html" in text, "nginx 未对 index.html 单独设缓存策略"
    #: 找到 index.html 那个 location 块，断言它有 no-store 或 no-cache
    import re

    blocks = re.findall(r"location\s*=\s*/index\.html\s*\{[^}]*\}", text, re.DOTALL)
    assert blocks, "缺少 `location = /index.html` 块"
    joined = " ".join(blocks)
    assert ("no-store" in joined) or ("no-cache" in joined), (
        "index.html 必须是 no-store / no-cache，否则发版后用户白屏"
    )


# --------------------------------------------------------------------------- #
# ⑤ systemd：不给 root、不在家目录、能自愈但不会无限重启
# --------------------------------------------------------------------------- #
def test_systemd_service_runs_as_non_root():
    """服务账户不能是 root。

    用 root 跑的后遗症：上传的图片 / 导出文件属主是 root，之后管理员
    用普通账户操作时写不进去（表现为「保存失败但没报错」）。
    """
    text = SERVICE.read_text(encoding="utf-8")
    assert "User=" in text, "缺 User=（会以 root 运行）"
    assert "User=root" not in text, "不能用 root 运行"


def test_systemd_service_has_restart_limits():
    """`Restart=always` 必须配 `StartLimitBurst`。

    没有上限时，配置错（如 secret_key 仍是默认值）会让服务**无限重启**，
    日志被刷满，而 `systemctl status` 只显示「正在重启」，看不出「起不来」。
    """
    text = SERVICE.read_text(encoding="utf-8")
    assert "Restart=always" in text, "缺自愈（Restart）"
    assert "StartLimitBurst" in text, "缺重启上限（配置错时会无限重启刷满日志）"


def test_systemd_service_writes_only_to_data():
    """`ProtectSystem=strict` 下必须显式列出**唯一**可写目录。

    项目实测过：`storage_dir` 是相对路径时，systemd 换 CWD 就静默丢文件。
    显式 `ReadWritePaths` 把「能写哪」变成可审计的一条声明。
    """
    text = SERVICE.read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in text, "缺文件系统保护"
    assert "ReadWritePaths=" in text, "strict 模式下必须显式声明可写路径"


def test_systemd_service_does_not_use_main_with_frontend():
    """⚠️ 绝对不能把 `main.py --with-frontend` 当常驻服务。

    那是一次性阻塞、无监督循环的形态：launcher 一退，uvicorn 立刻变
    **孤儿**（父进程消失但端口仍持着），且被杀时只留空壳端口
    （netstat LISTENING + HTTP 502）。本项目实测复现过 4 次。
    """
    text = SERVICE.read_text(encoding="utf-8")
    exec_line = next(
        (line for line in text.splitlines() if line.strip().startswith("ExecStart=")),
        "",
    )
    assert "main.py" not in exec_line, (
        f"ExecStart 不能用 main.py（无监督循环，父进程退出后变孤儿）：{exec_line}"
    )
    assert "uvicorn" in exec_line, f"ExecStart 应直接跑 uvicorn：{exec_line}"


# --------------------------------------------------------------------------- #
# ⑥ .env.example 要把两个必填项写清楚
# --------------------------------------------------------------------------- #
def test_env_example_documents_the_required_keys():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in ("SECRET_KEY", "POSTGRES_PASSWORD"):
        assert key in text, f".env.example 缺 {key}"
    #: 必须给出生成命令 —— 否则用户会随手填一个弱密钥
    assert "token_urlsafe" in text, ".env.example 应给出 SECRET_KEY 的生成命令"
