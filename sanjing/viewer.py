"""把某一轮已经保存的账本，写成可离线打开的静态 HTML 页面。"""

from __future__ import annotations

import html
import json
import re
import uuid
from pathlib import Path
from typing import Any

from . import store


_STYLE = """
:root { color-scheme: light; font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
  color: #243047; background: #f4f6fa; }
body { max-width: 980px; margin: 0 auto; padding: 28px 18px 56px; line-height: 1.65; }
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
table { width: 100%; border-collapse: collapse; margin-top: 10px; }
th, td { padding: 8px; text-align: left; vertical-align: top; border-bottom: 1px solid #e7ebf2; }
th { color: #465675; }
.status { display: inline-block; padding: 2px 9px; border-radius: 999px; background: #edf1f7; }
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
    text = _plain(value)
    return html.escape(text if text else empty, quote=True)


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


def _evidence_card(item: dict[str, Any]) -> str:
    """只渲染工具产生的业务字段，特别保留用户自述和来源。"""
    kind = item.get("kind")
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
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
        '<article class="card">'
        f'<h4>{_e(title)}</h4>'
        f'<div class="muted">类型：{_e(kind)}　记录 ID：<code>{_e(item.get("id"))}</code></div>'
        f"{content}</article>"
    )


def _recommendation_card(rec: dict[str, Any], index: int, user_input: str,
                         evidence_by_id: dict[str, dict[str, Any]],
                         evidence: list[dict[str, Any]], published: bool) -> str:
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
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

    # 若建議沒有明確列出採用的 evidence_ids，不把「本輪搜到」冒充為「建議採用」。
    evidence_items = linked_evidence
    evidence_heading = "该建议采用的依据" if linked_evidence else "本轮记录的候选依据"
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
        f"<h4>{_e(evidence_heading)}</h4>",
    ]
    if evidence_items:
        for item in evidence_items:
            pieces.append(_evidence_card(item))
    else:
        pieces.append('<p class="muted">没有关联到可展示的证据记录。</p>')
    pieces.append(_field_html("限制、缺失信息与待验证事项", limitations))
    pieces.append(
        '<p class="muted">提醒：记录之间存在引用关系，只能说明系统保存了这些关联；'
        '它本身不能证明引用材料真实支持这条结论。团队演示规则应明确标为待验证假设。</p>'
    )
    pieces.append("</article>")
    return "".join(pieces)


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
    status = turn.get("status") or "unknown"
    answer = turn.get("answer")
    error_code = turn.get("error_code")
    published = status == "completed"
    cards = "".join(_recommendation_card(rec, index, user_input, evidence_by_id, evidence, published)
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
            cards += "".join(_evidence_card(item) for item in evidence if isinstance(item, dict))

    event_rows: list[str] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        values = [item.get("tool_name"), item.get("call_id"), item.get("status"),
                  item.get("error_code"), item.get("duration_ms")]
        event_rows.append("<tr>" + "".join(f"<td>{_e(value, '—')}</td>" for value in values) + "</tr>")
    log_table = (
        "<table><thead><tr><th>工具</th><th>调用 ID</th><th>状态</th>"
        "<th>错误代码</th><th>耗时（毫秒）</th></tr></thead><tbody>"
        + ("".join(event_rows) if event_rows else '<tr><td colspan="5">没有工具执行记录。</td></tr>')
        + "</tbody></table>"
    )

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
