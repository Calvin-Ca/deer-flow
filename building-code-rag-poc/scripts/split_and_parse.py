"""分块解析长 PDF（解决 MinerU 处理 400+ 页 PDF 时的 OOM / 挂起问题）。

流程：
  1. 把大 PDF 切成若干 N 页小块（默认 80 页）
  2. 逐块调 MinerU 解析（已完成的块自动跳过，支持断点续传）
  3. 合并所有块的 _content_list.json（修正 page_idx 偏移）
  4. 合并 Markdown 全文
  5. 合并 images/ 目录（统一路径引用）

使用方式：
  # 基本用法（80 页 / 块）
  .venv/bin/python scripts/split_and_parse.py \\
    --pdf data/raw/GB50016-2014_2018.pdf

  # 调小块大小（内存不足时）
  .venv/bin/python scripts/split_and_parse.py \\
    --pdf data/raw/GB50016-2014_2018.pdf --chunk-size 50

  # 只跑特定块（调试用，块编号从 0 开始）
  .venv/bin/python scripts/split_and_parse.py \\
    --pdf data/raw/GB50016-2014_2018.pdf --only-chunk 3

输出：data/parsed/<basename>/auto/（与 01_parse_pdf.py 输出格式完全一致，
      可直接传给 02_extract_clauses.py）
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click
from rich.console import Console
from rich.progress import track

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "data" / "parsed"
DEFAULT_CHUNK_SIZE = 80


# ---------------------------------------------------------------------------
# PDF 分割（优先用 pymupdf，回退到 pypdf）
# ---------------------------------------------------------------------------

def split_pdf_pymupdf(pdf_path: Path, chunk_dir: Path, chunk_size: int) -> list[tuple[Path, int]]:
    """用 pymupdf(fitz) 分割，返回 [(chunk_path, start_page), ...]。"""
    import fitz  # type: ignore

    doc = fitz.open(str(pdf_path))
    total = doc.page_count
    console.print(f"  总页数: {total}，每块 {chunk_size} 页，共 {-(-total // chunk_size)} 块")

    chunks = []
    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)  # exclusive
        chunk_path = chunk_dir / f"chunk_{start:04d}_{end - 1:04d}.pdf"
        if not chunk_path.exists():
            sub = fitz.open()
            sub.insert_pdf(doc, from_page=start, to_page=end - 1)
            sub.save(str(chunk_path))
            sub.close()
        chunks.append((chunk_path, start))

    doc.close()
    return chunks


def split_pdf_pypdf(pdf_path: Path, chunk_dir: Path, chunk_size: int) -> list[tuple[Path, int]]:
    """用 pypdf 分割，返回 [(chunk_path, start_page), ...]。"""
    from pypdf import PdfReader, PdfWriter  # type: ignore

    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    console.print(f"  总页数: {total}，每块 {chunk_size} 页，共 {-(-total // chunk_size)} 块")

    chunks = []
    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)
        chunk_path = chunk_dir / f"chunk_{start:04d}_{end - 1:04d}.pdf"
        if not chunk_path.exists():
            writer = PdfWriter()
            for i in range(start, end):
                writer.add_page(reader.pages[i])
            with open(chunk_path, "wb") as f:
                writer.write(f)
        chunks.append((chunk_path, start))

    return chunks


def split_pdf(pdf_path: Path, chunk_dir: Path, chunk_size: int) -> list[tuple[Path, int]]:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    try:
        import fitz  # noqa: F401
        return split_pdf_pymupdf(pdf_path, chunk_dir, chunk_size)
    except ImportError:
        pass
    try:
        import pypdf  # noqa: F401
        return split_pdf_pypdf(pdf_path, chunk_dir, chunk_size)
    except ImportError:
        pass
    console.print(
        "[red]✗ 找不到 pymupdf 或 pypdf。[/red]\n"
        "  请安装其中一个：\n"
        "    uv pip install --python .venv/bin/python pymupdf\n"
        "    uv pip install --python .venv/bin/python pypdf"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# MinerU 调用
# ---------------------------------------------------------------------------

def detect_mineru_cli() -> str:
    for candidate in ("mineru", "magic-pdf"):
        if shutil.which(candidate):
            return candidate
    console.print("[red]✗ 找不到 MinerU CLI（mineru / magic-pdf）[/red]")
    sys.exit(1)


def parse_chunk(chunk_path: Path, chunk_out_dir: Path, device: str, cli: str, backend: str = "pipeline") -> bool:
    """解析单块 PDF，返回是否成功。输出落在 chunk_out_dir/<chunk_stem>/auto/。"""
    if cli == "mineru":
        cmd = [cli, "-p", str(chunk_path), "-o", str(chunk_out_dir), "--device", device, "--backend", backend]
    else:
        cmd = [cli, "-p", str(chunk_path), "-o", str(chunk_out_dir), "-m", "auto"]

    # 直接透传 stdout/stderr，方便实时观察 MinerU 进度
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        console.print(f"[red]  ✗ {chunk_path.name} 失败（退出码 {result.returncode}）[/red]")
        return False
    return True


def chunk_done(chunk_path: Path, chunk_out_dir: Path) -> bool:
    """判断某块是否已经解析完成（content_list.json 存在且非空）。"""
    auto_dir = chunk_out_dir / chunk_path.stem / "auto"
    json_files = list(auto_dir.glob("*_content_list.json")) if auto_dir.exists() else []
    return bool(json_files and json_files[0].stat().st_size > 100)


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------

def merge_content_lists(
    chunks: list[tuple[Path, int]],
    chunk_out_dir: Path,
    merged_out_dir: Path,
    basename: str,
) -> Path:
    """合并所有块的 content_list.json，修正 page_idx，写到最终输出目录。"""
    merged: list[dict] = []

    for chunk_path, page_offset in chunks:
        auto_dir = chunk_out_dir / chunk_path.stem / "auto"
        json_files = list(auto_dir.glob("*_content_list.json"))
        if not json_files:
            console.print(f"[yellow]  ⚠ 跳过 {chunk_path.name}（无 content_list.json）[/yellow]")
            continue

        items: list[dict] = json.loads(json_files[0].read_text(encoding="utf-8"))
        for item in items:
            # 修正页码偏移
            if "page_idx" in item:
                item["page_idx"] = item["page_idx"] + page_offset
            # 修正图片路径（images/ 目录统一到最终输出）
            if item.get("type") == "image" and "img_path" in item:
                img_name = Path(item["img_path"]).name
                item["img_path"] = f"images/{img_name}"
        merged.extend(items)

    merged_out_dir.mkdir(parents=True, exist_ok=True)
    out_path = merged_out_dir / f"{basename}_content_list.json"
    out_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"  合并条目数: {len(merged)}")
    return out_path


def merge_markdown(
    chunks: list[tuple[Path, int]],
    chunk_out_dir: Path,
    merged_out_dir: Path,
    basename: str,
) -> Path:
    """按页序合并所有块的 Markdown 全文。"""
    parts: list[str] = []
    for chunk_path, _ in chunks:
        auto_dir = chunk_out_dir / chunk_path.stem / "auto"
        md_files = list(auto_dir.glob("*.md")) if auto_dir.exists() else []
        if md_files:
            parts.append(md_files[0].read_text(encoding="utf-8"))

    merged_out_dir.mkdir(parents=True, exist_ok=True)
    out_path = merged_out_dir / f"{basename}.md"
    out_path.write_text("\n\n".join(parts), encoding="utf-8")
    return out_path


def merge_images(
    chunks: list[tuple[Path, int]],
    chunk_out_dir: Path,
    merged_out_dir: Path,
) -> int:
    """把所有块的 images/ 合并到统一目录，返回总图片数。"""
    img_dir = merged_out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for chunk_path, _ in chunks:
        src_img_dir = chunk_out_dir / chunk_path.stem / "auto" / "images"
        if src_img_dir.exists():
            for img in src_img_dir.iterdir():
                dest = img_dir / img.name
                if not dest.exists():
                    shutil.copy2(img, dest)
                count += 1
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--pdf", "pdf_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="原始 PDF 路径（data/raw/ 下）。",
)
@click.option(
    "--output-dir", "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_OUTPUT,
    help="解析输出根目录，默认 data/parsed/。",
)
@click.option(
    "--chunk-size", default=DEFAULT_CHUNK_SIZE, show_default=True,
    help="每块页数。内存不足时调小（50~60）。",
)
@click.option(
    "--device",
    type=click.Choice(["cuda", "cpu"], case_sensitive=False),
    default="cuda",
    help="加速设备。",
)
@click.option(
    "--only-chunk", "only_chunk",
    default=None, type=int,
    help="只处理第 N 块（0 起）；不传则处理全部。",
)
@click.option(
    "--backend", default="pipeline", show_default=True,
    help="mineru 后端。pipeline=纯 OCR+版面检测（推荐，无 VLM 依赖）；hybrid-auto-engine=含 VLM 图示理解。",
)
@click.option(
    "--skip-merge", is_flag=True, default=False,
    help="跳过合并步骤（只解析各块）。",
)
def main(
    pdf_path: Path,
    output_dir: Path,
    chunk_size: int,
    device: str,
    backend: str,
    only_chunk: int | None,
    skip_merge: bool,
) -> None:
    cli = detect_mineru_cli()
    basename = pdf_path.stem

    # 临时目录存放分块 PDF 和各块解析结果
    chunk_pdf_dir = output_dir / f"{basename}_chunks" / "pdfs"
    chunk_out_dir = output_dir / f"{basename}_chunks" / "parsed"
    # 最终合并输出，格式与 01_parse_pdf.py 完全一致
    final_auto_dir = output_dir / basename / "auto"

    console.rule(f"[bold]分块解析：{pdf_path.name}[/bold]")
    console.print(f"  CLI         : {cli}")
    console.print(f"  后端        : {backend}")
    console.print(f"  设备        : {device}")
    console.print(f"  块大小      : {chunk_size} 页")
    console.print(f"  临时目录    : {chunk_pdf_dir.parent}")
    console.print(f"  最终输出    : {final_auto_dir}")
    console.print()

    # ── 1. 分割 ──
    console.print("[bold]步骤 1/3  分割 PDF[/bold]")
    chunks = split_pdf(pdf_path, chunk_pdf_dir, chunk_size)
    console.print(f"  共 {len(chunks)} 块\n")

    # ── 2. 逐块解析 ──
    console.print("[bold]步骤 2/3  逐块解析[/bold]")
    failed: list[int] = []
    for idx, (chunk_path, page_offset) in enumerate(chunks):
        if only_chunk is not None and idx != only_chunk:
            continue

        label = f"[{idx + 1}/{len(chunks)}] {chunk_path.name}  (第 {page_offset + 1} 页起)"

        if chunk_done(chunk_path, chunk_out_dir):
            console.print(f"  ✓ 跳过（已完成）: {label}")
            continue

        console.print(f"  → 解析中: {label}")
        ok = parse_chunk(chunk_path, chunk_out_dir, device, cli, backend)
        if ok:
            console.print(f"  [green]✓[/green] 完成: {chunk_path.name}")
        else:
            failed.append(idx)

    if failed:
        console.print(
            f"\n[red]⚠ {len(failed)} 块解析失败：块编号 {failed}[/red]\n"
            "  修复后用 --only-chunk <N> 重跑失败的块，再不加参数重跑合并步骤。"
        )
        if only_chunk is None:  # 全量跑时遇到失败，停止合并
            sys.exit(1)

    if skip_merge or only_chunk is not None:
        console.print("\n[yellow]跳过合并（--skip-merge 或只跑单块）[/yellow]")
        sys.exit(0)

    # ── 3. 合并 ──
    console.print("\n[bold]步骤 3/3  合并输出[/bold]")
    final_auto_dir.mkdir(parents=True, exist_ok=True)

    json_path = merge_content_lists(chunks, chunk_out_dir, final_auto_dir, basename)
    md_path = merge_markdown(chunks, chunk_out_dir, final_auto_dir, basename)
    img_count = merge_images(chunks, chunk_out_dir, final_auto_dir)

    console.print(f"\n[green bold]✓ 全部完成[/green bold]")
    console.print(f"  Markdown      : {md_path}")
    console.print(f"  content_list  : {json_path}")
    console.print(f"  图片          : {img_count} 张 → {final_auto_dir / 'images'}")
    console.print(
        f"\n[bold]下一步[/bold]：\n"
        f"  .venv/bin/python scripts/02_extract_clauses.py \\\n"
        f"    {json_path} \\\n"
        f"    --output data/structured/{basename}_clauses.json"
    )


if __name__ == "__main__":
    main()
