"""
配置模板：复制为 config/config.py 后填写真实值（config/config.py 已被 .gitignore 忽略）。
所有配置项均可通过同名环境变量覆盖。
"""
import os

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "<your_password>")
DB_NAME = os.getenv("DB_NAME", "recsys_db")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
STATIC_DIR = os.path.join(ROOT_DIR, "static")

PRODUCTS_CSV = os.getenv("PRODUCTS_CSV", os.path.join(DATA_DIR, "clean_products.csv"))
RATINGS_CSV = os.getenv("RATINGS_CSV", os.path.join(DATA_DIR, "clean_ratings.csv"))

COLD_START_USER_ID = "cold_start_user"
COLD_START_USERNAME = "新注册用户(冷启动测试)"


def get_db_params(dbname: str = None) -> dict:
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "dbname": dbname or DB_NAME,
    }
