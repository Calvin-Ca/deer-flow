"""BIM 底座层配置 —— 存储后端与服务端口（env 可覆盖）。

BIM 底座是项目模型的单一 owner，自身**不依赖**知识服务(:8100)/任务服务(:8101)，
也不碰关系库/向量库/LLM（见 ``ce-bim/PRD.md``）。这里只配置两件事：
1. IFC 原件与解析索引的存储后端 —— MinIO 优先（生产，与 ce-cost 复用同一对象存储），
   未配置 ``BCBIM_MINIO_ENDPOINT`` 时回退本地文件系统（开发/单测）；
2. 服务监听地址与端口（:8102）。
"""
from __future__ import annotations

import os
from pathlib import Path

# 服务监听
HOST = os.environ.get("BCBIM_HOST", "0.0.0.0")
PORT = int(os.environ.get("BCBIM_PORT", "8102"))

# 本地文件系统存储根（MinIO 未配置时的回退后端；落盘处不入 git，见 .gitignore）
STORE_DIR = Path(
    os.environ.get(
        "BCBIM_STORE_DIR",
        str(Path(__file__).resolve().parent.parent / "data" / "models"),
    )
)

# MinIO（配置 ENDPOINT 即启用，否则用本地文件系统）
MINIO_ENDPOINT = os.environ.get("BCBIM_MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.environ.get("BCBIM_MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("BCBIM_MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.environ.get("BCBIM_MINIO_BUCKET", "ce-bim-models")
MINIO_SECURE = os.environ.get("BCBIM_MINIO_SECURE", "false").lower() == "true"


def store_backend() -> str:
    """返回当前生效的存储后端名。

    功能：根据是否配置 MinIO 端点决定后端，供 ``store.get_store`` 选型与 /health 上报。
    参数：无（读模块级 env 配置）。
    返回：``"minio"``（已配 ``BCBIM_MINIO_ENDPOINT``）或 ``"local"``（回退本地 FS）。
    """
    return "minio" if MINIO_ENDPOINT else "local"
