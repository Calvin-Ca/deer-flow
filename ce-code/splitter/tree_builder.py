"""建树器 — TreeBuilder（结构层·阶段 1 建树 = 目录条目骨架 + 正文挂载 → 节点树）。

读目录打标器（CatalogLabeler）产出的标注块 **与有序目录条目表（entries）**，以
**目录条目为骨架**还原成保留 parent/child 的语义树（``nodes.json``，单一真值）。

建树策略（PRD §3.1「按文档原生目录层级建树」；2026-06-13 改用 catalog 建树，解决父链断裂）：

  ① **目录条目物化骨架**：每个 TOC 条目 → 一个骨架节点（**恒存在**，即使正文里没有
     对应正文块 / 被 MinerU 漏抽），条目标题跑 ``classify_heading`` 取 clause_path /
     node_type。这是「父链断裂」的根治——上层章/节不再因「只有标题没正文」被丢。
  ② **条目按号段嵌套**：目录只给有序扁平条目，「5.3 属于 5」仍靠 clause_path 号段
     （``_resolve_parent``）；号段失效（无编号散文）则回退到 catalog 归属。
  ③ **正文/条款挂载**：正文标题块若 clause_path 已是骨架节点 → **并入**（接地：补
     provenance / 正文 / 表格）；否则建为新节点（条/款，目录通常只列到节），按号段找父、
     号段失效则回退到「它 ``catalog`` 所属条目」的骨架节点（catalog 只定位到节深，
     不能决定条内层级，故条款内嵌套仍以号段为准）。
  ④ **固有事实一次算定**：祖先链（``_attach_ancestors``）、引用图分型 + 反向边
     （``references.annotate_references``）。

  无目录页时（entries 为空）退化为「保留骨架 + 号段建树」：正文标题块各自成节点、
  号段连边、空骨架不丢——即旧号段路径 + 不丢标题节点，best-effort。

归属（2026-06-13 迁入新框架）：本模块是切分策略 ``TocSplitter``（splitter/toc.py，基于
原生目录的多层级切分）的**内部实现件**，与目录打标 ``catalog_labeler.py``、引用图分型
``references.py`` 同在 splitter/ 包内；格式适配 ``format_adapter.py``（parser/）是切分前的
通用适配，不随本切法内聚。命名（承 2026-06-13 术语统一）：旧名 ``GranularityAxis`` 失准——
「granularity」已专指索引期树上视图（``core.view``），与建树无关，故更名 ``TreeBuilder``。

依赖：``core.schema``（节点契约）、``splitter.references``（引用图）、``core.parse_profile``
（配置契约）——均绝对 import，从 ce-code 根运行即解析（无 sys.path hack）。

输入：① CatalogLabeler.annotate() 产出的标注块列表（每块带 text_level / catalog /
      catalog_source / standard_id / block_idx 溯源）；② CatalogLabeler.entries（有序
      目录条目表，骨架真值）。
输出：节点树 list[schema.Node]（含 parent_id/children_ids + 祖先链 + 引用图）。
"""

from __future__ import annotations

import re

from core import schema  # 节点契约
from core.parse_profile import ParseProfile  # 流水线配置契约，PRD §3.2
from splitter import references  # 引用图分型 + 反向边，纯 stdlib


# ---------------------------------------------------------------------------
# 条文号正则（从标题文字识别条款号）
# ---------------------------------------------------------------------------

CLAUSE_NUM_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\s*[　 一-鿿]")
APPENDIX_RE = re.compile(r"^附录\s*([A-Z])\b")
APPENDIX_CLAUSE_RE = re.compile(r"^([A-Z]\.\d+(?:\.\d+){0,2})\s*[一-鿿]")

# ---------------------------------------------------------------------------
# node_type 推断（由条款路径号段数定章/节/条，对应 PRD §3.1 节点 schema）
# ---------------------------------------------------------------------------

_APPENDIX_ROOT_RE = re.compile(r"^附录\s*[A-Z]$")


def _infer_node_type(path: str) -> str:
    """由条款路径推断 node_type（层级 = 号段数，自包含，不依赖标题栈）。

    参数：
        path (str): 条款号，如 "1" / "5.3.4" / "附录E" / "E.1.1"。
    返回：
        str: node_type 枚举值 — chapter / section / clause / appendix。
            层级按小数点号段数：1 段→chapter、2 段→section、≥3 段→clause；
            "附录X" 整根为 appendix。paragraph / table / figure / formula 由建树层
            按元素类型在挂载点赋值。
    """
    if _APPENDIX_ROOT_RE.match(path):
        return "appendix"
    level = path.count(".") + 1
    if level == 1:
        return "chapter"
    if level == 2:
        return "section"
    return "clause"


# ---------------------------------------------------------------------------
# 条文号 → 类型/置信度（建树器调用）
# ---------------------------------------------------------------------------


def classify_heading(text: str) -> dict | None:
    """从一行标题文字识别条款号 / 类型 / 置信度 —— **无状态纯函数**（建树器调用）。

    功能：把「这行标题对应哪个条款号、是什么 node_type、路径来源置信几何」这件纯文本
        判定独立出来，便于单测与复用。node_type 由条款路径号段数自推（_infer_node_type），
        不依赖标题栈/外部层级。

    参数：
        text (str): 标题块文字（调用方已判定 text_level 存在，即 MinerU 标题块；
            建骨架时亦对目录条目标题调用）。
    返回：
        dict | None: ``{clause_path, node_type, path_source, path_confidence}``。
            返回 None 表示该行实为交叉引用片段（如「5.3节…」），应按内容块处理。
            ``path_source``：number（命中编号正则，置信 1.0）/ text_level（无编号、
            靠 MinerU 标题标记 + 标题文字兜底作路径，置信 0.6）。
    """
    # 附录根（附录A / 附录B）
    app_m = APPENDIX_RE.match(text)
    if app_m:
        return {"clause_path": f"附录{app_m.group(1)}", "node_type": "appendix",
                "path_source": "number", "path_confidence": 1.0}

    # 附录字母条号（E.1 / E.2.2）
    appc_m = APPENDIX_CLAUSE_RE.match(text)
    if appc_m:
        path = appc_m.group(1)
        return {"clause_path": path, "node_type": _infer_node_type(path),
                "path_source": "number", "path_confidence": 1.0}

    # 本规范条号（5 / 5.3 / 5.3.4）
    m = CLAUSE_NUM_RE.match(text)
    if m:
        num = m.group(1)
        # "节条款项"后缀说明这是交叉引用片段，非真实条号
        if text[len(num):len(num) + 1] in "节条款项":
            return None
        return {"clause_path": num, "node_type": _infer_node_type(num),
                "path_source": "number", "path_confidence": 1.0}

    # 无编号标题（"前言"、"术语和定义" 等）：用标题文字作路径
    path = text[:30].strip()
    return {"clause_path": path, "node_type": _infer_node_type(path),
            "path_source": "text_level", "path_confidence": 0.6}


# ---------------------------------------------------------------------------
# 父路径推断（由 clause_path 反推父节点路径）
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
        by_path (dict): clause_path → 节点 的映射。
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
        self.profile = profile

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

        by_path: dict[str, dict] = {}   # clause_path → 节点（骨架 + 正文，先现先占）
        order: list[dict] = []          # 节点创建序（≈文档序），供剪枝/连边遍历

        # ① 目录条目 → 骨架节点（恒存在）
        self._build_skeleton(entries or [], std, meta, by_path, order)
        # ② 正文标题块：并入同号骨架（接地）或建新条/款节点；内容块累积进当前节点
        self._absorb_body(annotated, std, meta, by_path, order)
        # ③ 连边：号段为主、catalog 兜底
        self._wire_tree(order, by_path)
        # ④ 剪空正文节点（骨架恒留），算祖先链 + 引用图
        nodes = self._prune(order)
        self._attach_ancestors(nodes)
        references.annotate_references(nodes)  # 固有事实：引用图分型 + referenced_by 反向边

        for n in nodes:  # 清理建树期临时键（不进 nodes.json）
            n.pop("_catalog", None)
            n.pop("_skeleton", None)
        return nodes

    # -- ① 骨架 ----------------------------------------------------------------

    @staticmethod
    def _build_skeleton(
        entries: list[dict], std: str, meta: dict,
        by_path: dict[str, dict], order: list[dict],
    ) -> None:
        """目录条目 → 骨架节点（恒存在，synthesized 溯源；同号条目去重取首现）。

        参数：
            entries (list[dict]): 有序目录条目表 {title, norm, page}。
            std (str): 规范标识。
            meta (dict): source_file（provenance 溯源用）。
            by_path (dict): clause_path → 节点（原地填）。
            order (list): 节点创建序（原地追加）。
        返回：
            无。
        """
        for ent in entries:
            title = ent.get("title", "").strip()
            info = classify_heading(title) if title else None
            if info is None:
                continue  # "目录"标题行 / 交叉引用片段等：不开骨架节点
            path = info["clause_path"]
            if path in by_path:
                continue  # 同号条目去重（目录重复列、跨页续行等）
            node = schema.new_node(
                std, path, info["node_type"],
                title=title, page=ent.get("page") or 0,
                path_source=info["path_source"], path_confidence=info["path_confidence"],
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
        annotated: list[dict], std: str, meta: dict,
        by_path: dict[str, dict], order: list[dict],
    ) -> None:
        """正文块按标题分组：同号骨架则并入（接地），否则建新节点；内容块累积进当前节点。

        功能：
            标题块（有 text_level）跑 classify_heading 取 clause_path：
              · 已是骨架/已建节点 → **并入**（补 provenance/page，骨架由此接地），切为当前节点；
              · 否则建新条/款节点（目录通常只列到节，5.3.4 等在此诞生），记 _catalog 供兜底连边。
            非标题块（含表格/图示）累积进当前节点的 content / tables / images + provenance；
            catalog=="目录" 的目录页块跳过；首个节点前的游离块丢弃。

        参数：
            annotated (list[dict]): 标注块列表（文档序）。
            std (str): 规范标识。
            meta (dict): 规范级元数据。
            by_path (dict): clause_path → 节点（原地补/并入）。
            order (list): 节点创建序（原地追加新节点）。
        返回：
            无。
        """
        cur: dict | None = None
        for elem in annotated:
            if elem.get("catalog") == "目录":
                continue  # 目录页块不开节点、不并入正文（structure.json 已全量保留 + 溯源）

            info = classify_heading(elem.get("text", "")) if elem.get("text_level") is not None else None
            if info is not None:
                path = info["clause_path"]
                node = by_path.get(path)
                if node is None:  # 新条/款节点（不在目录骨架里）
                    node = schema.new_node(
                        std, path, info["node_type"],
                        title=elem["text"], page=elem["page"],
                        path_source=info["path_source"], path_confidence=info["path_confidence"],
                        provenance={
                            "source_file": meta["source_file"],
                            "block_idx": [elem["block_idx"]] if "block_idx" in elem else [],
                            "page": [elem["page"]],
                        },
                    )
                    node["tables"] = []
                    node["images"] = []
                    node["_catalog"] = elem.get("catalog")
                    by_path[path] = node
                    order.append(node)
                else:  # 并入同号骨架/既有节点：接地（补溯源），保留骨架的目录条目标题
                    if not node.get("page"):
                        node["page"] = elem["page"]
                    if "block_idx" in elem:
                        node["provenance"]["block_idx"].append(elem["block_idx"])
                    node["provenance"]["page"].append(elem["page"])
                    if node.get("_catalog") is None:
                        node["_catalog"] = elem.get("catalog")
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
            每个节点先按 clause_path 号段反推最近**已存在**祖先（_resolve_parent）；
            号段反推不到（无编号散文 / 目录与编号不一致）则回退到「它 _catalog 所属
            目录条目」对应的骨架节点（按条目标题匹配，且不自指）。条款内层级（5.3.4.1
            归 5.3.4）由号段决定——catalog 只定位到节深，故不参与条内嵌套。

        参数：
            order (list[dict]): 全部节点（骨架 + 正文，创建序）。
            by_path (dict): clause_path → 节点。
        返回：
            无（原地写 parent_id / children_ids）。
        """
        by_title: dict[str, dict] = {}
        for n in order:
            by_title.setdefault(n["title"], n)  # catalog 兜底用：目录条目标题 → 骨架节点
            n["children_ids"] = []
        for n in order:
            parent: dict | None = None
            pp = _resolve_parent(n["clause_path"], by_path)
            if pp is not None:
                parent = by_path.get(pp)
            if parent is None and n.get("_catalog"):  # 号段失效 → catalog 归属兜底
                cand = by_title.get(n["_catalog"])
                if cand is not None and cand["node_id"] != n["node_id"]:
                    parent = cand
            n["parent_id"] = parent["node_id"] if parent is not None else None
            if parent is not None:
                parent["children_ids"].append(n["node_id"])

    # -- ④ 剪枝 + 祖先链 -------------------------------------------------------

    @staticmethod
    def _prune(order: list[dict]) -> list[dict]:
        """剪除空正文节点（骨架恒留），级联剪到稳定；原地修父节点 children_ids。

        功能：节点保留条件 = 骨架 / 有正文 / 有表格 / 有存活子节点。剪一个空叶可能令其
            父变空叶，故循环到稳定。删除时从父 children_ids 摘除，保证树一致。

        参数：
            order (list[dict]): 已连边的全部节点。
        返回：
            list[dict]: 存活节点（保持原创建序）。
        """
        alive: dict[str, dict] = {n["node_id"]: n for n in order}
        changed = True
        while changed:
            changed = False
            for n in order:
                nid = n["node_id"]
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
        return [n for n in order if n["node_id"] in alive]

    @staticmethod
    def _attach_ancestors(nodes: list[dict]) -> None:
        """原地算定祖先链：沿 parent_id 上溯，写 ancestor_titles / ancestor_paths（不含自身）。

        参数：
            nodes (list[dict]): 已连边（含 parent_id）的节点列表。
        返回：
            无（原地写 ancestor_titles / ancestor_paths，自顶向下）。
        """
        by_id = {n["node_id"]: n for n in nodes}
        for n in nodes:
            titles: list[str] = []
            paths: list[str] = []
            cur = by_id.get(n["parent_id"]) if n["parent_id"] else None
            seen: set[str] = set()
            while cur is not None and cur["node_id"] not in seen:
                seen.add(cur["node_id"])
                titles.append(cur["title"])
                paths.append(cur["clause_path"])
                cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
            n["ancestor_titles"] = titles[::-1]
            n["ancestor_paths"] = paths[::-1]
