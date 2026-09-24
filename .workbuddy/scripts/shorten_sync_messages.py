# -*- coding: utf-8 -*-
"""同步链路提示文案收短（2026-09-22 用户口径：「缩短异常，异常信息只需要简洁保留核心即可」）。

一次性脚本，跑完即弃。每条替换都断言命中次数，避免静默漏改。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

NOTE = ROOT / "backend/app/services/note_sync_service.py"
GOODS = ROOT / "backend/app/services/shop_goods_sync_service.py"

# (文件, 旧串, 新串)
PAIRS = [
    # --- note_sync_service.py ---
    (
        NOTE,
        '"该账号需要主页访问令牌（xsec_token）才能读取笔记列表，"\n                "请在「我的笔记」页粘贴主页链接后重试。",',
        '"需要主页链接（xsec_token）才能读取笔记列表。",',
    ),
    (
        NOTE,
        'identity.self_message or "无法识别当前账号身份，请确认 Cookie 是否有效",',
        'identity.self_message or "无法识别账号身份",',
    ),
    (
        NOTE,
        'warnings.append(f"第 {page_no} 页前等待限流额度超时，本次到此为止")',
        'warnings.append(f"第 {page_no} 页前限流等待超时")',
    ),
    (
        NOTE,
        'warnings.append(\n                f"已达绝对翻页兜底（{sts.ABSOLUTE_PAGE_CAP} 页），终止本轮以免死循环"\n            )',
        'warnings.append(f"已达绝对页数兜底（{sts.ABSOLUTE_PAGE_CAP} 页）")',
    ),
    (
        NOTE,
        'warnings.append(f"已达本次指定的翻页上限（{max_pages} 页）")',
        'warnings.append(f"已达指定页数上限（{max_pages} 页）")',
    ),
    (
        NOTE,
        'warnings.append(f"第 {page_no} 页获取失败，已保留前面同步的 {stored} 条")',
        'warnings.append(f"第 {page_no} 页获取失败")',
    ),
    (
        NOTE,
        '            warnings.append(f"{source[\'source\']} 源失败：{reason}")\n',
        "",
    ),
    (
        NOTE,
        'warnings.append(\n            f"正文与图集待补齐 {hydration[\'pending\']} 条"\n            "（受平台限流约束，每轮同步继续补；打开笔记详情会即时补齐该条）"\n        )',
        'warnings.append(f"正文待补齐 {hydration[\'pending\']} 条")',
    ),
    (
        NOTE,
        '        missing = ["网页版（PC）", "千帆商家后台"]\n        if account.sub_type == SUB_TYPE_CREATOR:\n            missing.append("创作者平台")\n        raise SyncFailure(\n            "该账号没有可用的笔记同步令牌，请登录 "\n            + " / ".join(missing)\n            + " 后再次触发同步笔记。",\n            code="NOTE_SOURCES_UNAVAILABLE",\n        )',
        '        raise SyncFailure("没有可用的笔记同步令牌。", code="NOTE_SOURCES_UNAVAILABLE")',
    ),
    (
        NOTE,
        '"CREATOR_SESSION_REQUIRED: 该账号没有可用的创作者平台登录会话，"\n                "请补齐创作者平台登录后再次触发同步笔记。"',
        '"CREATOR_SESSION_REQUIRED: 缺少创作者平台登录会话，请补齐后再试。"',
    ),
    # --- shop_goods_sync_service.py ---
    (
        GOODS,
        '"该账号还没有千帆（ark）令牌，无法同步商品。请先给这个账号导入千帆登录态。",',
        '"该账号缺少千帆（ark）令牌。",',
    ),
    (
        GOODS,
        'warnings.append(f"第 {page_no} 页前等待限流额度超时，本次到此为止")',
        'warnings.append(f"第 {page_no} 页前限流等待超时")',
    ),
    (
        GOODS,
        'warnings.append(\n                f"已达绝对翻页兜底（{sts.ABSOLUTE_PAGE_CAP} 页），终止本轮以免死循环"\n            )',
        'warnings.append(f"已达绝对页数兜底（{sts.ABSOLUTE_PAGE_CAP} 页）")',
    ),
    (
        GOODS,
        'warnings.append(f"已达本次指定的翻页上限（{max_pages} 页）")',
        'warnings.append(f"已达指定页数上限（{max_pages} 页）")',
    ),
    (
        GOODS,
        'warnings.append(f"第 {page_no} 页获取失败，保留前 {len(stored_ids)} 条")',
        'warnings.append(f"第 {page_no} 页获取失败")',
    ),
    (
        GOODS,
        'warnings.append("本次未翻完，保留全部既有商品行（不删）")',
        'warnings.append("未翻完，保留既有行")',
    ),
    (
        GOODS,
        'warnings.append(\n                f"补详情第 {start // SKU_BATCH_SIZE + 1} 批前等待限流额度超时，本段到此为止"\n            )',
        'warnings.append(f"补详情第 {start // SKU_BATCH_SIZE + 1} 批前限流等待超时")',
    ),
    (
        GOODS,
        'warnings.append(f"一批 {len(batch)} 个商品的详情获取异常，保留原状")',
        'warnings.append(f"{len(batch)} 个商品详情获取异常")',
    ),
    (
        GOODS,
        'warnings.append(f"一批 {len(batch)} 个商品的详情获取失败：{message or \'未知原因\'}")',
        'warnings.append(f"{len(batch)} 个商品详情获取失败：{message or \'未知原因\'}")',
    ),
]


def main() -> int:
    cache: dict[Path, str] = {}
    hits: dict[Path, int] = {}
    misses: list[str] = []

    for path, old, new in PAIRS:
        text = cache.get(path)
        if text is None:
            text = path.read_text(encoding="utf-8")
        if old not in text:
            misses.append(f"{path.name}: {old[:48]!r}")
            cache[path] = text
            continue
        count = text.count(old)
        cache[path] = text.replace(old, new)
        hits[path] = hits.get(path, 0) + count

    for path, text in cache.items():
        path.write_text(text, encoding="utf-8")
        print(f"WROTE {path.name}  replacements={hits.get(path, 0)}")

    if misses:
        print("MISSES:")
        for item in misses:
            print("  -", item)
        return 1
    print("ALL_REPLACEMENTS_APPLIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
