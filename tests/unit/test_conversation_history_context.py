from uuid import uuid4

import pytest

from src.agent.conversation_history import ConversationHistoryService


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""
        self.args = ()

    async def fetch(self, query, *args):
        self.query, self.args = query, args
        return self.rows


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_context_query_reads_only_completed_turns_in_chronological_order():
    current_id = uuid4()
    session_id = uuid4()
    rows = [
        {"id": uuid4(), "sequence_number": 1, "execution_status": "success"},
        {"id": uuid4(), "sequence_number": 3, "execution_status": "success"},
    ]
    conn = _Connection(rows)
    service = ConversationHistoryService(_Pool(conn))

    result = await service.get_conversation_context(
        session_id=session_id, user_id="user-a", limit=5, exclude_query_id=current_id
    )

    assert result == rows
    assert "execution_status = 'success'" in conn.query
    assert "id != $3" in conn.query
    assert "ORDER BY sequence_number ASC" in conn.query
    assert conn.args == (session_id, "user-a", current_id, 5)
