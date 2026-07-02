"""Rerank 微服务（常驻 FastAPI，端口 8098）—— 从知识层拆出的唯一 GPU 消费者。

背景：知识服务 :8100 的检索栈原本进程内加载 ``FlagReranker``（cross-encoder 精排，bge-reranker-large），
这是 :8100 唯一的本地 GPU 用途（embedding 早已独立在 :8097、Milvus/BM25 皆非 GPU）。把它拆成独立
HTTP 服务后，:8100 即纯 CPU，GPU 隔离到本服务——可独立起停、共卡、按需伸缩，与部署解耦。

契约（供 ``retrieval.hybrid_retriever.rerank`` HTTP 调用）：
  POST /rerank {query, passages:[str], normalize:bool} -> {scores:[float]}
  GET  /health

降级：模型加载失败 → /rerank 返回全 0 分（不 500，调用方据此退回 RRF 顺序，检索仍可用、只是精排失效）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from config import RERANK_DEVICE, RERANK_MODEL

logger = logging.getLogger("ce-code.rerank")

# 进程内单例（唯一 owner）：模型只加载一份，惰性加载、失败缓存不重试（承旧 hybrid_retriever 语义）。
_RERANKER = None
_RERANKER_FAILED = False


def _get_reranker():
    """惰性加载并缓存 rerank 模型（进程内一份）；加载失败缓存为「不可用」，后续不再重试。

    参数：无。返回：FlagReranker 实例或 None（加载失败）。
    devices 钉死单卡（config.RERANK_DEVICE）：否则新版 FlagReranker 默认吃光全部可见 GPU、每次
      compute_score 跨多卡 spawn 多进程池（起池 ~21s），重排几十个候选反被池开销拖成分钟级。
    """
    global _RERANKER, _RERANKER_FAILED
    if _RERANKER is not None or _RERANKER_FAILED:
        return _RERANKER
    try:
        from FlagEmbedding import FlagReranker
        _RERANKER = FlagReranker(RERANK_MODEL, use_fp16=True, devices=RERANK_DEVICE)
        logger.info("Rerank 模型已加载：%s @ %s", RERANK_MODEL, RERANK_DEVICE)
    except Exception as e:  # noqa: BLE001 —— 加载失败即降级，不让服务起不来
        logger.error("Rerank 模型加载失败（%s），/rerank 将返回全 0 分（调用方退回 RRF）", e)
        _RERANKER_FAILED = True
    return _RERANKER


class RerankRequest(BaseModel):
    """精排请求：query 与候选段落，normalize 控制分数是否归一到 [0,1]。"""

    query: str = Field(..., description="查询文本")
    passages: list[str] = Field(..., description="候选段落文本列表（与调用方行序一一对应）")
    normalize: bool = Field(True, description="是否把 cross-encoder 分数 sigmoid 归一到 [0,1]")


class RerankResponse(BaseModel):
    """精排响应：与 passages 同序的分数列表。"""

    scores: list[float] = Field(..., description="与 passages 一一对应的精排分（降序在调用方排）")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动即预加载模型：避免第一个 /rerank 冷启动慢（首推理编译 CUDA kernel）导致调用方超时降级。

    加载在 healthcheck ``start_period``（40s）窗口内完成；加载失败不阻断启动（/rerank 走全 0 降级）。
    """
    _get_reranker()
    yield


app = FastAPI(title="CE Rerank Service", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    """健康检查：报模型/设备与加载状态（loaded/failed）。"""
    return {
        "status": "ok", "service": "rerank",
        "model": RERANK_MODEL, "device": RERANK_DEVICE,
        "loaded": _RERANKER is not None, "failed": _RERANKER_FAILED,
    }


@app.post("/rerank", response_model=RerankResponse)
def do_rerank(req: RerankRequest) -> dict:
    """cross-encoder 精排打分。

    参数：req —— RerankRequest（query / passages / normalize）。
    返回：``{scores}``——与 passages 同序。模型不可用或空 passages → 全 0 分（不 500，调用方退回 RRF）。
    """
    reranker = _get_reranker()
    if reranker is None or not req.passages:
        return {"scores": [0.0] * len(req.passages)}
    pairs = [[req.query, p] for p in req.passages]
    scores = reranker.compute_score(pairs, normalize=req.normalize)
    if not isinstance(scores, list):  # 单条候选时 FlagReranker 返回标量
        scores = [scores]
    return {"scores": [float(s) for s in scores]}


if __name__ == "__main__":
    import os

    import uvicorn
    # 端口 env 可配（默认 8095；:8098 在部分机器被占）——与 config.RERANK_URL / compose 保持一致。
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("CE_RERANK_PORT", "8095")))
