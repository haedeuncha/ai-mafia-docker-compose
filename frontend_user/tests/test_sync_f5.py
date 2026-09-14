import pytest

from copy import deepcopy
from concurrent.futures import Future
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from frontend_user.core.sync import SyncEnvelopeError, apply_envelope


GAME_ID = "d9ae9b5d-1d17-4f80-8f1a-276bfe170412"


class _SpeechQueueDouble:
    """UI 경계만 관찰하며 전송 완료 시점은 테스트가 Future로 직접 결정한다."""

    def __init__(self, client, snapshot):
        self.user_id = str(client.user_id)
        self.game_id = snapshot["game"]["game_id"]
        self.snapshot = deepcopy(snapshot)
        self.messages = []
        self.future = Future()
        self.advance_calls = 0
        self.observed = []
        self.cancellations = []

    @property
    def busy(self):
        return bool(self.messages)

    def matches(self, snapshot, user_id):
        from frontend_user.components.action_panel import _speech_scope

        return (_speech_scope(snapshot=snapshot, user_id=user_id)
                == _speech_scope(snapshot=self.snapshot, user_id=self.user_id))

    def enqueue(self, message):
        from frontend_user.core.commands import normalize_message

        self.messages.append(normalize_message(message))

    def observe(self, snapshot, user_id):
        self.observed.append((snapshot, user_id))

    def cancel(self, reason):
        self.cancellations.append(reason)
        self.messages.clear()

    def advance(self):
        self.advance_calls += 1
        return self.future.result() if self.future.done() else None

    def view(self):
        return {"pending": list(self.messages), "notice": None}


def _discussion_snapshot():
    """AI 예약 창이 바뀌어도 인간이 계속 입력할 수 있는 합성 자유 토론이다."""

    return {**_snapshot(),
            "game": {**_snapshot()["game"], "phase": "DAY_DISCUSSION", "status": "IN_PROGRESS"},
            "me": {"player_id": "human", "alive": True}, "legal_actions": ["SPEAK", "PASS"],
            "action_window": {"window_id": "00000000-0000-4000-8000-000000000001",
                              "turn_player_id": "ai-one", "kind": "SPEECH", "has_submitted": False,
                              "opened_state_version": 12,
                              "deadline_at": "2026-09-08T00:01:45Z",
                              "server_time": "2026-09-08T00:00:00Z", "remaining_ms": 105000}}


def _speech_recovery_app(initial, client):
    """실제 dispatcher의 pending 재사용과 재조회 순서를 합성 client로 재현한다."""

    import streamlit as st
    from frontend_user.app_pages import game_page

    st.session_state.setdefault("game.latest_snapshot", initial)
    st.session_state.setdefault("game.sync_status", "LIVE")
    st.session_state["game.client"] = client
    pending = st.session_state.get("game.command_pending") or {}
    reuse = st.session_state.pop("game.sync_render_snapshot", False)
    reuse = reuse or pending.get("status") in {"PENDING_TO_RENDER", "IN_FLIGHT"}
    response = st.session_state["game.latest_snapshot"] if reuse else client.get_game(initial["game"]["game_id"])
    snapshot = response.get("data", response)
    st.session_state["game.latest_snapshot"] = snapshot
    game_page.render(snapshot)


def _speech_recovery_view(monkeypatch):
    """브라우저·네트워크 없이 실제 채팅 callback과 화면 rerun을 실행한다."""

    from streamlit.testing.v1 import AppTest
    from frontend_user.app_pages import game_page
    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    client = Mock(user_id="synthetic")
    client.get_game.side_effect = lambda *_: {"data": deepcopy(current)}
    client.submit_command.return_value = {"data": {"command_type": "SPEAK"}}
    monkeypatch.setattr(action_panel, "_render_status_bar_tick", Mock())
    monkeypatch.setattr(action_panel, "SpeechQueue", _SpeechQueueDouble)
    monkeypatch.setattr(game_page, "_sync_snapshot", lambda **kwargs: kwargs["snapshot"])
    monkeypatch.setattr(game_page.vote_insights, "render", Mock())
    view = AppTest.from_function(_speech_recovery_app, args=(current, client)).run()
    assert not view.exception
    return view, current, client


@pytest.mark.parametrize("message", ["첫 줄\n다음 줄", "첫 줄\r다음 줄", "첫 줄\r\n다음 줄"])
def test_speech_callback_normalizes_only_line_breaks(monkeypatch, message):
    """UI의 CR/LF만 공백으로 바꾸고 기존 command 정규화가 최종 본문을 만든다."""

    view, _, client = _speech_recovery_view(monkeypatch)
    view.chat_input[0].set_value(message).run()
    assert not view.exception
    assert view.session_state["game.speech_queue"].messages == ["첫 줄 다음 줄"]
    client.submit_command.assert_not_called()


@pytest.mark.parametrize("message", ["거부\t본문", "거부\x00본문", "거부\u200b본문", "가" * 201])
def test_speech_validation_failure_survives_connection_rerun(monkeypatch, message):
    """입력 거부와 연결 전환이 겹쳐도 오류와 수정할 원문이 화면에 남아야 한다."""

    from frontend_user.app_pages import game_page

    view, current, client = _speech_recovery_view(monkeypatch)

    def sync(**kwargs):
        game_page.st.session_state["game.sync_status"] = "POLLING"
        return kwargs["snapshot"]

    monkeypatch.setattr(game_page, "_sync_snapshot", sync)
    view.chat_input[0].set_value(message).run()
    assert not view.exception
    assert any("발언은 제어 문자 없이" in error.value for error in view.error)
    assert view.chat_input[0].proto.set_value
    assert view.chat_input[0].proto.value == message
    client.submit_command.assert_not_called()
    current["game"].update(state_version=13, last_sequence=43)
    current["action_window"]["window_id"] = "00000000-0000-4000-8000-000000000002"
    view.run()
    assert not view.exception
    assert view.error
    # 원문을 매번 위젯으로 밀어 넣으면 사용자가 수정 중인 초안을 다시 덮게 된다.
    assert not view.chat_input[0].proto.set_value
    client.submit_command.assert_not_called()
    view.chat_input[0].set_value("수정한 정상 발언").run()
    assert view.session_state["game.speech_queue"].messages == ["수정한 정상 발언"]
    client.submit_command.assert_not_called()
    assert not view.error


def test_speech_fifo_busy_queue_keeps_chat_enabled_and_pass_locked(monkeypatch):
    """응답 대기 중에도 같은 입력 위젯을 사용하고 순서를 깨는 PASS만 막는다."""

    view, _, client = _speech_recovery_view(monkeypatch)
    widget_id = view.chat_input[0].proto.id
    for message in ["첫 발언", "두 줄\n발언", "세 번째 발언"]:
        view.chat_input[0].set_value(message).run()
        assert not view.exception and not view.error
        assert len(view.chat_input) == 1
        assert view.chat_input[0].proto.id == widget_id
        assert not view.chat_input[0].disabled
        assert view.button(key="action.PASS").disabled
    queue = view.session_state["game.speech_queue"]
    assert queue.messages == ["첫 발언", "두 줄 발언", "세 번째 발언"]
    assert not queue.future.done()
    assert "game.command_pending" not in view.session_state
    client.submit_command.assert_not_called()
    client.get_game.assert_called_once()


@pytest.mark.parametrize("boundary", ["phase", "deadline", "user", "game", "player", "round"])
def test_failed_speech_draft_is_not_restored_across_scope_changes(monkeypatch, boundary):
    """입력 형식 오류의 원문은 다른 사용자·게임·토론으로 복원하지 않는다."""

    view, current, client = _speech_recovery_view(monkeypatch)
    original = "거부\t원문"
    view.chat_input[0].set_value(original).run()
    assert view.error
    if boundary == "phase":
        current["game"]["phase"] = "FINAL_DISCUSSION"
    elif boundary == "deadline":
        current["action_window"]["deadline_at"] = "2026-09-08T00:02:00Z"
    elif boundary == "user":
        client.user_id = "other-user"
    elif boundary == "game":
        current["game"]["game_id"] = "00000000-0000-4000-8000-000000000009"
    elif boundary == "player":
        current["me"]["player_id"] = "other-player"
    else:
        current["game"]["round"] = 2
    view.run()
    assert not view.exception and not view.error
    assert "game.command_pending" not in view.session_state
    assert all(widget.proto.value != original for widget in view.chat_input)
    client.submit_command.assert_not_called()


def test_new_speech_uses_latest_validated_window_and_version(monkeypatch):
    """첫 GET 이후 sync가 AI 예약 창을 바꾸면 새 발언 body는 최신 창으로 고정한다."""

    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    latest = deepcopy(current)
    latest["game"].update(state_version=13, last_sequence=43)
    latest["action_window"].update(window_id="00000000-0000-4000-8000-000000000002",
                                   opened_state_version=13, turn_player_id="ai-two")
    state = {"game.latest_snapshot": latest}
    monkeypatch.setattr(action_panel.st, "session_state", state)
    monkeypatch.setattr(action_panel.st, "rerun", Mock(side_effect=RuntimeError("rerun")))
    with pytest.raises(RuntimeError, match="rerun"):
        action_panel._queue_command(game_id=GAME_ID, snapshot=current, command_type="SPEAK", message="합성 발언")
    command = state["game.command_pending"]["command"]
    assert command["expected_state_version"] == 13
    assert command["window_id"] == latest["action_window"]["window_id"]


@pytest.mark.parametrize("pending_status", ["PENDING_TO_RENDER", "IN_FLIGHT", "RETRYABLE_UNKNOWN"])
def test_unknown_pending_survives_window_change_and_new_submission(monkeypatch, pending_status):
    """응답 불명 요청은 AI 창이 넘어가도 본문·멱등 키를 유지하고 새 입력으로 덮지 않는다."""

    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    pending = {"game_id": GAME_ID, "status": pending_status, "idempotency_key": "fixed-key",
               "command": {"type": "SPEAK", "expected_state_version": 11,
                           "window_id": "previous-window", "message": "기존 발언"}}
    state = {"game.latest_snapshot": current, "game.command_pending": deepcopy(pending),
             "game.client": Mock(user_id="synthetic")}
    monkeypatch.setattr(action_panel, "SpeechQueue", _SpeechQueueDouble)
    monkeypatch.setattr(action_panel.st, "session_state", state)
    monkeypatch.setattr(action_panel.st, "rerun", Mock())
    assert action_panel._pending_for_window(game_id=GAME_ID, window=current["action_window"]) == pending
    action_panel._queue_command(game_id=GAME_ID, snapshot=current, command_type="SPEAK", message="새 발언")
    assert state["game.command_pending"] == pending
    state[f"form.message.{GAME_ID}"] = "새 줄바꿈\n발언"
    action_panel._capture_discussion_command(game_id=GAME_ID, snapshot=current,
                                             command_type="SPEAK", user_id="synthetic")
    assert state["game.command_pending"] == pending
    assert "game.speech_queue" not in state
    state["game.client"].submit_command.assert_not_called()


@pytest.mark.parametrize("command_type", ["SPEAK", "PASS"])
def test_enter_is_captured_before_the_next_script_reads(monkeypatch, command_type):
    """채팅 예약과 PASS 의도가 본 실행의 조회 전에 각각의 저장 경계에 남는다."""

    from streamlit.testing.v1 import AppTest
    from frontend_user.components import action_panel

    monkeypatch.setattr(action_panel, "_render_status_bar_tick", Mock())

    monkeypatch.setattr(action_panel, "SpeechQueue", _SpeechQueueDouble)

    def app_body(current):
        import streamlit as st
        from types import SimpleNamespace
        from frontend_user.components import action_panel

        st.session_state["game.latest_snapshot"] = current
        st.session_state["game.client"] = SimpleNamespace(user_id="synthetic")
        observed = st.session_state.setdefault("test.before_reads", [])
        observed.append(bool(st.session_state.get("game.command_pending")
                             or st.session_state.get("game.speech_queue")))
        action_panel._render_discussion(client=None, game_id=current["game"]["game_id"], snapshot=current)

    view = AppTest.from_function(app_body, args=(_discussion_snapshot(),)).run()
    if command_type == "SPEAK":
        view.chat_input[0].set_value("조회 전 보존할 합성 발언").run()
    else:
        view.button(key="action.PASS").click().run()
    assert not view.exception
    assert view.session_state["test.before_reads"][1] is True


@pytest.mark.parametrize("change", ["user", "game", "player", "phase", "round", "status",
                                   "permission", "version", "window", "deadline", "submitted", "expired"])
def test_new_command_rejects_changed_scope_or_action_boundary(monkeypatch, change):
    """새 입력을 다른 소유자·행동 구간·마감 이후 상태로 자동 재해석하지 않는다."""

    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    latest = deepcopy(current)
    state = {"game.latest_snapshot": latest, "game.client": SimpleNamespace(user_id="original")}
    if change == "user":
        state["game.client"] = SimpleNamespace(user_id="other")
    elif change == "game":
        latest["game"]["game_id"] = "other"
    elif change == "player":
        latest["me"]["player_id"] = "other"
    elif change == "phase":
        latest["game"]["phase"] = "FINAL_DISCUSSION"
    elif change == "round":
        latest["game"]["round"] = 2
    elif change == "status":
        latest["game"]["status"] = "SAVED"
    elif change == "permission":
        latest["legal_actions"] = []
    elif change == "version":
        latest["game"]["state_version"] = 11
    elif change == "window":
        current["action_window"]["deadline_at"] = latest["action_window"]["deadline_at"] = None
        latest["action_window"]["window_id"] = "other"
    elif change == "deadline":
        latest["action_window"]["deadline_at"] = "2026-09-08T00:02:00Z"
    elif change == "submitted":
        latest["action_window"]["has_submitted"] = True
    else:
        latest["action_window"]["remaining_ms"] = 0
    monkeypatch.setattr(action_panel.st, "session_state", state)
    error = Mock()
    monkeypatch.setattr(action_panel.st, "error", error)
    action_panel._queue_command(game_id=GAME_ID, snapshot=current, command_type="SPEAK",
                                message="합성 발언", user_id="original", rerun=False)
    error.assert_called_once()
    assert "game.command_pending" not in state


def test_clock_does_not_drop_unknown_body_and_retry_keeps_original_key(monkeypatch):
    """읽기 전용 clock이 새 창을 보더라도 불명 요청의 재전송은 최초 body·키로 수행한다."""

    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    pending = {"game_id": GAME_ID, "status": "RETRYABLE_UNKNOWN", "idempotency_key": "fixed-key",
               "command": {"type": "SPEAK", "expected_state_version": 11,
                           "window_id": "previous-window", "message": "기존 발언"}}
    state = {"game.latest_snapshot": current, "game.command_pending": deepcopy(pending)}
    monkeypatch.setattr(action_panel.st, "session_state", state)
    monkeypatch.setattr(action_panel, "ACTION_ATTENTION_COMPONENT", Mock())
    for _ in range(3):
        action_panel._render_status_bar_tick(game_id=GAME_ID, snapshot=current, running=True)
    assert state["game.command_pending"] == pending
    state["game.command_pending"]["status"] = "IN_FLIGHT"
    client = Mock()
    client.get_game.return_value = {"data": current}
    monkeypatch.setattr(action_panel.st, "rerun", Mock(side_effect=RuntimeError("rerun")))
    with pytest.raises(RuntimeError, match="rerun"):
        action_panel._process_pending(client=client, game_id=GAME_ID, snapshot=current)
    client.submit_command.assert_called_once_with(
        game_id=GAME_ID, command=pending["command"], idempotency_key=pending["idempotency_key"])


def test_speech_fifo_connection_change_cannot_consume_enter(monkeypatch):
    """연결 표시가 바뀌어도 최신 상태로 예약한 발언을 잃거나 중복 전송하지 않는다."""

    from streamlit.testing.v1 import AppTest
    from frontend_user.app_pages import game_page
    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    latest = deepcopy(current)
    latest["game"].update(state_version=13, last_sequence=43)
    latest["action_window"]["window_id"] = "00000000-0000-4000-8000-000000000002"
    sync_calls = []

    def sync(**kwargs):
        sync_calls.append(True)
        game_page.st.session_state["game.latest_snapshot"] = latest
        game_page.st.session_state["game.sync_status"] = "LIVE" if len(sync_calls) == 1 else "POLLING"
        return latest

    monkeypatch.setattr(game_page, "_sync_snapshot", sync)
    monkeypatch.setattr(game_page.vote_insights, "render", Mock())
    monkeypatch.setattr(action_panel, "_render_status_bar_tick", Mock())
    monkeypatch.setattr(action_panel, "SpeechQueue", _SpeechQueueDouble)
    client = Mock(user_id="synthetic")
    client.get_game.return_value = {"data": latest}

    def app_body(current, client):
        import streamlit as st
        from frontend_user.app_pages import game_page

        st.session_state.setdefault("game.latest_snapshot", current)
        st.session_state.setdefault("game.sync_status", "LIVE")
        st.session_state["game.client"] = client
        st.session_state["game.game_id"] = current["game"]["game_id"]
        value = st.session_state["game.latest_snapshot"]
        # 실제 dispatcher와 같이 command GET의 envelope를 화면용 snapshot으로 푼다.
        st.session_state["game.latest_snapshot"] = value.get("data", value)
        game_page.render(st.session_state["game.latest_snapshot"])

    view = AppTest.from_function(app_body, args=(current, client)).run()
    view.chat_input[0].set_value("연결 변경에도 보존할 발언").run()
    assert not view.exception
    queue = view.session_state["game.speech_queue"]
    assert queue.snapshot["game"]["state_version"] == 13
    assert queue.snapshot["action_window"]["window_id"] == latest["action_window"]["window_id"]
    assert queue.messages == ["연결 변경에도 보존할 발언"]
    assert not view.chat_input[0].disabled
    client.submit_command.assert_not_called()
    client.get_game.assert_not_called()
    assert view.session_state["game.sync_status"] == "POLLING"


def test_activity_tick_refreshes_once_without_bootstrap_or_duplicate_get(monkeypatch):
    """첫 화면과 동일 tick은 GET을 반복하지 않고 새 tick만 부가 상태를 조회한다."""

    from frontend_user.app_pages import game_page

    current = _discussion_snapshot()
    state = {"game.sync_tick": 0, "game.latest_snapshot": current}
    client = Mock(config=SimpleNamespace(api_url="http://localhost"), user_id="synthetic")
    state["game.client"] = client
    client.get_game.return_value = {"data": current}
    monkeypatch.setattr(game_page.st, "session_state", state)
    monkeypatch.setattr(game_page, "mount_sse", lambda **kwargs: None)
    game_page._sync_snapshot(client=client, snapshot=current)
    client.get_game.assert_not_called()
    client.get_sync.assert_not_called()
    state["game.sync_tick"] = 1000
    game_page._sync_snapshot(client=client, snapshot=current)
    game_page._sync_snapshot(client=client, snapshot=current)
    client.get_game.assert_called_once_with(GAME_ID)
    assert state["game.activity_tick"] == 1000


@pytest.mark.parametrize("rate_limited", [False, True])
def test_public_updates_preserve_input_shell_and_focus_scope(monkeypatch, rate_limited):
    """AI 갱신과 발언 제한 응답은 같은 토론의 입력 shell과 focus 단위를 보존한다."""

    from frontend_user.app_pages import game_page
    from frontend_user.components import action_panel

    monkeypatch.setattr(action_panel.st, "session_state", {})
    current = _discussion_snapshot()
    current["legal_actions"] = ["SPEAK", "SAVE_AND_EXIT"]
    current["action_window"]["legal_actions"] = ["SPEAK", "SAVE_AND_EXIT"]
    updated = deepcopy(current)
    updated["game"].update(state_version=13, last_sequence=43)
    updated["action_window"].update(window_id="00000000-0000-4000-8000-000000000002",
                                    turn_player_id="ai-two", remaining_ms=100000,
                                    opened_state_version=13,
                                    server_time="2026-09-08T00:00:05Z")
    updated["public_events"] = [{"event_id": "synthetic"}]
    updated["agent_activity"] = [{"stage": "DECIDING"}]
    if rate_limited:
        updated["legal_actions"] = ["SAVE_AND_EXIT"]
        updated["action_window"].update(legal_actions=["SAVE_AND_EXIT"], has_submitted=True)
    assert game_page._shell_projection(current) == game_page._shell_projection(updated)
    assert action_panel._attention_payload(current)["window_id"] == action_panel._attention_payload(updated)["window_id"]


@pytest.mark.parametrize("boundary", ["phase", "dead", "legal", "deadline", "submitted", "targets"])
def test_action_boundaries_require_shell_refresh(boundary):
    """권한·마감·사망·후보 변화는 공개 기록 갱신과 구별해 입력을 즉시 교체한다."""

    from frontend_user.app_pages import game_page

    current = _discussion_snapshot()
    updated = deepcopy(current)
    if boundary == "phase":
        updated["game"]["phase"] = "NIGHT_ACTION"
    elif boundary == "dead":
        updated["me"]["alive"] = False
    elif boundary == "legal":
        updated["legal_actions"] = []
    elif boundary == "deadline":
        updated["action_window"]["deadline_at"] = "2026-09-08T00:02:00Z"
    elif boundary == "submitted":
        # 차례제 제출 완료는 입력 경계이며 timed 토론의 일시 빈도 제한과 구별한다.
        current["action_window"].update(deadline_at=None, remaining_ms=None)
        updated["action_window"].update(deadline_at=None, remaining_ms=None)
        updated["action_window"]["has_submitted"] = True
    else:
        current["game"]["phase"] = updated["game"]["phase"] = "DAY_VOTE"
        updated["action_window"]["valid_targets"] = [{"player_id": "candidate"}]
    assert game_page._shell_projection(current) != game_page._shell_projection(updated)


def test_live_tick_updates_public_region_without_rendering_inputs(monkeypatch):
    """느린 조회가 실행되는 fragment는 공개 영역만 출력하고 전체 rerun을 요청하지 않는다."""

    from frontend_user.app_pages import game_page

    current = _discussion_snapshot()
    updated = deepcopy(current)
    updated["game"].update(state_version=13, last_sequence=43)
    updated["public_events"] = [{"event_id": "synthetic"}]
    state = {"game.latest_snapshot": current, "game.sync_status": "LIVE", "game.sync_hidden": False}
    monkeypatch.setattr(game_page.st, "session_state", state)
    monkeypatch.setattr(game_page, "_sync_snapshot", Mock(return_value=updated))
    timeline = Mock()
    inputs = Mock()
    rerun = Mock()
    monkeypatch.setattr(game_page, "_render_timeline", timeline)
    monkeypatch.setattr(game_page, "_render_agent_activity", Mock())
    monkeypatch.setattr(game_page.vote_insights, "render", Mock())
    monkeypatch.setattr(game_page, "render_action_panel", inputs)
    monkeypatch.setattr(game_page.st, "rerun", rerun)
    game_page._render_live_updates.__wrapped__(client=Mock(), snapshot=current)
    assert timeline.call_args.kwargs["snapshot"] == updated
    inputs.assert_not_called()
    rerun.assert_not_called()


def test_clock_expiry_requests_one_cached_shell_refresh(monkeypatch):
    """표시 시간이 0에 도달하면 자동 제출 없이 입력 잠금을 위한 화면 전환만 요청한다."""

    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    state = {"game.latest_snapshot": current}
    monkeypatch.setattr(action_panel.st, "session_state", state)
    monkeypatch.setattr(action_panel, "_countdown_remaining_ms", lambda **kwargs: 0)
    rerun = Mock(side_effect=RuntimeError("rerun"))
    monkeypatch.setattr(action_panel.st, "rerun", rerun)
    monkeypatch.setattr(action_panel, "_render_action_status", Mock())
    monkeypatch.setattr(action_panel, "_mount_action_attention", Mock())
    with pytest.raises(RuntimeError, match="rerun"):
        action_panel._render_status_bar_tick(game_id=GAME_ID, snapshot=current, running=True)
    assert state["game.sync_render_snapshot"] is True
    for _ in range(3):
        action_panel._render_status_bar_tick(game_id=GAME_ID, snapshot=current, running=True)
    rerun.assert_called_once_with(scope="app")
    assert "game.command_pending" not in state


@pytest.mark.parametrize("wrapped", [False, True])
def test_clock_uses_latest_snapshot_for_status_and_panel_without_mutating_inputs(monkeypatch, wrapped):
    """공개 영역 sync 뒤 이전 callback도 최신 서버 시각을 사용하고 입력·네트워크는 건드리지 않는다."""

    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    latest = deepcopy(current)
    latest["game"].update(state_version=13, last_sequence=43)
    latest["action_window"].update(server_time="2026-09-08T00:01:00Z", remaining_ms=45000)
    before = deepcopy(latest)
    client = Mock()
    state = {"game.latest_snapshot": {"data": latest} if wrapped else latest,
             "game.client": client, f"form.message.{GAME_ID}": "작성 중인 발언"}
    monkeypatch.setattr(action_panel.st, "session_state", state)
    now = [1000.0]
    monkeypatch.setattr(action_panel, "monotonic", lambda: now[0])
    status, text, attention = Mock(), Mock(), Mock()
    monkeypatch.setattr(action_panel, "_render_action_status", status)
    monkeypatch.setattr(action_panel, "_mount_action_attention", attention)
    monkeypatch.setattr(action_panel.st, "markdown", text)
    action_panel._render_status_bar_tick(game_id=GAME_ID, snapshot=current, running=True)
    assert status.call_args.kwargs["snapshot"]["action_window"]["remaining_ms"] == 45000
    now[0] += 1
    action_panel._render_panel_time_tick(game_id=GAME_ID, snapshot=current)
    assert text.call_args.args == ("## 00:44",)
    assert state[f"form.message.{GAME_ID}"] == "작성 중인 발언"
    assert latest == before
    assert current["action_window"]["remaining_ms"] == 105000
    client.assert_not_called()
    assert client.mock_calls == []


def test_clock_expiry_does_not_dismiss_open_save_dialog(monkeypatch):
    """기존 타이머 callback이 늦게 실행되어도 저장 확인창을 닫는 전체 rerun은 막는다."""

    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    state = {"game.latest_snapshot": current, "game.save_dialog_game_id": GAME_ID}
    monkeypatch.setattr(action_panel.st, "session_state", state)
    monkeypatch.setattr(action_panel, "_countdown_remaining_ms", lambda **kwargs: 0)
    monkeypatch.setattr(action_panel, "_render_action_status", Mock())
    monkeypatch.setattr(action_panel, "_mount_action_attention", Mock())
    rerun = Mock()
    monkeypatch.setattr(action_panel.st, "rerun", rerun)
    action_panel._render_status_bar_tick(game_id=GAME_ID, snapshot=current, running=True)
    rerun.assert_not_called()
    assert "game.sync_render_snapshot" not in state


@pytest.mark.parametrize("phase", ["DAY_DISCUSSION", "NIGHT_ACTION", "DAY_VOTE"])
def test_streamlit_assigns_sync_clock_and_inputs_to_separate_render_scopes(monkeypatch, phase):
    """실제 Streamlit 실행에서 GET·시계 fragment는 채팅과 대상 선택을 소유하지 않는다."""

    from streamlit.runtime.scriptrunner import get_script_run_ctx
    from streamlit.testing.v1 import AppTest
    from frontend_user.app_pages import game_page
    from frontend_user.components import action_panel

    owners = {}

    def fragment_owner():
        """지원 버전의 실행 context에서 현재 fragment 소유권만 관찰한다."""

        context = get_script_run_ctx()
        if hasattr(context, "current_fragment_id"):
            return context.current_fragment_id
        from streamlit.runtime.scriptrunner_utils.script_run_context import ThreadState
        return ThreadState.get().fragment_id

    def mount(**kwargs):
        owners["sync"] = fragment_owner()
        return None

    input_function = {
        "DAY_DISCUSSION": "_render_discussion", "NIGHT_ACTION": "_render_night_action",
        "DAY_VOTE": "_render_vote_action",
    }[phase]
    original_input = getattr(action_panel, input_function)

    def render_input(**kwargs):
        owners["input"] = fragment_owner()
        return original_input(**kwargs)

    def attention(**kwargs):
        owners["clock"] = fragment_owner()

    monkeypatch.setattr(game_page, "mount_sse", mount)
    monkeypatch.setattr(game_page.vote_insights, "render", Mock())
    monkeypatch.setattr(action_panel, input_function, render_input)
    monkeypatch.setattr(action_panel, "_mount_action_attention", attention)
    current = _discussion_snapshot()
    current["game"]["phase"] = phase
    current["me"].update(role="DOCTOR", player_id="00000000-0000-4000-8000-000000000010")
    current["players"] = []
    if phase != "DAY_DISCUSSION":
        target = {"player_id": "00000000-0000-4000-8000-000000000011", "display_name": "합성 후보"}
        current["players"] = [{**target, "alive": True}]
        current["legal_actions"] = ["SUBMIT_NIGHT_ACTION" if phase == "NIGHT_ACTION" else "SUBMIT_VOTE"]
        current["action_window"].update(
            kind="NIGHT" if phase == "NIGHT_ACTION" else "VOTE", valid_targets=[target],
        )

    def app_body(snapshot):
        import streamlit as st
        from types import SimpleNamespace
        from frontend_user.app_pages import game_page

        st.session_state["game.latest_snapshot"] = snapshot
        st.session_state["game.game_id"] = snapshot["game"]["game_id"]
        st.session_state["game.sync_status"] = "LIVE"
        st.session_state["game.client"] = SimpleNamespace(
            config=SimpleNamespace(api_url="http://localhost"), user_id="synthetic")
        game_page.render(snapshot)

    view = AppTest.from_function(app_body, args=(current,)).run()
    assert not view.exception
    assert owners["input"] is None
    assert owners["sync"] is not None and owners["clock"] is not None
    assert owners["sync"] != owners["clock"]
    inputs = view.chat_input if phase == "DAY_DISCUSSION" else view.radio
    assert len(inputs) == 1 and inputs[0].disabled is False


@pytest.mark.parametrize("reuse_reason", ["sync", "PENDING_TO_RENDER", "IN_FLIGHT"])
def test_dispatcher_reuses_sync_snapshot_only_for_the_immediate_game_refresh(monkeypatch, reuse_reason):
    """화면 전환·전송 대기에는 선행 GET을 생략하고 이후 일반 실행은 서버를 확인한다."""

    from uuid import UUID
    from frontend_user import app
    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    state = {"navigation.page": "game", "game.game_id": GAME_ID,
             "game.latest_snapshot": current}
    if reuse_reason == "sync":
        state["game.sync_render_snapshot"] = True
    else:
        state["game.command_pending"] = {"game_id": GAME_ID, "status": reuse_reason}
    ui = Mock(session_state=state)
    monkeypatch.setattr(app, "st", ui)
    monkeypatch.setattr(action_panel, "st", ui)
    monkeypatch.setattr(app, "render_app_theme", Mock())
    monkeypatch.setattr(app, "sync_page_navigation", lambda page: page)
    monkeypatch.setattr(app, "load_identity", lambda **kwargs: (
        UUID("00000000-0000-4000-8000-000000000101"), "LOCAL", None))
    monkeypatch.setattr(app, "set_identity", Mock())
    client = Mock(user_id=UUID("00000000-0000-4000-8000-000000000101"))
    client.get_game.return_value = {"data": current}
    monkeypatch.setattr(app, "ApiClient", Mock(return_value=client))
    render = Mock()
    monkeypatch.setattr(app, "render_game", render)
    app.main()
    client.get_game.assert_not_called()
    render.assert_called_once_with(current)
    assert "game.sync_render_snapshot" not in state
    state.pop("game.command_pending", None)
    app.main()
    client.get_game.assert_called_once_with(GAME_ID)


@pytest.mark.parametrize("legacy_status", [None, "SUCCEEDED", "IN_FLIGHT", "RETRYABLE_UNKNOWN"])
def test_speech_fifo_three_callbacks_reserve_in_order_without_http(monkeypatch, legacy_status):
    """첫 응답 전 연속 Enter 세 건을 보존하고 기존 단일 요청의 본문·키는 건드리지 않는다."""

    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    latest = deepcopy(current)
    latest["game"].update(state_version=13, last_sequence=43)
    latest["action_window"]["window_id"] = "00000000-0000-4000-8000-000000000002"
    client = Mock(user_id="synthetic")
    state = {"game.latest_snapshot": latest, "game.client": client}
    pending = {"game_id": GAME_ID, "status": legacy_status, "idempotency_key": "synthetic-fixed-key",
               "command": {"type": "SUBMIT_VOTE", "expected_state_version": 11}}
    if legacy_status is not None:
        state["game.command_pending"] = deepcopy(pending)
    monkeypatch.setattr(action_panel.st, "session_state", state)
    monkeypatch.setattr(action_panel, "SpeechQueue", _SpeechQueueDouble)
    rerun = Mock(side_effect=AssertionError("callback에서 전체 실행을 시작하면 안 됩니다"))
    monkeypatch.setattr(action_panel.st, "rerun", rerun)
    for message in ["첫 발언", "둘째\r\n발언", "셋째 발언"]:
        state[f"form.message.{GAME_ID}"] = message
        action_panel._capture_discussion_command(game_id=GAME_ID, snapshot=current,
                                                  command_type="SPEAK", user_id="synthetic")
    if legacy_status in {None, "SUCCEEDED"}:
        queue = state["game.speech_queue"]
        assert queue.messages == ["첫 발언", "둘째 발언", "셋째 발언"]
        assert queue.snapshot == latest
        assert queue.advance_calls == 0
        assert state["game.sync_render_snapshot"] is True
    else:
        assert "game.speech_queue" not in state
    assert state.get("game.command_pending") == (pending if legacy_status else None)
    assert client.method_calls == []
    rerun.assert_not_called()


def test_speech_fifo_dispatcher_reuses_latest_snapshot_until_queue_finishes(monkeypatch):
    """실제 앱 dispatcher의 연속 실행에서도 예약 전송 중 추가 GET이 입력 앞을 막지 않는다."""

    from uuid import UUID
    from frontend_user import app
    from frontend_user.components import action_panel

    user_id = UUID("00000000-0000-4000-8000-000000000101")
    client = Mock(user_id=user_id)
    current = _discussion_snapshot()
    client.get_game.return_value = {"data": current}
    queue = _SpeechQueueDouble(client, current)
    queue.enqueue("조회보다 먼저 예약한 발언")
    state = {"navigation.page": "game", "game.game_id": GAME_ID,
             "game.latest_snapshot": current, "game.speech_queue": queue}
    ui = Mock(session_state=state)
    monkeypatch.setattr(app, "st", ui)
    monkeypatch.setattr(action_panel, "st", ui)
    monkeypatch.setattr(action_panel, "SpeechQueue", _SpeechQueueDouble)
    monkeypatch.setattr(app, "render_app_theme", Mock())
    monkeypatch.setattr(app, "sync_page_navigation", lambda page: page)
    monkeypatch.setattr(app, "load_identity", lambda **kwargs: (user_id, "LOCAL", None))
    monkeypatch.setattr(app, "set_identity", Mock())
    monkeypatch.setattr(app, "ApiClient", Mock(return_value=client))
    render = Mock()
    monkeypatch.setattr(app, "render_game", render)
    for _ in range(3):
        app.main()
    assert render.call_count == 3
    assert all(call.args == (current,) for call in render.call_args_list)
    client.get_game.assert_not_called()
    client.submit_command.assert_not_called()
    queue.messages.clear()
    app.main()
    client.get_game.assert_called_once_with(GAME_ID)


def _speech_fifo_fragment_state(monkeypatch):
    """fragment를 단독 실행해 입력 위젯 생성과 네트워크 대기를 감시한다."""

    from frontend_user.components import action_panel

    snapshot = _discussion_snapshot()
    client = Mock(user_id="synthetic")
    queue = _SpeechQueueDouble(client, snapshot)
    queue.enqueue("미전송 발언")
    state = {"game.client": client, "game.latest_snapshot": snapshot,
             "game.speech_queue": queue, "navigation.page": "game"}
    ui = Mock(session_state=state)
    ui.container.side_effect = lambda **kwargs: nullcontext()
    monkeypatch.setattr(action_panel, "st", ui)
    monkeypatch.setattr(action_panel, "SpeechQueue", _SpeechQueueDouble)
    return action_panel, state, queue, ui


def test_speech_fifo_dispatch_fragment_returns_while_future_pending_without_input_widgets(monkeypatch):
    """느린 요청은 Future 완료 전 즉시 반환하고 채팅·PASS 위젯을 새로 만들지 않는다."""

    action_panel, state, queue, ui = _speech_fifo_fragment_state(monkeypatch)
    original = state["game.latest_snapshot"]
    for _ in range(3):
        action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    assert queue.advance_calls == 3
    assert not queue.future.done()
    assert state["game.latest_snapshot"] is original
    assert queue.messages == ["미전송 발언"]
    ui.chat_input.assert_not_called()
    ui.text_input.assert_not_called()
    ui.text_area.assert_not_called()
    ui.button.assert_not_called()
    ui.rerun.assert_not_called()
    assert state["game.client"].method_calls == []


@pytest.mark.parametrize("version,sequence,accepted", [
    (11, 43, False), (13, 41, False), (12, 42, True), (13, 43, True),
    (True, 43, False), (13, "43", False),
])
def test_speech_fifo_dispatch_result_never_reverses_either_cursor(monkeypatch, version, sequence, accepted):
    """버전과 sequence 중 하나라도 역행하거나 정수가 아니면 현재 UI를 보존한다."""

    action_panel, state, queue, _ = _speech_fifo_fragment_state(monkeypatch)
    original = state["game.latest_snapshot"]
    response = deepcopy(original)
    response["game"].update(state_version=version, last_sequence=sequence)
    response["public_events"] = [{"event_id": "synthetic-late-result"}]
    queue.future.set_result(response)
    action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    assert state["game.latest_snapshot"] is (response if accepted else original)


@pytest.mark.parametrize("boundary", ["user", "game", "player"])
def test_speech_fifo_dispatch_discards_results_from_other_owner_or_game(monkeypatch, boundary):
    """과거 요청의 결과가 다른 사용자의 게임 화면에 적용되는 것을 막는다."""

    action_panel, state, queue, _ = _speech_fifo_fragment_state(monkeypatch)
    original = state["game.latest_snapshot"]
    response = deepcopy(original)
    response["game"].update(state_version=20, last_sequence=50)
    if boundary == "user":
        state["game.client"].user_id = "other-user"
    elif boundary == "game":
        response["game"]["game_id"] = "00000000-0000-4000-8000-000000000009"
    else:
        response["me"]["player_id"] = "other-player"
    queue.future.set_result(response)
    action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    assert state["game.latest_snapshot"] is original
    assert queue.cancellations
    if boundary == "user":
        assert queue.advance_calls == 0
        assert "game.speech_queue" not in state


@pytest.mark.parametrize("boundary", ["navigation", "user", "no-user", "game", "save-pending",
                                    "save-flight", "save-unknown", "save-refresh", "save-failed"])
def test_speech_fifo_maintenance_cancels_unsent_on_save_or_navigation(monkeypatch, boundary):
    """이탈과 저장 재확인 상태에서 미전송 예약을 남겨 뒤늦게 제출하지 않는다."""

    action_panel, state, queue, _ = _speech_fifo_fragment_state(monkeypatch)
    args = {"user_id": "synthetic", "page": "game", "game_id": GAME_ID,
            "snapshot": state["game.latest_snapshot"]}
    if boundary.startswith("save-"):
        status = {"save-pending": "PENDING_TO_RENDER", "save-flight": "IN_FLIGHT",
                  "save-unknown": "RETRYABLE_UNKNOWN", "save-refresh": "REFRESH_REQUIRED",
                  "save-failed": "REFRESH_FAILED"}[boundary]
        state["game.save_pending"] = {"game_id": GAME_ID, "status": status}
    elif boundary == "navigation":
        args["page"] = "home"
    elif boundary == "game":
        args["game_id"] = "other-game"
    else:
        args["user_id"] = None if boundary == "no-user" else "other-user"
    action_panel.maintain_speech_queue(**args)
    assert queue.messages == []
    assert queue.cancellations
    assert queue.advance_calls == 0
    assert state["game.client"].method_calls == []


@pytest.mark.parametrize("payload", ["delta", "tick", "gap", "component-failure"])
def test_speech_fifo_busy_sync_applies_events_without_extra_get(monkeypatch, payload):
    """대기열이 있어도 SSE를 유지하며 공개 delta는 적용하고 보강 GET만 미룬다."""

    from frontend_user.app_pages import game_page
    from frontend_user.components import action_panel

    current = _discussion_snapshot()
    client = Mock(user_id="synthetic", config=SimpleNamespace(api_url="http://localhost"))
    queue = _SpeechQueueDouble(client, current)
    queue.enqueue("전송 대기")
    state = {"game.latest_snapshot": current, "game.client": client, "game.speech_queue": queue,
             "game.sync_tick": 1000, "game.activity_tick": 0}
    envelope = None
    if payload in {"delta", "gap"}:
        envelope = _envelope([_operation(front_sequence=43 if payload == "delta" else 45)])
    elif payload == "component-failure":
        state["game.sync_component_failed"] = True
    mount = Mock(return_value=envelope)
    monkeypatch.setattr(game_page.st, "session_state", state)
    monkeypatch.setattr(action_panel, "SpeechQueue", _SpeechQueueDouble)
    monkeypatch.setattr(game_page, "mount_sse", mount)
    result = game_page._sync_snapshot(client=client, snapshot=current)
    mount.assert_called_once()
    assert mount.call_args.kwargs["last_sequence"] == 42
    assert state["game.activity_tick"] == 0
    if payload == "delta":
        assert result["game"]["state_version"] == 13
        assert result["game"]["last_sequence"] == 43
        assert result["public_events"] == [_operation()["payload"]]
        assert state["game.latest_snapshot"] == result
    else:
        assert result == current
    client.get_game.assert_not_called()
    client.get_sync.assert_not_called()
    client.submit_command.assert_not_called()


def _speech_fifo_controlled_engine(monkeypatch):
    """실제 큐의 요청 스레드를 수동 실행해 sleep과 실서비스 없이 경합 순서를 고정한다."""

    from frontend_user.components import action_panel
    from frontend_user.core import commands

    now = [0.0]
    work = []

    class ControlledThread:
        """시작은 예약만 기록하고 요청 본문은 테스트가 선택한 시점에 실행한다."""

        def __init__(self, *, target, args, **kwargs):
            self.target = target
            self.args = args

        def start(self):
            work.append(lambda: self.target(*self.args))

    class ControlledQueue(commands.SpeechQueue):
        """큐의 실제 상태 전이는 그대로 사용하고 경과 시간만 합성 시계로 제공한다."""

        def __init__(self, client, snapshot):
            super().__init__(client, snapshot, clock=lambda: now[0])

    current = _discussion_snapshot()
    client = Mock(user_id="synthetic")
    client.get_game.side_effect = lambda *_: {"data": deepcopy(current)}

    def succeed(**kwargs):
        current["game"]["state_version"] += 1
        current["game"]["last_sequence"] += 1
        version = current["game"]["state_version"]
        current["action_window"]["window_id"] = f"00000000-0000-4000-8000-{version:012d}"
        return {"data": {"command_id": kwargs["idempotency_key"], "command_type": "SPEAK",
                         "accepted_state_version": kwargs["command"]["expected_state_version"],
                         "result_state_version": version,
                         "sync_url": f"/api/v1/games/{GAME_ID}/sync"}}

    client.submit_command.side_effect = succeed
    state = {"game.client": client, "game.latest_snapshot": deepcopy(current),
             "navigation.page": "game"}
    ui = Mock(session_state=state)
    ui.container.side_effect = lambda **kwargs: nullcontext()
    monkeypatch.setattr(action_panel, "st", ui)
    monkeypatch.setattr(action_panel, "SpeechQueue", ControlledQueue)
    monkeypatch.setattr(commands, "Thread", ControlledThread)
    return action_panel, state, current, client, work, now


def test_speech_fifo_three_enters_dispatch_once_each_in_reserved_order(monkeypatch):
    """첫 요청이 끝나기 전 입력한 세 발언이 실제 큐에서 최신 버전·독립 키로 전송된다."""

    action_panel, state, _, client, work, _ = _speech_fifo_controlled_engine(monkeypatch)
    initial = deepcopy(state["game.latest_snapshot"])
    for message in ["첫 발언", "두 줄\n발언", "셋째 발언"]:
        state[f"form.message.{GAME_ID}"] = message
        action_panel._capture_discussion_command(game_id=GAME_ID, snapshot=initial,
                                                  command_type="SPEAK", user_id="synthetic")
    queue = state["game.speech_queue"]
    assert queue.view()["pending"] == ["첫 발언", "두 줄 발언", "셋째 발언"]
    assert client.method_calls == []
    assert not work
    for completed in range(3):
        for _ in range(3):
            action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
        assert len(work) == 1
        assert client.submit_command.call_count == completed
        work.pop(0)()
        action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
        assert queue.view()["completed"] == completed + 1
        assert state["game.latest_snapshot"]["game"]["state_version"] == 13 + completed
    assert not queue.busy
    assert queue.view()["pending"] == []
    assert "game.command_pending" not in state
    calls = [call.kwargs for call in client.submit_command.call_args_list]
    assert [call["command"]["message"] for call in calls] == ["첫 발언", "두 줄 발언", "셋째 발언"]
    assert [call["command"]["expected_state_version"] for call in calls] == [12, 13, 14]
    assert len({call["idempotency_key"] for call in calls}) == 3


def test_speech_fifo_conflict_retries_automatically_without_losing_next_message(monkeypatch):
    """같은 토론의 명확한 버전 충돌은 원문을 다시 입력하지 않아도 새 창으로 재시도한다."""

    from frontend_user.core.api_client import ApiResponseError

    action_panel, state, current, client, work, _ = _speech_fifo_controlled_engine(monkeypatch)
    succeed = client.submit_command.side_effect

    def reject_first(**kwargs):
        current["game"].update(state_version=13, last_sequence=43)
        current["action_window"]["window_id"] = "00000000-0000-4000-8000-000000000002"
        client.submit_command.side_effect = succeed
        raise ApiResponseError(status_code=409, code="STALE_STATE_VERSION")

    client.submit_command.side_effect = reject_first
    for message in ["경합한 첫 발언", "대기 중인 둘째 발언"]:
        state[f"form.message.{GAME_ID}"] = message
        action_panel._capture_discussion_command(game_id=GAME_ID, snapshot=state["game.latest_snapshot"],
                                                  command_type="SPEAK", user_id="synthetic")
    for _ in range(3):
        action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
        assert len(work) == 1
        work.pop(0)()
        action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    calls = [call.kwargs for call in client.submit_command.call_args_list]
    assert [call["command"]["message"] for call in calls] == [
        "경합한 첫 발언", "경합한 첫 발언", "대기 중인 둘째 발언"]
    assert [call["command"]["expected_state_version"] for call in calls] == [12, 13, 14]
    assert calls[0]["idempotency_key"] != calls[1]["idempotency_key"]
    assert state["game.speech_queue"].view()["completed"] == 2
    assert "game.command_pending" not in state


@pytest.mark.parametrize("boundary", ["phase", "dead", "saved", "round", "day", "deadline", "expired"])
def test_speech_fifo_observed_scope_change_cancels_before_scheduled_request(monkeypatch, boundary):
    """요청 스레드가 예약된 직후 토론 경계가 바뀌면 미전송 발언을 POST하지 않는다."""

    action_panel, state, _, client, work, _ = _speech_fifo_controlled_engine(monkeypatch)
    snapshot = state["game.latest_snapshot"]
    state[f"form.message.{GAME_ID}"] = "경계 전 미전송 발언"
    action_panel._capture_discussion_command(game_id=GAME_ID, snapshot=snapshot,
                                              command_type="SPEAK", user_id="synthetic")
    queue = state["game.speech_queue"]
    action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    changed = deepcopy(snapshot)
    changed["game"].update(state_version=13, last_sequence=43)
    if boundary == "phase":
        changed["game"]["phase"] = "NIGHT_ACTION"
    elif boundary == "dead":
        changed["me"]["alive"] = False
    elif boundary == "saved":
        changed["game"]["status"] = "SAVED"
    elif boundary in {"round", "day"}:
        changed["game"]["round" if boundary == "round" else "day_number"] = 2
    elif boundary == "deadline":
        changed["action_window"]["deadline_at"] = "2026-09-08T00:02:00Z"
    else:
        changed["action_window"]["remaining_ms"] = 0
    action_panel.maintain_speech_queue(user_id="synthetic", page="game", game_id=GAME_ID,
                                       snapshot=changed)
    assert queue.view()["pending"] == []
    assert queue.view()["status"] == "CANCELLED"
    assert len(work) == 1
    work.pop(0)()
    action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    client.submit_command.assert_not_called()
    assert not queue.busy


@pytest.mark.parametrize("phase", ["DAY_DISCUSSION", "FINAL_DISCUSSION"])
def test_speech_fifo_rate_limited_input_remains_available(monkeypatch, phase):
    """제한 응답이 제출 완료를 표시해도 일반·최종 토론에서 8·9번 입력은 열어 둔다."""

    from frontend_user.components import action_panel

    view, current, client = _speech_recovery_view(monkeypatch)
    current["game"]["phase"] = phase
    current["legal_actions"] = ["SAVE_AND_EXIT"]
    current["action_window"].update(legal_actions=["SAVE_AND_EXIT"], has_submitted=True)
    view.run()
    assert not view.exception
    assert len(view.chat_input) == 1
    assert not view.chat_input[0].disabled
    status = action_panel._action_status(current)
    assert status is not None and "예약" in status[0] and "발언 제한" in status[0]
    widget_id = view.chat_input[0].proto.id
    messages = ["8번째 예약 발언", "9번째 예약 발언"]
    for index, message in enumerate(messages, start=1):
        view.chat_input[0].set_value(message).run()
        assert not view.exception and not view.error
        assert view.session_state["game.speech_queue"].messages == messages[:index]
        assert len(view.chat_input) == 1 and not view.chat_input[0].disabled
        assert view.chat_input[0].proto.id == widget_id
    client.submit_command.assert_not_called()


@pytest.mark.parametrize("next_state", ["allowed", "expired", "paused", "dead", "saved"])
def test_speech_fifo_rate_limit_preserves_order_until_permission_returns(monkeypatch, next_state):
    """제출 완료를 동반한 제한은 8·9번을 보존하되 마감·정지·사망·저장은 취소한다."""

    action_panel, state, current, client, work, now = _speech_fifo_controlled_engine(monkeypatch)
    rendered = deepcopy(current)
    current["legal_actions"] = ["SAVE_AND_EXIT"]
    current["action_window"].update(legal_actions=["SAVE_AND_EXIT"], has_submitted=True)
    state["game.latest_snapshot"] = deepcopy(current)
    messages = ["8번째 예약 발언", "9번째 예약 발언"]
    for message in messages:
        state[f"form.message.{GAME_ID}"] = message
        # 입력을 그린 뒤 제한 응답이 도착해도 callback은 최신 상태에서 예약을 유지한다.
        action_panel._capture_discussion_command(game_id=GAME_ID, snapshot=rendered,
                                                  command_type="SPEAK", user_id="synthetic")
    queue = state["game.speech_queue"]
    action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    assert len(work) == 1
    work.pop(0)()
    action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    assert queue.view()["pending"] == messages
    assert queue.view()["status"] == "WAITING"
    client.submit_command.assert_not_called()
    for _ in range(3):
        action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    assert not work
    if next_state != "allowed":
        if next_state == "expired":
            current["action_window"]["remaining_ms"] = 0
        elif next_state == "paused":
            current["action_window"]["paused"] = True
        elif next_state == "dead":
            current["me"]["alive"] = False
        else:
            current["game"]["status"] = "SAVED"
        state["game.latest_snapshot"] = deepcopy(current)
        action_panel.maintain_speech_queue(user_id="synthetic", page="game", game_id=GAME_ID,
                                           snapshot=current)
        action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
        assert queue.view()["status"] == "CANCELLED"
        assert queue.view()["pending"] == []
        assert queue.view()["completed"] == 0
        assert not work and not queue.busy
        client.submit_command.assert_not_called()
        return
    current["legal_actions"] = ["SPEAK", "PASS"]
    current["action_window"].update(legal_actions=["SPEAK", "PASS"], has_submitted=False)
    now[0] = 3.0
    for _ in range(2):
        action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
        assert len(work) == 1
        work.pop(0)()
        action_panel._render_speech_queue.__wrapped__(game_id=GAME_ID)
    assert queue.view()["completed"] == 2
    assert queue.view()["pending"] == []
    calls = [call.kwargs for call in client.submit_command.call_args_list]
    assert [call["command"]["message"] for call in calls] == messages
    assert [call["command"]["expected_state_version"] for call in calls] == [12, 13]
    assert len({call["idempotency_key"] for call in calls}) == 2


def _snapshot():
    return {"game": {"game_id": GAME_ID, "state_version": 12, "last_sequence": 42}, "players": [], "public_events": []}


def _envelope(operations):
    return {"data": {"game_id": GAME_ID, "mode": "DELTA", "from_state_version": 12, "state_version": 13, "last_sequence": 43, "operations": operations}}


def _operation(**changes):
    return {"schema_version": 1, "state_version": 13, "type": "APPEND_PUBLIC_EVENT",
            "front_sequence": 43, "operation_index": 0,
            "payload": {"event_id": "e", "event_type": "GAME_BEGAN", "message": "시작"}, **changes}


def test_delta_applies_complete_operation_batch_once() -> None:
    # 팀 전달 사항: Backend contract test도 한 visible transaction의 operation을
    # 동일 front_sequence와 0부터 연속인 operation_index로 반환해야 한다.
    envelope = _envelope([_operation()])
    updated, mode = apply_envelope(snapshot=_snapshot(), envelope=envelope)
    assert mode == "DELTA"
    assert updated["game"]["last_sequence"] == 43
    assert len(updated["public_events"]) == 1
    replayed, _ = apply_envelope(snapshot=updated, envelope=envelope)
    assert replayed == updated


def test_unknown_operation_and_sequence_gap_are_rejected_atomically() -> None:
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=_snapshot(), envelope=_envelope([_operation(type="UNKNOWN")]))
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=_snapshot(), envelope=_envelope([_operation(front_sequence=45)]))


def test_snapshot_mode_replaces_authoritative_state() -> None:
    replacement = {"game": {"game_id": GAME_ID, "state_version": 20, "last_sequence": 50}}
    updated, mode = apply_envelope(snapshot=_snapshot(), envelope={"data": {"game_id": GAME_ID, "mode": "SNAPSHOT", "snapshot": replacement}})
    assert mode == "SNAPSHOT"
    assert updated == replacement


def test_delayed_snapshot_cannot_reverse_latest_cursor():
    """오래된 SSE snapshot은 현재 상태를 덮기 전에 최신 GET으로 다시 확인한다."""

    replacement = {"game": {"game_id": GAME_ID, "state_version": 11, "last_sequence": 41}}
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=_snapshot(), envelope={"game_id": GAME_ID, "mode": "SNAPSHOT", "snapshot": replacement})


def test_nested_sequence_batches_apply_phase_transition_atomically() -> None:
    """Backend의 sequence별 중첩 operation이 최신 phase까지 반영되는지 확인한다."""

    envelope = {
        "data": {
            "game_id": GAME_ID,
            "mode": "DELTA",
            "from_state_version": 12,
            "state_version": 14,
            "last_sequence": 44,
            "operations": [{
                "schema_version": 1,
                "front_sequence": 43,
                "state_version": 13,
                "operations": [{
                    "operation_index": 0,
                    "type": "SET_GAME_STATE",
                    "payload": {
                        "phase": "NIGHT_ACTION",
                        "state_version": 13,
                    },
                }],
            }, {
                "schema_version": 1,
                "front_sequence": 44,
                "state_version": 14,
                "operations": [{
                    "operation_index": 0,
                    "type": "SET_ACTION_WINDOW",
                    "payload": {"kind": "NIGHT", "valid_targets": []},
                }],
            }],
        },
    }
    updated, mode = apply_envelope(snapshot=_snapshot(), envelope=envelope)

    assert mode == "DELTA"
    assert updated["game"]["phase"] == "NIGHT_ACTION"
    assert updated["game"]["last_sequence"] == 44
    assert updated["action_window"]["kind"] == "NIGHT"


@pytest.mark.parametrize("changes", [
    {"schema_version": 2}, {"schema_version": None}, {"schema_version": True},
    {"front_sequence": True}, {"operation_index": True}, {"operation_index": 1},
    {"state_version": 12}, {"payload": []}, {"type": []},
])
def test_invalid_operation_never_partially_changes_snapshot(changes):
    """schema·cursor·payload 오류는 현재 화면을 변경하지 않고 GET 복구로 보낸다."""

    snapshot = _snapshot()
    original = deepcopy(snapshot)
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=snapshot, envelope=_envelope([_operation(**changes)]))
    assert snapshot == original


def test_missing_schema_and_nested_index_are_not_invented():
    """구형 응답에 schema나 index를 합성하면 손상 batch를 정상으로 오인할 수 있다."""

    operation = _operation()
    operation.pop("schema_version")
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=_snapshot(), envelope=_envelope([operation]))
    operation.pop("operation_index")
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=_snapshot(), envelope=_envelope([{
            "schema_version": 1, "front_sequence": 43, "state_version": 13,
            "operations": [operation],
        }]))


def test_backend_nested_operations_use_explicit_inner_schema():
    operation = _operation()
    operation.pop("front_sequence")
    operation.pop("state_version")
    envelope = _envelope([{"front_sequence": 43, "state_version": 13, "operations": [operation]}])
    updated, _ = apply_envelope(snapshot=_snapshot(), envelope=envelope)
    assert len(updated["public_events"]) == 1
    operation["front_sequence"] = 44
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=_snapshot(), envelope=envelope)


@pytest.mark.parametrize("changes", [
    {"last_sequence": 44}, {"last_sequence": True}, {"state_version": 14},
    {"from_state_version": 13},
])
def test_envelope_cursor_must_match_complete_batch(changes):
    envelope = _envelope([_operation()])
    envelope["data"].update(changes)
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=_snapshot(), envelope=envelope)


def test_replayed_batch_before_new_batch_is_skipped():
    first, _ = apply_envelope(snapshot=_snapshot(), envelope=_envelope([_operation()]))
    second = _envelope([_operation(), _operation(front_sequence=44, state_version=14)])
    second["data"].update(last_sequence=44, state_version=14)
    updated, _ = apply_envelope(snapshot=first, envelope=second)
    assert len(updated["public_events"]) == 2
    replayed, _ = apply_envelope(snapshot=updated, envelope=_envelope([_operation()]))
    assert replayed == updated


def test_duplicate_operation_in_current_batch_is_applied_once():
    updated, _ = apply_envelope(snapshot=_snapshot(), envelope=_envelope([_operation(), _operation()]))
    assert len(updated["public_events"]) == 1


def test_unknown_after_valid_operation_is_atomic_and_snapshot_game_is_checked():
    snapshot = _snapshot()
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=snapshot, envelope=_envelope([
            _operation(), _operation(operation_index=1, type="UNKNOWN"),
        ]))
    assert snapshot == _snapshot()
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=snapshot, envelope={"game_id": GAME_ID, "mode": "SNAPSHOT",
                       "snapshot": {"game": {"game_id": "another-game", "state_version": 20, "last_sequence": 50}}})


def test_noop_keeps_cursor_and_rejects_advanced_empty_delta():
    envelope = _envelope([])
    with pytest.raises(SyncEnvelopeError):
        apply_envelope(snapshot=_snapshot(), envelope=envelope)
    envelope["data"].update(state_version=12, last_sequence=42)
    updated, _ = apply_envelope(snapshot=_snapshot(), envelope=envelope)
    assert updated == _snapshot()


def test_bridge_scope_tick_and_envelope_delivery(monkeypatch):
    """동일 widget 상태 재수신은 중복 적용하지 않고 이전 UUID scope 응답은 폐기한다."""

    from types import SimpleNamespace
    from uuid import UUID
    from frontend_user.components import sync_bridge

    session = {}
    returned = SimpleNamespace(envelope=None, status_tick=None)
    calls = []
    monkeypatch.setattr(sync_bridge.st, "session_state", session)
    monkeypatch.setattr(sync_bridge, "SYNC_COMPONENT", lambda **kwargs: calls.append(kwargs) or returned)
    args = {"backend_url": "http://127.0.0.1:8000", "game_id": GAME_ID,
            "user_id": UUID("00000000-0000-4000-8000-000000000101"),
            "last_sequence": 42, "after_state_version": 12}
    assert sync_bridge.mount_sse(**args) is None
    data = calls[-1]["data"]
    assert data["after_sequence"] == 42
    assert data["after_state_version"] == 12
    assert data["policy"]["foreground_poll_ms"] == 2000
    scope = {key: data[key] for key in ("schema_version", "component_instance_id", "scope_version")}
    returned.envelope = {**scope, "type": "SYNC_ENVELOPE", "event_id": "delivery-1", "envelope": _envelope([_operation()])}
    returned.status_tick = {**scope, "type": "SYNC_STATUS", "tick": 1000, "status": "POLLING", "hidden": False}
    assert sync_bridge.mount_sse(**args) == returned.envelope["envelope"]
    assert session["game.sync_tick"] == 1000
    assert session["game.sync_status"] == "POLLING"
    assert sync_bridge.mount_sse(**args) is None
    returned.status_tick = {**returned.status_tick, "tick": 900, "status": "STALE"}
    assert sync_bridge.mount_sse(**args) is None
    assert session["game.sync_status"] == "POLLING"
    returned.envelope = {**returned.envelope, "event_id": "delivery-2", "envelope": None}
    assert sync_bridge.mount_sse(**args) == {}
    args["user_id"] = UUID("00000000-0000-4000-8000-000000000102")
    assert sync_bridge.mount_sse(**args) is None
    assert session["game.sync_tick"] == 0
    assert calls[-1]["data"]["scope_version"] != scope["scope_version"]
    assert calls[-1]["key"] != calls[0]["key"]


def test_browser_sync_transport_lifecycle_with_mock_fetch():
    """실제 JS에 가상 시계·stream을 주입해 네트워크 없이 장애·복귀·cleanup을 검증한다."""

    import json
    import shutil
    import subprocess
    from dataclasses import asdict
    from pathlib import Path
    from frontend_user.core.sync_policy import DEFAULT_SYNC_POLICY

    node = shutil.which("node")
    if node is None:
        pytest.skip("브라우저 sync JS 검증에는 Node.js가 필요합니다.")
    source = (Path(__file__).parents[1] / "components/browser_components/sync/index.js").read_text()
    script = r"""
import assert from 'node:assert/strict';
const {default: render} = await import('data:text/javascript;base64,' + Buffer.from(SOURCE_TEXT).toString('base64'));
let now = 0, nextTimer = 1;
const timers = new Map();
globalThis.setTimeout = (fn, delay = 0) => { const id = nextTimer++; timers.set(id, {fn, at: now + delay}); return id; };
globalThis.clearTimeout = id => timers.delete(id);
globalThis.requestAnimationFrame = fn => setTimeout(fn, 0);
Date.now = () => 100000 + now;
Math.random = () => 0.5;
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
async function advance(delay) {
  const until = now + delay;
  await flush();
  let steps = 0;
  while (true) {
    const next = [...timers.entries()].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
    if (!next) break;
    assert.ok(++steps < 1000, '타이머가 무한 반복되면 안 된다');
    now = next[1].at;
    timers.delete(next[0]);
    next[1].fn();
    await flush();
  }
  now = until;
  await flush();
}
function eventTarget(extra = {}) {
  const listeners = new Map();
  return {...extra, listeners,
    addEventListener: (type, fn) => { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
    removeEventListener: (type, fn) => listeners.get(type)?.delete(fn),
    fire(type) { for (const fn of [...(listeners.get(type) || [])]) fn(); },
  };
}
const scrolls = [];
const timeline = {scrollHeight: 900, scrollTo: options => scrolls.push(options)};
globalThis.document = eventTarget({visibilityState: 'visible', querySelector: () => timeline});
globalThis.window = eventTarget();
Object.defineProperty(globalThis, 'navigator', {value: {onLine: true}, configurable: true});
let sseHealthy = false, pollingHealthy = true, pendingPoll = false;
const requests = [], streams = [], outputs = [];
const noOp = (gameId, sequence, version) => ({game_id: gameId, mode: 'DELTA', from_state_version: version,
  state_version: version, last_sequence: sequence, operations: [], snapshot: null});
globalThis.fetch = async (url, options) => {
  const request = {url, options, at: now};
  requests.push(request);
  if (url.includes('/sync?')) {
    if (pendingPoll) return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(Error('중단'))));
    if (!pollingHealthy) throw Error('목 장애');
    const parsed = new URL(url);
    return {ok: true, json: async () => ({data: noOp(parsed.pathname.split('/')[4],
      Number(parsed.searchParams.get('after_sequence')), Number(parsed.searchParams.get('after_state_version')))})};
  }
  if (!sseHealthy) return {ok: false};
  const stream = {request};
  const body = new ReadableStream({
    start(controller) {
      stream.controller = controller;
      controller.enqueue(new TextEncoder().encode(': heartbeat\n\n'));
      options.signal.addEventListener('abort', () => { try { controller.error(Error('중단')); } catch (_) {} });
    },
    cancel() { stream.cancelled = true; },
  });
  streams.push(stream);
  return {ok: true, body};
};
const props = {backend_url: 'http://127.0.0.1:8000', game_id: 'game-one', user_id: 'user-one',
  component_instance_id: 'component-one', scope_version: 'scope-one', schema_version: 1,
  last_sequence: 42, after_sequence: 42, after_state_version: 12, policy: POLICY_VALUE};
const root = {ownerDocument: document};
let cleanup;
function run(data = props) {
  cleanup = render({data, parentElement: root, setStateValue: (key, value) => outputs.push({key, value, at: now})});
}
const polls = () => requests.filter(request => request.url.includes('/sync?'));
const sse = () => requests.filter(request => request.url.endsWith('/events'));
const ticks = () => outputs.filter(output => output.key === 'status_tick');
const envelopes = () => outputs.filter(output => output.key === 'envelope');
run();
await advance(5000);
assert.deepEqual(polls().map(request => request.at), [0, 2000, 4000]);
assert.deepEqual(scrolls, [{top: 900, behavior: 'smooth'}]);
assert.deepEqual(sse().map(request => request.at), [0, 1000, 3000]);
assert.equal(envelopes().length, 0);
assert.equal(ticks().length, 2);
assert.equal(ticks().at(-1).value.status, 'POLLING');
assert.ok(requests.every(request => request.options.headers['X-User-Id'] === 'user-one'));
assert.ok(requests.every(request => !request.url.includes('user-one')));
assert.ok(requests.every(request => request.options.headers['X-Request-Id']));

// SSE가 복구되면 polling을 멈추고 no-op heartbeat에도 2초 상태 tick은 유지한다.
sseHealthy = true;
await advance(3000);
assert.equal(ticks().at(-1).value.status, 'LIVE');
const count = polls().length;
await advance(2000);
assert.equal(polls().length, count);
const liveRequest = streams.at(-1).request;
cleanup();
run();
await advance(0);
assert.equal(liveRequest.options.signal.aborted, false);
assert.equal(sse().length, 4);
assert.equal(scrolls.length, 1);

// frame 조각·다중 data line은 완성 뒤 한 번만 전달하고 수신 cursor를 독자 전진시키지 않는다.
const event = {...noOp('game-one', 43, 13), from_state_version: 12,
  operations: [{schema_version: 1, state_version: 13, front_sequence: 43, operation_index: 0,
    type: 'CLEAR_ACTION_WINDOW', payload: {window_id: null}}]};
const frame = 'id: 43\r\nevent: game_sync\r\ndata: ' + JSON.stringify(event).replace(',"operations"', ',\r\ndata: "operations"') + '\r\n\r\n';
streams.at(-1).controller.enqueue(new TextEncoder().encode(frame.slice(0, 35)));
await flush();
assert.equal(envelopes().length, 0);
streams.at(-1).controller.enqueue(new TextEncoder().encode(frame.slice(35)));
await flush();
assert.equal(envelopes().length, 1);
const beforeDisconnect = polls().length;
streams.at(-1).controller.close();
await flush();
await advance(0);
assert.equal(polls().length, beforeDisconnect + 1);
await advance(1000);
assert.equal(sse().at(-1).options.headers['Last-Event-ID'], '42');
const accepted = {...props, last_sequence: 43, after_sequence: 43, after_state_version: 13};
cleanup();
run(accepted);
await advance(0);
assert.equal(scrolls.length, 2);
window.fire('online');
await flush();
assert.match(polls().at(-1).url, /after_sequence=43/);

// background에서는 tick을 늦추고 foreground 복귀 순간 polling·상태 확인을 수행한다.
document.visibilityState = 'hidden';
document.fire('visibilitychange');
const hiddenCount = ticks().length;
await advance(9999);
assert.equal(ticks().length, hiddenCount);
await advance(1);
assert.equal(ticks().length, hiddenCount + 1);
const pollBeforeWake = polls().length;
document.visibilityState = 'visible';
document.fire('visibilitychange');
await flush();
assert.equal(polls().length, pollBeforeWake + 1);
assert.equal(ticks().at(-1).value.hidden, false);

// 서버가 잘못된 SSE id를 보내면 cursor를 넘기지 않고 Python snapshot 복구를 요청한다.
streams.at(-1).controller.enqueue(new TextEncoder().encode('id: 999\nevent: game_sync\ndata: ' + JSON.stringify(event) + '\n\n'));
await flush();
assert.deepEqual(envelopes().at(-1).value.envelope, {});
await advance(1000);
assert.equal(sse().at(-1).options.headers['Last-Event-ID'], '43');

// 새 게임·UUID scope로 교체하면 이전 stream과 모든 listener를 정리한다.
const previousRequest = streams.at(-1).request;
const nextProps = {...props, game_id: 'game-two', user_id: 'user-two', scope_version: 'scope-two'};
cleanup();
run(nextProps);
await advance(0);
assert.equal(previousRequest.options.signal.aborted, true);
assert.equal(streams.at(-1).request.options.headers['X-User-Id'], 'user-two');
assert.equal(document.listeners.get('visibilitychange').size, 1);
const activeRequest = streams.at(-1).request;
cleanup();
await advance(0);
assert.equal(activeRequest.options.signal.aborted, true);
assert.equal(document.listeners.get('visibilitychange').size, 0);
assert.equal(window.listeners.get('online').size, 0);
const outputCount = outputs.length, requestCount = requests.length;
await advance(60000);
assert.equal(outputs.length, outputCount);
assert.equal(requests.length, requestCount);
assert.equal(timers.size, 0);

// 연속 polling 장애는 2·4·8·16·30초로 늦추며 5회 실패 후 STALE이 된다.
sseHealthy = false;
pollingHealthy = false;
const failureStart = now;
run(nextProps);
await advance(32000);
const failedPolls = polls().filter(request => request.at >= failureStart).map(request => request.at - failureStart);
assert.deepEqual(failedPolls, [0, 2000, 6000, 14000, 30000]);
assert.equal(ticks().at(-1).value.status, 'STALE');
pollingHealthy = true;
window.fire('online');
await flush();
await advance(2000);
assert.equal(ticks().at(-1).value.status, 'POLLING');

// 종료 시 대기 중인 polling도 중단해 새 scope의 화면에 응답하지 못하게 한다.
pendingPoll = true;
window.fire('online');
await flush();
const pendingRequest = polls().at(-1);
cleanup();
await advance(0);
assert.equal(pendingRequest.options.signal.aborted, true);
assert.equal(timers.size, 0);
console.log('sync browser lifecycle passed');
""".replace("SOURCE_TEXT", json.dumps(source)).replace("POLICY_VALUE", json.dumps(asdict(DEFAULT_SYNC_POLICY)))
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sync browser lifecycle passed" in result.stdout
