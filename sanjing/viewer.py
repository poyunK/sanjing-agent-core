"""把某一轮已经保存的账本，写成可离线打开的静态 HTML 页面。"""

from __future__ import annotations

import html
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from . import store


_STYLE = """
:root { color-scheme: light; font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
  color: #243047; background: #f4f6fa; }
body { max-width: 980px; margin: 0 auto; padding: 28px 18px 56px; line-height: 1.65; overflow-wrap: anywhere; }
h1 { margin-bottom: 4px; font-size: 1.8rem; }
h2 { margin-top: 30px; padding-bottom: 8px; border-bottom: 1px solid #dbe1eb; }
h3 { margin: 0 0 8px; font-size: 1.15rem; }
.muted { color: #64718a; }
.notice { padding: 14px 16px; border-left: 4px solid #d97706; background: #fff7e6; border-radius: 6px; }
.card { margin: 14px 0; padding: 18px; background: #fff; border: 1px solid #e1e6ef; border-radius: 10px; box-shadow: 0 2px 8px #2430470a; }
.field { margin: 12px 0; }
.field strong { display: block; margin-bottom: 3px; color: #384967; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: #f5f7fb; border-radius: 6px; }
code { overflow-wrap: anywhere; }
details { margin-top: 14px; padding: 12px 14px; background: #fff; border: 1px solid #e1e6ef; border-radius: 8px; }
summary { cursor: pointer; font-weight: 600; }
table { width: 100%; table-layout: fixed; border-collapse: collapse; margin-top: 10px; }
th, td { padding: 8px; text-align: left; vertical-align: top; border-bottom: 1px solid #e7ebf2; overflow-wrap: anywhere; }
th { color: #465675; }
.status { display: inline-block; padding: 2px 9px; border-radius: 999px; background: #edf1f7; }
.source-link { overflow-wrap: anywhere; }
.claim { margin: 12px 0; padding: 12px; border-left: 3px solid #6684b7; background: #f7f9fc; }
.source-value { margin: 7px 0; padding: 8px 10px; background: #fff; border: 1px solid #e4e9f1; border-radius: 6px; }
"""


def _safe_turn_filename(turn_id: str) -> str:
    """只接受 UUID 轮次 ID，确保它不能变成目录穿越路径。"""
    value = str(turn_id)
    # store.new_id("turn") 会返回 turn_ 加 32 位 UUID 十六进制字符串。
    if value.startswith("turn_") and re.fullmatch(r"turn_[0-9a-fA-F]{32}", value):
        return value.lower() + ".html"
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("turn_id 必须是 UUID，才能安全生成账本文件名") from exc
    return f"turn-{normalized}.html"


def _plain(value: Any) -> str:
    """把任意字段变成可读文本；具体 HTML 转义统一在 _e 中进行。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def _e(value: Any, empty: str = "暂无记录") -> str:
    text = _redact_sensitive_text(_plain(value))
    return html.escape(text if text else empty, quote=True)


_SECRET_LABEL_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?key|access[_ -]?token|token|secret|password|authorization|cookie)"
    r"(\s*[:=]\s*)([^\s,;\"'<>]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;\"'<>]+")
_SK_KEY_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}")
_SENSITIVE_NAME_RE = re.compile(
    r"(?i)(?:api.?key|access.?key|access.?token|authorization|bearer|token|secret|password|cookie|header|signature|credential)"
)
_SENSITIVE_URL_PARAM_RE = re.compile(
    r"(?i)(?:^|[_-])(?:key|auth|secret|token|password|signature|credential)(?:$|[_-])"
)
_SAFE_RECORD_FIELDS = {
    "name", "title", "label", "id", "record_id", "code", "credit_code", "stock_code",
    "ticker", "symbol", "type", "status", "date", "published_at", "published_date",
    "updated_at", "year", "region", "province", "city", "industry", "category",
    "summary", "description", "amount", "unit", "currency", "value", "rate", "ratio",
    "score", "rank", "price", "volume", "revenue", "profit", "registered_capital",
    "founded_at", "established_at", "company_name", "company_status", "policy_name",
    "paper_title", "journal", "doi", "source_type", "missing_fields",
    "series_name", "indicator_name", "entity_name", "source_system", "dataset_id",
    "dataset_name", "time_raw", "time_grain", "unit_raw", "p_date", "date_published",
}
_FACT_EVIDENCE_KINDS = {"professional_record", "job_profile", "assessment", "calculation", "prototype_rule"}


def _redact_sensitive_text(text: str) -> str:
    text = _BEARER_RE.sub("Bearer [已隐藏]", text)
    text = _SECRET_LABEL_RE.sub(r"\1\2[已隐藏]", text)
    return _SK_KEY_RE.sub("[已隐藏密钥]", text)


def _anchor_id(evidence_id: Any) -> str:
    digest = hashlib.sha256(str(evidence_id).encode("utf-8", "replace")).hexdigest()[:16]
    return f"evidence-{digest}"


def _safe_http_url(value: Any) -> str | None:
    """只允许不含凭证的绝对 HTTP(S) URL；不回显被拒绝的链接。"""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if "\\" in value or any(ch.isspace() or ord(ch) < 32 or ch in '<>"\'' for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port  # 触发非法端口校验
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
            if _SENSITIVE_NAME_RE.search(key) or _SENSITIVE_URL_PARAM_RE.search(key):
                return None
        if _SENSITIVE_NAME_RE.search(parsed.fragment):
            return None
    except (ValueError, UnicodeError):
        return None
    return value


def _id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for entry in value:
        if isinstance(entry, dict):
            entry = _pick(entry, "evidence_id", "id", "snapshot_id")
        if isinstance(entry, (str, int)) and str(entry):
            result.append(str(entry))
    return result


def _path_parts(field_path: Any) -> list[str]:
    if not isinstance(field_path, str) or not field_path or len(field_path) > 512:
        return []
    if field_path.startswith("/"):
        parts = [part.replace("~1", "/").replace("~0", "~")
                 for part in field_path.split("/")[1:]]
    else:
        parts = [part for part in field_path.replace(".", "/").strip("/").split("/") if part]
    while parts and parts[0] in {"data", "record"}:
        parts.pop(0)
    if not parts or any(_SENSITIVE_NAME_RE.search(part) for part in parts):
        return []
    return parts


def _resolve_record_path(record: dict[str, Any], field_path: Any) -> Any:
    current: Any = record
    parts = _path_parts(field_path)
    if not parts:
        return None
    try:
        for part in parts:
            if isinstance(current, dict):
                current = current[part]
            elif isinstance(current, (list, tuple)) and part.isdigit():
                current = current[int(part)]
            else:
                return None
    except (KeyError, IndexError, TypeError):
        return None
    return current


def _readable_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (dict, set)):
        return None
    if isinstance(value, (list, tuple)):
        if len(value) > 20 or any(isinstance(part, (dict, list, tuple, set)) for part in value):
            return None
        return list(value)
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > 1200:
            return value[:1200] + "…"
        return value
    return None


def _selected_record_fields(data: dict[str, Any], record: dict[str, Any],
                            cited_paths: list[str] | None = None) -> list[tuple[str, Any]]:
    """返回明确选择、被引用，或常用且安全的浅层源字段；不序列化整条记录。"""
    selected: list[tuple[str, Any]] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        parts = _path_parts(path)
        if not parts:
            return
        normalized = "/".join(parts)
        if normalized in seen:
            return
        value = _readable_scalar(_resolve_record_path(record, normalized))
        if value is None:
            return
        seen.add(normalized)
        selected.append((normalized, value))

    requested = data.get("selected_fields")
    if isinstance(requested, dict):
        requested = list(requested.keys())
    if isinstance(requested, (list, tuple)):
        for field in requested[:30]:
            if isinstance(field, str):
                add(field)
            elif isinstance(field, dict):
                add(_pick(field, "field_path", "path", "field") or "")
    for path in cited_paths or []:
        add(path)
    for key in record:
        if not isinstance(key, str) or _SENSITIVE_NAME_RE.search(key):
            continue
        if key.lower() in _SAFE_RECORD_FIELDS or key in {"公司名称", "企业名称", "企业状态", "经营状态", "成立日期", "统一社会信用代码"}:
            add(key)
    return selected[:40]


def _url_html(url: Any) -> str:
    safe_url = _safe_http_url(url)
    if safe_url is None:
        if url:
            return '<span class="muted">来源链接已隐藏（无效或包含凭证信息）</span>'
        return '<span class="muted">未提供可核验链接</span>'
    safe = html.escape(safe_url, quote=True)
    return f'<a class="source-link" href="{safe}" rel="noreferrer noopener">{safe}</a>'


def _field_html(label: str, value: Any) -> str:
    """渲染一个普通文本字段，不把资料内容解释成 HTML。"""
    if value is None or value == "" or value == [] or value == {}:
        return ""
    return f'<div class="field"><strong>{_e(label)}</strong><pre>{_e(value)}</pre></div>'


def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return None


def _contributions_html(value: Any) -> str:
    """把程序计算的逐项贡献排成表格，方便人逐项复核。"""
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
        return _field_html("匹配过程与各项贡献", value)
    headings = ["测评维度", "本次输入", "规则权重", "规则匹配度", "贡献分", "缺失"]
    columns = ["label", "user_value", "weight", "fit_percent", "contribution_points", "missing"]
    header = "".join(f"<th>{_e(label)}</th>" for label in headings)
    rows = []
    for item in value:
        cells = "".join(f"<td>{_e(item.get(key), '—')}</td>" for key in columns)
        rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="field"><strong>匹配过程与各项贡献</strong>'
        '<p class="muted">匹配度与贡献分来自团队演示规则，不代表录用概率。</p>'
        f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _professional_query_card(item: dict[str, Any], data: dict[str, Any]) -> str:
    candidate_indexes = _pick(data, "candidate_indexes", "candidate_indices", "candidate_index")
    if isinstance(candidate_indexes, (list, tuple)):
        indexes = [value for value in candidate_indexes[:100]
                   if isinstance(value, (str, int)) and len(str(value)) < 80]
        candidate_text: Any = indexes if indexes else None
    elif isinstance(candidate_indexes, (str, int)):
        candidate_text = candidate_indexes
    else:
        candidate_count = data.get("candidate_count")
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            response = data.get("response")
            if isinstance(response, list):
                candidates = response
            elif isinstance(response, dict):
                candidates = _pick(response, "candidates", "items", "records")
        if not isinstance(candidates, list):
            payload = data.get("payload")
            if isinstance(payload, list):
                candidates = payload
        if isinstance(candidate_count, int) and not isinstance(candidate_count, bool) and candidate_count >= 0:
            candidate_text = list(range(min(candidate_count, 100)))
        elif isinstance(candidates, list):
            candidate_text = list(range(min(len(candidates), 100)))
        else:
            candidate_text = None

    snapshot = data.get("snapshot")
    snapshot_meta = snapshot if isinstance(snapshot, dict) else {}
    response_hash = _pick(data, "snapshot_hash", "snapshot_sha256", "response_sha256")
    if response_hash is None:
        response_hash = _pick(snapshot_meta, "snapshot_hash", "sha256", "hash")
    route_verified = data.get("route_verified")
    route_text = "已验证" if route_verified is True else "未验证" if route_verified is False else route_verified
    fields = [
        ("数据类别", data.get("category")),
        ("数据集类型", data.get("dataset_type")),
        ("查询内容", data.get("query")),
        ("调用追踪 ID", data.get("trace_id")),
        ("路由验证", route_text),
        ("获取时间", data.get("fetched_at")),
        ("快照 / 响应哈希", response_hash),
        ("候选索引（不含原始候选内容）", candidate_text),
        ("限制", data.get("limitations")),
    ]
    content = "".join(_field_html(label, value) for label, value in fields)
    return (
        f'<article class="card" id="{_anchor_id(item.get("id"))}" data-kind="professional-query">'
        '<h4>专业数据查询记录</h4>'
        f'<div class="muted">类型：专业数据查询　证据 ID：<code>{_e(item.get("id"))}</code></div>'
        f"{content}"
        '<p class="muted">原始 payload、snapshot、response 和请求头不在页面展开；请查看单独保存的源记录证据。</p>'
        '</article>'
    )


def _professional_record_card(item: dict[str, Any], data: dict[str, Any],
                              cited_paths: list[str] | None = None) -> str:
    record = data.get("record") if isinstance(data.get("record"), dict) else {}
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    selected = _selected_record_fields(data, record, cited_paths)
    selected_html = "".join(
        '<tr><th>' + _e(path) + '</th><td>' + _e(value) + '</td></tr>'
        for path, value in selected
    )
    if not selected_html:
        selected_html = '<tr><td colspan="2">没有可安全展开的已选源字段。</td></tr>'
    missing = _pick(source, "missing_fields", "missing")
    route_verified = data.get("route_verified")
    route_text = "已验证" if route_verified is True else "未验证" if route_verified is False else route_verified
    safe_url = _url_html(source.get("url"))
    provenance = [
        ("数据类别", data.get("category")),
        ("数据集类型", data.get("dataset_type")),
        ("查询", data.get("query")),
        ("调用追踪 ID", data.get("trace_id")),
        ("路由验证", route_text),
        ("获取时间", data.get("fetched_at")),
        ("来源提供方", source.get("provider")),
        ("来源记录 ID", source.get("record_id")),
        ("来源发布时间", source.get("published_at")),
        ("原文年份", source.get("year")),
        ("原文日期字段（保持原含义）", source.get("time_fields")),
        ("原始来源系统", source.get("source_system")),
        ("原始数据集编号", source.get("dataset_id")),
        ("统计期（原字段）", source.get("statistical_period")),
        ("单位（原字段）", source.get("unit")),
        ("缺失字段", missing),
        ("查询证据 ID", data.get("query_evidence_id")),
        ("依赖快照 ID", data.get("dependency_evidence_ids")),
        ("限制", data.get("limitations")),
    ]
    metadata = "".join(_field_html(label, value) for label, value in provenance)
    return (
        f'<article class="card" id="{_anchor_id(item.get("id"))}" data-kind="professional-record">'
        '<h4>专业数据源记录</h4>'
        f'<div class="muted">类型：专业数据记录　证据 ID：<code>{_e(item.get("id"))}</code></div>'
        f"{metadata}"
        f'<div class="field"><strong>来源链接</strong><p>{safe_url}</p></div>'
        '<details><summary>展开可读源字段</summary>'
        '<table><thead><tr><th>字段路径</th><th>程序选择的字段值</th></tr></thead>'
        f'<tbody>{selected_html}</tbody></table></details>'
        '</article>'
    )


def _evidence_card(item: dict[str, Any], cited_paths: list[str] | None = None) -> str:
    """只渲染工具产生的业务字段，特别保留用户自述和来源。"""
    kind = item.get("kind")
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    if kind == "professional_query":
        return _professional_query_card(item, data)
    if kind == "professional_record":
        return _professional_record_card(item, data, cited_paths)
    nested: dict[str, Any] = {}
    fields: list[tuple[str, Any]] = []

    if kind == "assessment":
        nested = data.get("assessment") if isinstance(data.get("assessment"), dict) else {}
        title = _pick(nested, "label", "assessment_id") or "测评输入"
        fields = [("测评结果", nested.get("scores")), ("数据来源", data.get("source") or nested.get("source")),
                  ("限制", data.get("limitations")), ("演示状态", data.get("demo_label"))]
    elif kind == "job_profile":
        nested = data.get("job") if isinstance(data.get("job"), dict) else {}
        title = _pick(nested, "title", "job_id") or "岗位资料"
        fields = [("岗位介绍", nested.get("summary")), ("岗位要求", nested.get("requirements")),
                  ("资料来源", nested.get("source") or data.get("source")),
                  ("检索方式", data.get("retrieval_method")), ("演示状态", data.get("demo"))]
    elif kind == "prototype_rule":
        title = data.get("rule_label") or "团队原型规则"
        fields = [("规则版本", data.get("rule_version")), ("规则内容", data.get("rule")),
                  ("规则来源", data.get("source")), ("限制", data.get("limitations")),
                  ("演示状态", data.get("demo"))]
    elif kind == "calculation":
        title = data.get("job_title") or "匹配计算"
        computed = data.get("computed") if isinstance(data.get("computed"), dict) else {}
        fields = [("岗位编号", data.get("job_id")), ("规则版本", data.get("rule_version")),
                  ("规则名称", data.get("rule_label")), ("计算分数", computed.get("score")),
                  ("各项贡献", computed.get("contributions")), ("计算公式", computed.get("formula")),
                  ("缺失项", computed.get("missing")), ("限制", data.get("limitations")),
                  ("演示标记", computed.get("demo_label"))]
    elif kind == "user_condition":
        title = "用户自述 / 待确认条件"
        fields = [("条件字段", data.get("field")), ("用户原话", data.get("quote")),
                  ("确认状态", data.get("status")), ("来源消息 ID", data.get("source_message_id")),
                  ("说明", data.get("protection"))]
    elif kind == "user_input":
        title = "本轮用户输入记录"
        fields = [("用户原话", data.get("quote")), ("来源", data.get("source"))]
    else:
        title = str(kind or "依据记录")
        fields = [("记录内容", data)]

    content = "".join(_field_html(label, value) for label, value in fields)
    return (
        f'<article class="card" id="{_anchor_id(item.get("id"))}">'
        f'<h4>{_e(title)}</h4>'
        f'<div class="muted">类型：{_e(kind)}　记录 ID：<code>{_e(item.get("id"))}</code></div>'
        f"{content}</article>"
    )


def _resolved_value_html(resolved: dict[str, Any]) -> str:
    value = _readable_scalar(resolved.get("value"))
    if value is None:
        return "程序未提供可展示的已解析值。"
    unit = _pick(resolved, "unit", "currency")
    text = _plain(value)
    if unit is not None and str(unit) not in text:
        text += " " + _plain(unit)
    return _e(text)


def _citation_source_html(evidence_id: str, field_path: str,
                          evidence_by_id: dict[str, dict[str, Any]]) -> str:
    item = evidence_by_id.get(evidence_id)
    if not item:
        return '<div class="muted">来源记录不在本轮账本中，无法展开出处。</div>'
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    kind = item.get("kind")
    if kind == "professional_query":
        rows = "".join(_field_html(label, data.get(key)) for label, key in (
            ("数据类别", "category"), ("数据集类型", "dataset_type"),
            ("查询内容", "query"), ("获取时间", "fetched_at"),
            ("响应哈希", "response_sha256"), ("限制", "limitations"),
        ))
        return (
            f'<div class="source-value"><a href="#{_anchor_id(evidence_id)}">打开查询记录 {_e(evidence_id)}</a>'
            f'<div class="muted">字段：<code>{_e(field_path)}</code>；查询元数据不是源记录</div>'
            f'{rows}</div>'
        )
    if kind == "professional_record":
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        meta = [
            ("提供方", source.get("provider")),
            ("源记录 ID", source.get("record_id")),
            ("发布时间", source.get("published_at")),
            ("原文年份", source.get("year")),
            ("原文日期字段", source.get("time_fields")),
            ("原始来源系统", source.get("source_system")),
            ("统计期（原字段）", source.get("statistical_period")),
            ("获取时间", data.get("fetched_at")),
            ("数据集", data.get("dataset_type")),
            ("单位（原字段）", source.get("unit")),
            ("限制", data.get("limitations")),
            ("来源缺失项", _pick(source, "missing_fields", "missing")),
        ]
        resolved_record = data.get("record") if isinstance(data.get("record"), dict) else {}
        parts = _path_parts(field_path)
        if parts and parts[0] == "record":
            parts = parts[1:]
        if parts:
            sibling = resolved_record
            for part in parts[:-1]:
                sibling = sibling.get(part) if isinstance(sibling, dict) else None
            if isinstance(sibling, dict):
                sibling_unit = _pick(sibling, "unit_raw", "unit", "currency", "measurement_unit")
                if sibling_unit is not None:
                    meta = [(label, sibling_unit if label == "单位（原字段）" else value)
                            for label, value in meta]
        rows = "".join(_field_html(label, value) for label, value in meta)
        anchor = _anchor_id(evidence_id)
        source_link = _url_html(source.get("url"))
        return (
            f'<div class="source-value"><a href="#{anchor}">打开源记录 {_e(evidence_id)}</a>'
            f'<div class="muted">字段：<code>{_e(field_path)}</code></div>{rows}'
            f'<div class="field"><strong>来源链接</strong><p>{source_link}</p></div></div>'
        )

    title = str(kind or "依据记录")
    source_value = None
    if kind == "job_profile":
        job = data.get("job") if isinstance(data.get("job"), dict) else {}
        title = str(_pick(job, "title", "job_id") or title)
        source_value = _pick(job, "source") or data.get("source")
    elif kind == "assessment":
        assessment = data.get("assessment") if isinstance(data.get("assessment"), dict) else {}
        title = str(_pick(assessment, "label", "assessment_id") or "测评依据")
        source_value = _pick(assessment, "source") or data.get("source")
    elif kind == "user_condition":
        title = "用户自述 / 待确认条件"
        source_value = _pick(data, "quote", "source_message_id")
    rows = _field_html("依据类型", kind) + _field_html("依据名称", title)
    rows += _field_html("来源说明", source_value)
    return (
        f'<div class="source-value"><a href="#{_anchor_id(evidence_id)}">打开依据 {_e(evidence_id)}</a>'
        f'<div class="muted">字段：<code>{_e(field_path)}</code></div>{rows}</div>'
    )


def _claim_html(claim: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> str:
    kind = claim.get("kind")
    text = claim.get("text")
    citations = claim.get("citations") if isinstance(claim.get("citations"), list) else []
    resolved = claim.get("resolved") if isinstance(claim.get("resolved"), list) else []
    resolved_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for value in resolved:
        if isinstance(value, dict):
            pair = (str(value.get("evidence_id", "")), str(value.get("field_path", "")))
            resolved_by_pair.setdefault(pair, []).append(value)

    has_source_resolved_value = any(
        any(_readable_scalar(value.get("value")) is not None for value in resolved_by_pair.get(
            (str(citation.get("evidence_id", "")), str(citation.get("field_path", ""))), []
        ))
        and str(citation.get("evidence_id", "")) in evidence_by_id
        and evidence_by_id[str(citation.get("evidence_id"))].get("kind") in _FACT_EVIDENCE_KINDS
        for citation in citations if isinstance(citation, dict)
    )

    if kind == "fact":
        label = "来源字段事实（由程序解析）" if has_source_resolved_value else "事实声明待核验：没有完整的程序解析来源"
        text_label = "说明文字（模型生成，需与下列字段核对）"
    elif kind == "user_statement":
        label = "用户自述"
        text_label = "用户原话 / 自述内容"
    elif kind == "inference":
        label = "模型生成推断"
        text_label = "模型生成推断（并非源数据事实）"
    else:
        label = "未分类主张（来源待确认）"
        text_label = "主张文字"

    parts = ['<section class="claim">', f'<strong>{_e(label)}</strong>']
    parts.append(_field_html("声明来源类型", claim.get("origin")))
    if text is not None and text != "":
        parts.append(_field_html(text_label, text))
    if not citations:
        parts.append('<p class="muted">没有可展示的字段级来源引用。</p>')
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        evidence_id = str(citation.get("evidence_id", ""))
        field_path = str(citation.get("field_path", ""))
        parts.append('<div class="source-value">')
        parts.append(f'<div><strong>来源字段</strong>：<code>{_e(field_path)}</code></div>')
        parts.append(f'<div class="muted">证据 ID：<code>{_e(evidence_id)}</code></div>')
        matches = resolved_by_pair.get((evidence_id, field_path), [])
        source_item = evidence_by_id.get(evidence_id)
        if not source_item or source_item.get("kind") == "professional_query":
            matches = []
        if matches:
            for value in matches:
                parts.append('<div><strong>程序解析值：</strong>' + _resolved_value_html(value) + '</div>')
        else:
            parts.append('<div class="muted">没有与此引用对应的程序解析值，不能将文字当作已核实事实。</div>')
        parts.append(_citation_source_html(evidence_id, field_path, evidence_by_id))
        parts.append('</div>')
    parts.append('</section>')
    return "".join(parts)


def _option_cards(options: Any) -> str:
    if isinstance(options, dict):
        entries: list[Any] = [dict(value, name=key) if isinstance(value, dict) and "name" not in value
                              else value for key, value in options.items()]
    elif isinstance(options, (list, tuple)):
        entries = list(options[:30])
    elif options is None:
        entries = []
    else:
        entries = [options]
    cards = []
    allowed = ("title", "name", "label", "summary", "description", "rationale", "risks",
               "constraints", "feasibility", "next_step")
    for index, option in enumerate(entries, start=1):
        if isinstance(option, dict):
            title = _pick(option, "title", "name", "label") or f"选项 {index}"
            fields = "".join(_field_html(key, option.get(key)) for key in allowed if key in option and key not in {"title", "name", "label"})
        else:
            title = f"选项 {index}"
            fields = _field_html("选项内容", option)
        cards.append(f'<article class="source-value"><strong>{_e(title)}</strong>{fields}</article>')
    return "".join(cards) if cards else '<p class="muted">本轮没有记录决策选项。</p>'


def _evidence_refs_html(label: str, ids: list[str], evidence_by_id: dict[str, dict[str, Any]],
                        *, adopted: bool) -> str:
    title = "采用的依据" if adopted else "本轮候选依据"
    if not ids:
        return f'<h4>{_e(title)}</h4><p class="muted">{_e(label)}</p>'
    rows = []
    for evidence_id in ids:
        item = evidence_by_id.get(evidence_id)
        if not item:
            rows.append(f'<li><code>{_e(evidence_id)}</code>（该记录不在本轮账本中）</li>')
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        kind = item.get("kind")
        if kind == "professional_record":
            source = data.get("source") if isinstance(data.get("source"), dict) else {}
            display = " / ".join(str(value) for value in (
                data.get("category"), data.get("dataset_type"), source.get("record_id"), source.get("published_at")
            ) if value is not None)
        elif kind == "professional_query":
            display = " / ".join(str(value) for value in (
                data.get("category"), data.get("dataset_type"), data.get("fetched_at")
            ) if value is not None)
        else:
            display = str(kind or "依据记录")
        rows.append(
            f'<li><a href="#{_anchor_id(evidence_id)}">{_e(evidence_id)}</a>'
            f'　<span class="muted">{_e(display or kind)}</span></li>'
        )
    return f'<h4>{_e(title)}</h4><ul>{"".join(rows)}</ul>'


def _career_decision_card(rec: dict[str, Any], data: dict[str, Any], index: int,
                          user_input: str, evidence_by_id: dict[str, dict[str, Any]],
                          published: bool, cited_paths: dict[str, list[str]]) -> str:
    title = _pick(data, "title") or f"职业决策建议 {index}"
    evidence_ids = _id_list(data.get("evidence_ids"))
    candidate_ids = _id_list(_pick(data, "candidate_evidence_ids", "candidate_ids"))
    claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    claims_html = "".join(_claim_html(claim, evidence_by_id)
                          for claim in claims if isinstance(claim, dict))
    if not claims_html:
        claims_html = '<p class="muted">没有结构化结论；不会把普通回复提升为已核验建议。</p>'
    options = data.get("options")
    unknowns = _pick(data, "unknowns", "missing_information")
    next_actions = _pick(data, "next_actions", "next_action")
    limitations = _pick(data, "limitations", "caveats")
    citation_ids: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("citations"), list):
            continue
        citation_ids.extend(
            str(citation.get("evidence_id")) for citation in claim["citations"]
            if isinstance(citation, dict) and citation.get("evidence_id") is not None
        )
    referenced_ids = list(dict.fromkeys(evidence_ids + candidate_ids + citation_ids))
    source_cards = "".join(
        _evidence_card(evidence_by_id[evidence_id], cited_paths.get(evidence_id))
        for evidence_id in referenced_ids if evidence_id in evidence_by_id
    )
    pieces = [
        '<article class="card" data-kind="career-decision">',
        f"<h3>{_e(title)}</h3>",
        '<p class="status">已发布建议</p>' if published else
        '<p class="notice">这条建议记录尚未随本轮完成状态发布，仅作为未发布草稿查看。</p>',
        f'<div class="muted">建议记录：<code>{_e(rec.get("id"))}</code></div>',
        _field_html("决策类型", data.get("kind")),
        _field_html("本轮用户输入", user_input),
        _field_html("建议摘要", _pick(data, "summary", "explanation")),
        '<h4>可比较选项</h4>', _option_cards(options),
        '<h4>逐条结论、字段值与来源</h4>', claims_html,
        _evidence_refs_html("没有声明整体采用的证据 ID；请查看各结论的逐条引用。", evidence_ids,
                            evidence_by_id, adopted=True),
        _evidence_refs_html("没有记录候选依据 ID。", candidate_ids,
                            evidence_by_id, adopted=False),
        '<h4>关联来源记录与可读字段</h4>',
        source_cards or '<p class="muted">没有可展开的关联来源记录。</p>',
        _field_html("未知项 / 仍需确认", unknowns),
        _field_html("下一步行动", next_actions),
        _field_html("限制", limitations),
        '<p class="muted">逐条引用仅表示账本中保存了来源关联；仍需核对来源字段、适用时间和限制后再使用结论。</p>',
        '</article>',
    ]
    return "".join(pieces)


def _recommendation_card(rec: dict[str, Any], index: int, user_input: str,
                         evidence_by_id: dict[str, dict[str, Any]],
                         evidence: list[dict[str, Any]], published: bool,
                         cited_paths: dict[str, list[str]]) -> str:
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    if data.get("kind") == "career_decision":
        return _career_decision_card(rec, data, index, user_input, evidence_by_id,
                                     published, cited_paths)
    title = _pick(data, "title", "job_title", "label", "name", "role") or f"建议 {index}"
    summary = _pick(data, "explanation", "summary", "recommendation", "answer")
    rule_version = _pick(data, "rule_version", "rules_version", "version")
    computed = data.get("computed") if isinstance(data.get("computed"), dict) else {}
    inputs = _pick(data, "calculation_inputs", "inputs", "profile_inputs", "user_inputs")
    contributions = _pick(data, "contributions", "factors", "calculation", "match_details")
    if contributions is None:
        contributions = computed.get("contributions")
    limitations = _pick(data, "limitations", "missing_information", "unknowns", "caveats")
    evidence_ids = _pick(data, "evidence_ids", "adopted_evidence_ids", "source_ids") or []
    if isinstance(evidence_ids, str):
        evidence_ids = [evidence_ids]
    linked_evidence = [evidence_by_id[str(item)] for item in evidence_ids
                       if str(item) in evidence_by_id]
    candidate_ids = _id_list(_pick(data, "candidate_evidence_ids", "candidate_ids"))

    # 若建議沒有明確列出採用的 evidence_ids，不把「本輪搜到」冒充為「建議採用」。
    evidence_items = linked_evidence
    if not linked_evidence:
        rec_turn_id = rec.get("turn_id")
        evidence_items = [item for item in evidence
                          if not rec_turn_id or item.get("turn_id") == rec_turn_id]

    pieces = [
        '<article class="card" data-kind="recommendation">',
        f"<h3>{_e(title)}</h3>",
        '<p class="status">已发布建议</p>' if published else
        '<p class="notice">这条建议记录尚未随本轮完成状态发布，仅作为未发布草稿查看。</p>',
        f'<div class="muted">建议记录：<code>{_e(rec.get("id"))}</code></div>',
        _field_html("建议说明", summary),
        _field_html("本轮用户输入", user_input),
        _field_html("参与计算的输入", inputs),
        _field_html("规则版本", rule_version),
        _field_html("解释来源", _pick(data, "explanation_origin")),
        _field_html("规则/演示状态", _pick(data, "demo_label")),
        _field_html("程序计算分数", computed.get("score")),
        _field_html("计算公式", computed.get("formula")),
        _contributions_html(contributions),
        _field_html("计算中缺失的维度", _pick(computed, "missing") or data.get("missing")),
        _field_html("下一步验证建议", _pick(data, "next_action")),
        "<h4>该建议采用的依据</h4>" if linked_evidence else "<h4>本轮记录的候选依据</h4>",
    ]
    if evidence_items:
        for item in evidence_items:
            pieces.append(_evidence_card(item, cited_paths.get(str(item.get("id")))))
    else:
        pieces.append('<p class="muted">没有关联到可展示的证据记录。</p>')
    if candidate_ids:
        pieces.append(_evidence_refs_html("没有候选依据。", candidate_ids,
                                          evidence_by_id, adopted=False))
    pieces.append(_field_html("限制、缺失信息与待验证事项", limitations))
    pieces.append(
        '<p class="muted">提醒：记录之间存在引用关系，只能说明系统保存了这些关联；'
        '它本身不能证明引用材料真实支持这条结论。团队演示规则应明确标为待验证假设。</p>'
    )
    pieces.append("</article>")
    return "".join(pieces)


def _data_usage_html(usage: Any) -> str:
    labels = {
        "requests": "专业查询客户端尝试次数", "success": "返回有效候选次数",
        "failed": "查询失败次数", "timeouts": "查询超时次数",
        "cache_hits": "本轮结果复用次数", "blocked": "调用前拦截次数（未进入查询客户端）",
        "input_tokens": "输入 token 数", "prompt_tokens": "提示 token 数",
        "output_tokens": "输出 token 数", "completion_tokens": "生成 token 数",
        "total_tokens": "总 token 数", "cached_tokens": "缓存 token 数",
        "model_calls": "模型调用次数", "tool_calls": "工具调用次数",
        "data_queries": "数据查询次数", "records_seen": "候选记录数",
        "records_selected": "采用记录数", "elapsed_ms": "累计耗时（毫秒）",
    }
    rows = []
    if isinstance(usage, dict):
        for key, label in labels.items():
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                rows.append(f"<tr><th>{_e(label)}</th><td>{_e(value)}</td></tr>")
            elif isinstance(value, str) and len(value) <= 80:
                rows.append(f"<tr><th>{_e(label)}</th><td>{_e(value)}</td></tr>")
    table = (
        '<table><thead><tr><th>统计项</th><th>记录值</th></tr></thead><tbody>'
        + ("".join(rows) if rows else '<tr><td colspan="2">没有可展示的使用统计。</td></tr>')
        + '</tbody></table>'
    )
    return (
        '<h4>本轮使用统计</h4>' + table
        + '<p class="muted">费用没有在本地账本中核验。调用超时不代表供应商已取消请求，'
          '该次请求是否产生费用目前未知。</p>'
    )


def _build_html(snapshot: dict[str, Any]) -> str:
    turn = snapshot.get("turn") if isinstance(snapshot.get("turn"), dict) else {}
    turn_id = turn.get("id", "")
    session_id = snapshot.get("session_id", "")
    messages = snapshot.get("messages") if isinstance(snapshot.get("messages"), list) else []
    current_user_messages = [
        item for item in messages
        if isinstance(item, dict) and item.get("role") == "user"
        and (not item.get("turn_id") or item.get("turn_id") == turn_id)
    ]
    user_input = "\n\n".join(_plain(item.get("content")) for item in current_user_messages)
    recommendations = snapshot.get("recommendations")
    recommendations = recommendations if isinstance(recommendations, list) else []
    evidence = snapshot.get("evidence")
    evidence = evidence if isinstance(evidence, list) else []
    events = snapshot.get("events")
    events = events if isinstance(events, list) else []

    evidence_by_id = {str(item.get("id")): item for item in evidence if isinstance(item, dict)}
    cited_paths: dict[str, list[str]] = {}
    for rec in recommendations:
        if not isinstance(rec, dict) or not isinstance(rec.get("data"), dict):
            continue
        claims = rec["data"].get("claims")
        if not isinstance(claims, list):
            continue
        for claim in claims:
            if not isinstance(claim, dict) or not isinstance(claim.get("citations"), list):
                continue
            for citation in claim["citations"]:
                if not isinstance(citation, dict):
                    continue
                evidence_id = citation.get("evidence_id")
                field_path = citation.get("field_path")
                if evidence_id is None or not isinstance(field_path, str):
                    continue
                cited_paths.setdefault(str(evidence_id), []).append(field_path)
    status = turn.get("status") or "unknown"
    answer = turn.get("answer")
    error_code = turn.get("error_code")
    published = status == "completed"
    cards = "".join(_recommendation_card(rec, index, user_input, evidence_by_id, evidence,
                                           published, cited_paths)
                     for index, rec in enumerate(recommendations, start=1)
                     if isinstance(rec, dict))
    if not cards:
        cards = ('<div class="card"><p>本轮没有已验证的建议记录。</p>'
                 + (_field_html("当前状态", status))
                 + (_field_html("错误代码", error_code))
                 + (_field_html("Agent 回复", answer))
                 + '<p class="muted">如果 Agent 直接输出了职业结论，但没有成功提交建议账本，'
                   '此页面不会把它标记为已验证建议。</p></div>')
        if evidence:
            cards += '<h3>本轮输入、条件与候选依据</h3>'
            cards += "".join(_evidence_card(item, cited_paths.get(str(item.get("id"))))
                              for item in evidence if isinstance(item, dict))

    event_rows: list[str] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        values = [item.get("tool_name"), item.get("call_id"), item.get("status"),
                  item.get("error_code"), item.get("duration_ms")]
        timeout = any("TIMEOUT" in str(value).upper() for value in values if value is not None)
        cost = "未知；超时不代表请求已取消" if timeout else "未知；本地未记录费用"
        event_rows.append("<tr>" + "".join(f"<td>{_e(value, '—')}</td>" for value in values)
                          + f"<td>{_e(cost)}</td></tr>")
    log_table = (
        "<table><thead><tr><th>工具</th><th>调用 ID</th><th>状态</th>"
        "<th>错误代码</th><th>耗时（毫秒）</th><th>费用状态</th></tr></thead><tbody>"
        + ("".join(event_rows) if event_rows else '<tr><td colspan="6">没有工具执行记录。</td></tr>')
        + "</tbody></table>"
    )
    turn_usage = turn.get("usage") if isinstance(turn.get("usage"), dict) else {}
    usage_html = _data_usage_html(turn.get("data_usage") or turn_usage.get("data_requests"))
    if turn_usage:
        usage_html += _field_html("模型用量（与专业查询分别记录）", {
            key: turn_usage[key] for key in ("requests", "prompt_tokens", "completion_tokens", "total_tokens", "unreported_requests")
            if key in turn_usage})
        if turn_usage.get("unreported_requests"):
            usage_html += '<p class="muted">部分模型请求未回传完整 token 用量；当前累计值不代表全部消耗。</p>'

    # 页面不显示模型原始消息/思维内容；只展示结构化建议、用户输入和工具事件。
    page_title = "三镜 Agent · 本轮依据账本"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
  <title>{_e(page_title)}</title>
  <style>{_STYLE}</style>
</head>
<body>
  <h1>{_e(page_title)}</h1>
  <p class="muted">会话：<code>{_e(session_id)}</code>　轮次：<code>{_e(turn_id)}</code>　状态：<span class="status">{_e(status)}</span></p>
  <p class="notice">这里展示程序保存的输入、规则、证据关联和工具执行结果。引用有效不等于内容一定支持结论；自然语言说明也不代表职业建议已得到科学验证。页面不展示模型内部思维过程。</p>
  <h2>建议与依据</h2>
  {cards}
  <details>
    <summary>展开查看工具执行记录</summary>
    {log_table}
    {usage_html}
  </details>
</body>
</html>
"""


def write_snapshot(db_path: str | Path, session_id: str, turn_id: str,
                   output_dir: str | Path) -> Path:
    """读取数据库快照并写入该轮独立页面，返回生成文件的绝对路径。"""
    filename = _safe_turn_filename(turn_id)
    snapshot = store.snapshot(db_path, session_id, turn_id)
    if not isinstance(snapshot, dict):
        raise TypeError("store.snapshot 必须返回字典")
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    target.write_text(_build_html(snapshot), encoding="utf-8")
    return target
