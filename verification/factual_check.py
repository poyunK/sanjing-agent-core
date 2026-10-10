"""回放固定合成案例：自动检查结构与引用，原文能否支持推论留给人工核对。"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from sanjing import store, tools
from sanjing.offline import _reply
from sanjing.config import PROJECT_DIR


EXPECTED_ERRORS = {
    "empty": "data_empty",
    "business_error": "data_business_error",
    "wrong_type": "data_wrong_type",
    "bad_items": "data_structure_error",
}


def fixture_payload(case: dict[str, Any]) -> dict[str, Any]:
    """从案例文件中的具体合成原文构造返回字典，不查询真实服务。"""
    category = case["category"]
    payload: dict[str, Any] = {
        "code": 0,
        "msg": "success",
        "trace_id": "SYNTH-" + case["id"],
        "dataset_type": case.get("provider_dataset_type", category),
        "items": case.get("source_items", []),
    }
    variant = case.get("variant", "normal")
    if variant == "empty":
        payload["items"] = []
    elif variant == "business_error":
        payload["code"] = 403
        payload["msg"] = "synthetic business error"
    elif variant == "wrong_type":
        payload["dataset_type"] = case.get("provider_dataset_type", "academic_search")
    elif variant == "unknown_type":
        payload["dataset_type"] = case.get("provider_dataset_type", "unmapped_provider_type")
    elif variant == "bad_items":
        payload["items"] = case.get("source_items", ["not a record object"])
    return payload


def _tool_call(ctx: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """与真实 Agent 一样经过 execute_tool，保留参数校验与自动事件记录。"""
    reply = _reply([(name, arguments)])
    calls = reply.get("message", {}).get("tool_calls", [])
    if not calls:
        return {"ok": False, "data": None, "error": {"code": "fixture_call_missing"}, "evidence_ids": []}
    return tools.execute_tool(ctx, calls[0])


def _record_value(item: Any, field_path: str) -> tuple[bool, Any]:
    """Read a source_items value using the /record/... JSON Pointer used by get_record."""
    if not field_path.startswith("/record/") or not isinstance(item, dict):
        return False, None
    tokens = [token.replace("~1", "/").replace("~0", "~")
              for token in field_path.split("/")[2:]]
    value: Any = item
    for token in tokens:
        try:
            if isinstance(value, dict):
                value = value[token]
            elif isinstance(value, list) and token.isdigit():
                value = value[int(token)]
            else:
                return False, None
        except (KeyError, IndexError, TypeError):
            return False, None
    return True, value


def _field_path(value: str) -> str:
    if value.startswith("/record/"):
        return value
    if value.startswith("/source/"):
        return value
    return "/record/" + value.lstrip("/").replace(".", "/")


def _run_decision_demo(ctx: dict[str, Any], evidence_id: str,
                       expected_status: Any) -> dict[str, Any]:
    """Exercise one structured citation path; this does not score human semantic review."""
    result = _tool_call(ctx, "submit_decision", {
        "title": "核对登记状态引用",
        "options": ["继续核实招聘主体", "暂缓判断"],
        "claims": [
            {
                "kind": "fact", "text": "该字段显示企业登记状态。",
                "citations": [{"evidence_id": evidence_id, "field_path": "/record/status"}],
            },
            {
                "kind": "inference", "text": "登记状态不能单独证明岗位质量，仍需核对招聘主体和职位描述。",
                "citations": [{"evidence_id": evidence_id, "field_path": "/record/status"}],
            },
        ],
        "unknowns": ["当前招聘主体和岗位状态尚未核验"],
        "next_actions": ["查验招聘页面与合同主体"],
    })
    recommendation = result.get("data", {}).get("recommendation", {}) if result.get("ok") else {}
    claims = recommendation.get("claims", []) if isinstance(recommendation, dict) else []
    fact = next((claim for claim in claims if isinstance(claim, dict) and claim.get("kind") == "fact"), {})
    resolved = fact.get("resolved", []) if isinstance(fact, dict) else []
    first_value = resolved[0].get("value") if resolved and isinstance(resolved[0], dict) else None
    passed = bool(
        result.get("ok") and recommendation.get("kind") == "career_decision"
        and first_value == expected_status
        and resolved and resolved[0].get("field_path") == "/record/status"
        and resolved[0].get("evidence_id") == evidence_id
    )
    return {
        "attempted": True,
        "passed": passed,
        "error_code": result.get("error", {}).get("code") if not result.get("ok") else None,
        "citation_field": "/record/status",
        "program_resolved_value": first_value,
        "scope": "只核对结构化提交、已读取字段引用与程序解析值；不评价原文是否支持建议。",
    }


def evaluate() -> dict[str, Any]:
    catalog_path = PROJECT_DIR / "verification" / "factual_cases.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    decision_demo: dict[str, Any] | None = None

    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "factual-cases.sqlite3"
        for case in catalog["cases"]:
            session_id = store.ensure_session(db)
            turn_id = store.start_turn(db, session_id)
            message_id = store.add_message(db, session_id, turn_id, "user", "合成案例：" + case["prompt"])
            payload = fixture_payload(case)
            context: dict[str, Any] = {
                "db_path": db,
                "session_id": session_id,
                "turn_id": turn_id,
                "user_message_id": message_id,
                "allowed_evidence_ids": set(),
                "pending_recommendations": [],
                "professional_reads": set(),
                "data_cache": {},
                "data_failures": [],
                "data_client": lambda query, payload=payload: payload,
                "deadline": time.monotonic() + 60,
            }

            started = time.monotonic()
            query_started = time.monotonic()
            query_result = _tool_call(context, "query_professional_data", {
                "category": case["category"], "query": case["prompt"],
            })
            query_seconds = time.monotonic() - query_started
            expected_error = EXPECTED_ERRORS.get(case.get("variant", "normal"))
            if expected_error:
                schema_passed = (
                    not query_result.get("ok")
                    and query_result.get("error", {}).get("code") == expected_error
                    and not query_result.get("evidence_ids")
                )
                query_error = query_result.get("error", {}).get("code")
                query_data: dict[str, Any] = {}
            else:
                query_data = query_result.get("data") if isinstance(query_result.get("data"), dict) else {}
                candidate_rows = query_data.get("items") if isinstance(query_data.get("items"), list) else []
                source_items = case.get("source_items", [])
                schema_passed = bool(
                    query_result.get("ok")
                    and isinstance(query_data.get("query_evidence_id"), str)
                    and len(candidate_rows) == len(source_items)
                    and all(isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
                            for item in candidate_rows)
                )
                if case.get("variant") == "unknown_type":
                    schema_passed = schema_passed and query_data.get("route_verified") is False
                query_error = None if query_result.get("ok") else query_result.get("error", {}).get("code")

            field_checks: list[dict[str, Any]] = []
            snapshot_read_passed: bool | None = None
            field_read_seconds = 0.0
            decision_seconds = 0.0
            decision_mechanism: dict[str, Any] | None = None
            if query_result.get("ok"):
                query_evidence_id = query_data.get("query_evidence_id")
                snapshot_started = time.monotonic()
                snapshot_read = _tool_call(context, "get_record", {
                    "record_type": "evidence", "record_id": query_evidence_id,
                })
                field_read_seconds += time.monotonic() - snapshot_started
                snapshot_summary = snapshot_read.get("data", {}).get("record", {}) if snapshot_read.get("ok") else {}
                snapshot_hash = snapshot_summary.get("response_sha256") if isinstance(snapshot_summary, dict) else None
                snapshot_read_passed = bool(
                    snapshot_read.get("ok")
                    and isinstance(snapshot_summary, dict)
                    and snapshot_summary.get("kind") == "professional_query"
                    and snapshot_summary.get("category") == case["category"]
                    and snapshot_summary.get("id") == query_evidence_id
                    and isinstance(snapshot_hash, str)
                    and len(snapshot_hash) == 64
                    and all(character in "0123456789abcdef" for character in snapshot_hash)
                )

                source_items = case.get("source_items", [])
                candidate_rows = query_data.get("items", [])
                for candidate_index, candidate in enumerate(candidate_rows):
                    if not isinstance(candidate, dict) or candidate_index >= len(source_items):
                        continue
                    evidence_id = candidate.get("evidence_id")
                    source_item = source_items[candidate_index]
                    for requested_path in case.get("read_fields", []):
                        if not isinstance(requested_path, str):
                            continue
                        field_path = _field_path(requested_path)
                        read_started = time.monotonic()
                        read_result = _tool_call(context, "get_record", {
                            "record_type": "evidence", "record_id": evidence_id,
                            "field_path": field_path,
                        })
                        field_read_seconds += time.monotonic() - read_started
                        read_record = read_result.get("data", {}).get("record", {}) if read_result.get("ok") else {}
                        actual = read_record.get("value") if isinstance(read_record, dict) else None
                        expected_exists, expected = _record_value(source_item, field_path)
                        read_error = read_result.get("error", {}).get("code") if not read_result.get("ok") else None
                        if expected_exists:
                            passed = bool(read_result.get("ok") and actual == expected)
                        else:
                            passed = bool((not read_result.get("ok") and read_error == "field_not_found")
                                          or (read_result.get("ok") and actual is None))
                        field_checks.append({
                            "candidate_index": candidate_index,
                            "evidence_id": evidence_id,
                            "field_path": field_path,
                            "expected_value": expected,
                            "get_record_value": actual,
                            "get_record_error_code": read_error,
                            "passed": passed,
                            "source_field_exists": expected_exists,
                            "missing_in_source": not expected_exists,
                        })

                if case["id"] == "company_exact" and candidate_rows:
                    first_id = candidate_rows[0].get("evidence_id")
                    status_exists, expected_status = _record_value(source_items[0], "/record/status")
                    decision_started = time.monotonic()
                    decision_mechanism = _run_decision_demo(
                        context, first_id, expected_status if status_exists else None
                    )
                    decision_seconds = time.monotonic() - decision_started
                    decision_demo = decision_mechanism

            source_copy_passed = (all(item["passed"] for item in field_checks)
                                  if field_checks else None)
            present_field_checks = [item for item in field_checks if item["source_field_exists"]]
            rows.append({
                "id": case["id"],
                "category": case["category"],
                "prompt": case["prompt"],
                "source_items": case.get("source_items", []),
                "proposed_inference": case.get("proposed_inference", ""),
                "human_check": case.get("human_check", ""),
                "human_check_status": "pending_human_review",
                "schema_or_absence_passed": bool(schema_passed),
                "query_error_code": query_error,
                "route_verified": query_data.get("route_verified") if query_result.get("ok") else None,
                "candidate_count": len(query_data.get("items", [])) if query_result.get("ok") else 0,
                "query_metadata_read_passed": snapshot_read_passed,
                "source_field_reads": field_checks,
                "source_field_checks_passed": source_copy_passed,
                "source_field_copy_passed": (all(item["passed"] for item in present_field_checks)
                                              if present_field_checks else None),
                "missing_source_fields": [
                    {"candidate_index": item["candidate_index"], "field_path": item["field_path"],
                     "get_record_error_code": item["get_record_error_code"],
                     "handled_as_missing": item["passed"]}
                    for item in field_checks if item["missing_in_source"]
                ],
                "citation_support_review": "pending_human_review",
                "factual_scope_review": "pending_human_review",
                "decision_citation_mechanism": decision_mechanism,
                "elapsed_seconds": round(time.monotonic() - started, 4),
                "query_seconds": round(query_seconds, 4),
                "get_record_seconds": round(field_read_seconds, 4),
                "submit_decision_seconds": round(decision_seconds, 4),
            })

    successful_field_cases = [row for row in rows if row["source_field_reads"]]
    return {
        "cases": rows,
        "count": len(rows),
        "structure_passed": sum(row["schema_or_absence_passed"] for row in rows),
        "field_read_cases": len(successful_field_cases),
        "field_reads_passed": sum(
            check["passed"] for row in rows for check in row["source_field_reads"]
        ),
        "field_reads_total": sum(len(row["source_field_reads"]) for row in rows),
        "citation_mechanism_demo": decision_demo,
        "human_review": "pending_human_review",
        "notice": catalog["notice"],
    }


def main() -> None:
    report = evaluate()
    directory = PROJECT_DIR / "runtime" / "professional_verification"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "factual_report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "count": report["count"],
        "structure_passed": report["structure_passed"],
        "field_reads": f'{report["field_reads_passed"]}/{report["field_reads_total"]}',
        "citation_mechanism_demo": bool(report["citation_mechanism_demo"]
                                         and report["citation_mechanism_demo"]["passed"]),
        "human_review": report["human_review"],
        "report": str(target),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
