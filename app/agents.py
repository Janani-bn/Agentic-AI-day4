"""Three agents for the Leave Request Assistant.

    staff ──▶ supervisor ──ask_calendar──▶ calendar agent  (list_holidays, get_leave_type)
                         └─ask_desk───────▶ desk agent      (get_staff, check_can_apply,
                                                              apply_leave*, notify_hod*)
                                                              * side effects: run once per key

Each specialist is an ordinary agent loop with its own system prompt and its own small tool set.
To the supervisor a specialist is just a tool: "agent as tool", the simplest multi-agent pattern.
"""
import time
from collections.abc import Callable

from app.idempotency import idempotency_key
from app.leave_db import LeaveDb
from app.providers import AgentError
from app.tools.leave_tools import CalendarTools, DeskTools, Toolset

SPECIALIST_MAX_STEPS = 6

SUPERVISOR_SYSTEM = """You are the Campus Leave Request Assistant, talking to the staff member \
with roll number {roll_no}.
You never check calendars or apply leave yourself. Delegate:
- ask_calendar for checking public holidays and looking up leave-type details;
- ask_desk for anything about this member's leave balance, applications, or notifications.
Give each specialist a complete, specific request.
Then answer the staff member briefly, using only what the specialists reported."""

CALENDAR_SYSTEM = """You are the calendar specialist of a leave management system. Look up \
public holidays and leave-type entitlements. Report facts concisely.
You cannot apply leave or change any data. Be brief."""

DESK_SYSTEM = """You are the desk specialist of a leave management system, acting for staff \
member {roll_no} only.
Always call check_can_apply before apply_leave. Never decide policy yourself — report the reasons \
the tools give. Confirm a successful application with notify_hod. Report what you did, briefly."""


def run_tool(toolset: Toolset, db: LeaveDb, key: str, name: str, args: dict) -> tuple[dict, bool]:
    """Run one tool call for any agent. Returns (result, replayed). Never raises, except AgentError.

    Side effects run at most once per key; replayed is True when the stored result was returned
    and nothing was done. Delegations hand the key down so the specialist's side effects get keys
    derived from it: a replayed delegation replays its side effects safely too.
    """
    try:
        if name in toolset.DELEGATES:
            return toolset.delegate(name, args, key), False
        if name in toolset.SIDE_EFFECTS:
            result, fresh = db.once(key, name, lambda: toolset.call(name, args))
            return result, not fresh
        return toolset.call(name, args), False
    except AgentError:
        raise
    except NotImplementedError:
        return {"error": "not_implemented", "hint": f"{name} is not available yet."}, False
    except Exception as e:
        return {"error": "tool_failed",
                "hint": f"{name} failed ({type(e).__name__}). Try another way or tell the member."}, False


def run_specialist(agent: str, system: str, toolset: Toolset, *, db: LeaveDb, provider,
                   task: str, parent_key: str,
                   on_step: Callable[[dict], None] | None = None) -> dict:
    """A specialist's whole agent loop, run inside one tool call of the supervisor."""
    contents = [{"role": "user", "text": task}]
    functions = list(toolset.functions().values())
    used = []
    seq = 0
    while seq < SPECIALIST_MAX_STEPS:
        turn = provider.generate(system, contents, functions)
        seq += 1
        if not turn.tool_calls:
            return {"agent": agent, "answer": turn.text or "", "tools_used": used}
        contents.append({"role": "model", "text": turn.text, "raw": turn.raw,
                         "tool_calls": [{"name": c.name, "args": c.args} for c in turn.tool_calls]})
        for call in turn.tool_calls:
            seq += 1
            key = idempotency_key(parent_key, seq, call.name, call.args)
            started = time.perf_counter()
            result, replayed = run_tool(toolset, db, key, call.name, call.args)
            used.append(call.name)
            if on_step:
                on_step({"agent": agent, "kind": "tool", "tool": call.name, "args": call.args,
                         "result": result, "ok": "error" not in result, "replayed": replayed,
                         "ms": round((time.perf_counter() - started) * 1000)})
            contents.append({"role": "tool", "name": call.name, "result": result})
    return {"agent": agent, "error": "specialist_step_limit", "tools_used": used,
            "hint": "The specialist could not finish. Tell the member to try a simpler request."}


class SupervisorTools(Toolset):
    """The supervisor's only tools are the two specialists."""

    TOOL_NAMES = ("ask_calendar", "ask_desk")
    DELEGATES  = ("ask_calendar", "ask_desk")

    def __init__(self, db: LeaveDb, providers: dict, roll_no: str, on_step=None):
        self.db, self.providers, self.roll_no, self.on_step = db, providers, roll_no, on_step

    def ask_calendar(self, question: str) -> dict:
        """Ask the calendar specialist to look up public holidays or leave-type entitlements.

        Use for 'is <date> a holiday', 'how many sick days are allowed', 'check the calendar'.
        It cannot apply leave or change any data.

        Args:
            question: A complete request, e.g. 'List all holidays in 2026.'

        Returns:
            {"agent": "calendar", "answer": str, "tools_used": [str]}.
        """
        raise RuntimeError("delegations run through delegate()")

    def ask_desk(self, request: str) -> dict:
        """Ask the desk specialist to act on the current staff member's leave account.

        IT CAN CHANGE DATA: apply leave and send HOD notifications.
        Use for checking balance, applying leave, and sending notifications. The desk always
        acts for the current staff member only.

        Args:
            request: A complete instruction, e.g. 'Apply 3 days of sick leave from 2026-01-15 \
to 2026-01-17 and notify the HOD.'

        Returns:
            {"agent": "desk", "answer": str, "tools_used": [str]}.
        """
        raise RuntimeError("delegations run through delegate()")

    def delegate(self, name: str, args: dict, key: str) -> dict:
        bad = self.call_check(name, args)
        if bad:
            return bad
        if self.on_step:
            self.on_step({"agent": "supervisor", "kind": "delegate", "tool": name, "args": args})
        if name == "ask_calendar":
            return run_specialist("calendar", CALENDAR_SYSTEM, CalendarTools(self.db),
                                  db=self.db, provider=self.providers["calendar"],
                                  task=args["question"], parent_key=key, on_step=self.on_step)
        return run_specialist("desk", DESK_SYSTEM.format(roll_no=self.roll_no),
                              DeskTools(self.db, self.roll_no),
                              db=self.db, provider=self.providers["desk"],
                              task=args["request"], parent_key=key, on_step=self.on_step)

    def call_check(self, name: str, args: dict) -> dict | None:
        """Validate a delegation's arguments the same way dispatch validates any tool call."""
        field = "question" if name == "ask_calendar" else "request"
        if set(args) != {field} or not isinstance(args[field], str) or not args[field].strip():
            return {"error": "invalid_arguments",
                    "hint": f"{name} takes one non-empty string: {field}."}
        return None
