import shutil

import pytest

from core import config
from tests.helpers import PLAYGROUND


@pytest.fixture(autouse=True)
def ignore_local_env_file(monkeypatch):
    """Tests must not pick up the developer's .env (real SMTP account, tokens, ports)."""
    monkeypatch.setattr(config, "_load_dotenv", lambda path: None)
    monkeypatch.setenv("BUGHUNTER_SCHEDULER", "off")  # tests drive Scheduler.tick() themselves
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def api_copy(tmp_path):
    """A disposable copy of the buggy sample API that tests may patch."""
    target = tmp_path / "sample_api.py"
    shutil.copy(PLAYGROUND / "sample_api.py", target)
    return target


@pytest.fixture
def web_copy(tmp_path):
    """A disposable copy of the buggy sample web app (with its templates)."""
    target = tmp_path / "sample_web.py"
    shutil.copy(PLAYGROUND / "sample_web.py", target)
    shutil.copytree(PLAYGROUND / "templates", tmp_path / "templates")
    return target
