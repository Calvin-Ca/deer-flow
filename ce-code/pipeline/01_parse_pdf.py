"""阶段 0 第一步：用 MinerU 把规范 PDF 解析成 markdown + 资源（表格、图片等）。

这是 POC 的最小可执行版本——直接 shell out 到 MinerU CLI，
不调 Python API（API 在不同版本间变化较大；CLI 更稳定）。

跑通后，输出会落在 data/parsed/<pdf-basename>/，目录结构典型如下：
    <basename>/
      auto/
        <basename>.md          ← 主输出，markdown 形式的全文
        <basename>_content_list.json  ← 结构化版本（标题/段落/表格/图片分块）
        images/                ← 图片资源
        <basename>_model.json  ← OCR/版面分析的中间产物（调试用）

下一步（02_extract_clauses.py，未实现）将基于 _content_list.json 把
markdown/结构化输出转成"条款树"。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

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
        "  请先在服务器上跑 [bold]bash pipeline/setup_server.sh[/bold]。"
    )
    sys.exit(1)


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
    "--device",
    type=click.Choice(["cuda", "cpu"], case_sensitive=False),
    default="cuda",
    help="加速设备。GPU 服务器默认 cuda。",
)
def main(pdf_path: Path, output_dir: Path, device: str) -> None:
    cli = detect_mineru_cli()
    output_dir.mkdir(parents=True, exist_ok=True)

    console.rule(f"[bold]MinerU 解析：{pdf_path.name}[/bold]")
    console.print(f"  CLI       : {cli}")
    console.print(f"  设备      : {device}")
    console.print(f"  输入 PDF  : {pdf_path}")
    console.print(f"  输出目录  : {output_dir}")
    console.print()

    # MinerU 新版（mineru）和旧版（magic-pdf）参数略有不同。
    # 新版：mineru -p <pdf> -o <out_dir> --device <cpu|cuda>
    # 旧版：magic-pdf -p <pdf> -o <out_dir> -m auto
    # 这里以新版为主，旧版做兜底。
    if cli == "mineru":
        cmd = [cli, "-p", str(pdf_path), "-o", str(output_dir), "--device", device]
    else:  # magic-pdf
        cmd = [cli, "-p", str(pdf_path), "-o", str(output_dir), "-m", "auto"]

    console.print(f"[dim]$ {' '.join(cmd)}[/dim]\n")

    # 直接透传 stdout/stderr，方便看 MinerU 自己的进度条
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        console.print(f"\n[red]✗ MinerU 退出码 {result.returncode}[/red]")
        sys.exit(result.returncode)

    # 输出位置（MinerU 会自己建 <basename>/auto/ 子目录）
    basename = pdf_path.stem
    auto_dir = output_dir / basename / "auto"
    if auto_dir.exists():
        md_file = next(auto_dir.glob("*.md"), None)
        json_file = next(auto_dir.glob("*_content_list.json"), None)
        console.print("\n[green]✓ 解析完成[/green]")
        console.print(f"  Markdown   : {md_file}")
        console.print(f"  Structured : {json_file}")
        console.print(
            f"\n[bold]下一步（人工 review）[/bold]：打开 {md_file} 对照原 PDF，"
            "按 README.md 的『评估维度』逐项打分。"
        )
    else:
        console.print(
            f"\n[yellow]⚠ 找不到预期的输出目录 {auto_dir}——"
            "MinerU 版本可能改了输出布局，请进 {output_dir} 看实际产物。[/yellow]"
        )


if __name__ == "__main__":
    main()
