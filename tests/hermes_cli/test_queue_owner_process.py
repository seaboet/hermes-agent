"""Two real processes: ordinary TUI/Desktop startup and a separate queue CLI.

Only the model/agent boundary is deterministic. Owner startup, original client
transport, discovery, handshake, lease, RPC, persistence and queue drain are real.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

from hermes_cli.active_sessions import active_session_registry_snapshot
from hermes_cli.shared_session_attach import discover_attach_url
from hermes_state import SessionDB


def _run_owner(surface, control):
    """Child entry point: replace inference without replacing owner startup."""
    from agent.turn_context import _stage_turn_user_message
    from tests.tui_gateway.test_queued_prompt_persistence import _flush_agent
    from tui_gateway import server

    control = Path(control)
    patch = pytest.MonkeyPatch()

    def correction(*args, **kwargs):
        (control / "correction").touch()

    def build(sid, **kwargs):
        session = server._sessions[sid]
        agent = _flush_agent(server._get_db(), session["session_key"])

        def conversation(text, *, conversation_history, **kwargs):
            user, _ = _stage_turn_user_message(agent, text, text, None, None, None, None)
            messages = [*conversation_history, user]
            agent._persist_user_message_idx = len(conversation_history)
            agent._flush_messages_to_session_db(messages, conversation_history)
            if text == "FIRST":
                (control / "first_started").touch()
                deadline = time.monotonic() + 40
                while not (control / "release_first").exists():
                    assert time.monotonic() < deadline, "first turn release timed out"
                    time.sleep(0.02)
            if text == "SECOND":
                from tui_gateway import server_requests
                answer = server_requests.send("clarify", sid, {"questions": [
                    {"qid": "q", "question": "Confirm original window delivery?"}]}, timeout=15)
                assert answer == {"answers": {"q": "yes"}}
            messages.append({"role": "assistant", "content": "reply " + text})
            agent._flush_messages_to_session_db(messages, conversation_history)
            return {"messages": messages, "final_response": "reply " + text}

        agent.run_conversation = conversation
        agent.clear_interrupt = lambda: None
        agent.close = lambda: None
        agent.interrupt = agent.hard_interrupt = agent.steer = agent.redirect = correction
        agent._supports_active_turn_redirect = True
        session["agent"] = agent
        session["agent_ready"].set()

    patch.setattr(server, "_schedule_agent_build", build)
    patch.setattr(server, "_wire_callbacks", lambda sid: None)
    patch.setattr(server, "_sync_agent_model_with_config", lambda *a: None)
    patch.setattr(server, "_sync_session_key_after_compress", lambda *a, **kw: None)
    patch.setattr(server, "_tts_stream_begin", lambda: None)
    patch.setattr(server, "_get_usage", lambda agent: {})
    if surface == "tui":
        from tui_gateway.entry import main
        main()
    else:
        from hermes_cli.web_server import start_server
        start_server(host="127.0.0.1", port=0, open_browser=False, headless=True)


@pytest.mark.parametrize("surface", ["tui", "desktop"])
@pytest.mark.parametrize("busy", [True, False])
def test_queue_reaches_ordinary_owner_process(tmp_path, surface, busy):
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    home = tmp_path / "hermes-root" / "profiles" / "work"
    home.mkdir(parents=True)
    # Both correction modes must be overridden by the queue command.
    (home / "config.yaml").write_text("display:\n  busy_input_mode: " + ("steer" if surface == "tui" else "interrupt") + "\n")
    env = {**os.environ, "HOME": str(os_home), "HERMES_HOME": str(home),
           "HERMES_RUNTIME_DIR": str(tmp_path / "runtime"), "PYTHONUNBUFFERED": "1"}
    env.pop("PYTHONPATH", None)
    env.update(HERMES_DESKTOP="1" if surface == "desktop" else "0",
               HERMES_DASHBOARD_SESSION_TOKEN="owner-test-token")
    # The installed checkout bootstrap reads the user's PM manifest. Skip only
    # that installation concern; both real runtime entry points run below.
    bootstrap = """import sys
import hermes_cli.venv_sync as sync
import pm.environments as environments
import hermes_cli._early_recovery as recovery
sync.prepare_launch = lambda *a: None
environments.activate_dependencies = lambda *a: None
recovery.recover_if_needed = lambda *a: None
"""
    code = bootstrap + "from tests.hermes_cli.test_queue_owner_process import _run_owner\n_run_owner(sys.argv[1], sys.argv[2])\n"
    frames, seen = queue.Queue(), []
    stderr_path = tmp_path / "owner.stderr"
    with stderr_path.open("w") as stderr, ExitStack() as stack:
        owner = subprocess.Popen([sys.executable, "-c", code, surface, str(tmp_path)],
                                 env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=stderr, text=True)

        def pump(read):
            try:
                while True:
                    raw = read()
                    if not raw:
                        break
                    try:
                        frames.put(json.loads(raw))
                    except json.JSONDecodeError:
                        frames.put(raw.strip())
            except ConnectionClosed:
                return
            finally:
                frames.put(None)

        threading.Thread(target=pump, args=(owner.stdout.readline,), daemon=True).start()

        def receive(predicate, timeout=40):
            deadline = time.monotonic() + timeout
            while True:
                frame = frames.get(timeout=max(0, deadline - time.monotonic()))
                assert frame is not None, stderr_path.read_text()
                seen.append(frame)
                if predicate(frame):
                    return frame

        def event(frame, text):
            return isinstance(frame, dict) and frame.get("params", {}).get("type") == "message.complete" and frame["params"]["payload"].get("text") == "reply " + text

        try:
            if surface == "desktop":
                ready = receive(lambda f: isinstance(f, str) and f.startswith("HERMES_BACKEND_READY port="))
                port = ready.split("=")[1]
                ws = stack.enter_context(connect(f"ws://127.0.0.1:{port}/api/ws?token=owner-test-token", proxy=None))
                threading.Thread(target=pump, args=(ws.recv,), daemon=True).start()
                send = ws.send
            else:
                def send(data):
                    owner.stdin.write(data + "\n")
                    owner.stdin.flush()

            receive(lambda f: isinstance(f, dict) and f.get("params", {}).get("type") == "gateway.ready")

            def rpc(rid, method, **params):
                send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
                response = receive(lambda f: isinstance(f, dict) and f.get("id") == rid)
                assert "result" in response, json.dumps(response)
                return response["result"]

            rpc("caps", "client.capabilities", server_requests=True)
            created = rpc("create", "session.create", source=surface)
            sid, key = created["session_id"], created["stored_session_id"]
            first = "FIRST" if busy else "WARMUP"
            assert rpc("first", "prompt.submit", session_id=sid, text=first)["status"] == "streaming"
            if busy:
                deadline = time.monotonic() + 10
                while not (tmp_path / "first_started").exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
            else:
                if not any(event(f, first) for f in seen):
                    receive(lambda f: event(f, first))
            rpc("title", "session.title", session_id=sid, title="exact process name")
            lease_before = active_session_registry_snapshot(home)[0]
            assert lease_before["pid"] == owner.pid
            private_url = discover_attach_url(key, registry_home=home)
            result = subprocess.run([sys.executable, "-c", bootstrap + "from hermes_cli.main import main\nmain()\n",
                                     "-p", "work", "queue", "--session", key if busy else "exact process name", "--message", "SECOND"],
                                    env=env, capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, result.stderr
            assert private_url not in result.stdout + result.stderr
            assert private_url.split("token=")[1] not in result.stdout + result.stderr
            assert not (tmp_path / "correction").exists()
            with SessionDB(db_path=home / "state.db", read_only=True) as db:
                assert any(r["content"] == "SECOND" for r in db.get_messages(key))
                if busy:
                    assert not any(r["content"] == "reply FIRST" for r in db.get_messages(key))
                (tmp_path / "release_first").touch()
                question = receive(lambda f: isinstance(f, dict) and f.get("method") == "clarify")
                send(json.dumps({"jsonrpc": "2.0", "id": question["id"], "result": {"answers": {"q": "yes"}}}))
                receive(lambda f: event(f, "SECOND"))  # Original owner window still receives the turn.
                deadline = time.monotonic() + 10
                expected = [("user", first), ("assistant", "reply " + first), ("user", "SECOND"), ("assistant", "reply SECOND")]
                while [(r["role"], r["content"]) for r in db.get_messages_as_conversation(key)] != expected:
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
            lease_after = active_session_registry_snapshot(home)[0]
            assert (lease_after["pid"], lease_after["lease_id"]) == (owner.pid, lease_before["lease_id"])
            assert owner.poll() is None
        finally:
            (tmp_path / "release_first").touch()
            stack.close()
            owner.terminate()
            try:
                owner.wait(timeout=10)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.wait(timeout=10)
