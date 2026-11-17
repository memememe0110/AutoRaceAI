from run_ver330a_actions import _require_same, _race_set_sha256, ALLOWED_MODES


def test_require_same_rejects_changed_pin():
    try:
        _require_same({"app_sha256": "aaa"}, "app_sha256", "bbb", "app SHA")
    except SystemExit as exc:
        assert "app SHA mismatch" in str(exc)
    else:
        raise AssertionError("mismatch was not rejected")


def test_require_same_allows_first_run_and_same_value():
    _require_same({}, "mode", "Ver326", "mode")
    _require_same({"mode": "Ver330-A"}, "mode", "Ver330-A", "mode")


def test_race_set_hash_is_order_sensitive():
    assert _race_set_sha256(["a", "b"]) != _race_set_sha256(["b", "a"])
    assert _race_set_sha256(["a"]) == _race_set_sha256(["a"])


def test_modes_are_explicit():
    assert ALLOWED_MODES == ("Ver326", "Ver330-A")
