"""造价取数 HTTP 客户端 —— 任务层 CostAgent 够到组价取数原语的通道。

本模块封装 ce-code 新拆分入口的造价相关原语：
  - RAG（默认 :8100）: bill_match
  - DB  （默认 :8102）: bill_get / quota / price_query / price_compose
供任务层 CostAgent 以纯 HTTP 客户端复用——检索/取数逻辑只在知识层一份，任务层保持轻量。

端到端组价闭环：构件描述 → ``bill_match`` 拿清单候选 → CostAgent 内 LLM 在候选内选码（红线：
只建议不定稿、HITL 复核）→ ``price_compose`` 组价。本模块只管「取数 plumbing」，选码编排在 CostAgent。

注意：/quota、/price/compose 的 region/code 在 URL path 段，含中文（如「深圳」）须百分号编码，
本模块用 ``urllib.parse.quote`` 显式编码（手敲 curl 不编码会 404，见 ce-code TODO 实测坑）。
"""
from __future__ import annotations

from datetime import date
from urllib.parse import quote

import requests

from common.config import DB_URL, RAG_URL


def bill_match(
    query: str,
    spec: str,
    top_k: int = 10,
    structural: bool = True,
    rerank: bool = False,
    base_url: str | None = None,
    timeout: int = 120,
) -> dict:
    """打 ce-rag /search/bill-match：构件描述 → 清单候选召回（dense 向量 + 结构约束重排）。

    参数：query —— 构件/做法自然语言描述；spec —— **国标版本（必填）**：2013 / 2024，按版本路由清单库；
      top_k —— 候选数；structural —— 结构约束重排（默认开）；rerank —— cross-encoder 精排（默认关）。
    返回：``{"query","spec","count","candidates":[{code,name,unit,feature,chapter,score,...}]}``。
      知识层只召回候选，选码由 CostAgent 在候选内做（红线：只建议不定稿）；
      未知 spec→400、向量库未就绪/依赖不可达→503 经 ``requests.HTTPError`` 上抛。
      **CostAgent 调用前须向用户确认所用国标版本（2013/2024）再传 spec。**
    """
    base = (base_url or RAG_URL).rstrip("/")
    resp = requests.post(
        f"{base}/search/bill-match",
        json={"description": query, "spec": spec, "top_k": top_k, "code_prefixes": None},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def price_compose(
    region: str,
    code: str,
    spec: str,
    on_date: date | str | None = None,
    base_url: str | None = None,
    timeout: int = 120,
) -> dict:
    """打 ce-db /price/compose/{region}/{code}：清单 → 适用定额 → 工料机含量 + 信息价单价（含小计）。

    参数：region —— 地区（如「深圳」）；code —— 清单编码（9 位）；spec —— **国标版本（必填）**：2013 / 2024
      （按版本隔离 bill_spec 取数）；on_date —— 计价期（可选，缺省每资源取最新可用信息价期）。
      返回见知识层 ``cost.query.compose_price``；未知 spec→400、该版本组价数据未就绪→501、
      清单项不存在→404、PG 不可达→503 经 HTTPError 上抛。未命中信息价的工料机 price_status=no_source。
    """
    base = (base_url or DB_URL).rstrip("/")
    url = f"{base}/price/compose/{quote(region)}/{quote(code)}"
    params: dict = {"spec": spec}
    if on_date:
        params["on_date"] = on_date.isoformat() if isinstance(on_date, date) else on_date
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def price_query(
    name: str,
    region: str = "深圳",
    period: str | None = None,
    category: str | None = None,
    top_k: int = 10,
    base_url: str | None = None,
    timeout: int = 60,
) -> dict:
    """打 DB /price/query：材料/人工/机械名称 → 当期信息价（FR-I01，动态数据、与国标版本无关）。

    参数：name —— 名称（模糊匹配，如「钢筋」「商品混凝土」）；region —— 地区（默认深圳）；
      period —— 期号 ``YYYY-MM``（缺省各资源取最新期）；category —— 人工/材料/机械 过滤（可选）；
      top_k —— 返回行数。
    返回：``{"name","region","period","count","results":[{name,spec,unit,category,price,price_type,
      period_start,period_end,doc_id}...]}``；零命中 count=0（诚实 no_source，非 404）；
      PG 不可达→503 经 HTTPError 上抛。
    """
    base = (base_url or DB_URL).rstrip("/")
    params: dict = {"name": name, "region": region, "top_k": top_k}
    if period:
        params["period"] = period
    if category:
        params["category"] = category
    resp = requests.get(f"{base}/price/query", params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def bill_get(
    code: str,
    spec: str,
    base_url: str | None = None,
    timeout: int = 60,
) -> dict | None:
    """打 DB /bill/{code}：清单编码精确查询（FR-C 核对用：编码有效性 / 单位一致性）。

    参数：code —— 9 位清单编码；spec —— 国标版本（必填，按版本隔离同码不同义）。
    返回：``{code, name, unit, unit_options, feature, work_content, chapter, spec_version, doc_id}``；
      编码不存在 → None（404 吞掉转 None，核对场景「查无此码」是正常结论不是错误）；
      未知 spec→400、PG 不可达→503 仍经 HTTPError 上抛。
    """
    base = (base_url or DB_URL).rstrip("/")
    resp = requests.get(f"{base}/bill/{quote(code)}", params={"spec": spec}, timeout=timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def quota(
    region: str,
    code: str,
    base_url: str | None = None,
    timeout: int = 60,
) -> dict:
    """打 DB /quota/{region}/{code}：定额子目直取（子目字段 + 工料机含量）。

    参数：region —— 地区；code —— 定额子目编号（如「010001-3」）。
    返回：``{"item":{...},"resources":[...]}``；子目不存在→404、PG 不可达→503 经 HTTPError 上抛。
    """
    base = (base_url or DB_URL).rstrip("/")
    url = f"{base}/quota/{quote(region)}/{quote(code)}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
