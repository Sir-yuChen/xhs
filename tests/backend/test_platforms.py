from backend.app.core.platforms import PlatformId, get_platforms

import pytest




#: 测试分层标记（2026-10-23 立，见 `docs/04_测试文档/01_测试体系与流程规范.md` §15.2）。
#: 分类判据由 `.hermes/scripts/classify_tests.py` 机械生成，不靠人工印象：
#:   redline     = 挂了代表「数据会被写坏 / 凭据会泄漏 / 平台会被封 / 迁移不可执行」
#:   contract    = 端点 / 字段 / 行为与文档一致
#:   integration = 真跑迁移 / 子进程（慢且重，收尾跑）
pytestmark = pytest.mark.contract

def test_platform_registry_marks_xhs_enabled_and_others_coming_soon():
    platforms = get_platforms()
    by_id = {platform.id: platform for platform in platforms}

    assert by_id[PlatformId.XHS].enabled is True
    assert by_id[PlatformId.XHS].accent_color == "#ff2442"
    assert by_id[PlatformId.DOUYIN].enabled is False
    assert by_id[PlatformId.KUAISHOU].status == "coming_soon"
    assert set(by_id) == {
        PlatformId.XHS,
        PlatformId.DOUYIN,
        PlatformId.KUAISHOU,
        PlatformId.WEIBO,
        PlatformId.XIANYU,
        PlatformId.TAOBAO,
    }
