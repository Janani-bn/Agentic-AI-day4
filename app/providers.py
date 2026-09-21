"""Model providers. The agent only knows `generate`.

Supports:
- OpenAIProvider  : real calls via openai SDK (reads OPENAI_API_KEY)
- ScriptedProvider: replays a fixed list of turns in call order (no network)
- PositionalMock  : answers by position in the current turn (crash/resume tests)
- RoutedMock      : picks a script by phrase (demo + tests)
- demo_providers  : leave-domain scripted conversations, no key needed
"""
from dataclasses import dataclass, field
from typing import Any


class AgentError(Exception):
    """A run could not finish. `retryable` says whether trying again later could work."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class ModelTurn:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    raw: Any = None     # provider-native content, sent back as-is


# `contents` is a plain list the agent builds up:
#   {"role": "user",  "text": str}
#   {"role": "model", "text": str | None, "tool_calls": [{"name", "args"}], "raw": ...}
#   {"role": "tool",  "name": str, "result": dict}


# ─────────────────────────────────────────────────────────────────────────────
# Real provider — OpenAI
# ─────────────────────────────────────────────────────────────────────────────

class OpenAIProvider:
    """Calls the OpenAI Chat Completions API with function/tool calling."""

    def __init__(self, model: str = "gpt-4o-mini"):
        import openai
        self.client = openai.OpenAI()   # reads OPENAI_API_KEY from env
        self.model = model

    # ── convert our generic content list → OpenAI messages ──────────────────

    def _to_openai_messages(self, system: str, contents: list[dict]) -> list[dict]:
        msgs = [{"role": "system", "content": system}]
        for c in contents:
            if c["role"] == "user":
                msgs.append({"role": "user", "content": c["text"]})
            elif c["role"] == "model":
                msg: dict = {"role": "assistant"}
                if c.get("text"):
                    msg["content"] = c["text"]
                calls = c.get("tool_calls") or []
                if calls:
                    msg["tool_calls"] = [
                        {"id": f"call_{i}", "type": "function",
                         "function": {"name": t["name"], "arguments": __import__("json").dumps(t["args"])}}
                        for i, t in enumerate(calls)
                    ]
                msgs.append(msg)
            elif c["role"] == "tool":
                msgs.append({
                    "role": "tool",
                    "tool_call_id": "call_0",   # simplified: single call per turn in our loop
                    "content": __import__("json").dumps(c["result"], default=str),
                })
        return msgs

    # ── convert Python callables → OpenAI tool schema ───────────────────────

    @staticmethod
    def _to_openai_tools(functions: list) -> list[dict]:
        import inspect, typing, json

        tools = []
        for fn in functions:
            sig = inspect.signature(fn)
            hints = typing.get_type_hints(fn)
            props = {}
            required = []
            for name, param in sig.parameters.items():
                ptype = hints.get(name, str)
                json_type = {int: "integer", str: "string", float: "number",
                             bool: "boolean"}.get(ptype, "string")
                props[name] = {"type": json_type}
                if param.default is inspect.Parameter.empty:
                    required.append(name)
            schema = {"type": "object", "properties": props}
            if required:
                schema["required"] = required
            tools.append({
                "type": "function",
                "function": {
                    "name": fn.__name__,
                    "description": inspect.getdoc(fn) or "",
                    "parameters": schema,
                },
            })
        return tools

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        import json
        from openai import APIError, RateLimitError

        messages = self._to_openai_messages(system, contents)
        openai_tools = self._to_openai_tools(tools) if tools else []

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=openai_tools or None,
                tool_choice="auto" if openai_tools else None,
                temperature=0,
            )
        except RateLimitError as e:
            raise AgentError("provider_rate_limited", "OpenAI quota exceeded. Wait a minute.", True) from e
        except APIError as e:
            if e.status_code and e.status_code >= 500:
                raise AgentError("provider_unavailable", "OpenAI API failed.", True) from e
            raise AgentError("provider_error", str(e), False) from e

        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or None
        calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            calls.append(ToolCall(tc.function.name, args))
        usage = resp.usage
        return ModelTurn(
            text=text,
            tool_calls=calls,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            raw=msg,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Scripted / mock providers (no network, no quota)
# ─────────────────────────────────────────────────────────────────────────────

class ScriptedProvider:
    """Replays a fixed list of turns in call order. No network, no quota. Used by the tests."""

    model = "mock"

    def __init__(self, script: list, loop: bool = False):
        self.original, self.script, self.loop = list(script), list(script), loop
        self.calls: list[list[dict]] = []

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        self.calls.append([dict(c) for c in contents])
        if not self.script and self.loop:
            self.script = list(self.original)
        if not self.script:
            return ModelTurn(text="(mock) script exhausted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class PositionalMock:
    """A scripted model that answers by position in the current turn, not by call count.
    A fresh process that resumes a half-finished run gets the NEXT turn, not the first one.
    `slow` sleeps before each answer, so you have time to kill the worker mid-run."""

    model = "mock"

    def __init__(self, turns: list[ModelTurn], slow: float = 0.0):
        self.turns, self.slow = turns, slow
        self.calls: list[list[dict]] = []

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        import time

        self.calls.append([dict(c) for c in contents])
        last_user = max(i for i, c in enumerate(contents) if c["role"] == "user")
        position = sum(1 for c in contents[last_user:] if c["role"] == "model")
        if self.slow:
            time.sleep(self.slow)
        if position >= len(self.turns):
            return ModelTurn(text="(mock) nothing more to do.")
        return self.turns[position]


class RoutedMock:
    """Several scripted conversations in one mock: picks a script by a phrase in the current
    request, then answers by position (like PositionalMock). Used by the demo and the tests."""

    model = "mock"

    def __init__(self, routes: dict[str, list[ModelTurn]], slow: float = 0.0):
        self.routes, self.slow = routes, slow
        self.calls: list[list[dict]] = []

    def generate(self, system: str, contents: list[dict], tools: list) -> ModelTurn:
        import time

        self.calls.append([dict(c) for c in contents])
        last_user = max(i for i, c in enumerate(contents) if c["role"] == "user")
        request = contents[last_user]["text"]
        position = sum(1 for c in contents[last_user:] if c["role"] == "model")
        if self.slow:
            time.sleep(self.slow)
        for phrase, turns in self.routes.items():
            if phrase.lower() in request.lower():
                return turns[position] if position < len(turns) else ModelTurn(text="(mock) done.")
        return ModelTurn(text="(mock) I have no script for that request.")


# ─────────────────────────────────────────────────────────────────────────────
# Scripted demo conversations for the leave domain
# ─────────────────────────────────────────────────────────────────────────────

def _call(tool_name, **args):
    return ModelTurn(text=None, tool_calls=[ToolCall(tool_name, args)], tokens_in=100, tokens_out=10)


def demo_providers(slow: float = 0.0) -> dict:
    """Scripted models for the three agents, covering the two demo questions (no API key)."""
    return {
        "supervisor": RoutedMock({
            # Q1: Priya applies for sick leave — should succeed
            "sick leave": [
                _call("ask_calendar", question="List public holidays in 2026 and confirm sick leave type details."),
                _call("ask_desk", request="Apply 3 days of sick leave from 2026-01-15 to 2026-01-17 for S001 and notify the HOD."),
                ModelTurn(text="(mock) Your 3-day sick leave from 2026-01-15 to 2026-01-17 has been applied and your HOD has been notified."),
            ],
            # Q2: Arjun tries to take 5 days sick leave — refused: no balance
            "casual leave": [
                _call("ask_calendar", question="Confirm casual leave type details."),
                _call("ask_desk", request="Check if S002 can take 15 days of casual leave."),
                ModelTurn(text="(mock) Sorry, you cannot take 15 days of casual leave: you only have 10 days remaining."),
            ],
        }, slow),

        "calendar": RoutedMock({
            "sick leave": [
                _call("get_leave_type", name="sick"),
                ModelTurn(text="(mock) Sick leave allows up to 10 days per year. No holidays fall in the requested window."),
            ],
            "casual leave": [
                _call("get_leave_type", name="casual"),
                ModelTurn(text="(mock) Casual leave allows up to 15 days per year."),
            ],
        }, slow),

        "desk": RoutedMock({
            # Q1 desk: apply leave and notify HOD
            "Apply 3 days of sick leave": [
                _call("check_can_apply", leave_type="sick", days=3),
                ModelTurn(text=None, tool_calls=[
                    ToolCall("apply_leave", {"leave_type": "sick", "from_date": "2026-01-15",
                                             "to_date": "2026-01-17", "days": 3}),
                    ToolCall("notify_hod", {"message": "Priya Raman (S001) has applied for 3 days sick leave from 2026-01-15 to 2026-01-17."}),
                ], tokens_in=150, tokens_out=25),
                ModelTurn(text="(mock) Applied 3 days sick leave and notified the HOD."),
            ],
            # Q2 desk: check can apply — should be refused
            "Check if S002 can take 15 days of casual leave": [
                _call("check_can_apply", leave_type="casual", days=15),
                ModelTurn(text="(mock) Cannot apply: only 10 casual days remaining, 15 requested."),
            ],
        }, slow),
    }
