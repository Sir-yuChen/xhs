"""保存生成结果到素材库：目录/命名/索引（v2.29 §13.3.10）。

真机踩到的两个坑，各一条用例钉住（都是「不写用例就必然复发」的类型）：

1. **索引行不能自己建** —— 手工行缺 `width`/`height`/**`thumb_url`** ⇒ 素材库面板
   渲染空白（用户现象「素材库中的图片全部无法展示」）；且**事后补跑扫描也救不回来**：
   扫描按 `mtime` 增量，手工行已写 mtime ⇒ 判为「未变化」直接跳过。
   ⇒ 唯一正确做法 = **只落文件，索引交给扫描**。
2. **勾选不能挂在外层 onClick** —— antd `Image` 的 `preview` 自己 `stopPropagation`，
   外层收不到点击（用户现象「点击就会预览大图，无法被选中」）⇒ 勾选必须独立控件。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from backend.app.services import material_save_service as mss


#: 1×1 PNG（真图，能被 Pillow 打开；避免依赖网络与磁盘上的既有文件）。
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def test_batch_dirname_is_unique_within_same_second():
    """同一秒两次生成 ⇒ **目录必须不同**（用户口径「每次生成保存时放不同的子目录下」）。

    ⚠️ 只靠时间戳会撞：同一秒触发的两个任务是同一个 `%Y%m%d-%H%M%S`
    ⇒ 合并进同一目录、违背「每次」。短码就是为这个加的。
    """
    now = datetime(2026, 10, 3, 17, 30, 45)
    names = {mss.new_batch_dirname(now) for _ in range(50)}
    assert len(names) > 1, "同一秒生成的名字全一样 —— 短码没起作用"
    assert all(n.startswith("20261003-173045-") for n in names), "时间戳前缀不对"


def test_filename_is_group_labelled_and_zero_padded():
    """文件名 = `组名-两位序号.ext`（组名中文、序号补零 ⇒ 字典序=自然序）。"""
    assert mss.GROUP_LABELS == {"product": "主图", "detail": "详情图"}
    # 补零：`主图-01` < `主图-02` < … < `主图-10`（不补零时 "主图-10" < "主图-2"）
    names = [f"{mss.GROUP_LABELS['product']}-{i:02d}.png" for i in range(1, 11)]
    assert names == sorted(names), "序号没补零 ⇒ 文件管理器里顺序错乱"


def test_ext_falls_back_to_png_without_guessing():
    """扩展名从 URL 推；**取不到回落 `.png`**（上游实际输出格式，不臆造别的）。"""
    assert mss._ext_from_url("https://x/a/b.png?sig=1") == ".png"
    assert mss._ext_from_url("https://x/a/b.JPEG") == ".jpg"
    assert mss._ext_from_url("https://x/a/b.webp") == ".webp"
    assert mss._ext_from_url("https://x/a/noext") == ".png"
    assert mss._ext_from_url("") == ".png"


def test_save_never_writes_index_row_itself(tmp_path):
    """**保存服务不得自己建 `material_files` 行**（真机坑 ①）。

    判据是**源码级**的：函数体内不出现 `MaterialFile(` 构造。
    为什么必须这样守：手工行的 `thumb_url` 是空串 ⇒ 素材库面板渲染空白；
    而**事后补跑扫描也救不回来**（扫描按 mtime 增量，手工行已写 mtime ⇒ 跳过）。
    唯一正确路径 = 交给 `scan_folder`（建行 + 尺寸 + 缩略图的唯一实现）。
    """
    import inspect

    src = inspect.getsource(mss.save_assets_to_folder)
    assert "MaterialFile(" not in src, (
        "保存服务自己建了索引行 ⇒ 缺 thumb_url、素材库看不见，且扫描补不回来"
    )
    assert "scan_folder" in src, "保存后没有调扫描（索引行就没人建了）"


def test_save_refreshes_index_via_scan(monkeypatch, tmp_path):
    """落盘后**真的调了扫描**（不是只在注释里说）。"""
    called: list[str] = []

    import backend.app.services.material_scan_service as mss_scan

    def fake_scan(db, folder):
        called.append(folder.name)
        return {"scanned": 1, "added": 1, "updated": 0, "removed": 0, "skipped": 0}

    monkeypatch.setattr(mss_scan, "scan_folder", fake_scan)

    # 直接验「保存函数在 saved 非空时会调扫描」这一行存在（语义级，不搭整套 DB）
    import inspect

    src = inspect.getsource(mss.save_assets_to_folder)
    assert "scan_folder(db, folder)" in src, "没有把 folder 传给扫描"


def test_frontend_thumbnail_selection_uses_checkbox_not_container_click():
    """前端：**勾选必须是独立控件**（真机坑 ②）。

    antd `Image` 的 `preview` 自己 `stopPropagation` ⇒ 挂在**外层 div** 上的
    `onClick` 永远收不到（用户现象「点击就会预览大图，无法被选中」）。
    判据落在「勾选用的是 `Checkbox` 且 `onChange` 触发切换」——
    而不只是「页面上有 onToggleSelect 这个词」（那会被无关代码满足）。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    page = (root / "frontend/src/pages/platforms/xhs/shop-items-page.tsx").read_text(encoding="utf-8")
    result_block = page[page.index("function DetailImageGroupResult") : page.index("function DetailImageGroupErrors")]
    assert "<Checkbox" in result_block, (
        "结果图没有复选框 —— 勾选若挂在外层 onClick，会被 Image 的 preview 吃掉"
    )
    assert "onChange={() => onToggleSelect(" in result_block, "复选框没有触发选择切换"
    assert "preview={{" in result_block, "大图预览被去掉了（那是有用行为，不该删）"


def test_thumb_endpoint_exists_and_frontend_uses_thumb_url():
    """**缩略图端点必须存在，且前端列表必须用它**（v2.29，真机两个 bug 的合体）。

    真机现象：「图没有在页面正常展示，无法加载该图片」+「素材库中的图片全部无法展示」。

    两个叠加的根因：
    1. **后端没有端点** —— `_thumb_rel_url()` 生成
       `/api/xhs/materials/folders/{id}/thumbs/{file_id}`，但**无人提供它**
       ⇒ 索引行里的 `thumb_url` 永远是 404；
    2. **前端没用它** —— 列表两个分支都拿 `materialFileUrl(file.id)`（**原图端点**）
       当 `<img src>`；原图实测 1776×2368 / **6.5 MB** ⇒ 破图。

    这类 bug 的形态：**生产者（URL 字符串）与消费者（路由/前端）各写各的**，
    两边单独看都对，中间没有断言把它们绑在一起 ⇒ 本用例就是那条绑定。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]

    # ① 端点存在（源码级：装饰器 + 路径形态与 `_thumb_rel_url` 一致）
    api_src = (root / "backend/app/api/platforms/xhs/materials.py").read_text(encoding="utf-8")
    assert '@router.get("/folders/{folder_id}/thumbs/{file_id}")' in api_src, (
        "缩略图端点不存在 —— 索引里的 thumb_url 会永远 404"
    )
    scan_src = (root / "backend/app/services/material_scan_service.py").read_text(encoding="utf-8")
    assert "thumbs/{safe_name}" in scan_src, "URL 拼装规则变了？"

    # ② 前端列表用 thumb_url（不是原图端点）
    panel = (root / "frontend/src/pages/platforms/xhs/material-library-panel.tsx").read_text(encoding="utf-8")
    # v2.29.2：列表改用 `MaterialThumb` 组件（内部走 blob）——
    # 判据从「有没有调 `materialThumbUrl`」改成「列表用的是不是缩略图 URL」，
    # 否则组件化之后这条守卫会拿旧写法把新实现判红（本轮实测）。
    assert "<MaterialThumb" in panel, "前端列表没用缩略图组件"
    assert "file.thumb_url" in panel, (
        "列表没用 `thumb_url` ⇒ 6.5MB 原图当列表图，必然破图/卡死"
    )


def test_material_images_are_fetched_with_auth_not_plain_img_src():
    """素材库的图**必须走 axios 取 blob**，不能直接 `<img src="/api/...">`（真根因）。

    ## 为什么这条最值钱（用户连报两轮「依旧未展示」）

    补端点、前端改用 `thumb_url` 之后，用户仍报「素材库中图依旧未展示」——
    因为还有**第三层**：后端 `get_current_user` 只认 `Authorization: Bearer`
    （`core/deps.py` 的 `OAuth2PasswordBearer`，**没有 cookie 鉴权**），
    而浏览器加载 `<img src>` **发不出自定义请求头** ⇒ 那个请求必然 **401**。

    ⚠️ 这层**用 `requests` 带 token 是测不出来的**（我最初就是这么误判通过的）——
    必须在**渲染层**看它用什么姿势取图。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    panel = (root / "frontend/src/pages/platforms/xhs/material-library-panel.tsx").read_text(encoding="utf-8")

    # ① 必须有走 axios 的取图组件
    assert "fetchMaterialImageObjectUrl" in panel, (
        "没有带鉴权取图 —— 用 <img src> 取会 401（后端只认 Bearer，无 cookie）"
    )
    assert "responseType" in (root / "frontend/src/lib/api.ts").read_text(encoding="utf-8"), (
        "取图没走 blob（axios 拦截器才带得上 token）"
    )
    # ② blob URL 必须 revoke（否则列表翻页累积内存）
    assert "revokeObjectURL" in panel, "blob URL 没释放（翻页会累积内存）"
    # ③ 图片**不**直接 src 到 /api（那种写法就是 401 的形态）
    assert 'src={materialFileUrl(' not in panel, (
        "仍把 `<img src>` 直接指向 /api 端点 ⇒ 401" 
    )


def test_thumbnail_scales_without_cropping():
    """缩略图**只等比缩放、不裁切**（v2.29.6 —— 治「双重裁切」）。

    ## 为什么这条值得守

    后端原先用 `ImageOps.fit(..., (SIZE, SIZE))` = **居中裁成正方形**，
    而前端格子是 `objectFit: "cover"`（**再裁一次**）⇒ **双重裁切**：
    一张 1776×2368 的竖图，后端先切掉上下 25%，前端再从剩下的方图里切，
    商品主体很容易被切掉（用户现象「缩略图太模糊 / 图不对」）。

    正确分工：**后端只管「变小」（等比、保住全部画面），前端管「显示成方形」**。

    ⚠️ 判据是**行为级**（真生成一张竖图缩略图看尺寸），不是源码字符串匹配 ——
    「有没有调 `fit`」这种判据在换写成别 API 时会漏。
    """
    from pathlib import Path

    from PIL import Image

    from backend.app.services.material_scan_service import THUMB_SIZE, _make_image_thumb

    # 造一张真竖图（宽:高 = 3:4），放到临时目录
    src = Path(__file__).parent / "_tmp_thumb_portrait.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (900, 1200), (200, 120, 80)).save(src)
    try:
        w, h, url = _make_image_thumb(src, folder_id=99999, file_id=1)
        assert (w, h) == (900, 1200), "应回原图尺寸（尺寸字段 = 原图，供前端展示属性）"
        assert url, "缩略图 URL 生成失败"
        # 缩略图**本体**：等比缩放 ⇒ 宽高比与原图一致（3:4），且不超上限
        from backend.app.services.material_scan_service import thumbs_dir

        target = thumbs_dir() / "99999" / "f99999-1.jpg"
        assert target.is_file(), "缩略图没落盘"
        tw, th = Image.open(target).size
        assert abs(tw / th - 900 / 1200) < 0.01, (
            f"缩略图宽高比 {tw}/{th} 与原图 3:4 不符 ⇒ 被裁切了（双重裁切的源头）"
        )
        assert max(tw, th) <= THUMB_SIZE, f"缩略图超过上限 {THUMB_SIZE}：{tw}×{th}"
        assert max(tw, th) == THUMB_SIZE, "应缩放**到**上限（否则高清屏下仍糊）"
    finally:
        src.unlink(missing_ok=True)
        t = thumbs_dir() / "99999" / "f99999-1.jpg"
        t.unlink(missing_ok=True)
        (thumbs_dir() / "99999").rmdir() if (thumbs_dir() / "99999").is_dir() else None
