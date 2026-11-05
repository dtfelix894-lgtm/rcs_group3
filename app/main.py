"""
阶段三：FastAPI 后端服务

启动：uvicorn main:app --host 127.0.0.1 --port 8000
访问：http://127.0.0.1:8000/

接口一览（注意：/api/products/hot 与 /api/products/search 必须注册在 /api/products/{item_id} 之前）
  GET    /                                   前端首页 static/index.html
  GET    /api/users/sample                   测试账号列表（冷启动账号 + 10 个活跃用户）
  GET    /api/categories                     商品数量前 15 的子分类
  GET    /api/products/hot                   贝叶斯热门榜单
  GET    /api/products/search                关键词 + 分类搜索
  GET    /api/products/{item_id}             商品详情 + 当前用户评分/收藏状态
  GET    /api/recommend/similar/{item_id}    看了又看（混合相似度 + 同类热门兜底）
  GET    /api/recommend/user/{user_id}       实时个性化推荐 + 可解释理由（含冷启动分支）
  POST   /api/interactions                   打分 / 收藏 / 取消收藏
  GET    /api/users/{user_id}/history        用户足迹与收藏
  DELETE /api/users/{user_id}/interactions   重置测试账号（仅限冷启动测试账号）
"""
import os
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

app = FastAPI(title="智选电商 · 个性化推荐系统", version="1.0.0")
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
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ----------------------------------------------------------------------------
# 用户
# ----------------------------------------------------------------------------
@app.get("/api/users/sample")
def users_sample():
    with get_cursor() as cur:
        cur.execute(
            "SELECT u.user_id, u.username, "
            "(SELECT COUNT(*) FROM interactions i WHERE i.user_id = u.user_id) AS interaction_count "
            "FROM users u WHERE u.user_id = %s",
            (config.COLD_START_USER_ID,),
        )
        cold = cur.fetchone()
        cur.execute(
            """
            SELECT t.user_id, u.username, t.n AS interaction_count
            FROM (
                SELECT user_id, COUNT(*) AS n FROM interactions
                WHERE user_id <> %s
                GROUP BY user_id ORDER BY n DESC LIMIT 10
            ) t JOIN users u USING (user_id)
            ORDER BY t.n DESC
            """,
            (config.COLD_START_USER_ID,),
        )
        active = cur.fetchall()
    result = []
    if cold:
        result.append({**cold, "is_cold_start_account": True})
    result += [{**r, "is_cold_start_account": False} for r in active]
    return to_native(result)


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
    limit: int = Query(24, ge=1, le=100),
):
    q, category = q.strip(), category.strip()
    conds, params = [], []
    if q:
        pattern = f"%{escape_like(q)}%"
        conds.append("(p.title ILIKE %s OR p.description ILIKE %s)")
        params += [pattern, pattern]
    if category:
        conds.append("p.sub_category = %s")
        params.append(category)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with get_cursor() as cur:
        cur.execute(
            f"SELECT {LIST_COLUMNS} FROM products p {where} ORDER BY p.hot_score DESC LIMIT %s",
            params + [limit],
        )
        items = cur.fetchall()
    return to_native({"q": q, "category": category, "count": len(items), "items": items})


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

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        prods = fetch_products_by_ids(cur, [j for j, _ in ranked])
        items = []
        for j, sc in ranked:
            p = prods.get(j)
            if not p:
                continue
            _, h, src_title = best[j]
            if h["is_fav"]:
                reason = f"💡 因为您收藏了「{short_title(src_title)}」"
            else:
                reason = f"💡 因为您给「{short_title(src_title)}」打了 {h['rating']:g} 星"
            items.append({**p, "reason": reason, "rec_score": round(sc, 4),
                          "based_on_item_id": h["item_id"]})

        # 不足 limit 时用热门补齐（排除已交互与已推荐）
        if len(items) < limit:
            exclude = interacted | {it["item_id"] for it in items}
            for p in fetch_hot(cur, limit - len(items), exclude):
                items.append({**p, "reason": REASON_FILL, "rec_score": None})

    return to_native({"is_cold_start": False, "user_id": user_id,
                      "based_on_count": len(positives), "items": items})


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
