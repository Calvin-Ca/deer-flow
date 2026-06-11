"""IfcOpenShell 解析 —— IFC 字节 → 解析索引（构件 + 基础几何量 + 属性 + 空间结构）。

BIM 底座的取数核心：把上传的 IFC 一次性解析为只读索引，落存储后供原语查询，
**避免每次请求重解析**。三条铁律落在这里（见 ``ce-bim/PRD.md §1``）：
- **GlobalId 是连接键**：每个构件以 ``global_id`` 标识，贯穿索引/原语/前端渲染/消费方回填；
- **几何只读、量从 IFC 确定性来**：基础几何量取自 IfcOpenShell（``IfcElementQuantity``），不让模型猜；
- 造价扣减/清单、规范校验等**不在此**（归各消费方）。

ifcopenshell 延迟导入：未安装时 FastAPI 应用仍可启动（/health 可用），仅解析时报错。
"""
from __future__ import annotations

import tempfile
from pathlib import Path


def _extract_quantities(element, ue) -> dict:
    """提取构件的基础几何量（IfcElementQuantity）。

    功能：汇总该构件所有数量集（Qto_*）里的量值，扁平为 名称→数值。
    参数：``element`` IfcElement 实例；``ue`` ``ifcopenshell.util.element`` 模块。
    返回：``dict[str, float|int]``（如 ``{"NetVolume": 1.2, "Length": 3.0}``）；无量则空 dict。
    """
    out: dict = {}
    for qset in (ue.get_psets(element, qtos_only=True) or {}).values():
        for name, value in qset.items():
            if name == "id":  # get_psets 注入的 pset 实体 id，非量值
                continue
            out[name] = value
    return out


def _extract_properties(element, ue) -> dict:
    """提取构件的属性集（IfcPropertySet，不含数量集）。

    功能：取该构件的常规属性（材料/规格/防火等级等），按属性集名分组。
    参数：``element`` IfcElement 实例；``ue`` ``ifcopenshell.util.element`` 模块。
    返回：``dict[str, dict]``（``{pset_name: {prop: value}}``，已去除注入的 ``id`` 键）。
    """
    out: dict = {}
    for pset_name, pset in (ue.get_psets(element, psets_only=True) or {}).items():
        out[pset_name] = {k: v for k, v in pset.items() if k != "id"}
    return out


def _spatial_node(element, ue) -> dict:
    """递归构建单个空间结构节点（项目→场地→楼栋→层）。

    功能：以该空间元素为根，递归挂接下级空间元素（IfcRelAggregates），并统计直接归属其下的构件数（IfcRelContainedInSpatialStructure）。
    参数：``element`` IfcSpatialStructureElement/IfcProject 实例；``ue`` util 模块。
    返回：``dict``，含 ``global_id/type/name/element_count``，有下级时含 ``children``。
    """
    node = {
        "global_id": getattr(element, "GlobalId", None),
        "type": element.is_a(),
        "name": getattr(element, "Name", None),
    }

    children = []
    for rel in getattr(element, "IsDecomposedBy", None) or []:
        for child in rel.RelatedObjects:
            if child.is_a("IfcSpatialElement") or child.is_a("IfcSpatialStructureElement"):
                children.append(_spatial_node(child, ue))

    contained = 0
    for rel in getattr(element, "ContainsElements", None) or []:
        contained += len(rel.RelatedElements)
    node["element_count"] = contained

    if children:
        node["children"] = children
    return node


def parse_ifc(data: bytes) -> dict:
    """解析 IFC 字节为解析索引。

    功能：开模型，遍历 IfcElement 提取构件（GlobalId/类型/名称/楼层/量/属性），
          构建空间结构树与汇总计数，组装为可落存储的只读索引。
    参数：``data`` IFC 文件字节（来自 ``/model/ingest`` 上传）。
    返回：``dict`` 索引——
          ``{"elements": [...], "spatial_tree": {...}|None,
             "summary": {"element_count": int, "type_counts": {type: n},
                         "storey_counts": {storey: n}}}``。
    """
    import ifcopenshell
    import ifcopenshell.util.element as ue

    # ifcopenshell.open 需要文件路径，先把字节落临时文件
    with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        model = ifcopenshell.open(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    elements: list[dict] = []
    type_counts: dict[str, int] = {}
    storey_counts: dict[str, int] = {}

    for el in model.by_type("IfcElement"):
        container = ue.get_container(el)
        storey = getattr(container, "Name", None) if container is not None else None
        ifc_type = el.is_a()
        elements.append(
            {
                "global_id": el.GlobalId,
                "ifc_type": ifc_type,
                "name": getattr(el, "Name", None),
                "storey": storey,
                "quantities": _extract_quantities(el, ue),
                "properties": _extract_properties(el, ue),
            }
        )
        type_counts[ifc_type] = type_counts.get(ifc_type, 0) + 1
        if storey:
            storey_counts[storey] = storey_counts.get(storey, 0) + 1

    projects = model.by_type("IfcProject")
    spatial_tree = _spatial_node(projects[0], ue) if projects else None

    return {
        "elements": elements,
        "spatial_tree": spatial_tree,
        "summary": {
            "element_count": len(elements),
            "type_counts": type_counts,
            "storey_counts": storey_counts,
            "schema": model.schema,
        },
    }
