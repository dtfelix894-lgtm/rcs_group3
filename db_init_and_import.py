"""
阶段一：PostgreSQL 建库建表与高速导入

流程：
  1. 连接默认 postgres 库（autocommit），若 recsys_db 不存在则自动创建
  2. DROP ... CASCADE 后重建 4 张核心表（先只建主键/唯一约束，外键与二级索引在导入后再建，导入更快）
  3. 深度清洗 CSV（修复编码/HTML 实体、剔除无意义/乱码标题、剔除混入 All Beauty 的非美妆商品）
     -> 同步更新 clean_products.csv / clean_ratings.csv
     -> execute_values(page_size=5000) 批量导入 products / users / interactions
  4. 导入完成后创建外键、二级索引，并 ANALYZE
"""
import html
import re
import time

import numpy as np
import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

import config

PAGE_SIZE = 5000          # execute_values 每页行数
COMMIT_EVERY = 50_000     # 每导入多少行提交一次
PRICE_MAX = 99_999_999.99  # NUMERIC(10,2) 上限

# ----------------------------------------------------------------------------
# 美妆与个护领域清洗规则（剔除源数据中误标为 All Beauty 的非美妆商品与脏数据）
# ----------------------------------------------------------------------------
# 1. 核心美妆/个护保护词（用于在通用规则下保护美甲贴纸、双眼皮贴、假发胶带、化妆包、卷发棒等）
BEAUTY_GUARD_RE = re.compile(
    r"\b("
    r"nail|nails|manicure|pedicure|cuticle|acrylic|gel polish|"
    r"eyelash|eyelashes|lash|lashes|eyebrow|brow|eyeliner|eyeshadow|mascara|eye makeup|eyelid|"
    r"makeup|cosmetic|cosmetics|lipstick|lip gloss|lip balm|lip liner|foundation|concealer|blush|bronzer|highlighter|primer|contour|powder puff|"
    r"skincare|skin care|facial|face mask|sheet mask|peel|cleanser|cleansing|moisturizer|moisturizing|lotion|cream|serum|toner|acne|pimple|blackhead|wrinkle|retinol|hyaluronic|niacinamide|salicylic|sunscreen|spf|"
    r"hair|hairs|haircare|shampoo|conditioner|hairspray|pomade|mousse|wig|wigs|toupee|hairpiece|braid|braiding|locs|dreadlock|curling|straightener|flat iron|hair dryer|blow dryer|comb|hairbrush|bobby pin|hairpin|hair clip|claw clip|barrette|scrunchie|hair band|hair tie|hair bow|durag|headwrap|bonnet|"
    r"barber|clipper|trimmer|shaver|shaving|razor|aftershave|beard|mustache|waxing|epilator|depilatory|tweezers|dermaplaning|"
    r"perfume|fragrance|cologne|eau de|body mist|body spray|deodorant|antiperspirant|essential oil|aromatherapy|"
    r"bath|shower|soap|body wash|bath bomb|loofah|washcloth|exfoliating|"
    r"tooth|teeth|toothbrush|tootbrush|toothpaste|mouthwash|floss|dental|whitening|breath|tongue scraper|denture|"
    r"tattoo|microblading|piercing|henna|body paint|face paint"
    r")\b"
)

# 2. 强非美妆黑名单（即使标题含瓶贴/宠物香波/发胶电影DVD/米奇耳朵发箍也直接剔除）
HARD_NON_BEAUTY_RE = re.compile(
    r"\b("
    r"stickers? per roll|labels? per roll|sticker labels?|circle dot|color coding|thank you stickers?|thank you labels?|"
    r"address labels?|shipping labels?|mailing labels?|barcode labels?|chalkboard.*labels?|kraft.*stickers?|kraft.*labels?|adhesive labels?|"
    r"note cards?|greeting cards?|postcards?|index cards?|business cards?|playing cards?|tarot cards?|flash cards?|self seal envelopes?|reference charts?|"
    r"microwave|oven|refrigerator|fridge|freezer|dishwasher|washing machine|clothes dryer|grease filter|furnace filter|oil filter|fuel filter|cabin filter|vacuum filter|pool filter|water filter|hepa filter|"
    r"lawn mower|chainsaw|weed eater|leaf blower|snow blower|carburetor|spark plug|ignition coil|air compressor|generator|"
    r"bark deterrents?|for dogs?|for cats?|for pets?|dog grooming|cat grooming|pet grooming|dog shampoo|cat shampoo|puppy shampoo|pet shampoo|"
    r"dog paw|paw print|dog collar|dog leash|dog harness|cat litter|litter box|poop bags?|flea and tick|flea collar|chew toys?|catnip|scratching post|bird cage|aquarium|fish tank|equine|for horses?|livestock|farm animals?|"
    r"two-disc|single-disc|blu-ray|\bdvd\b|\bvhs\b|audio cd|vinyl lp|paperback|hardcover|coloring book|activity book|textbook|cookbook|dictionary|encyclopedia|audiobook|bible expositor|wimmelbuch|address book|"
    r"mouse ears|minnie ears|mickey ears|cosplay costume|fancy dress|halloween costume|mascot costume|inflatable costume|party supplies|party favors|birthday banner|balloon garland|latex balloons|foil balloons|cake topper|cupcake wrapper|tablecloth|"
    r"stuffed animal|plush toy|action figure|jigsaw puzzle|board game|"
    r"iphone|ipad|airpods|macbook|samsung galaxy|phone case|screen protector|hdmi cable|ethernet cable|car charger|gaming mouse|laptop sleeve|router|modem|remote control|workout headphones|"
    r"coffee mug|travel mug|wine glass|shot glass|beer glass|baking sheet|cake pan|muffin tin|cookie cutter|rolling pin|cutting board|chef knife|frying pan|skillet|saucepan|dutch oven|slow cooker|instant pot|air fryer|coffee maker|espresso|french press|tea kettle|toaster|"
    r"dish soap|dishwasher pods|laundry detergent|fabric softener|dryer sheets|floor cleaner|toilet cleaner|trash bags?|garbage bags?|boot tray|"
    r"coffee beans|ground coffee|tea bags|whey protein|pre-workout|trying for a baby|prenatal vitamin|seasoning|spice rub|hot sauce|maple syrup|chewing gum|"
    r"screwdriver|torque wrench|socket set|drill bits?|saw blades?|sandpaper|soldering iron|multimeter|tape measure|allen wrench|hex key|"
    r"wiper blades?|brake pads?|headlight|taillight|tire gauge|jumper cables?|motor oil|light bulbs?|led bulbs?|extension cord|power strip|surge protector|circuit breaker|"
    r"continuous ribbon|yards.*ribbon|satin ribbon.*yards|polyester.*ribbon|comfort gel pack|"
    r"not available|old discontinued listing deleted|do not buy|test listing"
    r")\b"
)

# 3. 通用非美妆品类词（当标题不含任何 BEAUTY_GUARD_RE 核心美妆词时剔除）
SOFT_NON_BEAUTY_RE = re.compile(
    r"\b("
    r"stickers?|labels?|envelopes?|ribbons?|cards?|pens?|pencils?|markers?|binders?|folders?|staplers?|notebooks?|journals?|planners?|calendars?|boxes|mailers?|tape|"
    r"filters?|vacuums?|pumps?|valves?|hoses?|nozzles?|faucets?|toilets?|bidet|plumbing|hardware|tools?|wrenches?|pliers|hammers?|screws?|bolts?|nuts?|brackets?|hinges?|locks?|"
    r"dogs?|cats?|pets?|puppies|kittens|animals?|horses?|birds?|fishes|"
    r"costumes?|party|balloons?|banners?|toys?|games?|puzzles?|dolls?|figurines?|"
    r"books?|movies?|films?|music|albums?|dvds?|cds?|"
    r"phones?|tablets?|laptops?|computers?|cameras?|cables?|chargers?|adapters?|batteries|speakers?|headphones?|earbuds|keyboards?|"
    r"kitchen|cooking|baking|mugs?|cups?|glasses|plates?|bowls?|knives|forks?|spoons?|pans?|pots?|"
    r"shirts?|pants?|jeans|jackets?|coats?|sweaters?|hoodies?|dresses|skirts?|shorts|socks?|pantyhose|tights|leggings|shoes?|boots?|sneakers?|sandals?|slippers?|"
    r"necklaces?|bracelets?|earrings?|rings?|watches|sunglasses|eyeglasses|keychains?|lanyards?|"
    r"curtains?|drapes|rugs?|mats?|blankets?|bedding|bed pads?|sheets?|frames?|vases?|candles?|lamps?|bulbs?|"
    r"automotive|cars?|trucks?|bikes?|bicycles?|motorcycles?|camping|fishing|hunting|golf|tennis|football|basketball|baseball|soccer|yoga|fitness|gym|weights?|dumbbells?"
    r")\b"
)

MOJIBAKE_MAP = {
    "â€™": "'",
    "â€˜": "'",
    "â€œ": '"',
    "â€\x9d": '"',
    "â€“": "-",
    "â€”": "-",
    "â€¦": "...",
    "â€": "'",
    "Â®": "®",
    "Â°": "°",
    "Â½": "1/2",
    "Ã©": "é",
    "Ã¨": "è",
    "Ã¢": "â",
}

# 美妆子分类派生规则（按顺序匹配标题，命中即停止；原始 category 只有 All Beauty / Premium Beauty 两个值）
SUB_CATEGORY_RULES = [
    ("美甲", r"\b(nail|nails|manicure|pedicure|cuticle|nail polish|gel polish|lacquer|acrylic powder|press[- ]on)\b"),
    ("口腔护理", r"\b(tooth|teeth|toothbrush|tootbrush|toothpaste|mouthwash|floss|flosser|dental|oral|denture|whitening strips?|tongue scraper)\b"),
    ("香水香氛", r"\b(perfume|parfum|fragrance|cologne|eau de|body mist|attar|aromatherapy|essential oils?)\b"),
    ("剃须脱毛", r"\b(shaver|shaving|shave|razors?|aftershave|beard|mustache|moustache|epilator|depilatory|hair removal|waxing|wax strips?|trimmer|clippers?|barber)\b"),
    ("假发发饰", r"\b(wigs?|toupee|hairpiece|hair extensions?|extensions|braid(ing)? hair|crochet hair|ponytail|headbands?|scrunchies?|hair clips?|claw clips?|barrettes?|hair bows?|bobby pins?|hairpins?|hair ties?|durag|bonnet|headwrap)\b"),
    ("美发护发", r"\b(hair|shampoo|conditioner|scalp|hairspray|pomade|curl|curls|curly|dandruff|keratin|flat iron|straightener|curling|blow dryer|hair dryer|combs?|hairbrush|detangl\w*|hair dye|hair color)\b"),
    ("彩妆", r"\b(makeup|make-up|lipstick|lip gloss|lip liner|lip stain|eyeliner|eyeshadow|eye shadow|mascara|foundation|concealer|blush|bronzer|highlighter|contour|palette|primer|setting spray|eyebrow|brow|lash|lashes|eyelash|eyelashes|kajal|kohl|cosmetic brush|makeup brush|beauty blender|sponge)\b"),
    ("身体沐浴", r"\b(bath|shower|body wash|soap|bath bomb|body lotion|body butter|body oil|body scrub|deodorant|antiperspirant|loofah|hand cream|foot|feet|hand wash)\b"),
    ("护肤", r"\b(skin|skincare|face|facial|serum|moisturi[sz]er|moisturi[sz]ing|cream|lotion|cleanser|toner|mask|masks|acne|pimple|sunscreen|spf|retinol|hyaluronic|niacinamide|wrinkle|anti-aging|eye cream|lip balm|chapstick|peel|exfoliat\w*)\b"),
]
SUB_CATEGORY_DEFAULT = "美容工具及其他"


def derive_sub_category(titles: pd.Series) -> pd.Series:
    low = titles.astype(str).str.lower()
    result = pd.Series(SUB_CATEGORY_DEFAULT, index=titles.index, dtype=object)
    assigned = pd.Series(False, index=titles.index)
    for name, pat in SUB_CATEGORY_RULES:
        hit = ~assigned & low.str.contains(pat, regex=True)
        result[hit] = name
        assigned |= hit
    return result


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# 1. 建库
# ----------------------------------------------------------------------------
def ensure_database():
    conn = psycopg2.connect(**config.get_db_params("postgres"))
    conn.autocommit = True  # CREATE DATABASE 不能在事务块中执行
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (config.DB_NAME,))
            if cur.fetchone():
                log(f"数据库 {config.DB_NAME} 已存在")
            else:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(config.DB_NAME)))
                log(f"数据库 {config.DB_NAME} 创建成功")
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# 2. 建表（外键与二级索引延后创建）
# ----------------------------------------------------------------------------
DDL_TABLES = """
DROP TABLE IF EXISTS item_similarity CASCADE;
DROP TABLE IF EXISTS interactions CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS products CASCADE;

CREATE TABLE products (
    item_id VARCHAR(32) PRIMARY KEY,
    title TEXT NOT NULL,
    price NUMERIC(10, 2),
    avg_rating NUMERIC(3, 2) DEFAULT 0.0,
    rating_num INTEGER DEFAULT 0,
    category VARCHAR(255),
    sub_category VARCHAR(32),          -- 由标题关键词派生的美妆子分类（原始 category 仅 2 个取值）
    description TEXT,
    image_url TEXT NOT NULL,
    hot_score NUMERIC(10, 4) DEFAULT 0.0
);

CREATE TABLE users (
    user_id VARCHAR(64) PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE interactions (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    item_id VARCHAR(32) NOT NULL,
    rating NUMERIC(2, 1) NOT NULL,
    action_type VARCHAR(16) DEFAULT 'rate',  -- 最近一次行为: rate / fav / unfav
    is_fav BOOLEAN NOT NULL DEFAULT FALSE,   -- 收藏状态，与评分相互独立
    timestamp BIGINT NOT NULL,               -- 毫秒级时间戳
    UNIQUE (user_id, item_id)
);

CREATE TABLE item_similarity (
    item_id VARCHAR(32) PRIMARY KEY,
    similar_items JSONB NOT NULL
);
"""

DDL_POST_LOAD = """
ALTER TABLE interactions
    ADD CONSTRAINT fk_interactions_user FOREIGN KEY (user_id)
        REFERENCES users(user_id) ON DELETE CASCADE,
    ADD CONSTRAINT fk_interactions_item FOREIGN KEY (item_id)
        REFERENCES products(item_id) ON DELETE CASCADE;

ALTER TABLE item_similarity
    ADD CONSTRAINT fk_item_similarity_item FOREIGN KEY (item_id)
        REFERENCES products(item_id) ON DELETE CASCADE;

CREATE INDEX idx_products_hot_score ON products(hot_score DESC);
CREATE INDEX idx_products_category ON products(category);
CREATE INDEX idx_products_sub_category_hot ON products(sub_category, hot_score DESC);
CREATE INDEX idx_interactions_user_time ON interactions(user_id, timestamp DESC);
CREATE INDEX idx_interactions_item ON interactions(item_id);
"""


def create_tables(conn):
    with conn.cursor() as cur:
        cur.execute(DDL_TABLES)
    conn.commit()
    log("4 张核心表已重建（外键/二级索引将在导入后创建）")


# ----------------------------------------------------------------------------
# 3. 数据清洗
# ----------------------------------------------------------------------------
def _none_if_nan(series: pd.Series) -> list:
    """将 Series 转为原生 Python 列表，NaN / pd.NA -> None。"""
    return [None if pd.isna(v) else v for v in series.astype(object).tolist()]


def _clean_text_val(val):
    if not isinstance(val, str):
        return None
    s = html.unescape(val)
    for bad, good in MOJIBAKE_MAP.items():
        if bad in s:
            s = s.replace(bad, good)
    s = s.strip()
    return s if s else None


def _blank_to_none(series: pd.Series) -> pd.Series:
    """修复 HTML 实体与常见 UTF-8 乱码，去除首尾空白，空串/NaN 统一为 None（object dtype）。"""
    return series.astype(object).map(_clean_text_val)


def load_products(sync_csv: bool = True) -> pd.DataFrame:
    df = pd.read_csv(config.PRODUCTS_CSV, dtype={"item_id": str})
    n_raw = len(df)
    log(f"products.csv 原始规模: {df.shape}, 字段: {list(df.columns)}")
    log(f"  缺失值统计: {df.isna().sum().to_dict()}")

    df["item_id"] = df["item_id"].astype(str).str.strip()
    df["title"] = _blank_to_none(df["title"])
    df["image_url"] = _blank_to_none(df["image_url"])
    df["category"] = _blank_to_none(df["category"])
    df["description"] = _blank_to_none(df["description"])

    # 1) 基础必填与主键去重
    df = df[df["item_id"].ne("") & df["title"].notna() & df["image_url"].notna()]
    df = df.drop_duplicates(subset="item_id", keep="first")

    # 2) 剔除乱码/占位符/过短无意义标题 与 误标为 All Beauty 的非美妆商品
    title_str = df["title"].astype(str)
    title_low = title_str.str.lower()

    invalid_title = (
        (title_str.str.len() < 4)
        | (~title_str.str.contains(r"[a-zA-Z]{2,}", regex=True))
        | title_str.str.match(r"^[0-9a-zA-Z]{10,}$")
    )
    hard_nb = title_low.str.contains(HARD_NON_BEAUTY_RE)
    soft_nb = title_low.str.contains(SOFT_NON_BEAUTY_RE) & ~title_low.str.contains(BEAUTY_GUARD_RE)
    remove_mask = invalid_title | hard_nb | soft_nb

    n_invalid = int(invalid_title.sum())
    n_hard = int((hard_nb & ~invalid_title).sum())
    n_soft = int((soft_nb & ~invalid_title & ~hard_nb).sum())
    n_removed = int(remove_mask.sum())
    if n_removed > 0:
        log(
            f"  领域与质量过滤: 共剔除 {n_removed} 条 "
            f"(异常/乱码标题 {n_invalid} 条, 强非美妆特征 {n_hard} 条, 泛非美妆品类 {n_soft} 条)"
        )
    df = df[~remove_mask].copy()

    # 3) 数值字段规范化
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df.loc[(df["price"] < 0) | (df["price"] > PRICE_MAX), "price"] = np.nan
    df["price"] = df["price"].round(2)

    df["avg_rating"] = pd.to_numeric(df["avg_rating"], errors="coerce").fillna(0.0).clip(0, 5).round(2)
    df["rating_num"] = pd.to_numeric(df["rating_num"], errors="coerce").fillna(0).clip(lower=0).astype(np.int64)
    df["category"] = df["category"].map(lambda v: v[:255] if isinstance(v, str) else None)
    df["sub_category"] = derive_sub_category(df["title"])
    log(f"  子分类分布: {df['sub_category'].value_counts().to_dict()}")

    df = df.reset_index(drop=True)
    log(f"products 清洗后: {len(df)} 条 (较原始 {n_raw} 条净减少 {n_raw - len(df)} 条)")

    if sync_csv and len(df) != n_raw:
        df.to_csv(config.PRODUCTS_CSV, index=False, encoding="utf-8")
        log(f"  已将清洗后的商品数据同步写回 {config.PRODUCTS_CSV}")

    return df


def load_ratings(valid_items: set, sync_csv: bool = True) -> pd.DataFrame:
    df = pd.read_csv(config.RATINGS_CSV, dtype={"user_id": str, "item_id": str})
    n_raw = len(df)
    log(f"ratings.csv 原始规模: {df.shape}, 字段: {list(df.columns)}")

    df["user_id"] = df["user_id"].astype(str).str.strip()
    df["item_id"] = df["item_id"].astype(str).str.strip()
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["user_id", "item_id", "rating", "timestamp"])
    df = df[(df["user_id"] != "") & (df["user_id"].str.len() <= 64)]
    df["rating"] = df["rating"].clip(0, 9.9).round(1)
    df["timestamp"] = df["timestamp"].astype(np.int64)

    n0 = len(df)
    # 按 (user_id, item_id) 去重，保留 timestamp 最大（最新）的一条
    df = df.sort_values("timestamp").drop_duplicates(subset=["user_id", "item_id"], keep="last")
    n1 = len(df)
    # 仅保留 products 中存在的商品，确保外键不冲突且剔除非美妆商品的交互
    df = df[df["item_id"].isin(valid_items)]
    n2 = len(df)
    log(f"ratings 清洗: 有效 {n0} -> 去重后 {n1} -> 商品外键过滤后 {n2} (剔除非美妆/无效商品交互 {n1 - n2} 条)")

    df = df.reset_index(drop=True)
    if sync_csv and len(df) != n_raw:
        df.to_csv(config.RATINGS_CSV, index=False, encoding="utf-8")
        log(f"  已将清洗后的交互数据同步写回 {config.RATINGS_CSV}")

    return df


# ----------------------------------------------------------------------------
# 4. 批量导入
# ----------------------------------------------------------------------------
def bulk_insert(conn, insert_sql: str, rows: list, name: str, template: str = None):
    total = len(rows)
    t0 = time.time()
    with conn.cursor() as cur:
        for start in range(0, total, COMMIT_EVERY):
            chunk = rows[start:start + COMMIT_EVERY]
            execute_values(cur, insert_sql, chunk, template=template, page_size=PAGE_SIZE)
            conn.commit()
            log(f"  {name}: {min(start + COMMIT_EVERY, total)}/{total}")
    log(f"{name} 导入完成，共 {total} 条，耗时 {time.time() - t0:.1f}s")


def import_products(conn, df: pd.DataFrame):
    rows = list(zip(
        df["item_id"].tolist(),
        df["title"].tolist(),
        _none_if_nan(df["price"]),
        df["avg_rating"].astype(float).tolist(),
        df["rating_num"].astype(np.int64).tolist(),
        _none_if_nan(df["category"]),
        df["sub_category"].tolist(),
        _none_if_nan(df["description"]),
        df["image_url"].tolist(),
    ))
    bulk_insert(
        conn,
        "INSERT INTO products (item_id, title, price, avg_rating, rating_num, "
        "category, sub_category, description, image_url) VALUES %s",
        rows, "products",
    )


def import_users(conn, ratings: pd.DataFrame):
    user_ids = pd.unique(ratings["user_id"]).tolist()
    rows = [(uid, f"User_{uid[-6:]}") for uid in user_ids]
    bulk_insert(conn, "INSERT INTO users (user_id, username) VALUES %s", rows, "users")

    # 额外插入冷启动测试账号（无任何历史行为）
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (config.COLD_START_USER_ID, config.COLD_START_USERNAME),
        )
        inserted = cur.rowcount
    conn.commit()
    if inserted:
        log(f"冷启动测试账号已插入: {config.COLD_START_USER_ID}")
    else:
        log(f"[警告] user_id={config.COLD_START_USER_ID} 已存在于真实数据中，未覆盖")


def import_interactions(conn, ratings: pd.DataFrame):
    rows = list(zip(
        ratings["user_id"].tolist(),
        ratings["item_id"].tolist(),
        ratings["rating"].astype(float).tolist(),
        # 注意：Windows 下 numpy 的 int 为 int32，毫秒时间戳必须显式使用 int64，否则溢出
        ratings["timestamp"].astype(np.int64).tolist(),
    ))
    bulk_insert(
        conn,
        "INSERT INTO interactions (user_id, item_id, rating, action_type, timestamp) VALUES %s",
        rows, "interactions", template="(%s, %s, %s, 'rate', %s)",
    )


def post_load(conn):
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(DDL_POST_LOAD)
    conn.commit()
    log(f"外键与二级索引创建完成，耗时 {time.time() - t0:.1f}s")

    old = conn.autocommit
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("ANALYZE products; ANALYZE users; ANALYZE interactions; ANALYZE item_similarity;")
    conn.autocommit = old
    log("ANALYZE 完成")


def main():
    t_start = time.time()
    ensure_database()

    products = load_products(sync_csv=True)
    ratings = load_ratings(set(products["item_id"]), sync_csv=True)

    conn = psycopg2.connect(**config.get_db_params())
    try:
        create_tables(conn)
        import_products(conn, products)
        import_users(conn, ratings)
        import_interactions(conn, ratings)
        post_load(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log(f"阶段一全部完成，总耗时 {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    main()
