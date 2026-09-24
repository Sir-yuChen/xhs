"""今日日志收尾（字节级，全部经 Python，不用 heredoc）：

1. 修复 2794~3041 字节处**早前 heredoc 造成的单行损坏**：原行首 2 字节被
   `\x96\x87` 替换，其余完好。按上下文（上一行讲沙箱 curl 坑、本行讲
   "…文档校验脚本 PASS；服务已用新 launcher（pid 42480）重启…"）还原为
   "两个" —— 与项目口径「两个校验脚本都要跑」一致。**此还原记录在本次
   追加的日志条目里，不静默**。
2. 追加本轮（商品挂链笔记二次报告 → 修复）的工作记录。
"""
from pathlib import Path

LOG = Path(__file__).resolve().parents[2] / ".workbuddy" / "memory" / "2026-09-23.md"
ADD = Path(__file__).with_name("log-append.md")

raw = LOG.read_bytes()

# --- 1. 修复既有损坏行 -----------------------------------------------------
bad_start, bad_end = 2794, 3041
damaged = raw[bad_start:bad_end]
assert damaged[:2] == b"\x96\x87", f"损坏区起点不符: {damaged[:8]!r}"
rest_line = damaged[2:].decode("utf-8")  # 其余部分完好
repaired = ("两个" + rest_line).encode("utf-8")
raw = raw[:bad_start] + repaired + raw[bad_end:]
raw.decode("utf-8")  # 整体必须合法
print("损坏行已修复，新长度:", len(repaired), "字节")

# --- 2. 追加本轮日志 -------------------------------------------------------
add = ADD.read_text(encoding="utf-8")
text = raw.decode("utf-8")
if not text.endswith("\n"):
    text += "\n"
LOG.write_bytes((text + add).encode("utf-8"))

back = LOG.read_bytes()
back.decode("utf-8")
print("日志追加 OK，总字节:", len(back), "，总行数:", back.decode("utf-8").count("\n"))
