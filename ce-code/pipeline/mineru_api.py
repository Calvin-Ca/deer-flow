"""远程 MinerU API 客户端：调 `POST /file_parse` 解析 PDF，落盘成标准 auto/ 布局。

这是管线的**默认解析方式**（见 README「Step 3 方式 A」）。相比本地 CLI：
- 无需本地 GPU / MinerU 环境，调用方零依赖
- API 主机常驻热服务，单页 ~1.8s（CLI 每次冷启动 ~23s 模型加载）
- API 主机 vllm 正常，`hybrid-auto-engine`（定额表逐列对位）现成可用；
  本地 venv 的 vllm 当前 ABI 损坏，CLI 跑不了 hybrid（详见 DEV.md）

实现要点：请求 `response_format_zip=true`，API 返回的 ZIP 就是标准 MinerU 布局
（`<basename>/auto/<basename>.md` + `_content_list.json` + `images/`），解压即与
CLI 产出完全一致，下游 02_parse_hierarchy.py 无需改动。

注意：API 每次调用都会重传整个 PDF。大 PDF 用 start_page/end_page 分段时会重复
上传，若要省带宽走本地分块（01_split_and_parse.py + --local），见 DEV.md。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import requests

# 远程 MinerU API 主机（版本 3.2.1，vllm 正常）。共享环境细节见 DEV.md。
DEFAULT_SERVER_URL = "http://172.19.2.2:8000"
DEFAULT_BACKEND = "hybrid-auto-engine"


def parse_pdf_via_api(
    pdf_path: Path,
    output_dir: Path,
    *,
    backend: str = DEFAULT_BACKEND,
    lang: str = "ch",
    start_page: int = 0,
    end_page: int = 99999,
    formula: bool = True,
    table: bool = True,
    server_url: str = DEFAULT_SERVER_URL,
    timeout: int = 1800,
) -> Path:
    """调远程 MinerU API 解析 pdf_path，把返回的 ZIP 解压到 output_dir。

    产出 output_dir/<basename>/auto/{<basename>.md, <basename>_content_list.json,
    images/}，布局与本地 CLI 完全一致。返回 auto 目录路径。

    解析失败（HTTP 非 2xx 或返回的不是 ZIP）时抛 RuntimeError，带上服务端错误信息。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    url = server_url.rstrip("/") + "/file_parse"

    # response_format_zip=true → 返回标准 MinerU 目录布局的 ZIP；return_images 一并打包。
    data = {
        "backend": backend,
        "lang_list": lang,
        "parse_method": "auto",
        "formula_enable": str(formula).lower(),
        "table_enable": str(table).lower(),
        "return_md": "true",
        "return_content_list": "true",
        "return_images": "true",
        "response_format_zip": "true",
        "start_page_id": str(start_page),
        "end_page_id": str(end_page),
    }

    with open(pdf_path, "rb") as f:
        files = {"files": (pdf_path.name, f, "application/pdf")}
        resp = requests.post(url, data=data, files=files, timeout=timeout)

    # 失败时服务端返回 JSON 错误体（不是 ZIP），透出来便于排查
    if resp.status_code != 200 or not resp.content[:2] == b"PK":
        detail = resp.text[:500] if resp.content[:2] != b"PK" else f"HTTP {resp.status_code}"
        raise RuntimeError(f"MinerU API 解析失败（{url}）: {detail}")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        zf.extractall(output_dir)

    auto_dir = output_dir / pdf_path.stem / "auto"
    if not auto_dir.exists():
        # ZIP 顶层目录名可能与 stem 不符（API 对超长/中文文件名做 sanitize/截断）。
        # 必须从**本次 ZIP 的 namelist**里定位 auto 目录，绝不能 rglob 整个 output_dir
        # ——那会命中 data/parsed/ 下的历史解析，把别的规范的旧产物误报成本次输出。
        rel = next((n for n in names if n.endswith("_content_list.json")), None)
        if rel is None:
            raise RuntimeError(
                f"ZIP 已解压但内部找不到 _content_list.json，API 返回内容异常：{names[:20]}"
            )
        auto_dir = (output_dir / rel).parent
    return auto_dir
