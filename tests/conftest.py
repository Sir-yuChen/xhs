"""测试树的公共引导（只做「把仓库根放上 sys.path」这一件事）。

放在这里而不是每个测试文件里 `sys.path.insert(0, str(ROOT))` —— 那套样板
在 41 个测试文件里重复了 41 份。新增测试文件**不要再抄**这套。

⚠️ 这里**故意不 import** `backend.app.main` 等重模块：一 import 就会把
app / 调度器 / 全部模型拖进会话，连纯单元测试也要付这个代价（实测 ~1.5s），
而且会让「本来不依赖 app 的测试文件」意外获得副作用。要用的**在 fixture 里
惰性 import**，反正 Python 模块缓存会让第二次免费。

> **注册开关**（`register.enabled`）的测试口径**不在这里**：它在
> `tests/backend/conftest.py::api_client` 里按夹具打开（理由见那里的注释 ——
> 全仓 `POST /auth/register` 都是「造用户」的夹具手段，而产品基线是关着的）。
> 做成**会话级 autouse** 会让「读产品基线字面值」的守卫读到被打过补丁的值，
> 从而把「基线是不是关着」这件事变成永真断言（实测：两条守卫因此假红/假绿）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
