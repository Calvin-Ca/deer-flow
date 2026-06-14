"""build —— 知识库构建入口（薄壳，转 ``service.build_service.main``）。

编排实现已迁至 ``service/build_service.py``（解析 → 切分 → 表征 → 索引，阶段 1→3）。本文件保留为
从 ce-code 根直跑的薄入口，命令与选项不变：

  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --preview                    # 只测切分
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --terminal-stage structure   # 切分落盘
  python build.py --input data/parsed/<std>/auto/<std>_content_list.json --terminal-stage index        # 全量建库
"""
from __future__ import annotations

from service.build_service import main

if __name__ == "__main__":
    main()
