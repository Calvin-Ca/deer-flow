"""造价取数 HTTP 客户端 —— 任务层 CostAgent 够到组价取数原语的通道。

与 ``knowledge_client``（规范检索 /search /expand /clause）并列：本模块封装 ce-code 知识服务
（:8100）的**造价取数原语** /bill/match /price/compose /quota，供任务层 CostAgent 以纯 HTTP
客户端复用（与规范轨同模式——检索/取数逻辑只在知识层一份，任务层保持轻量）。

端到端组价闭环：构件描述 → ``bill_match`` 拿清单候选 → CostAgent 内 LLM 在候选内选码（红线：
只建议不定稿、HITL 复核）→ ``price_compose`` 组价。本模块只管「取数 plumbing」，选码编排在 CostAgent。

注意：/quota、/price/compose 的 region/code 在 URL path 段，含中文（如「深圳」）须百分号编码，
本模块用 ``urllib.parse.quote`` 显式编码（手敲 curl 不编码会 404，见 ce-code TODO 实测坑）。
"""
from __future__ import annotations

from datetime import date
from urllib.parse import quote

import requests

from common.config import KNOWLEDGE_URL


def bill_match(
    query: str,
    top_k: int = 10,
    structural: bool = True,
    rerank: bool = False,
    base_url: str | None = None,
    timeout: int = 120,
) -> dict:
    """打 /bill/match：构件描述 → 清单候选召回（dense 向量 + 结构约束重排）。

    参数：query —— 构件/做法自然语言描述；top_k —— 候选数；structural —— 结构约束重排（默认开）；
      rerank —— cross-encoder 精排（默认关，实测劣化短清单名）。
    返回：``{"query","count","candidates":[{code,name,unit,feature,chapter,score,...}]}``。
      知识层只召回候选，选码由 CostAgent 在候选内做（红线：只建议不定稿）；
      向量库未就绪/依赖不可达→503 经 ``requests.HTTPError`` 上抛。
    """
    base = (base_url or KNOWLEDGE_URL).rstrip("/")
    resp = requests.post(
        f"{base}/bill/match",
        json={"query": query, "top_k": top_k, "structural": structural, "rerank": rerank},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def price_compose(
    region: str,
    code: str,
    on_date: date | str | None = None,
    base_url: str | None = None,
    timeout: int = 120,
) -> dict:
    """打 /price/compose/{region}/{code}：清单 → 适用定额 → 工料机含量 + 信息价单价（含小计）。

    参数：region —— 地区（如「深圳」）；code —— 清单编码（9 位）；on_date —— 计价期（可选，
      缺省每资源取最新可用信息价期）。返回见知识层 ``cost.query.compose_price``；
      清单项不存在→404、PG 不可达→503 经 HTTPError 上抛。未命中信息价的工料机 price_status=no_source。
    """
    base = (base_url or KNOWLEDGE_URL).rstrip("/")
    url = f"{base}/price/compose/{quote(region)}/{quote(code)}"
    params = {"on_date": on_date.isoformat() if isinstance(on_date, date) else on_date} if on_date else None
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def quota(
    region: str,
    code: str,
    base_url: str | None = None,
    timeout: int = 60,
) -> dict:
    """打 /quota/{region}/{code}：定额子目直取（子目字段 + 工料机含量）。

    参数：region —— 地区；code —— 定额子目编号（如「010001-3」）。
    返回：``{"item":{...},"resources":[...]}``；子目不存在→404、PG 不可达→503 经 HTTPError 上抛。
    """
    base = (base_url or KNOWLEDGE_URL).rstrip("/")
    url = f"{base}/quota/{quote(region)}/{quote(code)}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
