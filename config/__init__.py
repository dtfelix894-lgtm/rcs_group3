"""配置模块入口：支持 `import config` 直接访问所有配置项。"""
import importlib.util
import os

_dir = os.path.dirname(__file__)
_local = os.path.join(_dir, "config.py")
_path = _local if os.path.isfile(_local) else os.path.join(_dir, "config.example.py")

_spec = importlib.util.spec_from_file_location("config._runtime", _path)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)

COLD_START_USERNAME = _mod.COLD_START_USERNAME
COLD_START_USER_ID = _mod.COLD_START_USER_ID
DATA_DIR = _mod.DATA_DIR
DB_HOST = _mod.DB_HOST
DB_NAME = _mod.DB_NAME
DB_PASSWORD = _mod.DB_PASSWORD
DB_PORT = _mod.DB_PORT
DB_USER = _mod.DB_USER
PRODUCTS_CSV = _mod.PRODUCTS_CSV
RATINGS_CSV = _mod.RATINGS_CSV
ROOT_DIR = _mod.ROOT_DIR
STATIC_DIR = _mod.STATIC_DIR
get_db_params = _mod.get_db_params

__all__ = [
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME",
    "ROOT_DIR",
    "DATA_DIR",
    "STATIC_DIR",
    "PRODUCTS_CSV",
    "RATINGS_CSV",
    "COLD_START_USER_ID",
    "COLD_START_USERNAME",
    "get_db_params",
]
