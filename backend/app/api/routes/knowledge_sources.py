"""已认证用户查看引用所指向的原始 Markdown 文档。"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from app.api.deps import current_actor
from app.config.rag import rag_settings
from app.persistence.mysql.knowledge import get_vectorized_chunks_by_ids


router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("/source/{chunk_id}", response_class=PlainTextResponse)
def source_document(chunk_id: str, _actor: dict = Depends(current_actor)) -> PlainTextResponse:
    """从权威 chunk 定位文件，且只允许读取配置知识目录内的 Markdown。"""
    chunks = get_vectorized_chunks_by_ids([chunk_id])
    if not chunks or chunks[0].source_type != "markdown" or not chunks[0].source_path:
        raise HTTPException(status_code=404, detail="来源原文不可用")
    root = rag_settings.knowledge_directory.resolve()
    path = (root / chunks[0].source_path).resolve()
    if not path.is_relative_to(root) or path.suffix.lower() != ".md" or not path.is_file():
        raise HTTPException(status_code=404, detail="来源原文不可用")
    return PlainTextResponse(Path(path).read_text(encoding="utf-8"))
