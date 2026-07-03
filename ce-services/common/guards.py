"""校验闸共享契约（③ 层，AGENT_DEV §9.1）—— 跨能力（norm / cost）统一的 ``GuardReport``。

> 对应 §9.2 ③「cost 侧对齐同一 GuardReport 契约」、§9.5 T-A3；AGENT_PRD C-01 溯源 / C-02 口径纯净 /
> C-03 空结果不幻觉。

**为什么抽到 common**：校验闸最初生于 norm 侧（``norm/guards.py``），但 C-01/02/03 是**全能力红线**，
cost 侧也要产出同形 ``meta.guard`` 供审计/前端渲染。把「报告契约」（数据结构 + 红线编号 + 裁决枚举）
下沉到 common，norm 与 cost 各自实现自己的审计逻辑（``norm.guards.audit_answer`` /
``cost.guards.audit_cost_result``）但产出**同一个 ``GuardReport``** —— 这就是「对齐同契约」。

契约字段是能力无关的：``verdict`` / ``tier`` / ``violations`` / ``provenance_complete`` /
``caliber_pure``。``cited_total`` / ``cited_dropped`` 是 norm 引用计数用，cost 侧留 0（同结构、不同填法）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 红线编号（与 AGENT_PRD C-01/02/03 对齐）
GUARD_C01 = "C-01"  # 全量溯源
GUARD_C02 = "C-02"  # 地域/版本口径纯净
GUARD_C03 = "C-03"  # 空结果不幻觉

VERDICT_PASS = "pass"
VERDICT_REJECT = "reject"


@dataclass
class GuardReport:
    """校验闸结论（结构化、可审计；进响应 ``meta.guard``）。

    字段：
        verdict —— ``pass`` / ``reject``（reject = 降级为无依据/转人工，不当权威结论呈现）。
        violations —— 命中的红线条目 ``[{code, severity, detail}]``（severity: error 剔除/严重 / warn 标记）。
        provenance_complete —— C-01：溯源是否完整（norm=引用全带条款号；cost=定额/信息价均带来源）。
        caliber_pure —— C-02：口径是否纯净（norm=无他部/跨版条文；cost=取数无跨版串库）。
        cited_total —— 进闸前引用/计数（norm 用；cost 留 0）。
        cited_dropped —— 被 C-02 剔除数（norm 用；cost 留 0）。
        tier —— 来源层级（§8.3/FR-K07）：``local`` 本地权威命中（Tier-1）/ ``web`` 联网兜底
          （Tier-2，答案带硬降级标注 + URL + 访问日期）/ ``none`` 无可信命中（Tier-3，拒答/转人工）。
    """

    verdict: str
    violations: list[dict] = field(default_factory=list)
    provenance_complete: bool = True
    caliber_pure: bool = True
    cited_total: int = 0
    cited_dropped: int = 0
    tier: str = "local"

    def add(self, code: str, severity: str, detail: str) -> None:
        """追加一条红线命中。参数：code 红线编号；severity error/warn；detail 说明。返回：None。"""
        self.violations.append({"code": code, "severity": severity, "detail": detail})

    def as_meta(self) -> dict:
        """序列化为响应 meta 片段（进 ``meta.guard``）。参数：无。返回：契约 dict。"""
        return {
            "verdict": self.verdict,
            "tier": self.tier,
            "provenance_complete": self.provenance_complete,
            "caliber_pure": self.caliber_pure,
            "cited_total": self.cited_total,
            "cited_dropped": self.cited_dropped,
            "violations": self.violations,
        }
