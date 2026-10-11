"""三镜 Agent 的首批业务工具。

模型只能从下方固定注册表选择能力；本模块不提供 Shell、任意文件或网络地址、SQL
或任意 Python 执行接口。函数尽量保持直白，便于跟着调用链学习。
"""

from __future__ import annotations

import json
import hashlib
import inspect
import math
import queue
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import safety
from .privacy import redact
from .evidence_quality import quality_flags
from .config import MAX_DATA_REQUESTS, DATA_TIMEOUT_SECONDS, MAX_SEARCH_REQUESTS


DATA_DIR = Path(__file__).resolve().parent / "data"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MAX_DATA_RESPONSE_BYTES = 1_048_576
_MAX_PROFESSIONAL_INDEX_CHARS = 1_500
_MAX_PROFESSIONAL_RECORD_CHARS = 6_000
_MAX_DATA_REQUESTS_PER_TURN = MAX_DATA_REQUESTS
_PROFESSIONAL_CATEGORIES = {
    "company_profile": "查询企业工商与画像资料：",
    "company_risk": "查询企业经营与风险资料：",
    "industry": "查询行业与宏观资料：",
    "research": "查询研究与学术资料：",
    "public_financials": "查询公开财务资料：",
}
_DATASET_TYPE_ALIASES = {
    "web_search": "web_search",
    "company_profile": "company_profile", "companyprofile": "company_profile",
    "enterprise_info": "company_profile", "enterpriseinfo": "company_profile",
    "企业画像": "company_profile", "企业信息": "company_profile", "工商信息": "company_profile",
    "company_risk": "company_risk", "companyrisk": "company_risk",
    "enterprise_risk": "company_risk", "enterpriserisk": "company_risk",
    "company_info": "company_profile",
    "企业风险": "company_risk", "风险信息": "company_risk", "经营风险": "company_risk",
    "industry": "industry", "industry_data": "industry", "宏观": "industry", "行业": "industry",
    "macro": "industry", "macroeconomic": "industry",
    "行业数据": "industry", "宏观数据": "industry",
    "research": "research", "paper": "research", "academic": "research",
    "academic_search": "research", "research_search": "research",
    "研究": "research", "学术": "research", "论文": "research",
    "public_financials": "public_financials", "publicfinancials": "public_financials",
    "financials": "public_financials", "public_finance": "public_financials",
    "financial_report": "public_financials", "公开财务": "public_financials", "财务数据": "public_financials",
}
_SECRET_FIELD_RE = re.compile(r"(?i)(?:api[_-]?key|access[_-]?key|token|secret|password|authorization|credential)")
_DATASET_TYPE_ALIASES_NORMALIZED = {
    re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", alias.casefold()): category
    for alias, category in _DATASET_TYPE_ALIASES.items()
}


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


def _save_evidence(
    ctx: dict[str, Any], kind: str, data: dict[str, Any], *, event_id: str | None = None,
    grant: bool = True,
) -> str:
    session_id, turn_id, db_path = _session(ctx)
    event_id = event_id or ctx.get("current_event_id")
    evidence_id = _store().add_evidence(
        db_path, session_id, turn_id, kind, data, event_id=event_id
    )
    if grant:
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


def _data_usage(ctx: dict[str, Any]) -> dict[str, int]:
    usage = ctx.get("data_usage")
    if not isinstance(usage, dict):
        usage = {}
        ctx["data_usage"] = usage
    for key in ("requests", "success", "failed", "timeouts", "cache_hits", "blocked"):
        if type(usage.get(key)) is not int or usage[key] < 0:
            usage[key] = 0
    return usage


def _record_data_failure(ctx: dict[str, Any], category: str, code: str) -> None:
    failures = ctx.get("data_failures")
    if not isinstance(failures, list):
        failures = []
        ctx["data_failures"] = failures
    safe_code = code if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code or "") else "data_error"
    item = {"category": category, "code": safe_code}
    if item not in failures and len(failures) < _MAX_DATA_REQUESTS_PER_TURN:
        failures.append(item)


def _data_client(ctx: dict[str, Any]):
    client = ctx.get("data_client")
    if callable(client):
        return client
    try:
        from .datapro import query_data
    except Exception:
        raise ToolFailure("data_provider_unavailable", "专业资料服务当前不可用。") from None
    return query_data


def _call_data_client(ctx: dict[str, Any], category: str, query: str) -> dict[str, Any]:
    """使用主循环传入的查询函数，最多尝试一次，返回字典供业务层校验。

    ctx 保存本轮截止时间与计数。等待超时后不会采用迟到结果，也不会重试。
    网络线程不写账本；只有当前执行工具的线程能保存通过检查的结果。
    """
    usage = _data_usage(ctx)
    deadline = ctx.get("deadline")
    remaining = float(DATA_TIMEOUT_SECONDS)
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        remaining = min(float(DATA_TIMEOUT_SECONDS), float(deadline) - time.monotonic())
    if remaining <= 0:
        usage["blocked"] += 1
        _record_data_failure(ctx, category, "deadline_exceeded")
        raise ToolFailure("data_timeout", "本轮专业资料查询时间已用尽。")
    if usage["requests"] >= ctx.get("data_request_limit", _MAX_DATA_REQUESTS_PER_TURN):
        usage["blocked"] += 1
        _record_data_failure(ctx, category, "request_budget_exceeded")
        raise ToolFailure("data_budget_exceeded", "本轮专业资料查询次数已达上限。")

    try:
        client = _data_client(ctx)
    except ToolFailure as exc:
        usage["blocked"] += 1
        _record_data_failure(ctx, category, exc.code)
        raise
    # 在进入查询客户端之前计一次尝试；缓存命中不会走到这里。
    usage["requests"] += 1
    results: queue.Queue = queue.Queue(maxsize=1)
    try:
        parameters = inspect.signature(client).parameters.values()
        accepts_timeout = any((parameter.name == "timeout_seconds" and
                               parameter.kind != inspect.Parameter.POSITIONAL_ONLY) or
                              parameter.kind == inspect.Parameter.VAR_KEYWORD
                              for parameter in parameters)
    except (TypeError, ValueError):
        accepts_timeout = False

    def request_once() -> None:
        try:
            payload = (client(query, timeout_seconds=remaining)
                       if accepts_timeout else client(query))
            results.put((payload, None))
        except Exception as exc:
            code = getattr(exc, "code", "data_provider_error")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code):
                code = "data_provider_error"
            results.put((None, code))

    threading.Thread(target=request_once, daemon=True).start()
    try:
        payload, error_code = results.get(timeout=remaining)
    except queue.Empty:
        usage["failed"] += 1
        usage["timeouts"] += 1
        _record_data_failure(ctx, category, "timeout")
        raise ToolFailure("data_timeout", "专业资料查询超时，本轮没有采用迟到结果。") from None
    if error_code:
        usage["failed"] += 1
        if "TIMEOUT" in error_code.upper() or "DEADLINE" in error_code.upper():
            usage["timeouts"] += 1
            _record_data_failure(ctx, category, error_code)
            raise ToolFailure("data_timeout", "专业资料查询超时；结果和计费状态未知，未自动重试。")
        _record_data_failure(ctx, category, error_code)
        if error_code in {"HTTP_401", "HTTP_403", "KEY_INVALID", "KEY_UNAVAILABLE"}:
            raise ToolFailure("data_auth_error", "专业资料凭证读取或鉴权失败；请检查工程外 Key 文件与服务权限。")
        raise ToolFailure("data_provider_error", "专业资料服务返回错误，本轮未采用其结果。")
    return payload


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ToolFailure("data_structure_error", "专业资料服务返回了无法解析的数据结构。") from None


def _json_chars(value: Any) -> int:
    return len(_json_bytes(value).decode("utf-8"))


def _clean_provider_data(value: Any) -> Any:
    """Keep a bounded, redacted business snapshot; never persist provider credentials."""
    if isinstance(value, dict):
        return {
            str(key): "[已隐藏]" if _SECRET_FIELD_RE.search(str(key)) else _clean_provider_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_clean_provider_data(item) for item in value]
    return redact(value)


def _normalized_dataset_category(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())
    return _DATASET_TYPE_ALIASES_NORMALIZED.get(key)


def _json_pointer_tokens(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 300:
        raise ToolFailure("invalid_field_path", "字段路径必须是有效的 JSON Pointer。")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _json_pointer_value(value: Any, pointer: str) -> Any:
    current = value
    for part in _json_pointer_tokens(pointer):
        if isinstance(current, dict):
            if part not in current:
                raise ToolFailure("field_not_found", "字段路径在该依据中不存在。")
            current = current[part]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part) or int(part) >= len(current):
                raise ToolFailure("field_not_found", "字段路径在该依据中不存在。")
            current = current[int(part)]
        else:
            raise ToolFailure("field_not_found", "字段路径在该依据中不存在。")
    if _json_chars(current) > _MAX_PROFESSIONAL_RECORD_CHARS:
        raise ToolFailure("field_too_large", "引用字段过大，请选择更具体的字段。")
    return current


def _scalar_paths(value: Any, prefix: str = "/record", depth: int = 0) -> list[tuple[str, Any]]:
    if depth > 4:
        return []
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if len(str(key)) > 100 or _SECRET_FIELD_RE.search(str(key)):
                continue
            token = str(key).replace("~", "~0").replace("/", "~1")
            found.extend(_scalar_paths(child, prefix + "/" + token, depth + 1))
            if len(found) >= 24:
                break
    elif isinstance(value, list):
        if (value and all(not isinstance(child, (dict, list)) for child in value)
                and len(_json_bytes(value)) <= 600):
            found.append((prefix, value))
            return found
        for index, child in enumerate(value[:10]):
            found.extend(_scalar_paths(child, prefix + "/" + str(index), depth + 1))
            if len(found) >= 24:
                break
    elif isinstance(value, (str, int, float, bool)) or value is None:
        if len(_json_bytes(value)) <= 800:
            found.append((prefix, value))
    return found


def _professional_preview(item: dict[str, Any]) -> dict[str, Any]:
    paths = _scalar_paths(item)
    preferred = (
        "/record/title", "/record/name", "/record/company_name", "/record/companyName",
        "/record/公司名称", "/record/企业名称", "/record/企业全称",
        "/record/risk_level", "/record/risk_type", "/record/risk_name", "/record/risk_status",
        "/record/风险等级", "/record/风险类型", "/record/风险事项", "/record/风险描述",
        "/record/企业状态", "/record/status", "/record/经营状态",
        "/record/成立日期", "/record/统一社会信用代码", "/record/公司ID",
        "/record/series_name", "/record/indicator_name", "/record/time_raw", "/record/unit_raw",
        "/record/report_title", "/record/summary", "/record/description",
        "/record/abstract", "/record/conclusion",
    )
    by_path = dict(paths)
    selected = [path for path in preferred if path in by_path]
    for path, _value in paths:
        if path not in selected:
            selected.append(path)
        if len(selected) >= 4:
            break
    fields = [{"field_path": path, "value": by_path[path]} for path in selected[:4]]
    excerpt = ""
    for path in selected:
        value = by_path[path]
        if isinstance(value, str) and value.strip():
            excerpt = value.strip()[:180]
            break
    return {"field_paths": selected[:4], "excerpt": excerpt, "fields": fields}


def _professional_source(item: dict[str, Any]) -> dict[str, Any]:
    """从供应商原记录提取已有来源字段，不补造链接或日期含义。

    返回值随候选证据保存并用于页面；核准日期与统计期不冒充发布时间。
    """
    nested = item.get("source") if isinstance(item.get("source"), dict) else {}
    aliases = {
        "url": ("url", "source_url", "link"),
        "provider": ("provider", "publisher", "source_name"),
        "record_id": ("record_id", "source_id", "company_id", "enterprise_id",
                      "unified_social_credit_code", "social_credit_code", "credit_code", "uscc",
                      "统一社会信用代码", "公司ID", "企业ID", "id"),
        "published_at": ("published_at", "publish_date", "published_date", "date_published"),
        "source_system": ("source_system",),
        "dataset_id": ("dataset_id",),
        "statistical_period": ("time_raw", "period", "reporting_period"),
        "unit": ("unit_raw", "unit", "measurement_unit", "currency"),
    }

    def find(names: tuple[str, ...]):
        for container, prefix in ((nested, "source"), (item, "record")):
            for name in names:
                value = container.get(name)
                if value not in (None, ""):
                    return name, value, f"/{prefix}/{name}"
        return None, None, None

    source: dict[str, Any] = {}
    missing: list[str] = []
    for target, names in aliases.items():
        field_name, value, _field_path = find(names)
        if target == "provider" and value is None:
            value = "DataPro"
        if target == "url" and value is not None:
            if not isinstance(value, str) or not value.lower().startswith(("https://", "http://")):
                value = None
                missing.append("url_invalid")
        if isinstance(value, str):
            max_chars = 1_000 if target == "url" else 300
            value = redact(value[:max_chars])
        elif value is not None and not isinstance(value, (int, float, bool)):
            value = None
            missing.append(target + "_invalid")
        source[target] = value
        if target == "published_at":
            source["published_at_field"] = field_name
        if value is None:
            missing.append(target)

    time_aliases = (
        "published_at", "publish_date", "published_date", "date_published", "date",
        "time_raw", "p_date", "approval_date", "approved_date", "核准日期",
        "valid_from", "validity_start", "validity_start_date", "有效期起", "有效期自",
        "valid_to", "validity_end", "validity_end_date", "有效期止", "有效期至",
        "year", "年份", "年度",
    )
    time_fields = []
    seen_paths = set()
    for container, prefix in ((nested, "source"), (item, "record")):
        for name in time_aliases:
            value = container.get(name)
            field_path = f"/{prefix}/{name}"
            if value in (None, "") or field_path in seen_paths:
                continue
            if isinstance(value, str):
                safe_value = redact(value[:300])
            elif isinstance(value, (int, float, bool)):
                safe_value = value
            else:
                continue
            time_fields.append({"field": name, "field_path": field_path, "value": safe_value})
            seen_paths.add(field_path)
    source["time_fields"] = time_fields
    year_name, year_value, _year_path = find(("year", "年份", "年度"))
    if isinstance(year_value, str):
        year_value = redact(year_value[:100])
    elif year_value is not None and not isinstance(year_value, (int, float, bool)):
        year_value = None
    source["year"] = year_value
    source["year_field"] = year_name
    source["missing_fields"] = sorted(set(missing))
    return source


def _save_professional_record(
    ctx: dict[str, Any], *, category: str, query: str, trace_id: str | None,
    dataset_type: str | None, route_verified: bool, item: dict[str, Any],
    query_evidence_id: str, limitations: list[str], item_index: int | None = None,
    source_event_id: str | None = None, grant: bool = True,
) -> str:
    safe_item = _clean_provider_data(item)
    source = _professional_source(safe_item)
    if category == 'web_search':
        # 网页没有统计单位和数据库编号很正常；只提示网页核查需要的缺失字段。
        source['missing_fields'] = [field for field in source['missing_fields']
                                    if field in ('url', 'url_invalid', 'published_at', 'provider')]
    data = {
        "category": category,
        "source_type": "web_search" if category == "web_search" else "professional_dataset",
        "dataset_type": dataset_type,
        "query": query,
        "trace_id": trace_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "item_index": item_index,
        "record": safe_item,
        "source": source,
        "route_verified": route_verified,
        "limitations": list(limitations),
        "query_evidence_id": query_evidence_id,
        "dependency_evidence_ids": [query_evidence_id],
    }
    data['quality_flags'] = quality_flags(data)
    return _save_evidence(ctx, "professional_record", data, event_id=source_event_id, grant=grant)


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


def _query_professional_data(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """args 是模型提出且已校验的类别与查询；ctx 来自主循环。

    调用客户端后检查业务状态，保存查询快照与候选证据，返回短索引给模型。
    索引只是定位入口，正式引用还需要 get_record 实际读取采用字段。
    """
    category = args["category"]
    query = args["query"].strip()
    if category not in _PROFESSIONAL_CATEGORIES:
        raise ToolFailure("invalid_category", "专业资料类别不在允许范围内。")
    if not query or len(query) > 600:
        raise ToolFailure("invalid_query", "查询内容必须为 1 到 600 个字符。")

    cache = ctx.get("data_cache")
    if not isinstance(cache, dict):
        cache = {}
        ctx["data_cache"] = cache
    cache_key = (category, query)
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("error"), dict):
        _data_usage(ctx)["cache_hits"] += 1
        raise ToolFailure(cached["error"]["code"], cached["error"]["message"])
    if isinstance(cached, dict) and isinstance(cached.get("data"), dict) and isinstance(cached.get("evidence_ids"), list):
        for evidence_id in cached["evidence_ids"]:
            _grant(ctx, evidence_id)
        _data_usage(ctx)["cache_hits"] += 1
        return {"data": cached["data"], "evidence_ids": list(cached["evidence_ids"])}

    prefixed_query = _PROFESSIONAL_CATEGORIES[category] + query
    try:
        payload = _call_data_client(ctx, category, prefixed_query)
    except ToolFailure as exc:
        # 同一轮连失败也复用，防止模型反复提出相同查询而重复发起付费尝试。
        cache[cache_key] = {"error": {"code": exc.code, "message": exc.message}}
        raise
    return _save_query_result(ctx, category, query, payload, cache, cache_key)


def _search_web(ctx, args):
    """独立搜索计数，共用外部证据保存逻辑；不允许模型选择服务地址。"""
    from .doubao_search import build_request_body, search_web, SearchError
    try:
        body = build_request_body(args['query'], args.get('count', 5), args.get('sites'),
                                  args.get('time_range', ''), args.get('authority', 0))
    except SearchError as error:
        raise ToolFailure(error.code, '搜索条件无效，尚未发出请求。') from None
    # 浅复制让账本、当前事件和引用许可仍共享；只替换查询计数和缓存。
    routed = dict(ctx)
    routed['data_usage'] = ctx.setdefault('search_usage', {})
    routed['data_cache'] = ctx.setdefault('search_cache', {})
    routed['data_failures'] = ctx.setdefault('search_failures', [])
    routed['data_request_limit'] = MAX_SEARCH_REQUESTS
    client = ctx.get('search_client') or search_web
    options = {'count': args.get('count', 5), 'sites': args.get('sites'),
               'time_range': args.get('time_range', ''), 'authority': args.get('authority', 0)}
    routed['data_client'] = lambda query, timeout_seconds: client(query, **options, timeout_seconds=timeout_seconds)
    cache = routed['data_cache']
    cache_key = json.dumps(body, sort_keys=True, ensure_ascii=False)
    cached = cache.get(cache_key)
    if cached:
        _data_usage(routed)['cache_hits'] += 1
        if 'error' in cached:
            raise ToolFailure(cached['error']['code'], cached['error']['message'])
        for evidence_id in cached['evidence_ids']:
            _grant(ctx, evidence_id)
        return {'data': cached['data'], 'evidence_ids': list(cached['evidence_ids'])}
    try:
        payload = _call_data_client(routed, 'web_search', body['Query'])
    except ToolFailure as error:
        cache[cache_key] = {'error': {'code': error.code, 'message': error.message}}
        raise
    result = _save_query_result(routed, 'web_search', body['Query'], payload, cache, cache_key)
    return result


def _save_query_result(ctx, category, query, payload, cache, cache_key):
    """DataPro 和网页搜索共用的资料检查、候选保存与短索引；来源类型由程序赋值。"""
    try:
        if not isinstance(payload, dict):
            raise ToolFailure("data_structure_error", "专业资料服务返回格式无效。")
        raw_bytes = _json_bytes(payload)
        if len(raw_bytes) >= _MAX_DATA_RESPONSE_BYTES:
            raise ToolFailure("data_response_too_large", "专业资料返回内容超过安全大小上限。")
        code = payload.get("code")
        if isinstance(code, bool) or code not in (0, "0"):
            raise ToolFailure("data_business_error", "专业资料查询未成功，本轮没有引用该结果。")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ToolFailure("data_structure_error", "专业资料服务没有返回候选记录列表。")
        if not items:
            raise ToolFailure("data_empty", "没有找到可引用的专业资料记录。")
        if any(not isinstance(item, dict) for item in items):
            raise ToolFailure("data_structure_error", "专业资料候选记录格式无效。")
        if not any(_professional_preview(item)["field_paths"] for item in items):
            raise ToolFailure("data_structure_error", "专业资料候选没有可索引的业务字段。")

        dataset_type = payload.get("dataset_type")
        dataset_type = dataset_type[:120] if isinstance(dataset_type, str) else None
        normalized_type = _normalized_dataset_category(dataset_type)
        if normalized_type and normalized_type != category:
            raise ToolFailure("data_wrong_type", "返回资料类别与请求类别不匹配。")
        route_verified = normalized_type == category
        limitations: list[str] = []
        if category == "web_search":
            limitations.append("豆包结果仅为搜索摘要或片段，未阅读全文；排序与权威标签不证明内容正确。")
        if not route_verified:
            limitations.append("返回资料类型未映射到已核验类别，不能单独作为充分事实。")
        trace_id = payload.get("trace_id")
        trace_id = redact(trace_id[:200]) if isinstance(trace_id, str) else None
        clean_payload = _clean_provider_data(payload)
        clean_bytes = _json_bytes(clean_payload)
        if len(clean_bytes) >= _MAX_DATA_RESPONSE_BYTES:
            raise ToolFailure("data_response_too_large", "脱敏后的专业资料超过安全大小上限。")
    except ToolFailure as exc:
        usage = _data_usage(ctx)
        usage["failed"] += 1
        _record_data_failure(ctx, category, exc.code)
        cache[cache_key] = {"error": {"code": exc.code, "message": exc.message}}
        raise

    usage = _data_usage(ctx)
    usage["success"] += 1
    snapshot_data = {
        "category": category,
        "query": query,
        "source_type": "web_search" if category == "web_search" else "professional_dataset",
        "trace_id": trace_id,
        "dataset_type": dataset_type,
        "route_verified": route_verified,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "response_sha256": hashlib.sha256(clean_bytes).hexdigest(),
        "response": clean_payload,
        "limitations": list(limitations),
    }
    snapshot_id = _save_evidence(ctx, "professional_query", snapshot_data)

    source_event_id = ctx.get("current_event_id")
    candidate_ids: list[str] = []
    for item_index, item in enumerate(items):
        source = _professional_source(_clean_provider_data(item))
        if category == 'web_search':
            source['missing_fields'] = [field for field in source['missing_fields']
                                        if field in ('url', 'url_invalid', 'published_at', 'provider')]
        record_limitations = list(limitations)
        if source["missing_fields"]:
            record_limitations.append("来源元数据缺失：" + "、".join(source["missing_fields"]))
        candidate_ids.append(_save_professional_record(
            ctx, category=category, query=query, trace_id=trace_id,
            dataset_type=dataset_type, route_verified=route_verified, item=item,
            query_evidence_id=snapshot_id, limitations=record_limitations,
            item_index=item_index, source_event_id=source_event_id, grant=False,
        ))

    index_data: dict[str, Any] = {
        "category": category,
        "query_evidence_id": snapshot_id,
        "dataset_type": dataset_type,
        "route_verified": route_verified,
        "items": [],
        "limitations": list(limitations),
        "more_items_path_prefix": "/response/items/",
    }
    evidence_ids = [snapshot_id]
    for item_index, item in enumerate(items):
        preview = _professional_preview(item)
        if not preview["field_paths"]:
            continue
        row = {
            "evidence_id": candidate_ids[item_index],
            "field_paths": preview["field_paths"],
            "excerpt": preview["excerpt"][:120],
        }
        trial = {**index_data, "items": [*index_data["items"], row]}
        trial_ids = [*evidence_ids, row["evidence_id"]]
        # Include the complete tool-result envelope in the 1,500-character limit.
        envelope = {"ok": True, "data": trial, "error": None, "evidence_ids": trial_ids}
        if len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))) > _MAX_PROFESSIONAL_INDEX_CHARS:
            if not index_data["items"]:
                row["field_paths"] = row["field_paths"][:1]
                row["excerpt"] = row["excerpt"][:40]
                trial["items"] = [row]
                envelope["data"] = trial
                envelope["evidence_ids"] = trial_ids
                if len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))) > _MAX_PROFESSIONAL_INDEX_CHARS:
                    continue
            else:
                break

        record_id = candidate_ids[item_index]
        _grant(ctx, record_id)
        index_data["items"].append(row)
        evidence_ids.append(record_id)

    data = index_data
    if len(json.dumps({"ok": True, "data": data, "error": None, "evidence_ids": evidence_ids},
                      ensure_ascii=False, separators=(",", ":"))) > _MAX_PROFESSIONAL_INDEX_CHARS:
        # This should be unreachable because each candidate was checked before saving.
        raise ToolFailure("data_index_too_large", "专业资料索引超过安全大小上限。")
    cache[cache_key] = {"data": data, "evidence_ids": list(evidence_ids)}
    return {"data": data, "evidence_ids": evidence_ids}


def _professional_record_preview(record: dict[str, Any]) -> dict[str, Any]:
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    item = data.get("record") if isinstance(data.get("record"), dict) else {}
    preview = _professional_preview(item)
    return {
        "id": record.get("id"), "kind": record.get("kind"),
        "category": data.get("category"), "dataset_type": data.get("dataset_type"),
        "route_verified": data.get("route_verified"), "source": data.get("source"),
        "field_paths": preview["field_paths"], "excerpt": preview["excerpt"],
        "limitations": data.get("limitations", []),
    }


def _get_professional_record(ctx: dict[str, Any], record: dict[str, Any], field_path: str | None):
    """把会话内保存的资料变成有长度上限的工具结果，不执行原文中的指令。

    整条记录过大时只提供预览；模型可用字段路径继续读取，避免把全文塞进上下文。
    """
    data = record["data"]
    if record.get("kind") == "professional_query":
        response = data.get("response") if isinstance(data.get("response"), dict) else {}
        items = response.get("items") if isinstance(response.get("items"), list) else []
        if not field_path:
            return {
                "id": record.get("id"), "kind": record.get("kind"),
                "category": data.get("category"), "dataset_type": data.get("dataset_type"),
                "route_verified": data.get("route_verified"), "trace_id": data.get("trace_id"),
                "fetched_at": data.get("fetched_at"), "response_sha256": data.get("response_sha256"),
                "item_count": len(items), "limitations": data.get("limitations", []),
                "field_path_example": "/response/items/0",
            }

        tokens = _json_pointer_tokens(field_path)
        if (len(tokens) == 3 and tokens[0] == "response" and tokens[1] == "items"
                and re.fullmatch(r"0|[1-9][0-9]*", tokens[2])):
            item_index = int(tokens[2])
            if item_index >= len(items) or not isinstance(items[item_index], dict):
                raise ToolFailure("field_not_found", "字段路径在该查询快照中不存在。")
            store = _store()
            existing = store.list_evidence(_session(ctx)[2], _session(ctx)[0], record.get("turn_id"))
            candidate_id = None
            for item in existing:
                candidate_data = _dict_data(item)
                if (item.get("kind") == "professional_record" and candidate_data
                        and candidate_data.get("query_evidence_id") == record.get("id")
                        and candidate_data.get("item_index") == item_index):
                    candidate_id = item.get("id")
                    break
            if candidate_id is None:
                limitations = list(data.get("limitations", []))
                source = _professional_source(items[item_index])
                if source["missing_fields"]:
                    limitations.append("来源元数据缺失：" + "、".join(source["missing_fields"]))
                candidate_id = _save_professional_record(
                    ctx, category=data["category"], query=data.get("query", ""),
                    trace_id=data.get("trace_id"), dataset_type=data.get("dataset_type"),
                    route_verified=bool(data.get("route_verified")), item=items[item_index],
                    query_evidence_id=record["id"], limitations=limitations, item_index=item_index,
                    source_event_id=record.get("event_id"),
                )
            _grant(ctx, candidate_id)
            candidate = _evidence(ctx, candidate_id)
            return _professional_record_preview(candidate)

        return {"id": record.get("id"), "field_path": field_path,
                "value": _json_pointer_value(data, field_path)}

    if record.get("kind") != "professional_record":
        if field_path:
            raise ToolFailure("field_selection_unsupported", "字段选择只适用于专业资料依据。")
        return record
    if field_path:
        return {"id": record.get("id"), "kind": record.get("kind"),
                "field_path": field_path, "value": _json_pointer_value(data, field_path)}

    if _json_chars(record) <= _MAX_PROFESSIONAL_RECORD_CHARS:
        return record
    return _professional_record_preview(record)


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
        field_path = args.get("field_path")
        if record.get("kind") in {"professional_query", "professional_record"}:
            visible_record = _get_professional_record(ctx, record, field_path)
            extra_ids: list[str] = []
            if isinstance(visible_record, dict):
                selected_id = visible_record.get("id")
                if isinstance(selected_id, str) and selected_id != record_id:
                    _grant(ctx, selected_id)
                    extra_ids = _dependency_closure(ctx, [selected_id], grant_discovered=True)
            result = {"data": {"record_type": record_type, "record": visible_record},
                      "evidence_ids": list(dict.fromkeys(granted + extra_ids + [record.get("id")]))}
            result_ids = result["evidence_ids"]
            if record.get('kind') == 'professional_record':
                result['data']['quality_flags'] = quality_flags(record['data'])
            envelope = {"ok": True, "data": result["data"], "error": None, "evidence_ids": result_ids}
            if _json_chars(envelope) > _MAX_PROFESSIONAL_RECORD_CHARS:
                if not field_path and record.get("kind") == "professional_record":
                    result["data"]["record"] = _professional_record_preview(record)
                envelope["data"] = result["data"]
                if _json_chars(envelope) > _MAX_PROFESSIONAL_RECORD_CHARS:
                    raise ToolFailure("field_too_large", "Professional record response exceeds the size limit; select a smaller field.")
            # 授权 ID 不等于读过字段。只标记这次实际回传给模型的完整字段或子树。
            visible_record = result["data"]["record"]
            if visible_record.get("kind") == "professional_record":
                if "data" in visible_record:
                    _mark_professional_read(ctx, visible_record["id"], "/record")
                elif "value" in visible_record and "field_path" in visible_record:
                    _mark_professional_read(ctx, visible_record["id"], visible_record["field_path"])
            return result
        if field_path:
            raise ToolFailure("field_selection_unsupported", "字段选择只适用于专业资料依据。")
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


def _list_history(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """模型用分页索引找旧 ID；看到编号不等于读过或采用证据。"""
    session_id, _, db_path = _session(ctx)
    data = _store().history_index(db_path, session_id, args['record_type'],
                                  args.get('before_seq'), args.get('limit', 10))
    return {'data': dict(data, record_type=args['record_type']), 'evidence_ids': []}


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
    supersedes_id = args.get("supersedes_evidence_id")
    superseded = None
    if supersedes_id:
        supersedes_id = _require_id(supersedes_id, "待替代条件")
        if supersedes_id not in ctx.get("allowed_evidence_ids", set()):
            raise ToolFailure("evidence_not_allowed", "只能替代本轮已读取的同会话条件记录。")
        superseded = _evidence(ctx, supersedes_id)
        old_data = superseded["data"]
        if (superseded.get("kind") != "user_condition" or old_data.get("field") != args["field"]
                or old_data.get("status") not in {"自述", "待确认", "已核验"}):
            raise ToolFailure("invalid_superseded_condition", "只能关联同字段的既有用户条件，原记录不会被覆盖。")
    condition = {
        "field": args["field"],
        "quote": quote,
        "status": args["status"],
        "source_message_id": message_id,
        "source_message_turn_id": message.get("turn_id"),
        "protection": "用户自述与测评结果分开保存，不会覆盖测评分数。",
    }
    if superseded:
        condition["supersedes_evidence_id"] = superseded["id"]
        condition["superseded_source_message_id"] = superseded["data"].get("source_message_id")
        condition["dependency_evidence_ids"] = [superseded["id"]]
    evidence_id = _save_evidence(ctx, "user_condition", condition)
    return {"data": {"condition": condition, "evidence_id": evidence_id}, "evidence_ids": [evidence_id]}


_CLARIFYING_QUESTIONS = {
    "skills": "你目前掌握哪些具体技能？可以举一个实际例子吗？",
    "experience": "你做过哪些课程项目、社团活动或实习？",
    "location": "你希望在哪个城市或地区学习、实习或工作？",
    "availability": "你每周大约能投入几天，什么时候可以开始？",
    "goal": "你现阶段最想优先解决什么：探索方向、寻找实习，还是准备求职？",
    "company": "你想了解哪家企业？请提供企业全称或可核对的企业标识。",
    "industry": "你想比较哪个行业、地区或时间范围？",
    "job_description": "请提供岗位名称或岗位描述，说明你最想核实的要求。",
    "offer_terms": "你想评估哪些录用条件？可以说明岗位、地点、薪酬结构或需要核实的条款。",
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
    if _contains_free_text_number(free_text_fields):
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


_CHINESE_NUMERALS = "零〇一二两三四五六七八九十百千万亿点半"
_CHINESE_NUMBER_RE = re.compile(
    rf"(?:百分之[{_CHINESE_NUMERALS}]+|[{_CHINESE_NUMERALS}]+"
    rf"(?:万元|亿元|千元|百元|万|亿|千|百|成|折|倍|元|人|岁|个|家|项|年|月|天|季度|小时|分钟|公里|级|名|多|余))"
)


def _mark_professional_read(ctx: dict[str, Any], evidence_id: str, field_path: str) -> None:
    """记录本轮已回传的字段范围，只保留 ID 与路径，不复制资料。"""
    reads = ctx.setdefault("professional_reads", set())
    reads.add((evidence_id, tuple(_json_pointer_tokens(field_path))))


def _professional_field_was_read(ctx: dict[str, Any], evidence_id: str, field_path: str) -> bool:
    tokens = tuple(_json_pointer_tokens(field_path))
    return any(read_id == evidence_id and tokens[:len(prefix)] == prefix
               for read_id, prefix in ctx.get("professional_reads", set()))


def _contains_free_text_number(values: list[str]) -> bool:
    return any(
        any(character.isdigit() or character in "%％" for character in value)
        or _CHINESE_NUMBER_RE.search(value) is not None
        for value in values
    )


def _submit_decision(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """模型提出理由与引用，程序核对来源、已读字段及执行事件后保存建议。

    事实值从账本取出，生成标签不能替换原文。返回建议 ID 供主循环发布与页面展示。
    """
    session_id, turn_id, db_path = _session(ctx)
    free_text = [args["title"], *args["options"], *args["unknowns"], *args["next_actions"]]
    free_text.extend(claim["text"] for claim in args["claims"] if claim["kind"] == "inference")
    if _contains_free_text_number(free_text):
        raise ToolFailure("numeric_claim_not_allowed", "自由文本不能包含数字；已引用事实的数值由程序单独展示。")

    resolved_claims: list[dict[str, Any]] = []
    cited_ids: list[str] = []
    successful_professional_ids: set[str] = set()
    limitations: list[str] = []
    for claim in args["claims"]:
        kind = claim["kind"]
        citations = claim["citations"]
        if not citations:
            raise ToolFailure("missing_citation", "每条决策理由都必须关联可读取的依据。")
        resolved: list[dict[str, Any]] = []
        for citation in citations:
            evidence_id = _require_id(citation["evidence_id"], "依据")
            field_path = citation["field_path"]
            if evidence_id not in ctx.get("allowed_evidence_ids", set()):
                raise ToolFailure("evidence_not_allowed", "理由引用了本轮没有读取或授权的依据。")
            record = _evidence(ctx, evidence_id)
            data = record["data"]
            evidence_kind = record.get("kind")

            if evidence_kind == "professional_record":
                if kind == "user_statement":
                    raise ToolFailure("wrong_citation_kind", "用户自述理由只能引用本会话保存的用户原话。")
                if not field_path.startswith("/record/"):
                    raise ToolFailure("invalid_citation_path", "专业事实只能引用候选记录中的具体字段。")
                if not data.get("route_verified") and kind == "fact":
                    raise ToolFailure("unverified_data_route", "资料类型尚未核实，不能作为已确认事实提交。")
                value = _json_pointer_value(data, field_path)
                if value is None or value == "" or value == []:
                    raise ToolFailure("citation_empty", "The cited field is empty.")
                if isinstance(value, dict):
                    raise ToolFailure("citation_too_broad", "Cite a leaf field instead of a record object.")
                if isinstance(value, list):
                    if (any(isinstance(item, (dict, list)) for item in value)
                            or len(_json_bytes(value)) > 600):
                        raise ToolFailure("citation_too_broad", "Scalar arrays must fit within 600 bytes.")
                elif isinstance(value, str) and len(value) > 1_200:
                    raise ToolFailure("citation_too_broad", "Cited text fields are limited to 1,200 characters.")
                query_id = data.get("query_evidence_id")
                if not isinstance(query_id, str):
                    raise ToolFailure("invalid_professional_source", "专业记录没有对应的查询快照。")
                query_record = _evidence(ctx, query_id)
                if query_record.get("kind") != "professional_query":
                    raise ToolFailure("invalid_professional_source", "专业记录的查询来源无效。")
                if (query_record.get("turn_id") != record.get("turn_id")
                        or query_record["data"].get("category") != data.get("category")):
                    raise ToolFailure("invalid_professional_source", "专业记录与查询快照轮次不一致。")
                event_id = record.get("event_id")
                event = _store().get_event(db_path, session_id, event_id) if event_id else None
                if (not event or event.get("status") != "success"
                        or event.get("tool_name") != ("search_web" if data.get("category") == "web_search" else "query_professional_data")
                        or event.get("turn_id") != record.get("turn_id")):
                    raise ToolFailure("source_event_incomplete", "引用的专业查询尚未成功完成。")
                query_event_id = query_record.get("event_id")
                query_event = _store().get_event(db_path, session_id, query_event_id) if query_event_id else None
                if (not query_event or query_event.get("status") != "success"
                        or query_event.get("tool_name") != ("search_web" if data.get("category") == "web_search" else "query_professional_data")
                        or query_event.get("turn_id") != query_record.get("turn_id")):
                    raise ToolFailure("source_event_incomplete", "专业资料查询快照尚未成功完成。")
                if not _professional_field_was_read(ctx, evidence_id, field_path):
                    raise ToolFailure("field_not_read", "请先用 get_record 读取该具体字段，再提交引用。")
                successful_professional_ids.add(evidence_id)
                if not data.get("route_verified"):
                    limitation = "存在资料类型未核实的候选记录，不能作为事实依据。"
                    if limitation not in limitations:
                        limitations.append(limitation)
                source = data.get("source") if isinstance(data.get("source"), dict) else {}
                for flag in quality_flags(data):
                    if flag['message'] not in limitations:
                        limitations.append(flag['message'])
                missing = source.get("missing_fields", [])
                for item_limitation in data.get("limitations", []):
                    if isinstance(item_limitation, str) and item_limitation not in limitations:
                        limitations.append(item_limitation)
                if missing:
                    limitation = "部分引用资料缺少来源元数据：" + "、".join(str(item) for item in missing[:5])
                    if limitation not in limitations:
                        limitations.append(limitation)
            elif evidence_kind in {"user_input", "user_condition"}:
                if kind == "fact":
                    raise ToolFailure("wrong_citation_kind", "用户自述不能作为外部事实引用。")
                if field_path != "/quote" or not isinstance(data.get("quote"), str):
                    raise ToolFailure("invalid_citation_path", "用户自述只能引用已保存的原话字段。")
                value = _json_pointer_value(data, field_path)
            else:
                raise ToolFailure("wrong_citation_kind", "该记录类型不能作为通用决策理由的直接引用。")

            if kind == "user_statement" and evidence_kind not in {"user_input", "user_condition"}:
                raise ToolFailure("wrong_citation_kind", "用户自述理由只能引用本会话保存的用户原话。")

            if evidence_kind != "professional_record" and _json_chars(value) > 600:
                raise ToolFailure("citation_too_broad", "Cited user quote is too long.")
            cited_ids.append(evidence_id)
            resolved.append({"evidence_id": evidence_id, "field_path": field_path, "value": value})

        resolved_claims.append({
            "kind": kind,
            "text": ("引用字段：" + "、".join(citation["field_path"].rsplit("/", 1)[-1]
                                                for citation in claim["citations"])
                     if kind == "fact" else "用户原话" if kind == "user_statement" else claim["text"]),
            **({"generated_label": claim["text"]} if kind == "fact" else {}),
            "citations": [{"evidence_id": item["evidence_id"], "field_path": item["field_path"]}
                          for item in resolved],
            "resolved": resolved,
            "origin": "model_generated" if kind == "inference" else
                      "user_statement" if kind == "user_statement" else "source_derived",
        })

    if not successful_professional_ids:
        raise ToolFailure("professional_evidence_required", "通用决策至少需要一条查询成功的外部来源记录。")

    for failure in list(ctx.get("data_failures", [])) + list(ctx.get("search_failures", [])):
        if not isinstance(failure, dict):
            continue
        category = failure.get("category", "professional")
        code = failure.get("code", "data_error")
        limitation = f"本轮 {category} 资料查询未成功（{code}）；相关信息尚未核实。"
        if limitation not in limitations:
            limitations.append(limitation)

    evidence_ids = list(dict.fromkeys(cited_ids))
    closure = _dependency_closure(ctx, evidence_ids)
    data = {
        "kind": "career_decision",
        "title": args["title"],
        "options": list(args["options"]),
        "claims": resolved_claims,
        "unknowns": list(args["unknowns"]),
        "next_actions": list(args["next_actions"]),
        "limitations": limitations,
        "evidence_ids": closure,
        "turn_id": turn_id,
    }
    recommendation_id = _store().add_recommendation(db_path, session_id, turn_id, data)
    if isinstance(ctx.get("pending_recommendations"), list):
        ctx["pending_recommendations"].append(recommendation_id)
    return {
        "data": {"recommendation_id": recommendation_id, "recommendation": data},
        "evidence_ids": closure,
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
            "field_path": {"type": "string", "minLength": 1, "maxLength": 300},
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
            "supersedes_evidence_id": _ID_SCHEMA,
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
    "search_web": {
        "description": "豆包网页搜索：查官方招聘页、政策、公开网页线索；摘要并非全文。结果需 get_record 读取后引用。",
        "parameters": _object_schema({
            "query": {"type": "string", "minLength": 1, "maxLength": 100},
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
            "sites": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 253}},
            "time_range": {"type": "string", "enum": ["", "OneDay", "OneWeek", "OneMonth", "OneYear"]},
            "authority": {"type": "integer", "enum": [0, 1]},
        }, ["query"]),
        "permission": "business_read", "handler": _search_web,
    },
    "query_professional_data": {
        "description": "按类别查询受限的专业资料；返回已索引候选及依据编号，更多候选可按查询快照的 JSON Pointer 读取。",
        "parameters": _object_schema({
            "category": {"type": "string", "enum": list(_PROFESSIONAL_CATEGORIES)},
            "query": {"type": "string", "minLength": 1, "maxLength": 600},
        }, ["category", "query"]),
        "permission": "business_read",
        "handler": _query_professional_data,
    },
    "list_history": {
        "description": "分页查询本会话旧依据或已发布建议的编号；定位后须用 get_record 读取原文，索引不授予引用权限。",
        "parameters": _object_schema({
            "record_type": {"type": "string", "enum": ["evidence", "recommendation"]},
            "before_seq": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        }, ["record_type"]),
        "permission": "business_read",
        "handler": _list_history,
    },
    "submit_decision": {
        "description": "提交通用决策。事实值由引用字段自动解析；fact 标签由程序按字段名生成，user_statement 固定显示用户原话，只有 inference 的 text 会展示为模型推论。不要在标签中重写来源数字。",
        "parameters": _object_schema({
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "options": {"type": "array", "minItems": 1, "maxItems": 4,
                         "items": {"type": "string", "minLength": 1, "maxLength": 300}},
            "claims": {"type": "array", "minItems": 1, "maxItems": 12, "items": _object_schema({
                "kind": {"type": "string", "enum": ["fact", "user_statement", "inference"]},
                "text": {"type": "string", "minLength": 1, "maxLength": 300, "description": "fact/user_statement 的 text 不展示，标签由程序生成；仅为 inference 填写会展示的推论描述。"},
                "citations": {"type": "array", "minItems": 1, "maxItems": 6,
                              "items": _object_schema({
                                  "evidence_id": _ID_SCHEMA,
                                  "field_path": {"type": "string", "minLength": 1, "maxLength": 300},
                              }, ["evidence_id", "field_path"])},
            }, ["kind", "text", "citations"])},
            "unknowns": {"type": "array", "maxItems": 12,
                         "items": {"type": "string", "minLength": 1, "maxLength": 300}},
            "next_actions": {"type": "array", "maxItems": 8,
                             "items": {"type": "string", "minLength": 1, "maxLength": 300}},
        }, ["title", "options", "claims", "unknowns", "next_actions"]),
        "permission": "business_write",
        "handler": _submit_decision,
    },
    "ask_user": {
        "description": "从固定问题库中挑选澄清问题；不能传入自拟问题文本。主循环应把结果作为待用户回答的问题。",
        "parameters": _object_schema({
            "fields": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "uniqueItems": True,
                "items": {"type": "string", "enum": ["skills", "experience", "location", "availability", "goal",
                                                           "company", "industry", "job_description", "offer_terms"]},
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
