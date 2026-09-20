import importlib.util
import pathlib
import sys

import pytest
from fakes import start_stub

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "requesty-coder-sync.py"


@pytest.fixture(scope="session")
def sync():
    spec = importlib.util.spec_from_file_location("requesty_coder_sync", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["requesty_coder_sync"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def relaxed_guard(monkeypatch, sync):
    """Small test catalogs would trip the truncated-catalog guard."""
    monkeypatch.setattr(sync, "MIN_ELIGIBLE_MODELS", 1)


@pytest.fixture
def stub():
    server = start_stub()
    yield server
    server.shutdown()
    server.server_close()
