"""IFC 原件与解析索引的存储后端（MinIO 优先，本地文件系统回退）。

BIM 底座是项目模型单一 owner，需把上传的 IFC 原件与其解析索引持久化：
- **原件**供前端 web-ifc 客户端渲染拉取（与后端取量同源，保 GlobalId 对齐）；
- **索引**（构件/量/属性/空间树）供 BIM 原语只读查询，避免每次请求重解析。

后端优先 MinIO（生产，与 ce-cost 复用同一对象存储），未配置时回退本地文件系统
（开发/单测）。两后端实现同一 ``Store`` 协议，上层 api 不感知差异。
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Protocol

from common import config


class Store(Protocol):
    """存储后端协议 —— IFC 原件与解析索引的读写。

    功能：抽象 MinIO / 本地 FS 两种后端，按 ``model_id`` 存取原件与索引 JSON。
    参数：各方法的 ``model_id`` 为模型唯一标识（取 IFC 字节的 sha256 前缀）。
    返回：见各方法签名。
    """

    def put_ifc(self, model_id: str, data: bytes) -> None: ...
    def get_ifc(self, model_id: str) -> bytes: ...
    def has(self, model_id: str) -> bool: ...
    def put_index(self, model_id: str, index: dict) -> None: ...
    def get_index(self, model_id: str) -> dict | None: ...
    def health(self) -> dict: ...


class LocalStore:
    """本地文件系统存储后端（开发/单测；MinIO 未配置时启用）。

    功能：把原件与索引落在 ``{root}/{model_id}/{model.ifc,index.json}``。
    参数：``root`` 存储根目录（来自 ``config.STORE_DIR``）。
    返回：实例；方法行为见 ``Store`` 协议。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, model_id: str) -> Path:
        d = self.root / model_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put_ifc(self, model_id: str, data: bytes) -> None:
        (self._dir(model_id) / "model.ifc").write_bytes(data)

    def get_ifc(self, model_id: str) -> bytes:
        return (self.root / model_id / "model.ifc").read_bytes()

    def has(self, model_id: str) -> bool:
        return (self.root / model_id / "model.ifc").exists()

    def put_index(self, model_id: str, index: dict) -> None:
        (self._dir(model_id) / "index.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8"
        )

    def get_index(self, model_id: str) -> dict | None:
        p = self.root / model_id / "index.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def health(self) -> dict:
        """探活本地存储后端。

        功能：确认存储根目录存在且可写，供 /health 上报。
        参数：无。
        返回：``{"backend": "local", "ok": bool, "path": str}``（不可写时附 ``error``）。
        """
        out = {"backend": "local", "path": str(self.root)}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".health"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            out["ok"] = True
        except Exception as exc:  # noqa: BLE001
            out["ok"] = False
            out["error"] = str(exc)
        return out


class MinioStore:
    """MinIO 对象存储后端（生产）。

    功能：object 键为 ``{model_id}/model.ifc`` 与 ``{model_id}/index.json``；首次连接确保 bucket 存在。
    参数：无（读 ``config`` 的 MinIO 端点/凭据/bucket）。
    返回：实例；方法行为见 ``Store`` 协议。
    """

    def __init__(self) -> None:
        from minio import Minio  # 延迟导入：本地无 minio 也能起 LocalStore

        self.bucket = config.MINIO_BUCKET
        self.client = Minio(
            config.MINIO_ENDPOINT,
            access_key=config.MINIO_ACCESS_KEY,
            secret_key=config.MINIO_SECRET_KEY,
            secure=config.MINIO_SECURE,
        )
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def _put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            self.bucket, key, io.BytesIO(data), length=len(data), content_type=content_type
        )

    def put_ifc(self, model_id: str, data: bytes) -> None:
        self._put(f"{model_id}/model.ifc", data, "application/octet-stream")

    def get_ifc(self, model_id: str) -> bytes:
        return self.client.get_object(self.bucket, f"{model_id}/model.ifc").read()

    def has(self, model_id: str) -> bool:
        from minio.error import S3Error

        try:
            self.client.stat_object(self.bucket, f"{model_id}/model.ifc")
            return True
        except S3Error:
            return False

    def put_index(self, model_id: str, index: dict) -> None:
        self._put(
            f"{model_id}/index.json",
            json.dumps(index, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )

    def get_index(self, model_id: str) -> dict | None:
        from minio.error import S3Error

        try:
            raw = self.client.get_object(self.bucket, f"{model_id}/index.json").read()
            return json.loads(raw)
        except S3Error:
            return None

    def health(self) -> dict:
        """探活 MinIO 后端连通性。

        功能：调 ``bucket_exists`` 触发一次实连，确认端点可达、凭据有效、bucket 在，供 /health 上报。
        参数：无。
        返回：``{"backend": "minio", "ok": bool, "endpoint": str, "bucket": str}``（失败时附 ``error``）。
        """
        out = {"backend": "minio", "endpoint": config.MINIO_ENDPOINT, "bucket": self.bucket}
        try:
            out["ok"] = bool(self.client.bucket_exists(self.bucket))
        except Exception as exc:  # noqa: BLE001 — 连接/凭据失败都视为不健康
            out["ok"] = False
            out["error"] = str(exc)
        return out


_store: Store | None = None


def get_store() -> Store:
    """返回进程内单例存储后端。

    功能：按 ``config.store_backend()`` 选 MinIO 或本地 FS，懒构造并缓存。
    参数：无。
    返回：``Store`` 实例（``MinioStore`` 或 ``LocalStore``）。
    """
    global _store
    if _store is None:
        _store = MinioStore() if config.store_backend() == "minio" else LocalStore(config.STORE_DIR)
    return _store
