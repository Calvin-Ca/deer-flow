"""建树器 — TreeBuilder（结构层·阶段 1 建树 = 目录条目骨架 + 正文挂载 → 节点树）。

读目录打标器（CatalogLabeler）产出的标注块 **与有序目录条目表（entries）**，以
**目录条目为骨架**还原成保留 parent/child 的语义树（``nodes.json``，单一真值）。

建树策略（PRD §3.1「按文档原生目录层级建树」；2026-06-13 改用 catalog 建树，解决父链断裂）：

  ① **目录条目物化骨架**：每个 TOC 条目 → 一个骨架节点（**恒存在**，即使正文里没有
     对应正文块 / 被 MinerU 漏抽），条目标题跑 ``classify_heading`` 取 node_path（种类
     node_type / 深度 node_level 建树末统一算定）。这是「父链断裂」的根治——上层章/节
     不再因「只有标题没正文」被丢。
  ② **条目按号段嵌套**：目录只给有序扁平条目，「5.3 属于 5」仍靠 node_path 号段
     （``_resolve_parent``）；号段失效（无编号散文）则回退到 catalog 归属。
  ③ **正文挂载（树严格镜像目录，不建更细节点）**：正文标题块若 node_path 命中骨架节点
     → **接地**（补 provenance / 正文 / 表格）；若不命中（目录没列的更细标题：条 5.3.4 /
     款 / 表小标题）→ **不建节点**，归入其所属骨架节点（号段最近祖先）的正文，原始块
     （block_idx）完整留在该节点 provenance 下。**建条与建树解耦**（2026-06-14）：条款不一定
     有编号、有的整个就是一张表，切分规则因规范而异，故下沉到独立可配的 Stage 2 ClauseSplitter
     （``profile.clause_strategy``，默认 none·未实装），届时按 block_idx 回查原块细拆、不重跑 MinerU。
  ④ **固有事实一次算定**：祖先链（``_attach_ancestors``）、引用图分型 + 反向边
     （``references.annotate_references``）。

  无目录页时（entries 为空）退化为「号段建树」best-effort：正文标题块按号段连边、空骨架不丢。

  **过渡期限制**：Stage 2 未实装前，目录没列的条（如 5.3.4）不是独立节点 → 对**条级**目标的
  ``referenced_by`` / 引用扩展会落空（节级正常）；Stage 2 落地即恢复。

归属（2026-06-13 迁入新框架）：本模块是切分策略 ``TocSplitter``（splitter/toc.py，基于
原生目录的多层级切分）的**内部实现件**，与目录打标 ``catalog_labeler.py``、引用图分型
``references.py`` 同在 splitter/ 包内；格式适配 ``FormatAdapter``（parser/mineru.py）是切分前的
通用适配，不随本切法内聚。命名（承 2026-06-13 术语统一）：旧名 ``GranularityAxis`` 失准——
「granularity」已专指索引期树上视图（``index.manager.view``），与建树无关，故更名 ``TreeBuilder``。

依赖：``splitter.references``（引用图）、``core.profile``（配置契约）——均绝对 import，从 ce-code
根运行即解析（无 sys.path hack）。建树内部用**节点 dict**形态（``_new_node`` 工厂），出口由
``toc_splitter._node_to_chunk`` 转 ``core.chunk.Chunk`` IR。

输入：① CatalogLabeler.annotate() 产出的标注块列表（每块带 text_level / catalog /
      catalog_source / standard_id / block_idx 溯源）；② CatalogLabeler.entries（有序
      目录条目表，骨架真值）。
输出：节点树 list[schema.Node]（含 parent_id/children_ids + 祖先链 + 引用图）。
"""

from __future__ import annotations

import re

from core.profile import ParseProfile  # 流水线配置契约，PRD §3.2
from splitter import references  # 引用图分型 + 反向边，纯 stdlib


def _new_node(standard_id: str, node_path: str, **kw: object) -> dict:
    """建树期的**节点 dict 工厂**（建树内部用 dict 形态，出口由 toc_splitter 转 Chunk IR）。

    产出带默认值的节点 dict：node_type/node_level 由建树末算定（_assign_node_type /
    _attach_ancestors），故此处留空/0；provenance 等可经 kw 覆盖。键集与 Chunk.from_dict 兼容
    （node_type→chunk_type、node_level→level、reprs 丢弃 由 toc_splitter._node_to_chunk 处理）。
    """
    node: dict = {
        "node_path": node_path,
        "standard_id": standard_id,
        "node_type": "",
        "node_level": int(kw.pop("node_level", 0)),  # type: ignore[arg-type]
        "parent_id": None,
        "children_ids": [],
        "title": "",
        "content": "",
        "ancestor_titles": [],
        "ancestor_paths": [],
        "references": [],
        "referenced_by": [],
        "node_path_source": "",
        "node_path_confidence": 1.0,
        "provenance": {"source_file": "", "block_idx": [], "page": []},
    }
    node.update(kw)  # type: ignore[arg-type]
    return node


# ---------------------------------------------------------------------------
# 条文号正则（从标题文字识别条款号）
# ---------------------------------------------------------------------------

CLAUSE_NUM_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\s*[　 一-鿿]")
APPENDIX_RE = re.compile(r"^附录\s*([A-Z])\b")
APPENDIX_CLAUSE_RE = re.compile(r"^([A-Z]\.\d+(?:\.\d+){0,2})\s*[一-鿿]")

# ---------------------------------------------------------------------------
# 附录根识别（父路径推断用：附录E → 顶层、E.1 → 附录E）
# ---------------------------------------------------------------------------

_APPENDIX_ROOT_RE = re.compile(r"^附录\s*[A-Z]$")


# ---------------------------------------------------------------------------
# 条文号 → 路径/置信度（建树器调用）
# ---------------------------------------------------------------------------


def classify_heading(text: str) -> dict | None:
    """从一行标题文字识别条款号 / 路径来源 / 置信度 —— **无状态纯函数**（建树器调用）。

    功能：把「这行标题对应哪个条款号、路径来源置信几何」这件纯文本判定独立出来，便于
        单测与复用。**只定 node_path**——节点种类（node_type）建树末按结构判定
        （_assign_node_type），深度（node_level）按真实父链算（_attach_ancestors），
        二者均不在此推。

        判定顺序（**四级短路**，命中即返回；只看本行文字，不依赖上下文/父节点/页码）：
          ① 「附录X」开头（附录E 钢筋计算）         → node_path="附录"+字母（附录E），number/1.0
          ② 「字母.数字…」开头（E.1.1 ……）         → node_path=该号（E.1.1），number/1.0
          ③ 「数字(可带小数点)+紧跟汉字/空格」开头   → node_path=开头数字号（1 / 5.3 / 5.3.4），
               number/1.0；**但**数字后紧跟「节/条/款/项」者 = 交叉引用片段（"5.3节…"）
               → 返回 None，调用方按内容块处理、不建节点。
          ④ 三者都不中（无编号标题，如 前言 / 术语和定义）→ node_path=标题文字前 30 字，
               text_level/0.6（低置信，进 03 抽查）。
        号 ③ 的"紧跟汉字/空格"约束让 "1总则" 命中而纯页码 "9"/年份 "2024" 不误判为条号
        （但注意：排除引用只看号后**紧邻**字符，"5.3 节"带空格会绕过排除、误当标题——已知边界）。

    参数：
        text (str): 标题块文字（调用方已判定 text_level 存在，即 MinerU 标题块；
            建骨架时亦对目录条目标题调用）。
    返回：
        dict | None: ``{node_path, node_path_source, node_path_confidence}``。
            返回 None 表示该行实为交叉引用片段（如「5.3节…」），应按内容块处理。
            ``node_path_source``：number（命中编号正则，置信 1.0）/ text_level（无编号、
            靠 MinerU 标题标记 + 标题文字兜底作路径，置信 0.6）。
            ⚠ 无编号标题取 ``text[:30]`` 作 node_path，前 30 字相同的两个散文标题会撞 path：
            骨架阶段去重丢后者（``_build_skeleton`` 的 ``path in by_path`` 跳过），无目录页
            （build_missing）时后者并入前者节点（``by_path.get(path)`` 命中即接地）——均非
            数据丢失但是非预期合并，故 text_level 路径置信仅 0.6、进 03 抽查。
    """
    # 附录根（附录A / 附录B）
    app_m = APPENDIX_RE.match(text)
    if app_m:
        return {"node_path": f"附录{app_m.group(1)}",
                "node_path_source": "number", "node_path_confidence": 1.0}

    # 附录字母条号（E.1 / E.2.2）
    appc_m = APPENDIX_CLAUSE_RE.match(text)
    if appc_m:
        return {"node_path": appc_m.group(1),
                "node_path_source": "number", "node_path_confidence": 1.0}

    # 本规范条号（5 / 5.3 / 5.3.4）
    m = CLAUSE_NUM_RE.match(text)
    if m:
        num = m.group(1)
        # "节条款项"后缀说明这是交叉引用片段，非真实条号
        if text[len(num):len(num) + 1] in "节条款项":
            return None
        return {"node_path": num,
                "node_path_source": "number", "node_path_confidence": 1.0}

    # 无编号标题（"前言"、"术语和定义" 等）：用标题文字作路径
    return {"node_path": text[:30].strip(),
            "node_path_source": "text_level", "node_path_confidence": 0.6}


# ---------------------------------------------------------------------------
# 父路径推断（由 node_path 反推父节点路径）
# ---------------------------------------------------------------------------

_NUMERIC_PATH_RE = re.compile(r"^\d+(?:\.\d+)*$")        # 5 / 5.3 / 5.3.4
_APPENDIX_CLAUSE_PATH_RE = re.compile(r"^[A-Z](?:\.\d+)+$")  # E.1 / E.2.2


def _parent_path(path: str) -> str | None:
    """由条款路径反推其父节点的条款路径（仅按编号，不查节点是否存在）。

    参数：
        path (str): 条款号，如 "5.3.4" / "5" / "附录E" / "E.1.1" / "前言"。
    返回：
        str | None: 父节点条款路径；顶层节点（章 / 附录根 / 无编号标题）返回 None。
            "5.3.4"→"5.3"，"5"→None，"E.1.1"→"E.1"，"E.1"→"附录E"，"附录E"→None。
    """
    if _APPENDIX_ROOT_RE.match(path):           # 附录E → 顶层
        return None
    if _APPENDIX_CLAUSE_PATH_RE.match(path):    # E.1.1 → E.1；E.1 → 附录E
        parent = path.rsplit(".", 1)[0]
        return f"附录{parent}" if "." not in parent else parent
    if _NUMERIC_PATH_RE.match(path):            # 5.3.4 → 5.3；5 → 顶层
        return path.rsplit(".", 1)[0] if "." in path else None
    return None                                 # 标题路径节点（前言 / 术语…）顶层


def _resolve_parent(path: str, by_path: dict[str, dict]) -> str | None:
    """解析最近的**已存在**祖先路径（中间层级缺节点时继续上探）。

    参数：
        path (str): 当前节点条款路径。
        by_path (dict): node_path → 节点 的映射。
    返回：
        str | None: 最近存在的祖先条款路径；无则 None（顶层）。
    """
    p = _parent_path(path)
    while p is not None and p not in by_path:
        p = _parent_path(p)
    return p


# ---------------------------------------------------------------------------
# 建树 — TreeBuilder（结构层：目录条目骨架 + 正文挂载 → 保留 parent/child 的节点树）
# ---------------------------------------------------------------------------


class TreeBuilder:
    """结构层建树：以**目录条目为骨架**建保留 parent/child 的节点树（单一真值）。

    功能（2026-06-13 改用 catalog 建树）：
        ① 目录条目物化为骨架节点（恒存在，根治「空骨架被丢→父链断裂」）；
        ② 正文标题块并入同号骨架（接地）或建为新条/款节点；
        ③ 连边：号段反推为主、catalog 归属为兜底；
        ④ 算定祖先链 + 引用图分型 + 反向边（PRD §3.1「固有事实」）。
        空骨架节点恒保留，空且无子的正文节点剪除。表格 / 图示暂留节点 tables / images
        字段（过渡），T8 转表征层子节点 + table_struct 表征。

    参数：
        profile (ParseProfile): 解析配置（建树不依赖粒度——粒度是索引期在树上选的视图，
            见 view.py；此处仅作占位/未来扩展用）。
    返回：
        调用 apply(annotated, entries=..., ...) 返回节点树 list[Node]。
    """

    def __init__(self, profile: ParseProfile) -> None:
        """初始化建树器。

        参数：
            profile (ParseProfile): 解析配置。
        返回：
            无。
        """
        self.profile = profile  # 当前建树逻辑未读取（号段/catalog 自决）；留作 Stage 2 clause_strategy 等扩展挂载点

    def apply(
        self,
        annotated: list[dict],
        *,
        entries: list[dict] | None = None,
        source_file: str = "",
    ) -> list[dict]:
        """目录条目骨架 + 正文挂载 → 节点树（含 parent/child + 引用图 + 祖先链）。

        参数：
            annotated (list[dict]): CatalogLabeler.annotate() 产出的标注块列表。
            entries (list[dict] | None): CatalogLabeler.entries（有序目录条目表，骨架真值）；
                None / 空 → 退化为「保留骨架 + 号段建树」（无目录页 best-effort）。
            source_file (str): 原始 content_list.json 路径（写入 provenance 溯源）。
        返回：
            list[dict]: 节点树（schema.Node 形态）。
        """
        std = next((b.get("standard_id", "") for b in annotated if b.get("standard_id")), "")
        meta = {"source_file": source_file}

        by_path: dict[str, dict] = {}   # node_path → 节点（骨架 + 正文，先现先占）
        order: list[dict] = []          # 节点创建序（≈文档序），供剪枝/连边遍历

        # ① 目录条目 → 骨架节点（恒存在）
        self._build_skeleton(entries or [], std, meta, by_path, order)
        # ② 正文标题块：命中骨架则接地，更细标题（目录没列的条/款）并入所属骨架节点正文
        #    （树严格镜像目录、不建更细节点；建条下沉 Stage 2 ClauseSplitter）；内容块累积进当前节点。
        #    无目录页（entries 空）退化：标题即结构，build_missing 直接建骨架节点 best-effort。
        self._absorb_body(annotated, by_path, std=std, meta=meta, order=order,
                          build_missing=not entries)
        # ③ 连边：号段为主、catalog 兜底
        self._wire_tree(order, by_path)
        # ④ 剪空正文节点（骨架恒留），算祖先链 + 深度 + 种类 + 引用图
        nodes = self._prune(order)
        self._attach_ancestors(nodes)      # ancestor_titles/paths + node_level（真实树深度）
        self._assign_node_type(nodes)      # node_type 种类：有子→container / 无子→leaf
        references.annotate_references(nodes)  # 固有事实：引用图分型 + referenced_by 反向边

        # 清理建树期临时键（不进 nodes.json）。未接地空骨架不额外打标——它即
        # ``provenance.block_idx == []``（无原始块 = 目录列了但正文没抽到，schema.Provenance
        # 已声明此不变式）；node_path_source 因此保留 node_path 的真实来源（number / text_level），
        # 不被 synthesized 覆盖。
        for n in nodes:
            n.pop("_catalog", None)
            n.pop("_skeleton", None)
            # page 累积时按块追加,同页多块会重复;去重升序后 page[0]=首页(展示页)。
            pg = n["provenance"].get("page")
            if pg:
                n["provenance"]["page"] = sorted(set(pg))
        return nodes

    # -- ① 骨架 ----------------------------------------------------------------

    @staticmethod
    def _build_skeleton(
        entries: list[dict], std: str, meta: dict,
        by_path: dict[str, dict], order: list[dict],
    ) -> None:
        """目录条目 → 骨架节点（恒存在，初始 provenance 空 = 未接地；同号条目去重取首现）。

        参数：
            entries (list[dict]): 有序目录条目表 {title, norm, page}。
            std (str): 规范标识。
            meta (dict): source_file（provenance 溯源用）。
            by_path (dict): node_path → 节点（原地填）。
            order (list): 节点创建序（原地追加）。
        返回：
            无。
        """
        for ent in entries:
            title = ent.get("title", "").strip()
            info = classify_heading(title) if title else None
            if info is None:
                continue  # "toc"标题行 / 交叉引用片段等：不开骨架节点
            path = info["node_path"]
            if path in by_path:
                continue  # 同号条目去重（目录重复列、跨页续行等）
            node = _new_node(
                std, path,
                title=title,
                node_path_source=info["node_path_source"], node_path_confidence=info["node_path_confidence"],
                provenance={"source_file": meta["source_file"], "block_idx": [], "page": []},
            )
            node["tables"] = []
            node["images"] = []
            node["_catalog"] = title       # 骨架自身即该目录条目
            node["_skeleton"] = True        # 恒保留标记
            by_path[path] = node
            order.append(node)

    # -- ② 正文挂载 ------------------------------------------------------------

    @staticmethod
    def _absorb_body(
        annotated: list[dict], by_path: dict[str, dict],
        *, std: str = "", meta: dict | None = None,
        order: list[dict] | None = None, build_missing: bool = False,
    ) -> None:
        """正文块按目录骨架归并——**有目录时树严格镜像目录，不建更细节点**（2026-06-14 解耦）。

        功能：
            标题块（有 text_level）跑 classify_heading 取 node_path：
              · **命中骨架节点**（目录列出的章/节）→ **接地**（补 provenance/page），切为当前节点；
                标题文字不入 content（它就是节点 title）。
              · **不命中**：
                  - 有目录（``build_missing=False``）→ 目录没列的更细标题（条 5.3.4 / 款 / 表小标题）
                    **不建节点**，归入其所属骨架节点（号段最近祖先 _resolve_parent，失败则沿用 cur），
                    标题文字并入该节点 content + 溯源。建条（条款不一定有编号、可能整个是表，需按
                    规范配置）下沉到 Stage 2 ClauseSplitter（profile.clause_strategy，未实装）——骨架
                    节点完整保留其下所有原始块（provenance.block_idx），Stage 2 据此细拆、不重跑 MinerU。
                  - **无目录页**（``build_missing=True``）→ 此时标题即唯一结构，**用标题建骨架节点**
                    （best-effort 号段树，退化兜底），追加进 by_path / order。
            非标题块（含表格/图示）累积进当前节点 content / tables / images + provenance；
            catalog=="toc" 的目录页块跳过；首个节点前的游离块丢弃。

        参数：
            annotated (list[dict]): 标注块列表（文档序）。
            by_path (dict): node_path → 骨架节点（原地补 provenance/content；build_missing 时追加新节点）。
            std (str) / meta (dict): build_missing 建节点用（规范标识 / source_file 溯源）。
            order (list): 节点创建序（build_missing 时原地追加）。
            build_missing (bool): 无目录页退化——标题块直接建骨架节点。
        返回：
            无。
        """
        meta = meta or {}
        order = order if order is not None else []
        cur: dict | None = None
        for elem in annotated:
            if elem.get("catalog") == "toc":
                continue  # 目录页块不开节点、不并入正文（structure.json 已全量保留 + 溯源）

            info = classify_heading(elem.get("text", "")) if elem.get("text_level") is not None else None
            if info is not None:
                path = info["node_path"]
                node = by_path.get(path)        # 命中目录骨架节点？
                heading_is_content = False
                if node is None:
                    if build_missing:           # 无目录页：标题即结构 → 建骨架节点（退化兜底）
                        node = _new_node(
                            std, path, title=elem["text"],
                            node_path_source=info["node_path_source"], node_path_confidence=info["node_path_confidence"],
                            provenance={
                                "source_file": meta.get("source_file", ""),
                                "block_idx": [elem["block_idx"]] if "block_idx" in elem else [],
                                "page": [elem["page"]],
                            },
                        )
                        node["tables"] = []
                        node["images"] = []
                        node["_catalog"] = elem.get("catalog")
                        by_path[path] = node
                        order.append(node)
                        cur = node
                        continue
                    # 有目录：更细标题 → 归入所属骨架节点（号段最近祖先，失败沿用 cur），文字入 content
                    owner_path = _resolve_parent(path, by_path)
                    node = by_path.get(owner_path) if owner_path else cur
                    if node is None:
                        continue  # 无所属骨架（首个章节前的游离细标题）→ 丢弃
                    heading_is_content = True
                if "block_idx" in elem:
                    node["provenance"]["block_idx"].append(elem["block_idx"])
                node["provenance"]["page"].append(elem["page"])
                if node.get("_catalog") is None:
                    node["_catalog"] = elem.get("catalog")
                if heading_is_content:  # 细标题文字是所属节内容的一部分，并入 content
                    txt = elem.get("text", "")
                    if txt:
                        node["content"] = (node["content"] + "\n" + txt).lstrip("\n")
                cur = node
                continue

            if cur is None:
                continue  # 首个标题之前的内容块，无归属节点 → 丢弃

            t = elem.get("type")
            if "block_idx" in elem:
                cur["provenance"]["block_idx"].append(elem["block_idx"])
            cur["provenance"]["page"].append(elem["page"])
            if t == "table":
                cur["tables"].append({
                    "caption": elem.get("text", ""),
                    "body": elem.get("body", []),
                    "page": elem["page"],
                })
            elif t == "image":
                cur["images"].append({
                    "path": elem.get("img_path", ""),
                    "caption": elem.get("text", ""),
                    "page": elem["page"],
                })
            else:
                text = elem.get("text", "")
                if text:
                    cur["content"] = (cur["content"] + "\n" + text).lstrip("\n")

    # -- ③ 连边 ----------------------------------------------------------------

    @staticmethod
    def _wire_tree(order: list[dict], by_path: dict[str, dict]) -> None:
        """原地连边：号段反推为主、catalog 归属为兜底，回填 children_ids。

        功能：
            每个节点先按 node_path 号段反推最近**已存在**祖先（_resolve_parent）；
            号段反推不到（无编号散文 / 目录与编号不一致）则回退到「它 _catalog 所属
            目录条目」对应的骨架节点（按条目标题匹配，且不自指）。条款内层级（5.3.4.1
            归 5.3.4）由号段决定——catalog 只定位到节深，故不参与条内嵌套。

            ⚠ **catalog 兜底假定标题在全规范唯一**：``by_title`` 取首现（setdefault），故若
            「一般规定 / 术语」等重复节标题走到兜底（仅号段失效时触发，带编号节通常不会），
            会一律挂到首个同名节点、错挂父。带编号节点几乎都靠号段解析，触发率低；无编号散文
            才有此风险，待 Stage 2 按位置消歧。

        参数：
            order (list[dict]): 全部节点（骨架 + 正文，创建序）。
            by_path (dict): node_path → 节点。
        返回：
            无（原地写 parent_id / children_ids）。
        """
        by_title: dict[str, dict] = {}
        for n in order:
            by_title.setdefault(n["title"], n)  # catalog 兜底用：目录条目标题 → 骨架节点
            n["children_ids"] = []
        for n in order:
            parent: dict | None = None
            pp = _resolve_parent(n["node_path"], by_path)
            if pp is not None:
                parent = by_path.get(pp)
            if parent is None and n.get("_catalog"):  # 号段失效 → catalog 归属兜底
                cand = by_title.get(n["_catalog"])
                if cand is not None and cand["node_path"] != n["node_path"]:
                    parent = cand
            n["parent_id"] = parent["node_path"] if parent is not None else None
            if parent is not None:
                parent["children_ids"].append(n["node_path"])

    # -- ④ 剪枝 + 祖先链 -------------------------------------------------------

    @staticmethod
    def _prune(order: list[dict]) -> list[dict]:
        """剪除空正文节点（骨架恒留），级联剪到稳定；原地修父节点 children_ids。

        功能：节点保留条件 = 骨架 / 有正文 / 有表格 / 有存活子节点。剪一个空叶可能令其
            父变空叶，故循环到稳定。删除时从父 children_ids 摘除，保证树一致。

            ⚠ **未接地空骨架的叶子会留下**（``_skeleton`` 恒留压过空正文）：容器骨架留作父链
            是对的，但 ``provenance.block_idx==[]`` 且无子的 **leaf** 骨架（目录列了、正文从未
            抽到块）``content`` 为空，对检索是死单元。**契约**：下游 view / index 须跳过
            「``block_idx==[]`` 且 node_type==leaf」的空骨架，勿 emit 成空检索单元。

        参数：
            order (list[dict]): 已连边的全部节点。
        返回：
            list[dict]: 存活节点（保持原创建序）。
        """
        alive: dict[str, dict] = {n["node_path"]: n for n in order}
        changed = True
        while changed:
            changed = False
            for n in order:
                nid = n["node_path"]
                if nid not in alive:
                    continue
                has_child = any(c in alive for c in n["children_ids"])
                if n.get("_skeleton") or n["content"].strip() or n["tables"] or has_child:
                    continue
                # 空正文叶：删除并从父摘除
                parent = alive.get(n["parent_id"]) if n.get("parent_id") else None
                if parent is not None and nid in parent["children_ids"]:
                    parent["children_ids"].remove(nid)
                del alive[nid]
                changed = True
        return [n for n in order if n["node_path"] in alive]

    @staticmethod
    def _attach_ancestors(nodes: list[dict]) -> None:
        """原地算定祖先链 + 树深度：沿 parent_id 上溯写 ancestor_titles / ancestor_paths
        （不含自身）+ node_level（= 祖先数 +1，1-base，根=1）。

        node_level 取**真实父链深度**而非 node_path 号段数：中间层级缺节点时按实际父链算
        （5.3 缺 → 5.3.4 的父是 5、深度 2），也适配无"章/节/条"原生层级的文档。

        参数：
            nodes (list[dict]): 已连边（含 parent_id）的节点列表。
        返回：
            无（原地写 ancestor_titles / ancestor_paths / node_level）。
        """
        by_path = {n["node_path"]: n for n in nodes}
        for n in nodes:
            titles: list[str] = []
            paths: list[str] = []
            cur = by_path.get(n["parent_id"]) if n["parent_id"] else None
            seen: set[str] = set()
            while cur is not None and cur["node_path"] not in seen:
                seen.add(cur["node_path"])
                titles.append(cur["title"])
                paths.append(cur["node_path"])
                cur = by_path.get(cur["parent_id"]) if cur["parent_id"] else None
            n["ancestor_titles"] = titles[::-1]
            n["ancestor_paths"] = paths[::-1]
            n["node_level"] = len(paths) + 1

    @staticmethod
    def _assign_node_type(nodes: list[dict]) -> None:
        """原地判定节点**种类**（kind，与深度正交）：容器 / 叶，纯由"有无子节点"定。

        功能：建树末（children_ids 已定型）按**树结构**二分赋 node_type，与 node_path 无关：
            · 有存活子节点 → ``container``（章/节/附录根等目录骨架，不单独 emit，small-to-big 回补）；
            · 无子节点     → ``leaf``（条款/总则/无编号正文段/附录条款·粒度视图 emit 这层）。
        "是不是附录"等语义不进 node_type，消费方按需读 node_path 前缀（"附录X"），避免与 node_path 冗余。

        参数：
            nodes (list[dict]): 已连边 + 剪枝后的节点列表（children_ids 反映存活子）。
        返回：
            无（原地写 node_type）。
        """
        for n in nodes:
            n["node_type"] = "container" if n["children_ids"] else "leaf"
