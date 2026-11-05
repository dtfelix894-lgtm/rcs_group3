# -*- coding: utf-8 -*-
"""
================================================================================
脚本名称：推荐系统演示.py
项目名称：智选电商 · 个性化推荐系统 (PostgreSQL + FastAPI + Vue 3)
================================================================================
【使用方法】
  方式一（命令行运行）：
      在任意目录下执行：
      python "D:\\PycharmProjects\\rcs_group3\\推荐系统演示.py"
      或在项目根目录下执行：
      python 推荐系统演示.py

  方式二（PyCharm 一键运行）：
      在 PyCharm 中右键点击 `推荐系统演示.py` -> 选择 `Run '推荐系统演示'`

  方式三（双击批处理启动）：
      直接双击同目录下的 `推荐系统演示.bat`（即使将该 .bat 复制到桌面也可直接双击运行）

【脚本自动化流程】
  1. 自动切换工作目录（CD）至项目根目录：D:\\PycharmProjects\\rcs_group3
  2. 自动检测 PostgreSQL 数据库端口 (5432) 与 Web 服务端口 (默认 8000)：
     - 若端口 8000 被旧进程占用，自动识别占用进程 PID 并强制清理释放端口；
     - 若系统权限限制无法释放，则自动顺延切换至可用端口（如 8001）。
  3. 同时启动项目后端（FastAPI 推荐引擎 API）与前端（Vue 3 + Tailwind 电商交互界面）。
  4. 轮询检测前后端服务就绪状态（HTTP 200 OK），全部就绪后自动打开系统默认浏览器访问首页。
  5. 按 Ctrl + C 可随时优雅停止前后端服务。
================================================================================
"""
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

# 目标项目根目录
TARGET_PROJECT_DIR = r"D:\PycharmProjects\rcs_group3"
HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DB_PORT = 5432


def log(step: str, msg: str):
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] [{step}] {msg}", flush=True)


def cd_to_project_dir():
    """1. 自动切换工作目录到目标项目文件夹。"""
    target = TARGET_PROJECT_DIR if os.path.isdir(TARGET_PROJECT_DIR) else os.path.dirname(os.path.abspath(__file__))
    os.chdir(target)
    if target not in sys.path:
        sys.path.insert(0, target)
    log("目录切换", f"已自动切换工作目录 (CD) 至: {os.getcwd()}")


def is_port_in_use(host: str, port: int) -> bool:
    """检测指定端口是否处于监听状态。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.8)
        return s.connect_ex((host, port)) == 0


def find_pids_on_port(port: int) -> list:
    """通过 netstat 查找占用指定端口的监听进程 PID 列表。"""
    pids = set()
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP":
                local_addr = parts[1]
                state = parts[3].upper()
                pid_str = parts[4]
                if local_addr.endswith(f":{port}") and state == "LISTENING" and pid_str.isdigit():
                    pid = int(pid_str)
                    if pid > 0 and pid != os.getpid():
                        pids.add(pid)
    except Exception:
        pass
    return sorted(pids)


def kill_pid(pid: int) -> bool:
    """终止冲突进程。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return True
    except Exception:
        return False


def resolve_port_conflict(preferred_port: int) -> int:
    """2. 自动检测端口并处理冲突：若被占用则自动清理占用进程，必要时自动切换备用端口。"""
    if not is_port_in_use(HOST, preferred_port):
        log("端口检测", f"目标端口 {preferred_port} 空闲可用。")
        return preferred_port

    pids = find_pids_on_port(preferred_port)
    log("端口冲突", f"检测到端口 {preferred_port} 已被占用 (占用进程 PID: {pids or '未知'})，正在自动清理...")
    for pid in pids:
        kill_pid(pid)

    # 等待端口释放（最多等待 4 秒）
    for _ in range(16):
        time.sleep(0.25)
        if not is_port_in_use(HOST, preferred_port):
            log("端口检测", f"已成功终止冲突进程并释放端口 {preferred_port}。")
            return preferred_port

    # 若仍被占用，顺延寻找可用端口
    for alt_port in range(preferred_port + 1, preferred_port + 50):
        if not is_port_in_use(HOST, alt_port):
            log("端口切换", f"原端口 {preferred_port} 仍被占用，已自动切换至可用端口 {alt_port}。")
            return alt_port

    raise RuntimeError("未能找到可用服务端口，请检查本机网络配置。")


def check_database():
    """检查 PostgreSQL 数据库端口 (5432) 是否就绪。"""
    if is_port_in_use(HOST, DB_PORT):
        log("数据库检测", f"PostgreSQL 数据库服务 (端口 {DB_PORT}) 运行正常。")
    else:
        log("数据库警告", f"未检测到端口 {DB_PORT} 上的 PostgreSQL 服务，请确认 postgresql 服务已启动。")


def wait_and_open_browser(url: str):
    """3. 轮询等待后端 API 与前端页面全部就绪后，自动打开系统默认浏览器。"""
    api_url = f"{url}/api/users/sample"
    fe_url = f"{url}/"

    for _ in range(60):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(api_url, timeout=2) as r_api, urllib.request.urlopen(fe_url, timeout=2) as r_fe:
                if r_api.status == 200 and r_fe.status == 200:
                    log("服务就绪", f"后端推荐引擎 API ({api_url}) 与前端交互界面 ({fe_url}) 均已就绪！")
                    log("浏览器启动", f"正在自动打开默认浏览器访问: {url}")
                    webbrowser.open(url)
                    return
        except Exception:
            continue

    log("超时提示", f"服务启动耗时较长，请手动在浏览器访问: {url}")


def main():
    print("=" * 78)
    print("        智选电商 · 个性化推荐系统 一键启动脚本 (推荐系统演示)")
    print("=" * 78)

    # 步骤 1：CD 到项目根目录
    cd_to_project_dir()

    # 步骤 2：检测数据库与 Web 端口冲突并自动处理
    check_database()
    port = resolve_port_conflict(DEFAULT_PORT)
    url = f"http://{HOST}:{port}"

    # 步骤 3：启动后台线程，待前后端全部就绪后自动打开默认浏览器
    threading.Thread(target=wait_and_open_browser, args=(url,), daemon=True).start()

    # 步骤 4：启动 FastAPI 后端与 Vue 3 前端托管服务
    log("服务启动", f"正在同时启动后端 FastAPI 接口服务与前端 Vue 3 电商页面 ({url}) ...")
    log("操作提示", f"首页地址: {url}  |  接口文档: {url}/docs  |  按 Ctrl+C 停止服务")

    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
