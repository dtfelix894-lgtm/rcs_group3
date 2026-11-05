"""数据库连接参数（本地 DB_* 与 Vercel/Neon 注入变量统一解析）。"""
import os
from urllib.parse import urlparse, urlunparse


def _first_env(*names: str, default=None):
    for name in names:
        val = os.getenv(name)
        if val not in (None, ""):
            return val
    return default


def _dsn_with_dbname(dsn: str, dbname: str) -> str:
    parsed = urlparse(dsn)
    return urlunparse(parsed._replace(path="/" + dbname.lstrip("/")))


def resolve_db_settings() -> dict:
    return {
        "DB_HOST": _first_env("PGHOST", "POSTGRES_HOST", "DB_HOST", default="localhost"),
        "DB_PORT": int(_first_env("PGPORT", "DB_PORT", default="5432")),
        "DB_USER": _first_env("PGUSER", "POSTGRES_USER", "DB_USER", default="postgres"),
        "DB_PASSWORD": _first_env(
            "PGPASSWORD", "POSTGRES_PASSWORD", "DB_PASSWORD", default="<your_password>"
        ),
        "DB_NAME": _first_env("PGDATABASE", "POSTGRES_DATABASE", "DB_NAME", default="recsys_db"),
    }


def get_db_params(dbname: str = None) -> dict:
    """返回 psycopg2 / ThreadedConnectionPool 可用的连接参数。"""
    dsn = _first_env("POSTGRES_URL", "DATABASE_URL", "POSTGRES_URL_NON_POOLING")
    if dsn:
        target = dbname or _first_env("PGDATABASE", "POSTGRES_DATABASE", "DB_NAME")
        if target:
            current = urlparse(dsn).path.lstrip("/")
            if target != current:
                dsn = _dsn_with_dbname(dsn, target)
        return {"dsn": dsn}

    host = _first_env("PGHOST", "POSTGRES_HOST", "DB_HOST", default="localhost")
    target_db = dbname or _first_env("PGDATABASE", "POSTGRES_DATABASE", "DB_NAME", default="recsys_db")
    params = {
        "host": host,
        "port": int(_first_env("PGPORT", "DB_PORT", default="5432")),
        "user": _first_env("PGUSER", "POSTGRES_USER", "DB_USER", default="postgres"),
        "password": _first_env("PGPASSWORD", "POSTGRES_PASSWORD", "DB_PASSWORD", default=""),
        "dbname": target_db,
    }
    if "neon.tech" in host or _first_env("NEON_PROJECT_ID"):
        params["sslmode"] = _first_env("DB_SSLMODE", default="require")
    return params
