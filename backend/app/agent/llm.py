"""The language model that drives the agent's tool loop.

    AgentChat
        ├── GeminiAgentChat   Gemini function calling (AGENT_LLM=gemini)
        └── OpenAIAgentChat   any OpenAI-compatible server: Ollama / LM Studio / vLLM
                              running e.g. Qwen3 (AGENT_LLM=local), or Together AI

A chat keeps its own message history in its provider's format. The loop only
sees: step() -> tool calls (or plain text), and add_results().
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.analyzers.base import ConversationTurn, Usage
from app.core.errors import ProviderUnavailable

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass
class StepResult:
    calls: list[ToolCall] = field(default_factory=list)
    text: str = ""  # a plain answer when the model did not call a tool


class AgentChat(ABC):
    usage: Usage

    @abstractmethod
    async def step(self, *, force_final: bool) -> StepResult: ...

    @abstractmethod
    def add_results(self, results: list[tuple[ToolCall, Any]]) -> None: ...

    @abstractmethod
    def nudge(self, text: str) -> None:
        """Add a user message (e.g. 'call final_answer')."""


class GeminiAgentChat(AgentChat):
    def __init__(self, client: Any, model: str, system: str, history: list[ConversationTurn], user_text: str, tool_schemas: list[dict[str, Any]]) -> None:
        from google.genai import types

        self.client, self.model, self.system = client, model, system
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.tools = [types.Tool(function_declarations=[types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]) for t in tool_schemas])]
        self.contents: list[Any] = [types.Content(role="user" if t.role == "user" else "model", parts=[types.Part(text=t.content)]) for t in history]
        self.contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

    async def step(self, *, force_final: bool) -> StepResult:
        from google.genai import errors, types

        from app.analyzers.gemini import _provider_error, _with_retries

        config = types.GenerateContentConfig(
            system_instruction=self.system,
            tools=self.tools,
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY", allowed_function_names=["final_answer"] if force_final else None)),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2,
        )
        try:
            response = await _with_retries(lambda: self.client.aio.models.generate_content(model=self.model, contents=self.contents, config=config))
        except errors.APIError as exc:
            raise _provider_error(exc) from exc
        meta = response.usage_metadata
        self.usage.input_tokens += getattr(meta, "prompt_token_count", 0) or 0
        self.usage.output_tokens += getattr(meta, "candidates_token_count", 0) or 0
        calls = response.function_calls or []
        if calls:
            self.contents.append(response.candidates[0].content)  # keeps thought signatures for the next turn
        text = "" if calls else (getattr(response, "text", "") or "")
        return StepResult(calls=[ToolCall(c.name, dict(c.args or {})) for c in calls], text=text)

    def add_results(self, results: list[tuple[ToolCall, Any]]) -> None:
        from google.genai import types

        # The Gemini API accepts only "user"/"model" roles: function results go in a user turn.
        self.contents.append(types.Content(role="user", parts=[types.Part.from_function_response(name=c.name, response={"result": r}) for c, r in results]))

    def nudge(self, text: str) -> None:
        from google.genai import types

        self.contents.append(types.Content(role="user", parts=[types.Part(text=text)]))


def inline_calls(text: str, n: int = 0) -> list[ToolCall]:
    """Small local models sometimes write the final call as text instead of a tool call:
    `final_answer({...})`, or `{"name": "final_answer", "arguments": {...}}`. Recover it."""
    import re

    for pattern in (r"final_answer\s*\(\s*(\{.*\})\s*\)", r"(\{\s*\"name\"\s*:\s*\"final_answer\".*\})"):
        m = re.search(pattern, text, re.DOTALL)
        if not m:
            continue
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("name") == "final_answer":
            obj = obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(obj, str):
                try:
                    obj = json.loads(obj)
                except json.JSONDecodeError:
                    continue
        if isinstance(obj, dict) and isinstance(obj.get("answer"), str):
            return [ToolCall("final_answer", obj, f"inline_{n}")]
    return []


class OpenAIAgentChat(AgentChat):
    """Tool calling over the OpenAI chat-completions API (Ollama, LM Studio, vLLM, Together)."""

    def __init__(self, *, base_url: str, model: str, api_key: str | None, system: str, history: list[ConversationTurn], user_text: str,
                 tool_schemas: list[dict[str, Any]], reasoning_effort: str | None = None, timeout: float = 120) -> None:
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.reasoning_effort, self.timeout = reasoning_effort, timeout
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tool_schemas]
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.messages += [{"role": "user" if t.role == "user" else "assistant", "content": t.content} for t in history]
        self.messages.append({"role": "user", "content": user_text})

    async def step(self, *, force_final: bool) -> StepResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "tools": self.tools,
            "tool_choice": {"type": "function", "function": {"name": "final_answer"}} if force_final else "required",
            "temperature": 0.2,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = None
        for attempt in (1, 2):  # a stalled local server gets one retry instead of hanging the chat
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                    response = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                logger.info("agent LLM %s step: %.1fs (HTTP %s, %d messages)", self.model, time.perf_counter() - started, response.status_code, len(self.messages))
                break
            except httpx.TimeoutException:
                logger.warning("agent LLM %s timed out after %.0fs (attempt %d)", self.model, time.perf_counter() - started, attempt)
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(f"The local assistant model is not reachable at {self.base_url}", code="agent_llm_unavailable") from exc
        if response is None:
            raise ProviderUnavailable("The local assistant model did not respond in time", code="agent_llm_timeout")
        if response.status_code != 200:
            raise ProviderUnavailable(f"The assistant model returned HTTP {response.status_code}: {response.text[:200]}", code="agent_llm_error")
        body = response.json()
        usage = body.get("usage") or {}
        self.usage.input_tokens += usage.get("prompt_tokens") or 0
        self.usage.output_tokens += usage.get("completion_tokens") or 0
        message = body["choices"][0]["message"]
        raw_calls = message.get("tool_calls") or []
        calls = []
        for i, c in enumerate(raw_calls):
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}") if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(fn.get("name", ""), args if isinstance(args, dict) else {}, c.get("id") or f"call_{len(self.messages)}_{i}"))
        if not calls:
            calls = inline_calls(message.get("content") or "", len(self.messages))
        # Keep the assistant turn (with its tool calls) so tool results can refer to it.
        self.messages.append({"role": "assistant", "content": message.get("content") or "", **({"tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}} for c in calls
        ]} if calls else {})})
        return StepResult(calls=calls, text="" if calls else (message.get("content") or "").strip())

    def add_results(self, results: list[tuple[ToolCall, Any]]) -> None:
        for call, result in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(result, ensure_ascii=False, default=str)})

    def nudge(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
