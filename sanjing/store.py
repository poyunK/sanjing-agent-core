"""SQLite 就是后端记忆；所有用户查询都带 session_id，不执行模型给的 SQL。"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .privacy import redact


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def encode(value):
    return json.dumps(redact(value), ensure_ascii=False, allow_nan=False)


@contextmanager
def connect(db_path):
    """每次调用打开连接，避免把数据库连接混入模型参数。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        # SQLite 的 with 只负责事务，不会自动 close；Windows 必须明确释放文件。
        conn.close()


def init_db(db_path):
    with connect(db_path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            created_at TEXT NOT NULL, status TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
            error_code TEXT, usage TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            turn_id TEXT NOT NULL REFERENCES turns(id), role TEXT NOT NULL, content TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            turn_id TEXT NOT NULL REFERENCES turns(id), tool_name TEXT NOT NULL,
            call_id TEXT NOT NULL, status TEXT NOT NULL, error_code TEXT, duration_ms REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evidence (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            turn_id TEXT NOT NULL REFERENCES turns(id), kind TEXT NOT NULL,
            data TEXT NOT NULL, event_id TEXT REFERENCES events(id)
        );
        CREATE TABLE IF NOT EXISTS recommendations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            turn_id TEXT NOT NULL REFERENCES turns(id), data TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS message_session ON messages(session_id,seq);
        CREATE INDEX IF NOT EXISTS evidence_session ON evidence(session_id,seq);
        """)


def ensure_session(db_path, session_id=None):
    """不传 ID 时创建；传入已有 ID 才恢复，拼错 ID 不会生成空会话。"""
    init_db(db_path)
    if session_id:
        with connect(db_path) as conn:
            if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError("会话不存在，请用 /session 查看 ID")
        return session_id
    session_id = new_id("session")
    with connect(db_path) as conn:
        conn.execute("INSERT INTO sessions VALUES (?,?)", (session_id, now()))
    return session_id


def start_turn(db_path, session_id):
    turn_id = new_id("turn")
    with connect(db_path) as conn:
        conn.execute("INSERT INTO turns(id,session_id,created_at,status) VALUES (?,?,?,?)",
                     (turn_id, session_id, now(), "running"))
    return turn_id


def finish_turn(db_path, session_id, turn_id, status, answer, error_code=None, usage=None):
    with connect(db_path) as conn:
        conn.execute("UPDATE turns SET status=?,answer=?,error_code=?,usage=? WHERE id=? AND session_id=?",
                     (status, redact(answer), error_code, encode(usage or {}), turn_id, session_id))


def add_message(db_path, session_id, turn_id, role, content):
    if role not in {"user", "assistant"}:
        raise ValueError("业务对话仅存用户原话和发布的回复；工具记录使用 events")
    message_id = new_id("msg")
    with connect(db_path) as conn:
        conn.execute("INSERT INTO messages(id,session_id,turn_id,role,content) VALUES (?,?,?,?,?)",
                     (message_id, session_id, turn_id, role, redact(content)))
    return message_id


def get_message(db_path, session_id, message_id):
    return _get(db_path, "messages", session_id, message_id)


def get_turn(db_path, session_id, turn_id):
    return _get(db_path, "turns", session_id, turn_id)


def _get(db_path, table, session_id, record_id):
    # table 只来自本模块常量，不接受模型输入。
    with connect(db_path) as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND session_id=?",
                           (record_id, session_id)).fetchone()
    return _decode(row) if row else None


def _decode(row):
    result = dict(row)
    for field in ("data", "usage"):
        if field in result:
            result[field] = json.loads(result[field])
    return result


def list_messages(db_path, session_id):
    return _list(db_path, "messages", session_id)


def list_turns(db_path, session_id):
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM turns WHERE session_id=? ORDER BY rowid", (session_id,)).fetchall()
    return [_decode(row) for row in rows]


def _list(db_path, table, session_id, turn_id=None):
    query = f"SELECT * FROM {table} WHERE session_id=?"
    args = [session_id]
    if turn_id:
        query += " AND turn_id=?"
        args.append(turn_id)
    query += " ORDER BY seq"
    with connect(db_path) as conn:
        return [_decode(row) for row in conn.execute(query, args).fetchall()]


def add_event(db_path, session_id, turn_id, tool_name, call_id, status, error_code=None, duration_ms=0):
    event_id = new_id("event")
    with connect(db_path) as conn:
        conn.execute("INSERT INTO events(id,session_id,turn_id,tool_name,call_id,status,error_code,duration_ms) VALUES (?,?,?,?,?,?,?,?)",
                     (event_id, session_id, turn_id, redact(tool_name), redact(call_id), status, error_code, duration_ms))
    return event_id


def update_event(db_path, event_id, status, error_code=None, duration_ms=0):
    with connect(db_path) as conn:
        conn.execute("UPDATE events SET status=?,error_code=?,duration_ms=? WHERE id=?",
                     (status, error_code, duration_ms, event_id))


def get_event(db_path, session_id, event_id):
    return _get(db_path, "events", session_id, event_id)


def add_evidence(db_path, session_id, turn_id, kind, data, event_id=None):
    evidence_id = new_id("ev")
    with connect(db_path) as conn:
        conn.execute("INSERT INTO evidence(id,session_id,turn_id,kind,data,event_id) VALUES (?,?,?,?,?,?)",
                     (evidence_id, session_id, turn_id, kind, encode(data), event_id))
    return evidence_id


def get_evidence(db_path, session_id, evidence_id):
    return _get(db_path, "evidence", session_id, evidence_id)


def list_evidence(db_path, session_id, turn_id=None):
    return _list(db_path, "evidence", session_id, turn_id)


def add_recommendation(db_path, session_id, turn_id, data):
    recommendation_id = new_id("rec")
    with connect(db_path) as conn:
        # 一个批次部分失败后模型可能重交成功项；同轮同计算只保留一条建议。
        calculation_id = data.get("calculation_id")
        if data.get("kind") == "career_decision":
            encoded = encode(data)
            previous = conn.execute("SELECT id,data FROM recommendations WHERE session_id=? AND turn_id=?",
                                    (session_id, turn_id)).fetchall()
            for row in previous:
                if row["data"] == encoded:
                    return row["id"]
        if calculation_id:
            existing = conn.execute("SELECT id,data FROM recommendations WHERE session_id=? AND turn_id=?",
                                    (session_id, turn_id)).fetchall()
            for row in existing:
                if json.loads(row["data"]).get("calculation_id") == calculation_id:
                    return row["id"]
        conn.execute("INSERT INTO recommendations(id,session_id,turn_id,data) VALUES (?,?,?,?)",
                     (recommendation_id, session_id, turn_id, encode(data)))
    return recommendation_id


def get_recommendation(db_path, session_id, recommendation_id):
    record = _get(db_path, "recommendations", session_id, recommendation_id)
    if record and get_turn(db_path, session_id, record["turn_id"])["status"] == "completed":
        return record
    return None


def list_recommendations(db_path, session_id, turn_id=None):
    return _list(db_path, "recommendations", session_id, turn_id)


def snapshot(db_path, session_id, turn_id):
    """每轮快照只取本轮建议及采用的历史依据；不会夹带别的会话。"""
    turn = get_turn(db_path, session_id, turn_id)
    if not turn:
        raise ValueError("账本不存在")
    evidence = list_evidence(db_path, session_id, turn_id)
    recommendations = list_recommendations(db_path, session_id, turn_id)
    known = {item["id"] for item in evidence}
    for recommendation in recommendations:
        for record_id in recommendation["data"].get("evidence_ids", []):
            if record_id not in known:
                record = get_evidence(db_path, session_id, record_id)
                if record:
                    evidence.append(record)
                    known.add(record_id)
    return {"session_id": session_id, "turn": turn,
            "recommendations": recommendations, "evidence": evidence,
            "events": _list(db_path, "events", session_id, turn_id),
            "messages": _list(db_path, "messages", session_id, turn_id)}
