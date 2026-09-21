"""Six tools across two toolsets for the Leave Request Assistant.

CalendarTools  (read-only, no writes):
    list_holidays   – list public holidays for a year
    get_leave_type  – fetch leave-type details (max days, id)

DeskTools  (bound to one roll_no, has side effects):
    get_staff        – fetch staff profile
    check_can_apply  – eligibility check (reads policy + balance)
    apply_leave      – deduct balance + insert application (idempotent)
    notify_hod       – send deduplicated notification to HOD
"""
import uuid

from app.leave_db import LeaveDb
from app.tools.dispatch import dispatch


class Toolset:
    """Base class: schema introspection + call routing used by agents.py."""

    TOOL_NAMES: tuple[str, ...] = ()
    SIDE_EFFECTS: tuple[str, ...] = ()   # run through db.once()
    DELEGATES: tuple[str, ...] = ()      # handled by SupervisorTools.delegate()

    def functions(self) -> dict:
        """Return {name: callable} for every tool this toolset exposes."""
        return {n: getattr(self, n) for n in self.TOOL_NAMES}

    def call(self, name: str, args: dict) -> dict:
        return dispatch(self.functions(), name, args)


# ─────────────────────────────────────────────────────────────────────────────
# Calendar specialist tools — read only, no writes
# ─────────────────────────────────────────────────────────────────────────────

class CalendarTools(Toolset):
    TOOL_NAMES = ("list_holidays", "get_leave_type")

    def __init__(self, db: LeaveDb):
        self.db = db

    def list_holidays(self, year: int) -> dict:
        """Return all public holidays for the given calendar year.

        Use this tool before applying leave to check whether the requested dates
        fall on a public holiday (which would not count against the leave balance).
        Do NOT use this tool to apply leave or change any data — it is read-only.

        Args:
            year: The four-digit calendar year, e.g. 2026.

        Returns:
            {"holidays": [{"date": "YYYY-MM-DD", "name": str}, ...]}
        """
        return {"holidays": self.db.list_holidays(year)}

    def get_leave_type(self, name: str) -> dict:
        """Fetch details about a leave type: its id, name and maximum days allowed per year.

        Use this tool to look up the leave-type id before asking the desk to apply leave,
        or to answer a member's question about entitlements.
        Do NOT use this tool to apply leave or change any data — it is read-only.

        Args:
            name: The leave type name — one of 'sick', 'casual', or 'earned'.

        Returns:
            {"id": int, "name": str, "max_per_year": int} or {"error": "not_found"}.
        """
        lt = self.db.get_leave_type(name)
        if lt is None:
            return {"error": "not_found", "hint": f"Unknown leave type '{name}'. Use sick, casual, or earned."}
        return lt


# ─────────────────────────────────────────────────────────────────────────────
# Desk specialist tools — bound to one staff member, has side effects
# ─────────────────────────────────────────────────────────────────────────────

class DeskTools(Toolset):
    TOOL_NAMES   = ("get_staff", "check_can_apply", "apply_leave", "notify_hod")
    SIDE_EFFECTS = ("apply_leave", "notify_hod")

    def __init__(self, db: LeaveDb, roll_no: str):
        self.db = db
        self._roll_no = roll_no   # private: not a parameter any tool accepts

    def get_staff(self) -> dict:
        """Return the current staff member's profile: name, department, and role.

        Use this tool first to confirm the staff member's identity and department
        before processing any leave request.
        Do NOT use this to act on behalf of any other staff member — this tool always
        returns data for the currently bound member only and changes nothing.

        Returns:
            {"roll_no": str, "name": str, "dept": str, "role": str} or {"error": "not_found"}.
        """
        s = self.db.get_staff(self._roll_no)
        if s is None:
            return {"error": "not_found", "hint": f"Staff {self._roll_no} not found."}
        return s

    def check_can_apply(self, leave_type: str, days: int) -> dict:
        """Check whether the current staff member is eligible to apply for leave.

        Always call this before apply_leave. It reads the policy table and the member's
        remaining balance and returns the reasons for any refusal.
        Do NOT use this to apply leave — it changes nothing.

        Args:
            leave_type: One of 'sick', 'casual', or 'earned'.
            days: Number of leave days requested (must be a positive integer).

        Returns:
            {"can_apply": bool, "reasons": [str]} — reasons is empty when can_apply is True.
        """
        lt = self.db.get_leave_type(leave_type)
        if lt is None:
            return {"can_apply": False, "reasons": [f"unknown leave type '{leave_type}'"]}
        return self.db.check_can_apply(self._roll_no, lt["id"], days)

    def apply_leave(self, leave_type: str, from_date: str, to_date: str, days: int) -> dict:
        """Apply for leave on behalf of the current staff member and deduct the balance.

        This tool CHANGES DATA: it inserts a leave application and reduces the remaining
        balance. It enforces the balance rule even if check_can_apply was skipped — it will
        return an error if the balance is insufficient. It is idempotent: calling it again
        with the same arguments returns the existing application rather than creating a duplicate.

        Call check_can_apply first and only call this tool if can_apply is True.
        Do NOT call this tool more than once for the same leave request.

        Args:
            leave_type: One of 'sick', 'casual', or 'earned'.
            from_date:  Start date in 'YYYY-MM-DD' format.
            to_date:    End date in 'YYYY-MM-DD' format.
            days:       Number of working days (positive integer, already calculated).

        Returns:
            {"application_id": str, "status": "pending", "already_applied": bool}
            or {"error": "not_allowed", "reasons": [str]}.
        """
        lt = self.db.get_leave_type(leave_type)
        if lt is None:
            return {"error": "not_allowed", "reasons": [f"unknown leave type '{leave_type}'"]}
        # Enforce rule in data — even if model skips check_can_apply
        eligibility = self.db.check_can_apply(self._roll_no, lt["id"], days)
        if not eligibility["can_apply"]:
            return {"error": "not_allowed", "reasons": eligibility["reasons"]}
        app_id = str(uuid.uuid4())
        try:
            return self.db.apply(self._roll_no, app_id, lt["id"], from_date, to_date, days)
        except ValueError as e:
            return {"error": "not_allowed", "reasons": [str(e)]}

    def notify_hod(self, message: str) -> dict:
        """Send a notification message to the HOD on behalf of the current staff member.

        This tool CHANGES DATA: it inserts a notification row. Sending the same message on
        the same day returns the existing notification id and sets duplicate=True — so it is
        safe to call again after a crash.
        Call this after a successful apply_leave to confirm the leave to the HOD.
        Do NOT call this tool before confirming that the leave application succeeded.

        Args:
            message: The message text to send to the HOD (non-empty string).

        Returns:
            {"notification_id": int, "duplicate": bool}.
        """
        if not message.strip():
            return {"error": "empty_message", "hint": "Provide a non-empty message."}
        return self.db.record_notification(self._roll_no, message)
