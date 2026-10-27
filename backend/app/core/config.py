from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

try:
    from pydantic_settings import (
        BaseSettings,
        PydanticBaseSettingsSource,
        SettingsConfigDict,
    )
except ImportError:  # pragma: no cover - handled by dependency installation
    BaseSettings = object
    PydanticBaseSettingsSource = None  # type: ignore[assignment,misc]
    SettingsConfigDict = dict

_HAS_PYDANTIC_SETTINGS = PydanticBaseSettingsSource is not None

#: `secret_key` 的**出厂默认值** —— 唯一常量（启动自检与字段默认值同源，
#: 两处各写一遍字符串，将来改了一处就有一个守卫失效）。见 `assert_production_ready`。
DEFAULT_SECRET_KEY = "dev-only-change-me"


def _load_yaml_config() -> Dict[str, Any]:
    """Load YAML configuration files and flatten into env-var-style keys.

    Merge order inside YAML (later values override earlier ones):
      1. config/default.yaml (always loaded if it exists)
      2. File specified by CONFIG_FILE environment variable

    The result is consumed by :class:`_YamlSettingsSource` as the **lowest
    priority** settings source, so environment variables always win over YAML.
    """
    try:
        import yaml
    except ImportError:
        return {}

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    flat: Dict[str, Any] = {}

    # Mapping from nested YAML paths to Settings field names
    yaml_key_map = {
        "server.host": "server_host",
        "server.port": "server_port",
        "server.cors_origins": "backend_cors_origins",
        #: PostgreSQL —— **唯一**支持的数据库（v1.1.0 / 2026-10-24）。
        #: 原 `database.type` / `database.sqlite_path` / `database.mysql_*` 六条映射
        #: 已随对应 Settings 字段一起删除 —— 字段没了，映射留着就是死键。
        #: PostgreSQL 主库（2026-10-23，设计 51/58）
        "database.postgres_host": "database_postgres_host",
        "database.postgres_port": "database_postgres_port",
        "database.postgres_user": "database_postgres_user",
        "database.postgres_password": "database_postgres_password",
        "database.postgres_database": "database_postgres_database",
        "security.secret_key": "secret_key",
        "security.fernet_key": "fernet_key",
        #: 开放注册基线（v1.1.0 / 设计 46 §388）。第三个真值点：
        #: `config/default.yaml` = 随包种子 `system_settings.json` =
        #: `settings_service.BUILTIN_DEFAULTS`，三处同值（守卫
        #: `test_settings_baseline_is_one_number_everywhere`）。
        #: **yaml 平铺后的键名带点**（`feature.register.enabled`）—— 这不是 bug：
        #: `_flatten` 只按已登记的路径展开，登记的键就是「用点连起来的原文」。
        "feature.register.enabled": "register_enabled",
        "scheduler.enabled": "scheduler_enabled",
        "scheduler.interval_seconds": "scheduler_interval_seconds",
        "qianfan.health_check_enabled": "qianfan_health_check_enabled",
        "qianfan.health_check_interval_hours": "qianfan_health_check_interval_hours",
        # M1.9 账号矩阵同步（T-806）：两个定时同步开关 **默认关**（需求 N2：
        # 「慢慢同步」，不默认替用户打平台），周期默认 6 小时。
        "xhs.note_sync.enabled": "xhs_note_sync_enabled",
        "xhs.note_sync.interval_hours": "xhs_note_sync_interval_hours",
        # §44（2026-09-30）：商品笔记同步（千帆挂链笔记）—— 与普通笔记各自开关。
        "xhs.goods_note_sync.enabled": "xhs_goods_note_sync_enabled",
        "xhs.goods_note_sync.interval_hours": "xhs_goods_note_sync_interval_hours",
        # 调度轮内账号之间的停顿（秒）：多账号矩阵不一窝蜂。
        "xhs.sync.account_gap_seconds": "xhs_sync_account_gap_seconds",
        # 同步并行账号数（设计 13 §38.3，2026-09-27）：同一账号始终串行（账号级互斥），
        # 并行只发生在**账号之间**。运行期可在设置页改（system_settings 行覆盖本值）。
        "xhs.sync.account_parallelism": "xhs_sync_account_parallelism",
        "xhs.sync.image_upload_parallelism": "xhs_image_upload_parallelism",
        
        # 平台请求限流（次/分钟，按 `<账号>:<源>` 键计量；等待式，不即停）。
        # DEF-069 提速（2026-09-25）：原实现没有这一项，额度实际落到
        # `scheduler_interval_seconds`（60）兜底 —— 语义混乱且改不动。
        "xhs.sync.rate_limit_per_minute": "crawl_rate_limit_per_minute",
        # 商品笔记重复发布开关（设计 13 §31.5.2，2026-09-28）：默认 false = 同一商品
        # 已有笔记（已发布 / 正在发布）时不再发布。运行期可在设置页改（行覆盖本值）。
        "xhs.publish.allow_duplicate_goods": "xhs_allow_duplicate_goods_publish",
        "frontend.serve_static": "frontend_serve_static",
        "frontend.build_dir": "frontend_build_dir",
    }

    def _flatten(data: Any, prefix: str = "") -> None:
        if isinstance(data, dict):
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if full_key in yaml_key_map:
                    flat[yaml_key_map[full_key]] = value
                _flatten(value, full_key)

    # 1. Load config/default.yaml
    default_yaml = project_root / "config" / "default.yaml"
    if default_yaml.exists():
        with open(default_yaml, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if data:
                _flatten(data)

    # 2. Load CONFIG_FILE override
    config_file = os.environ.get("CONFIG_FILE")
    if config_file:
        config_path = Path(config_file)
        if not config_path.is_absolute():
            config_path = project_root / config_path
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if data:
                    _flatten(data)

    return flat


if _HAS_PYDANTIC_SETTINGS:

    class _YamlSettingsSource(PydanticBaseSettingsSource):  # type: ignore[misc]
        """Settings source backed by ``config/default.yaml`` (+ ``CONFIG_FILE``).

        Registered as the **lowest-priority** source, so YAML values behave as
        defaults that environment variables can override. This is what makes
        ``SECRET_KEY`` / ``FERNET_KEY`` / ``FRONTEND_SERVE_STATIC`` /
        ``SCHEDULER_ENABLED`` work as documented.
        """

        def get_field_value(self, field: Any, field_name: str):  # pragma: no cover
            # Whole-mapping source: per-field lookup is never used because
            # __call__ returns the complete flattened dict at once.
            return None, field_name, False

        def __call__(self) -> Dict[str, Any]:
            return _load_yaml_config()


class Settings(BaseSettings):
    app_name: str = "Spider_XHS"
    api_title: str = "Spider_XHS Operations Platform"
    environment: str = "development"

    # Database —— **只有 PostgreSQL**（v1.1.0 / 2026-10-24 用户口径「完全放弃 SQLite」）
    #:
    #: ⚠️ 删掉的三类字段（不要加回来）：
    #:   * `database_type`（原 `"sqlite"` 默认值）—— 只有一个选项的开关等于没有开关，
    #:     留着会让人以为能切回去；
    #:   * `database_sqlite_path` —— SQLite 已彻底退役；
    #:   * `database_mysql_*` —— 从未真正支持过（只在 `model_post_init` 里拼过串）。
    database_url: str = ""
    #: PostgreSQL 分量（设计 51/58）。本机实例见 `D:\work\PostgreSQL`。
    database_postgres_host: str = "127.0.0.1"
    database_postgres_port: int = 5432
    database_postgres_user: str = "postgres"
    database_postgres_password: str = "123456"
    database_postgres_database: str = "spider_xhs"

    # Security
    secret_key: str = DEFAULT_SECRET_KEY
    fernet_key: str = ""

    #: 开放注册（v1.1.0 / 设计 46 §388 + 设计 50 §261）。**默认 false**。
    #: 三处同值：`config/default.yaml` / 随包种子 `system_settings.json` /
    #: `settings_service.BUILTIN_DEFAULTS`。运行期真值走设置页（`register.enabled`），
    #: 这里的值只是「没有配置行时的最后兜底」。
    register_enabled: bool = False

    #: ── SMTP（v1.1.0 / 邮箱验证码注册与登录，设计 55 §1.2）──────────────
    #: 口径对齐 dataPanel（用户「注册改为邮箱验证与 dataPanel 一致」）。
    #:
    #: ⚠️ **未配置时验证码降级为只写日志**（`email_service.send_code_email` 返回
    #: False 而不抛异常），这样本地开发 / 首装能跑通注册流程。生产必须配。
    #:
    #: 配法（二选一）：`config/default.yaml` 的 `email:` 段，或环境变量
    #: `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USERNAME` / `EMAIL_PASSWORD`。
    email_host: str = ""
    email_port: int = 0
    email_username: str = ""
    email_password: str = ""
    #: 发件人地址（缺省用 `email_username`）
    email_from_addr: str = ""
    #: `true` = 直连 SSL（465）；`false` = 明文 25 / STARTTLS 587
    email_use_ssl: bool = False

    # Server
    server_host: str = "0.0.0.0"
    server_port: int = 8000

    # CORS
    backend_cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    # Scheduler
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 60

    # 平台请求限流（次/分钟，按 `<账号>:<源>` 键计量；`wait_allow` 等待式，不即停）。
    # 默认 **8**（2026-09-27 由 60 下调，用户口径，见设计 13 §38.7）：实测 60 次/分钟
    # 会让平台踢掉网页会话 ⇒ 保守区间 5~10；嫌慢可在设置页调大（改完立即生效），
    # 但平台风控是真实的 —— 详情请求密度过高会触发反爬。
    # ⚠️ 本字段只是**第 3 层兜底**（构造参数 > 运行时配置 > 本字段 > 内置 5）：
    #    随包种子已按用户铺设 `system_settings` 行，正常安装路径下读的是那一行。
    crawl_rate_limit_per_minute: int = 8
    #: 日志保留期（设计 64 §2.4）：record_cleanup 清理全局日志表的基线（天数）。
    log_retention_days: int = 90
    job_log_retention_days: int = 30

    # 千帆（ark）店铺绑定的登录态巡检（T-31）—— 只读探活，见 services/shop_health_service.py
    # 关闭开关 = 不注册定时任务（手动「立即巡检」按钮仍可用）。
    qianfan_health_check_enabled: bool = True
    # 周期（小时）：默认 6。千帆登录态按天/周级过期，无需更密。
    qianfan_health_check_interval_hours: int = 6

    # 无流量笔记自动清理（写操作，风控口径见 services/no_traffic_cleanup_service.py）。
    # 开关关闭 = 不注册定时任务（手动「开始清理」仍可用，且仍受批次上限 + 限流约束）。
    no_traffic_cleanup_enabled: bool = True
    # 周期（小时）：默认 6。216 篇候选 / 每批 20 篇，4 小时内绰绰有余。
    no_traffic_cleanup_interval_hours: int = 6

    # M1.9 账号矩阵同步的定时任务（T-806）。
    # **两个开关都默认 False**：同步是「慢慢来」的后台活，什么时候开始由用户决定；
    # 手动按钮不受开关影响（开关只管定时任务注册）。
    xhs_note_sync_enabled: bool = False
    # 周期（小时）：默认 6。有界翻页下每轮每账号只走几页，靠多轮摊完大账号。
    xhs_note_sync_interval_hours: int = 6
    # §44（2026-09-30）：**商品笔记**同步独立成任务（千帆 goods-note/list 挂链笔记），
    # 与上面的普通笔记同步各自开关 / 周期。同款语义：默认 False = 只在手动按钮触发。
    xhs_goods_note_sync_enabled: bool = False
    xhs_goods_note_sync_interval_hours: int = 6
    # 调度轮内**账号之间**的停顿（秒）：多账号矩阵不能一窝蜂同时打平台。
    xhs_sync_account_gap_seconds: int = 10
    # 同步**并行账号数**（设计 13 §38.3，2026-09-27 用户口径「账号之间的任务应支持并行」）。
    # 同一账号始终串行（账号级互斥 —— 同账号并发只会互抢同一个限流桶，既不提速又让
    # 「每账号每源 N 次/分钟」的语义失真）；并行只发生在账号之间。
    # ⚠️ 运行期可在设置页改（`system_settings` 行优先于本值，见 services/settings_service.py）。
    # ⚠️ 与限流额度是**乘性**关系：N 个账号 × 单账号速率 = 出口 IP 的总请求速率（设计 13 §38.6）。
    xhs_sync_account_parallelism: int = 3

    # OCR 并发路数（设计 24 §22.9，2026-10-13 用户口径「当前的并发值配置优化为，
    # 初始并发数配置，并配置在系统参数中，默认 5」）。
    # ⚠️ 语义是**初始值**：`resolve_parallelism()` 以它为起点，再按本机核数夹一次、
    # 按目标耗时上推（夹在 1–32）。它是「这台机器大概能开几路」的声明，
    # 不是硬上限 —— 单图推理是纯 CPU 密集，实测 4 路即饱和。
    # ⚠️ 运行期可在设置页改（`system_settings` 行优先于本值，见 services/settings_service.py）。
    xhs_ocr_parallelism: int = 5

    # SKU 规格名称字数上限（用户口径：30 字 = 60 字符，汉字计 2、ASCII 计 1）。
    # ⚠️ 第 3 层兜底：正常安装路径读随包种子铺设的 `system_settings` 行。
    xhs_sku_name_max_chars: int = 60

    # 商品笔记重复发布开关（设计 13 §31.5.2，2026-09-28 用户口径）。
    # 默认 **False** = 同一商品已有笔记（已发布 / 正在发布）时，再点发布 / 批量发布
    # **不再发布**，该笔记落 `publish_status=failed` + 原因「挂载的商品已经发布过笔记」。
    # ⚠️ 本字段只是**第 3 层兜底**（构造参数 > 运行时配置 > 本字段 > 内置默认）：
    #    随包种子已按用户铺设 `system_settings` 行，正常安装路径下读的是那一行
    #    （设置页可改、改完立即生效）。
    xhs_allow_duplicate_goods_publish: bool = False

    # 媒体上传并发数（设计 24 §22，2026-10-11 用户口径：「方案 B，并发数默认 5，
    # 支持在 config 表配置」）。
    # ⚠️ 为什么敢并发：上传是**两段式** —— ① `upload/web/permit` 打小红书（**整批只 1 次**）；
    #    ② PUT 字节打 ROS/COS **对象存储**（`https://<uploadAddr>/<fileId>`，不是小红书域名）。
    #    单张 ~2.5s 的耗时基本全在 ② ⇒ 并发第二节**不产生平台请求**。
    #    平台自身证据：笔记发布链路 `note_publish_service._upload_media` 早已
    #    `upload_web_permit(len(downloaded))` 一次批量申请 N 个上传位。
    # ⚠️ 运行期可在设置页改（`system_settings` 行优先于本值）。
    xhs_image_upload_parallelism: int = 8

    #: 笔记**图片总数**（一篇笔记最多几张图）—— 生成链路的组合总闸（设计 41 §17）。
    #: ⚠️ **平台硬顶 18**（官方 `xhs.postNote` 原文「图片可以多张（1-18）」），
    #:    本值是我们**自己收窄**的产品默认（改它不动平台契约）。
    #: 用户口径 2026-10-16：「笔记图的总数是固定的…配置到 config 表中支持动态调整」。
    xhs_note_image_total_limit: int = 6

    # Asset storage
    asset_storage_type: str = "local"

    # Frontend static serving
    frontend_serve_static: bool = False
    #: 前端构建产物目录。**空串 = 由 `core/paths.frontend_dist()` 兜底**（设计 53 的 A2）：
    #: 原来默认 `"./frontend/dist"` 是相对 CWD 的，systemd / Docker / 换目录起 uvicorn
    #: 任一处 CWD 不同 ⇒ 整个前端 404。留空让路径按**文件位置**推出仓库根。
    #: 部署时若把 dist 放到别处（如 `/srv/spider-xhs/web`），在这里写绝对路径即可。
    frontend_build_dir: str = ""

    if hasattr(BaseSettings, "model_config"):
        model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    if _HAS_PYDANTIC_SETTINGS:

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: Any,
            init_settings: Any,
            env_settings: Any,
            dotenv_settings: Any,
            file_secret_settings: Any,
        ) -> tuple:
            """Declare source precedence: **init > env vars > .env > YAML > secrets**.

            Fixes DEF-004 / T-110. Previously ``get_settings()`` passed the YAML
            mapping as *init kwargs*, which pydantic-settings treats as the
            highest-priority source — so ``config/default.yaml`` silently
            overrode environment variables, contradicting both the file header
            and this module's own documentation.
            """
            return (
                init_settings,
                env_settings,
                dotenv_settings,
                _YamlSettingsSource(settings_cls),
                file_secret_settings,
            )

    def model_post_init(self, __context: Any) -> None:
        """没显式给 `database_url` ⇒ 用分量字段拼出 **PostgreSQL** 连接串。

        ⚠️ v1.1.0（2026-10-24，用户口径「完全放弃 SQLite，全部迁移到 PG」）：
        **删掉了 MySQL 与 SQLite 两个分支**，也删掉了 SQLite 时代的
        「旧默认路径存在就用旧路径」兼容补丁（那段逻辑是为了让老桌面安装
        升级后自动接上老库 —— 桌面端退役后没有「老安装」这回事了）。

        现在只剩一种结果：`postgresql+psycopg://...`（驱动 psycopg3，
        有 Windows 预编译 wheel，免本地编译）。
        """
        if self.database_url:
            return
        object.__setattr__(
            self,
            "database_url",
            f"postgresql+psycopg://{self.database_postgres_user}:{self.database_postgres_password}"
            f"@{self.database_postgres_host}:{self.database_postgres_port}/{self.database_postgres_database}",
        )

    @property
    def storage_dir(self) -> Path:
        """媒体 / 导出 / 暂存的根目录 —— **绝对路径**（修设计 53 的 A2）。

        委托 :func:`backend.app.core.paths.storage_dir`（按文件位置推仓库根），
        不再写死相对 CWD 的 `"backend/app/storage"`。11 个消费方一行都不用改
        （S4 再把它们逐个改成直接调 `paths.storage_dir()`，那时本属性退为别名）。
        """
        from backend.app.core.paths import storage_dir as _storage_dir

        return _storage_dir()

    @property
    def frontend_build_dir_resolved(self) -> Path:
        """`frontend_build_dir` 的绝对形态：空 ⇒ `paths.frontend_dist()`。

        绝对路径原样尊重（部署时指向 `/srv/spider-xhs/web` 是正常用法）；
        相对路径按仓库根解析（**不按 CWD**）—— 这是 A2 的判据。
        """
        from backend.app.core.paths import frontend_dist, resolve_from_root

        if not self.frontend_build_dir.strip():
            return frontend_dist()
        return resolve_from_root(self.frontend_build_dir)

    def assert_production_ready(self) -> None:
        """启动安全自检（设计 53 的 C1）—— **不满足就拒绝启动**。

        判据：`secret_key` 仍是随仓库发布的默认值 `dev-only-change-me` ⇒
        **任何人都能伪造任意用户的 JWT，包括 admin**。

        ⚠️ v1.1.0（2026-10-24）：**去掉了「SQLite 就放过」的例外**。
        原设计说「桌面端跑在用户自己机器上、只监听本机，拦了会让已有安装升级后
        起不来」—— 那个前提随「桌面端退役 + SQLite 完全退役」一起消失了：
        现在只有服务器形态，**一律要求真密钥**。原来是 if 分支，现在无条件执行。

        抛 `RuntimeError` 而不是 warning：warning 在 systemd / Docker 的日志里
        没人看，而伪造 JWT 是不可事后发现的那种事故。
        """
        if self.secret_key.strip() == DEFAULT_SECRET_KEY:
            raise RuntimeError(
                "拒绝启动：secret_key 必须改掉（当前仍是随仓库发布的默认值），"
                f"当前仍是默认值 {DEFAULT_SECRET_KEY!r} —— 任何人都能用它伪造任意用户的令牌。"
                "请设置环境变量 SECRET_KEY（或 config/default.yaml 的 security.secret_key）。"
            )


@lru_cache
def get_settings() -> Settings:
    # The YAML mapping is injected by Settings.settings_customise_sources as the
    # lowest-priority source, so it must NOT be passed as init kwargs here —
    # init kwargs outrank environment variables (defect DEF-004 / task T-110).
    return Settings()
