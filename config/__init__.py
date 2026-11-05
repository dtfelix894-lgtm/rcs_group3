"""配置模块入口：支持 `import config` 直接访问所有配置项。"""
from .config import (
    COLD_START_USERNAME,
    COLD_START_USER_ID,
    DATA_DIR,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    PRODUCTS_CSV,
    RATINGS_CSV,
    ROOT_DIR,
    STATIC_DIR,
    get_db_params,
)

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
