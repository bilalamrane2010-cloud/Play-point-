import os
import pathlib
import sys
import tempfile

import pytest

# The bot reads BOT_DATA_DIR at import time, so it must be set before importing core.
TEST_DATA_DIR = tempfile.mkdtemp(prefix="playpoint_test_data_")
os.environ["BOT_DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("HEADLESS", "1")
os.environ.pop("OPENAI_API_KEY", None)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import core  # noqa: E402


@pytest.fixture(autouse=True)
def clean_storage():
    """Start every test with an empty data directory."""
    root = pathlib.Path(TEST_DATA_DIR)
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        except OSError:
            pass
    yield


@pytest.fixture
def data_dir():
    return pathlib.Path(TEST_DATA_DIR)


@pytest.fixture
def sample_account():
    assert core.add_account(
        email="tester@gmail.com",
        first_name="Test",
        last_name="User",
        app_password="abcd efgh ijkl mnop",
    )
    return core.get_account("tester@gmail.com")
