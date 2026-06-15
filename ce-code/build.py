"""build —— 知识库构建入口（薄壳，转 ``service.build_service.main``）。

编排实现已迁至 ``service/build_service.py``（解析 → 切分 → 表征 → 索引，三阶段单独执行）。本文件
保留为从 ce-code 根直跑的薄入口，``--stage`` 选只跑哪一阶段（阶段间靠 chunks.json 落盘解耦）：

  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --preview                # 只测切分
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --stage structure        # ①切分落盘
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --stage reprs            # ②挂表征
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --stage index            # ③建索引
"""
from __future__ import annotations

from service.build_service import main

if __name__ == "__main__":
    main()
