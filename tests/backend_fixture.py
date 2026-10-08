"""Load backend with import-time logging isolated from the user's profile."""
import importlib
import atexit
import os
import tempfile
from pathlib import Path
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parents[1] / 'build' / 'tests'
TEST_ROOT.mkdir(parents=True, exist_ok=True)
_profile = tempfile.TemporaryDirectory(prefix='luxedge-test-', dir=TEST_ROOT)
with mock.patch.dict(os.environ, {'APPDATA': _profile.name}):
    backend = importlib.import_module('ambilight_pc')


def cleanup():
    for handler in backend.log.handlers[:]:
        handler.close()
        backend.log.removeHandler(handler)
    _profile.cleanup()


atexit.register(cleanup)
