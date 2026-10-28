"""**数据包列规则**（设计 40，2026-10-14）。

## 为什么要有这张表

数据包里「某一列该怎么取值」**每个包不一样**，而且是**每类包固定一份**，不是每包现算：

| 数据包 | SKU 列 | 形态 | 要哪一段 |
|---|---|---|---|
| 店透SKU预览 | `SKU信息` = `木;中古色-实木三斗柜;上门安装` | 分号三段 | 第 2 段 |
| 商品SKU数据 | `sku名称` = `颜色分类:【中古风】实木三斗柜` | 冒号两段 | 第 1 段 |

⚠️ **实测证据**（设计 40 §1.2）：拿包1的规则跑包2 ⇒ **0/26**；反向 ⇒ **0/52**。
⇒ 规则**跨包必然失效**，所以「临场反推」是错的路线，「配置化 + 按包归属」才对。
包内则相反：给 1~2 行样例即可覆盖整列（52/52、26/26）。

## 两层结构

```
data_package_rule_sets（规则包 / 父）   ← 一类数据包一条，如「店透SKU预览」
  └── data_package_rules（规则 / 子）   ← 包下的一条**具名规则**（取值写法）
```

⚠️ **规则不绑列**（用户口径 2026-10-14）：一条规则是「**怎么从一段文本里取出一段**」
的写法，本身与列无关 —— **用在哪一列由使用时选**。
⇒ 同一条「取第 2 段」既能用在 `SKU信息` 也能用在别的列，不必为每列复制一条。

⚠️ **`user_id` 只放父表**，子表经 `set_id` 归属：冗余一份 `user_id` 会带来
「父子 user_id 不一致」的静默脏数据（本项目同类教训）。查询一律 join 父表收口。

## 启用语义

**生效 = `set.enabled AND rule.enabled`**（父停 ⇒ 整包失效）。
⚠️ 父停**不改写子行的 `enabled`** —— 那是破坏用户设置：父恢复启用后，
子行按自己原来的状态走，用户不必逐条改回来。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base
from backend.app.core.time import shanghai_now

#: ---------------------------------------------------------------------------
#: 操作类型（`op` 枚举）
#: ---------------------------------------------------------------------------
#: ⚠️ **字段一次定型，实现分批**（用户口径 2026-10-14：「目前需求只有**提取 sku 名称**
#: 规则，其它**可能**支持……可完全取代当前表格顶部对列的批量操作功能」）。
#: ⇒ 本轮**只实现三个 `extract_*`**；`fixed_text` / `math_scale` 先落枚举与参数口径，
#: 免得将来加操作再迁移一次表。

#: 按分隔符切段取第 N 段。`params = {"sep": ";", "index": 2}`
#: ⚠️ `index` **1 起数**，支持负数（`-1` = 最后一段）；`0` 非法。
OP_EXTRACT_SEGMENT = "extract_segment"
#: 取包裹符之间的内容。`params = {"open": "(", "close": ")", "occurrence": 1}`
#: ⚠️ 只给 `open` 不给 `close` ⇒ 取到该行末尾。
OP_EXTRACT_BETWEEN = "extract_between"
#: 去前缀 / 去后缀。`params = {"prefix": "SKU图_01_", "suffix": ".jpg", "is_regex": false}`
#: ⚠️ **默认按字面**（`is_regex=false`）—— 正则**不做普通用户的默认工具**（设计 40 §1.2
#: 结论 3：把数据写死/学出枚举都是正则在这件事上的必然失败形态）。需要通配才显式打开。
OP_EXTRACT_STRIP = "extract_strip"
#: 整列设为固定文本 / 固定数值。`params = {"text": "100"}`
#: ⚠️ 用户口径 2026-10-15：「新增数值/固定文本规则，将当前操作列**批量设置为
#: 固定数值/文本**」—— `text` 原样写字符串，数值也只是「文本形态的数值」，
#: **不需要单独的数值 op**（`math_scale` 才是运算）。
OP_FIXED_TEXT = "fixed_text"

#: **复制列值**。`params = {}`（源列在执行时由前端传 `source_column`）。
#: ⚠️ 用户口径 2026-10-15：「新增一个复制列值的操作规则，如果为复制列值规则，
#: 则在 tip 中指定需要将哪一列复制到当前列」—— 源列是**执行参数**（表头弹窗里选），
#: 不是规则自身的属性（规则不绑列，设计 40 §9.1）⇒ `params` 为空。
OP_COPY_COLUMN = "copy_column"
#: 批量运算。`params = {"steps": [{"op": "mul"|"add"|"sub", "value": 1.7}, ...]}`
#: **按数组顺序依次执行** —— 加 / 减 / 乘**可随意搭配、不限步数**
#: （用户口径 2026-10-15：「计算操作类型的规则，加减乘可随意搭配」）。
#: 例：`×1.7` = `[{mul,1.7}]`；`+10−2` = `[{add,10},{sub,2}]`；
#: `×2+3×1.5` = `[{mul,2},{add,3},{mul,1.5}]`。
#: ⚠️ **顺序有意义**（先加后乘 ≠ 先乘后加）⇒ 不做任何排序/归并。
OP_MATH_SCALE = "math_scale"
#: **复制 + 提取（多步）**（用户口径 2026-10-15）：
#: 「新增一个复制加提取规则，**分为多步执行**，第一步规则是将指定列**复制到当前列**，
#:  第二步是将复制过来的列值按照规则**提取结果覆盖当前列**，第二步的规则是包含支持
#:  按分隔符取第 N 段、取包裹符之间的内容、去前缀/去后缀」。
#: ⚠️ 它不是「复制」+「提取」两个独立的 op 串起来 —— 那样用户要配两条规则、
#: 才能得到「A 列取第 2 段放到 B 列」这一个意图；这里**一条规则**表达完整意图。
#: ⚠️ 提取步骤**复用** `extract_*` 的同一批实现（见 `apply_extract_*`），
#: 不另写一份算法（DEF-044：同一件事不留两份实现）。
OP_COPY_EXTRACT = "copy_extract"

#: `math_scale` 每一步的运算符。
MATH_STEP_OPS: tuple[str, ...] = ("mul", "add", "sub")
#: 单条运算规则的**步数上限**（防手滑粘贴几百行；上限内随便搭）。
MATH_MAX_STEPS = 20

#: 本轮**已实现**的 op（其余在枚举内但调用即 400，见 service 的 `_IMPLEMENTED`）。
RULE_OPS: tuple[str, ...] = (
    OP_EXTRACT_SEGMENT,
    OP_EXTRACT_BETWEEN,
    OP_EXTRACT_STRIP,
    OP_FIXED_TEXT,
    OP_MATH_SCALE,
    OP_COPY_COLUMN,
    OP_COPY_EXTRACT,
)

#: ⚠️ **需要选「来源列」的 op**（用户口径 2026-10-15）：
#: · `copy_column`：源列 = **要复制哪一列**；
#: · `math_scale` ：源列 = **按哪一列计算**（「计算基础列」）。
#: 两者的**交互形态相同**（表头弹窗里选一列），但**语义不同** ⇒ 文案要分开。
#: 提取类 / 固定文本**不需要**（它们读的就是当前列自己）。
OPS_NEED_SOURCE_COLUMN: tuple[str, ...] = (
    OP_COPY_COLUMN,
    OP_MATH_SCALE,
    #: ⚠️ 复制+提取的第一步就是「把指定列复制过来」⇒ 同样需要来源列。
    OP_COPY_EXTRACT,
)

#: 「复制 + 提取」第二步可用的提取方式（与前端下拉、`apply_copy_extract` 三处同集合）。
#: ⚠️ 值刻意用**无前缀的短名**（`segment` / `between` / `strip`）而不是 `extract_segment`：
#: 它表达的是「方式」，`op` 前缀在嵌套里多余（读起来是 `extract.op = "segment"`）。
COPY_EXTRACT_STEPS: tuple[str, ...] = ("segment", "between", "strip")

#: ⚠️ **规则不再绑定列**（用户口径 2026-10-14：「规则**不限制只针对于固定列**，
#: 使用时根据**用户选择**」）—— 原 `column_name` / `column_match` 两列与
#: `COL_MATCH_*` 枚举整批退场；改用时由列表头弹窗把「列」当执行参数传进来。

class DataPackageRule(Base):
    """**数据包列规则** —— 一张自引用父子表（设计 40，2026-10-28 合并）。

    `parent_id IS NULL` = **规则包**（一类数据包一条，如「店透SKU预览」）；
    `parent_id` 非空 = **包下的一条具名规则**（取值写法）。
    合并前是 `data_package_rule_sets` + `data_package_rules` 两张表，父子 1:N。

    ⚠️ **规则不绑列**：一条规则是「怎么从一段文本里取出一段」的写法，
    用在哪一列由使用时选。

    ⚠️ **`user_id` 每行都写**（父行与子行同值）：查询一律按它收口，不必 join 父表。
    写入时子行的 `user_id` 一律取父行的，不接受客户端传入。

    **生效 = 父行 enabled AND 子行 enabled**（父停 ⇒ 整包失效）。
    ⚠️ 父停**不改写子行的 `enabled`** —— 那是破坏用户设置：父恢复启用后，
    子行按自己原来的状态走。
    """

    __tablename__ = "xhs_data_package_rules"
    __table_args__ = (
        #: 同用户下**父行（规则包）名不重名**；子行在**同一父包内规则名不重名**。
        #: 一个部分索引覆盖两种（`parent_id IS NULL` / 非空各一条）。
        Index(
            "uq_xhs_dp_rules_owner_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
        ),
        Index(
            "uq_xhs_dp_rules_child_name",
            "parent_id",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: `NULL` = 规则包（父行）；非空 = 该包下的一条规则（子行）。
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("xhs_data_package_rules.id", ondelete="CASCADE"), index=True, nullable=True
    )
    #: 数据隔离（AGENTS.md 硬约束）：规则是**用户资产**，不是全局共享配置。
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_users.id"), index=True, nullable=False)
    #: 父行 = 规则包名（同用户下不重名）；子行 = **规则名称**（不是列名）。
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: 父行备注；子行不用。
    remark: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    #: 子行操作类型（`OP_*`）；父行留空。
    op: Mapped[str] = mapped_column(String(30), default="", nullable=False)
    #: 子行操作参数（JSON 文本）。父行留 `"{}"`。
    #: ⚠️ 用 `Text` 存 JSON 而不是 `JSON` 列：原地改不标脏的坑。
    params_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    #: 单条规则 / 整包的启用态（子行看父行是否也启用）。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 展示排序：列表要有稳定顺序，否则「刷新一下就变位」。
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=shanghai_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=shanghai_now, onupdate=shanghai_now, nullable=False
    )

    #: ---- 自引用父子关系（**懒加载**：访问 `.children` / `.parent` 时才发 SQL）----
    #: 不预加载：规则包列表页不需要子行，父行详情按需再查。
    children: Mapped[list["DataPackageRule"]] = relationship(
        back_populates="parent",
        foreign_keys=[parent_id],
        lazy="select",
    )
    parent: Mapped[Optional["DataPackageRule"]] = relationship(
        back_populates="children",
        foreign_keys=[parent_id],
        remote_side=[id],
        lazy="select",
    )

    #: ---- 旧字段名的读写别名（合并前的 `set_id` / `rule_name`）----
    #: 服务层与前端按旧名读写，这里映射到底层列，避免改 20 多处调用点。
    @property
    def set_id(self) -> Optional[int]:
        return self.parent_id

    @set_id.setter
    def set_id(self, value: Optional[int]) -> None:
        self.parent_id = value

    @property
    def rule_name(self) -> str:
        return self.name

    @rule_name.setter
    def rule_name(self, value: str) -> None:
        self.name = value


__all__ = [
    "OPS_NEED_SOURCE_COLUMN",
    "OP_COPY_COLUMN",
    "OP_COPY_EXTRACT",
    "COPY_EXTRACT_STEPS",
    "MATH_MAX_STEPS",
    "MATH_STEP_OPS",
    "DataPackageRule",
    "OP_EXTRACT_BETWEEN",
    "OP_EXTRACT_SEGMENT",
    "OP_EXTRACT_STRIP",
    "OP_FIXED_TEXT",
    "OP_MATH_SCALE",
    "RULE_OPS",
]
