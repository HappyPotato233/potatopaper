# 应用入口：FastAPI 实例 + 启动初始化（建库建表/管理员/模型引导）+ worker/调度器 + 前端托管
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import database as db
from app.routers import ALL_ROUTERS


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：初始化数据库 → 注册队列 handler → 启动 worker 与调度器"""
    db.init_database()

    from worker.pipeline import run_followup
    from worker.queue import task_queue
    from worker.scheduler import scheduler

    task_queue.register_handler("follow_up", run_followup)
    task_queue.start()
    scheduler.start()
    print("[main] 文献雷达后端已就绪: http://127.0.0.1:8000 （文档: /docs）")
    yield
    scheduler.stop()


app = FastAPI(title="文献雷达 · 科研文献持续跟踪助手", version="1.0.0", lifespan=lifespan)

# CORS（前端 Vite dev server 独立端口调试用）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in ALL_ROUTERS:
    app.include_router(router)


@app.get("/api/health")
def health():
    """健康检查（前端启动探测）"""
    return {"status": "ok", "service": "literature-radar"}


# ---------- 前端 SPA 托管（vite build 产物；开发模式走 5173 独立端口） ----------
WEB_DIST = Path(__file__).resolve().parent / "web" / "dist"
if WEB_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        """SPA 路由回退：非 /api 路径统一回 index.html（前端路由接管）"""
        if full_path.startswith("api/"):
            return FileResponse(WEB_DIST / "index.html", status_code=404)
        target = WEB_DIST / full_path
        if target.is_file():
            return FileResponse(target)
        return FileResponse(WEB_DIST / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
