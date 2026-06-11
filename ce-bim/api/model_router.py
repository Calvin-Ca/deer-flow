"""BIM 原语路由 —— 按 GlobalId 寻址的 /model/* 端点（:8102）。

BIM 底座对外只暴露 BIM 原语，被各消费方（ce-cost 算量计价 / 审图 / FM）以纯 HTTP
客户端方式调用（见 ``ce-bim/PRD.md §2``）。本路由不含造价扣减/清单/组价、不含规范校验
——只做"存原件 + 解析索引 + 只读取数"。

端点：
  POST /model/ingest                上传 IFC → 存储 + 解析建索引 → 返回 model_id + 汇总
  GET  /model/{id}                   取 IFC 原件（前端 web-ifc 渲染拉取）
  GET  /model/{id}/elements          列构件（按 type/storey 过滤），每项带 GlobalId
  GET  /model/{id}/element/{guid}    单构件：属性 + 基础几何量 + 空间归属
  POST /model/{id}/quantity          按 GlobalId 批量取基础几何量（喂 ce-cost 算量引擎）
  GET  /model/{id}/spatial           空间结构树（项目→场地→楼栋→层→构件）
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from parse.ifc_parser import parse_ifc
from store.store import get_store

logger = logging.getLogger("bim.model")
router = APIRouter()


class QuantityRequest(BaseModel):
    """/model/{id}/quantity 请求体。"""

    global_ids: list[str] = Field(
        default_factory=list, description="目标构件 GlobalId 列表；为空则返回全部构件的量"
    )


def _load_index(model_id: str) -> dict:
    """加载模型解析索引，缺失则 404。

    功能：从存储后端取 ``index.json``，供各只读原语共用。
    参数：``model_id`` 模型唯一标识。
    返回：解析索引 ``dict``；不存在时抛 ``HTTPException(404)``。
    """
    index = get_store().get_index(model_id)
    if index is None:
        raise HTTPException(status_code=404, detail=f"模型不存在或未解析: {model_id}")
    return index


@router.post("/model/ingest")
async def ingest(file: UploadFile = File(...)) -> dict:
    """上传并解析 IFC 模型。

    功能：存原件（model_id 取字节 sha256 前缀，天然去重）+ IfcOpenShell 解析建索引 → 落存储。
    参数：``file`` 上传的 IFC 文件（multipart/form-data）。
    返回：``{model_id, filename, schema, element_count, type_counts}``。
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")

    model_id = hashlib.sha256(data).hexdigest()[:16]
    store = get_store()
    store.put_ifc(model_id, data)

    try:
        index = parse_ifc(data)
    except ImportError as exc:  # ifcopenshell 未安装
        logger.exception("ifcopenshell 不可用")
        raise HTTPException(
            status_code=503, detail=f"IFC 解析依赖不可用: {exc}（请 uv sync 安装 ifcopenshell）"
        ) from exc
    except Exception as exc:
        logger.exception("IFC 解析失败 model_id=%s", model_id)
        raise HTTPException(status_code=422, detail=f"IFC 解析失败: {exc}") from exc

    index["model_id"] = model_id
    index["filename"] = file.filename
    store.put_index(model_id, index)

    summary = index["summary"]
    logger.info(
        "ingest model_id=%s filename=%s elements=%d", model_id, file.filename, summary["element_count"]
    )
    return {
        "model_id": model_id,
        "filename": file.filename,
        "schema": summary.get("schema"),
        "element_count": summary["element_count"],
        "type_counts": summary["type_counts"],
    }


@router.get("/model/{model_id}")
def get_model(model_id: str) -> Response:
    """取 IFC 原件字节（前端 web-ifc 客户端渲染拉取）。

    功能：回原件二进制，与后端取量同源以保 GlobalId 对齐。
    参数：``model_id`` 模型唯一标识。
    返回：``application/octet-stream`` 响应；模型不存在抛 404。
    """
    store = get_store()
    if not store.has(model_id):
        raise HTTPException(status_code=404, detail=f"模型不存在: {model_id}")
    return Response(
        content=store.get_ifc(model_id),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{model_id}.ifc"'},
    )


@router.get("/model/{model_id}/elements")
def list_elements(model_id: str, type: str | None = None, storey: str | None = None) -> dict:
    """列出构件（可按 IFC 类型 / 楼层过滤）。

    功能：返回构件摘要（不含完整属性，列表场景轻量），每项带 GlobalId。
    参数：``model_id``；``type`` 按 ``ifc_type`` 精确过滤；``storey`` 按楼层名过滤。
    返回：``{model_id, count, elements: [{global_id, ifc_type, name, storey}]}``。
    """
    elements = _load_index(model_id)["elements"]
    if type:
        elements = [e for e in elements if e["ifc_type"] == type]
    if storey:
        elements = [e for e in elements if e["storey"] == storey]
    return {
        "model_id": model_id,
        "count": len(elements),
        "elements": [
            {"global_id": e["global_id"], "ifc_type": e["ifc_type"], "name": e["name"], "storey": e["storey"]}
            for e in elements
        ],
    }


@router.get("/model/{model_id}/element/{guid}")
def get_element(model_id: str, guid: str) -> dict:
    """取单个构件的完整信息（属性 + 基础几何量 + 空间归属）。

    功能：按 GlobalId 命中构件，供 viewer 属性面板 / 消费方决策用。
    参数：``model_id``；``guid`` 构件 GlobalId。
    返回：该构件索引项 ``dict``；未命中抛 404。
    """
    for e in _load_index(model_id)["elements"]:
        if e["global_id"] == guid:
            return e
    raise HTTPException(status_code=404, detail=f"构件不存在: {guid}")


@router.post("/model/{model_id}/quantity")
def get_quantities(model_id: str, req: QuantityRequest) -> dict:
    """按 GlobalId 批量取基础几何量（喂 ce-cost 算量引擎）。

    功能：返回指定构件的量值映射；``global_ids`` 为空则返回全部构件的量。
    参数：``model_id``；``req.global_ids`` 目标构件列表。
    返回：``{model_id, quantities: {global_id: {量名: 量值}}}``（缺失的 GlobalId 不在结果中）。
    """
    elements = _load_index(model_id)["elements"]
    wanted = set(req.global_ids)
    result = {
        e["global_id"]: e["quantities"]
        for e in elements
        if not wanted or e["global_id"] in wanted
    }
    return {"model_id": model_id, "quantities": result}


@router.get("/model/{model_id}/spatial")
def get_spatial(model_id: str) -> dict:
    """取空间结构树（项目→场地→楼栋→层→构件计数）。

    功能：供 viewer 按楼层/专业导航定位。
    参数：``model_id``。
    返回：``{model_id, spatial_tree: {...}|None}``。
    """
    return {"model_id": model_id, "spatial_tree": _load_index(model_id)["spatial_tree"]}
