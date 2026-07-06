#!/usr/bin/env python3
"""HITL 组价无头冒烟——一条命令把单构件从 start 逐闸自动走到总造价。

用途（验收/回归）：验 :8101 完整组价图（选码→定额→缺价→工程量→费率→参数→总价）对某
  spec/构件能否走到 status=done + rollup.total 出数。逐闸自动决策（confirm/review 采纳推荐、
  input 按字段填合法默认），省去手动逐条 resume；每闸打印摘要，终态打印总造价与逐件综合单价。
参数（命令行）：--feature 构件描述；--spec 国标版本（2013/2024）；--region 地区；
  --base-url 任务服务地址（默认 localhost:8101）；--quantity 工程量默认值（缺 Q 闸时填）；
  --tax 税金率%；--mgmt 管理费率%；--profit 利润率%；--fee-base 取费基数。
返回（退出码）：0=走到 done 且 rollup.total 有数；1=blocked/异常/未出总价（细节打印到 stdout）。
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

# input 闸按 field key 兜底的标准值（无 default 且 required 时用）；缺价 value 填 0 只入明细、
# 不影响综合单价（后者走定额 quota_basis 人材机基价），故总造价仍算准。
_NUMBER_DEFAULTS = {
    "value": 0,          # 缺价录入（材料明细，不进综合单价基价）
    "management_fee_rate": None,  # 由 CLI 覆写
    "profit_rate": None,
    "risk_rate": 0,
    "tax_rate": None,
    "quantity": None,
}


def _post(base_url: str, path: str, body: dict) -> dict:
    """POST JSON 到任务服务并解析响应。

    参数：base_url 服务根；path 端点路径；body 请求体 dict。
    返回：解析后的响应 dict；HTTP 非 2xx / 连接失败抛异常上抛由 main 兜。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _decide(interrupt: dict, cli: argparse.Namespace) -> dict:
    """按闸类型/字段自动构造 resume 决策体。

    参数：interrupt 当前闸 payload（gate_type/node/fields/actions）；cli 命令行参数（费率/税率等覆写值）。
    返回：resume 决策 dict——confirm/review 闸回 {action:approve}，input 闸回 {字段:值} 按 fields 逐项填。
    """
    gate = interrupt.get("gate_type")
    if gate in ("confirm", "review"):
        return {"action": "approve"}
    if gate != "input":
        return {"action": "approve"}
    # input 闸：遍历 fields 填合法值
    overrides = {
        "management_fee_rate": cli.mgmt, "profit_rate": cli.profit, "risk_rate": 0,
        "tax_rate": cli.tax, "quantity": cli.quantity,
    }
    decision: dict = {}
    for f in interrupt.get("fields", []):
        key, typ = f.get("key"), f.get("type")
        if key in overrides and overrides[key] is not None:
            decision[key] = overrides[key]
        elif "default" in f:
            decision[key] = f["default"]
        elif typ == "enum":
            opts = f.get("options") or []
            decision[key] = cli.fee_base if cli.fee_base in opts else (opts[0] if opts else None)
        elif typ == "number":
            decision[key] = _NUMBER_DEFAULTS.get(key, 0) or 0
        else:  # text 等
            decision[key] = ""
    return decision


def main() -> int:
    """驱动一次完整 HITL 组价冒烟并汇报结果。

    参数：无（读命令行）。返回：进程退出码（0=done+total 有数，1=未通关）。
    """
    p = argparse.ArgumentParser(description="HITL 组价无头冒烟（逐闸自动走到总造价）")
    p.add_argument("--feature", default="C30现浇混凝土矩形柱")
    p.add_argument("--spec", default="2013")
    p.add_argument("--region", default="深圳")
    p.add_argument("--base-url", default="http://localhost:8101")
    p.add_argument("--quantity", type=float, default=10.0, help="缺 Q 闸时填的工程量")
    p.add_argument("--tax", type=float, default=9.0, help="税金率(百分数)")
    p.add_argument("--mgmt", type=float, default=10.0, help="管理费率(百分数)")
    p.add_argument("--profit", type=float, default=5.0, help="利润率(百分数)")
    p.add_argument("--fee-base", default="labor_machine", help="取费基数 labor/labor_machine/lmm")
    p.add_argument("--max-steps", type=int, default=40, help="最多闸数（防死循环）")
    cli = p.parse_args()

    print(f"== HITL 冒烟：{cli.feature} · spec={cli.spec} · {cli.region} · {cli.base_url} ==\n")
    try:
        res = _post(cli.base_url, "/cost/session/start",
                    {"feature": cli.feature, "spec": cli.spec, "region": cli.region})
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"!! start 失败：{exc}")
        return 1

    tid = res.get("task_id")
    print(f"task_id={tid}\n")
    for step in range(cli.max_steps):
        status = res.get("status")
        itr = res.get("interrupt") or {}
        if status != "awaiting_input" or not itr:
            break
        decision = _decide(itr, cli)
        print(f"[闸{step+1}] {itr.get('gate_type'):7} {str(itr.get('node')):16} "
              f"{itr.get('title', '')[:40]}\n         → resume {json.dumps(decision, ensure_ascii=False)}")
        try:
            res = _post(cli.base_url, f"/cost/session/{tid}/resume", {"decision": decision})
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"!! resume 失败：{exc}")
            return 1
    else:
        print("!! 达到 max-steps 仍未终态（疑死循环）")
        return 1

    # ── 终态汇报 ──
    status = res.get("status")
    print(f"\n== 终态 status={status} ==")
    for i, it in enumerate(res.get("items") or []):
        code = (it.get("code") or {}).get("value")
        up = it.get("unit_price") or {}
        print(f"  构件[{i}] code={code} 综合单价={up.get('unit_price', up.get('status'))} "
              f"综合合价={up.get('total_price')}")
    rollup = res.get("rollup") or {}
    total = rollup.get("total") or rollup.get("grand_total")
    print(f"  rollup.total = {total}")
    if status == "done" and total not in (None, 0):
        print("\n✅ 走到 done 且总造价有数——链路通关")
        return 0
    print("\n❌ 未通关（status 非 done 或 total 空）；完整终态：")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:3000])
    return 1


if __name__ == "__main__":
    sys.exit(main())
