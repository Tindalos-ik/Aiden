from fastapi import FastAPI

from app.api.routes.auth import router as auth_router
from app.api.routes.conversations import router as conversations_router
from app.api.routes.knowledge_sources import router as knowledge_sources_router
from app.api.routes.rag_admin import router as rag_admin_router
from app.api.routes.staff import router as staff_router
from app.api.routes.topics import router as topics_router
from app.api.routes.observability import router as observability_router
from app.api.routes.service_workflow import user_router as service_router, staff_router as staff_service_router
from app.services.rag.admin_console import stop_owned_embedding_on_shutdown


# 应用导入阶段只注册路由；不连接 MySQL、不建表。表结构须先由 Alembic 升级。
app = FastAPI(title="Aiden local support API", version="0.1.0")
app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(knowledge_sources_router)
app.include_router(rag_admin_router)
app.include_router(staff_router)
app.include_router(topics_router)
app.include_router(observability_router)
app.include_router(service_router)
app.include_router(staff_service_router)


@app.on_event("shutdown")
def shutdown_owned_embedding() -> None:
    """仅清理当前 API 进程亲自启动的本地向量子进程。"""
    stop_owned_embedding_on_shutdown()


@app.get("/api/health")
def health() -> dict[str, str]:
    """提供进程存活检查；该轻量端点不代表数据库或模型已经可用。"""
    return {"status": "ok"}
