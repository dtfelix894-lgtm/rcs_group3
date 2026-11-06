"""
阶段三：FastAPI 后端服务

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
访问：http://127.0.0.1:8000/

接口一览（注意：固定路径必须注册在参数化路径之前）
  GET    /                                   前端首页 static/index.html
  GET    /api/users/sample                   分层预设体验账号列表（冷启动 + 高/中/低活跃用户）
  GET    /api/users/browse                   全库用户分页浏览 / 关键词检索（支持 offset 懒加载）
  GET    /api/users/random                   从全库 41 万真实用户中随机抽取体验账号（支持按活跃度分层）
  GET    /api/users/search                   按 user_id 或昵称搜索全库任意用户
  GET    /api/categories                     商品数量前 15 的子分类
  GET    /api/products/hot                   贝叶斯热门榜单
  GET    /api/products/search                关键词 + 分类 + 排序 + 分页搜索（含总命中数 total）
  GET    /api/products/{item_id}             商品详情 + 当前用户评分/收藏状态
  GET    /api/recommend/similar/{item_id}    看了又看（混合相似度 + 同类热门兜底）
  GET    /api/recommend/user/{user_id}       实时个性化推荐 + 可解释理由（含冷启动分支与同名规格去重）
  POST   /api/interactions                   打分 / 收藏 / 取消收藏
  GET    /api/users/{user_id}/history        用户足迹与收藏
  DELETE /api/users/{user_id}/interactions   重置测试账号（仅限冷启动测试账号）
  GET    /api/dashboard/overview             数据全景看板（四表规模、分类、稀疏度、评分、相似度质量，进程内缓存）
  GET    /api/dashboard/similarity-sample    单商品离线 Top-K 相似邻居样例
  GET    /api/metrics                        推荐系统离线评测与消融实验指标（Leave-Last-One-Out）
"""
import json
import os
import random
import sys
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from pydantic import BaseModel, Field

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config

STATIC_DIR = config.STATIC_DIR
EVAL_JSON_PATH = os.path.join(config.DATA_DIR, "eval_results.json")

POSITIVE_THRESHOLD = 3.5     # 正向反馈阈值
HISTORY_WINDOW = 15          # 个性化推荐参考的最近正向行为条数
TIME_DECAY = 0.95            # 时间衰减系数（最新一条为 1.0，依次乘 0.95）
RESETTABLE_USERS = {config.COLD_START_USER_ID}  # 允许被重置的测试账号

REASON_COLD_START = "🔥 全站高分热销精选（冷启动推荐）"
REASON_FILL = "🔥 全站高分热销精选（为您补充）"

# 列表接口使用的精简字段（不含长描述，减小响应体）
LIST_COLUMNS = """
    p.item_id, p.title, p.price::float8 AS price, p.avg_rating::float8 AS avg_rating,
    p.rating_num, p.category, p.sub_category, p.image_url, p.hot_score::float8 AS hot_score
"""

app = FastAPI(title="智选电商 · 个性化推荐系统", version="1.1.0")
_pool: Optional[ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


# ----------------------------------------------------------------------------
# 基础设施：连接池 / 类型转换
# ----------------------------------------------------------------------------
def _ensure_pool() -> ThreadedConnectionPool:
    """按需创建连接池，避免 Vercel 冷启动时因数据库不可达导致整站 500。"""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadedConnectionPool(minconn=1, maxconn=10, **config.get_db_params())
    return _pool


@app.on_event("shutdown")
def _shutdown():
    if _pool:
        _pool.closeall()


@contextmanager
def get_cursor(commit: bool = False):
    pool = _ensure_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()  # 结束只读事务，避免连接处于 idle in transaction
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def to_native(obj):
    """递归将 Decimal 转为 float，保证 JSON 可序列化（SQL 中已尽量 ::float8，此处兜底）。"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_native(v) for v in obj]
    return obj


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def short_title(title: str, n: int = 22) -> str:
    return title if len(title) <= n else title[:n] + "..."


def now_ms() -> int:
    # 与历史数据保持一致：毫秒级时间戳
    return int(time.time() * 1000)


def user_tier_label(n: int, is_cold_account: bool = False) -> str:
    if is_cold_account:
        return "🌱 新用户冷启动"
    if n >= 50:
        return "🔥 高活跃用户 (≥50条)"
    if n >= 10:
        return "✨ 中活跃用户 (10~49条)"
    return "🌿 低活跃长尾 (1~9条)"


def fetch_hot(cur, limit: int, exclude: Optional[set] = None) -> list:
    exclude = list(exclude or [])
    cur.execute(
        f"SELECT {LIST_COLUMNS} FROM products p "
        "WHERE NOT (p.item_id = ANY(%s)) ORDER BY p.hot_score DESC LIMIT %s",
        (exclude, limit),
    )
    return cur.fetchall()


def fetch_products_by_ids(cur, ids: list) -> dict:
    if not ids:
        return {}
    cur.execute(f"SELECT {LIST_COLUMNS} FROM products p WHERE p.item_id = ANY(%s)", (list(ids),))
    return {r["item_id"]: r for r in cur.fetchall()}


def user_exists(cur, user_id: str) -> bool:
    cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
    return cur.fetchone() is not None


def interaction_count(cur, user_id: str) -> int:
    cur.execute("SELECT COUNT(*) AS n FROM interactions WHERE user_id = %s", (user_id,))
    return cur.fetchone()["n"]


# ----------------------------------------------------------------------------
# 首页
# ----------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# ----------------------------------------------------------------------------
# 用户（含分层示例、随机抽样、全库搜索）
# ----------------------------------------------------------------------------
@app.get("/api/users/sample")
def users_sample():
    """返回覆盖 4 个活跃度层级的代表性演示账号（共 16 个），支撑不同稀疏度下的推荐对比。"""
    with get_cursor() as cur:
        cur.execute(
            "SELECT u.user_id, u.username, "
            "(SELECT COUNT(*) FROM interactions i WHERE i.user_id = u.user_id) AS interaction_count "
            "FROM users u WHERE u.user_id = %s",
            (config.COLD_START_USER_ID,),
        )
        cold = cur.fetchone()

        # 一次性按互动频次分层抽样高/中/低活跃代表用户
        cur.execute(
            """
            WITH counts AS (
                SELECT user_id, COUNT(*) AS n
                FROM interactions
                WHERE user_id <> %s
                GROUP BY user_id
            ),
            high_tier AS (
                SELECT user_id, n FROM counts WHERE n >= 50 ORDER BY n DESC LIMIT 5
            ),
            mid_tier AS (
                SELECT user_id, n FROM counts WHERE n BETWEEN 10 AND 49 ORDER BY n DESC, user_id LIMIT 5
            ),
            low_tier AS (
                SELECT user_id, n FROM counts WHERE n BETWEEN 2 AND 9 ORDER BY n DESC, user_id LIMIT 5
            ),
            combined AS (
                SELECT * FROM high_tier
                UNION ALL
                SELECT * FROM mid_tier
                UNION ALL
                SELECT * FROM low_tier
            )
            SELECT c.user_id, u.username, c.n AS interaction_count
            FROM combined c JOIN users u USING (user_id)
            ORDER BY c.n DESC, c.user_id
            """,
            (config.COLD_START_USER_ID,),
        )
        tiered_rows = cur.fetchall()

    result = []
    if cold:
        result.append({
            **cold,
            "is_cold_start_account": True,
            "group": user_tier_label(cold["interaction_count"], True),
        })
    for r in tiered_rows:
        result.append({
            **r,
            "is_cold_start_account": False,
            "group": user_tier_label(r["interaction_count"], False),
        })
    return to_native(result)


@app.get("/api/users/random")
def users_random(tier: Literal["any", "high", "medium", "low"] = Query("any")):
    """从全库 41.2 万真实用户中按指定活跃度随机抽取 1 名用户。"""
    with get_cursor() as cur:
        if tier == "high":
            having_sql = "HAVING COUNT(*) >= 30"
        elif tier == "medium":
            having_sql = "HAVING COUNT(*) BETWEEN 8 AND 29"
        elif tier == "low":
            having_sql = "HAVING COUNT(*) BETWEEN 2 AND 7"
        else:
            having_sql = "HAVING COUNT(*) >= 2"

        cur.execute(
            f"""
            WITH pool AS (
                SELECT user_id, COUNT(*) AS n
                FROM interactions
                WHERE user_id <> %s
                GROUP BY user_id
                {having_sql}
                LIMIT 500
            )
            SELECT p.user_id, u.username, p.n AS interaction_count
            FROM pool p JOIN users u USING (user_id)
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (config.COLD_START_USER_ID,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "未找到符合条件的随机用户")

    return to_native({
        **row,
        "is_cold_start_account": False,
        "group": user_tier_label(row["interaction_count"], False),
    })


def _user_list_item(row: dict) -> dict:
    return {
        **row,
        "is_cold_start_account": row["user_id"] == config.COLD_START_USER_ID,
        "group": user_tier_label(row["interaction_count"], row["user_id"] == config.COLD_START_USER_ID),
    }


@app.get("/api/users/browse")
def users_browse(
    q: str = Query("", max_length=64),
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
):
    """全库用户分页浏览；带 q 时按 user_id / username 模糊检索。"""
    q = q.strip()
    with get_cursor() as cur:
        if q:
            pattern = f"%{escape_like(q)}%"
            cur.execute(
                """
                SELECT COUNT(*) AS total FROM users u
                WHERE u.user_id ILIKE %s OR u.username ILIKE %s
                """,
                (pattern, pattern),
            )
            total = cur.fetchone()["total"]
            cur.execute(
                """
                SELECT u.user_id, u.username,
                       (SELECT COUNT(*) FROM interactions i WHERE i.user_id = u.user_id) AS interaction_count
                FROM users u
                WHERE u.user_id ILIKE %s OR u.username ILIKE %s
                ORDER BY interaction_count DESC, u.user_id
                LIMIT %s OFFSET %s
                """,
                (pattern, pattern, limit, offset),
            )
        else:
            cur.execute(
                """
                SELECT COUNT(*) AS total FROM (
                    SELECT user_id FROM interactions
                    WHERE user_id <> %s
                    GROUP BY user_id
                    HAVING COUNT(*) >= 2
                ) t
                """,
                (config.COLD_START_USER_ID,),
            )
            total = cur.fetchone()["total"]
            cur.execute(
                """
                SELECT u.user_id, u.username, stats.n AS interaction_count
                FROM (
                    SELECT user_id, COUNT(*) AS n
                    FROM interactions
                    WHERE user_id <> %s
                    GROUP BY user_id
                    HAVING COUNT(*) >= 2
                ) stats
                JOIN users u ON u.user_id = stats.user_id
                ORDER BY stats.n DESC, u.user_id
                LIMIT %s OFFSET %s
                """,
                (config.COLD_START_USER_ID, limit, offset),
            )
        rows = cur.fetchall()

    items = [_user_list_item(r) for r in rows]
    return to_native({
        "q": q,
        "total": total,
        "offset": offset,
        "limit": limit,
        "count": len(items),
        "has_more": offset + len(items) < total,
        "items": items,
    })


@app.get("/api/users/search")
def users_search(
    q: str = Query("", max_length=64),
    limit: int = Query(15, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    """按 user_id 或 username 模糊检索全库任意用户（兼容旧接口；推荐用 /api/users/browse）。"""
    q = q.strip()
    if not q:
        return []
    return users_browse(q=q, offset=offset, limit=limit)["items"]


@app.get("/api/users/{user_id}/history")
def user_history(user_id: str, limit: int = Query(30, ge=1, le=100)):
    with get_cursor() as cur:
        if not user_exists(cur, user_id):
            raise HTTPException(404, "用户不存在")
        cur.execute(
            f"""
            SELECT {LIST_COLUMNS}, i.rating::float8 AS user_rating, i.action_type AS user_action,
                   i.is_fav, i.timestamp
            FROM interactions i JOIN products p ON p.item_id = i.item_id
            WHERE i.user_id = %s
            ORDER BY i.timestamp DESC LIMIT %s
            """,
            (user_id, limit),
        )
        rows = cur.fetchall()
        total = interaction_count(cur, user_id)
    return to_native({"user_id": user_id, "total": total, "items": rows})


@app.delete("/api/users/{user_id}/interactions")
def reset_user(user_id: str):
    if user_id not in RESETTABLE_USERS:
        raise HTTPException(403, "仅允许重置冷启动测试账号，真实用户的历史数据不可删除")
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM interactions WHERE user_id = %s", (user_id,))
        deleted = cur.rowcount
    return {"success": True, "user_id": user_id, "deleted": deleted, "interaction_count": 0}


# ----------------------------------------------------------------------------
# 分类与商品（固定路径必须写在 /api/products/{item_id} 之前）
# ----------------------------------------------------------------------------
@app.get("/api/categories")
def categories():
    with get_cursor() as cur:
        cur.execute(
            "SELECT sub_category AS name, COUNT(*) AS count FROM products "
            "WHERE sub_category IS NOT NULL AND sub_category <> '' "
            "GROUP BY sub_category ORDER BY count DESC LIMIT 15"
        )
        return to_native(cur.fetchall())


@app.get("/api/products/hot")
def products_hot(limit: int = Query(12, ge=1, le=100)):
    with get_cursor() as cur:
        return to_native(fetch_hot(cur, limit))


@app.get("/api/products/search")
def products_search(
    q: str = Query("", max_length=100),
    category: str = Query("", max_length=64),
    sort: Literal["default", "hot", "rating", "reviews", "price_asc", "price_desc"] = Query("default"),
    offset: int = Query(0, ge=0, le=5000),
    limit: int = Query(24, ge=1, le=100),
):
    q, category = q.strip(), category.strip()
    conds, params = [], []
    pattern = ""
    if q:
        pattern = f"%{escape_like(q)}%"
        conds.append("(p.title ILIKE %s OR p.description ILIKE %s)")
        params += [pattern, pattern]
    if category:
        conds.append("p.sub_category = %s")
        params.append(category)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    # 排序策略
    sort_Prefix = "(p.title ILIKE %s) DESC, " if (q and sort == "default") else ""
    if sort == "rating":
        order_core = "p.avg_rating DESC, p.rating_num DESC, p.hot_score DESC"
    elif sort == "reviews":
        order_core = "p.rating_num DESC, p.hot_score DESC"
    elif sort == "price_asc":
        order_core = "p.price ASC NULLS LAST, p.hot_score DESC"
    elif sort == "price_desc":
        order_core = "p.price DESC NULLS LAST, p.hot_score DESC"
    else:
        order_core = "p.hot_score DESC"

    order_sql = f"ORDER BY {sort_Prefix}{order_core}"
    query_params = params + ([pattern] if sort_Prefix else []) + [limit, offset]

    with get_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM products p {where}", params)
        total = cur.fetchone()["total"]
        cur.execute(
            f"SELECT {LIST_COLUMNS} FROM products p {where} {order_sql} LIMIT %s OFFSET %s",
            query_params,
        )
        items = cur.fetchall()
    return to_native({
        "q": q,
        "category": category,
        "sort": sort,
        "offset": offset,
        "limit": limit,
        "total": total,
        "count": len(items),
        "items": items,
    })


@app.get("/api/products/{item_id}")
def product_detail(item_id: str, user_id: Optional[str] = None):
    with get_cursor() as cur:
        cur.execute(
            f"SELECT {LIST_COLUMNS}, p.description FROM products p WHERE p.item_id = %s",
            (item_id,),
        )
        product = cur.fetchone()
        if not product:
            raise HTTPException(404, "商品不存在")
        product = dict(product, user_rating=None, user_action=None, user_is_fav=False)
        if user_id:
            cur.execute(
                "SELECT rating::float8 AS rating, action_type, is_fav FROM interactions "
                "WHERE user_id = %s AND item_id = %s",
                (user_id, item_id),
            )
            row = cur.fetchone()
            if row:
                product.update(user_rating=row["rating"], user_action=row["action_type"],
                               user_is_fav=row["is_fav"])
    return to_native(product)


# ----------------------------------------------------------------------------
# 推荐
# ----------------------------------------------------------------------------
@app.get("/api/recommend/similar/{item_id}")
def recommend_similar(item_id: str, limit: int = Query(6, ge=1, le=20)):
    with get_cursor() as cur:
        cur.execute("SELECT sub_category FROM products WHERE item_id = %s", (item_id,))
        base = cur.fetchone()
        if not base:
            raise HTTPException(404, "商品不存在")

        cur.execute("SELECT similar_items FROM item_similarity WHERE item_id = %s", (item_id,))
        row = cur.fetchone()
        sims = (row["similar_items"] if row else None) or []

        # 多取一些候选，防止个别商品已下架导致 JOIN 后不足
        cand = [s for s in sims if s.get("item_id") != item_id][: limit * 2]
        prods = fetch_products_by_ids(cur, [s["item_id"] for s in cand])
        items = []
        for s in cand:
            p = prods.get(s["item_id"])
            if p:
                items.append({**p, "score": float(s["score"]), "is_fallback": False})
            if len(items) >= limit:
                break

        # 保底：同子分类热门 -> 全站热门
        if len(items) < limit:
            exclude = [item_id] + [it["item_id"] for it in items]
            cur.execute(
                f"SELECT {LIST_COLUMNS} FROM products p "
                "WHERE p.sub_category = %s AND NOT (p.item_id = ANY(%s)) "
                "ORDER BY p.hot_score DESC LIMIT %s",
                (base["sub_category"], exclude, limit - len(items)),
            )
            for p in cur.fetchall():
                items.append({**p, "score": None, "is_fallback": True})
        if len(items) < limit:
            exclude = {item_id} | {it["item_id"] for it in items}
            for p in fetch_hot(cur, limit - len(items), exclude):
                items.append({**p, "score": None, "is_fallback": True})

    return to_native({"item_id": item_id, "items": items})


@app.get("/api/recommend/user/{user_id}")
def recommend_user(user_id: str, limit: int = Query(12, ge=1, le=50)):
    with get_cursor() as cur:
        if not user_exists(cur, user_id):
            raise HTTPException(404, "用户不存在")

        cur.execute(
            "SELECT item_id, rating::float8 AS rating, action_type, is_fav, timestamp "
            "FROM interactions WHERE user_id = %s ORDER BY timestamp DESC",
            (user_id,),
        )
        history = cur.fetchall()
        interacted = {h["item_id"] for h in history}
        positives = [h for h in history if h["rating"] >= POSITIVE_THRESHOLD or h["is_fav"]][:HISTORY_WINDOW]

        # ---------------- 冷启动分支 ----------------
        if not positives:
            items = [{**p, "reason": REASON_COLD_START, "rec_score": None}
                     for p in fetch_hot(cur, limit, interacted)]
            return to_native({"is_cold_start": True, "user_id": user_id,
                              "based_on_count": 0, "items": items})

        # ---------------- 个性化分支 ----------------
        cur.execute(
            "SELECT s.item_id, s.similar_items, p.title FROM item_similarity s "
            "JOIN products p ON p.item_id = s.item_id WHERE s.item_id = ANY(%s)",
            ([h["item_id"] for h in positives],),
        )
        sim_map = {r["item_id"]: r for r in cur.fetchall()}

        scores: dict = {}
        best: dict = {}  # 候选 j -> (最大贡献值, 历史商品 i 的交互记录, 历史商品标题)
        for idx, h in enumerate(positives):
            entry = sim_map.get(h["item_id"])
            if not entry:
                continue
            decay = TIME_DECAY ** idx
            weight = 5.0 if h["is_fav"] else h["rating"]  # 收藏视为 5 分偏好
            for s in entry["similar_items"]:
                j = s["item_id"]
                if j in interacted:
                    continue
                contrib = weight * float(s["score"]) * decay
                scores[j] = scores.get(j, 0.0) + contrib
                if j not in best or contrib > best[j][0]:
                    best[j] = (contrib, h, entry["title"])

        # 多取候选，剔除同名多规格重复 ASIN 并优先保留口碑良好 (avg_rating >= 3.5) 的商品
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[: limit * 4]
        prods = fetch_products_by_ids(cur, [j for j, _ in ranked])
        items = []
        seen_titles: set = set()
        deferred_low: list = []
        for j, sc in ranked:
            p = prods.get(j)
            if not p:
                continue
            norm_title = " ".join(str(p["title"]).lower().split())
            if norm_title in seen_titles:
                continue
            seen_titles.add(norm_title)
            _, h, src_title = best[j]
            if h["is_fav"]:
                reason = f"💡 因为您收藏了「{short_title(src_title)}」"
            else:
                reason = f"💡 因为您给「{short_title(src_title)}」打了 {h['rating']:g} 星"
            entry_item = {**p, "reason": reason, "rec_score": round(sc, 4),
                          "based_on_item_id": h["item_id"]}
            if p["avg_rating"] >= 3.5:
                items.append(entry_item)
            else:
                deferred_low.append(entry_item)
            if len(items) >= limit:
                break

        if len(items) < limit and deferred_low:
            items.extend(deferred_low[: limit - len(items)])

        # 不足 limit 时用热门补齐（排除已交互与已推荐）
        if len(items) < limit:
            exclude = interacted | {it["item_id"] for it in items}
            for p in fetch_hot(cur, limit - len(items), exclude):
                items.append({**p, "reason": REASON_FILL, "rec_score": None})

    return to_native({"is_cold_start": False, "user_id": user_id,
                      "based_on_count": len(positives), "items": items})


# ----------------------------------------------------------------------------
# 数据全景看板
# ----------------------------------------------------------------------------
DASHBOARD_TTL = 600  # 秒；全表聚合较重，结果在进程内缓存
_dashboard_cache: dict = {"at": 0.0, "data": None}
_dashboard_lock = threading.Lock()

ACTIVITY_BUCKETS = [
    ("none", "无行为", None, 0),
    ("one", "仅 1 条", 1, 1),
    ("low", "长尾 2~7 条", 2, 7),
    ("medium", "中活跃 8~29 条", 8, 29),
    ("high", "高活跃 ≥30 条", 30, None),
]


def _build_dashboard(cur) -> dict:
    cur.execute(
        """
        SELECT (SELECT COUNT(*) FROM products)        AS products,
               (SELECT COUNT(*) FROM users)           AS users,
               (SELECT COUNT(*) FROM interactions)    AS interactions,
               (SELECT COUNT(*) FROM item_similarity) AS item_similarity
        """
    )
    tables = cur.fetchone()

    cur.execute(
        """
        SELECT COUNT(*) FILTER (WHERE price IS NOT NULL)   AS with_price,
               COUNT(*) FILTER (WHERE rating_num > 0)      AS with_rating,
               AVG(avg_rating) FILTER (WHERE rating_num > 0)::float8 AS mean_rating,
               percentile_cont(0.8) WITHIN GROUP (ORDER BY rating_num)::float8 AS m_quantile,
               MAX(rating_num) AS max_rating_num
        FROM products
        """
    )
    product_stats = cur.fetchone()

    cur.execute(
        """
        SELECT COALESCE(sub_category, '未分类') AS name, COUNT(*) AS count,
               AVG(avg_rating) FILTER (WHERE rating_num > 0)::float8 AS avg_rating,
               SUM(rating_num) AS reviews,
               AVG(hot_score)::float8 AS avg_hot
        FROM products GROUP BY 1 ORDER BY 2 DESC
        """
    )
    categories = cur.fetchall()

    cur.execute(
        """
        WITH c AS (SELECT user_id, COUNT(*) AS n FROM interactions GROUP BY user_id)
        SELECT COUNT(*) AS active_users,
               COUNT(*) FILTER (WHERE n = 1)                AS one,
               COUNT(*) FILTER (WHERE n BETWEEN 2 AND 7)    AS low,
               COUNT(*) FILTER (WHERE n BETWEEN 8 AND 29)   AS medium,
               COUNT(*) FILTER (WHERE n >= 30)              AS high,
               MAX(n) AS max_n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY n)::float8 AS median_n
        FROM c
        """
    )
    act = cur.fetchone()
    act_counts = {**act, "none": tables["users"] - act["active_users"]}
    activity = [
        {"key": key, "label": label, "min": lo, "max": hi, "count": act_counts[key]}
        for key, label, lo, hi in ACTIVITY_BUCKETS
    ]

    cur.execute(
        """
        SELECT LEAST(GREATEST(ROUND(rating)::int, 1), 5) AS star, COUNT(*) AS count
        FROM interactions GROUP BY 1 ORDER BY 1
        """
    )
    ratings = cur.fetchall()
    cur.execute(
        """
        SELECT COUNT(*) FILTER (WHERE rating >= %s) AS positive,
               COUNT(*) FILTER (WHERE is_fav)        AS favorites,
               MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts
        FROM interactions
        """,
        (POSITIVE_THRESHOLD,),
    )
    inter_stats = cur.fetchone()

    cur.execute(
        """
        SELECT EXTRACT(YEAR FROM to_timestamp(timestamp / 1000.0))::int AS year, COUNT(*) AS count
        FROM interactions GROUP BY 1 ORDER BY 1
        """
    )
    timeline = cur.fetchall()

    # 邻居质量在约 2% 的确定性抽样上估计，避免对 200 万条邻居全量展开
    cur.execute(
        """
        WITH s AS (
            SELECT item_id, similar_items FROM item_similarity TABLESAMPLE SYSTEM (2) REPEATABLE (42)
        ), e AS (
            SELECT s.item_id, (x->>'score')::float8 AS score, x->>'item_id' AS nid
            FROM s CROSS JOIN LATERAL jsonb_array_elements(s.similar_items) AS x
        )
        SELECT COUNT(DISTINCT e.item_id) AS sampled_items,
               COUNT(*) AS sampled_pairs,
               AVG(e.score) AS avg_score,
               AVG((p1.sub_category = p2.sub_category)::int)::float8 AS same_category_ratio,
               COUNT(*) FILTER (WHERE e.score < 0.2) AS b0,
               COUNT(*) FILTER (WHERE e.score >= 0.2 AND e.score < 0.4) AS b1,
               COUNT(*) FILTER (WHERE e.score >= 0.4 AND e.score < 0.6) AS b2,
               COUNT(*) FILTER (WHERE e.score >= 0.6 AND e.score < 0.8) AS b3,
               COUNT(*) FILTER (WHERE e.score >= 0.8) AS b4
        FROM e
        JOIN products p1 ON p1.item_id = e.item_id
        JOIN products p2 ON p2.item_id = e.nid
        """
    )
    sim = cur.fetchone()
    sampled_items = sim["sampled_items"] or 0
    score_buckets = [
        {"label": label, "count": sim[k] or 0}
        for k, label in (("b0", "0~0.2"), ("b1", "0.2~0.4"), ("b2", "0.4~0.6"), ("b3", "0.6~0.8"), ("b4", "≥0.8"))
    ]

    hot_top = fetch_hot(cur, 5)

    n_users, n_items, n_inter = tables["users"], tables["products"], tables["interactions"]
    return to_native({
        "generated_at": now_ms(),
        "tables": [
            {"name": "products", "label": "商品", "rows": n_items, "desc": "清洗后的美妆商品 · 含派生子分类与贝叶斯 hot_score"},
            {"name": "users", "label": "用户", "rows": n_users, "desc": "真实评论用户 + 冷启动测试账号"},
            {"name": "interactions", "label": "交互", "rows": n_inter, "desc": "评分 / 收藏行为 · 毫秒级时间戳"},
            {"name": "item_similarity", "label": "相似度", "rows": tables["item_similarity"], "desc": "离线预计算 Top-20 混合相似邻居 (JSONB)"},
        ],
        "sparsity": {
            "density": n_inter / max(n_users * n_items, 1),
            "avg_per_user": n_inter / max(n_users, 1),
            "avg_per_active_user": n_inter / max(act["active_users"], 1),
            "median_per_active_user": act["median_n"],
            "max_per_user": act["max_n"],
            "active_users": act["active_users"],
            "single_ratio": act["one"] / max(act["active_users"], 1),
        },
        "activity": activity,
        "products": {
            "with_price": product_stats["with_price"],
            "price_ratio": product_stats["with_price"] / max(n_items, 1),
            "with_rating": product_stats["with_rating"],
            "max_rating_num": product_stats["max_rating_num"],
        },
        "bayes": {
            "C": product_stats["mean_rating"],
            "m": product_stats["m_quantile"] or 5.0,
        },
        "categories": categories,
        "ratings": ratings,
        "interactions": {
            "positive": inter_stats["positive"],
            "positive_ratio": inter_stats["positive"] / max(n_inter, 1),
            "favorites": inter_stats["favorites"],
            "first_ts": inter_stats["first_ts"],
            "last_ts": inter_stats["last_ts"],
            "positive_threshold": POSITIVE_THRESHOLD,
        },
        "timeline": timeline,
        "similarity": {
            "coverage": tables["item_similarity"] / max(n_items, 1),
            "avg_neighbors": (sim["sampled_pairs"] or 0) / max(sampled_items, 1),
            "avg_score": sim["avg_score"],
            "same_category_ratio": sim["same_category_ratio"],
            "sampled_items": sampled_items,
            "score_buckets": score_buckets,
            "weights": {"cf": 0.6, "content": 0.4},
        },
        "hot_top": hot_top,
    })


@app.get("/api/dashboard/overview")
def dashboard_overview(refresh: bool = False):
    """数据全景：四表规模、子分类分布、用户稀疏度、评分分布、时间跨度与相似度质量。"""
    with _dashboard_lock:
        cached = _dashboard_cache["data"]
        if cached and not refresh and time.time() - _dashboard_cache["at"] < DASHBOARD_TTL:
            return cached
        t0 = time.time()
        with get_cursor() as cur:
            data = _build_dashboard(cur)
        data["query_ms"] = int((time.time() - t0) * 1000)
        _dashboard_cache.update(at=time.time(), data=data)
        return data


@app.get("/api/dashboard/similarity-sample")
def dashboard_similarity_sample(item_id: Optional[str] = None, limit: int = Query(8, ge=1, le=20)):
    """展示单个商品在 item_similarity 中的离线 Top-K 邻居；不传 item_id 时从热门且有交互的商品中随机抽取。"""
    with get_cursor() as cur:
        if not item_id:
            cur.execute(
                """
                SELECT p.item_id FROM products p
                JOIN item_similarity s ON s.item_id = p.item_id
                WHERE p.rating_num >= 20
                ORDER BY p.hot_score DESC LIMIT 200
                """
            )
            pool = [r["item_id"] for r in cur.fetchall()]
            if not pool:
                raise HTTPException(404, "暂无可展示的相似度样例")
            item_id = random.choice(pool)

        base = fetch_products_by_ids(cur, [item_id]).get(item_id)
        if not base:
            raise HTTPException(404, "商品不存在")
        cur.execute("SELECT similar_items FROM item_similarity WHERE item_id = %s", (item_id,))
        row = cur.fetchone()
        sims = [s for s in ((row["similar_items"] if row else None) or []) if s.get("item_id") != item_id]
        prods = fetch_products_by_ids(cur, [s["item_id"] for s in sims])
        neighbors = []
        for rank, s in enumerate(sims, 1):
            p = prods.get(s["item_id"])
            if p:
                neighbors.append({
                    **p,
                    "rank": rank,
                    "score": float(s["score"]),
                    "same_category": p["sub_category"] == base["sub_category"],
                })
        cur.execute("SELECT COUNT(*) AS n FROM interactions WHERE item_id = %s", (item_id,))
        n_inter = cur.fetchone()["n"]

    return to_native({
        "base": {**base, "interaction_count": n_inter},
        "total_neighbors": len(neighbors),
        "same_category_count": sum(1 for n in neighbors if n["same_category"]),
        "neighbors": neighbors[:limit],
    })


# ----------------------------------------------------------------------------
# 离线评测指标接口
# ----------------------------------------------------------------------------
@app.get("/api/metrics")
def get_metrics():
    """返回 Leave-Last-One-Out 离线消融评测实验数据（优先读取 JSON 缓存，兜底查询 eval_results 表）。"""
    if os.path.isfile(EVAL_JSON_PATH):
        with open(EVAL_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT algorithm, k, label, description,
                   hit_rate::float8 AS hit_rate, recall::float8 AS recall,
                   precision::float8 AS precision, ndcg::float8 AS ndcg,
                   mrr::float8 AS mrr, coverage::float8 AS coverage,
                   unique_items, novelty::float8 AS novelty,
                   hit_rate_high::float8 AS hit_rate_high, ndcg_high::float8 AS ndcg_high,
                   hit_rate_low::float8 AS hit_rate_low, ndcg_low::float8 AS ndcg_low
            FROM eval_results
            ORDER BY k, CASE algorithm
                WHEN 'Random' THEN 1
                WHEN 'Popular' THEN 2
                WHEN 'Content' THEN 3
                WHEN 'ItemCF' THEN 4
                WHEN 'Hybrid' THEN 5
                ELSE 6 END
            """
        )
        rows = cur.fetchall()
    return to_native({
        "evaluated_at": None,
        "protocol": "Leave-Last-One-Out (LLOO, Zero Data Leakage)",
        "eval_users": 2000,
        "k_values": [5, 10, 20],
        "metrics": rows,
    })


# ----------------------------------------------------------------------------
# 交互反馈闭环
# ----------------------------------------------------------------------------
class InteractionIn(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    item_id: str = Field(..., min_length=1, max_length=32)
    rating: Optional[float] = Field(None, ge=1, le=5)
    action_type: Literal["rate", "fav", "unfav"] = "rate"


@app.post("/api/interactions")
def create_interaction(body: InteractionIn):
    ts = now_ms()
    with get_cursor(commit=True) as cur:
        if not user_exists(cur, body.user_id):
            raise HTTPException(404, "用户不存在")
        cur.execute("SELECT 1 FROM products WHERE item_id = %s", (body.item_id,))
        if not cur.fetchone():
            raise HTTPException(404, "商品不存在")

        if body.action_type == "rate":
            if body.rating is None:
                raise HTTPException(422, "打分操作必须提供 rating (1~5)")
            rating = round(body.rating * 2) / 2  # 规整到 0.5 精度
            # 打分只更新评分，不影响收藏状态
            cur.execute(
                """
                INSERT INTO interactions (user_id, item_id, rating, action_type, is_fav, timestamp)
                VALUES (%s, %s, %s, 'rate', FALSE, %s)
                ON CONFLICT (user_id, item_id) DO UPDATE
                SET rating = EXCLUDED.rating, action_type = EXCLUDED.action_type,
                    timestamp = EXCLUDED.timestamp
                RETURNING rating::float8 AS rating, is_fav, action_type
                """,
                (body.user_id, body.item_id, rating, ts),
            )
        elif body.action_type == "fav":
            # 收藏：新记录按 5 分偏好写入；已有评分则保留原评分，只置收藏标记
            cur.execute(
                """
                INSERT INTO interactions (user_id, item_id, rating, action_type, is_fav, timestamp)
                VALUES (%s, %s, 5.0, 'fav', TRUE, %s)
                ON CONFLICT (user_id, item_id) DO UPDATE
                SET is_fav = TRUE, action_type = 'fav', timestamp = EXCLUDED.timestamp
                RETURNING rating::float8 AS rating, is_fav, action_type
                """,
                (body.user_id, body.item_id, ts),
            )
        else:  # unfav
            cur.execute(
                """
                UPDATE interactions SET is_fav = FALSE, action_type = 'unfav', timestamp = %s
                WHERE user_id = %s AND item_id = %s
                RETURNING rating::float8 AS rating, is_fav, action_type
                """,
                (ts, body.user_id, body.item_id),
            )
        row = cur.fetchone()
        total = interaction_count(cur, body.user_id)

    return to_native({
        "success": True,
        "user_id": body.user_id,
        "item_id": body.item_id,
        "user_rating": row["rating"] if row else None,
        "user_is_fav": row["is_fav"] if row else False,
        "user_action": row["action_type"] if row else None,
        "timestamp": ts,
        "interaction_count": total,
    })


# 静态资源（放在最后挂载，避免覆盖 API 路由）
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
