import os

from app.leave_db import LeaveDb
from app.memory import RunStore

AGENT_DB  = os.environ.get("AGENT_DB",  "agent.db")
LEAVE_DB  = os.environ.get("LEAVE_DB",  "leave.db")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def open_stores() -> tuple[RunStore, LeaveDb]:
    store, db = RunStore(AGENT_DB), LeaveDb(LEAVE_DB)
    store.migrate()
    db.migrate()
    return store, db


def make_providers(mock: bool, slow: float = 0.0) -> dict:
    """One provider per agent. With OpenAI all three share one client; each keeps its own prompt."""
    if mock:
        from app.providers import demo_providers
        return demo_providers(slow)
    from app.providers import OpenAIProvider
    openai = OpenAIProvider(OPENAI_MODEL)
    return {"supervisor": openai, "calendar": openai, "desk": openai}
