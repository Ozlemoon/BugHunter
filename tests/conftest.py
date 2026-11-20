import shutil

import pytest

from tests.helpers import PLAYGROUND


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
