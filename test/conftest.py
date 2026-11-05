import importlib
import os
import pkgutil
import tempfile

import pytest

import core.shared

# Snapshot the process env BEFORE any test module is imported (collection
# order makes some module import Rems_Dl, whose load_dotenv() spills .env
# values into os.environ and breaks test_settings' env-derived defaults).
# Every test starts and ends with this pristine env.
_ENV_SNAPSHOT = dict(os.environ)


@pytest.fixture(autouse=True)
def _restore_env():
    yield
    # diff-restore: clear() would also drop pytest's own runtime env vars
    for k in [k for k in os.environ
              if k not in _ENV_SNAPSHOT and not k.startswith("PYTEST")]:
        os.environ.pop(k, None)
    os.environ.update(_ENV_SNAPSHOT)


@pytest.fixture(autouse=True)
def _reset_rate_pace():
    """Tests share core.shared's process-wide rate budget — start each one
    from a clean slot so pacing math never leaks between tests."""
    core.shared._pace_next = 0.0
    yield
    core.shared._pace_next = 0.0


@pytest.fixture(scope="session", autouse=True)
def _isolate_data_files():
    """Redirect every database/* file constant (core + workers) and
    SettingsManager's .env writer to a session temp dir.

    Several tests drive the real DatabaseManager/shared caches and Flask
    routes; unisolated they write junk rows into the app's live
    history/gallery — and the atexit flushes, which run AFTER per-test
    patches have been restored, dump their seeded caches over the real
    files at every pytest exit. Deliberately never un-patched: the pytest
    process dies right after teardown and the atexit handlers must still
    point at the temp dir."""
    import core
    import workers

    tmp = tempfile.mkdtemp(prefix="rems_dl_test_")
    for pkg in (core, workers):
        for info in pkgutil.iter_modules(pkg.__path__):
            try:
                mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
            except (ImportError, ModuleNotFoundError):
                continue
            for name, value in list(vars(mod).items()):
                if (name.endswith("_FILE") and isinstance(value, str)
                        and f"{os.sep}database{os.sep}" in value):
                    setattr(mod, name, os.path.join(tmp, os.path.basename(value)))

    from core import database
    database.SettingsManager._env_path = lambda self: os.path.join(tmp, ".env")

    # A store singleton built while collecting tests captured the REAL paths
    # in its constructor, so the redirect above never reached it and every
    # test that touched a store wrote into the app's live data. Drop it so
    # the first use rebuilds against the temp dir.
    from core import dedup_store, gallery_store
    for mod, attr in ((gallery_store, "_store"), (dedup_store, "_store_instance")):
        s = getattr(mod, attr, None)
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
            setattr(mod, attr, None)
    yield tmp


@pytest.fixture
def bypass_access_guard(monkeypatch):
    """Opt-in bypass for legacy route tests that target Flask handlers.

    Security tests deliberately do not request this fixture, so the WSGI
    boundary remains covered by the real AccessGuard implementation.
    """
    from core.web_security import AccessGuard

    def _permissive(ga, environ, start_response):
        return ga.application(environ, start_response)

    monkeypatch.setattr(AccessGuard, "__call__", _permissive)
    yield
