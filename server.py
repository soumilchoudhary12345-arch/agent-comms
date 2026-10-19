#!/usr/bin/env python3
"""Local, persistent MCP message bus shared by coding-agent CLIs."""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


SERVER_NAME = "agent-comms"
SERVER_VERSION = "1.0.0"
MAX_MESSAGE_LENGTH = 20_000
MAX_PURPOSE_LENGTH = 2_000
TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")

TOOLS = [
    {
        "name": "open_session",
        "description": (
            "Join or create a persistent peer discussion for a task. Use the same "
            "short, lowercase topic slug across Copilot CLI, OpenCode, and Antigravity CLI. "
            "All agents are equal peers; messages are ordered by time, with no priority."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Shared task slug, e.g. mobile-auth"},
                "agent": {"type": "string", "description": "Your agent name"},
                "purpose": {"type": "string", "description": "Short task summary"},
            },
            "required": ["topic", "agent", "purpose"],
        },
    },
    {
        "name": "list_sessions",
        "description": "List recent shared peer discussion sessions and their participants.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "send_message",
        "description": (
            "Post a message visible to every participant in the shared task session. "
            "Use this to confer directly with Copilot CLI, OpenCode, and Antigravity CLI "
            "during planning, implementation, debugging, and review; do not route peer "
            "discussion through the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "agent": {"type": "string"},
                "message": {"type": "string", "description": "Message to the peer agents"},
            },
            "required": ["topic", "agent", "message"],
        },
    },
    {
        "name": "read_messages",
        "description": (
            "Read messages in a shared task session in chronological order. Pass the "
            "last seen message_id as after_message_id to continue reading new messages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "agent": {"type": "string"},
                "after_message_id": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            "required": ["topic", "agent"],
        },
    },
]


class RequestError(Exception):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def database_path():
    configured = os.environ.get("AGENT_COMMS_DB")
    if configured:
        return Path(configured).expanduser()
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "agent-comms" / "messages.sqlite3"


def connect():
    path = database_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    os.chmod(path, 0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            topic TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_activity TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS participants (
            topic TEXT NOT NULL REFERENCES sessions(topic),
            agent TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (topic, agent)
        );
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL REFERENCES sessions(topic),
            agent TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_topic_id ON messages(topic, message_id);
        """
    )
    return connection


def required_text(args, name, maximum, pattern=None):
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RequestError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum:
        raise RequestError(f"{name} must be at most {maximum} characters")
    if pattern and not pattern.fullmatch(value):
        raise RequestError(f"{name} must be a lowercase slug using letters, digits, '.', '_' or '-'")
    return value


def number_arg(args, name, default, minimum, maximum):
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RequestError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def join_session(connection, topic, agent):
    now = timestamp()
    connection.execute(
        """INSERT INTO participants(topic, agent, joined_at, last_seen)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(topic, agent) DO UPDATE SET last_seen = excluded.last_seen""",
        (topic, agent, now, now),
    )


def call_tool(name, args):
    if not isinstance(args, dict):
        raise RequestError("tool arguments must be an object")
    connection = connect()
    try:
        if name == "open_session":
            topic = required_text(args, "topic", 80, TOPIC_PATTERN)
            agent = required_text(args, "agent", 60)
            purpose = required_text(args, "purpose", MAX_PURPOSE_LENGTH)
            now = timestamp()
            with connection:
                result = connection.execute(
                    """INSERT INTO sessions(topic, purpose, created_at, last_activity)
                       VALUES (?, ?, ?, ?) ON CONFLICT(topic) DO NOTHING""",
                    (topic, purpose, now, now),
                )
                join_session(connection, topic, agent)
                session = connection.execute(
                    "SELECT topic, purpose, created_at FROM sessions WHERE topic = ?", (topic,)
                ).fetchone()
                participants = connection.execute(
                    "SELECT agent FROM participants WHERE topic = ? ORDER BY joined_at, agent",
                    (topic,),
                ).fetchall()
            return {
                "topic": session["topic"],
                "purpose": session["purpose"],
                "created_at": session["created_at"],
                "created": result.rowcount == 1,
                "participants": [row["agent"] for row in participants],
            }

        if name == "list_sessions":
            limit = number_arg(args, "limit", 20, 1, 50)
            with connection:
                rows = connection.execute(
                    """SELECT s.topic, s.purpose, s.created_at, s.last_activity,
                              GROUP_CONCAT(p.agent, ', ') AS participants
                       FROM sessions s LEFT JOIN participants p ON p.topic = s.topic
                       GROUP BY s.topic
                       ORDER BY s.last_activity DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]

        if name == "send_message":
            topic = required_text(args, "topic", 80, TOPIC_PATTERN)
            agent = required_text(args, "agent", 60)
            message = required_text(args, "message", MAX_MESSAGE_LENGTH)
            now = timestamp()
            with connection:
                if not connection.execute("SELECT 1 FROM sessions WHERE topic = ?", (topic,)).fetchone():
                    raise RequestError(f"session '{topic}' does not exist; call open_session first")
                join_session(connection, topic, agent)
                cursor = connection.execute(
                    "INSERT INTO messages(topic, agent, message, created_at) VALUES (?, ?, ?, ?)",
                    (topic, agent, message, now),
                )
                connection.execute(
                    "UPDATE sessions SET last_activity = ? WHERE topic = ?", (now, topic)
                )
            return {"message_id": cursor.lastrowid, "topic": topic, "created_at": now}

        if name == "read_messages":
            topic = required_text(args, "topic", 80, TOPIC_PATTERN)
            agent = required_text(args, "agent", 60)
            after_id = number_arg(args, "after_message_id", 0, 0, 2**63 - 1)
            limit = number_arg(args, "limit", 50, 1, 100)
            with connection:
                if not connection.execute("SELECT 1 FROM sessions WHERE topic = ?", (topic,)).fetchone():
                    raise RequestError(f"session '{topic}' does not exist; call open_session first")
                join_session(connection, topic, agent)
                rows = connection.execute(
                    """SELECT message_id, agent, message, created_at FROM messages
                       WHERE topic = ? AND message_id > ? ORDER BY message_id LIMIT ?""",
                    (topic, after_id, limit),
                ).fetchall()
            messages = [dict(row) for row in rows]
            return {
                "topic": topic,
                "messages": messages,
                "next_after_message_id": messages[-1]["message_id"] if messages else after_id,
            }

        raise RequestError(f"unknown tool: {name}")
    finally:
        connection.close()


def send_response(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        send_response(
            {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32600, "message": "invalid JSON-RPC request"},
            }
        )
        return
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return
    if method == "initialize":
        params = request.get("params") or {}
        if not isinstance(params, dict):
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "initialize params must be an object"},
                }
            )
            return
        send_response(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        )
        return
    if method == "ping":
        send_response({"jsonrpc": "2.0", "id": request_id, "result": {}})
        return
    if method == "tools/list":
        send_response({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        params = request.get("params") or {}
        if not isinstance(params, dict):
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "tools/call params must be an object"},
                }
            )
            return
        try:
            result = call_tool(params.get("name"), params.get("arguments", {}))
            tool_result = {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": False,
            }
        except RequestError as exc:
            tool_result = {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
        send_response({"jsonrpc": "2.0", "id": request_id, "result": tool_result})
        return
    if request_id is not None:
        send_response(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        )


def main():
    os.umask(0o077)
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": str(exc)},
                }
            )
            continue
        try:
            handle(request)
        except RequestError as exc:
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32600, "message": str(exc)},
                }
            )
        except Exception as exc:
            print(f"{SERVER_NAME}: {exc}", file=sys.stderr, flush=True)
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": "internal server error"},
                }
            )


if __name__ == "__main__":
    main()
