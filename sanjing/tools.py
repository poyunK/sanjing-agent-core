"""三镜 Agent 的首批业务工具。

模型只能从下方固定注册表选择能力；本模块不提供 Shell、文件、网络、SQL
或任意 Python 执行接口。函数尽量保持直白，便于跟着调用链学习。
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from . import safety
from .privacy import redact


DATA_DIR = Path(__file__).resolve().parent / "data"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class ToolFailure(Exception):
    """工具预期内的失败，message 可以安全地交给模型继续处理。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _store():
    """延迟载入 SQLite 存储层，方便主循环和离线单元测试分别接入。"""
    from . import store
    return store


def _load_json(filename: str) -> Any:
    """只从随程序发布的固定 data 目录读取 JSON，不接收外部路径。"""
    path = DATA_DIR / filename
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ToolFailure("invalid_id", f"{label}编号格式无效。")
    return value


def _dict_data(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """兼容存储层返回已解码字典或 JSON 文本的两种实现。"""
    if record is None:
        return None
    data = record.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _session(ctx: dict[str, Any]) -> tuple[str, str, Path]:
    session_id = ctx.get("session_id")
    turn_id = ctx.get("turn_id")
    db_path = ctx.get("db_path")
    if not isinstance(session_id, str) or not session_id or not isinstance(turn_id, str) or not turn_id:
        raise ToolFailure("invalid_context", "会话或轮次信息无效。")
    if not isinstance(db_path, (str, Path)):
        raise ToolFailure("invalid_context", "数据库位置无效。")
    return session_id, turn_id, Path(db_path)


def _grant(ctx: dict[str, Any], evidence_id: str) -> None:
    allowed = ctx.get("allowed_evidence_ids")
    if not isinstance(allowed, set):
        allowed = set()
        ctx["allowed_evidence_ids"] = allowed
    allowed.add(evidence_id)


def _save_evidence(ctx: dict[str, Any], kind: str, data: dict[str, Any]) -> str:
    session_id, turn_id, db_path = _session(ctx)
    event_id = ctx.get("current_event_id")
    evidence_id = _store().add_evidence(
        db_path, session_id, turn_id, kind, data, event_id=event_id
    )
    _grant(ctx, evidence_id)
    return evidence_id


def _evidence(ctx: dict[str, Any], evidence_id: str) -> dict[str, Any]:
    session_id, _, db_path = _session(ctx)
    record = _store().get_evidence(db_path, session_id, evidence_id)
    if not record or record.get("session_id") != session_id:
        raise ToolFailure("evidence_not_found", "找不到这条会话内的依据记录。")
    data = _dict_data(record)
    if data is None:
        raise ToolFailure("invalid_evidence", "依据记录内容格式无效。")
    return {**record, "data": data}


def _dependency_closure(
    ctx: dict[str, Any], initial_ids: list[str], grant_discovered: bool = False
) -> list[str]:
    """沿依据记录中的依赖关系展开，且每一项都必须属于本会话。"""
    session_id, _, db_path = _session(ctx)
    store = _store()
    allowed = ctx.get("allowed_evidence_ids", set())
    result: list[str] = []
    visited: set[str] = set()
    stack = list(initial_ids)
    while stack:
        evidence_id = stack.pop()
        _require_id(evidence_id, "依据")
        if evidence_id in visited:
            continue
        visited.add(evidence_id)
        if len(visited) > 100:
            raise ToolFailure("dependency_limit", "依据关联数量超过限制。")
        if evidence_id not in allowed and not grant_discovered:
            raise ToolFailure("evidence_not_allowed", "本轮没有读取或授权使用其中一条依据。")
        record = store.get_evidence(db_path, session_id, evidence_id)
        if not record or record.get("session_id") != session_id:
            raise ToolFailure("evidence_not_found", "找不到这条会话内的依据记录。")
        if grant_discovered:
            _grant(ctx, evidence_id)
        data = _dict_data(record)
        if data is None:
            raise ToolFailure("invalid_evidence", "依据记录内容格式无效。")
        result.append(evidence_id)
        dependencies = data.get("dependency_evidence_ids", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise ToolFailure("invalid_evidence", "依据关联格式无效。")
        stack.extend(dependencies)
    return result


def _path_value(data: dict[str, Any], dotted_path: str) -> Any:
    value: Any = data
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _get_assessment(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    assessment = _load_json("assessment.json")
    data = {
        "assessment": assessment,
        "source": assessment["source"],
        "demo": True,
        "demo_label": "仅用于本地原型演示，不代表真实用户测评",
        "limitations": ["演示数据为合成样例，不能用于真实职业判断。"],
    }
    evidence_id = _save_evidence(ctx, "assessment", data)
    return {"data": data, "evidence_ids": [evidence_id]}


def _search_jobs(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    query = args["query"].strip().casefold()
    limit = args.get("limit", 5)
    words = [word for word in re.split(r"\s+", query) if word]
    jobs = _load_json("jobs.json")
    matched: list[dict[str, Any]] = []
    for job in jobs:
        haystack = " ".join(
            [job.get("title", ""), job.get("summary", ""), *job.get("keywords", [])]
        ).casefold()
        if any(word in haystack for word in words):
            matched.append(job)
        if len(matched) >= limit:
            break

    results: list[dict[str, Any]] = []
    evidence_ids: list[str] = []
    for job in matched:
        data = {
            "job": job,
            "source": job["source"],
            "retrieval_method": "本地关键词匹配（不是向量检索）",
            "demo": True,
        }
        evidence_id = _save_evidence(ctx, "job_profile", data)
        evidence_ids.append(evidence_id)
        results.append({"job": job, "evidence_id": evidence_id})
    return {
        "data": {
            "query": args["query"],
            "retrieval_method": "本地关键词匹配（不是向量检索）",
            "results": results,
            "notice": "岗位与资料均为演示样例，不是实时招聘信息。",
        },
        "evidence_ids": evidence_ids,
    }


def _get_record(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    session_id, _, db_path = _session(ctx)
    store = _store()
    record_type = args["record_type"]
    record_id = _require_id(args["record_id"], "记录")
    if record_type == "evidence":
        record = _evidence(ctx, record_id)
        # 先确认记录属于本会话，再授权并展开其底层依据。
        _grant(ctx, record_id)
        granted = _dependency_closure(ctx, [record_id], grant_discovered=True)
        for item in granted:
            _grant(ctx, item)
        return {"data": {"record_type": record_type, "record": record}, "evidence_ids": granted}

    recommendation = store.get_recommendation(db_path, session_id, record_id)
    if not recommendation:
        raise ToolFailure("recommendation_not_found", "找不到这条会话内的建议记录。")
    if recommendation.get("session_id", session_id) != session_id:
        raise ToolFailure("recommendation_not_found", "找不到这条会话内的建议记录。")
    data = _dict_data(recommendation)
    if data is None:
        raise ToolFailure("invalid_recommendation", "建议记录内容格式无效。")
    ids = data.get("evidence_ids", [])
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ToolFailure("invalid_recommendation", "建议引用格式无效。")
    # 读取建议后，允许本会话继续追查该建议及其全部底层依据。
    for evidence_id in ids:
        _grant(ctx, evidence_id)
    granted = _dependency_closure(ctx, ids, grant_discovered=True)
    return {
        "data": {"record_type": record_type, "record": recommendation},
        "evidence_ids": granted,
    }


def _match_job(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    assessment_evidence_id = _require_id(args["assessment_evidence_id"], "测评依据")
    job_evidence_id = _require_id(args["job_evidence_id"], "岗位依据")
    if assessment_evidence_id not in ctx.get("allowed_evidence_ids", set()):
        raise ToolFailure("evidence_not_allowed", "请先读取本轮测评，再计算匹配。")
    if job_evidence_id not in ctx.get("allowed_evidence_ids", set()):
        raise ToolFailure("evidence_not_allowed", "请先检索本轮岗位资料，再计算匹配。")

    assessment_record = _evidence(ctx, assessment_evidence_id)
    job_record = _evidence(ctx, job_evidence_id)
    if assessment_record.get("kind") != "assessment":
        raise ToolFailure("wrong_evidence_kind", "指定记录不是测评依据。")
    if job_record.get("kind") != "job_profile":
        raise ToolFailure("wrong_evidence_kind", "指定记录不是岗位依据。")
    assessment_data = assessment_record["data"].get("assessment", {})
    job_data = job_record["data"].get("job", {})
    if job_data.get("job_id") != args["job_id"]:
        raise ToolFailure("job_evidence_mismatch", "岗位编号与岗位依据不匹配。")

    rules = _load_json("prototype_rules.json")
    job_rules = rules.get("jobs", {}).get(args["job_id"])
    if not isinstance(job_rules, dict):
        raise ToolFailure("job_not_found", "本地演示规则中没有该岗位。")

    rule_evidence_id = _save_evidence(ctx, "prototype_rule", {
        "rule_version": rules["version"],
        "rule_label": rules["label"],
        "rule": job_rules,
        "source": rules["source"],
        "limitations": rules["limitations"],
        "demo": True,
        "dependency_evidence_ids": [],
    })

    contributions: list[dict[str, Any]] = []
    missing: list[str] = []
    score = 0.0
    total_weight = 0.0
    for criterion in job_rules["criteria"]:
        feature = criterion["feature"]
        weight = float(criterion["weight"])
        user_value = _path_value(assessment_data.get("scores", {}), feature)
        if user_value is None:
            missing.append(feature)
            contributions.append({
                "feature": feature,
                "label": criterion["label"],
                "weight": weight,
                "user_value": None,
                "fit_percent": None,
                "contribution_points": 0.0,
                "missing": True,
            })
            continue

        if criterion["kind"] == "minimum_score":
            target = float(criterion["target"])
            fit = min(max(float(user_value), 0.0) / target * 100.0, 100.0)
        elif criterion["kind"] == "preferred_value":
            fit = 100.0 if user_value == criterion["target"] else 0.0
        else:
            raise ToolFailure("invalid_rule", "演示规则包含未支持的计算类型。")
        points = weight * fit
        score += points
        total_weight += weight
        contributions.append({
            "feature": feature,
            "label": criterion["label"],
            "weight": weight,
            "user_value": user_value,
            "fit_percent": round(fit, 2),
            "contribution_points": round(points, 3),
            "missing": False,
        })

    # 缺失维度不抬高分数，满分始终按规则总权重 1.0 计算。
    if not math.isclose(sum(float(item["weight"]) for item in job_rules["criteria"]), 1.0, abs_tol=0.0001):
        raise ToolFailure("invalid_rule", "演示规则权重之和必须为 1。")
    dependencies = [assessment_evidence_id, job_evidence_id, rule_evidence_id]
    calculation = {
        "job_id": job_data["job_id"],
        "job_title": job_data["title"],
        "rule_version": rules["version"],
        "rule_label": rules["label"],
        "computed": {
            "score": round(score, 2),
            "contributions": contributions,
            "missing": missing,
            "formula": "各项匹配度 × 规则权重后求和；缺失项按 0 分计入。",
            "demo": True,
            "demo_label": "团队原型假设，未验证，不代表录用概率或科学效度",
        },
        "limitations": list(rules["limitations"]) + list(job_rules.get("limitations", [])),
        "dependency_evidence_ids": dependencies,
        "demo": True,
    }
    calculation_id = _save_evidence(ctx, "calculation", calculation)
    closure = _dependency_closure(ctx, dependencies + [calculation_id])
    return {
        "data": {"calculation_id": calculation_id, **calculation},
        "evidence_ids": closure,
    }


def _record_condition(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    session_id, turn_id, db_path = _session(ctx)
    message_id = _require_id(args["source_message_id"], "消息")
    if message_id != ctx.get("user_message_id"):
        raise ToolFailure("not_current_user_message", "条件整理只能引用本轮的用户消息。")
    quote = args["quote"]
    message = _store().get_message(db_path, session_id, message_id)
    if not message or message.get("session_id", session_id) != session_id or message.get("role") != "user":
        raise ToolFailure("user_message_not_found", "找不到这条会话内的用户原话。")
    original = message.get("content")
    if not isinstance(original, str) or quote not in original:
        raise ToolFailure("quote_mismatch", "引用内容必须逐字出现在本会话的用户原话中。")
    condition = {
        "field": args["field"],
        "quote": quote,
        "status": args["status"],
        "source_message_id": message_id,
        "source_message_turn_id": message.get("turn_id"),
        "protection": "用户自述与测评结果分开保存，不会覆盖测评分数。",
    }
    evidence_id = _save_evidence(ctx, "user_condition", condition)
    return {"data": {"condition": condition, "evidence_id": evidence_id}, "evidence_ids": [evidence_id]}


_CLARIFYING_QUESTIONS = {
    "skills": "你目前掌握哪些具体技能？可以举一个实际例子吗？",
    "experience": "你做过哪些课程项目、社团活动或实习？",
    "location": "你希望在哪个城市或地区学习、实习或工作？",
    "availability": "你每周大约能投入几天，什么时候可以开始？",
    "goal": "你现阶段最想优先解决什么：探索方向、寻找实习，还是准备求职？",
}


def _ask_user(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """从固定问题库挑选澄清问题，不接收模型自拟的问题正文。"""
    return {
        "data": {"questions": [_CLARIFYING_QUESTIONS[field] for field in args["fields"]]},
        "evidence_ids": [],
    }


def _submit_recommendation(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    session_id, turn_id, db_path = _session(ctx)
    calculation_id = _require_id(args["calculation_id"], "计算")
    calculation_record = _evidence(ctx, calculation_id)
    if calculation_record.get("kind") != "calculation":
        raise ToolFailure("not_a_calculation", "该依据不是岗位匹配计算。")
    if calculation_record.get("turn_id") != turn_id:
        raise ToolFailure("calculation_not_current", "建议必须引用本轮的匹配计算。")
    event_id = calculation_record.get("event_id")
    if not event_id:
        raise ToolFailure("calculation_not_completed", "匹配计算没有成功事件记录。")
    event = _store().get_event(db_path, session_id, event_id)
    if not event or event.get("status") != "success" or event.get("tool_name") != "match_job":
        raise ToolFailure("calculation_not_completed", "匹配计算尚未由成功的 match_job 工具完成。")

    calculation = calculation_record["data"]

    # 推荐说明只写定性理由；数字与百分数由程序的 computed 字段展示，
    # 防止模型复制错误分数或自行制造概率。依据原文中的数字仍保留在账本。
    free_text_fields = [args["title"], args["explanation"], args["next_action"], *args["limitations"]]
    if any(
        any(character.isdigit() or character in "%％" for character in text)
        or "百分之" in text
        for text in free_text_fields
    ):
        raise ToolFailure("numeric_claim_not_allowed", "自由文本不能包含数字或百分号；匹配数值由程序单独展示。")

    required_ids = _dependency_closure(ctx, [calculation_id])
    submitted_ids = list(dict.fromkeys(args["evidence_ids"]))
    for evidence_id in submitted_ids:
        _require_id(evidence_id, "依据")
        if evidence_id not in ctx.get("allowed_evidence_ids", set()):
            raise ToolFailure("evidence_not_allowed", "建议引用了本轮没有读取或授权的依据。")
        record = _evidence(ctx, evidence_id)
        if record.get("session_id") != session_id:
            raise ToolFailure("evidence_not_found", "建议引用不属于当前会话。")
    if not set(required_ids).issubset(submitted_ids):
        raise ToolFailure("missing_dependency", "建议必须列出本次计算所用的测评、岗位、规则及计算依据。")
    if calculation_id not in submitted_ids:
        raise ToolFailure("missing_calculation", "建议必须引用本轮的匹配计算依据。")

    computed = calculation.get("computed")
    if not isinstance(computed, dict):
        raise ToolFailure("invalid_calculation", "匹配计算没有可用的程序结果。")
    limitations = list(args["limitations"])
    for limitation in calculation.get("limitations", []):
        if isinstance(limitation, str) and limitation not in limitations:
            limitations.append(limitation)
    data = {
        "title": args["title"],
        "explanation": args["explanation"],
        "explanation_origin": "模型生成，需结合下方记录核查",
        "calculation_id": calculation_id,
        "evidence_ids": submitted_ids,
        "limitations": limitations,
        "next_action": args["next_action"],
        "rule_version": calculation["rule_version"],
        "computed": computed,
        "missing": computed.get("missing", []),
        "demo": True,
        "demo_label": computed.get("demo_label", "演示规则结果，未验证"),
        "job_id": calculation.get("job_id"),
        "turn_id": turn_id,
    }
    recommendation_id = _store().add_recommendation(db_path, session_id, turn_id, data)
    if isinstance(ctx.get("pending_recommendations"), list):
        ctx["pending_recommendations"].append(recommendation_id)
    return {
        "data": {"recommendation_id": recommendation_id, "recommendation": data},
        "evidence_ids": submitted_ids,
    }


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_ID_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 100, "pattern": "^[A-Za-z0-9_-]+$", "description": "由系统返回的记录编号"}
_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "get_assessment": {
        "description": "读取本地合成测评样例，并自动把输入和来源记入依据账本。",
        "parameters": _object_schema({}, []),
        "permission": "business_read",
        "handler": _get_assessment,
    },
    "search_jobs": {
        "description": "在本地演示岗位中做关键词匹配；这不是向量检索，也不是实时招聘搜索。",
        "parameters": _object_schema({
            "query": {"type": "string", "minLength": 1, "maxLength": 100},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        }, ["query"]),
        "permission": "business_read",
        "handler": _search_jobs,
    },
    "get_record": {
        "description": "按编号读取本会话的依据或历史建议，并授权追查它引用的依据。",
        "parameters": _object_schema({
            "record_type": {"type": "string", "enum": ["evidence", "recommendation"]},
            "record_id": _ID_SCHEMA,
        }, ["record_type", "record_id"]),
        "permission": "business_read",
        "handler": _get_record,
    },
    "match_job": {
        "description": "用明确标为未验证的原型规则计算演示岗位匹配，并保存输入、规则、贡献与依赖。人格偏好不等于技能。",
        "parameters": _object_schema({
            "job_id": {"type": "string", "enum": ["job_user_research", "job_product_assistant"]},
            "assessment_evidence_id": _ID_SCHEMA,
            "job_evidence_id": _ID_SCHEMA,
        }, ["job_id", "assessment_evidence_id", "job_evidence_id"]),
        "permission": "business_read",
        "handler": _match_job,
    },
    "record_condition": {
        "description": "从本会话用户原话中记录条件；引用必须逐字匹配，不会修改测评结果。",
        "parameters": _object_schema({
            "source_message_id": _ID_SCHEMA,
            "field": {"type": "string", "minLength": 1, "maxLength": 60},
            "quote": {"type": "string", "minLength": 1, "maxLength": 300},
            "status": {"type": "string", "enum": ["自述", "待确认"]},
        }, ["source_message_id", "field", "quote", "status"]),
        "permission": "business_write",
        "handler": _record_condition,
    },
    "submit_recommendation": {
        "description": "提交结构化建议。标题、解释、限制和下一步不能写数字或百分号，匹配数值由程序单独展示。只能引用本轮成功的岗位计算；程序会填入真实计算值、缺失项、规则版本和原型限制。",
        "parameters": _object_schema({
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "explanation": {"type": "string", "minLength": 1, "maxLength": 1500},
            "calculation_id": _ID_SCHEMA,
            "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 40, "items": _ID_SCHEMA},
            "limitations": {"type": "array", "maxItems": 15, "items": {"type": "string", "minLength": 1, "maxLength": 300}},
            "next_action": {"type": "string", "minLength": 1, "maxLength": 500},
        }, ["title", "explanation", "calculation_id", "evidence_ids", "limitations", "next_action"]),
        "permission": "business_write",
        "handler": _submit_recommendation,
    },
    "ask_user": {
        "description": "从固定问题库中挑选澄清问题；不能传入自拟问题文本。主循环应把结果作为待用户回答的问题。",
        "parameters": _object_schema({
            "fields": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "uniqueItems": True,
                "items": {"type": "string", "enum": ["skills", "experience", "location", "availability", "goal"]},
            },
        }, ["fields"]),
        "permission": "business_read",
        "handler": _ask_user,
    },
}


def tool_definitions() -> list[dict[str, Any]]:
    """返回给模型 API 的公开工具定义，不暴露 Python handler 或权限策略。"""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": json.loads(json.dumps(spec["parameters"], ensure_ascii=False)),
            },
        }
        for name, spec in _TOOL_SPECS.items()
    ]


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"code": code, "message": redact(message)}, "evidence_ids": []}


def execute_tool(ctx: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    """校验权限、建立事件、执行注册工具，并统一返回和记账。

    任何异常都转成不包含内部堆栈或原始异常文本的安全错误。
    """
    if not isinstance(ctx, dict):
        return _error("invalid_context", "当前会话上下文无效。")
    raw_call_id = call.get("id", "") if isinstance(call, dict) else ""
    raw_function = call.get("function", {}) if isinstance(call, dict) else {}
    raw_name = raw_function.get("name", "") if isinstance(raw_function, dict) else ""
    name = raw_name[:80] if isinstance(raw_name, str) else "unknown"
    call_id = raw_call_id[:120] if isinstance(raw_call_id, str) else "invalid-call"

    try:
        _, parsed_name, arguments = safety.parse_arguments(call)
        name = parsed_name
        spec = _TOOL_SPECS.get(name)
        if spec is None:
            raise safety.ValidationError("unknown_tool", "这个工具未注册，已拒绝执行。")
        safety.check_tool_permission(name, spec["permission"], ctx)
        safety.validate_schema(arguments, spec["parameters"])
    except safety.ValidationError as exc:
        return _record_rejected(ctx, name, call_id, exc.code, exc.message)

    try:
        session_id, turn_id, db_path = _session(ctx)
        event_id = _store().add_event(db_path, session_id, turn_id, name, call_id, "running")
    except Exception:
        return _error("event_log_unavailable", "无法建立工具执行记录，因此没有运行该工具。")

    ctx["current_event_id"] = event_id
    started = time.perf_counter()
    try:
        outcome = spec["handler"](ctx, arguments)
        if not isinstance(outcome, dict) or not isinstance(outcome.get("data"), dict):
            raise ToolFailure("invalid_tool_result", "工具返回结果格式无效。")
        evidence_ids = outcome.get("evidence_ids", [])
        if not isinstance(evidence_ids, list) or any(not isinstance(item, str) for item in evidence_ids):
            raise ToolFailure("invalid_tool_result", "工具返回的依据编号格式无效。")
        duration_ms = int((time.perf_counter() - started) * 1000)
        _store().update_event(db_path, event_id, "success", duration_ms=duration_ms)
        return {"ok": True, "data": redact(outcome["data"]), "error": None, "evidence_ids": evidence_ids}
    except ToolFailure as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            _store().update_event(db_path, event_id, "failed", error_code=exc.code, duration_ms=duration_ms)
        except Exception:
            pass
        return _error(exc.code, exc.message)
    except Exception:
        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            _store().update_event(db_path, event_id, "failed", error_code="internal_error", duration_ms=duration_ms)
        except Exception:
            pass
        return _error("internal_error", "工具执行失败，内部错误详情已隐藏。")
    finally:
        ctx.pop("current_event_id", None)


def _record_rejected(ctx: dict[str, Any], name: str, call_id: str, code: str, message: str) -> dict[str, Any]:
    """记录权限或参数拒绝；记录服务不可用时仍然不执行工具。"""
    try:
        session_id, turn_id, db_path = _session(ctx)
        _store().add_event(db_path, session_id, turn_id, name or "unknown", call_id or "invalid-call", "rejected", error_code=code)
    except Exception:
        return _error("event_log_unavailable", "调用被拒绝；拒绝记录暂时无法保存。")
    return _error(code, message)
