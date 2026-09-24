---
name: xhs-app-initial-data
description: 把 XHS_ALL_IN_ONE 里的某类「运行期配置 / 基线数据」固化成**随安装包分发**的应用初始化数据（design 15 §4.5 的先例：定时任务配置）。当用户说「把这批配置/数据定义为初始化数据」「随应用的安装自动附有」「装完就该有」「不要每次手动新建」时使用；也用于排查「安装包里少了数据文件」「装完页面是空的」「改了配置但没生效」这类问题。核心口径：种子只有一份真相源且必须随包走；播种与功能开关解耦；打包要有正向断言；已有行一律不覆盖。
agent_created: true
---

# 应用初始化数据（随安装包分发）

## 0. 一句话

**「随安装附有」= 数据文件随包 + 启动时播种 + 已有行不覆盖。** 三件事缺一件需求就不成立。

已落地的样板（照抄它）：`config/initial_data/scheduled_jobs.json` +
`backend/app/core/initial_data.py` + `scheduler_service.ensure_builtin_jobs()` + 设计 15 §4.5。

## 1. 五步法

| # | 做什么 | 落地位置 |
|---|---|---|
| 1 | **数据文件**（UTF-8、可读可编辑、含 `version` + 业务数组） | `config/initial_data/<name>.json` |
| 2 | **加载器**：双路定位 + 白名单校验 + 缺失/坏值 **warning 不静默** | `backend/app/core/initial_data.py` |
| 3 | **播种**：权威 = 数据文件，代码默认兜底；**只补缺失、不覆盖已有**；**挂在应用启动（lifespan）而不是功能开关后面** | `main.py` lifespan + `services/*` |
| 4 | **打包**：组装复制整目录 + spec `datas` 兜底 + **正向断言** | `packaging/build.ps1`、`SpiderXHS.spec` |
| 5 | **测试守卫**：文件结构 / 播种值 / 幂等+不覆盖 / 坏数据降级 / 打包静态断言 / 迁移不播种 | `tests/backend/test_initial_data.py`（样板） |

## 2. 四条硬口径（踩过才知道）

1. **种子只有一份真相源，且必须随包走。** 写死在代码里、写死在迁移里都不算「随安装附有」；
   两处并存必然漂移（R24 的教训：迁移那份还是**已退役**的形态）。
2. **迁移里的播种段要删掉，不能留着。** 判重逻辑（`job_key + is_builtin`）会让已存在的行**永不更新**
   ⇒ 新装库拿不到初始化数据，**改运行期代码也修不掉**。删时保留旧常量 + 标注「被 XX 取代」
   （项目规范：不静默删除）。⚠️ 改历史迁移只影响**尚未执行到该 revision** 的库（新装库），已升级的库零影响。
3. **播种与功能开关解耦。** 挂在「调度器/某模块总开关」后面 ⇒ 开关关着时页面就是空的。
4. **已存在行一律不覆盖。** 用户改过的配置不能被一次升级重置回基线。测试必须显式钉这条。

## 3. 打包侧两件容易漏的事

- **正向断言**：自检只查「不该有的没有」（禁止项）会漏掉「该有的没有」
  —— 必须加 `if (-not (Test-Path <数据文件>)) { Fail ... }`，且命中禁止项要**硬失败**（别只印红字）。
- **测试代码不进包**：`tests`/`pytest`/`_pytest` 都要进禁止项；`smoke-test.ps1` 里也要查一遍安装目录。
- 判据落在**行**上，不落在**文件**上：冒烟要查库「内置行已铺设 N 条」，而不只是「文件存在」。

## 4. 验证命令（改完必跑）

```bash
# ① 初始化数据 + 调度相关用例
"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" -m pytest \
  tests/backend/test_initial_data.py tests/backend/test_scheduled_jobs.py -q --junitxml=.workbuddy/tmp/j.json

# ② 顺序污染自检（caplog 用例必须在这种顺序下也过 —— 见 REF §B）
"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" -m pytest \
  "tests/backend/test_scheduled_jobs.py::test_migration_converts_legacy_auto_tasks_four_ways" \
  tests/backend/test_initial_data.py -q

# ③ 真实仓库解析确认（只读，不写库）
"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" -c \
  "from backend.app.core.initial_data import resolve_initial_data_file; print(resolve_initial_data_file('scheduled_jobs.json'))"
```

## 5. 坑（两条，全量回归才会暴露）

- **`caplog` 在全量里恒空**：先跑的真 alembic 用例调 `env.py` 的
  `fileConfig(disable_existing_loggers=True)`，`backend.app.*` 不在 ini 的 `[loggers]` 名单里 ⇒ 被 disable。
  症状极像「断言写错」：**单独跑全绿、全量跑连红**。修法 = 测试文件加 autouse fixture 复位
  （`logging.disable(NOTSET)` + `logger.disabled = False` + `propagate = True`），**不是**放宽断言。
- **文档数字要同步**：新增/修改用例后，`AGENTS.md` §5 的回归数字、设计文档 §12 数字表、
  验收清单都要回写，并跑 `xhs-doc-stats-verify` 的两个脚本（见该技能）。
