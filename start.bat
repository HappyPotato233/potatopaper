@echo off
chcp 65001 >nul
cd /d %~dp0
echo ================================================
echo  文献雷达 · 科研文献持续跟踪助手
echo  浏览器访问: http://127.0.0.1:8000  (账号 admin/admin123)
echo  按 Ctrl+C 停止服务
echo ================================================
echo.
REM 延迟 4 秒打开浏览器（等后端就绪）
start "" /b cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"
D:\Anaconda\envs\shixi\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000

REM 服务退出后窗口保留，显示退出原因（如端口被占用），按任意键再关窗
echo.
echo ================================================
echo  服务已停止。如果上方有报错（如端口被占用 error 10048），
echo  请先结束占用 8000 端口的旧进程后重新运行本脚本。
echo ================================================
pause >nul
