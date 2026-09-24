"""定向变异：验证内容库页「缺值不展示 + 不解释机制」守卫**仍然有效**。

守护对象是 `test_library_shows_the_real_author_or_nothing` 那三条断言：
  ① `'"未知"' not in code` —— 昵称缺值不得写「未知」充数；
  ② `"下一轮同步" not in code` —— 界面不得再解释「同步会把待发布升回去」；
  ③ 正向闸门 `selectedNote.author_name ?` / `note.author_name &&` 必须还在。

变异刻意分三类：
  * **加回型**（M1/M2）—— 把被删掉的文案塞回去，抓反向断言是否恒真；
  * **真移除型**（M3/M4/M5）—— 把闸门拆掉，抓正向断言；
  * **同义串扰型**（M6）—— 别处冒出「未知」，抓断言是否只认自己那一处。

每个变异：备份 -> 改写 -> 只跑目标用例 -> 必须 FAIL -> 还原。
任何一步不符预期就报错退出，避免留下被改坏的源码。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
LIBRARY = ROOT / "frontend/src/pages/platforms/xhs/library-page.tsx"

TARGET = "test_library_shows_the_real_author_or_nothing"

MUTATIONS = [
    (
        "M1 详情作者加回「未知」",
        LIBRARY,
        "{selectedNote.author_name}",
        '{selectedNote.author_name || "未知"}',
    ),
    (
        "M2 发布状态加回机制说明",
        LIBRARY,
        """onChange={(checked) => void togglePublished(selectedNote, checked)}
                  />
                </Space>""",
        """onChange={(checked) => void togglePublished(selectedNote, checked)}
                  />
                  <Text type="secondary">同步回来的笔记默认「已发布」；下一轮同步会把改回「待发布」的再升回去</Text>
                </Space>""",
    ),
    (
        "M3 表格作者列改名（作者真的不展示了）",
        LIBRARY,
        '"author_name"',
        '"author_nick"',
    ),
    (
        "M4 详情闸门退回缺值回退",
        LIBRARY,
        "{selectedNote.author_name ? (",
        "{selectedNote.author_name || (",
    ),
    (
        "M5 卡片闸门退回缺值回退",
        LIBRARY,
        "{note.author_name && <Text",
        "{note.author_name || <Text",
    ),
    (
        "M6 别处冒出「未知」（同义串扰）",
        LIBRARY,
        '{note.title || "未命名"}',
        '{note.title || "未知"}',
    ),
]


def run_one(test_name: str) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", f"tests/backend/test_page_optimizations.py::{test_name}", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    bad: list[str] = []
    for label, path, old, new in MUTATIONS:
        data = path.read_bytes()
        text = data.decode("utf-8")
        if old not in text:
            print(f"[SKIP] {label} -- 锚点不存在：{old!r}")
            bad.append(label)
            continue
        try:
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            code, out = run_one(TARGET)
            tail = [ln for ln in out.splitlines() if "passed" in ln or "failed" in ln][-1:]
            if code != 0:
                print(f"[OK  ] {label} -> 变红（守卫有效） {' '.join(tail)}")
            else:
                print(f"[BAD ] {label} -> 仍然绿（守卫失效！） {' '.join(tail)}")
                bad.append(label)
        finally:
            path.write_bytes(data)
        if path.read_bytes() != data:
            print(f"[FATAL] {path} 还原失败")
            return 2
    print()
    if bad:
        print(f"MUTATION: FAIL —— {len(bad)} 个变异未被抓住：")
        for b in bad:
            print("  -", b)
        return 1
    print(f"MUTATION: PASS —— {len(MUTATIONS)} 个变异全部被抓住")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
