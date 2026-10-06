"""Imports the overlay providers against the vendored upstream tree.

server/overlay precedes the vendored main/xiaozhi-server on sys.path; upstream
`core/` and `config/` are namespace packages, so the two trees merge as in the
image. Modules that load data/.config.yaml or the connection stack are stubbed.
Tests run from a scratch directory with bytecode writing off.
"""
import ctypes.util
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

sys.dont_write_bytecode = True

SERVER_DIR = Path(__file__).resolve().parent.parent
OVERLAY_DIR = SERVER_DIR / "overlay"
VENDOR_DIR = (
    Path(os.environ.get("WEIXUE_VENDOR", Path.home() / "Projects" / "weixue-vendor"))
    / "xiaozhi-esp32-server"
    / "main"
    / "xiaozhi-server"
)
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"
CREDENTIALS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path.home() / "Projects" / "ha-agent" / ".secrets" / "google-cloud-ha.json"),
)
TEST_CONFIG = {
    "selected_module": {"TTS": "GoogleTTS"},
    "TTS": {
        "GoogleTTS": {
            "type": "google_tts",
            "output_dir": "tmp/",
            "speaker": "Charon",
            "default_language": "en",
            "speaking_rate": 1.0,
        }
    },
}

if not (VENDOR_DIR / "core" / "providers" / "asr" / "base.py").exists():
    pytest.exit(f"vendored upstream not found at {VENDOR_DIR} (set WEIXUE_VENDOR)", returncode=4)

for path in (str(VENDOR_DIR), str(OVERLAY_DIR)):
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

# opuslib_next calls ctypes.util.find_library("opus") at import; this exposes <venv>/lib/libopus.*.
_VENV_LIB = Path(sys.prefix) / "lib"
if ctypes.util.find_library("opus") is None and any(_VENV_LIB.glob("libopus.*")):
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(
        p for p in (str(_VENV_LIB), os.environ.get("DYLD_FALLBACK_LIBRARY_PATH")) if p
    )

class StubLogger:
    def __init__(self):
        self.records = []

    def bind(self, **_kwargs):
        return self

    def __getattr__(self, level):
        def log(message, *_args, **_kwargs):
            self.records.append((level, str(message)))

        return log


LOGGER = StubLogger()


def _noop(*_args, **_kwargs):
    return None


def _stub_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


_stub_module("config.logger", setup_logging=lambda *_a, **_k: LOGGER)
_stub_module("core.handle.receiveAudioHandle", startToChat=_noop, handleAudioMessage=_noop)
_stub_module(
    "core.handle.reportHandle",
    enqueue_asr_report=_noop,
    enqueue_tts_report=_noop,
    enqueue_tool_report=_noop,
)


@pytest.fixture(scope="session", autouse=True)
def provider_workdir():
    """Working directory whose core/ is the overlay's, as upstream create_instance expects."""
    previous = os.getcwd()
    workdir = Path(tempfile.mkdtemp(prefix="weixue-tests-"))
    (workdir / "core").symlink_to(OVERLAY_DIR / "core")
    os.chdir(workdir)
    yield workdir
    os.chdir(previous)


@pytest.fixture(autouse=True)
def clear_log_records():
    LOGGER.records.clear()
    yield


@pytest.fixture
def log_records():
    return LOGGER.records
