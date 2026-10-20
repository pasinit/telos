import os
from pathlib import Path

import pytest

from telos_cardea._vendor import SOURCES
from telos_cardea.config import ConfigError, load_config

from conftest import PACKAGE_ROOT

REPO_ROOT = PACKAGE_ROOT.parents[1]
VENDOR = PACKAGE_ROOT / "src" / "telos_cardea" / "_vendor"


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_vendored_copy_matches_telos_source(name: str) -> None:
    source, rewrites = SOURCES[name]
    path = REPO_ROOT / source
    if not path.exists():
        pytest.skip("not inside the telos repository")
    expected = path.read_text()
    for old, new in rewrites:
        expected = expected.replace(old, new)
    assert (VENDOR / name).read_text() == expected, f"re-copy {source} into _vendor/{name}"


def test_package_never_imports_telos() -> None:
    for path in (PACKAGE_ROOT / "src").rglob("*.py"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            assert not stripped.startswith(("import telos.", "from telos.", "import telos\n")), f"{path}: {line}"


def test_env_overrides_private_file(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text('[smtp]\nhost = "smtp-relay.brevo.com"\nuser = "u"\npassword = "file-pass"\nfrom = "telos@example.org"\n'
                    '[recordatio.scopes."telos-owner/telos-homey"]\nuser_id = "owner"\nslug = "homey"\n'
                    '[telos]\nurl = "http://telos.test:7445"\ntoken_file = "/run/telos-cardea/telos-internal.jwt"\n')
    os.chmod(file, 0o600)
    config = load_config({"TELOS_CARDEA_CONFIG": str(file), "SMTP_PASSWORD": "env-pass", "SEARCH_ENGINE": "Serper"})
    assert config.smtp.host == "smtp-relay.brevo.com" and config.smtp.port == 587
    assert config.smtp.password == "env-pass"
    assert config.recordatio_scope("telos-owner", "telos-homey") == config.recordatio_scopes["telos-owner/telos-homey"]
    assert config.recordatio_scope("telos-owner", None) is None
    assert config.telos_url == "http://telos.test:7445"
    assert config.telos_token_file == "/run/telos-cardea/telos-internal.jwt"
    assert load_config({"TELOS_CARDEA_CONFIG": str(file), "TELOS_INTERNAL_URL": "http://telos.internal.example:7445"}).telos_url == "http://telos.internal.example:7445"
    assert config.search_engine == "serper"


def test_group_readable_config_is_refused(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text("")
    os.chmod(file, 0o640)
    with pytest.raises(ConfigError):
        load_config({"TELOS_CARDEA_CONFIG": str(file)})


def test_missing_config_file_is_allowed(tmp_path: Path) -> None:
    assert load_config({"TELOS_CARDEA_CONFIG": str(tmp_path / "absent.toml")}).smtp.host == ""
