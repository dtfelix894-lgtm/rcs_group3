"""
阶段三/四接口自测脚本：
  1. GET  /api/users/sample
  2. GET  /api/categories
  3. GET  /api/products/hot
  4. GET  /api/products/search
  5. GET  /api/recommend/user/cold_start_user（验证冷启动模式）
  6. POST /api/interactions（cold_start_user 给热门商品打 5 星）
  7. GET  /api/recommend/user/cold_start_user（验证切换为个性化推荐 + 打印前 3 个推荐商品及 reason）
  8. GET  /api/products/{item_id}?user_id=cold_start_user & GET /api/recommend/similar/{item_id}
  9. GET  /api/metrics、/api/users/browse、/api/dashboard/overview、/api/dashboard/similarity-sample
     GET  /api/users/cold_start_user/history
 10. DELETE /api/users/cold_start_user/interactions（测试结束后恢复冷启动状态，方便前端演示）
"""
import sys
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "http://127.0.0.1:8000"


def hr(title: str):
    print("\n" + "=" * 95)
    print(title)
    print("=" * 95)


def check(res: requests.Response, label: str):
    assert res.status_code == 200, f"[{label}] HTTP {res.status_code}: {res.text}"
    return res.json()


def main():
    # 先确保 cold_start_user 处于初始无行为状态
    requests.delete(f"{BASE_URL}/api/users/cold_start_user/interactions", timeout=10)

    hr("1. 测试 GET / (前端首页 HTML)")
    r_home = requests.get(f"{BASE_URL}/", timeout=10)
    assert r_home.status_code == 200 and "智选电商" in r_home.text
    print(f"  STATUS 200 OK, 页面长度: {len(r_home.text):,} 字符")

    hr("2. 测试 GET /api/users/sample & GET /api/categories")
    users = check(requests.get(f"{BASE_URL}/api/users/sample", timeout=10), "users/sample")
    print(f"  返回测试用户数: {len(users)}")
    print(f"  首位冷启动账号: {users[0]}")
    print(f"  首位活跃真用户: {users[1]}")

    cats = check(requests.get(f"{BASE_URL}/api/categories", timeout=10), "categories")
    print(f"  派生美妆子分类 ({len(cats)} 个): " + ", ".join(f"{c['name']}({c['count']})" for c in cats))

    hr("3. 测试 GET /api/products/hot?limit=12")
    hot = check(requests.get(f"{BASE_URL}/api/products/hot?limit=12", timeout=10), "products/hot")
    print(f"  返回热门商品数: {len(hot)}")
    for i, p in enumerate(hot[:3], 1):
        print(f"  Top {i}: [{p['item_id']}] hot={p['hot_score']:.4f} | {p['sub_category']} | {p['title'][:65]}")

    hr("4. 测试 GET /api/recommend/user/cold_start_user (冷启动验证)")
    rec_before = check(
        requests.get(f"{BASE_URL}/api/recommend/user/cold_start_user?limit=12", timeout=10),
        "recommend/cold_start",
    )
    assert rec_before["is_cold_start"] is True, "期望 is_cold_start == True"
    print(f"  is_cold_start = {rec_before['is_cold_start']}, 返回商品数 = {len(rec_before['items'])}")
    print(f"  首条推荐: [{rec_before['items'][0]['item_id']}] {rec_before['items'][0]['title'][:55]}")
    print(f"  首条理由: {rec_before['items'][0]['reason']}")

    target_item = hot[0]
    hr(f"5. 测试 POST /api/interactions (cold_start_user 给 [{target_item['item_id']}] 打 5.0 星)")
    post_res = check(
        requests.post(
            f"{BASE_URL}/api/interactions",
            json={
                "user_id": "cold_start_user",
                "item_id": target_item["item_id"],
                "rating": 5.0,
                "action_type": "rate",
            },
            timeout=10,
        ),
        "post/interactions",
    )
    print(f"  写入成功: {post_res}")

    hr("6. 再次请求 GET /api/recommend/user/cold_start_user (验证实时切换为个性化推荐)")
    rec_after = check(
        requests.get(f"{BASE_URL}/api/recommend/user/cold_start_user?limit=12", timeout=10),
        "recommend/personalized",
    )
    assert rec_after["is_cold_start"] is False, "期望 is_cold_start == False"
    print(f"  is_cold_start = {rec_after['is_cold_start']}, based_on_count = {rec_after['based_on_count']}")
    print("  前 3 个个性化推荐商品及可解释理由:")
    for i, item in enumerate(rec_after["items"][:3], 1):
        print(f"    #{i} [{item['item_id']}] rec_score={item['rec_score']} | {item['title'][:65]}")
        print(f"       reason: {item['reason']}")

    hr("7. 测试商品详情、相似推荐、搜索与用户足迹接口")
    detail = check(
        requests.get(
            f"{BASE_URL}/api/products/{target_item['item_id']}?user_id=cold_start_user", timeout=10
        ),
        "product/detail",
    )
    print(f"  商品详情: [{detail['item_id']}] user_rating={detail['user_rating']}, user_action={detail['user_action']}")

    sim = check(
        requests.get(f"{BASE_URL}/api/recommend/similar/{target_item['item_id']}?limit=6", timeout=10),
        "recommend/similar",
    )
    print(f"  看了又看返回 {len(sim['items'])} 件，首件匹配度: {sim['items'][0]['score']*100:.1f}% - {sim['items'][0]['title'][:55]}")

    srch = check(
        requests.get(f"{BASE_URL}/api/products/search?q=lotion&category=护肤&limit=24", timeout=10),
        "products/search",
    )
    print(f"  搜索 q='lotion' & category='护肤' 总命中 total={srch['total']} 件，当前页返回 {srch['count']} 件")

    rnd_user = check(
        requests.get(f"{BASE_URL}/api/users/random?tier=medium", timeout=10),
        "users/random",
    )
    print(f"  随机抽取中活跃用户: {rnd_user}")

    usr_srch = check(
        requests.get(f"{BASE_URL}/api/users/search?q=User_AE2&limit=5", timeout=10),
        "users/search",
    )
    print(f"  搜索用户 q='User_AE2' 返回 {len(usr_srch)} 名用户，首位: {usr_srch[0] if usr_srch else None}")

    metrics = check(
        requests.get(f"{BASE_URL}/api/metrics", timeout=10),
        "metrics",
    )
    print(f"  离线评测接口 /api/metrics 返回 {len(metrics['metrics'])} 条算法消融记录，评测人数: {metrics['eval_users']}")

    browse = check(
        requests.get(f"{BASE_URL}/api/users/browse?offset=0&limit=5", timeout=30),
        "users/browse",
    )
    assert len(browse["items"]) == 5 and browse["has_more"]
    print(f"  全库用户分页浏览: total={browse['total']:,}，has_more={browse['has_more']}")

    dash = check(
        requests.get(f"{BASE_URL}/api/dashboard/overview", timeout=120),
        "dashboard/overview",
    )
    rows = {t["name"]: t["rows"] for t in dash["tables"]}
    assert rows["products"] > 0 and rows["item_similarity"] > 0
    assert sum(b["count"] for b in dash["activity"]) == rows["users"]
    print(f"  数据全景: {rows}，单条行为用户占比 {dash['sparsity']['single_ratio']:.1%}，"
          f"邻居同类比例 {dash['similarity']['same_category_ratio']:.1%}")

    sample = check(
        requests.get(f"{BASE_URL}/api/dashboard/similarity-sample?limit=5", timeout=30),
        "dashboard/similarity-sample",
    )
    assert sample["neighbors"] and all(n["item_id"] != sample["base"]["item_id"] for n in sample["neighbors"])
    print(f"  相似度样例: {sample['base']['title'][:50]} → Top-1 {sample['neighbors'][0]['score']:.3f}")

    hist = check(
        requests.get(f"{BASE_URL}/api/users/cold_start_user/history", timeout=10),
        "user/history",
    )
    print(f"  cold_start_user 足迹条数: {hist['total']}，最新一条时间戳: {hist['items'][0]['timestamp']}")

    hr("8. 重置 cold_start_user 测试账号（恢复为 0 条行为，方便前端演示冷启动）")
    rst = check(
        requests.delete(f"{BASE_URL}/api/users/cold_start_user/interactions", timeout=10),
        "reset/cold_start_user",
    )
    print(f"  重置结果: {rst}")


if __name__ == "__main__":
    main()
