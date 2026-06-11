"""阶段 0 第一步：把规范 PDF 解析成 markdown + 资源（表格、图片等）。

**默认走远程 MinerU API**（`172.19.2.2:8000`，热服务 + hybrid 现成可用）；
加 --local 改用本地 MinerU CLI（shell out 到 mineru/magic-pdf）。两条路径产出布局
完全一致，下游 02_parse_hierarchy.py 无差别消费。选型/环境差异见 DEV.md。

无论哪种方式，输出都落在 data/parsed/<pdf-basename>/auto/：
    <basename>/
      auto/
        <basename>.md                 ← 主输出，markdown 全文
        <basename>_content_list.json  ← 结构化版本（标题/段落/表格/图片分块）
        images/                       ← 图片资源

下一步（02_parse_hierarchy.py）基于 _content_list.json 跑三轴层级化解析。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import click
from rich.console import Console

from mineru_api import DEFAULT_BACKEND, DEFAULT_SERVER_URL, parse_pdf_via_api

console = Console()

# 项目根目录（ce-code/），脚本在 pipeline/ 下
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "data" / "parsed"


def detect_mineru_cli() -> str:
    """探测当前环境装的是新版 mineru 还是旧版 magic-pdf。"""
    for candidate in ("mineru", "magic-pdf"):
        if shutil.which(candidate):
            return candidate
    console.print(
        "[red]✗ 找不到 MinerU CLI（既没有 mineru 也没有 magic-pdf）。[/red]\n"
        "  请先在服务器上跑 [bold]bash pipeline/setup_server.sh[/bold]，"
        "或去掉 --local 改用默认的远程 API。"
    )
    sys.exit(1)


def parse_local(pdf_path: Path, output_dir: Path, backend: str, device: str) -> Path:
    """本地 CLI 解析。返回 auto 目录。"""
    cli = detect_mineru_cli()
    console.print(f"  方式      : 本地 CLI（{cli}）")
    console.print(f"  后端      : {backend}")
    console.print(f"  设备      : {device}")

    # 新版：mineru -p <pdf> -o <out> --device <cpu|cuda> --backend <backend>
    # 旧版：magic-pdf -p <pdf> -o <out> -m auto（无 backend 概念）
    if cli == "mineru":
        cmd = [cli, "-p", str(pdf_path), "-o", str(output_dir), "--device", device, "--backend", backend]
    else:  # magic-pdf
        cmd = [cli, "-p", str(pdf_path), "-o", str(output_dir), "-m", "auto"]

    console.print(f"[dim]$ {' '.join(cmd)}[/dim]\n")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        console.print(f"\n[red]✗ MinerU 退出码 {result.returncode}[/red]")
        sys.exit(result.returncode)
    return output_dir / pdf_path.stem / "auto"


@click.command()
@click.option(
    "--pdf",
    "pdf_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="规范 PDF 路径（一般放在 data/raw/ 下）。",
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_OUTPUT,
    help="解析输出根目录，默认 data/parsed/。",
)
@click.option(
    "--local",
    "use_local",
    is_flag=True,
    default=False,
    help="改用本地 MinerU CLI；不加则走默认的远程 API。",
)
@click.option(
    "--backend",
    default=DEFAULT_BACKEND,
    show_default=True,
    help="解析后端。hybrid-auto-engine=含 VLM 图示理解（定额表逐列对位，需 vllm）；"
    "pipeline=纯 OCR+版面（无 VLM 依赖，密集表格会错位）。本地 venv 的 vllm 当前损坏，"
    "--local 跑 hybrid 会失败，详见 DEV.md。",
)
@click.option(
    "--server-url",
    default=DEFAULT_SERVER_URL,
    show_default=True,
    help="远程 MinerU API 地址（仅默认 API 方式生效）。",
)
@click.option(
    "--lang",
    default="ch",
    show_default=True,
    help="OCR 语言，定额/规范类用 ch。",
)
@click.option(
    "--device",
    type=click.Choice(["cuda", "cpu"], case_sensitive=False),
    default="cuda",
    help="加速设备（仅 --local 生效）。GPU 服务器默认 cuda。",
)
def main(
    pdf_path: Path,
    output_dir: Path,
    use_local: bool,
    backend: str,
    server_url: str,
    lang: str,
    device: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    console.rule(f"[bold]MinerU 解析：{pdf_path.name}[/bold]")
    console.print(f"  输入 PDF  : {pdf_path}")
    console.print(f"  输出目录  : {output_dir}")

    t0 = time.perf_counter()
    if use_local:
        auto_dir = parse_local(pdf_path, output_dir, backend, device)
    else:
        console.print(f"  方式      : 远程 API（{server_url}）")
        console.print(f"  后端      : {backend}")
        console.print()
        try:
            auto_dir = parse_pdf_via_api(
                pdf_path,
                output_dir,
                backend=backend,
                lang=lang,
                server_url=server_url,
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"\n[red]✗ 远程 API 解析失败：{e}[/red]")
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


if __name__ == "__main__":
    main()
