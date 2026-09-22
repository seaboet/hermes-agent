"""Turn-source envelope facts passed to ``pre_llm_call`` without prompt injection."""

from types import SimpleNamespace

import pytest

from agent.turn_context import _collect_pre_llm_call_context
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageType
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from gateway.turn_source import build_turn_source_envelope


def _telegram_event(*, attachment=False, forwarded=False, quoted=False, replied=False):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    reply_to_message = (
        SimpleNamespace(message_id=55, text="prior text", caption=None)
        if replied else None
    )
    raw_message = SimpleNamespace(
        chat=SimpleNamespace(id="chat-1", type="private", title=None, full_name="User One"),
        from_user=SimpleNamespace(id="user-1", full_name="User One", is_bot=False),
        text="current text",
        message_id=56,
        message_thread_id=None,
        is_topic_message=False,
        reply_to_message=reply_to_message,
        forward_origin=SimpleNamespace() if forwarded else None,
        quote=SimpleNamespace(text="selected text") if quoted else None,
        entities=[],
        date=None,
    )
    event = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))._build_message_event(
        raw_message, MessageType.TEXT,
    )
    if attachment:
        event.message_type = MessageType.PHOTO
        event.media_urls.append("/opaque/cache/image.jpg")
    return event


def _agent(envelope=None):
    agent = SimpleNamespace(
        _persist_disabled=False,
        session_id="session-1",
        model="test/model",
        platform="telegram",
        _parent_session_id=None,
        _user_id="user-1",
    )
    if envelope is not None:
        agent._turn_source_envelope = envelope
    return agent


def _fire_pre_llm(agent, monkeypatch):
    captured = []

    def invoke_hook(name, **kwargs):
        captured.append((name, kwargs))
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke_hook)
    assert _collect_pre_llm_call_context(
        agent,
        effective_task_id="task-1",
        turn_id="turn-1",
        original_user_message="current text",
        messages=[{"role": "user", "content": "current text"}],
        conversation_history=[],
    ) == ""
    assert [name for name, _ in captured] == ["pre_llm_call"]
    return captured[0][1]["turn_source_envelope"]


def test_telegram_ingress_envelope_reaches_pre_llm_hook_without_content(monkeypatch):
    event = _telegram_event()
    captured = []

    class HookCapturingAgent:
        _persist_disabled = False
        session_id = "session-1"
        model = "test/model"
        platform = "telegram"
        _parent_session_id = None
        _user_id = "user-1"

        def run_conversation(self, user_message, **_kwargs):
            captured.append(_fire_pre_llm(self, monkeypatch))
            return {"final_response": "ok"}

    gateway = SimpleNamespace(_consume_pending_native_image_paths=lambda _key: [])
    ctx = TurnContext(
        source=event.source,
        turn_source_envelope=build_turn_source_envelope(event),
        message=event.text,
        session_id="session-1",
        session_key="gateway-session-1",
    )
    agent = HookCapturingAgent()
    result = TurnRunner(gateway, ctx)._run_conversation_with_approval(agent, [], None, None, None)
    assert result == {"final_response": "ok"}
    assert not hasattr(agent, "_turn_source_envelope")
    envelope = captured[0]

    assert envelope == {
        "schema_version": 1,
        "platform": "telegram",
        "human_user_message": "yes",
        "plain_text_only": "yes",
        "current_text_isolated": "yes",
        "forwarded": "no",
        "quoted": "no",
        "reply_or_reference": "no",
    }
    with pytest.raises(TypeError):
        envelope["platform"] = "changed"


def test_telegram_attachment_and_context_facts_make_text_non_isolated(monkeypatch):
    event = _telegram_event(attachment=True, forwarded=True, quoted=True, replied=True)
    envelope = _fire_pre_llm(_agent(build_turn_source_envelope(event)), monkeypatch)

    assert envelope["plain_text_only"] == "no"
    assert envelope["current_text_isolated"] == "no"
    assert envelope["forwarded"] == "yes"
    assert envelope["quoted"] == "yes"
    assert envelope["reply_or_reference"] == "yes"
    assert "current text" not in repr(envelope)
    assert "prior text" not in repr(envelope)
    assert "image.jpg" not in repr(envelope)


def test_non_gateway_pre_llm_hook_gets_explicit_unknown_envelope(monkeypatch):
    envelope = _fire_pre_llm(_agent(), monkeypatch)

    assert envelope == {
        "schema_version": 1,
        "platform": "unknown",
        "human_user_message": "unknown",
        "plain_text_only": "unknown",
        "current_text_isolated": "unknown",
        "forwarded": "unknown",
        "quoted": "unknown",
        "reply_or_reference": "unknown",
    }
