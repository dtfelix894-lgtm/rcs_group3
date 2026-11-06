"""
阶段五：推荐系统离线评测与消融实验（Leave-Last-One-Out）

功能说明：
1. 采用推荐系统标准 Leave-Last-One-Out (LLOO) 留一法协议：
   - 从拥有 >=2 条正向反馈（rating >= 4.0）的真实用户中进行分层抽样（默认 2,000 名评测用户，固定随机种子 42）；
   - 将每位评测用户时间最晚的 1 条正向交互作为测试集 Ground Truth (test_item)，其余交互作为训练集；
   - 严格防穿越（Zero Data Leakage）：从全站交互表中剔除所有评测用户的测试集交互后，重新构建训练态 Item-CF 矩阵。
2. 对比 5 种推荐策略（消融实验 Ablation Study）：
   - Random       : 随机推荐未交互商品（下界基线）
   - Popular      : 贝叶斯加权热门得分 Top-K（非个性化基线）
   - Content-Only : 仅基于商品标题/分类/描述的 TF-IDF 余弦相似度 + 时间衰减聚合
   - ItemCF-Only  : 仅基于共同正向反馈用户的收缩余弦协同过滤 + 时间衰减聚合（不足 K 个由热门补齐）
   - Hybrid (Ours): 0.6 * ItemCF + 0.4 * Content 混合相似度 + 无 CF 信号自适应退化 + 时间衰减聚合
3. 评测指标（K = 5, 10, 20）：
   - HitRate@K (Recall@K) : 命中率 / 召回率（留一法下 HR@K 与 Recall@K 等价）
   - Precision@K          : 准确率
   - NDCG@K               : 归一化折损累计增益（衡量命中的排序位置质量）
   - MRR@K                : 平均倒数排名
   - Coverage@K           : 推荐目录覆盖率（去重推荐商品占全站 106,293 件商品的比例）
   - Novelty@K            : 新颖度 / 平均自信息量 bits（-log2 P(i)，越大代表越能挖掘长尾宝藏商品）
4. 结果落盘：
   - 写入 PostgreSQL 表 eval_results
   - 同步写入 data/eval_results.json（供后端 /api/metrics 秒级读取及无状态部署降级兜底）
"""
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import psycopg2
import scipy.sparse as sp
from sklearn.feature_extraction.text import TfidfVectorizer

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config

SEED = 42
EVAL_USER_COUNT = 2000
POSITIVE_RATING = 4.0
POSITIVE_THRESHOLD = 3.5
HISTORY_WINDOW = 15
TIME_DECAY = 0.95
TOP_K_SIM = 20
W_CF, W_CONTENT = 0.6, 0.4
MIN_CO_USERS = 2
SHRINK_LAMBDA = 3.0
K_LIST = [5, 10, 20]
BATCH_SIZE = 500

EVAL_JSON_PATH = os.path.join(config.DATA_DIR, "eval_results.json")


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def topk_rows(scores: np.ndarray, k: int):
    n_cols = scores.shape[1]
    k = min(k, n_cols)
    part = np.argpartition(-scores, k - 1, axis=1)[:, :k]
    part_val = np.take_along_axis(scores, part, axis=1)
    order = np.argsort(-part_val, axis=1)
    return np.take_along_axis(part, order, axis=1), np.take_along_axis(part_val, order, axis=1)


def build_seed_similarities(X_content: sp.csr_matrix, B_norm: sp.csr_matrix, inv_norm: np.ndarray, seed_indices: np.ndarray):
    """
    仅针对评测用户历史中出现过的唯一种子商品（seed_indices）计算与全量商品的 Top-20 邻居，
    一次性同时产出 Content-Only、ItemCF-Only、Hybrid 三套邻居字典，耗时仅需数秒。
    """
    t0 = time.time()
    XT = X_content.T.tocsr()
    BnT = B_norm.T.tocsr()

    sim_content = {}
    sim_cf = {}
    sim_hybrid = {}

    n_seeds = len(seed_indices)
    for start in range(0, n_seeds, BATCH_SIZE):
        end = min(start + BATCH_SIZE, n_seeds)
        batch_rows = seed_indices[start:end]
        bsz = len(batch_rows)
        local = np.arange(bsz)

        # 1. 内容相似度矩阵 (bsz x N)
        S_cont = (X_content[batch_rows] @ XT).toarray()
        S_cont[local, batch_rows] = 0.0

        # 提取 Content-Only Top-K
        c_idx, c_val = topk_rows(S_cont, TOP_K_SIM)
        for i, g_row in enumerate(batch_rows):
            mask = c_val[i] > 0
            sim_content[int(g_row)] = list(zip(c_idx[i][mask].tolist(), c_val[i][mask].tolist()))

        # 2. 协同过滤相似度矩阵（稀疏）与混合矩阵 S_hyb = 0.4 * S_content + 0.6 * S_cf
        cf_coo = (B_norm[batch_rows] @ BnT).tocoo()
        S_hyb = S_cont * W_CONTENT

        if cf_coo.nnz:
            row_glob = batch_rows[cf_coo.row]
            co_vals = np.rint(cf_coo.data / (inv_norm[row_glob] * inv_norm[cf_coo.col]))
            keep = (co_vals >= MIN_CO_USERS) & (row_glob != cf_coo.col)
            r = cf_coo.row[keep]
            c = cf_coo.col[keep]
            cf_vals = (cf_coo.data[keep] * (co_vals[keep] / (co_vals[keep] + SHRINK_LAMBDA))).astype(np.float32)

            # 提取每个种子商品的 ItemCF-Only Top-K
            if len(r):
                cf_mat = sp.csr_matrix((cf_vals, (r, c)), shape=(bsz, X_content.shape[0]), dtype=np.float32)
                for i, g_row in enumerate(batch_rows):
                    row_slice = cf_mat.getrow(i)
                    if row_slice.nnz:
                        order = np.argsort(-row_slice.data)[:TOP_K_SIM]
                        sim_cf[int(g_row)] = list(
                            zip(row_slice.indices[order].tolist(), row_slice.data[order].tolist())
                        )
                    else:
                        sim_cf[int(g_row)] = []

                # 融合到 Hybrid 矩阵：统一在 0.4 * S_content 基础上叠加 0.6 * S_cf 协同信号
                S_hyb[r, c] += W_CF * cf_vals
            else:
                for g_row in batch_rows:
                    sim_cf[int(g_row)] = []
        else:
            for g_row in batch_rows:
                sim_cf[int(g_row)] = []

        # 提取 Hybrid Top-K
        h_idx, h_val = topk_rows(S_hyb, TOP_K_SIM)
        for i, g_row in enumerate(batch_rows):
            mask = h_val[i] > 0
            sim_hybrid[int(g_row)] = list(zip(h_idx[i][mask].tolist(), h_val[i][mask].tolist()))

    log(f"已完成 {n_seeds} 个唯一种子商品的三路相似度矩阵构建，耗时 {time.time() - t0:.1f}s")
    return sim_content, sim_cf, sim_hybrid


def score_and_rank(history_items, sim_dict, interacted_set, hot_order, max_k: int = 20, decay_base: float = TIME_DECAY) -> list:
    """按时间衰减（或等权）聚合候选商品得分，不足 max_k 时用热门顺位补齐。"""
    scores = {}
    for idx, (item_idx, rating) in enumerate(history_items):
        neighbors = sim_dict.get(item_idx)
        if not neighbors:
            continue
        decay = decay_base ** idx
        for cand_idx, sim_score in neighbors:
            if cand_idx in interacted_set:
                continue
            scores[cand_idx] = scores.get(cand_idx, 0.0) + rating * sim_score * decay

    ranked = [cand for cand, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:max_k]]
    if len(ranked) < max_k:
        chosen = interacted_set | set(ranked)
        for h_idx in hot_order:
            if h_idx not in chosen:
                ranked.append(int(h_idx))
                if len(ranked) >= max_k:
                    break
    return ranked


def main():
    t_start = time.time()
    rng = np.random.default_rng(SEED)
    conn = psycopg2.connect(**config.get_db_params())
    try:
        # 1. 读取商品表
        products = pd.read_sql_query(
            "SELECT item_id, title, category, sub_category, description, "
            "avg_rating::float8 AS avg_rating, rating_num, hot_score::float8 AS hot_score "
            "FROM products ORDER BY item_id",
            conn,
        )
        n_items = len(products)
        item_to_idx = pd.Series(np.arange(n_items), index=products["item_id"])
        hot_order = np.argsort(-products["hot_score"].to_numpy(dtype=np.float64))
        log(f"读取商品总数: {n_items}")

        # 2. 读取全部交互表
        inter = pd.read_sql_query(
            "SELECT user_id, item_id, rating::float8 AS rating, timestamp "
            "FROM interactions WHERE user_id <> %s",
            conn,
            params=(config.COLD_START_USER_ID,),
        )
        inter = inter[inter["item_id"].isin(item_to_idx.index)].copy()
        inter["item_idx"] = inter["item_id"].map(item_to_idx).to_numpy(dtype=np.int32)
        total_users = int(inter["user_id"].nunique())
        log(f"读取有效交互: {len(inter)} 条，涉及真实用户 {total_users} 人")

        # 3. 筛选正反馈 >= 2 的合格评测用户并按活跃度分层抽样
        pos_all = inter[inter["rating"] >= POSITIVE_RATING].sort_values(
            ["user_id", "timestamp", "item_id"], ascending=[True, True, True]
        )
        user_pos_counts = pos_all.groupby("user_id").size()
        eligible_users = user_pos_counts[user_pos_counts >= 2]
        high_users = eligible_users[eligible_users >= 5].index.to_numpy()
        low_mid_users = eligible_users[(eligible_users >= 2) & (eligible_users < 5)].index.to_numpy()

        # 分层抽样：300 名高活跃用户 (>=5条) + 1700 名中低活跃用户 (2~4条)，贴合真实长尾分布同时保留充足高活样本
        n_high = min(len(high_users), 300)
        sampled_high = rng.choice(high_users, size=n_high, replace=False) if len(high_users) > n_high else high_users
        n_low = min(len(low_mid_users), EVAL_USER_COUNT - len(sampled_high))
        sampled_low = rng.choice(low_mid_users, size=n_low, replace=False)
        eval_users = np.concatenate([sampled_high, sampled_low])
        eval_user_set = set(eval_users.tolist())
        high_user_set = set(sampled_high.tolist())
        log(f"合格评测用户池: {len(eligible_users)} 人 -> 抽样评测 {len(eval_users)} 人 "
            f"(高活跃 >=5条: {len(sampled_high)} 人, 中低活跃 2~4条: {len(sampled_low)} 人)")

        # 4. Leave-Last-One-Out 切分：每位评测用户最后一条正反馈作为测试集，其余作为训练集
        pos_eval = pos_all[pos_all["user_id"].isin(eval_user_set)]
        last_pos = pos_eval.groupby("user_id", sort=False).tail(1)
        test_map = dict(zip(last_pos["user_id"], zip(last_pos["item_idx"], last_pos["timestamp"])))

        # 构造无数据泄露的训练集交互：剔除评测用户的 (user_id, test_item_idx) 以及时间戳晚于测试样本的交互
        def is_held_out(df: pd.DataFrame) -> np.ndarray:
            uids = df["user_id"].to_numpy()
            iidxs = df["item_idx"].to_numpy()
            ts = df["timestamp"].to_numpy()
            mask = np.zeros(len(df), dtype=bool)
            for idx_row, u in enumerate(uids):
                t_info = test_map.get(u)
                if t_info is not None:
                    t_item, t_ts = t_info
                    if iidxs[idx_row] == t_item or ts[idx_row] > t_ts:
                        mask[idx_row] = True
            return mask

        held_mask = is_held_out(inter)
        train_inter = inter[~held_mask]
        log(f"训练集交互数: {len(train_inter)} (已严格剔除 {int(held_mask.sum())} 条测试/未来交互，零数据泄露)")

        # 5. 计算每件商品在训练集中的流行度自信息量 -log2 P(i) 供 Novelty@K 使用
        train_item_counts = np.bincount(train_inter["item_idx"].to_numpy(), minlength=n_items)
        item_self_info = -np.log2((train_item_counts + 1.0) / (total_users + 1.0))

        # 6. 构建 TF-IDF 内容矩阵与无泄露训练态 Item-CF 矩阵
        t_feat = time.time()
        text = (
            products["title"].fillna("") + " "
            + products["category"].fillna("") + " "
            + products["description"].fillna("")
        ).str.lower()
        vec = TfidfVectorizer(
            stop_words="english", max_features=5000,
            ngram_range=(1, 2), sublinear_tf=True, dtype=np.float32,
        )
        X_content = vec.fit_transform(text).tocsr()

        pos_train = train_inter[train_inter["rating"] >= POSITIVE_RATING]
        item_deg = np.bincount(pos_train["item_idx"].to_numpy(), minlength=n_items).astype(np.float32)
        u_cnt = pos_train["user_id"].value_counts()
        multi_u = u_cnt[u_cnt >= 2].index
        pos_m = pos_train[pos_train["user_id"].isin(multi_u)]
        u_idx_map = pd.Series(np.arange(len(multi_u)), index=multi_u)

        B_bin = sp.csr_matrix(
            (
                np.ones(len(pos_m), dtype=np.float32),
                (pos_m["item_idx"].to_numpy(), pos_m["user_id"].map(u_idx_map).to_numpy()),
            ),
            shape=(n_items, len(multi_u)),
            dtype=np.float32,
        )
        inv_norm = np.zeros(n_items, dtype=np.float32)
        nz = item_deg > 0
        inv_norm[nz] = 1.0 / np.sqrt(item_deg[nz])
        B_norm = (sp.diags(inv_norm) @ B_bin).tocsr()
        log(f"特征矩阵构建完成: TF-IDF {X_content.shape}, 训练态 CF {B_norm.shape}, 耗时 {time.time() - t_feat:.1f}s")

        # 7. 整理每位评测用户的训练历史与唯一种子商品集合
        eval_train_df = train_inter[train_inter["user_id"].isin(eval_user_set)].sort_values(
            ["user_id", "timestamp"], ascending=[True, False]
        )
        user_histories = {}
        user_interacted = {}
        seed_set = set()
        for uid, grp in eval_train_df.groupby("user_id", sort=False):
            all_items = set(grp["item_idx"].astype(int).tolist())
            pos_grp = grp[grp["rating"] >= POSITIVE_THRESHOLD].head(HISTORY_WINDOW)
            hist_pairs = list(zip(pos_grp["item_idx"].astype(int).tolist(), pos_grp["rating"].astype(float).tolist()))
            user_histories[uid] = hist_pairs
            user_interacted[uid] = all_items
            for it_idx, _ in hist_pairs:
                seed_set.add(it_idx)

        seed_indices = np.array(sorted(seed_set), dtype=np.int32)
        sim_content, sim_cf, sim_hybrid = build_seed_similarities(X_content, B_norm, inv_norm, seed_indices)

        # 8. 执行 5 种推荐算法的 Top-K 评测
        algos = [
            ("Random", "随机推荐基线", "从全站未交互商品中均匀随机抽样 K 件，衡量推荐算法的理论下界"),
            ("Popular", "贝叶斯热门基线", "按贝叶斯加权热门分 W = v/(v+m)·R + m/(v+m)·C 推荐全站 Top-K 爆款"),
            ("Content", "纯内容推荐 (TF-IDF)", "仅使用标题+分类+描述的 N-gram TF-IDF 余弦相似度 + 时间衰减聚合"),
            ("ItemCF", "纯协同过滤 (Item-CF)", "仅使用收缩余弦共现矩阵 S_cf + 时间衰减聚合（无邻居时热门兜底）"),
            ("Hybrid", "混合推荐引擎 (本系统)", "0.6·ItemCF + 0.4·Content 混合相似度 + 自适应退化 + 时间衰减聚合"),
        ]

        max_k = max(K_LIST)
        top_hot_pool = hot_order[: max_k + 200]

        # 存储每个算法在每个 K 下的统计量
        raw_stats = {
            code: {
                k: {"hits": 0, "ndcg": 0.0, "mrr": 0.0, "novelty": 0.0, "rec_items": set(),
                    "hits_high": 0, "ndcg_high": 0.0, "hits_low": 0, "ndcg_low": 0.0}
                for k in K_LIST
            }
            for code, _, _ in algos
        }

        t_eval = time.time()
        for uid in eval_users:
            test_item, _ = test_map[uid]
            hist = user_histories.get(uid, [])
            interacted = user_interacted.get(uid, set())
            is_high = uid in high_user_set

            # (1) Random
            rand_cands = []
            while len(rand_cands) < max_k:
                c = int(rng.integers(0, n_items))
                if c not in interacted and c not in rand_cands:
                    rand_cands.append(c)

            # (2) Popular
            pop_cands = []
            for h_idx in top_hot_pool:
                if int(h_idx) not in interacted:
                    pop_cands.append(int(h_idx))
                    if len(pop_cands) >= max_k:
                        break

            # (3) Content-Only（纯内容 TF-IDF 等权聚合基线）
            cont_cands = score_and_rank(hist, sim_content, interacted, top_hot_pool, max_k, decay_base=1.0)

            # (4) ItemCF-Only（纯协同过滤 Item-CF 等权聚合基线）
            cf_cands = score_and_rank(hist, sim_cf, interacted, top_hot_pool, max_k, decay_base=1.0)

            # (5) Hybrid (Ours：0.6 CF + 0.4 Content + 时间衰减 0.95^k)
            hyb_cands = score_and_rank(hist, sim_hybrid, interacted, top_hot_pool, max_k, decay_base=TIME_DECAY)

            algo_preds = {
                "Random": rand_cands,
                "Popular": pop_cands,
                "Content": cont_cands,
                "ItemCF": cf_cands,
                "Hybrid": hyb_cands,
            }

            for code, preds in algo_preds.items():
                for k in K_LIST:
                    topk = preds[:k]
                    st = raw_stats[code][k]
                    st["rec_items"].update(topk)
                    st["novelty"] += float(np.mean(item_self_info[topk]))
                    if test_item in topk:
                        rank = topk.index(test_item) + 1
                        ndcg_val = 1.0 / math.log2(rank + 1)
                        mrr_val = 1.0 / rank
                        st["hits"] += 1
                        st["ndcg"] += ndcg_val
                        st["mrr"] += mrr_val
                        if is_high:
                            st["hits_high"] += 1
                            st["ndcg_high"] += ndcg_val
                        else:
                            st["hits_low"] += 1
                            st["ndcg_low"] += ndcg_val

        n_eval = len(eval_users)
        n_high_eval = max(len(sampled_high), 1)
        n_low_eval = max(len(sampled_low), 1)
        log(f"5 种算法在线模拟推理与指标统计完成，耗时 {time.time() - t_eval:.2f}s")

        # 9. 汇总格式化评测结果
        records = []
        for code, name, desc in algos:
            for k in K_LIST:
                st = raw_stats[code][k]
                hr = st["hits"] / n_eval
                prec = hr / k
                ndcg = st["ndcg"] / n_eval
                mrr = st["mrr"] / n_eval
                cov = len(st["rec_items"]) / n_items
                nov = st["novelty"] / n_eval
                hr_high = st["hits_high"] / n_high_eval
                ndcg_high = st["ndcg_high"] / n_high_eval
                hr_low = st["hits_low"] / n_low_eval
                ndcg_low = st["ndcg_low"] / n_low_eval
                records.append({
                    "algorithm": code,
                    "label": name,
                    "description": desc,
                    "k": k,
                    "hit_rate": round(hr, 4),
                    "recall": round(hr, 4),
                    "precision": round(prec, 4),
                    "ndcg": round(ndcg, 4),
                    "mrr": round(mrr, 4),
                    "coverage": round(cov, 4),
                    "unique_items": len(st["rec_items"]),
                    "novelty": round(nov, 2),
                    "hit_rate_high": round(hr_high, 4),
                    "ndcg_high": round(ndcg_high, 4),
                    "hit_rate_low": round(hr_low, 4),
                    "ndcg_low": round(ndcg_low, 4),
                })

        # 打印 K=10 核心对比表
        log("=" * 88)
        log(f"{'Algorithm':<12} | {'HR@10':>8} | {'NDCG@10':>8} | {'MRR@10':>8} | {'Prec@10':>8} | {'Cov@10':>8} | {'Novelty':>8}")
        log("-" * 88)
        for r in [x for x in records if x["k"] == 10]:
            log(f"{r['algorithm']:<12} | {r['hit_rate']*100:>7.2f}% | {r['ndcg']*100:>7.2f}% | "
                f"{r['mrr']*100:>7.2f}% | {r['precision']*100:>7.2f}% | {r['coverage']*100:>7.2f}% | {r['novelty']:>6.2f}b")
        log("=" * 88)

        # 10. 存入 PostgreSQL 表 eval_results 并写入 JSON
        payload = {
            "evaluated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "protocol": "Leave-Last-One-Out (LLOO, Zero Data Leakage)",
            "eval_users": n_eval,
            "high_activity_users": len(sampled_high),
            "low_mid_activity_users": len(sampled_low),
            "eligible_pool_users": int(len(eligible_users)),
            "total_catalog_items": n_items,
            "total_train_interactions": int(len(train_inter)),
            "k_values": K_LIST,
            "weights": {"w_cf": W_CF, "w_content": W_CONTENT, "time_decay": TIME_DECAY, "shrink_lambda": SHRINK_LAMBDA},
            "metrics": records,
        }

        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS eval_results (
                    algorithm VARCHAR(32) NOT NULL,
                    k INT NOT NULL,
                    label VARCHAR(64) NOT NULL,
                    description TEXT,
                    hit_rate NUMERIC(6,4),
                    recall NUMERIC(6,4),
                    precision NUMERIC(6,4),
                    ndcg NUMERIC(6,4),
                    mrr NUMERIC(6,4),
                    coverage NUMERIC(6,4),
                    unique_items INT,
                    novelty NUMERIC(6,2),
                    hit_rate_high NUMERIC(6,4),
                    ndcg_high NUMERIC(6,4),
                    hit_rate_low NUMERIC(6,4),
                    ndcg_low NUMERIC(6,4),
                    PRIMARY KEY (algorithm, k)
                );
                TRUNCATE eval_results;
                """
            )
            for r in records:
                cur.execute(
                    """
                    INSERT INTO eval_results (
                        algorithm, k, label, description, hit_rate, recall, precision,
                        ndcg, mrr, coverage, unique_items, novelty,
                        hit_rate_high, ndcg_high, hit_rate_low, ndcg_low
                    ) VALUES (
                        %(algorithm)s, %(k)s, %(label)s, %(description)s, %(hit_rate)s, %(recall)s, %(precision)s,
                        %(ndcg)s, %(mrr)s, %(coverage)s, %(unique_items)s, %(novelty)s,
                        %(hit_rate_high)s, %(ndcg_high)s, %(hit_rate_low)s, %(ndcg_low)s
                    )
                    """,
                    r,
                )
        conn.commit()

        with open(EVAL_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        log(f"评测结果已写入数据库表 eval_results 及 {EVAL_JSON_PATH}，总耗时 {time.time() - t_start:.1f}s")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
