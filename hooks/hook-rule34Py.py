# PyInstaller hook for rule34Py
# Collects distribution metadata required by importlib.metadata.version('rule34Py')
from PyInstaller.utils.hooks import copy_metadata

try:
    datas = copy_metadata('rule34Py')
except Exception:
    datas = []
