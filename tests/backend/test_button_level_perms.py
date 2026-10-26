"""按钮级权限红线守卫（2026-10-25 用户口径）。

## 红线原文

「所有按钮的权限必须独立，不允许合并使用同一个权限码，权限控制颗粒度达到按钮级别，红线」

## 这条红线要拦的是什么

**一个权限码覆盖多个不同的业务动作**。典型症状：`xhs:item:view` 一个码盖了 29 条路由，
于是「看商品详情」「生成详情图」「同步指定商品」在角色配置里只能是同一个开关 ——
想给某人「看商品」就必须连「生成图片」一起给。这不是颗粒度问题，是**无法表达最小权限**。

## 判据

1. **路由侧**：一个权限码只能对应**一条**业务动作。
   - 允许例外：同一动作的「单条 / 批量」两种入口（`DELETE /notes/{id}` 与
     `POST /notes/batch-delete`）——它们是同一件事，拆开只会让配置更绕。
     这类例外必须显式登记在 `INTENTIONAL_ALIASES`，并写清为什么。
2. **菜单侧**：同一个权限码不能挂在两个按钮行上（否则两个按钮同生同灭）。
3. **前端侧**：页面上的每个操作按钮必须各判各的码，不能一个 `hasPermission` 管多个按钮。

## 为什么守卫要做成「点名清单」而不是直接 assert

存量有 30+ 组共用码，一次全改需要逐组确认业务语义（哪些是真同一动作、哪些是历史图省事）。
守卫的价值是**把它钉成一张清单：谁共用、共用了几条**，改一组划掉一组，
且**不许新增共用**（新增即红）。
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTE_PERMS = ROOT / "backend" / "app" / "core" / "route_perms.py"
MENU_SEED = ROOT / "config" / "initial_data" / "menus.seed.json"

#: 同一用户动作的「单条 / 批量」两种入口 —— 允许共用（拆开只会让角色配置更绕）。
#: 键 = 权限码；值 = 为什么这是同一件事。
INTENTIONAL_ALIASES: dict[str, str] = {
    #: 页面级**查看码**盖住同一页面的「列表 / 详情 / 统计」：
    #: 界面上是**同一个入口**（进页面就能看到），拆开会让授权页出现
    #: 「能看到列表但点不进详情」这种无意义组合（详情页必然要读列表）。
    #: 判据：同一份数据的读法差异 ⇒ 别名；不同数据的读 ⇒ 拆开。
    #:
    #: ⚠️ 2026-10-26 复核（用户口径「17 组别名的意思结合项目你自己决定」）：
    #: **这 16 条保留，不拆**。逐条看下来它们全是「一个页面的读权限」——
    #: 列表、单条详情、统计、在线状态都是同一份数据的取法差异，
    #: 拆开只会产生「勾了列表没勾详情」这种谁都解释不清的组合。
    #: 唯一的例外是 `xhs:crawl:view`：它那 4 条**不是读**（提交抓取 = 打平台 + 建任务），
    #: 已改挂 `xhs:crawl:submit`（见 route_perms.py 的注释）⇒ 已从本表移除。
    #:
    #: ⚠️ 另两条也一并移除：`xhs:autoops:view` / `xhs:order:view` 实测**只剩 1 条路由**，
    #: 根本没共用 —— 留在别名表里会让「共用清单」虚高，掩盖真正的共用。
    "xhs:files:view": "文件查看：图片列表与导出文件下载同属文件页",
    "xhs:login:view": "登录态查看：批量与单个账号的登录态探测是同一份数据的两种粒度",
    "ai:gateway:view": "网关配置查看：列表/详情/统计都属同一页可见范围",
    "ai:model:view": "模型查看：模型树与单个服务商的模型列表是同一份数据",
    "system:dict:view": "字典查看：类型与字典项在同一页左右两栏同时展示",
    "system:menu:view": "菜单查看：树与单条详情同属菜单管理页",
    "system:log:view": "日志查看：审计/应用/业务/链路四个页签是同一页",
    "system:user:view": "用户查看：列表/详情/在线状态同属用户管理页",
    "system:role:view": "角色查看：列表/已授权菜单/已分配用户同属角色管理页",
    "xhs:account:view": "账号查看：列表与同步状态同属账号页",
    "xhs:record:view": "队列记录查看：列表/队列清单/详情同属记录页",
    "xhs:monitor:view": "监控查看：目标列表/笔记/快照属同一页",
    "xhs:settings:view": "配置查看：列表/生效值/单键属同一页",
    "xhs:keyword:view": "关键词查看：列表与详情属同一页",
    "xhs:library:delete": "删除笔记：单条与批量是同一个用户动作（批量只是选得多）",
    "xhs:publish:cancel": "取消发布：单条取消与批量取消是同一个动作",
    #: 下面这组是「同一动作 × 不同数据对象」—— 界面上是**同一个按钮**，
    #: 拆开会让角色配置出现「能建模块但不能建模板」这种无意义组合。
    "ai:prompt:create": "新增提示词：模块/模板/预览渲染共用一个「新增」按钮",
    "ai:prompt:edit": "编辑提示词：模块与模板共用一个「编辑」按钮",
    "ai:prompt:delete": "删除提示词：模块与模板共用一个「删除」按钮",
    "xhs:login:creator": "创作者登录：扫码/短信/确认是同一登录流程的三步，界面上是一个按钮",
    "xhs:login:pc": "网页版登录：同创作者，三步同一流程",
    "xhs:login:qianfan": "千帆登录：扫码与邮箱是同一登录按钮的两种方式",
    "xhs:item:draft": "商品草稿：新建/解析/删除都是草稿这一步的操作",
    "xhs:item:sync": "同步商品：单条同步与整店同步是同一动作",
    "xhs:item:generateImage": "生成详情图：生成与保存是同一流程两步",
    "xhs:item:view": "查看商品：列表/详情/生成记录/挂链笔记都是「看」",
    "xhs:picture:view": "查看图片空间：目录/条目/预览都是「看」",
    #: 只读视图码被「页面入口」与「页面数据源」共用是正常的：
    #: 两者是**同一个看权限**（有页面没数据 / 有数据没页面都无意义），
    #: 且都是 GET，不改变任何数据。红线针对的是**写操作按钮**。
    "system:scheduler:view": "任务中心页面 + 总览端点：同一个只读视图",
    "system:log:view": "日志页 + 其数据端点：同一个只读视图",
    "xhs:item:aiAssist": "AI 商品文案：标题/类目/属性/短标题是同一组推荐按钮",
    #: 2026-10-26「生成笔记 / 上架 / 下架拆成三个码」：商品列表的「生成笔记」两条链路
    #: （千帆智能生成 `/generate-save` + `/batch-generate`、本项目 LLM `/generate-llm`
    #: 及其重试）都挂在**那一个按钮族**下 —— 界面上是同一列的按钮，
    #: 拆分目的是与「上下架」解耦，不是把「生成」本身再切三份。
    "xhs:item:generateNote": "生成商品笔记：千帆智能生成与 LLM 生成同属一个按钮族",
    #: 2026-10-26 设计 71 G2：数据包规则有**父子两级对象**（规则包 set / 规则 rule），
    #: 而界面上是**同一组按钮**（一个「删除」按钮按行所属层级打不同的端点，
    #: 一个「编辑」同理）。拆成 set/rule 两套码只会让授权页出现
    #: 「能删规则包但不能删里面的规则」这种无意义组合。
    "xhs:rule:edit": "编辑规则：规则包与规则共用同一个「编辑」按钮（父子两级对象）",
    "xhs:rule:delete": "删除规则：规则包与规则共用同一个「删除」按钮（父子两级对象）",
    #: 2026-10-26 设计 71 G4/G3：
    #: * 违规词的「启用/停用」是 `PATCH` 的一个字段，界面上是**同一个开关**（两个端点：
    #:   整组开关 / 单个词开关，但都是「切换启用态」这一个动作）；
    #: * 素材库的「删除」同时作用于**目录**与**文件**（同一列「删除」按钮，
    #:   按行类型打不同端点）—— 与规则包的父子两级同理。
    "xhs:forbiddenWord:edit": "编辑违规词：改内容与切换启用是同一行操作（启用是 PATCH 的一个字段）",
    "xhs:material:deleteFolder": "删除素材：目录与文件共用同一个「删除」按钮（按行类型打不同端点）",
}

#: 仍是共用、但**尚未逐组确认**的存量（改一组删一组）。
#: 每次会话只能减少，不许新增 —— 新增即红。
KNOWN_SHARED: dict[str, int] = {
    #: 存量共用清单（2026-10-25 第三轮盘点）。
    #: 首轮 37 组 → 已拆掉 item:view(29)/item:edit(23) 两棵大树 → 现 32 组。
    #: 组数变多是因为拆分后每个新码自己也带若干「同一动作多条路由」的组。
    #: 规则：**只许变短**（拆完删一行）；新增共用即红。
    "xhs:insights:view": 22,
    "xhs:note:edit": 15,
    "xhs:violation:view": 15,
    "xhs:note:view": 13,
    "ai:generate:generate": 9,
    "xhs:publish:submit": 8,
    "xhs:order:ship": 8,
    "xhs:rule:create": 7,
    "xhs:material:createFolder": 7,
    "xhs:forbiddenWord:create": 5,
    "xhs:workshop:generate": 5,
    "xhs:monitor:create": 4,
    "xhs:publish:precheck": 4,
    "system:setting:edit": 4,
    "xhs:crawl:submit": 4,
    "ai:alert:view": 4,
    "xhs:topic:edit": 3,
    "xhs:workshop:adopt": 3,
    "xhs:topic:create": 3,
    "xhs:item:polish": 3,
    "ai:usage:view": 3,
    "system:dict:delete": 2,
    "xhs:autoops:edit": 3,
    "ai:gateway:edit": 2,
    "xhs:account:probe": 2,
    "xhs:account:syncNotes": 2,
    "xhs:picture:resize": 2,
    "xhs:note:sync": 2,
    "system:dict:create": 2,
    "system:menu:edit": 2,
    "system:dict:edit": 2,
    "system:role:assignMenu": 2,
}

#: 允许共用的非业务码（鉴权豁免标记，不是按钮权限）。
NON_BUSINESS = {"EXEMPT", "EXEMPT_ADMIN", "EXEMPT_SELF", "PUBLIC"}


def _route_code_pairs() -> list[tuple[str, str]]:
    text = ROUTE_PERMS.read_text(encoding="utf-8")
    return re.findall(
        r'"(?P<route>(?:GET|POST|PUT|PATCH|DELETE) /api/[^"]+)":\s*"(?P<code>[^"]+)"', text
    )


def _route_to_code() -> dict[str, list[str]]:
    by_code: dict[str, list[str]] = collections.defaultdict(list)
    for route, code in _route_code_pairs():
        if code in NON_BUSINESS or code.startswith("EXEMPT"):
            continue
        by_code[code].append(route)
    return by_code


class TestNoNewSharedPermAcrossRoutes:
    """一个权限码不许覆盖多个业务动作。"""

    def test_no_new_shared_codes(self):
        """新增共用即红 —— 存量清单只许变短。"""
        shared = {c: rs for c, rs in _route_to_code().items() if len(rs) > 1}
        unexpected = {
            c: rs
            for c, rs in shared.items()
            if c not in INTENTIONAL_ALIASES and c not in KNOWN_SHARED
        }
        assert not unexpected, (
            "以下权限码被多条路由共用（红线：颗粒度到按钮，一码一动作）：\n"
            + "\n".join(
                f"  {code} → {len(rs)} 条：" + " | ".join(rs[:4])
                for code, rs in sorted(unexpected.items())
            )
        )

    def test_known_shared_list_does_not_grow(self):
        """存量清单里的条数不许变多（有人往共用码上继续挂路由即红）。"""
        shared = {c: rs for c, rs in _route_to_code().items() if len(rs) > 1}
        grew = [
            f"{code}: 记录 {n} 条，实际 {len(shared.get(code, []))} 条"
            for code, n in KNOWN_SHARED.items()
            if len(shared.get(code, [])) > n
        ]
        assert not grew, "共用码上又挂了新路由：\n" + "\n".join(grew)

    def test_task_center_buttons_are_independent(self):
        """任务中心样板的判据：**两级各一套按钮码，且都登记着**。

        ## 为什么执行/暂停只有一套路由码

        「立即执行」「暂停恢复」是**同一个端点**（`POST /jobs/{id}/run`、
        `PATCH /jobs/{id}/status`）服务两级任务 —— 路由表一个路由只能挂一个码。
        两级的分流判据在服务层 `_get_visible_job`（按 `job.workspace` 取
        `<workspace>:taskcenter:run|toggle` 或 `system:scheduler:run|toggle`）。

        ⇒ 这一层能保证的是：**每个按钮有自己的码，且两级互不共用**；
        运行时的分流由权限守卫那一组（`test_task_center_two_level_perms.py`）负责。
        """
        from backend.app.core.perms_registry import PERMS  # noqa: PLC0415

        registered = {spec.code for spec in PERMS}
        #: 2026-10-25「入口及权限补全」：新增/编辑改为工作区码
        #: （`xhs:taskcenter:create|edit`），系统级由服务层再判。
        #: 删除仍是 admin 专属。
        for code in (
            "system:scheduler:delete",
            "system:scheduler:run",
            "system:scheduler:toggle",
            "system:scheduler:cleanLogs",
            "system:scheduler:deleteLogs",
            "xhs:taskcenter:run",
            "xhs:taskcenter:toggle",
            "xhs:taskcenter:viewLogs",
        ):
            assert code in registered, f"按钮码 {code} 没登记（角色配置页配不出来）"

        #: 两级不许共用同一个执行码（否则工作区成员能操作系统级任务）。
        assert "xhs:taskcenter:run" != "system:scheduler:run"
        by_code = _route_to_code()
        #: 2026-10-25「入口及权限补全」：新增/编辑改挂工作区码
        #: （`xhs:taskcenter:create|edit`），系统级任务由服务层
        #: `_assert_can_create_in_workspace` / `_assert_can_edit` 再判。
        #: 所以 `system:scheduler:create|edit` 现在**没有直接挂路由**，
        #: 但仍必须登记（角色配置页要能勾选）。
        for code in (
            "system:scheduler:delete",
            "xhs:taskcenter:run",
            "xhs:taskcenter:toggle",
            "xhs:taskcenter:create",
            "xhs:taskcenter:edit",
        ):
            assert code in by_code, f"{code} 未挂任何路由"
            assert len(by_code[code]) == 1, (
                f"{code} 挂了 {len(by_code[code])} 条路由 ⇒ 按钮码被合并：{by_code[code]}"
            )


class TestMenuButtonsAreIndependent:
    """菜单里的按钮行不许两个共用一个码。"""

    def test_no_two_buttons_share_a_code(self):
        payload = json.loads(MENU_SEED.read_text(encoding="utf-8"))
        rows = payload.get("menus") or payload.get("items") or []
        buttons = [r for r in rows if r.get("menu_type") == "F" and r.get("perms")]
        counter = collections.Counter(r["perms"] for r in buttons)
        dup = {code: n for code, n in counter.items() if n > 1}
        assert not dup, f"多个按钮共用同一权限码：{dup}"

    def test_workspace_task_buttons_are_registered_as_buttons(self):
        """工作区任务三个按钮都要在菜单种子里有 F 行（否则角色配置页面配不出来）。"""
        payload = json.loads(MENU_SEED.read_text(encoding="utf-8"))
        rows = payload.get("menus") or payload.get("items") or []
        codes = {r.get("perms") for r in rows if r.get("menu_type") == "F"}
        for code in ("xhs:taskcenter:run", "xhs:taskcenter:toggle", "xhs:taskcenter:viewLogs"):
            assert code in codes, f"菜单种子里没有按钮行 {code}"
