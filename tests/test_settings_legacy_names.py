"""Pre-rename MEMENTO_* settings still configure the Recordatio URLs."""

from telos.api.settings import Settings


def test_legacy_memento_env_names_are_accepted(monkeypatch):
    monkeypatch.delenv("RECORDATIO_URL", raising=False)
    monkeypatch.delenv("RECORDATIO_PUBLIC_URL", raising=False)
    monkeypatch.setenv("MEMENTO_URL", "http://memento:8787")
    monkeypatch.setenv("MEMENTO_PUBLIC_URL", "https://host/memento")
    s = Settings(_env_file=None)
    assert s.recordatio_url == "http://memento:8787"
    assert s.recordatio_public_url == "https://host/memento"


def test_new_name_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("MEMENTO_URL", "http://old:8787")
    monkeypatch.setenv("RECORDATIO_URL", "http://recordatio:8787")
    assert Settings(_env_file=None).recordatio_url == "http://recordatio:8787"
