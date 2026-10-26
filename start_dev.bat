@echo off
chcp 65001 >nul
echo ================================================
echo  文献雷达 · 开发模式（前后端同时启动）
echo  后端: http://127.0.0.1:8000   前端: http://localhost:5173
echo  改前端代码请访问 5173（热更新），关闭两个窗口即停止
echo ================================================
start "backend-8000" cmd /k "cd /d %~dp0 && D:\Anaconda\envs\shixi\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000"
start "frontend-5173" cmd /k "cd /d %~dp0web && npm run dev"
