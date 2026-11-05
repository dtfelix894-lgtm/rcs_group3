# 智选电商 · 个性化推荐系统 (PostgreSQL + FastAPI + Vue 3)

基于 PostgreSQL 17、Scikit-Learn / SciPy 稀疏矩阵离线预计算、FastAPI 实时推荐引擎与 Vue 3 + Tailwind CSS 响应式前端构建的电商个性化推荐系统。

---

## 目录结构

```text
rcs_group3/
├── app/                            # 【阶段三】在线推荐后端服务 (FastAPI)
│   ├── __init__.py
│   └── main.py                     # RESTful API 路由、连接池管理与实时推荐逻辑
├── config/                         # 【配置中心】数据库与路径统一配置
│   ├── __init__.py                 # 导出配置项，支持 `import config`
│   ├── config.example.py           # 配置文件模板（纳入 Git 版本控制）
│   └── config.py                   # 本地真实配置（已被 .gitignore 忽略）
├── data/                           # 【数据集目录】清洗后的美妆电商数据集（已被 .gitignore 忽略）
│   ├── clean_products.csv          # 商品主数据（106,293 条有效美妆商品）
│   └── clean_ratings.csv           # 用户评分交互数据（469,486 条记录）
├── docs/                           # 【项目文档目录】
│   └── 项目实践报告.md             # 完整项目实践报告（架构、技术栈、算法数学原理、踩坑与注意事项）
├── scripts/                        # 【阶段一 & 阶段二】离线数据工程与推荐算法预计算脚本
│   ├── __init__.py
│   ├── db_init_and_import.py       # 自动建库建表、非美妆脏数据过滤、派生子分类、execute_values 高速导入
│   └── build_recommender.py        # 贝叶斯加权热门得分 (hot_score) 与 Content TF-IDF + Item-CF 混合相似度预计算
├── static/                         # 【阶段四】前端静态资源
│   └── index.html                  # HTML5 + Tailwind CSS + Vue 3 单文件电商交互页面
├── tests/                          # 【验证与测试】数据库验证与 API 端到端闭环自测
│   ├── __init__.py
│   ├── verify_results.py           # 验证 4 张表数据量、Top-5 热门商品与相似推荐质量
│   └── test_api.py                 # 验证全套 API 接口与冷启动 -> 实时个性化推荐切换闭环
├── .gitignore                      # Git 忽略规则（忽略本地密码配置与大体积 CSV）
├── requirements.txt                # Python 依赖包清单
├── README.md                       # 项目架构与使用说明文档
├── 推荐系统演示.bat                # Windows 双击一键启动脚本（自动 CD、清端口、启前后端、开浏览器）
└── 推荐系统演示.py                 # Python 一键启动脚本（支持 PyCharm / 命令行一键运行）
```

---

## 快速开始

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 配置数据库连接
复制 `config/config.example.py` 为 `config/config.py`，填写本地 PostgreSQL 连接信息（或通过环境变量 `DB_HOST`、`DB_PORT`、`DB_USER`、`DB_PASSWORD`、`DB_NAME` 覆盖）。

### 3. 阶段一：建库建表与数据清洗高速导入
```bash
python scripts/db_init_and_import.py
```

### 4. 阶段二：离线推荐算法预计算
```bash
python scripts/build_recommender.py
```

### 5. 阶段三 & 四：一键启动前后端服务并打开浏览器
直接双击 `推荐系统演示.bat`，或在终端执行：
```bash
python 推荐系统演示.py
```
脚本将自动切换至项目目录、检测并清理端口冲突、启动前后端服务，并在就绪后自动打开默认浏览器访问 http://127.0.0.1:8000/。

### 6. 运行验证与接口自测
```bash
python tests/verify_results.py
python tests/test_api.py
```
