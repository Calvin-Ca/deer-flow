"""调用远端 MinerU HTTP 服务批量解析规范 PDF（任务 1.2：PDF → 结构化中间件）。

对 `data/raw/` 下的每份规范 PDF 调用 MinerU 的同步解析接口 `/file_parse`，
把返回的 zip 结果解包到 `data/interim/parsed/<文件名>/auto/`，产出 MinerU 原生
四件套（`.md` / `_content_list.json` / `_content_list_v2.json` / `_middle.json`），
供下游 1.3 条款分块、1.4 表格抽取、1.5 refs 抽取消费。

- 禁用 PyPDF2/pdfplumber（见 CLAUDE.md §2、铁律 6：原始 PDF 只读）。
- backend 默认 `pipeline`（通用、多语、无幻觉；本服务上 hybrid 引擎初始化失败）。
- 仅用 Python 3.10 标准库，无三方依赖，Mac / 服务器均可直接跑。

功能：遍历输入目录的 PDF，逐份提交 MinerU 解析并解包结果，失败留痕、可断点续跑。
参数（命令行，见 build_arg_parser）：--service / --input / --output / --backend /
    --lang / --force / --timeout。
返回：进程退出码 0 表示全部成功，非 0 表示存在失败文件（详见 stdout 汇总与 failed/ 目录）。
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import sys
import time
import uuid
import zipfile
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


def build_arg_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。

    功能：声明脚本可调参数及默认值（路径相对仓库 ce-code 根解析）。
    参数：无。
    返回：配置完成的 argparse.ArgumentParser 实例。
    """
    p = argparse.ArgumentParser(description="批量调用 MinerU 服务解析规范 PDF")
    p.add_argument("--service", default="http://172.19.2.2:8000",
                   help="MinerU 服务根地址（默认内网 172.19.2.2:8000）")
    p.add_argument("--input", default="data/raw",
                   help="输入目录，扫描其下 *.pdf（默认 data/raw）")
    p.add_argument("--output", default="data/interim/parsed",
                   help="输出目录，解包结果落于此（默认 data/interim/parsed）")
    p.add_argument("--backend", default="hybrid-auto-engine",
                   help="MinerU backend（默认 hybrid-auto-engine：表格/版面精度更高；"
                        "备选 pipeline，无 VLM、无幻觉但复杂表格残缺）")
    p.add_argument("--lang", default="ch", help="OCR 语言（默认 ch 中英繁）")
    p.add_argument("--force", action="store_true",
                   help="已解析过的 PDF 也重新解析（默认跳过已完成项）")
    p.add_argument("--timeout", type=int, default=1800,
                   help="单份 PDF 同步解析超时秒数（默认 1800）")
    return p


def encode_multipart(fields: dict[str, str], pdf_path: Path) -> tuple[bytes, str]:
    """把普通字段与单个 PDF 文件编码为 multipart/form-data 请求体。

    功能：手工拼装 multipart body，避免引入 requests 等三方依赖。
    参数：fields 为普通表单字段名→值；pdf_path 为待上传的 PDF 路径。
    返回：(请求体字节流, Content-Type 头值含 boundary) 二元组。
    """
    boundary = f"----mineru{uuid.uuid4().hex}"
    buf = io.BytesIO()

    def write_field(name: str, value: str) -> None:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(value.encode("utf-8"))
        buf.write(b"\r\n")

    for name, value in fields.items():
        write_field(name, value)

    ctype = mimetypes.guess_type(pdf_path.name)[0] or "application/pdf"
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(
        f'Content-Disposition: form-data; name="files"; filename="{pdf_path.name}"\r\n'
        .encode("utf-8")
    )
    buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
    buf.write(pdf_path.read_bytes())
    buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())

    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def parse_one(service: str, pdf_path: Path, backend: str, lang: str,
              timeout: int) -> tuple[bool, bytes, str]:
    """对单份 PDF 调用 MinerU 同步解析接口。

    功能：POST /file_parse，请求返回 zip（含 md/content_list/middle_json，不含位图）。
    参数：service 服务根地址；pdf_path PDF 路径；backend 解析后端；lang OCR 语言；
        timeout 超时秒数。
    返回：(是否成功, 响应体字节, 响应 Content-Type) 三元组；失败时响应体为错误 JSON/文本。
    """
    fields = {
        "backend": backend,
        "lang_list": lang,
        "return_md": "true",
        "return_content_list": "true",
        "return_middle_json": "true",
        "return_images": "false",
        "response_format_zip": "true",
    }
    body, ctype = encode_multipart(fields, pdf_path)
    req = urlrequest.Request(
        f"{service.rstrip('/')}/file_parse", data=body, method="POST",
        headers={"Content-Type": ctype, "Content-Length": str(len(body))},
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return True, resp.read(), resp.headers.get("Content-Type", "")
    except HTTPError as e:
        return False, e.read(), e.headers.get("Content-Type", "") if e.headers else ""
    except URLError as e:
        return False, str(e.reason).encode(), "text/plain"


def unpack_zip(zip_bytes: bytes, output_dir: Path) -> list[str]:
    """把 MinerU 返回的 zip 解包到输出目录。

    功能：将 zip 内 `<stem>/auto/*.json|*.md` 直接落盘到 output_dir 下。
    参数：zip_bytes 为 zip 二进制；output_dir 为解包根目录。
    返回：写出的文件相对路径列表。
    """
    written: list[str] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            target = output_dir / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(member))
            written.append(member)
    return written


def already_done(output_dir: Path, stem: str) -> bool:
    """判断某份 PDF 是否已解析完成（用于断点续跑）。

    功能：检查输出目录下是否已存在该 PDF 的 markdown 产物（子目录名随 backend
        变化：pipeline 为 auto/、hybrid 为 hybrid_auto/，故按 glob 匹配任意子目录）。
    参数：output_dir 输出根目录；stem PDF 去扩展名后的文件名。
    返回：已存在返回 True，否则 False。
    """
    return any((output_dir / stem).glob(f"*/{stem}.md"))


def main() -> int:
    """脚本入口：遍历输入目录逐份解析并汇总结果。

    功能：解析参数→扫描 PDF→逐份调用 MinerU→解包/失败留痕→打印汇总。
    参数：无（从命令行读取）。
    返回：全部成功 0，存在失败 1。
    """
    args = build_arg_parser().parse_args()
    repo_root = Path(__file__).resolve().parents[2]  # ce-code/
    input_dir = (repo_root / args.input).resolve()
    output_dir = (repo_root / args.output).resolve()
    failed_dir = (repo_root / "data" / "interim" / "failed").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(input_dir.glob("*.pdf"))
    if not pdfs:
        print(f"[!] {input_dir} 下没有 PDF", file=sys.stderr)
        return 1

    print(f"[*] MinerU={args.service} backend={args.backend} 待解析 {len(pdfs)} 份")
    ok, skipped, failed = 0, 0, 0
    for i, pdf in enumerate(pdfs, 1):
        stem = pdf.stem
        if not args.force and already_done(output_dir, stem):
            print(f"[{i}/{len(pdfs)}] 跳过（已解析）：{stem}")
            skipped += 1
            continue
        print(f"[{i}/{len(pdfs)}] 解析中：{stem}  ({pdf.stat().st_size/1e6:.1f} MB)")
        t0 = time.time()
        success, payload, ctype = parse_one(
            args.service, pdf, args.backend, args.lang, args.timeout)
        dt = time.time() - t0
        if success and "zip" in ctype.lower():
            files = unpack_zip(payload, output_dir)
            print(f"    ✅ {dt:.1f}s，写出 {len(files)} 文件")
            ok += 1
        else:
            err_path = failed_dir / f"{stem}.error.json"
            err_path.write_bytes(payload)
            print(f"    ❌ {dt:.1f}s，失败（ctype={ctype}），留痕 {err_path}",
                  file=sys.stderr)
            failed += 1

    print(f"\n[=] 汇总：成功 {ok} / 跳过 {skipped} / 失败 {failed} / 共 {len(pdfs)}")
    print(f"[=] 产物目录：{output_dir}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
