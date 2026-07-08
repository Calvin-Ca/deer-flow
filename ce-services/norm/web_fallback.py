"""规范问答联网兜底（FR-K07）—— 三道闸在服务端确定性执行。

> 对应 AGENT_PRD FR-K07（本地优先 / 三道闸）、C-01（联网结果 URL+访问日期）、C-02（口径约束 +
> 结果筛污染）、C-03（仍无可信命中 → 拒答给出路）。

**关键架构选择：三道闸放服务端，不交给弱模型 agent**（§8.3）——让 agent 自由 web_search =
把「查询口径约束 / 可信度筛查」交给弱模型即兴发挥 = 污染源（联网正是把 2024/他省口径捞回来的
最大入口）。本模块确定性执行检索/分级/筛查，LLM 只做**带降级标注的总结**（且标注头由代码强制
前置，不靠 LLM 自觉）。**仅 /norm/qa 零召回分支接入；组价/价格（FR-P/FR-I）永不联网。**

三道闸：
  ① **来源分级**：域名白名单分级（政府 .gov.cn > 行业站 > 其他；博客/文库直接排除），
     Tier-2 呈现（头部硬标注 + URL + 访问日期）；
  ② **查询带口径约束 + 结果筛查**：口径内注入「GB 编号-版本 + 深圳」；返回结果剔除冲突版本
     年份 / 他省杂质（挡污染，确定性字符串规则）；口径外（query 自带他省/他版）按问题自身口径查，
     整段打降级标注；
  ③ **仍无可信源 → 返回 None**，由 pipeline 落 C-03 拒答（给出路：已查范围 + 建议渠道）。

实现取向：零新依赖（requests + html.parser + urllib.parse）——DDG HTML 端点召回 +
Jina Reader（r.jina.ai）取正文，均免 key（§8.4 决策 1「DDG 召回 + Jina 取原文」）。
``search_fn / fetch_fn / summarize_fn`` 均可注入，自测离线跑（无网络/无 LLM）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests

from common.guards import GUARD_C01, GUARD_C02, VERDICT_PASS, GuardReport
from norm.standard_router import family_version_of

logger = logging.getLogger("ce-services.norm.web")

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
JINA_READER_URL = "https://r.jina.ai/"
_UA = "Mozilla/5.0 (compatible; ce-norm-web-fallback/1.0)"

# Tier-2 硬标注头（PRD FR-K07 呈现契约）：由代码强制前置到 answer，不靠 LLM 自觉。
WEB_WARN_HEADER = "⚠️ 非本系统深圳·2013 权威口径，联网检索结果，请人工核验。"

# ── 闸① 域名分级 ──
# 政府/权威（Tier-2 内最高）：住建部 / 深圳市及各地住建·造价管理部门 / 标准信息平台。
_GOV_SUFFIXES = (".gov.cn",)
_GOV_EXTRA = ("mohurd.gov.cn", "sac.gov.cn", "csres.com")
# 行业站（次级）：造价/建工领域常用资料站。
_INDUSTRY_DOMAINS = ("zjtcn.com", "gldjc.com", "cbi360.net", "jianbiaoku.com", "soujianzhu.cn")
# 排除（最低层，直接不用）：内容农场/文库/问答/博客——不可信且最易带错版口径。
_EXCLUDED_DOMAINS = (
    "wenku.baidu.com", "baijiahao.baidu.com", "tieba.baidu.com", "zhidao.baidu.com",
    "doc88.com", "docin.com", "book118.com", "renrendoc.com", "360doc.com",
    "zhihu.com", "csdn.net", "jianshu.com", "sohu.com", "163.com", "toutiao.com",
)

# ── 闸② 口径筛查 ──
# 他省/市杂质地名（与 routing.prerouter.OTHER_REGION_KW 同源语义；本地留一份避免 norm→routing 反向依赖）。
_POLLUTION_REGIONS = (
    "北京", "上海", "广州", "天津", "重庆", "浙江", "江苏", "山东", "河南", "湖北", "湖南",
    "四川", "陕西", "福建", "安徽", "江西", "云南", "贵州", "广西", "河北",
)


def domain_tier(url: str) -> int:
    """域名分级（闸①）。参数：url。返回：1 政府/权威 / 2 行业站 / 3 其他 / 4 排除。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return 4
    if any(host == d or host.endswith("." + d) for d in _EXCLUDED_DOMAINS):
        return 4
    if host.startswith("blog.") or ".blog." in host:
        return 4
    if any(host.endswith(s) for s in _GOV_SUFFIXES) or any(host.endswith(d) for d in _GOV_EXTRA):
        return 1
    if any(host == d or host.endswith("." + d) for d in _INDUSTRY_DOMAINS):
        return 2
    return 3


class _DDGParser(HTMLParser):
    """DDG HTML 结果页解析器：抽 ``result__a`` 链接（标题+跳转 URL）与 ``result__snippet`` 摘要。

    功能：流式 HTML 解析，零依赖（html.parser）。参数：无（喂 ``feed()``）。
    返回：``self.results`` —— ``[{url, title, snippet}]``（顺序即 DDG 排序）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_link = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        cls = a.get("class", "")
        if tag == "a" and "result__a" in cls:
            href = a.get("href", "")
            # DDG html 端点的链接是跳转形态 //duckduckgo.com/l/?uddg=<真实URL>；抽出真实 URL。
            real = href
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                real = (qs.get("uddg") or [href])[0]
            self.results.append({"url": real, "title": "", "snippet": ""})
            self._in_link = True
        elif tag == "a" and "result__snippet" in cls:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_link = False
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if not self.results:
            return
        if self._in_link:
            self.results[-1]["title"] += data
        elif self._in_snippet:
            self.results[-1]["snippet"] += data


def ddg_search(query: str, max_results: int = 8, timeout: int = 15) -> list[dict[str, str]]:
    """DDG HTML 端点检索（免 key）。参数：query / max_results / timeout。
    返回：``[{url, title, snippet}]``（DDG 排序）；网络失败经 requests 异常上抛（调用方吸收降级）。
    """
    resp = requests.post(DDG_HTML_URL, data={"q": query}, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    parser = _DDGParser()
    parser.feed(resp.text)
    return parser.results[:max_results]


def jina_fetch(url: str, timeout: int = 20, max_chars: int = 5000) -> str:
    """Jina Reader（r.jina.ai）取网页正文（免 key，低频额度足够兜底场景）。

    参数：url —— 目标页；timeout；max_chars —— 正文截断长度（控 LLM 上下文）。
    返回：正文文本（截断）；网络失败经 requests 异常上抛（调用方逐条吸收，单页失败不拖垮）。
    """
    resp = requests.get(f"{JINA_READER_URL}{url}", headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    return resp.text[:max_chars]


@dataclass
class WebSource:
    """一条联网来源（C-01 溯源：URL + 标题 + 访问日期 + 域名层级）。"""

    url: str
    title: str
    tier: int
    accessed: str
    text: str = ""

    def as_citation(self) -> dict[str, Any]:
        return {"url": self.url, "title": self.title.strip(), "domain_tier": self.tier,
                "accessed": self.accessed}


def _build_query(query: str, resolved_standard: str) -> tuple[str, bool]:
    """闸②前半：查询带口径约束。

    参数：query —— 用户问题；resolved_standard —— 本次裁定规范（如 gb50854-2013）。
    返回：``(检索串, in_caliber)``——口径内（query 未自带他省口径）注入「GB 编号-版本」硬约束；
      口径外（query 显式他省）按问题自身口径查、不注入（整段将打降级标注）。
    """
    fam, ver = family_version_of(resolved_standard)
    in_caliber = not any(r in query for r in _POLLUTION_REGIONS)
    if not in_caliber:
        return query, False
    gb = f"GB {fam[2:]}-{ver}" if fam else ""
    return f"{query} {gb} 深圳".strip(), True


def _filter_pollution(candidates: list[dict[str, str]], resolved_version: str,
                      report: GuardReport) -> list[dict[str, str]]:
    """闸②后半：结果筛污染（口径内用）——剔除「只提冲突版本年份」或「他省口径」的候选。

    参数：candidates —— 检索结果；resolved_version —— 裁定版本（2013/2024）；report —— 违规记录。
    返回：过滤后的候选。规则（确定性字符串，宁严勿松）：标题+摘要含冲突年份且不含裁定年份 → 剔；
      含他省地名且不含「深圳」→ 剔。
    """
    conflict = "2024" if resolved_version == "2013" else "2013"
    kept: list[dict[str, str]] = []
    for c in candidates:
        blob = f"{c.get('title', '')} {c.get('snippet', '')}"
        if conflict in blob and resolved_version not in blob:
            report.add(GUARD_C02, "warn", f"联网候选剔除（冲突版本 {conflict}）：{c.get('url', '')[:80]}")
            continue
        if any(r in blob for r in _POLLUTION_REGIONS) and "深圳" not in blob:
            report.add(GUARD_C02, "warn", f"联网候选剔除（他省口径杂质）：{c.get('url', '')[:80]}")
            continue
        kept.append(c)
    return kept


def answer_from_web(
    query: str,
    resolved_standard: str,
    *,
    llm_url: str,
    model_id: str,
    search_fn: Callable[[str], list[dict[str, str]]] | None = None,
    fetch_fn: Callable[[str], str] | None = None,
    summarize_fn: Callable[[str, list[WebSource]], dict[str, Any]] | None = None,
    max_sources: int = 3,
    max_fetch: int = 2,
) -> tuple[dict[str, Any] | None, GuardReport]:
    """联网兜底主流程（三道闸，确定性）：检索 → 分级/筛查 → 取正文 → LLM 降级标注总结。

    参数：query —— 用户问题；resolved_standard —— 本次裁定规范全码；llm_url/model_id —— 总结 LLM
      （桶 A 8b）；search_fn/fetch_fn/summarize_fn —— 可注入（自测离线）；max_sources —— 进入总结的
      来源上限；max_fetch —— 实际取正文页数上限（其余仅作 URL 引用）。
    返回：``(result, GuardReport)``——命中可信源且总结成功：result 为
      ``{answer(硬标注头+正文), cited_clauses:[], web_citations:[{url,title,domain_tier,accessed}],
      uncertain_aspects, out_of_scope_warnings}``、report.tier="web"；
      **无可信源 / 全程失败 → result=None**（闸③，调用方落 C-03 拒答给出路），异常不外抛。
    """
    from norm import generation  # 局部 import 防环（generation 不依赖本模块）

    _search = search_fn or ddg_search
    _fetch = fetch_fn or jina_fetch
    _summarize = summarize_fn or (
        lambda q, srcs: generation.answer_web(q, srcs, llm_url, model_id))
    report = GuardReport(verdict=VERDICT_PASS, tier="web")
    _, version = family_version_of(resolved_standard)

    # 闸②前半：查询带口径约束
    search_query, in_caliber = _build_query(query, resolved_standard)

    try:
        raw = _search(search_query)
    except requests.RequestException as exc:
        logger.warning("联网兜底检索失败（降级拒答）：%s", exc)
        return None, report
    if not raw:
        return None, report

    # 闸①：域名分级——排除层剔掉（留违规记录），按（层级, 原排序）稳定排序
    graded_all = [(domain_tier(c.get("url", "")), i, c) for i, c in enumerate(raw)]
    for t, _, c in graded_all:
        if t == 4:
            report.add(GUARD_C02, "warn", f"排除低可信域名：{c.get('url', '')[:80]}")
    graded = sorted(((t, i, c) for t, i, c in graded_all if t < 4), key=lambda x: (x[0], x[1]))
    candidates = [c for _, _, c in graded]

    # 闸②后半：口径内筛污染（口径外整段降级标注、不筛——按问题自身口径答）
    if in_caliber and version:
        candidates = _filter_pollution(candidates, version, report)
    candidates = candidates[:max_sources]
    if not candidates:
        return None, report  # 闸③：无可信源 → 交调用方 C-03 拒答

    # 取正文（Jina）：单页失败跳过不拖垮；一页都取不到 → 无可信源
    today = date.today().isoformat()
    sources: list[WebSource] = []
    fetched = 0
    for c in candidates:
        src = WebSource(url=c.get("url", ""), title=c.get("title", ""),
                       tier=domain_tier(c.get("url", "")), accessed=today)
        if fetched < max_fetch:
            try:
                src.text = _fetch(src.url)
                fetched += 1
            except requests.RequestException as exc:
                logger.info("联网兜底取正文失败（跳过该页）：%s %s", src.url[:80], exc)
        sources.append(src)
    if fetched == 0:
        return None, report

    # LLM 总结（仅基于取到的正文；失败 → 降级拒答，不硬编）
    try:
        out = _summarize(query, sources)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("联网兜底总结 LLM 失败（降级拒答）：%s", exc)
        return None, report

    if not isinstance(out, dict) or not out.get("answer"):
        return None, report

    # 硬标注头由代码强制前置（PRD FR-K07 呈现契约，不靠 LLM 自觉）；C-01：URL + 访问日期恒带。
    citations = [s.as_citation() for s in sources]
    aspects = list(out.get("uncertain_aspects") or [])
    if not in_caliber:
        aspects.append("问题为非深圳/非默认版本口径，联网结果按问题自身口径检索，请特别注意核验")
    report.provenance_complete = all(c.get("url") for c in citations)
    if not report.provenance_complete:
        report.add(GUARD_C01, "warn", "存在缺 URL 的联网来源")
    result = {
        "answer": f"{WEB_WARN_HEADER}\n\n{out['answer']}",
        "cited_clauses": [],
        "web_citations": citations,
        "uncertain_aspects": aspects,
        "out_of_scope_warnings": list(out.get("out_of_scope_warnings") or []),
    }
    return result, report


# ─────────────────────────── 内置自测（注入 stub，离线无网络/无 LLM）───────────────────────────
# 运行：cd ce-services && uv run python -m norm.web_fallback
def _selftest() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    passed = failed = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        print(f"{'✓' if cond else '✗'} {name}{('  ' + extra) if extra else ''}")
        if cond:
            passed += 1
        else:
            failed += 1

    # ① 域名分级
    check("分级：gov.cn=1", domain_tier("https://www.mohurd.gov.cn/x") == 1)
    check("分级：zjj.sz.gov.cn=1", domain_tier("https://zjj.sz.gov.cn/a/b") == 1)
    check("分级：行业站=2", domain_tier("https://www.zjtcn.com/x") == 2)
    check("分级：普通站=3", domain_tier("https://example.com/x") == 3)
    check("分级：文库=4 排除", domain_tier("https://wenku.baidu.com/view/1") == 4)

    # ② 查询口径约束
    q, in_cal = _build_query("满堂脚手架工程量怎么算", "gb50854-2013")
    check("口径内：注入 GB 编号-版本 + 深圳", "GB 50854-2013" in q and "深圳" in q and in_cal)
    q2, in_cal2 = _build_query("北京市模板超高降效怎么算", "gb50854-2013")
    check("口径外（他省）：不注入、标记口径外", q2 == "北京市模板超高降效怎么算" and not in_cal2)

    # ③ 污染筛查：冲突年份剔、他省剔、合规留
    rep = GuardReport(verdict=VERDICT_PASS, tier="web")
    cands = [
        {"url": "https://a.gov.cn/1", "title": "GB50854-2013 脚手架计量", "snippet": "2013 版规则"},
        {"url": "https://b.com/2", "title": "2024 新标准解读", "snippet": "按 2024 版"},
        {"url": "https://c.com/3", "title": "湖南省定额解读", "snippet": "湖南口径"},
    ]
    kept = _filter_pollution(cands, "2013", rep)
    check("筛污染：3 进 1 出（剔冲突版 + 他省）", len(kept) == 1 and kept[0]["url"].endswith("/1"),
          f"kept={[k['url'] for k in kept]}")
    check("筛污染：违规记录 2 条", len(rep.violations) == 2)

    # ④ 主流程（全 stub）：命中 → 硬标注头 + web_citations + tier=web
    def stub_search(q: str) -> list[dict]:
        return [
            {"url": "https://wenku.baidu.com/v", "title": "某文库", "snippet": "x"},
            {"url": "https://zjj.sz.gov.cn/doc", "title": "深圳造价站 2013 计价", "snippet": "2013"},
        ]

    result, rep2 = answer_from_web(
        "满堂脚手架工程量怎么算", "gb50854-2013", llm_url="", model_id="",
        search_fn=stub_search, fetch_fn=lambda u: "正文……按第 5.1.1 条",
        summarize_fn=lambda q, s: {"answer": "按来源1……", "uncertain_aspects": []},
    )
    check("主流程：命中返回结果", result is not None)
    check("主流程：硬标注头强制前置", result["answer"].startswith("⚠️"))
    check("主流程：web_citations 带 URL+访问日期",
          result["web_citations"] and result["web_citations"][0]["url"].startswith("https://zjj")
          and result["web_citations"][0]["accessed"])
    check("主流程：文库域名被排除（citations 无 wenku）",
          all("wenku" not in c["url"] for c in result["web_citations"]))
    check("主流程：tier=web", rep2.tier == "web")

    # ⑤ 闸③：零可信源 → None（交 C-03 拒答）
    result3, _ = answer_from_web(
        "x", "gb50854-2013", llm_url="", model_id="",
        search_fn=lambda q: [{"url": "https://wenku.baidu.com/v", "title": "文库", "snippet": ""}],
        fetch_fn=lambda u: "t", summarize_fn=lambda q, s: {"answer": "a"},
    )
    check("闸③：全排除 → None（落拒答）", result3 is None)

    # ⑥ LLM 失败 → None（降级拒答，不硬编）
    def bad_summarize(q, s):
        raise ValueError("bad json")

    result4, _ = answer_from_web(
        "满堂脚手架", "gb50854-2013", llm_url="", model_id="",
        search_fn=stub_search, fetch_fn=lambda u: "t", summarize_fn=bad_summarize,
    )
    check("总结 LLM 失败 → None（降级拒答）", result4 is None)

    print(f"\n自测：{passed} 过 / {failed} 败 / 共 {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
