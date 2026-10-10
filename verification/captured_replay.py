"""用已保存的真实返回离线重放业务接线。无新模型/专业数据请求。"""
import json
from pathlib import Path

from sanjing import store, tools
from sanjing.agent import render_recommendations
from sanjing.offline import _reply
from sanjing.viewer import write_snapshot
from sanjing.config import PROJECT_DIR


def main():
    directory = PROJECT_DIR / "runtime" / "professional_verification"
    report = json.loads((directory / "live_report.json").read_text(encoding="utf-8"))
    sources = [(case, directory / "live.sqlite3") for case in report["cases"]]
    macro_db = directory / "industry_probe.sqlite3"
    if macro_db.exists():
        with store.connect(macro_db) as connection:
            row = connection.execute("SELECT session_id,id FROM turns ORDER BY created_at LIMIT 1").fetchone()
        if row:
            sources.append(({"case": "industry", "session_id": row[0], "turn_id": row[1]}, macro_db))
    results = []
    for case, original_db in sources:
        source_snapshot = store.snapshot(original_db, case["session_id"], case["turn_id"])
        queries = [row for row in source_snapshot["evidence"] if row["kind"] == "professional_query"]
        db = directory / "replay.sqlite3"
        session = store.ensure_session(db)
        turn = store.start_turn(db, session)
        message = store.add_message(db, session, turn, "user", "离线读取已保存的真实返回；不做新查询、不用模型生成建议。")
        ctx = {"db_path": db, "session_id": session, "turn_id": turn,
               "user_message_id": message, "allowed_evidence_ids": set()}
        citations = []
        for query in queries:
            data = query["data"]
            ctx["data_client"] = lambda text, data=data: data["response"]
            call = _reply([("query_professional_data", {"category": data["category"], "query": data["query"]})])["message"]["tool_calls"][0]
            outcome = tools.execute_tool(ctx, call)
            if not outcome["ok"]:
                raise RuntimeError(outcome["error"]["code"])
            # 只修改本次回放新建记录的元数据，保留原响应的真正获取时间；旧库从不改写。
            for row in store.list_evidence(db, session, turn):
                value = row["data"]
                if row["kind"] in {"professional_query", "professional_record"} and value.get("query") == data["query"]:
                    value["replayed_at"] = store.now()
                    value["fetched_at"] = data["fetched_at"]
                    value["limitations"] = list(value.get("limitations", [])) + ["已保存真实响应的离线回放，不是新的外部查询"]
                    with store.connect(db) as connection:
                        connection.execute("UPDATE evidence SET data=? WHERE id=? AND session_id=?",
                                           (store.encode(value), row["id"], session))
            first = outcome["data"]["items"][0]
            citation = {"evidence_id": first["evidence_id"], "field_path": first["field_paths"][0]}
            read = tools.execute_tool(ctx, _reply([("get_record", {"record_type": "evidence",
                                      "record_id": citation["evidence_id"],
                                      "field_path": citation["field_path"]})])["message"]["tool_calls"][0])
            if not read["ok"]:
                raise RuntimeError(read["error"]["code"])
            citations.append(citation)
            # 该验证刻意不调用模型；只证明同一份业务返回能经过新的校验与提交接口。
        arguments = {"title": "真实返回的离线字段回放", "options": ["核对源字段"],
                     "claims": [{"kind": "fact", "text": "字段", "citations": citations}],
                     "unknowns": ["这是已保存响应的离线回放，不是一次新查询",
                                  "业务字段的适用性和个人职业判断需要另行核查"],
                     "next_actions": ["核对来源时间与原始字段；不把程序回放当作新的模型验收"]}
        outcome = tools.execute_tool(ctx, _reply([("submit_decision", arguments)])["message"]["tool_calls"][0])
        if not outcome["ok"]:
            raise RuntimeError(outcome["error"]["code"])
        records = store.list_recommendations(db, session, turn)
        answer = render_recommendations(records)
        store.finish_turn(db, session, turn, "completed", answer, usage={"requests": 0, "external_requests": 0})
        store.add_message(db, session, turn, "assistant", answer)
        path = write_snapshot(db, session, turn, directory / "replay_ledger")
        results.append({"case": case["case"], "status": "completed", "ledger_path": str(path),
                        "original_fetched_at": [query["data"]["fetched_at"] for query in queries],
                        "notice": "这里只验证已保存响应的结构接线与程序提交；无模型输出、无新专业查询。"})
    (directory / "replay_report.json").write_text(json.dumps({"cases": results, "new_network_requests": 0}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"replayed_cases": len(results), "new_network_requests": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
