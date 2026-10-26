# 路由汇总：7 组 routers（auth / subscriptions / unread / followup / qa / settings / admin）
from app.routers import admin, auth, followup, qa, settings, subscriptions, unread

ALL_ROUTERS = [
    auth.router,
    subscriptions.router,
    unread.router,
    followup.router,
    qa.router,
    settings.router,
    admin.router,
]
