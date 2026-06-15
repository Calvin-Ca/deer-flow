"""build —— 知识库构建入口（薄壳，转 ``service.build_service.main``）。

编排实现已迁至 ``service/build_service.py``（一条命令跑完 解析 → 切分 → 表征 → 索引）。本文件
保留为从 ce-code 根直跑的薄入口：

  python build.py --input data/parsed/<std>/auto/<std>_content_list.json             # 全量建库
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --preview   # 只测切分不落盘
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --bm25-only # 无 Milvus 只建 BM25
"""
from __future__ import annotations

from service.build_service import main

if __name__ == "__main__":
    main()
