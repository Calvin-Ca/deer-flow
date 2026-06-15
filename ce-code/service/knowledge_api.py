"""知识服务 HTTP API（常驻 FastAPI，端口 8100）—— 承旧 ``retrieval/server.py``。

职责单一：**对底层知识库提供检索原语 HTTP 端点**（裸检索 / 引用扩展 / 单条直取）。检索编排在
``service.retrieve_service``，本文件只做 HTTP ↔ 调用翻译 + 异常→状态码映射。生成（问答）与合规编排
在 ``ce-services`` 任务层，作本服务的纯 HTTP 客户端。

**对外契约逐字保持**（ce-services 依赖）：
  GET  /health                       健康检查（含已就绪 standard 列表）
  POST /search                       裸检索（clauses[] + meta）
  POST /expand                       对 node_path 做引用图扩展
  GET  /clause/{standard}/{path}     单条款直取

启动（服务器，从 ce-code 根，模块式）：
  cd /mnt/nvme/calvin/code/deer-flow/ce-code
  .venv/bin/python -m service.knowledge_api
  # 或： .venv/bin/python -m uvicorn service.knowledge_api:app --host 0.0.0.0 --port 8100
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from config import DEFAULTS, STANDARD_ALIASES
from ir.query import RetrievalQuery
from service.retrieve_service import KnowledgeRetrieveService

# data/ 路径由本文件位置推出（service/knowledge_api.py → 上两级即 ce-code 根），与 cwd 无关
_ROOT = Path(__file__).resolve().parent.parent
_VECTOR_STORE = _ROOT / "data" / "vector_store"

_service = KnowledgeRetrieveService(_VECTOR_STORE)

app = FastAPI(title="Building Code RAG · Retrieval Service", version="3.0.0")


# ── 请求模型 ──────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = Field(..., description="自然语言查询")
    standard: str = "gb50016"
    top_k: int = DEFAULTS["top_k"]
    skip_rerank: bool = False


class ExpandRequest(BaseModel):
    node_paths: list[str] = Field(..., description="种子条款号列表")
    standard: str = "gb50016"


# ── 端点（异常映射：未知代号→400、store 未就绪→503，在各端点内 try/except 做）──────

@app.get("/health")
def health() -> dict:
    ready = sorted(
        {name for name in set(STANDARD_ALIASES.values())
         if (_VECTOR_STORE / name).exists()}
    )
    return {
        "status": "ok",
        "service": "retrieval",
        "ready_standards": ready,
        "vector_store": str(_VECTOR_STORE),
        "deps": {k: DEFAULTS[k] for k in ("embed_url", "llm_url", "milvus_host", "milvus_port")},
    }


@app.post("/search")
def search_endpoint(req: SearchRequest) -> dict:
    """裸检索：只返回条款 + meta，不做生成。供算量/审图等 agent 自由组合。"""
    query = RetrievalQuery(text=req.query, standard=req.standard,
                           top_k=req.top_k, skip_rerank=req.skip_rerank)
    try:
        return _service.search(query)
    except ValueError as exc:                       # 未知规范代号
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:                # store 未就绪
        raise HTTPException(
            status_code=503,
            detail=(f"向量索引未就绪（{exc} 不存在）。这是服务端配置问题，请在服务器上构建索引："
                    f"python build.py --input data/parsed/.../*_content_list.json"),
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"检索失败: {exc}") from exc


@app.post("/expand")
def expand_endpoint(req: ExpandRequest) -> dict:
    """对给定 node_path 做一跳引用图扩展，返回新增的关联条款。"""
    try:
        return _service.expand(req.node_paths, req.standard)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"向量索引未就绪（{exc} 不存在）") from exc


@app.get("/clause/{standard}/{node_path}")
def clause_endpoint(standard: str, node_path: str) -> dict:
    """按条款号直取单条款。"""
    try:
        resp = _service.get_clause(standard, node_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"向量索引未就绪（{exc} 不存在）") from exc
    if resp["clause"] is None:
        raise HTTPException(status_code=404, detail=f"条款 {node_path} 不存在于 {resp['standard']}")
    return resp


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8100)
