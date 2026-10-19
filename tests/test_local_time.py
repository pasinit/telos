from telos import local_time


def test_format_local_timestamp_converts_utc_to_configured_local_timezone(monkeypatch):
    monkeypatch.setenv("TELOS_LOCAL_TIMEZONE", "Europe/London")
    local_time.local_timezone.cache_clear()

    try:
        assert local_time.format_local_timestamp("2026-06-02T23:30:00+00:00") == "2026-06-03 00:30"
    finally:
        local_time.local_timezone.cache_clear()


def test_format_local_timestamp_treats_naive_values_as_utc(monkeypatch):
    monkeypatch.setenv("TELOS_LOCAL_TIMEZONE", "Europe/London")
    local_time.local_timezone.cache_clear()

    try:
        assert local_time.format_local_timestamp("2026-06-02T23:30:00") == "2026-06-03 00:30"
    finally:
        local_time.local_timezone.cache_clear()
