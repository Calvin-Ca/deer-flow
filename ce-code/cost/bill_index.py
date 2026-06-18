"""造价清单向量库 bill_spec_kb —— 从 PG bill_spec 建 Milvus collection，供 /bill/match 构件→清单候选召回。

**源是 PG ``ce_cost.bill_spec``**（非 chunks.json；造价取数一律走 PG），把每条清单项嵌成向量入 Milvus。
**嵌入复用规范轨**（bge-large-zh-v1.5 @ ``embed_url``, dim 1024）与 ``index.vector_index.embed_texts``——
不新部署 embedding 服务，先 dense 单通道跑通；BGE-M3 sparse 混检为后续覆盖率升级项（见 TODO）。

嵌入文本拼法：``清单名 + 特征(feature_schema) + 章节``——清单匹配的核心信号是**名称 + 项目特征**
（feature_schema 区分同名异特征项，如「现浇混凝土柱」按「混凝土强度等级」分），章节给专业域上下文；
calc_rule/work_content 偏施工细节、对「构件→选码」区分度低，不入嵌入文本（留 PG 原表供取数）。

依赖：Milvus localhost:19530 + vLLM bge-large @ :8097（服务器已部署）+ PG :5433（psycopg）。

构建（服务器，从 ce-code 根，灌库后跑）。**推荐 --spec 按版本路由**（自动取 collection + doc_ids，
防漏写 --doc-id 把别版本混进同一库）：
  .venv/bin/python -m cost.bill_index --spec 2024     # 重建 2024 库 cost_bill_spec_kb（GB-50854 + GB-50856）
  .venv/bin/python -m cost.bill_index --spec 2013     # 重建 2013 库 cost_bill_spec_kb_2013（GB-50854-2013）

纯评测数据也可直读 jsonl 绕 PG：
  .venv/bin/python -m cost.bill_index --from-jsonl data/structured/cost/GB-50854-2013/bill_spec.jsonl \\
    --doc-id GB-50854-2013 --collection cost_bill_spec_kb_2013
"""
from __future__ import annotations

from config import COST_BILL_COLLECTION, DEFAULTS, EMBED_DIM


def cast_type(caption: str | None, unit: str | None) -> str:
    """从清单项的表标题/单位派生「现浇/预制」标记（纯函数，便于单测）。

    现浇柱（010502 矩形柱）与预制柱（010509 矩形柱）**同名**，索引的 chapter 又相同
    （都「附录 E 混凝土…」），dense 无从区分。判别信号在 caption（"预制混凝土柱"）/ unit
    （预制按"根/块"计量）。只在 caption 明示「预制/装配」时打标，其余返回 ""（不强加"现浇"，
    避免给脚手架/防水等非混凝土项贴错标；down-rank 只对预制/装配生效，未打标者不受罚）。

    参数：caption —— 来源表标题（如 "表 E.9 预制混凝土柱"）；unit —— 计量单位。
    返回："预制" / "装配" / ""（未明示）。

    注：MinerU 解析常在中文字间插空格（如 "预 制混凝土柱"），故匹配前先折叠所有空白，
    否则 ``"预制" in cap`` 会漏判（实测 010509 预制矩形柱 caption 即 "预 制"，见 notebooks E8）。
    """
    import re
    cap = re.sub(r"\s+", "", caption or "")
    if "预制" in cap:
        return "预制"
    if "装配" in cap:
        return "装配"
    return ""


def caption_category(caption: str | None) -> str:
    """从来源表标题派生「子表类别」（纯函数，便于单测）。

    措施项（附录S）的清单码名是结构名（"矩形梁"/"有梁板"），**不含"模板/脚手架"**，子类只在 caption
    里（如 "表 S.2 混凝土模板及支架(撑)(编码:011702)"）→ 不进嵌入文本就召不回（见 notebooks E8/E9）。
    本函数剥掉 caption 的「表号前缀」与「(编码:...)后缀」，留中间类别串（"混凝土模板及支架(撑)"），
    注入嵌入文本补回措施子类信号。对本体项同样有益（"现浇混凝土柱" 补现浇/分部上下文）。

    参数：caption —— 来源表标题（provenance.caption）。
    返回：清洗后的类别串；无 caption / 提不出 → ""。
    """
    import re
    cap = re.sub(r"\s+", "", caption or "")
    if not cap:
        return ""
    cap = re.sub(r"^(续)?表[A-Za-z0-9.\-]*", "", cap)       # 去「表 S.2 / 续表 A.1-1」前缀
    cap = re.sub(r"[（(](编码|编号)[：:][^）)]*[）)]", "", cap)  # 去「(编码:011702)」后缀
    return cap.strip("：: ")                                  # 只去空白/冒号，保留正文括号如"(撑)"


def bill_embed_text(name: str, feature_schema: list[str] | None, chapter: str | None,
                    category: str | None = None) -> str:
    """拼一条清单项的待嵌入文本（纯函数，建库与调试共用）。

    参数：
        name (str): 清单项名称（核心信号）。
        feature_schema (list[str] | None): 项目特征项名列表（区分同名异特征清单）。
        chapter (str | None): 所属章节（专业域上下文）。
        category (str | None): 子表类别（caption_category 派生）——补措施子类（模板/脚手架…）等
            name 不含的信号，提升召回（见 notebooks E8/E9）。
    返回：
        str: ``"{name}。特征:{特征/分隔}。{category}。{chapter}"``，缺项自然省略，首尾去空白。
    """
    parts = [name.strip()]
    feats = [f.strip() for f in (feature_schema or []) if f and f.strip()]
    if feats:
        parts.append("特征:" + "/".join(feats))
    if category and category.strip() and category.strip() != name.strip():  # 与名相同则不重复
        parts.append(category.strip())
    if chapter and chapter.strip():
        parts.append(chapter.strip())
    return "。".join(parts)


_BILL_FIELDS = ("code", "name", "unit", "feature_schema", "chapter", "doc_id", "spec_version")


def _fetch_bills(dsn: str | None, doc_ids: list[str] | None = None) -> list[dict]:
    """从 PG 读清单项（建库源数据），按 code 排序。

    参数：
        dsn —— PG 连接串（见 cost.query.resolve_dsn）。
        doc_ids —— 只取这些 doc_id（**版本隔离**：如只建 2013 传 ['GB-50854-2013']）；None=全部。
    返回：list[dict]，每项含 code/name/unit/feature_schema/chapter/doc_id/spec_version。
    """
    from cost import query as cost_query

    sql = ("SELECT code, name, unit, feature_schema, chapter, doc_id, spec_version, "
           "provenance->>'caption' AS caption "
           "FROM bill_spec")
    params: list = []
    if doc_ids:
        sql += " WHERE doc_id = ANY(%s)"
        params.append(list(doc_ids))
    sql += " ORDER BY code"
    with cost_query.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params or None)
        rows = cur.fetchall()
    for r in rows:                                   # 从 caption/unit 派生现浇预制标记 + 子表类别
        r["cast_type"] = cast_type(r.get("caption"), r.get("unit"))
        r["category"] = caption_category(r.get("caption"))
    return rows


def _read_bills_jsonl(path, doc_ids: list[str] | None = None) -> list[dict]:
    """从 bill_spec.jsonl 直读清单项（建库源，绕开 PG），按 code 排序。

    **纯评测数据专用**（如 2013 真实结算 gold）：2013 清单无定额映射、不参与组价取数、
    不被 /price/compose 服务，故其建库源直读 jsonl，不污染生产 PG bill_spec 表
    （PK 仅 code，2013/2024 同 9 位码会撞，混库会让 2024 取数返回重复行）。

    参数：
        path —— bill_spec.jsonl 路径。
        doc_ids —— 只取这些 doc_id；None=全部。
    返回：list[dict]，字段与 _fetch_bills 对齐（code/name/unit/feature_schema/chapter/doc_id/spec_version）。
    """
    import json
    from pathlib import Path

    wanted = set(doc_ids) if doc_ids else None
    bills: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if wanted is not None and rec.get("doc_id") not in wanted:
            continue
        bill = {k: rec.get(k) for k in _BILL_FIELDS}
        caption = (rec.get("provenance") or {}).get("caption")
        bill["cast_type"] = cast_type(caption, rec.get("unit"))   # 现浇/预制标记
        bill["category"] = caption_category(caption)              # 子表类别（补措施子类等）
        bills.append(bill)
    bills.sort(key=lambda b: b.get("code") or "")
    return bills


def build(
    dsn: str | None = None,
    collection_name: str = COST_BILL_COLLECTION,
    doc_ids: list[str] | None = None,
    from_jsonl: str | None = None,
    milvus_host: str = DEFAULTS["milvus_host"],
    milvus_port: int = DEFAULTS["milvus_port"],
    embed_url: str = DEFAULTS["embed_url"],
    embed_model_id: str = DEFAULTS["embed_model_id"],
    batch_size: int = 64,
) -> None:
    """建/重建 bill_spec_kb：读清单项 → 嵌入名称+特征文本 → 插入 Milvus。

    参数：
        dsn (str | None): PG 连接串（None 走默认 :5433/ce_cost）。
        collection_name (str): Milvus collection 名（默认 cost_bill_spec_kb；隔离版本用独立名）。
        doc_ids (list[str] | None): 只建这些 doc_id（**版本隔离**：2013 用独立 collection + doc_id，
            不与 2024 混；None=全部）。
        from_jsonl (str | None): 建库源 bill_spec.jsonl 路径（直读，绕开 PG）；None=读 PG。
            纯评测数据（如 2013 gold）走此路，不污染生产 PG bill_spec 表（见 _read_bills_jsonl）。
        milvus_host/milvus_port/embed_url/embed_model_id/batch_size: Milvus 与嵌入服务参数。
    返回：
        无（副作用：drop 重建 collection 并灌入向量 + 标量行）。
    """
    from pymilvus import DataType, MilvusClient
    from rich.console import Console
    from tqdm import tqdm

    from cost.embed import embed_texts

    console = Console()
    bills = _read_bills_jsonl(from_jsonl, doc_ids) if from_jsonl else _fetch_bills(dsn, doc_ids)
    src = f"jsonl {from_jsonl}" if from_jsonl else "PG bill_spec"
    scope = f"（doc_id={list(doc_ids)}）" if doc_ids else "（全部 doc_id）"
    console.print(f"读 {src}：{len(bills)} 条清单项 {scope} → collection {collection_name}")
    if not bills:
        console.print("[yellow]bill_spec 为空，跳过建库（PG 源先 load_pg 灌库 / jsonl 源检查路径与 doc_id）[/yellow]")
        return

    client = MilvusClient(uri=f"http://{milvus_host}:{milvus_port}")
    console.print(f"[green]已连接 Milvus {milvus_host}:{milvus_port}[/green]")
    if client.has_collection(collection_name):
        console.print(f"[yellow]集合 {collection_name} 已存在，将重建[/yellow]")
        client.drop_collection(collection_name)

    schema_ = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema_.add_field("id",          DataType.INT64,        is_primary=True)
    schema_.add_field("code",        DataType.VARCHAR,      max_length=32)
    schema_.add_field("name",        DataType.VARCHAR,      max_length=512)
    schema_.add_field("unit",        DataType.VARCHAR,      max_length=64)
    schema_.add_field("feature",     DataType.VARCHAR,      max_length=1_024)
    schema_.add_field("chapter",     DataType.VARCHAR,      max_length=256)
    schema_.add_field("doc_id",      DataType.VARCHAR,      max_length=32)
    schema_.add_field("spec_version", DataType.VARCHAR,     max_length=64)
    schema_.add_field("cast_type",   DataType.VARCHAR,      max_length=16)
    schema_.add_field("embedding",   DataType.FLOAT_VECTOR, dim=EMBED_DIM)

    index_params = client.prepare_index_params()
    index_params.add_index("embedding", metric_type="COSINE", index_type="HNSW",
                           params={"M": 16, "efConstruction": 200})
    index_params.add_index("code", index_type="INVERTED")
    client.create_collection(collection_name=collection_name,
                             schema=schema_, index_params=index_params)
    console.print(f"集合 {collection_name} 已创建")

    texts = [bill_embed_text(b["name"], b.get("feature_schema"), b.get("chapter"), b.get("category"))
             for b in bills]
    console.print(f"调用嵌入服务 {embed_url}，model={embed_model_id}…")
    total_batches = (len(bills) + batch_size - 1) // batch_size
    for i in tqdm(range(0, len(bills), batch_size), total=total_batches, desc="嵌入并插入"):
        batch = bills[i: i + batch_size]
        embeddings = embed_texts(texts[i: i + batch_size], embed_url, embed_model_id, len(batch))
        rows = []
        for b, emb in zip(batch, embeddings):
            feats = b.get("feature_schema") or []
            rows.append({
                "code": b["code"],
                "name": b["name"],
                "unit": b.get("unit") or "",
                "feature": "/".join(feats),
                "chapter": b.get("chapter") or "",
                "doc_id": b.get("doc_id") or "",
                "spec_version": b.get("spec_version") or "",
                "cast_type": b.get("cast_type") or "",
                "embedding": emb,
            })
        client.insert(collection_name=collection_name, data=rows)

    client.flush(collection_name)
    stats = client.get_collection_stats(collection_name)
    console.print(f"[green]✓ bill_spec_kb 建成：{stats['row_count']} 个向量[/green]")


def _cli():
    """构造 click 命令（click lazy import，模块顶层无依赖，保持 bill_embed_text 可被纯测 import）。"""
    import click

    @click.command()
    @click.option("--collection", default=COST_BILL_COLLECTION, show_default=True,
                  help="Milvus collection 名（隔离版本用独立名，如 cost_bill_spec_kb_2013）")
    @click.option("--doc-id", "doc_ids", multiple=True,
                  help="只建这些 doc_id（版本隔离，可多次；如 --doc-id GB-50854-2013）；不传=全部")
    @click.option("--spec", default=None,
                  help="国标版本（2013/2024）：按 config.SPEC_REGISTRY 自动取 collection + doc_ids，"
                       "免手写 --doc-id 漏写混版本（与 --collection/--doc-id 二选一）")
    @click.option("--from-jsonl", "from_jsonl", default=None,
                  help="建库源 bill_spec.jsonl 路径（直读绕开 PG，纯评测数据如 2013 gold 用）；不传=读 PG")
    @click.option("--dsn", default=None, help="PG 连接串（默认 :5433/ce_cost）")
    @click.option("--batch-size", default=64, show_default=True)
    def main(collection: str, doc_ids: tuple, spec: str | None, from_jsonl: str | None,
             dsn: str | None, batch_size: int) -> None:
        """从 PG bill_spec（或 --from-jsonl 指定的 jsonl）建/重建造价清单向量库。

        --spec 优先：按 SPEC_REGISTRY 解析出该版本的 collection 与 doc_ids（防止重建时漏写
        --doc-id 把别版本混进同一 collection）。显式 --collection/--doc-id 仍可覆盖（不传 --spec 时）。
        """
        from config import resolve_spec

        sel_collection, sel_doc_ids = collection, list(doc_ids) or None
        if spec:
            cfg = resolve_spec(spec)
            sel_collection = cfg["bill_collection"]
            sel_doc_ids = list(cfg["bill_doc_ids"])
        build(dsn=dsn, collection_name=sel_collection,
              doc_ids=sel_doc_ids, from_jsonl=from_jsonl, batch_size=batch_size)

    return main


if __name__ == "__main__":
    _cli()()
