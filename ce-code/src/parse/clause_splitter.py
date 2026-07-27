"""
条款分块（阶段 1.3）

输入：MinerU hybrid-auto 产出的 .md 文件 + 规范元数据
输出：条款记录列表（DATA_SPEC §1 格式），写入 data/interim/clauses.jsonl

关键规律（五本结构规范实测）：
  - `## X 章名`（不含页码数字后缀）→ 章标题
  - `## X.X 节名` / `## X.X.X 子节名` → 节/子节标题
  - 正文中 `X.X.X 条文...`（无 ## 前缀）→ 条款起始行
  - 表格：HTML <table>...</table>（hybrid-auto 产出）
  - 公式：LaTeX $$...$$ 块
  - 强制性条文：从前言公告段提取条款号清单
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# ── 正则 ──────────────────────────────────────────────────────────────────

# 章/节标题：`## X 名称` 或 `## X.X 名称`，结尾不是纯数字（排除目录行）
_RE_HEADING = re.compile(
    r'^(#{1,3})\s+'           # ## / ###
    r'((?:\d+\.)*\d+)'        # 编号部分 1 / 1.2 / 1.2.3
    r'(\s*\S.*?)$'            # 名称（编号与名称间的空格可缺失：实测 `## 3基本规定`）
)
_RE_TOC_SUFFIX = re.compile(r'\s+\d{1,4}\s*$')   # 目录页码后缀

# 条款号：行首 X.X.X 或 X.X.X.X（不含 ## 前缀）
# 两段式 X.X 是节标题，由 _RE_HEADING 处理，不当条款
_RE_CLAUSE_START = re.compile(
    r'^(\d+\.\d+\.\d+(?:\.\d+)?)\s+(\S)'
)

# 表格 caption：`表 X.X.X-X 名称` 或 `续表 X.X.X-X`
_RE_TABLE_CAPTION = re.compile(r'^(?:续\s*)?表\s+\S')

# 公式：$$ 块
_RE_FORMULA_START = re.compile(r'^\$\$')

# 强制性条文：从公告段抽取（"第X.X.X、Y.Y.Y条为强制性条文"）
_RE_MANDATORY_LIST = re.compile(
    r'第\s*([\d\.、，,\s]+)\s*条(?:款)?[^\n]*为强制性条文'
)
_RE_CLAUSE_NO_FRAG = re.compile(r'\d+\.\d+(?:\.\d+)*')

# 标准号从目录名提取：GB50010-2010 / JGJ3-2010
_RE_STD_CODE = re.compile(r'^((?:GB|JGJ|DBJ)\s*[\w/]*\d+-\d+)')

# 「条文说明」附录起始行：独占一行的「条文说明」（可带 # 前缀）。
# 目录里的「附：条文说明· 187」带页码与「附：」前缀，不会误匹配。
_RE_COMMENTARY_START = re.compile(r'^#{0,3}\s*条文说明\s*$')


# ── 数据结构 ─────────────────────────────────────────────────────────────

@dataclass
class _Clause:
    clause_no: str
    chapter_path: list[str]
    lines: list[str] = field(default_factory=list)


# ── 辅助函数 ──────────────────────────────────────────────────────────────

def _extract_mandatory(text: str) -> set[str]:
    """从前言/公告文本中抽取强制性条文编号集合。"""
    mandatory: set[str] = set()
    for m in _RE_MANDATORY_LIST.finditer(text):
        for no in _RE_CLAUSE_NO_FRAG.findall(m.group(1)):
            mandatory.add(no)
    return mandatory


def _is_real_heading(line: str, m: re.Match) -> bool:
    """排除目录行（结尾有页码数字）。"""
    return not _RE_TOC_SUFFIX.search(m.group(3))


def _find_commentary_start(lines: list[str]) -> int:
    """定位「条文说明」附录的起始行号。

    Args:
        lines: .md 全文按行切分的列表

    Returns:
        附录起始行的下标；未找到时返回 len(lines)（即不截断）
    """
    for i, line in enumerate(lines):
        if _RE_COMMENTARY_START.match(line.strip()):
            return i
    return len(lines)


def _is_structural_heading(no_str: str, cur_chapter: int) -> bool:
    """判定该编号标题是「真章节标题」还是「条内分项小标题」。

    正文里存在大量条文内部的分项标题（如第 6 章中的 `## 2 大偏心受拉构件`），
    其编号是条内序号而非章号，误当章节会把 chapter_path 的章覆盖掉。
    依据章号单调递增来区分。

    Args:
        no_str:      标题编号，如 "4" / "4.1" / "4.1.2"
        cur_chapter: 当前所处章号（尚未进入任何章时为 0）

    Returns:
        True 表示应更新 chapter_path，False 表示忽略该标题
    """
    head = int(no_str.split('.')[0])
    if '.' not in no_str:
        return head == cur_chapter + 1   # 单段编号：只认下一章，杜绝条内分项标题夺章
    return head == cur_chapter           # 多段编号：必须属于当前章
    # 注：多段编号不允许「顺延进入下一章」。放宽会被正文里跨章引用式的
    # 小标题提前顶掉章号（实测 GB50010 在第 3 章内即被顶到 4，
    # 导致真正的 `## 4 材 料` 反被拒），代价是章标题真缺失时该章会挂在前一章下。


def _update_path(path: list[str], no_str: str, title: str) -> None:
    """按标题编号的段数更新 chapter_path。

    不使用 markdown 的 # 级数：MinerU 对同层标题的 # 数并不一致
    （GB50010 实测「## 1 总则」与「# 2 术语和符号」同为章却级数不同），
    编号段数才是可靠的层级信号。

    Args:
        path:   待就地更新的层级路径（0=章，1=节，2=子节）
        no_str: 标题编号，如 "4" / "4.1" / "4.1.2"
        title:  完整标题文本，如 "4.1 混凝土"

    Returns:
        None（就地修改 path）
    """
    depth = min(no_str.count('.'), 2)   # 段数-1，最深到子节
    del path[depth:]                     # 截断当前深度及以下
    path.append(title)


def _extract_tables(body: str) -> list[dict]:
    """从条款正文中提取 HTML 表格及其 caption。

    MinerU hybrid-auto 的 caption 可能在 <table> 前或后，均需查找。
    """
    tables = []
    lines = body.split('\n')
    i = 0
    while i < len(lines):
        ln = lines[i]
        if '<table' in ln.lower():
            # 向前 5 行找 caption
            caption = ''
            for j in range(i - 1, max(i - 6, -1), -1):
                stripped = lines[j].strip()
                if _RE_TABLE_CAPTION.match(stripped):
                    caption = stripped
                    break

            # 收集 table HTML（可能跨多行）
            html_lines = []
            while i < len(lines):
                html_lines.append(lines[i])
                if '</table>' in lines[i].lower():
                    i += 1
                    break
                i += 1

            # 若向前未找到 caption，向后 5 行再找
            if not caption:
                for j in range(i, min(i + 5, len(lines))):
                    stripped = lines[j].strip()
                    if _RE_TABLE_CAPTION.match(stripped):
                        caption = stripped
                        break

            tables.append({'caption': caption, 'html': '\n'.join(html_lines)})
        else:
            i += 1
    return tables


def _extract_formulas(body: str) -> list[str]:
    """抽取所有 $$...$$ LaTeX 公式块。"""
    return re.findall(r'\$\$.*?\$\$', body, re.DOTALL)


def _extract_refs(body: str, std_code: str) -> list[str]:
    """抽取条文内的交叉引用（同规范内部引用 + 跨规范引用）。"""
    from src.eval.clause import extract
    refs = extract(body, default_std=std_code)
    return list({r.clause_id for r in refs})


def _finalize(c: _Clause, std_code: str, std_name: str,
              mandatory: set[str]) -> dict:
    body = '\n'.join(c.lines).strip()
    return {
        'clause_id':     f'{std_code}_{c.clause_no}',
        'standard_code': std_code,
        'standard_name': std_name,
        'clause_no':     c.clause_no,
        'chapter_path':  list(c.chapter_path),
        'text':          body,
        'tables':        _extract_tables(body),
        'formulas':      _extract_formulas(body),
        'refs':          _extract_refs(body, std_code),
        'is_mandatory':  c.clause_no in mandatory,
        'char_len':      len(body),
    }


# ── 主解析器 ──────────────────────────────────────────────────────────────

def split_clauses(md_path: Path, std_code: str, std_name: str) -> list[dict]:
    """
    解析单本规范的 MinerU .md 文件，返回条款记录列表。

    Args:
        md_path:   .md 文件路径
        std_code:  标准编号，如 "GB50010-2010"
        std_name:  规范名称，如 "混凝土结构设计规范"
    """
    text = md_path.read_text(encoding='utf-8')
    lines = text.split('\n')

    # 1. 从前言提取强制性条文（须在截断前做：公告段在文件最前面）
    # 取正文开始前约 2000 字作为前言区
    preamble_end = min(len(text), 8000)
    mandatory = _extract_mandatory(text[:preamble_end])

    # 1.5 截断「条文说明」附录。
    # 规范 PDF 的结构是「正文在前、条文说明在后」，两段使用**完全相同的条款号**。
    # 不截断会让后出现的条文说明覆盖正文（见下方去重逻辑），
    # 使整个条文库退化为解释性文字、丢失全部限值表格。
    lines = lines[:_find_commentary_start(lines)]

    # 2. 定位正文开始（第一个真实章节标题行）
    content_start = 0
    for i, line in enumerate(lines):
        m = _RE_HEADING.match(line)
        if m and _is_real_heading(line, m):
            # 向后看 10 行内是否出现条款号，确认是真实内容区
            for lookahead in lines[i:i + 15]:
                if _RE_CLAUSE_START.match(lookahead):
                    content_start = i
                    break
            if content_start:
                break

    # 3. 状态机：逐行解析章节 + 条款
    chapter_path: list[str] = []
    clauses: list[dict] = []
    current: _Clause | None = None
    cur_chapter = 0

    for line in lines[content_start:]:
        # ── 章节标题 ──────────────────────────────────────────
        m_h = _RE_HEADING.match(line)
        if m_h and _is_real_heading(line, m_h):
            no_str = m_h.group(2)
            if not _is_structural_heading(no_str, cur_chapter):
                # 条内分项小标题：并入当前条款正文，不动 chapter_path
                if current:
                    current.lines.append(line)
                continue
            cur_chapter = int(no_str.split('.')[0])
            name_str = m_h.group(3).strip()
            title = f'{no_str} {name_str}'
            _update_path(chapter_path, no_str, title)
            # 章节标题不追加进条款正文（只更新路径）
            continue

        # ── 条款起始 ──────────────────────────────────────────
        m_c = _RE_CLAUSE_START.match(line)
        if m_c:
            # 过滤：条款号第一段必须 ≥ 1（排除 0.x.x 前言附则格式）
            clause_no = m_c.group(1)
            if current:
                clauses.append(_finalize(current, std_code, std_name, mandatory))
            current = _Clause(
                clause_no=clause_no,
                chapter_path=list(chapter_path),
                lines=[line],
            )
            continue

        # ── 条款正文续行 ──────────────────────────────────────
        if current:
            current.lines.append(line)

    if current:
        clauses.append(_finalize(current, std_code, std_name, mandatory))

    # 去重：同一 clause_no 保留最后出现的（2015版修订条文会重复）
    seen: dict[str, dict] = {}
    for c in clauses:
        seen[c['clause_no']] = c
    return list(seen.values())


# ── 批量入口 ─────────────────────────────────────────────────────────────

# 五本规范的元数据（从目录名映射）
STANDARDS: dict[str, dict] = {
    'GB50010-2010': {'name': '混凝土结构设计规范',      'file_kw': 'GB50010'},
    'GB50011-2010': {'name': '建筑抗震设计规范',        'file_kw': 'GB50011'},
    'GB50007-2011': {'name': '建筑地基基础设计规范',    'file_kw': 'GB50007'},
    'GB50009-2012': {'name': '建筑结构荷载规范',        'file_kw': 'GB50009'},
    'JGJ3-2010':    {'name': '高层建筑混凝土结构技术规程', 'file_kw': 'JGJ3'},
}


def _find_md(parsed_root: Path, file_kw: str) -> Path | None:
    for d in parsed_root.iterdir():
        if not d.is_dir():
            continue
        if file_kw not in d.name:
            continue
        for md in (d / 'hybrid_auto').glob('*.md'):
            return md
    return None


def run_all(parsed_root: Path, out_path: Path) -> None:
    """解析全部五本规范，输出到 clauses.jsonl。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(out_path, 'w', encoding='utf-8') as f:
        for std_code, meta in STANDARDS.items():
            md = _find_md(parsed_root, meta['file_kw'])
            if not md:
                print(f'[WARN] 未找到 {std_code} 的 .md 文件', file=sys.stderr)
                continue
            print(f'解析 {std_code} ({md.name})...')
            clauses = split_clauses(md, std_code, meta['name'])
            for c in clauses:
                f.write(json.dumps(c, ensure_ascii=False) + '\n')
            print(f'  → {len(clauses)} 条')
            total += len(clauses)
    print(f'完成，共 {total} 条 → {out_path}')


if __name__ == '__main__':
    _root = Path(__file__).parent.parent.parent
    run_all(
        parsed_root=_root / 'data' / 'interim' / 'parsed',
        out_path=_root / 'data' / 'interim' / 'clauses.jsonl',
    )
