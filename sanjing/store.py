"""SQLite 就是后端记忆；所有用户查询都带 session_id，不执行模型给的 SQL。"""

import json
import sqlite3
import uuid
import time
import hashlib
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
        CREATE INDEX IF NOT EXISTS evidence_kind ON evidence(session_id,kind,seq);
        CREATE INDEX IF NOT EXISTS messages_turn ON messages(session_id,turn_id,seq);
        CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS recommendations_session ON recommendations(session_id,seq);
        CREATE INDEX IF NOT EXISTS confirmed_replacements ON evidence(
          session_id,json_extract(data,'$.supersedes_evidence_id'))
          WHERE kind IN ('condition','user_condition') AND json_extract(data,'$.status')='自述';
        CREATE TABLE IF NOT EXISTS session_leases (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id),
            turn_id TEXT NOT NULL REFERENCES turns(id), expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS turn_requests (
            session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT NOT NULL,
            input_hash TEXT NOT NULL, turn_id TEXT NOT NULL REFERENCES turns(id), result TEXT,
            PRIMARY KEY(session_id,request_id)
        );
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


def begin_request(db_path, session_id, user_text, request_id=None, lease_seconds=390):
    """短事务领取本会话；网络等待不占数据库锁。重复请求只返回旧结果。

    request_id 来自网页/调用方，一次用户操作固定一个 ID。不同文字不能复用 ID。
    租约大于整轮期限加一次请求超时；过期只标记旧轮中断，不删除其依据。
    """
    input_hash = hashlib.sha256(user_text.encode("utf-8")).hexdigest()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if request_id is not None:
            previous = conn.execute("SELECT * FROM turn_requests WHERE session_id=? AND request_id=?",
                                    (session_id, request_id)).fetchone()
            if previous:
                if previous["input_hash"] != input_hash:
                    return {"state": "conflict"}
                if previous["result"]:
                    return {"state": "cached", "result": json.loads(previous["result"])}
                old_turn = conn.execute("SELECT status FROM turns WHERE id=?", (previous["turn_id"],)).fetchone()
                if old_turn["status"] != "running":
                    return {"state": "interrupted", "turn_id": previous["turn_id"]}
                alive = conn.execute("SELECT 1 FROM session_leases WHERE session_id=? AND turn_id=? AND expires_at>?",
                                     (session_id, previous['turn_id'], time.time())).fetchone()
                if not alive:
                    conn.execute("UPDATE turns SET status='incomplete',error_code='INTERRUPTED',answer=? WHERE id=?",
                                 ('上次执行中断，已保留账目；未重新调用收费服务。', previous['turn_id']))
                    conn.execute("DELETE FROM session_leases WHERE session_id=? AND turn_id=?",
                                 (session_id, previous['turn_id']))
                    return {'state': 'interrupted', 'turn_id': previous['turn_id']}
                # 即使租约过期，同一个 ID 也不重新收费；新操作必须给新 ID。
                return {"state": "busy", "turn_id": previous["turn_id"]}
        lease = conn.execute("SELECT * FROM session_leases WHERE session_id=?", (session_id,)).fetchone()
        timestamp = time.time()
        if lease and lease["expires_at"] > timestamp:
            return {"state": "busy", "turn_id": lease["turn_id"]}
        if lease:
            conn.execute("UPDATE turns SET status='incomplete',error_code='INTERRUPTED',answer=? WHERE id=? AND status='running'",
                         ("上次执行中断，已保留账目；未重新调用收费服务。", lease["turn_id"]))
            conn.execute("DELETE FROM session_leases WHERE session_id=?", (session_id,))
        turn_id = new_id("turn")
        conn.execute("INSERT INTO turns(id,session_id,created_at,status) VALUES (?,?,?,?)",
                     (turn_id, session_id, now(), "running"))
        conn.execute("INSERT INTO session_leases VALUES (?,?,?)", (session_id, turn_id, timestamp + lease_seconds))
        if request_id is not None:
            conn.execute("INSERT INTO turn_requests VALUES (?,?,?,?,NULL)",
                         (session_id, request_id, input_hash, turn_id))
        return {"state": "owned", "turn_id": turn_id}


def owns_lease(db_path, session_id, turn_id):
    with connect(db_path) as conn:
        return bool(conn.execute("SELECT 1 FROM session_leases WHERE session_id=? AND turn_id=? AND expires_at>?",
                                 (session_id, turn_id, time.time())).fetchone())


def complete_request(db_path, session_id, turn_id, result=None):
    """结果落盘后释放本轮租约；旧执行方不能释放新执行方的占用。"""
    with connect(db_path) as conn:
        if result is not None:
            conn.execute("""UPDATE turn_requests SET result=? WHERE session_id=? AND turn_id=?
              AND EXISTS (SELECT 1 FROM session_leases WHERE session_id=? AND turn_id=? AND expires_at>?)""",
                         (encode(result), session_id, turn_id, session_id, turn_id, time.time()))
        conn.execute("DELETE FROM session_leases WHERE session_id=? AND turn_id=?", (session_id, turn_id))


def recent_conditions(db_path, session_id, limit=30):
    """数据库先排除被明确替代的条件，再限量读取；不加载全部证据。"""
    with connect(db_path) as conn:
        rows = conn.execute("""SELECT e.* FROM evidence e WHERE e.session_id=?
          AND e.kind IN ('condition','user_condition') AND NOT EXISTS (
            SELECT 1 FROM evidence n WHERE n.session_id=e.session_id
            AND n.kind IN ('condition','user_condition')
            AND json_extract(n.data,'$.status')='自述'
            AND json_extract(n.data,'$.supersedes_evidence_id')=e.id)
          ORDER BY e.seq DESC LIMIT ?""", (session_id, limit)).fetchall()
    return [_decode(row) for row in reversed(rows)]


def recent_recommendation_ids(db_path, session_id, limit=5):
    with connect(db_path) as conn:
        rows = conn.execute("""SELECT r.id FROM recommendations r JOIN turns t ON t.id=r.turn_id
          WHERE r.session_id=? AND t.status='completed' ORDER BY r.seq DESC LIMIT ?""",
                            (session_id, limit)).fetchall()
    return [row["id"] for row in reversed(rows)]


def history_index(db_path, session_id, record_type, before_seq=None, limit=20):
    """仅返回本会话的分页定位编号。表名由枚举选择，SQL 和路径不由模型填写。"""
    table = {'evidence': 'evidence', 'recommendation': 'recommendations'}[record_type]
    query = f"SELECT r.seq,r.id,r.turn_id FROM {table} r JOIN turns t ON t.id=r.turn_id WHERE r.session_id=?"
    args = [session_id]
    if record_type == 'recommendation':
        query += " AND t.status='completed'"
    if before_seq is not None:
        query += " AND r.seq<?"
        args.append(before_seq)
    query += " ORDER BY r.seq DESC LIMIT ?"
    args.append(limit + 1)
    with connect(db_path) as conn:
        rows = conn.execute(query, args).fetchall()
    return {'items': [dict(row) for row in rows[:limit]],
            'next_before_seq': rows[limit-1]['seq'] if len(rows) > limit else None}


def recent_dialogue(db_path, session_id, exclude_turn_id, limit=40, max_chars=48000):
    """最多读取指定数量的完整回合；超大旧回合只取长度，不把正文载入内存。"""
    with connect(db_path) as conn:
        turns = conn.execute("""SELECT id FROM turns WHERE session_id=? AND id<>?
          AND status<>'running' AND EXISTS (
            SELECT 1 FROM messages m WHERE m.session_id=turns.session_id AND m.turn_id=turns.id AND m.role='assistant')
          ORDER BY rowid DESC LIMIT ?""", (session_id, exclude_turn_id, limit)).fetchall()
        groups, loaded_chars = [], 0
        for turn in turns:
            size = conn.execute("SELECT coalesce(sum(length(content)),0) FROM messages WHERE session_id=? AND turn_id=?",
                                (session_id, turn["id"])).fetchone()[0]
            if size + loaded_chars > max_chars:
                break
            rows = conn.execute("SELECT role,content FROM messages WHERE session_id=? AND turn_id=? ORDER BY seq",
                                (session_id, turn["id"])).fetchall()
            groups.append([dict(row) for row in rows])
            loaded_chars += size
    return groups


def finish_turn(db_path, session_id, turn_id, status, answer, error_code=None, usage=None, *, require_lease=False):
    """只允许 running 转为终态；发布时在同一个 UPDATE 中核对租约，避免旧执行方复活。"""
    with connect(db_path) as conn:
        query = "UPDATE turns SET status=?,answer=?,error_code=?,usage=? WHERE id=? AND session_id=? AND status='running'"
        args = [status, redact(answer), error_code, encode(usage or {}), turn_id, session_id]
        if require_lease:
            query += " AND EXISTS (SELECT 1 FROM session_leases WHERE session_id=? AND turn_id=? AND expires_at>?)"
            args.extend([session_id, turn_id, time.time()])
        cursor = conn.execute(query, args)
        return cursor.rowcount == 1


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
