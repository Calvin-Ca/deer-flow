"""MinerU 解析工具 —— 一个文件 = 一个工具（适配器 + 门面）。

单文件聚齐 MinerU 的全生命周期：
  ① FormatAdapter   阶段1 适配的纯逻辑：MinerU v1 content_list → 统一块（page 归一、HTML 表格
                    展开 colspan/rowspan、text_level 透传、block_idx 溯源）。独立类，可单独单测。
  ② MineruParser    门面（factory 注册的工具实体），对外两个对称动词 + 一个 CLI 薄壳：
                      · ``adapt(raw)``  阶段1：content_list → ``Document``（统一契约，调 ①）。
                      · ``run(pdf)``    阶段0：调远程 MinerU API 把 PDF 跑成标准布局（无 click，
                        可程序化调用，返回 auto 目录 / 抛 RuntimeError）。
                      · ``run_cli()``   阶段0 的 click 命令（``run`` 的薄壳，含终端呈现）。

MinerU 是**一个解析工具的两个阶段**：阶段0（``run``）最贵、只跑一次，产物缓存于
``data/parsed/<std>/auto/``（不可变）；阶段1（``adapt``）把缓存转成 IR，被 ``service/build_service.py``
消费。``adapt`` 是所有解析工具的统一契约（``Parser.adapt``）；``run`` / ``run_cli`` 是工具私有的阶段0
能力（不进 ABC，鸭子类型），``parser.__main__`` 遍历注册表把有 ``run_cli`` 的工具挂成
``python -m parser <工具>``（占位工具无 ``run_cli`` → 不出现）。
"""
from __future__ import annotations

import io
import sys
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import click
import requests
from rich.console import Console

from core.document import Block, Document
from parser.base import Parser

console = Console()

# 项目根目录（ce-code/），本模块在 parser/ 下
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "data" / "parsed"

# 远程 MinerU API 主机（版本 3.2.1，vllm 正常）。共享环境细节见 DEV.md。
DEFAULT_SERVER_URL = "http://172.19.2.2:8000"
DEFAULT_BACKEND = "hybrid-auto-engine"


# ===========================================================================
# ① 阶段1 适配 —— FormatAdapter：MinerU v1 content_list → 统一块（纯逻辑，可独立单测）
# ===========================================================================

class _HTMLTableParser(HTMLParser):
    """把单个 ``<table>`` HTML 解析成「行→(文本, colspan, rowspan)」原始单元格列表。

    参数：
        无（构造后调用 feed(html)，结果从 raw_rows 取）。
    返回：
        无（数据存于 self.raw_rows）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.raw_rows: list[list[tuple[str, int, int]]] = []
        self._row: list[tuple[str, int, int]] | None = None
        self._buf: list[str] | None = None
        self._colspan = 1
        self._rowspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """开标签：``<tr>`` 起新行，``<td>/<th>`` 起新单元格并读出 colspan/rowspan。"""
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            a = dict(attrs)
            self._buf = []
            self._colspan = int(a.get("colspan") or 1)
            self._rowspan = int(a.get("rowspan") or 1)

    def handle_data(self, data: str) -> None:
        """单元格内文本：累积进当前缓冲。"""
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        """闭标签：``</td>`` 收单元格，``</tr>`` 收整行。"""
        if tag in ("td", "th") and self._buf is not None and self._row is not None:
            self._row.append(("".join(self._buf).strip(), self._colspan, self._rowspan))
            self._buf = None
        elif tag == "tr" and self._row is not None:
            self.raw_rows.append(self._row)
            self._row = None


def _expand_spans(raw_rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """带 colspan/rowspan 的原始单元格 → 矩形二维表（列对齐，防串列）。

    参数：
        raw_rows (list): _HTMLTableParser.raw_rows，每行为 (text, colspan, rowspan) 列表。
    返回：
        list[list[str]]: 展开后的矩形网格，行列严格对齐。
    """
    grid: list[list[str]] = []
    carry: dict[int, list] = {}
    for raw in raw_rows:
        row: list[str] = []
        col = 0
        ci = 0
        while ci < len(raw) or any(k >= col for k in carry):
            if col in carry:
                remaining, val = carry[col]
                row.append(val)
                if remaining - 1 > 0:
                    carry[col] = [remaining - 1, val]
                else:
                    del carry[col]
                col += 1
                continue
            if ci >= len(raw):
                break
            text, cspan, rspan = raw[ci]
            ci += 1
            for k in range(cspan):
                val = text if k == 0 else ""
                row.append(val)
                if rspan > 1:
                    carry[col] = [rspan - 1, val]
                col += 1
        grid.append(row)
    return grid


def _html_table_to_rows(html: str) -> list[list[str]]:
    """HTML ``<table>`` 串 → 矩形二维表体；空串返回空表。

    参数：
        html (str): HTML 表格字符串。
    返回：
        list[list[str]]: 二维表格内容。
    """
    if not html:
        return []
    parser = _HTMLTableParser()
    parser.feed(html)
    return _expand_spans(parser.raw_rows)


class FormatAdapter:
    """MinerU v1 格式适配器：纯格式转换，无结构语义（逐步动作见 adapt 方法）。

    本适配器是 MinerU v1 content_list schema 专用，复用维度是「任意基于 mineru 的 parser」，
    与切分层（splitter）无关——它只产统一块，切分/建树（条文号 / 目录标签 / 树边）由 splitter 消费。

    adapt(items) 返回的统一元素 schema：
        type       元素类型（text / list / table / equation / footer）
        text       文本内容（list 为空字符串，table 为 caption）
        page       页码（从 1 起）
        text_level MinerU 标题层级（原样透传；仅标题块有此键，缺则非标题）
        block_idx  原始 content_list 中的下标（供节点 provenance 溯源回 data/parsed）
        list_items list 条目列表（仅 list）
        body       矩形二维表体（仅 table）
        img_path   裁切图路径（仅 table）
    """

    @staticmethod
    def adapt(items: list[dict]) -> list[dict]:
        """MinerU v1 原始列表 → 统一元素列表（格式归一，不解析结构语义）。

        逐元素把 MinerU 私有 schema 削成项目统一 IR，只做版面/内容层面的转换，
        **不碰结构语义**（条文号 / 目录标签 / 树边都留给下游 splitter 算）。具体动作：
          · 页码归一    ：page_idx（0-base）→ page（1-base）。
          · 丢页码块    ：type == "page_number" 直接跳过。
          · 丢空文本    ：text.strip() 为空的块跳过。
          · 表格 caption：table_caption（数组）→ 拼成单个 text 字符串。
          · 表格展开    ：table_body（HTML 串）→ body（矩形二维数组），展开
                          colspan/rowspan 防串列（唯一的重逻辑转换）。
          · list 清洗   ：list_items 去空白、丢空条目；整条 list 为空则丢弃。
          · 加溯源下标  ：注入 block_idx（原 content_list 下标），供 Chunk 溯源回阶段0 缓存。
          · text_level  ：标题层级原样透传，仅标题块带此键。
          · 字段裁剪    ：只留 type/text/page/block_idx/text_level/list_items/body/img_path，
                          其余（bbox、score 等版面元数据）全丢。
        整体是「内容保真、结构不动、字段精简」：元素顺序/数量基本不变（仅删 page_number 与
        空块），文本一字不改，最大实质转换是表格 HTML → 二维数组。

        参数：
            items (list[dict]): MinerU v1 输出的扁平元素 list。
        返回：
            list[dict]: 统一元素列表（schema 见类 docstring）。
        """
        out: list[dict] = []
        for idx, it in enumerate(items):
            t = it.get("type", "text")
            page = it.get("page_idx", 0) + 1

            if t == "page_number":
                continue

            if t == "table":
                parts = it.get("table_caption") or []
                caption = " ".join(s.strip() for s in parts if isinstance(s, str) and s.strip())
                out.append({
                    "type": "table",
                    "text": caption,
                    "page": page,
                    "block_idx": idx,
                    "body": _html_table_to_rows(it.get("table_body", "")),
                    "img_path": it.get("img_path", ""),
                })
                continue

            if t == "list":
                list_items = [s.strip() for s in (it.get("list_items") or []) if s.strip()]
                if list_items:
                    out.append({
                        "type": "list",
                        "text": "",
                        "page": page,
                        "block_idx": idx,
                        "list_items": list_items,
                    })
                continue

            text = it.get("text", "").strip()
            if not text:
                continue
            elem = {
                "type": t,
                "text": text,
                "page": page,
                "block_idx": idx,
            }
            if "text_level" in it:  # MinerU 标题层级：原样透传，仅标题块有此键
                elem["text_level"] = it["text_level"]
            out.append(elem)
        return out


# ===========================================================================
# ② 门面 —— MineruParser：adapt（阶段1）+ run（阶段0 引擎）+ run_cli（阶段0 CLI 薄壳）
# ===========================================================================

class MineruParser(Parser):
    """MinerU 解析工具门面：阶段1 ``adapt`` + 阶段0 ``run`` / ``run_cli``。"""

    name = "mineru"

    # ── 阶段1：content_list → Document（统一契约，调 FormatAdapter）──
    def adapt(self, raw: object, *, standard_id: str = "", source_file: str = "") -> Document:
        """MinerU content_list 列表 → Document（阶段1）。

        参数：
            raw (list[dict]): MinerU v1 content_list.json 反序列化产物。
            standard_id (str): 规范标识。
            source_file (str): content_list.json 路径（相对 data/parsed/）。
        返回：
            Document: blocks 为统一元素（FormatAdapter 归一产物）。
        """
        items = raw if isinstance(raw, list) else []
        blocks = [Block.from_dict(d) for d in FormatAdapter.adapt(items)]
        return Document(standard_id=standard_id, source_file=source_file, blocks=blocks)

    # ── 阶段0：PDF → content_list.json（调远程 MinerU API，无 click，可程序化调用）──
    def run(
        self,
        pdf_path: Path,
        output_dir: Path = DEFAULT_OUTPUT,
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
        """调远程 MinerU API 解析 pdf_path，把返回的 ZIP 解压到 output_dir（阶段0）。

        请求 ``response_format_zip=true``，API 返回的 ZIP 就是标准 MinerU 布局，解压即得
        ``<basename>/auto/{md, _content_list.json, images/}``。API 主机资源充足、整本一次解析无 OOM。

        参数：
            pdf_path (Path): 待解析 PDF。
            output_dir (Path): 解压根目录，默认 data/parsed/。
            backend / lang / start_page / end_page / formula / table: MinerU 参数。
            server_url (str): API 地址。
            timeout (int): 请求超时（秒）。
        返回：
            Path: ``output_dir/<basename>/auto`` 目录。
        异常：
            RuntimeError: HTTP 非 2xx、返回的不是 ZIP、或 ZIP 内找不到 content_list.json。
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

    # ── 阶段0 CLI：run 的薄壳（声明工具私有参数 + 终端呈现），由 __main__ 通用挂载 ──
    def run_cli(self) -> click.Command:
        """阶段0 实跑入口：返回包装 ``self.run`` 的 click 命令（单 PDF，远程 API）。

        参数：无。
        返回：
            click.Command: 解析单 PDF 的命令；由 ``parser.__main__`` 挂载为 ``python -m parser mineru``。
        """
        run = self.run  # 闭包捕获绑定方法

        @click.command(name=self.name)
        @click.option(
            "--pdf", "pdf_path",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            required=True,
            help="规范 PDF 路径（一般放在 data/raw/ 下）。",
        )
        @click.option(
            "--output-dir", "output_dir",
            type=click.Path(file_okay=False, path_type=Path),
            default=DEFAULT_OUTPUT,
            help="解析输出根目录，默认 data/parsed/。",
        )
        @click.option(
            "--backend",
            default=DEFAULT_BACKEND, show_default=True,
            help="解析后端。hybrid-auto-engine=含 VLM 图示理解（定额表逐列对位，需 vllm）；"
            "pipeline=纯 OCR+版面（无 VLM 依赖，密集表格会错位）。",
        )
        @click.option(
            "--server-url",
            default=DEFAULT_SERVER_URL, show_default=True,
            help="远程 MinerU API 地址。",
        )
        @click.option(
            "--lang", default="ch", show_default=True,
            help="OCR 语言，定额/规范类用 ch。",
        )
        def _cmd(pdf_path: Path, output_dir: Path, backend: str, server_url: str, lang: str) -> None:
            """单 PDF 解析（远程 MinerU API）。"""
            console.rule(f"[bold]MinerU 解析：{pdf_path.name}[/bold]")
            console.print(f"  输入 PDF  : {pdf_path}")
            console.print(f"  输出目录  : {output_dir}")
            console.print(f"  方式      : 远程 API（{server_url}）")
            console.print(f"  后端      : {backend}")
            console.print()

            t0 = time.perf_counter()
            try:
                auto_dir = run(pdf_path, output_dir, backend=backend, lang=lang, server_url=server_url)
            except Exception as e:  # noqa: BLE001
                console.print(f"\n[red]✗ 解析失败：{e}[/red]")
                sys.exit(1)
            elapsed = time.perf_counter() - t0
            elapsed_str = f"{elapsed:.1f}s" if elapsed < 60 else f"{int(elapsed // 60)}m{elapsed % 60:.1f}s"

            # 输出位置（MinerU 会自己建 <basename>/auto/ 子目录）
            if auto_dir.exists():
                md_file = next(auto_dir.glob("*.md"), None)
                json_file = next(auto_dir.glob("*_content_list.json"), None)
                console.print("\n[green]✓ 解析完成[/green]")
                console.print(f"  解析耗时   : {elapsed_str}")
                console.print(f"  Markdown   : {md_file}")
                console.print(f"  Structured : {json_file}")
                console.print(
                    f"\n[bold]下一步（人工 review）[/bold]：打开 {md_file} 对照原 PDF，"
                    "按 README.md 的『评估维度』逐项打分。"
                )
            else:
                console.print(
                    f"\n[yellow]⚠ 找不到预期的输出目录 {auto_dir}——"
                    f"MinerU 版本可能改了输出布局，请进 {output_dir} 看实际产物。[/yellow]"
                )

        return _cmd
