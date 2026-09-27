# PyInstaller runtime hook to safeguard importlib.metadata in frozen bundles.
# This prevents PackageNotFoundError when libraries (e.g. rule34Py) call
# importlib.metadata.version(__package__) upon initialization.

try:
    import importlib.metadata

    _orig_version = importlib.metadata.version
    _orig_distribution = importlib.metadata.distribution

    class _SafeDistribution:
        def __init__(self, name="rule34Py", ver="4.2.0"):
            self.version = ver
            self.metadata = {"Version": ver, "Name": name}

    def _safe_version(distribution_name):
        try:
            return _orig_version(distribution_name)
        except Exception:
            if distribution_name and str(distribution_name).lower() in (
                "rule34py", "pinterest-dl", "pinterest_dl", "gallery-dl", "gallery_dl"
            ):
                return "4.2.0"
            raise

    def _safe_distribution(distribution_name):
        try:
            return _orig_distribution(distribution_name)
        except Exception:
            if distribution_name and str(distribution_name).lower() in (
                "rule34py", "pinterest-dl", "pinterest_dl", "gallery-dl", "gallery_dl"
            ):
                return _SafeDistribution(str(distribution_name))
            raise

    importlib.metadata.version = _safe_version
    importlib.metadata.distribution = _safe_distribution
except Exception:
    pass
