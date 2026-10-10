"""有明确预算的真实接线验证。仅调用 DeepSeek 与 DataPro；只使用合成用户案例。"""

import json
from pathlib import Path
from datetime import datetime, timezone

from sanjing import datapro, model
from sanjing.agent import run_turn
from sanjing.config import PROJECT_DIR


def main():
    directory = PROJECT_DIR / "runtime" / "professional_verification"
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / "live.sqlite3"
    counts = {"model_requests": 0, "data_requests": 0}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    provider_results = []

    def llm(messages, definitions):
        if counts["model_requests"] >= 12:
            raise model.ModelError("LIVE_MODEL_BUDGET")
        counts["model_requests"] += 1
        result = model.ask_llm(messages, definitions)
        for key in usage:
            usage[key] += result.get("usage", {}).get(key, 0)
        return result

    def data(query):
        if counts["data_requests"] >= 24:
            raise datapro.DataproError("LIVE_DATA_BUDGET")
        counts["data_requests"] += 1
        row = {"attempt": counts["data_requests"]}
        try:
            result = datapro.query_data(query)
            row.update(status="returned", business_code=result.get("code"),
                       dataset_type=result.get("dataset_type"),
                       item_count=len(result.get("items", [])))
            return result
        except datapro.DataproError as error:
            row.update(status="failed_or_unknown", error_code=error.code)
            raise
        finally:
            provider_results.append(row)

    cases = [
        ("research", "这是合成测试，不含真实个人资料。请只查询一次科研学术资料：Big Five personality job performance meta-analysis。"
         "不使用演示测评，不需要追问。我只需要了解找到的论文标题和发表年份，再给出阅读原文、核查适用范围的建议。"
         "不要断言测评能决定职业。用 query_professional_data 的 research 类别，读取引用字段后 submit_decision；"
         "事实值由程序展示，自由文字不写数字。"),
        ("company", "这是合成职业探索测试：一个没有提供真实个人资料的学生，想了解北京字节跳动科技有限公司的企业背景。"
         "请分别查询该主体的工商和风险资料，无需再追问。把查到的主体信息与未核实事项列入账本，"
         "不能断言无风险或岗位适合这个学生。不要查询证券行情，不要读取演示测评。"
         "若类型不明确只能作为候选分析并保留限制；使用 submit_decision 提交定性的核查建议。"),
    ]
    results = []
    for name, question in cases:
        if counts["model_requests"] >= 12 or counts["data_requests"] >= 24:
            break
        print("case " + name, flush=True)
        result = run_turn(None, question, db_path=db_path, client=llm, data_client=data,
                          on_progress=lambda event: print(event["stage"], flush=True))
        results.append({"case": name, "status": result["status"], "error_code": result["error_code"],
                        "usage": result["usage"], "ledger_path": result["ledger_path"],
                        "session_id": result["session_id"], "turn_id": result["turn_id"]})
        report = {"started_at": datetime.now(timezone.utc).isoformat(), "counts": counts,
                  "usage": usage, "cases": results, "provider_results": provider_results,
                  "billing": "供应商未返回实际AFP扣费；超时尝试可能计费。不自动重试。",
                  "limits": {"model_requests": 12, "data_requests": 24, "max_output_tokens": 1500}}
        (directory / "live_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": name, "status": result["status"], "counts": counts}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
