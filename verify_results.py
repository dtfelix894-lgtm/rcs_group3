"""
阶段一/二结果验证：
  1. 4 张表记录数
  2. hot_score Top-5 热门商品
  3. 随机抽取 1 个有交互记录的热门商品，打印其 Top-5 相似商品
  4. 相似度覆盖率统计
"""
import psycopg2

import config


def hr(title: str):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def main():
    conn = psycopg2.connect(**config.get_db_params())
    cur = conn.cursor()

    hr("1. 各表记录数")
    for t in ("products", "users", "interactions", "item_similarity"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"  {t:<16} {cur.fetchone()[0]:>10,}")
    cur.execute("SELECT username FROM users WHERE user_id = %s", (config.COLD_START_USER_ID,))
    row = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM interactions WHERE user_id = %s", (config.COLD_START_USER_ID,))
    print(f"  冷启动账号: {config.COLD_START_USER_ID} / {row[0] if row else '不存在'}，"
          f"交互数 = {cur.fetchone()[0]}")

    hr("2. hot_score Top-5 热门商品")
    cur.execute("""
        SELECT item_id, title, avg_rating, rating_num, hot_score
        FROM products ORDER BY hot_score DESC LIMIT 5
    """)
    print(f"  {'item_id':<12}{'avg':>6}{'num':>8}{'hot':>9}  title")
    for iid, title, avg, num, hot in cur.fetchall():
        print(f"  {iid:<12}{float(avg):>6.2f}{num:>8}{float(hot):>9.4f}  {title[:70]}")

    hr("3. 随机抽取一个有交互记录的热门商品，查看 Top-5 相似商品")
    cur.execute("""
        WITH hot AS (
            SELECT p.item_id FROM products p
            WHERE EXISTS (SELECT 1 FROM interactions i WHERE i.item_id = p.item_id)
              AND EXISTS (SELECT 1 FROM item_similarity s WHERE s.item_id = p.item_id)
            ORDER BY p.hot_score DESC LIMIT 100
        )
        SELECT item_id FROM hot ORDER BY random() LIMIT 1
    """)
    target = cur.fetchone()[0]
    cur.execute("""
        SELECT p.item_id, p.title, p.hot_score,
               (SELECT COUNT(*) FROM interactions i WHERE i.item_id = p.item_id)
        FROM products p WHERE p.item_id = %s
    """, (target,))
    iid, title, hot, n_inter = cur.fetchone()
    print(f"  目标商品: [{iid}] {title}")
    print(f"  hot_score={float(hot):.4f}, 交互数={n_inter}\n")

    cur.execute("""
        SELECT e.ord, e.elem->>'item_id', (e.elem->>'score')::float, p.title
        FROM item_similarity s
        CROSS JOIN LATERAL jsonb_array_elements(s.similar_items) WITH ORDINALITY AS e(elem, ord)
        JOIN products p ON p.item_id = e.elem->>'item_id'
        WHERE s.item_id = %s
        ORDER BY e.ord LIMIT 5
    """, (target,))
    print(f"  {'#':<3}{'item_id':<12}{'score':>8}  title")
    for ord_, sid, score, stitle in cur.fetchall():
        print(f"  {ord_:<3}{sid:<12}{score:>8.4f}  {stitle[:75]}")

    hr("4. 相似度覆盖统计")
    cur.execute("""
        SELECT COUNT(*),
               AVG(jsonb_array_length(similar_items)),
               (SELECT COUNT(*) FROM products)
        FROM item_similarity
    """)
    n_sim, avg_len, n_prod = cur.fetchone()
    print(f"  覆盖率: {n_sim:,}/{n_prod:,} = {n_sim / n_prod:.2%}")
    print(f"  平均相似商品数: {float(avg_len):.2f}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
