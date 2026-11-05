"""
配置模板：复制为 config/config.py 后填写真实值（config/config.py 已被 .gitignore 忽略）。
所有配置项均可通过同名环境变量覆盖。
Vercel + Neon 集成时会注入 POSTGRES_URL / PGHOST 等，见 config/connection.py。
"""
import os

from config.connection import get_db_params, resolve_db_settings

_settings = resolve_db_settings()
DB_HOST = _settings["DB_HOST"]
DB_PORT = _settings["DB_PORT"]
DB_USER = _settings["DB_USER"]
DB_PASSWORD = _settings["DB_PASSWORD"]
DB_NAME = _settings["DB_NAME"]

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
STATIC_DIR = os.path.join(ROOT_DIR, "static")

PRODUCTS_CSV = os.getenv("PRODUCTS_CSV", os.path.join(DATA_DIR, "clean_products.csv"))
RATINGS_CSV = os.getenv("RATINGS_CSV", os.path.join(DATA_DIR, "clean_ratings.csv"))

COLD_START_USER_ID = "cold_start_user"
COLD_START_USERNAME = "新注册用户(冷启动测试)"
