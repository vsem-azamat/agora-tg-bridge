import pytest


@pytest.fixture(autouse=True)
def no_user_config_file(tmp_path, monkeypatch):
    """Keep a config file of the machine running the tests out of them."""
    monkeypatch.setattr("tg_bridge.config.DEFAULT_CONFIG_FILE", str(tmp_path / "absent.toml"))
