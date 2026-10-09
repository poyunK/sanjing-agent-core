"""合成资料真实验证：共享十二次上限，不自动重试；Key仍只由model读取。"""

import json
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sanjing import model, store
from sanjing.agent import run_turn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--followup-only", action="store_true", help="修改后仅验证固定追问工具")
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "runtime" / "verification"
    output.mkdir(parents=True, exist_ok=True)
    db = output / "live.sqlite3"
    report_path = output / "live_results.json"
    previous = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    calls = previous.get("actual_api_requests", 0)

    def bounded_client(messages, tools):
        nonlocal calls
        if calls >= 12:
            raise model.ModelError("LIVE_BUDGET_LIMIT")
        calls += 1
        return model.ask_llm(messages, tools)

    session = store.ensure_session(db)
    cases = [
        "这是明确标注的合成案例。请读取本地演示测评并检索用户研究与产品经理助理，分别调用工具计算，比较两个探索方向。只用演示资料和规则，建议要正式提交并能查依据。",
        "继续这个合成案例：我每周可投入十五小时，没有实习经历。请记录我的自述条件，参考上轮建议（通过工具查账），说明目前还缺什么条件。需要补充信息请用ask_user询问，不编造技能或真实岗位。",
    ]
    if args.followup_only:
        cases = ["这是合成的对话追问测试。请立即调用ask_user询问skills和experience，不做职业推荐，不执行其他工具。"]
    reports = previous.get("cases", [])
    for question in cases:
        result = run_turn(session, question, db_path=db, client=bounded_client,
                          on_progress=lambda event: print(f"[{event['elapsed_seconds']}s] {event['stage']}", flush=True))
        snap = store.snapshot(db, session, result["turn_id"])
        reports.append({"status": result["status"], "error_code": result["error_code"],
                        "usage": result["usage"], "ledger_path": result["ledger_path"],
                        "tools": [{"tool": e["tool_name"], "status": e["status"], "error_code": e["error_code"]}
                                  for e in snap["events"]], "recommendation_count": len(snap["recommendations"]),
                        "condition_count": sum(item["kind"] in {"user_condition", "condition"} for item in snap["evidence"])})
        if calls >= 12 or result["error_code"] in {"HTTP_401", "KEY_UNAVAILABLE", "KEY_INVALID", "NETWORK_ERROR"}:
            break
    report = {"date": "2026-10-08", "actual_api_requests": calls, "automatic_retries": 0,
              "max_output_tokens_per_request": 1500, "cases": reports,
              "scope": "只验证工程协议和依据记录，不验证职业效度；超时请求的服务商实际费用可能无法获知。"}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
