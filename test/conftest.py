import importlib
import os
import pkgutil
import tempfile

import pytest

import core.shared


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
            mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
            for name, value in list(vars(mod).items()):
                if (name.endswith("_FILE") and isinstance(value, str)
                        and f"{os.sep}database{os.sep}" in value):
                    setattr(mod, name, os.path.join(tmp, os.path.basename(value)))

    import core.database as database
    database.SettingsManager._env_path = lambda self: os.path.join(tmp, ".env")
    yield tmp
