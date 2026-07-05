"""清单→定额映射语义富化（数据线 P0 · v1）—— 粗筛 + LLM 候选内判定，补名称匹配盖不住的边。

背景（2026-07-05 盘点定性）：定额库 1257 条（SJG171/170）**全在库**，但名称匹配种子映射
只盖住 11.2%（53/472）——败因是定额名前缀式（「非泵送现浇混凝土 独立基础 混凝土」），
``base_name`` 取首段永远对不上清单名「独立基础」。纯工程可解，零采购。

管线（离线数据工程工具；知识层「零 LLM」约束只管在线服务，本工具与 bill_quota.py 同为离线生成器）：
  ① 粗筛（确定性）：清单名 2-gram 在定额名中的覆盖率打分，全量 1257 条排序取 top-K；
  ② 精判（LLM，建议 32b）：清单项(码/名/单位/特征) + K 候选定额 → 适用子目列表 + confidence + 理由。
     红线同 select_code：**仅候选内**（越界作废计数）、**可空**（无适用→诚实空，不硬配——覆盖率
     虚高比缺口更毒）、**措施类子目不选**（模板/脚手架/支撑属组合伴随项，组合语义归 v2）；
  ③ 写回 bill_quota_map：``source='semantic_llm'`` + confidence 分层 + note=LLM 理由——
     与名称匹配边可区分、可整批回滚；低置信边入库后由任务层 quota_gate 多子目人工确认兜底
     （「只建议不定稿」架构红利：富化不必零错，闸会接住）。
  默认 **dry-run**（只出报告），``--write`` 才落库（复用 load_pg.load_bill_quota_map 幂等 upsert）。

用（服务器，从 ce-code 根；32b 判定质量优先）：
  .venv/bin/python -m cost.bill_quota_enrich --limit 20                       # 小批试跑（dry-run）
  .venv/bin/python -m cost.bill_quota_enrich --llm-url http://172.19.2.2:8001 \\
      --model /models/Qwen3-32B-AWQ --write                                    # 32b 全量落库
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

# 措施类关键词（LLM 红线 + 代码侧双保险）：这些子目是组合伴随项/措施项目，不是清单项的直接映射。
MEASURE_KW = ("模板", "脚手架", "支撑", "超高", "泵送费")

_NUM_RE = re.compile(r"\d")

_JUDGE_SYSTEM = """\
你是深圳建设工程计价的清单-定额映射判定员。给定一个清单项（GB 50854）和若干候选定额子目（SJG），\
判定哪些定额子目**直接适用于**该清单项的实体工程内容（一个清单常对应多个做法/材质变体子目，1:N）。

铁律：
1. **只能从候选列表里选子目号**，一个都不适用就返回空列表——绝不硬配、绝不编造子目号。
2. **措施类子目不选**：模板、脚手架、支撑、超高增加费等是措施/伴随项，不是清单实体工程的直接映射。
3. 变体子目（泵送/非泵送、不同材质规格）如都适用该清单项应**并列给出**（下游按项目特征人工选定）。
4. 每条给 confidence（0~1，你对「该定额确实适用此清单」的把握）和一句话 reason。
5. 单位量纲明显不符（清单 m³ vs 定额 m²）的不选，除非行业惯例换算成立并在 reason 说明。

只返回合法 JSON：{"mappings": [{"quota_code": "010002-28", "confidence": 0.9, "reason": "..."}]}\
"""


def bigrams(s: str) -> set[str]:
    """字符 2-gram 集（去空格数字；短串整串兜底）——粗筛打分基元。"""
    s = re.sub(r"[\s\d]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def rough_score(bill_name: str, quota_name: str) -> float:
    """粗筛分：清单名 2-gram 被定额名覆盖的比例（清单侧覆盖率）。

    「独立基础」⊂「非泵送现浇混凝土 独立基础 混凝土」→ 1.0（前缀式定额名的克星——
    名称匹配对不上是因为拿首段比对，覆盖率不看位置）。
    """
    bg = bigrams(bill_name)
    if not bg:
        return 0.0
    return len(bg & bigrams(quota_name)) / len(bg)


def shortlist(bill_name: str, quotas: list[dict[str, Any]], top_k: int = 15) -> list[dict[str, Any]]:
    """全量定额粗筛 top-K（1257 条量级全量打分零压力）；同分定额名短者优先（更通用的排前）。"""
    scored = sorted(quotas, key=lambda q: (-rough_score(bill_name, q.get("name") or ""),
                                           len(q.get("name") or "")))
    return [q for q in scored[:top_k] if rough_score(bill_name, q.get("name") or "") > 0]


def _judge_user(bill: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    cand = "\n".join(f"- {q['quota_code']}｜{q['name']}｜单位 {q.get('unit') or '?'}"
                     for q in candidates)
    return (f"清单项：{bill['code']}｜{bill['name']}｜单位 {bill.get('unit') or '?'}"
            f"｜项目特征：{bill.get('feature_schema') or '—'}\n\n候选定额子目：\n{cand}\n\n"
            "判定直接适用的定额子目（可为空），只返回合法 JSON。\n\n/no_think")


def call_llm(system: str, user: str, llm_url: str, model: str, timeout: int = 60) -> dict[str, Any]:
    """标准库版 vLLM chat 调用（ce-code 不引 requests 依赖；response_format 强制 JSON）。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.0, "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(f"{llm_url.rstrip('/')}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())  # 去 markdown 壳
    return json.loads(content)


def judge_mappings(bill: dict[str, Any], candidates: list[dict[str, Any]],
                   raw: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """LLM 判定结果 → 合法映射 records（红线校验：候选内 / 非措施类 / confidence 夹取）。

    返回：(records, dropped)——records 为 load_bill_quota_map 可直接消费的行；越界/措施类/
    字段缺失整条作废计数（作废出声）。
    """
    by_code = {str(q["quota_code"]).strip(): q for q in candidates}
    records: list[dict[str, Any]] = []
    dropped = 0
    for m in (raw.get("mappings") or []) if isinstance(raw, dict) else []:
        if not isinstance(m, dict):
            dropped += 1
            continue
        qcode = str(m.get("quota_code") or "").strip()
        q = by_code.get(qcode)
        # 红线：候选内 + 非措施类（代码侧双保险——prompt 说了不算，与假漏项同款分层）
        if q is None or any(kw in (q.get("name") or "") for kw in MEASURE_KW):
            dropped += 1
            continue
        try:
            conf = max(0.0, min(1.0, float(m.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        records.append({
            "bill_code": bill["code"],
            "bill_spec_version": bill["spec_version"],
            "quota_code": qcode,
            "quota_doc_id": q.get("doc_id") or "",
            "relation": "APPLIES",
            "confidence": round(conf, 2),
            "source": "semantic_llm",
            "note": str(m.get("reason") or "")[:180],
        })
    return records, dropped


def _cli() -> None:
    import argparse

    import psycopg  # DB 依赖收在 CLI 内：纯函数（打分/校验）可在无 psycopg 环境单测
    from psycopg.rows import dict_row

    from cost import load_pg, query as cost_query

    p = argparse.ArgumentParser(description="清单→定额映射语义富化（默认 dry-run）")
    p.add_argument("--spec-version", default="GB/T 50854-2024", help="清单版本（默认 2024 房建）")
    p.add_argument("--region", default="深圳")
    p.add_argument("--llm-url", default="http://localhost:8099", help="vLLM 地址（判定质量优先建议 32b）")
    p.add_argument("--model", default="qwen3-8b")
    p.add_argument("--top-k", type=int, default=15)
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 个未映射码（0=全量）")
    p.add_argument("--write", action="store_true", help="落库（缺省 dry-run 只出报告）")
    args = p.parse_args()

    with psycopg.connect(cost_query.resolve_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT bs.code, bs.name, bs.unit, bs.feature_schema, bs.spec_version FROM bill_spec bs "
            "WHERE bs.spec_version = %s AND NOT EXISTS ("
            "  SELECT 1 FROM bill_quota_map m WHERE m.bill_code = bs.code "
            "  AND m.bill_spec_version = bs.spec_version) ORDER BY bs.code",
            [args.spec_version])
        bills = cur.fetchall()
        cur.execute("SELECT quota_code, doc_id, name, unit FROM quota_item WHERE region = %s",
                    [args.region])
        quotas = cur.fetchall()

    if args.limit:
        bills = bills[: args.limit]
    print(f"未映射清单码 {len(bills)} 个 · 定额池 {len(quotas)} 条 · top_k={args.top_k} "
          f"· LLM={args.llm_url} ({args.model}) · {'WRITE' if args.write else 'DRY-RUN'}\n")

    all_records: list[dict[str, Any]] = []
    n_empty = n_err = n_dropped = 0
    for i, bill in enumerate(bills, 1):
        cands = shortlist(bill["name"], quotas, args.top_k)
        if not cands:
            n_empty += 1
            print(f"[{i}/{len(bills)}] {bill['code']} {bill['name']}: 粗筛零候选（库内无近似定额）")
            continue
        try:
            raw = call_llm(_JUDGE_SYSTEM, _judge_user(bill, cands), args.llm_url, args.model)
        except Exception as exc:  # noqa: BLE001 —— 单码失败不拖垮整批，出声跳过
            n_err += 1
            print(f"[{i}/{len(bills)}] {bill['code']} {bill['name']}: LLM 失败跳过（{exc}）")
            continue
        records, dropped = judge_mappings(bill, cands, raw)
        n_dropped += dropped
        if not records:
            n_empty += 1
            print(f"[{i}/{len(bills)}] {bill['code']} {bill['name']}: 判定无适用（诚实空）"
                  f"{f'，作废 {dropped}' if dropped else ''}")
            continue
        all_records.extend(records)
        tag = " ".join(f"{r['quota_code']}({r['confidence']})" for r in records)
        print(f"[{i}/{len(bills)}] {bill['code']} {bill['name']}: +{len(records)} → {tag}"
              f"{f'，作废 {dropped}' if dropped else ''}")

    lo = [r for r in all_records if (r["confidence"] or 0) < 0.7]
    print(f"\n报告：配上 {len({r['bill_code'] for r in all_records})} 码 / 共 {len(all_records)} 条边"
          f"；诚实空 {n_empty}；LLM 失败 {n_err}；红线作废 {n_dropped}"
          f"；低置信(<0.7)边 {len(lo)} 条（入库后由 quota_gate 人工确认兜底）")
    if args.write and all_records:
        with psycopg.connect(cost_query.resolve_dsn()) as conn:
            n = load_pg.load_bill_quota_map(conn, all_records)
            conn.commit()
        print(f"已落库 {n} 条（source=semantic_llm，可按 source 整批回滚）")
    elif all_records:
        print("（dry-run 未落库；确认报告后加 --write 重跑）")


if __name__ == "__main__":
    _cli()
