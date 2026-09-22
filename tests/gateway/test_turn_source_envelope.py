"""Turn-source envelope facts passed to ``pre_llm_call`` without prompt injection."""

from types import SimpleNamespace

import pytest

from agent.turn_context import _collect_pre_llm_call_context
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from gateway.turn_source import build_turn_source_envelope
from hermes_cli.plugins import PluginManager


def _telegram_event(
    *, attachment=False, forwarded=False, quoted=False, replied=False,
    automatic_forward=False, external_reply=False, reply_to_story=False,
    reply_to_checklist_task_id=None, reply_to_poll_option_id=None, bot_author=False,
):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    reply_to_message = (
        SimpleNamespace(message_id=55, text="prior text", caption=None)
        if replied else None
    )
    raw_message = SimpleNamespace(
        chat=SimpleNamespace(id="chat-1", type="private", title=None, full_name="User One"),
        from_user=SimpleNamespace(id="user-1", full_name="User One", is_bot=bot_author),
        text="current text",
        message_id=56,
        message_thread_id=None,
        is_topic_message=False,
        reply_to_message=reply_to_message,
        forward_origin=SimpleNamespace() if forwarded else None,
        is_automatic_forward=automatic_forward,
        quote=SimpleNamespace(text="selected text") if quoted else None,
        external_reply=SimpleNamespace() if external_reply else None,
        reply_to_story=SimpleNamespace() if reply_to_story else None,
        reply_to_checklist_task_id=reply_to_checklist_task_id,
        reply_to_poll_option_id=reply_to_poll_option_id,
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


def test_non_telegram_gateway_event_preserves_known_platform():
    event = MessageEvent(
        text="current text",
        source=SessionSource(platform=Platform.DISCORD, chat_id="chat-1", user_id="user-1"),
    )

    envelope = build_turn_source_envelope(event)

    assert envelope["platform"] == "discord"
    assert all(envelope[name] == "unknown" for name in (
        "human_user_message", "plain_text_only", "current_text_isolated",
        "forwarded", "quoted", "reply_or_reference",
    ))


def test_telegram_unavailable_raw_facts_remain_unknown():
    event = _telegram_event()
    del event.raw_message.forward_origin
    del event.raw_message.quote

    envelope = build_turn_source_envelope(event)

    assert envelope["forwarded"] == "unknown"
    assert envelope["quoted"] == "unknown"
    assert envelope["current_text_isolated"] == "unknown"


def test_telegram_unavailable_human_source_fact_does_not_change_content_isolation():
    event = _telegram_event()
    event.source = SimpleNamespace(platform=Platform.TELEGRAM, user_id="user-1")

    envelope = build_turn_source_envelope(event)

    assert envelope["human_user_message"] == "unknown"
    assert envelope["current_text_isolated"] == "yes"


def test_known_unsafe_fact_makes_text_non_isolated_despite_unknown_fact():
    event = _telegram_event(forwarded=True)
    del event.raw_message.quote

    envelope = build_turn_source_envelope(event)

    assert envelope["forwarded"] == "yes"
    assert envelope["quoted"] == "unknown"
    assert envelope["current_text_isolated"] == "no"


def test_safe_authoritative_telegram_facts_make_text_isolated():
    envelope = build_turn_source_envelope(_telegram_event())

    assert envelope["plain_text_only"] == "yes"
    assert envelope["forwarded"] == envelope["quoted"] == envelope["reply_or_reference"] == "no"
    assert envelope["current_text_isolated"] == "yes"


@pytest.mark.parametrize("marker", (
    "external_reply",
    "reply_to_story",
    "reply_to_checklist_task_id",
    "reply_to_poll_option_id",
))
def test_telegram_raw_reply_reference_markers_make_text_non_isolated(marker):
    value = "option-1" if marker == "reply_to_poll_option_id" else 1
    if marker in {"external_reply", "reply_to_story"}:
        value = True

    envelope = build_turn_source_envelope(_telegram_event(**{marker: value}))

    assert envelope["reply_or_reference"] == "yes"
    assert envelope["current_text_isolated"] == "no"
    assert "current text" not in repr(envelope)


def test_telegram_automatic_forward_marker_makes_text_non_isolated():
    envelope = build_turn_source_envelope(_telegram_event(automatic_forward=True))

    assert envelope["forwarded"] == "yes"
    assert envelope["current_text_isolated"] == "no"


def test_telegram_bot_author_is_not_human_but_safe_text_remains_isolated():
    envelope = build_turn_source_envelope(_telegram_event(bot_author=True))

    assert envelope["human_user_message"] == "no"
    assert envelope["current_text_isolated"] == "yes"


def test_unavailable_telegram_source_context_remains_unknown():
    event = _telegram_event()
    del event.raw_message.forward_origin
    del event.raw_message.is_automatic_forward
    del event.raw_message.quote
    del event.raw_message.external_reply
    del event.raw_message.reply_to_story
    del event.raw_message.reply_to_checklist_task_id
    del event.raw_message.reply_to_poll_option_id

    envelope = build_turn_source_envelope(event)

    assert envelope["forwarded"] == "unknown"
    assert envelope["quoted"] == "unknown"
    assert envelope["reply_or_reference"] == "unknown"
    assert envelope["current_text_isolated"] == "unknown"


def test_known_unsafe_telegram_marker_dominates_unavailable_context():
    event = _telegram_event(external_reply=True)
    del event.raw_message.forward_origin
    del event.raw_message.is_automatic_forward
    del event.raw_message.quote
    del event.raw_message.reply_to_story
    del event.raw_message.reply_to_checklist_task_id
    del event.raw_message.reply_to_poll_option_id

    envelope = build_turn_source_envelope(event)

    assert envelope["reply_or_reference"] == "yes"
    assert envelope["current_text_isolated"] == "no"


def test_narrow_pre_llm_hook_ignores_additive_turn_source_envelope(monkeypatch):
    captured = []

    def narrow_hook(session_id, user_message):
        captured.append((session_id, user_message))

    manager = PluginManager()
    manager._hooks["pre_llm_call"] = [narrow_hook]
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0)

    assert manager.invoke_hook(
        "pre_llm_call",
        session_id="session-1",
        user_message="current text",
        turn_source_envelope=build_turn_source_envelope(_telegram_event()),
    ) == []
    assert captured == [("session-1", "current text")]
