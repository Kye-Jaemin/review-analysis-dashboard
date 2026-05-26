from app.i18n import translate


def test_translate_basic():
    assert translate("en", "nav.dashboard") == "Dashboard"
    assert translate("ko", "nav.dashboard") == "대시보드"


def test_translate_missing_falls_back_to_en():
    # Unknown key returns the key itself
    assert translate("ko", "does.not.exist") == "does.not.exist"


def test_translate_with_params():
    assert "5" in translate("en", "analyze.unanalyzed", count=5)
