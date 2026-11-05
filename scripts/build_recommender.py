"""
阶段二：推荐算法离线预计算与入库

算法1：贝叶斯加权热门得分 hot_score
    W = v/(v+m) * R + m/(v+m) * C
    R = avg_rating, v = rating_num, C = 全体 avg_rating 均值, m = rating_num 的 80% 分位数（为 0 则取 5）

算法2：混合商品相似度（Content TF-IDF + Item-CF Hybrid），全量商品 + 稀疏矩阵 + 分块计算
    - S_content：title + category + description -> TF-IDF（L2 归一化，点积即余弦）
    - S_cf：仅使用 rating >= 4 的交互作为隐式正反馈（值记 1），构建 Item x User 稀疏矩阵并 L2 归一化；
            要求共同用户数 >= MIN_CO_USERS，并乘以收缩因子 n_co / (n_co + SHRINK_LAMBDA) 抑制偶然共现噪声
    - S_hybrid = 0.6 * S_cf + 0.4 * S_content；若某商品无任何 CF 信号，则 S_hybrid = S_content
    - 排除自身，提取相似度 > 0 的 Top-20 写入 item_similarity（JSONB）
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import psycopg2
import scipy.sparse as sp
from psycopg2.extras import execute_values
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config

PAGE_SIZE = 5000
BATCH_SIZE = 500
TOP_K = 20
W_CF, W_CONTENT = 0.6, 0.4
POSITIVE_RATING = 4.0   # >= 该评分视为正反馈
MIN_CO_USERS = 2        # CF 最少共同用户数
SHRINK_LAMBDA = 3.0     # CF 收缩系数
SCORE_DECIMALS = 4


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# 数据读取
# ----------------------------------------------------------------------------
def load_products(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT item_id, title, category, description, "
        "avg_rating::float8 AS avg_rating, rating_num FROM products ORDER BY item_id",
        conn,
    )
    log(f"读取 products: {len(df)} 条")
    return df


def load_interactions(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT user_id, item_id, rating::float8 AS rating FROM interactions", conn
    )
    log(f"读取 interactions: {len(df)} 条")
    return df


# ----------------------------------------------------------------------------
# 算法1：贝叶斯加权热门得分
# ----------------------------------------------------------------------------
def compute_hot_score(conn, products: pd.DataFrame) -> pd.Series:
    t0 = time.time()
    R = products["avg_rating"].fillna(0.0).to_numpy(dtype=np.float64)
    v = products["rating_num"].fillna(0).to_numpy(dtype=np.float64)
    C = float(np.mean(R))
    m = float(np.quantile(v, 0.8))
    if m <= 0:
        m = 5.0
    W = v / (v + m) * R + m / (v + m) * C
    W = np.round(W, 4)
    log(f"hot_score 参数: C={C:.4f}, m={m:.1f}; 得分范围 [{W.min():.4f}, {W.max():.4f}]")

    rows = list(zip(products["item_id"].tolist(), W.tolist()))
    with conn.cursor() as cur:
        execute_values(
            cur,
            "UPDATE products AS p SET hot_score = v.hot_score "
            "FROM (VALUES %s) AS v(item_id, hot_score) WHERE p.item_id = v.item_id",
            rows, template="(%s, %s::numeric)", page_size=PAGE_SIZE,
        )
    conn.commit()
    log(f"hot_score 已批量回写 {len(rows)} 条，耗时 {time.time() - t0:.1f}s")
    return pd.Series(W, index=products["item_id"])


# ----------------------------------------------------------------------------
# 算法2：特征构建
# ----------------------------------------------------------------------------
def build_content_matrix(products: pd.DataFrame) -> sp.csr_matrix:
    t0 = time.time()
    text = (
        products["title"].fillna("") + " "
        + products["category"].fillna("") + " "
        + products["description"].fillna("")
    ).str.lower()
    vec = TfidfVectorizer(
        stop_words="english", max_features=5000,
        ngram_range=(1, 2), sublinear_tf=True, dtype=np.float32,
    )
    X = vec.fit_transform(text).tocsr()  # 默认 L2 归一化
    log(f"TF-IDF 矩阵: {X.shape}, nnz={X.nnz}, 耗时 {time.time() - t0:.1f}s")
    return X


def build_cf_matrices(products: pd.DataFrame, inter: pd.DataFrame):
    """
    返回:
      B_norm   : Item x User(仅保留正反馈>=2 的用户) 的 L2 归一化稀疏矩阵（范数按全部正反馈用户计算）
      B_bin    : 同形状的 0/1 矩阵，用于统计共同用户数
      has_pos  : 每个商品是否有正反馈
    说明：只有 1 条正反馈的用户只会贡献对角线（商品自身），对两两共现无贡献，
         因此可剔除以加速矩阵乘法；但物品向量范数仍按全部用户计算，保证余弦值正确。
    """
    t0 = time.time()
    n_items = len(products)
    item_index = pd.Series(np.arange(n_items), index=products["item_id"])

    pos = inter[inter["rating"] >= POSITIVE_RATING][["user_id", "item_id"]]
    pos = pos[pos["item_id"].isin(item_index.index)]
    log(f"CF 正反馈交互 (rating>={POSITIVE_RATING}): {len(pos)} / {len(inter)}")

    # 每个物品的正反馈用户数（全部用户）-> 余弦范数
    item_deg = np.zeros(n_items, dtype=np.float32)
    deg_counts = pos["item_id"].map(item_index).value_counts()
    item_deg[deg_counts.index.to_numpy()] = deg_counts.to_numpy()

    user_cnt = pos["user_id"].value_counts()
    multi_users = user_cnt[user_cnt >= 2].index
    pos_m = pos[pos["user_id"].isin(multi_users)]
    user_index = pd.Series(np.arange(len(multi_users)), index=multi_users)

    rows = pos_m["item_id"].map(item_index).to_numpy()
    cols = pos_m["user_id"].map(user_index).to_numpy()
    data = np.ones(len(pos_m), dtype=np.float32)
    B_bin = sp.csr_matrix((data, (rows, cols)), shape=(n_items, len(multi_users)), dtype=np.float32)

    # L2 归一化：除以 sqrt(全部正反馈用户数)
    inv_norm = np.zeros(n_items, dtype=np.float32)
    nz = item_deg > 0
    inv_norm[nz] = 1.0 / np.sqrt(item_deg[nz])
    B_norm = sp.diags(inv_norm) @ B_bin
    B_norm = B_norm.tocsr()

    log(f"CF 矩阵: {B_bin.shape}（正反馈>=2 的用户 {len(multi_users)} 人）, nnz={B_bin.nnz}, "
        f"有正反馈商品 {int(nz.sum())}, 耗时 {time.time() - t0:.1f}s")
    return B_norm, inv_norm


# ----------------------------------------------------------------------------
# 算法2：分块融合 + Top-K
# ----------------------------------------------------------------------------
def topk_rows(scores: np.ndarray, k: int):
    """对每行取 Top-k（降序），返回 (idx, val)。"""
    n_cols = scores.shape[1]
    k = min(k, n_cols)
    part = np.argpartition(-scores, k - 1, axis=1)[:, :k]
    part_val = np.take_along_axis(scores, part, axis=1)
    order = np.argsort(-part_val, axis=1)
    return np.take_along_axis(part, order, axis=1), np.take_along_axis(part_val, order, axis=1)


def compute_similarity(conn, products, X_content, B_norm, inv_norm):
    item_ids = np.array(products["item_id"].tolist(), dtype=object)
    n = len(item_ids)
    XT = X_content.T.tocsr()
    BnT = B_norm.T.tocsr()

    with conn.cursor() as cur:
        cur.execute("TRUNCATE item_similarity")
    conn.commit()

    stats = {"items_written": 0, "neighbors": 0, "cf_items": 0}
    t0 = time.time()
    n_batches = (n + BATCH_SIZE - 1) // BATCH_SIZE

    for b, start in enumerate(range(0, n, BATCH_SIZE)):
        end = min(start + BATCH_SIZE, n)
        bsz = end - start
        local = np.arange(bsz)
        glob = np.arange(start, end)

        # ---- 内容相似度（稀疏乘法 -> 当前批次稠密 bsz x N，float32）----
        S = (X_content[start:end] @ XT).toarray()
        S[local, glob] = 0.0  # 排除自身（按全局列索引）

        # ---- CF 相似度（保持稀疏）：归一化向量点积 = 余弦 ----
        cf = (B_norm[start:end] @ BnT).tocoo()
        if cf.nnz:
            # 0/1 矩阵下 cos = n_co / sqrt(deg_i * deg_j)，反推共同用户数 n_co
            co_vals = np.rint(cf.data / (inv_norm[cf.row + start] * inv_norm[cf.col]))
            keep = (co_vals >= MIN_CO_USERS) & (cf.row + start != cf.col)
            r, c = cf.row[keep], cf.col[keep]
            cf_vals = cf.data[keep] * (co_vals[keep] / (co_vals[keep] + SHRINK_LAMBDA))
        else:
            r = c = np.array([], dtype=np.int64)
            cf_vals = np.array([], dtype=np.float32)

        # ---- 融合：有 CF 信号的行 0.6*cf + 0.4*content，否则纯 content ----
        if len(r):
            cf_rows = np.unique(r)
            S[cf_rows] *= W_CONTENT
            S[r, c] += W_CF * cf_vals.astype(np.float32)
            stats["cf_items"] += len(cf_rows)

        # ---- Top-K ----
        idx, val = topk_rows(S, TOP_K)
        records = []
        for i in range(bsz):
            mask = val[i] > 0
            if not mask.any():
                continue
            sims = [
                {"item_id": item_ids[j], "score": round(float(s), SCORE_DECIMALS)}
                for j, s in zip(idx[i][mask], val[i][mask])
            ]
            records.append((item_ids[start + i], json.dumps(sims, ensure_ascii=False)))
            stats["neighbors"] += len(sims)

        if records:
            with conn.cursor() as cur:
                execute_values(
                    cur, "INSERT INTO item_similarity (item_id, similar_items) VALUES %s",
                    records, template="(%s, %s::jsonb)", page_size=PAGE_SIZE,
                )
            conn.commit()
            stats["items_written"] += len(records)

        del S
        if (b + 1) % 20 == 0 or b + 1 == n_batches:
            el = time.time() - t0
            log(f"  相似度批次 {b + 1}/{n_batches}，已写入 {stats['items_written']} 条，"
                f"耗时 {el:.0f}s，预计剩余 {el / (b + 1) * (n_batches - b - 1):.0f}s")

    log(f"相似度计算完成：覆盖 {stats['items_written']}/{n} "
        f"({stats['items_written'] / n:.1%})，平均相似商品数 "
        f"{stats['neighbors'] / max(stats['items_written'], 1):.2f}，"
        f"含 CF 信号商品 {stats['cf_items']} ({stats['cf_items'] / n:.1%})，"
        f"总耗时 {time.time() - t0:.1f}s")


def main():
    t_start = time.time()
    conn = psycopg2.connect(**config.get_db_params())
    try:
        products = load_products(conn)
        inter = load_interactions(conn)

        compute_hot_score(conn, products)

        X_content = build_content_matrix(products)
        B_norm, inv_norm = build_cf_matrices(products, inter)
        del inter
        compute_similarity(conn, products, X_content, B_norm, inv_norm)

        old = conn.autocommit
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ANALYZE products; ANALYZE item_similarity;")
        conn.autocommit = old
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log(f"阶段二全部完成，总耗时 {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    main()
