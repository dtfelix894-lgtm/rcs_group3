@echo off
REM ============================================================================
REM 脚本名称：推荐系统演示.bat
REM 项目名称：智选电商 - 个性化推荐系统 (PostgreSQL + FastAPI + Vue 3)
REM ============================================================================
REM 【使用方法】
REM   1. 双击运行：在资源管理器中直接双击 推荐系统演示.bat 即可一键启动。
REM   2. 命令行运行：在 CMD 或 PowerShell 中执行 .\推荐系统演示.bat。
REM   3. 停止服务：在终端窗口中按 Ctrl+C 或直接关闭终端窗口。
REM
REM 【脚本自动执行流程】
REM   1. 自动切换工作目录 (CD) 至 D:\PycharmProjects\rcs_group3
REM   2. 自动检测端口 8000 冲突并清理旧占用进程（或自动切换至可用端口）
REM   3. 同时启动项目后端 FastAPI 服务与前端 Vue 3 电商界面
REM   4. 待前后端服务全部就绪后，自动调用系统默认浏览器打开首页
REM ============================================================================

if exist "D:\PycharmProjects\rcs_group3" (
    cd /d "D:\PycharmProjects\rcs_group3"
) else (
    cd /d "%~dp0"
)

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
python "推荐系统演示.py"
exit /b %ERRORLEVEL%
