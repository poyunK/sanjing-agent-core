"""通用本地交接与任务看板。只写协议状态，不执行任务命令或修改源码。"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import uuid


PROTOCOL = "LAH/1.0"
LOCAL = ".handoff-local"
SKILL = Path(__file__).resolve().parents[1]
ID_PATTERN = re.compile(r"t-[0-9]{8}-[a-f0-9]{12}\Z")
CLOSED = {"accepted", "rejected", "cancelled"}
STAGES = {
    "backlog": "待办", "offered": "已派单", "running": "执行中",
    "returned": "待验收", "accepted": "已验收", "rework": "需返工",
    "rejected": "未接受", "cancelled": "已取消",
}
SKIP_DIRS = {
    ".git", LOCAL, "runtime", "__pycache__", ".venv", "venv",
    "node_modules", ".idea", ".vscode", "secrets", "private", "credentials",
}
SECRET_PATTERN = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9_.-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|access[_-]?token|password|x-agent-plan-key)"
    r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{12,}", re.I
)


class ProtocolError(ValueError):
    """错误码交给 GUI 解释；不把任意原文或秘密回显给终端。"""


def require(condition, code):
    if not condition:
        raise ProtocolError(code)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def load_json(path):
    path = Path(path)
    require(path.stat().st_size <= 2 * 1024 * 1024, "message_too_large")
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=no_duplicate_keys)


def check_secrets(value):
    require(not SECRET_PATTERN.search(canonical(value)), "possible_credential_in_message")


def atomic_write(path, text):
    """先写临时文件再替换：崩溃时尽量保留上一次完整记录。"""
    path = Path(path)
    require(not path.is_symlink(), "symlink_state")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def is_link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def denied(relative):
    parts = PurePosixPath(relative).parts
    name = parts[-1].lower() if parts else ""
    return (
        any(part.lower() in SKIP_DIRS for part in parts)
        or name.startswith(".env") or ".sqlite" in name
        or name.endswith((".log", ".pem", ".p12", ".pfx", ".key"))
        or ("key" in name and name.endswith(".txt"))
        or name in {"id_rsa", "id_ed25519"}
        or (name.startswith("credentials") and name.endswith(".json"))
    )


def checked_path(root, relative):
    """验证相对路径，拒绝跳出项目、通配符、秘密路径和符号链接。"""
    require(isinstance(relative, str) and 0 < len(relative) <= 1024, "invalid_path")
    require(not any(char in relative for char in "\\:*?\n\r\0"), "invalid_path")
    stripped = relative.rstrip("/")
    require(stripped and not stripped.startswith("/"), "invalid_path")
    require(all(part not in {"", ".", ".."} for part in stripped.split("/")), "invalid_path")
    require(not denied(stripped), "sensitive_path")
    path = root.joinpath(*stripped.split("/"))
    cursor = path
    while cursor != root:
        require(not is_link(cursor), "symlink_path")
        cursor = cursor.parent
    require(path.resolve().is_relative_to(root), "path_outside_project")
    return path


def git(root, *args):
    """固定 argv 的只读 Git 调用；不执行任务给出的命令字符串。"""
    run = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30)
    return run.returncode, run.stdout


def git_head(root):
    try:
        code, top = git(root, "rev-parse", "--show-toplevel")
        if code:
            return None
        require(Path(os.fsdecode(top).strip()).resolve() == root, "use_git_project_root")
        code, head = git(root, "rev-parse", "--verify", "HEAD")
        return head.decode("ascii").strip() if not code else None
    except FileNotFoundError:
        return None


def source_files(root):
    """含未提交源码，不含运行产物；哈希清单不复制文件内容。"""
    try:
        code, top = git(root, "rev-parse", "--show-toplevel")
    except FileNotFoundError:
        code, top = 1, b""
    if not code:
        require(Path(os.fsdecode(top).strip()).resolve() == root, "use_git_project_root")
        code, listing = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        require(not code, "git_file_list_failed")
        candidates = sorted(set(os.fsdecode(item) for item in listing.split(b"\0") if item))
    else:
        candidates = []
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name.lower() not in SKIP_DIRS]
            for name in dirs:
                require(not is_link(Path(folder) / name), "symlink_path")
            candidates.extend((Path(folder) / name).relative_to(root).as_posix() for name in files)
    result = {}
    for relative in sorted(set(candidates)):
        if denied(relative):
            continue
        path = checked_path(root, relative)
        if path.is_file():
            require(path.stat().st_size <= 20 * 1024 * 1024, "source_file_too_large")
            result[relative] = digest(path.read_bytes())
    require(len(result) <= 5000, "snapshot_too_large")
    return result


def snapshot(root, project):
    payload = {"project_id": project["id"], "git_head": git_head(root), "files": source_files(root)}
    return {"id": digest(canonical(payload).encode("utf-8")), **payload}


def local_path(root, relative):
    path = root / LOCAL / relative
    cursor = path
    while cursor != root:
        require(not is_link(cursor), "symlink_state")
        cursor = cursor.parent
    require(path.resolve().is_relative_to(root), "state_outside_project")
    return path


def task_dir(root, task_id):
    require(isinstance(task_id, str) and ID_PATTERN.fullmatch(task_id), "invalid_task_id")
    return local_path(root, "tasks/" + task_id)


def project_info(root, create=False):
    path = local_path(root, "project.json")
    if path.exists():
        project = load_json(path)
        require(Path(project["root"]).resolve() == root, "project_moved_reissue_tasks")
        return project
    require(create, "project_not_initialized")
    project = {"id": "p-" + uuid.uuid4().hex, "root": str(root), "created_at": now()}
    save_json(path, project)
    return project


def active(root):
    path = local_path(root, "ACTIVE.json")
    return load_json(path) if path.exists() else None


@contextmanager
def transaction(root, stopped_note=None):
    """目录创建具有互斥性；持久 ACTIVE 另负责跨 GUI 的任务占用。"""
    require(root.is_dir(), "project_not_found")
    git_head(root)
    try:
        code, _ = git(root, "rev-parse", "--show-toplevel")
        if not code:
            code, _ = git(root, "check-ignore", "--no-index", LOCAL + "/probe")
            require(code == 0, "add_handoff_local_to_gitignore_first")
    except FileNotFoundError:
        pass
    state_root = local_path(root, "")
    state_root.mkdir(exist_ok=True)
    lock = local_path(root, "OPERATION.lock")
    if lock.exists() and stopped_note:
        # 只在调用方已核对停止证据时恢复，不依据年龄或超时抢锁。
        require(stopped_note.strip(), "recovery_needs_stop_evidence")
        check_secrets(stopped_note)
        lock.rmdir()
    try:
        lock.mkdir()
    except FileExistsError:
        raise ProtocolError("operation_busy_or_interrupted_audit_before_unlock") from None
    try:
        project_info(root, create=True)
        yield
    finally:
        lock.rmdir()


def strings(value, field, nonempty=False):
    require(isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value), "invalid_" + field)
    require(not nonempty or bool(value), "empty_" + field)


def validate_spec(spec, root):
    require(isinstance(spec, dict), "invalid_spec")
    required = {"sender", "receiver", "mode", "goal", "user_authorization", "context",
                "confirmed_decisions", "read_first", "write_scope", "acceptance", "out_of_scope", "external_actions"}
    optional = {"allow_delete", "allow_commit", "parent_receipt", "title", "priority", "depends_on"}
    require(required <= spec.keys() and spec.keys() <= required | optional, "spec_fields")
    for name in ("sender", "receiver", "goal", "user_authorization"):
        require(isinstance(spec[name], str) and spec[name].strip(), "invalid_" + name)
    require(isinstance(spec["context"], str), "invalid_context")
    require(spec["mode"] in {"edit", "review", "plan"}, "invalid_mode")
    for name in ("confirmed_decisions", "read_first", "write_scope", "acceptance", "out_of_scope", "external_actions"):
        strings(spec[name], name, name == "acceptance")
    require(bool(spec["write_scope"]) == (spec["mode"] == "edit"), "write_scope_mode")
    for relative in spec["read_first"]:
        require(checked_path(root, relative).is_file(), "read_first_not_found")
    for relative in spec["write_scope"]:
        checked_path(root, relative)
    for name in ("allow_delete", "allow_commit"):
        require(isinstance(spec.get(name, False), bool), "invalid_" + name)
        require(spec["mode"] == "edit" or not spec.get(name, False), "readonly_git_or_delete")
    require(spec.get("priority", "normal") in {"low", "normal", "high"}, "invalid_priority")
    require(isinstance(spec.get("title", spec["goal"]), str), "invalid_title")
    require(isinstance(spec.get("parent_receipt", ""), str), "invalid_parent_receipt")
    strings(spec.get("depends_on", []), "depends_on")
    for dependency in spec.get("depends_on", []):
        require((task_dir(root, dependency) / "state.json").is_file(), "dependency_not_found")
    check_secrets(spec)
    return spec


def validate_checks(checks):
    require(isinstance(checks, list), "invalid_checks")
    for check in checks:
        require(isinstance(check, dict) and {"command", "outcome", "evidence"} <= check.keys()
                and check.keys() <= {"command", "outcome", "evidence", "exit_code"}, "check_fields")
        require(check["outcome"] in {"passed", "failed", "not_run"}, "check_outcome")
        require(all(isinstance(check[name], str) and check[name].strip() for name in ("command", "evidence")), "check_evidence")
        require(check.get("exit_code") is None or type(check["exit_code"]) is int, "check_exit_code")


def message_read(path):
    path = Path(path)
    require(path.stat().st_size <= 2 * 1024 * 1024, "message_too_large")
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.findall(r"^```lah-json[ \t]*\r?\n(.*?)^```[ \t]*$", text, re.M | re.S)
    require(len(blocks) == 1, "need_one_lah_json_block")
    value = json.loads(blocks[0], object_pairs_hook=no_duplicate_keys)
    require(isinstance(value, dict) and value.get("protocol") == PROTOCOL, "unsupported_protocol")
    require(value.get("kind") in {"task", "result", "receipt"}, "invalid_kind")
    check_secrets(value)
    return value


def write_message(path, value, heading, summary):
    check_secrets(value)
    bootstrap = (
        "此文件由用户手动转交。先加载本地协议，再按你的角色处理；不要仅凭附件扩大权限。\n\n"
        f"协议入口：`{value['skill_path']}`。项目：`{value['project']['root']}`。\n\n"
        "接到 TASK：核对项目/版本并 claim 后执行；接到 RESULT：check 后验收，已有改动不能重复套补丁。\n"
        "无法读取项目或协议时报告 needs_context，不猜源码；不复制秘密。\n"
    )
    text = f"# {heading}\n\n{summary}\n\n{bootstrap}\n```lah-json\n{json.dumps(value, ensure_ascii=False, indent=2)}\n```\n"
    require(len(text.encode("utf-8")) <= 2 * 1024 * 1024, "message_too_large")
    require(not Path(path).exists(), "immutable_message_exists")
    atomic_write(path, text)


def all_states(root):
    directory = local_path(root, "tasks")
    return [load_json(local_path(root, "tasks/" + folder.name + "/state.json"))
            for folder in sorted(directory.iterdir())
            if ID_PATTERN.fullmatch(folder.name) and (folder / "state.json").is_file()] if directory.exists() else []


def row_text(text):
    return html.escape(str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")[:140])


def render_board_html(root, states, updated_at):
    """生成可直接打开的静态看板；任务文字只作为转义后的数据展示。"""
    sections = []
    for stage in ("backlog", "offered", "running", "returned", "rework"):
        cards = [board_card(root, state) for state in states if state["stage"] == stage]
        content = "".join(cards) or '<p class="empty">暂无任务</p>'
        sections.append(f'<section class="column"><h2>{STAGES[stage]} <span>{len(cards)}</span></h2>{content}</section>')
    closed = [state for state in states if state["stage"] in CLOSED]
    recent = "".join(board_card(root, state) for state in closed[:30]) or '<p class="empty">尚无结束的任务</p>'
    occupied = active(root)
    owner = html.escape(occupied["owner"]) if occupied else "当前没有交接占用"
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>本地 Agent 任务看板</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f9;color:#18253c;font:15px/1.6 system-ui,'Microsoft YaHei',sans-serif}
main{max-width:1700px;margin:auto;padding:28px}header{margin-bottom:24px}h1{font-size:28px;margin:0 0 8px}
.sub,.empty{color:#5b6a80}.owner{display:inline-block;background:#dde7ff;padding:4px 12px;border-radius:8px}
.board{display:grid;grid-template-columns:repeat(5,minmax(230px,1fr));gap:16px;overflow:auto;padding-bottom:12px}
.column{background:#e8edf5;padding:14px;border-radius:12px;min-height:200px}h2{font-size:17px;margin:0 0 14px}
h2 span{font-size:13px;color:#61738e}.card{background:white;border:1px solid #d7dfea;border-radius:10px;padding:14px;margin-bottom:12px;overflow-wrap:anywhere}
.card h3{font-size:16px;margin:0 0 8px}.id{font:11px/1.5 ui-monospace,monospace;color:#69778d}
.meta{font-size:13px;color:#476078}.note{font-size:13px;border-top:1px solid #e7ebf0;padding-top:8px;margin-top:8px;white-space:pre-wrap}
a{color:#2159b0;margin-right:10px}.history{margin-top:28px}.history-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
footer{margin-top:24px;color:#65758c;font-size:13px}@media(max-width:600px){main{padding:16px}.board{grid-template-columns:repeat(5,260px)}}
</style></head><body><main><header><h1>本地 Agent 任务看板</h1>
<div class="sub">共同读取本地记录 · 用户只转交文件 · 执行完成后仍需验收</div>
""" + f'<p class="owner">当前占用 / 负责方：{owner}</p>' + """<p class="sub">对任意 Agent 说“登记一项待办”或“把这项任务交给另一个 Agent”。看板由脚本维护，刷新页面即可看到最新状态。</p>
</header><div class="board">""" + "".join(sections) + f'</div><section class="history"><h2>最近结束的任务 · 共 {len(closed)} 项</h2><div class="history-grid">{recent}</div></section>' + f'<footer>更新时间（UTC）：{html.escape(updated_at)} · <a href="BOARD.md">文字版</a><a href="BOARD.json">完整索引</a> · 历史记录不会因关闭 GUI 丢失；这是静态本地页面，不启动网络服务。</footer></main></body></html>'


def board_card(root, state):
    task_id = state["task_id"]
    links = [f'<a href="tasks/{task_id}/state.json">状态</a>']
    for name, label in (("TASK.md", "任务书"), ("RESULT.md", "结果"), ("RECEIPT.md", "验收")):
        if (task_dir(root, task_id) / name).exists():
            links.append(f'<a href="tasks/{task_id}/{name}">{label}</a>')
    blocked = [dependency for dependency in state["spec"].get("depends_on", [])
               if load_json(task_dir(root, dependency) / "state.json")["stage"] != "accepted"]
    note = state.get("note", "") + ("\n前置任务未验收：" + ", ".join(blocked) if blocked else "")
    return (f'<article class="card"><h3>{html.escape(state["title"])}</h3>'
            f'<div class="meta">{STAGES[state["stage"]]} · {state["priority"]}</div>'
            f'<div class="meta">{html.escape(state["sender"])} → {html.escape(state["receiver"])}</div>'
            f'<p class="id">{task_id}</p><div>{"".join(links)}</div>'
            f'<div class="note">{html.escape(note)}</div></article>')


def refresh_board(root):
    """看板是可重建视图；单任务 state 和不可变消息才是历史来源。"""
    states = sorted(all_states(root), key=lambda item: item["updated_at"], reverse=True)
    view = {"protocol": PROTOCOL, "updated_at": now(), "active": active(root), "tasks": [
        {name: state[name] for name in ("task_id", "stage", "title", "sender", "receiver", "priority", "updated_at")}
        for state in states
    ]}
    save_json(local_path(root, "BOARD.json"), view)
    lines = ["# 本地 Agent 任务看板", "", f"更新时间（UTC）：{view['updated_at']}", "",
             "由交接脚本维护；无需用户编辑。执行方返回后进入待验收，派单方验收后才算完成。", "",
             "## 当前与未完成任务", "", "| 任务 | 状态 | 派单 → 执行 | 优先级 | 文件 |", "|---|---|---|---|---|"]
    def append_rows(items):
        for state in items:
            task_id = state["task_id"]
            base = "tasks/" + task_id + "/"
            links = [f"[状态]({base}state.json)"]
            for name, label in (("TASK.md", "任务"), ("RESULT.md", "结果"), ("RECEIPT.md", "验收")):
                if (task_dir(root, task_id) / name).exists():
                    links.append(f"[{label}]({base}{name})")
            lines.append(f"| {row_text(state['title'])}<br>{task_id} | {STAGES[state['stage']]} | "
                         f"{row_text(state['sender'])} → {row_text(state['receiver'])} | {state['priority']} | {' · '.join(links)} |")
            if state.get("note"):
                lines.append(f"| ↳ {row_text(state['note'])} | | | | |")
            dependencies = state["spec"].get("depends_on", [])
            blocked_by = [dependency for dependency in dependencies
                          if load_json(task_dir(root, dependency) / "state.json")["stage"] != "accepted"]
            if blocked_by:
                lines.append(f"| ↳ 前置任务尚未验收：{', '.join(blocked_by)} | | | | |")
    append_rows([state for state in states if state["stage"] not in CLOSED])
    closed = [state for state in states if state["stage"] in CLOSED]
    lines += ["", f"## 最近结束的任务（显示最多 30 项，共 {len(closed)} 项）", "",
              "| 任务 | 状态 | 派单 → 执行 | 优先级 | 文件 |", "|---|---|---|---|---|"]
    append_rows(closed[:30])
    lines += ["", "完整索引见 [BOARD.json](BOARD.json)。历史文件一直保留，新会话按任务 ID 读取，不必加载整个历史。", ""]
    atomic_write(local_path(root, "BOARD.md"), "\n".join(lines))
    atomic_write(local_path(root, "BOARD.html"), render_board_html(root, states, view["updated_at"]))
    return str(local_path(root, "BOARD.md"))


def transition(root, state, stage, note):
    state.update(stage=stage, updated_at=now(), note=note)
    state["history"].append({"at": state["updated_at"], "stage": stage, "note": note})
    save_json(task_dir(root, state["task_id"]) / "state.json", state)


def add_task(root, spec):
    validate_spec(spec, root)
    task_id = "t-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:12]
    state = {"task_id": task_id, "stage": "backlog", "title": spec.get("title", spec["goal"][:80]),
             "sender": spec["sender"], "receiver": spec["receiver"], "priority": spec.get("priority", "normal"),
             "created_at": now(), "updated_at": now(), "spec": spec, "history": [], "revision": 1}
    save_json(task_dir(root, task_id) / "spec-0001.json", spec)
    transition(root, state, "backlog", "待办已登记，尚未派单与获取代码快照。")
    return state


def dispatch(root, state):
    require(active(root) is None, "active_task")
    require(state["stage"] == "backlog", "task_not_backlog")
    spec = validate_spec(state["spec"], root)
    for dependency in spec.get("depends_on", []):
        dep = load_json(task_dir(root, dependency) / "state.json")
        require(dep["stage"] == "accepted", "dependency_not_accepted")
    project = project_info(root)
    value = {"protocol": PROTOCOL, "kind": "task", "task_id": state["task_id"],
             "message_id": "m-" + uuid.uuid4().hex, "created_at": now(), "skill_path": str(SKILL / "SKILL.md"),
             "project": project, "snapshot": snapshot(root, project), **spec}
    path = task_dir(root, state["task_id"]) / "TASK.md"
    write_message(path, value, "交接任务：" + state["title"], spec["goal"])
    save_json(local_path(root, "ACTIVE.json"), {"task_id": state["task_id"], "phase": "offered", "owner": spec["receiver"]})
    transition(root, state, "offered", "请转交 TASK.md；派单方暂停修改此工作副本。")
    return {"task_id": state["task_id"], "task": str(path)}


def match_task(root, path):
    value = message_read(path)
    require(value["kind"] == "task", "expected_task")
    folder = task_dir(root, value["task_id"])
    stored = message_read(folder / "TASK.md")
    require(value == stored, "task_not_matching_archive")
    require(value["project"] == project_info(root), "wrong_project")
    return value, load_json(folder / "state.json")


def in_scope(path, scopes):
    # Windows 不区分路径大小写；Unix 保留区分，避免同一协议扩大范围。
    compare = os.path.normcase
    path = compare(path)
    return any(path.startswith(compare(scope)) if scope.endswith("/") else path == compare(scope) for scope in scopes)


def changes(task, end_snapshot):
    before, after = task["snapshot"]["files"], end_snapshot["files"]
    output = []
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) != after.get(path):
            require(in_scope(path, task["write_scope"]), "out_of_scope_change")
            require(path in after or task.get("allow_delete", False), "delete_not_authorized")
            output.append({"path": path, "operation": "create" if path not in before else "delete" if path not in after else "modify",
                           "before": before.get(path), "after": after.get(path)})
    require(task.get("allow_commit", False) or task["snapshot"]["git_head"] == end_snapshot["git_head"], "git_head_changed")
    return output


def claim(root, path, actor):
    task, state = match_task(root, path)
    require(actor == task["receiver"], "wrong_actor")
    require(state["stage"] == "offered", "already_claimed_or_closed")
    require(active(root) and active(root)["task_id"] == task["task_id"], "active_task_mismatch")
    require(active(root)["phase"] == "offered", "phase_mismatch_use_recover")
    require(snapshot(root, project_info(root)) == task["snapshot"], "stale_snapshot")
    save_json(local_path(root, "ACTIVE.json"), {"task_id": task["task_id"], "phase": "running", "owner": actor})
    transition(root, state, "running", "执行方已核对版本并领取；尚无结果。")
    return {"task_id": task["task_id"], "stage": "running", "write_scope": task["write_scope"]}


def finish(root, path, actor, report):
    task, state = match_task(root, path)
    require(actor == task["receiver"] and state["stage"] == "running", "not_running_receiver")
    require(active(root) and active(root)["task_id"] == task["task_id"], "active_task_mismatch")
    require(active(root)["phase"] == "running", "phase_mismatch_use_recover")
    require(isinstance(report, dict) and set(report) == {"status", "summary", "decisions", "checks", "remaining", "context_requests"}, "report_fields")
    require(report["status"] in {"completed", "partial", "blocked", "needs_context", "proposed"}, "invalid_result_status")
    require(isinstance(report["summary"], str) and report["summary"].strip(), "empty_summary")
    for field in ("decisions", "remaining", "context_requests"):
        strings(report[field], field)
    require(report["status"] != "needs_context" or report["context_requests"], "missing_context_requests")
    require(task["mode"] != "plan" or report["status"] in {"proposed", "blocked", "needs_context"}, "plan_not_execution")
    validate_checks(report["checks"])
    check_secrets(report)
    end = snapshot(root, project_info(root))
    difference = changes(task, end)
    value = {"protocol": PROTOCOL, "kind": "result", "task_id": task["task_id"], "message_id": "m-" + uuid.uuid4().hex,
             "in_reply_to": task["message_id"], "based_on_snapshot": task["snapshot"]["id"],
             "created_at": now(), "skill_path": task["skill_path"], "project": task["project"],
             "sender": actor, "receiver": task["sender"], "changed_files": difference, "snapshot": end, **report}
    result_path = task_dir(root, task["task_id"]) / "RESULT.md"
    write_message(result_path, value, "交接结果：" + state["title"], report["summary"])
    save_json(local_path(root, "ACTIVE.json"), {"task_id": task["task_id"], "phase": "returned", "owner": task["sender"]})
    transition(root, state, "returned", report["status"] + "：" + report["summary"])
    return {"result": str(result_path), "stage": "returned", "changes": len(difference)}


def check_result(root, path):
    value = message_read(path)
    require(value["kind"] == "result", "expected_result")
    folder = task_dir(root, value["task_id"])
    task, state = match_task(root, folder / "TASK.md")
    require(value == message_read(folder / "RESULT.md"), "result_not_matching_archive")
    require(value["in_reply_to"] == task["message_id"] and value["based_on_snapshot"] == task["snapshot"]["id"], "wrong_parent_or_snapshot")
    require(value["project"] == task["project"] and value["sender"] == task["receiver"] and value["receiver"] == task["sender"], "wrong_result_identity")
    require(value["changed_files"] == changes(task, value["snapshot"]), "wrong_change_manifest")
    if (folder / "RECEIPT.md").exists():
        return value, state, {"already_processed": True, "receipt": str(folder / "RECEIPT.md")}
    require(state["stage"] == "returned", "not_returned")
    require(active(root) and active(root)["task_id"] == task["task_id"], "active_task_mismatch")
    require(active(root)["phase"] == "returned", "phase_mismatch_use_recover")
    require(snapshot(root, project_info(root)) == value["snapshot"], "files_changed_after_result")
    return value, state, {"structural_check": "passed", "semantic_acceptance": "not_checked", "task_id": value["task_id"]}


def accept(root, path, review):
    value, state, checked = check_result(root, path)
    if checked.get("already_processed"):
        return checked
    require(isinstance(review, dict) and set(review) == {"decision", "summary", "checks"}, "review_fields")
    require(review["decision"] in {"accepted", "rework", "rejected"}, "invalid_review_decision")
    require(review["decision"] != "accepted" or value["status"] in {"completed", "proposed"}, "incomplete_result_not_accepted")
    require(isinstance(review["summary"], str) and review["summary"].strip(), "empty_review_summary")
    validate_checks(review["checks"])
    require(review["checks"] and any(item["outcome"] == "passed" for item in review["checks"])
            or review["decision"] != "accepted", "acceptance_requires_evidence")
    check_secrets(review)
    receipt = {"protocol": PROTOCOL, "kind": "receipt", "task_id": value["task_id"], "message_id": "m-" + uuid.uuid4().hex,
               "in_reply_to": value["message_id"], "created_at": now(), "skill_path": value["skill_path"],
               "project": value["project"], "sender": value["receiver"], "receiver": value["sender"], **review}
    receipt_path = task_dir(root, value["task_id"]) / "RECEIPT.md"
    write_message(receipt_path, receipt, "交接验收：" + state["title"], review["summary"])
    transition(root, state, review["decision"], review["summary"])
    local_path(root, "ACTIVE.json").unlink()
    return {"decision": review["decision"], "receipt": str(receipt_path)}


def recover(root, task_id, note):
    """根据已有不可变消息修复中断的阶段，不修改源码或重复执行任务。"""
    require(isinstance(note, str) and note.strip(), "recovery_needs_stop_evidence")
    check_secrets(note)
    folder = task_dir(root, task_id)
    state = load_json(folder / "state.json")
    require(state["stage"] != "cancelled", "cancelled_task_not_resumable")
    occupied = active(root)
    require(occupied is None or occupied["task_id"] == task_id, "another_task_active")
    if not (folder / "TASK.md").exists():
        require(state["stage"] == "backlog" and occupied is None, "inconsistent_archive")
        return {"task_id": task_id, "stage": "backlog", "note": "没有执行任务，无需恢复。"}
    task, state = match_task(root, folder / "TASK.md")
    if (folder / "RECEIPT.md").exists():
        receipt = message_read(folder / "RECEIPT.md")
        result = message_read(folder / "RESULT.md")
        require(receipt["task_id"] == task_id and receipt["in_reply_to"] == result["message_id"]
                and receipt["project"] == task["project"], "inconsistent_receipt")
        transition(root, state, receipt["decision"], "恢复已有验收：" + note)
        if occupied:
            local_path(root, "ACTIVE.json").unlink()
    elif (folder / "RESULT.md").exists():
        result = message_read(folder / "RESULT.md")
        require(result["task_id"] == task_id and result["in_reply_to"] == task["message_id"]
                and result["based_on_snapshot"] == task["snapshot"]["id"] and result["project"] == task["project"], "inconsistent_result")
        require(result["changed_files"] == changes(task, result["snapshot"]), "wrong_change_manifest")
        require(snapshot(root, project_info(root)) == result["snapshot"], "files_changed_after_result")
        save_json(local_path(root, "ACTIVE.json"), {"task_id": task_id, "phase": "returned", "owner": task["sender"]})
        transition(root, state, "returned", "恢复已有结果，等待验收：" + note)
    else:
        require(state["stage"] in {"backlog", "offered", "running"}, "closed_task_not_resumable")
        current = snapshot(root, project_info(root))
        running = state["stage"] == "running" or (occupied and occupied["phase"] == "running")
        if running:
            changes(task, current)
        else:
            require(current == task["snapshot"], "stale_snapshot")
        phase = "running" if running else "offered"
        save_json(local_path(root, "ACTIVE.json"), {"task_id": task_id, "phase": phase, "owner": task["receiver"]})
        transition(root, state, phase, "恢复原任务，不重复领取或执行：" + note)
    return {"task_id": task_id, "stage": state["stage"], "source_files_modified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="LAH/1.0 任务交接与持久看板（无网络、无源码写入）")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("add", "revise", "pack", "claim", "finish", "accept", "cancel", "recover", "check", "status", "board"):
        sub = commands.add_parser(command)
        sub.add_argument("--project", required=True)
        if command in {"add", "revise"}:
            sub.add_argument("--spec", required=True)
        if command in {"revise", "status", "recover"}:
            sub.add_argument("--task-id", required=command != "status")
        if command == "pack":
            group = sub.add_mutually_exclusive_group(required=True)
            group.add_argument("--spec")
            group.add_argument("--task-id")
        if command in {"claim", "finish"}:
            sub.add_argument("--task", required=True)
        if command == "cancel":
            group = sub.add_mutually_exclusive_group(required=True)
            group.add_argument("--task")
            group.add_argument("--task-id")
        if command in {"claim", "finish"}:
            sub.add_argument("--actor", required=True)
        if command == "finish":
            sub.add_argument("--report", required=True)
        if command == "accept":
            sub.add_argument("--result", required=True)
            sub.add_argument("--review", required=True)
        if command == "check":
            sub.add_argument("--message", required=True)
        if command in {"cancel", "recover"}:
            sub.add_argument("--note", required=True)
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("message")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        value = message_read(args.message)
        return {name: value.get(name) for name in ("protocol", "kind", "task_id", "message_id", "sender", "receiver", "status", "goal", "project")}
    root = Path(args.project).resolve()
    if args.command == "status":
        if args.task_id:
            return load_json(task_dir(root, args.task_id) / "state.json")
        return {"active": active(root), "board": str(local_path(root, "BOARD.md")), "tasks": [
            {name: state[name] for name in ("task_id", "stage", "title", "receiver")}
            for state in all_states(root) if state["stage"] not in CLOSED]}
    if args.command == "check":
        return check_result(root, args.message)[2]
    with transaction(root, stopped_note=args.note if args.command == "recover" else None):
        if args.command == "add":
            state = add_task(root, load_json(args.spec))
            result = {"task_id": state["task_id"], "stage": state["stage"]}
        elif args.command == "revise":
            state = load_json(task_dir(root, args.task_id) / "state.json")
            require(state["stage"] == "backlog", "only_revise_backlog")
            spec = validate_spec(load_json(args.spec), root)
            require(args.task_id not in spec.get("depends_on", []), "self_dependency")
            # 已有任务依赖只允许无环图；变更依赖时检查可达性。
            stack, visited = list(spec.get("depends_on", [])), set()
            while stack:
                dep = stack.pop()
                require(dep != args.task_id, "dependency_cycle")
                if dep not in visited:
                    visited.add(dep)
                    stack.extend(load_json(task_dir(root, dep) / "state.json")["spec"].get("depends_on", []))
            state["revision"] += 1
            save_json(task_dir(root, args.task_id) / f"spec-{state['revision']:04d}.json", spec)
            state.update(spec=spec, title=spec.get("title", spec["goal"][:80]), sender=spec["sender"], receiver=spec["receiver"], priority=spec.get("priority", "normal"))
            transition(root, state, "backlog", "待办规格已修订；旧版本保留。")
            result = {"task_id": args.task_id, "revision": state["revision"]}
        elif args.command == "pack":
            require(active(root) is None, "active_task")
            state = add_task(root, load_json(args.spec)) if args.spec else load_json(task_dir(root, args.task_id) / "state.json")
            result = dispatch(root, state)
        elif args.command == "claim":
            result = claim(root, args.task, args.actor)
        elif args.command == "finish":
            result = finish(root, args.task, args.actor, load_json(args.report))
        elif args.command == "accept":
            result = accept(root, args.result, load_json(args.review))
        elif args.command == "cancel":
            if args.task:
                task, state = match_task(root, args.task)
            else:
                state = load_json(task_dir(root, args.task_id) / "state.json")
            require(state["stage"] in {"backlog", "offered", "running", "returned", "rework"}, "not_active_stage")
            if state["stage"] not in {"backlog", "rework"}:
                require(active(root) and active(root)["task_id"] == state["task_id"], "active_task_mismatch")
            require(args.note.strip(), "cancellation_needs_reason_and_stop_evidence")
            check_secrets(args.note)
            was_active = state["stage"] not in {"backlog", "rework"}
            transition(root, state, "cancelled", args.note)
            if was_active:
                local_path(root, "ACTIVE.json").unlink()
            result = {"task_id": state["task_id"], "stage": "cancelled", "files_rolled_back": False}
        elif args.command == "recover":
            result = recover(root, args.task_id, args.note)
        else:
            result = {}
        result["board"] = refresh_board(root)
        return result


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (ProtocolError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # 不回显文件原文、完整异常或 Key；不把失败写成成功。
        code = str(error) if isinstance(error, ProtocolError) else type(error).__name__
        print(json.dumps({"ok": False, "error": code}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
