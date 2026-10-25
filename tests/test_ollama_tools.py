"""Ollama tool round-trips: replayed tool calls must carry arguments as an object."""

from __future__ import annotations

import json

import httpx

from xplogent.providers.base import Message, Role, ToolCall, ToolSpec
from xplogent.providers.ollama import OllamaProvider, _to_ollama


def test_replayed_tool_call_arguments_are_objects():
    history = [Message(role=Role.USER, content="press two"),
               Message(role=Role.ASSISTANT, content="", tool_calls=[
                   ToolCall(id="c1", name="press_keys", arguments={"digits": "2"})]),
               Message(role=Role.TOOL, content="ok", tool_call_id="c1", name="press_keys")]
    out = _to_ollama(history)
    assert out[1]["tool_calls"][0]["function"]["arguments"] == {"digits": "2"}
    # The OpenAI wire format (shared with other providers) is unchanged: a JSON string.
    assert isinstance(history[1].to_openai()["tool_calls"][0]["function"]["arguments"], str)


async def test_second_round_after_a_tool_call_is_accepted():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        for m in body["messages"]:
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc["function"]["arguments"], dict):     # what Ollama does
                    return httpx.Response(400, json={"error": "Value looks like object, but can't find closing '}' symbol"})
        if len(seen) == 1:
            line = {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "press_keys", "arguments": {"digits": "2"}}}]}, "done": True}
        else:
            line = {"message": {"role": "assistant", "content": "Done."}, "done": True}
        return httpx.Response(200, content=(json.dumps(line) + "\n").encode())

    p = OllamaProvider("m", host="http://ollama.test", api_key="")
    p._client = httpx.AsyncClient(base_url="http://ollama.test", transport=httpx.MockTransport(handler))
    tools = [ToolSpec("press_keys", "keys", {"type": "object", "properties": {"digits": {"type": "string"}}})]
    msgs = [Message(role=Role.USER, content="press two")]
    first = await p.complete(msgs, tools)
    assert first.tool_calls[0].arguments == {"digits": "2"}
    msgs += [first, Message(role=Role.TOOL, content="ok", tool_call_id=first.tool_calls[0].id, name="press_keys")]
    second = await p.complete(msgs, tools)
    assert second.content == "Done." and len(seen) == 2
    await p.aclose()
