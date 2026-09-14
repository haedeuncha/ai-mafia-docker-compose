import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from frontend_user.components import action_panel
from frontend_user.core.api_client import ApiUnavailableError
from frontend_user.core.commands import build_command, normalize_message


def _snapshot(*, legal_actions=None, message_window=None):
    return {
        "game": {"state_version": 12},
        "me": {"player_id": "0fa54b68-a42a-4d52-81dd-8a59e54eb269", "alive": True},
        "legal_actions": legal_actions or ["SPEAK", "PASS"],
        "action_window": message_window or {
            "window_id": "11137761-d31b-46d1-8fb0-144ecf436069",
            "paused": False,
            "has_submitted": False,
            "turn_player_id": "0fa54b68-a42a-4d52-81dd-8a59e54eb269",
            "legal_actions": ["SPEAK", "PASS"],
            "remaining_ms": None,
            "valid_targets": [],
        },
    }


@pytest.mark.parametrize("length", [0, 201])
def test_message_length_is_rejected(length: int) -> None:
    with pytest.raises(ValueError):
        normalize_message("가" * length)


def test_message_200_chars_is_accepted_after_whitespace_normalization() -> None:
    assert len(normalize_message("가" * 200)) == 200


def test_speak_command_contains_contract_fields() -> None:
    command = build_command(snapshot=_snapshot(), command_type="SPEAK", message="  안녕   하세요 ")
    assert command == {
        "type": "SPEAK",
        "expected_state_version": 12,
        "window_id": "11137761-d31b-46d1-8fb0-144ecf436069",
        "message": "안녕 하세요",
    }


def test_target_uuid_is_forwarded_for_backend_validation() -> None:
    # Front snapshot의 valid_targets는 stale될 수 있으므로 Backend가 최종 검증한다.
    window = {
        "window_id": "11137761-d31b-46d1-8fb0-144ecf436069",
        "paused": False,
        "has_submitted": False,
        "legal_actions": ["SUBMIT_VOTE"],
        "remaining_ms": 30000,
        "valid_targets": [{"player_id": "70d5bd5d-61da-4db4-b218-6d0ac41f2a08", "display_name": "플레이어 2"}],
    }
    snapshot = _snapshot(legal_actions=["SUBMIT_VOTE"], message_window=window)
    command = build_command(snapshot=snapshot, command_type="SUBMIT_VOTE", target_player_id="e15f18b6-ea20-477e-9d99-8a22dc6048f5")
    assert command["target_player_id"] == "e15f18b6-ea20-477e-9d99-8a22dc6048f5"


def test_stale_window_is_forwarded_for_backend_validation() -> None:
    window = _snapshot()["action_window"] | {"remaining_ms": 0}
    command = build_command(snapshot=_snapshot(message_window=window), command_type="SPEAK", message="발언")
    assert command["type"] == "SPEAK"


@pytest.mark.parametrize("command_type", ["SAVE_AND_EXIT", "RESUME", "FAST_FORWARD"])
def test_lifecycle_commands_do_not_send_window_id(command_type: str) -> None:
    """window과 무관한 lifecycle command가 불필요한 필드를 보내지 않는지 확인한다."""

    snapshot = _snapshot(legal_actions=[command_type])
    command = build_command(snapshot=snapshot, command_type=command_type)
    assert command == {"type": command_type, "expected_state_version": 12}


GAME = "00000000-0000-4000-8000-000000000201"
HUMAN = "0fa54b68-a42a-4d52-81dd-8a59e54eb269"
TARGET = "70d5bd5d-61da-4db4-b218-6d0ac41f2a08"
DEAD = "00000000-0000-4000-8000-000000000204"
OUTSIDER = "00000000-0000-4000-8000-000000000205"


def _action_snapshot(phase="DAY_VOTE"):
    """실제 시각·DB 없이 후보와 서버 마감 계약을 검증하는 합성 snapshot을 만든다."""

    snapshot = _snapshot()
    snapshot["game"].update(game_id=GAME, status="IN_PROGRESS", phase=phase, last_sequence=42)
    snapshot["me"]["role"] = "DOCTOR"
    snapshot["players"] = [
        {"player_id": HUMAN, "display_name": "나", "alive": True},
        {"player_id": TARGET, "display_name": "후보", "alive": True},
        {"player_id": DEAD, "display_name": "탈락자", "alive": False},
    ]
    command = "SUBMIT_NIGHT_ACTION" if phase == "NIGHT_ACTION" else "SUBMIT_VOTE"
    snapshot["legal_actions"] = [command]
    snapshot["action_window"].update(
        kind={"NIGHT_ACTION": "NIGHT", "REVOTE": "REVOTE", "FINAL_ACCUSATION": "FINAL_VOTE"}.get(phase, "VOTE"),
        server_time="2026-09-07T00:00:00Z",
        deadline_at="2026-09-07T00:00:30Z",
        remaining_ms=30000,
        legal_actions=[command],
        valid_targets=[{"player_id": HUMAN, "display_name": "나"},
                       {"player_id": TARGET, "display_name": "후보"}],
    )
    return snapshot


@pytest.fixture
def panel_state(monkeypatch):
    """시계·command queue 단위 검증에서는 실제 Streamlit 세션을 사용하지 않는다."""

    state = {}
    monkeypatch.setattr(action_panel.st, "session_state", state)
    monkeypatch.setattr(action_panel, "monotonic", lambda: 1000.0)
    return state


@pytest.mark.parametrize("phase", ["DAY_VOTE", "REVOTE", "FINAL_ACCUSATION"])
def test_vote_targets_exclude_self_and_invalid_candidates(phase):
    snapshot = _action_snapshot(phase)
    snapshot["action_window"]["valid_targets"] += [
        {"player_id": HUMAN.upper()}, {"player_id": TARGET.upper()},
        {"player_id": DEAD}, {"player_id": OUTSIDER}, {"player_id": "bad"},
        {"player_id": []}, {}, None,
    ]
    assert action_panel._valid_targets(snapshot=snapshot, command_type="SUBMIT_VOTE") == [
        {"player_id": TARGET, "display_name": "후보"},
    ]


@pytest.mark.parametrize("targets", [[], None, {}, [{"player_id": OUTSIDER}]])
def test_server_candidates_are_not_replaced_with_all_living_players(targets):
    snapshot = _action_snapshot("REVOTE")
    snapshot["action_window"]["valid_targets"] = targets
    assert action_panel._valid_targets(snapshot=snapshot, command_type="SUBMIT_VOTE") == []


@pytest.mark.parametrize("field,value", [("alive", False), ("alive", "true"), ("game_id", OUTSIDER)])
def test_invalid_target_metadata_is_rejected(field, value):
    snapshot = _action_snapshot()
    snapshot["action_window"]["valid_targets"] = [{"player_id": TARGET, field: value}]
    assert action_panel._valid_targets(snapshot=snapshot, command_type="SUBMIT_VOTE") == []


@pytest.mark.parametrize("field,value", [("alive", False), ("alive", "true"), ("game_id", OUTSIDER)])
def test_invalid_public_player_metadata_is_rejected(field, value):
    snapshot = _action_snapshot()
    snapshot["players"][1][field] = value
    assert action_panel._valid_targets(snapshot=snapshot, command_type="SUBMIT_VOTE") == []


def test_night_preserves_server_allowed_self_protection_and_order():
    snapshot = _action_snapshot("NIGHT_ACTION")
    assert [target["player_id"] for target in action_panel._valid_targets(
        snapshot=snapshot, command_type="SUBMIT_NIGHT_ACTION"
    )] == [HUMAN, TARGET]


@pytest.mark.parametrize("target,game_id", [(HUMAN, GAME), (DEAD, GAME), (OUTSIDER, GAME),
                                           ("bad", GAME), (TARGET, OUTSIDER)])
def test_invalid_selection_is_rejected_before_creating_a_pending_command(panel_state, monkeypatch, target, game_id):
    error, rerun = Mock(), Mock()
    monkeypatch.setattr(action_panel.st, "error", error)
    monkeypatch.setattr(action_panel.st, "rerun", rerun)
    action_panel._queue_command(game_id=game_id, snapshot=_action_snapshot(),
                                command_type="SUBMIT_VOTE", target_player_id=target)
    error.assert_called_once()
    rerun.assert_not_called()
    assert "game.command_pending" not in panel_state


def test_removed_selection_is_cleared_without_selecting_a_replacement(panel_state):
    panel_state["target"] = DEAD
    action_panel._clear_invalid_selection(key="target", options=[TARGET])
    assert panel_state["target"] is None


def test_countdown_advances_without_reanchoring_the_same_snapshot(panel_state, monkeypatch):
    snapshot = _action_snapshot()
    original = deepcopy(snapshot)
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) == 30000
    monkeypatch.setattr(action_panel, "monotonic", lambda: 1012.25)
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=deepcopy(snapshot)) == 17750
    monkeypatch.setattr(action_panel, "monotonic", lambda: 1100.0)
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) == 0
    assert snapshot == original


def test_countdown_uses_server_deadline_and_new_server_observations(panel_state, monkeypatch):
    snapshot = _action_snapshot()
    snapshot["action_window"]["remaining_ms"] = 999999
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) == 30000
    monkeypatch.setattr(action_panel, "monotonic", lambda: 1002.0)
    snapshot["action_window"]["server_time"] = "2026-09-07T00:00:10Z"
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) == 20000
    snapshot["action_window"]["server_time"] = "2026-09-07T00:00:00Z"
    monkeypatch.setattr(action_panel, "monotonic", lambda: 1003.0)
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) == 19000


@pytest.mark.parametrize("saved", [True, False])
def test_saved_or_paused_countdown_is_frozen_until_new_resume_deadline(panel_state, monkeypatch, saved):
    snapshot = _action_snapshot()
    action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot)
    snapshot["game"]["status"] = "SAVED" if saved else "IN_PROGRESS"
    snapshot["action_window"].update(paused=not saved, deadline_at=None, remaining_ms=17500)
    monkeypatch.setattr(action_panel, "monotonic", lambda: 2000.0)
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) == 17500
    assert not action_panel._timer_is_running(snapshot)
    assert action_panel._countdown_text(snapshot) == "00:18 · 일시 정지"
    snapshot["game"]["status"] = "IN_PROGRESS"
    snapshot["action_window"].update(paused=False, server_time="2026-09-07T01:00:00Z",
                                      deadline_at="2026-09-07T01:00:17.500Z")
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) == 17500
    monkeypatch.setattr(action_panel, "monotonic", lambda: 2001.0)
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) == 16500


@pytest.mark.parametrize("server_time", [None, "bad", "2026-09-07T00:00:00", {}])
def test_invalid_server_clock_does_not_fall_back_to_local_wall_time(panel_state, server_time):
    snapshot = _action_snapshot()
    snapshot["action_window"]["server_time"] = server_time
    assert not action_panel._timer_is_running(snapshot)
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) is None


def test_untimed_window_has_no_clock_and_is_not_expired(panel_state):
    snapshot = _action_snapshot("DAY_DISCUSSION")
    snapshot["action_window"].update(kind="SPEECH", deadline_at=None, remaining_ms=None)
    assert action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot) is None
    assert action_panel._countdown_text(snapshot) == "시간 제한 없음"
    assert not action_panel._is_locked(window=snapshot["action_window"], pending=None)
    snapshot["action_window"]["paused"] = True
    assert action_panel._countdown_text(snapshot) == "일시 정지 · 시간 제한 없음"


def _action_app(snapshot, client):
    import streamlit as st
    from unittest.mock import patch
    from frontend_user.components import action_panel

    current = st.session_state.setdefault("test.snapshot", snapshot)
    st.session_state["game.client"] = client
    st.session_state["game.latest_snapshot"] = current
    # AppTest는 v2 component registry를 초기화하지 않으므로 브라우저 focus bridge만
    # 대체한다. 행동 패널·countdown·live region은 실제 Python 렌더 경로를 사용한다.
    with patch.object(action_panel, "ACTION_ATTENTION_COMPONENT") as attention:
        action_panel.render(client=client, game_id=current["game"]["game_id"], snapshot=current)
        action_panel.render_status_bar(game_id=current["game"]["game_id"], snapshot=current)
        st.session_state["test.attention_payload"] = attention.call_args.kwargs["data"]


def test_countdown_ui_updates_warnings_and_locks_without_network_or_submission(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(action_panel, "monotonic", lambda: now[0])
    client = Mock()
    app = AppTest.from_function(_action_app, args=(_action_snapshot(), client)).run()
    assert not app.exception
    assert app.radio[0].options == ["🔵 후보"]
    app.radio[0].set_value(TARGET).run()
    assert not app.button(key="action.SUBMIT_VOTE").disabled
    for elapsed, remaining, warning in [(16, "00:14", "15초"), (26, "00:04", "5초"), (31, "00:00", None)]:
        now[0] = 1000.0 + elapsed
        app.run()
        assert not app.exception
        assert any(remaining in item.value for item in app.markdown)
        if warning:
            assert warning in app.session_state["test.attention_payload"]["announcement"]
    assert app.button(key="action.SUBMIT_VOTE").disabled
    assert app.radio[0].disabled
    assert app.session_state["test.snapshot"]["action_window"]["remaining_ms"] == 30000
    client.submit_command.assert_not_called()
    client.get_game.assert_not_called()


@pytest.mark.parametrize(
    ("phase", "remaining", "expected_label", "expected_warning"),
    [
        ("NIGHT_ACTION", 9_000, "보호 대상을 선택하세요", "밤 행동 마감까지 10초 이하 남았습니다."),
        ("DAY_VOTE", 14_000, "투표 대상을 선택하세요", "투표 마감까지 15초 이하 남았습니다."),
        ("REVOTE", 4_000, "투표 대상을 선택하세요", "투표 마감까지 5초 이하 남았습니다."),
        ("FINAL_ACCUSATION", 30_000, "최종 판정 대상을 지목하세요", None),
    ],
)
def test_current_action_status_exposes_action_and_threshold_without_auto_target(
    panel_state, phase, remaining, expected_label, expected_warning
):
    snapshot = _action_snapshot(phase)
    snapshot["action_window"]["remaining_ms"] = remaining
    status = action_panel._action_status(snapshot)
    assert status == (expected_label, action_panel._remaining_text(remaining), expected_warning)
    assert "game.command_pending" not in panel_state


def test_attention_payload_separates_visual_second_tick_from_live_announcement(panel_state):
    snapshot = _action_snapshot()
    snapshot["action_window"]["remaining_ms"] = 30_000
    payload = action_panel._attention_payload(snapshot)
    assert payload == {
        "active": True,
        "window_id": snapshot["action_window"]["window_id"],
        "announcement": "새 행동: 투표 대상을 선택하세요.",
    }
    snapshot["action_window"]["remaining_ms"] = 14_000
    assert action_panel._attention_payload(snapshot)["announcement"].endswith(
        "투표 마감까지 15초 이하 남았습니다."
    )
    html = (
        Path(__file__).parents[1]
        / "components/browser_components/action_attention/index.html"
    ).read_text(encoding="utf-8")
    assert 'role="status"' in html and 'aria-live="polite"' in html
    assert "00:30" not in html


def test_final_accusation_uses_decisive_copy_and_distinct_submit_label(monkeypatch):
    monkeypatch.setattr(action_panel, "monotonic", lambda: 1000.0)
    snapshot = _action_snapshot("FINAL_ACCUSATION")
    app = AppTest.from_function(_action_app, args=(snapshot, Mock())).run()
    assert not app.exception
    assert any("최종 판정할 플레이어를 지목해 주세요" in item.value for item in app.markdown)
    assert any("시민을 선택하면 마피아가 승리" in item.value for item in app.warning)
    assert any("재투표하지 않으며" in item.value for item in app.caption)
    assert app.button(key="action.SUBMIT_VOTE").label == "최종 지목 제출  →"


def test_browser_attention_announces_without_moving_focus():
    """실제 bridge JS가 안내만 갱신하고 새 창·countdown에서 초안 focus를 유지하는지 확인한다."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("행동 안내 bridge JS 검증에는 Node.js가 필요합니다.")
    source = (
        Path(__file__).parents[1]
        / "components/browser_components/action_attention/index.js"
    ).read_text(encoding="utf-8")
    script = r"""
import assert from 'node:assert/strict';
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(SOURCE).toString('base64');
const {default: render} = await import(moduleUrl);
let scrolls = 0;
let focuses = 0;
let announcements = 0;
globalThis.requestAnimationFrame = callback => callback();
globalThis.setTimeout = callback => callback();
globalThis.window = {matchMedia: () => ({matches: true})};
const input = {focus: options => {
  assert.deepEqual(options, {preventScroll: true});
  focuses += 1;
}};
let liveText = '';
const liveRegion = {};
Object.defineProperty(liveRegion, 'textContent', {
  get: () => liveText,
  set: value => { liveText = value; announcements += 1; },
});
const region = {
  scrollIntoView: options => {
    assert.deepEqual(options, {block: 'start', behavior: 'auto'});
    scrolls += 1;
  },
  querySelector: () => input,
  setAttribute: () => { throw Error('input이 있으므로 region 자체를 focus하면 안 됩니다.'); },
};
const parentElement = {
  ownerDocument: {querySelector: () => region},
  querySelector: () => liveRegion,
};
const firstWindow = '00000000-0000-4000-8000-000000000001';
const secondWindow = '00000000-0000-4000-8000-000000000002';
render({data: {active: true, window_id: firstWindow, announcement: '새 행동'}, parentElement});
render({data: {active: true, window_id: firstWindow, announcement: '새 행동'}, parentElement});
assert.equal(scrolls, 0);
assert.equal(focuses, 0);
assert.equal(announcements, 1);
render({data: {active: true, window_id: firstWindow, announcement: '15초 경고'}, parentElement});
assert.equal(announcements, 2);
assert.equal(liveText, '15초 경고');
render({data: {active: true, window_id: secondWindow, announcement: '15초 경고'}, parentElement});
assert.equal(scrolls, 0);
assert.equal(focuses, 0);
render({data: {active: false, window_id: secondWindow, announcement: '마감'}, parentElement});
assert.equal(scrolls, 0);
assert.equal(announcements, 3);
""".replace("SOURCE", json.dumps(source))
    # 실행 파일은 PATH에서 찾은 고정 Node.js이고, shell을 거치지 않는다.
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "--eval", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_action_retry_preserves_exact_body_and_idempotency_key(monkeypatch):
    monkeypatch.setattr(action_panel, "monotonic", lambda: 1000.0)
    snapshot = _action_snapshot()
    client = Mock()
    client.submit_command.side_effect = [
        ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE", request_id="synthetic"),
        {"data": {"command_type": "SUBMIT_VOTE"}},
    ]
    client.get_game.return_value = {"data": snapshot}
    app = AppTest.from_function(_action_app, args=(snapshot, client)).run()
    app.radio[0].set_value(TARGET).run()
    app.button(key="action.SUBMIT_VOTE").click().run()
    assert not app.exception
    assert app.session_state["game.command_pending"]["status"] == "RETRYABLE_UNKNOWN"
    assert client.submit_command.call_count == 1
    original = client.submit_command.call_args
    app.run()
    assert client.submit_command.call_count == 1
    app.button(key="action.retry_pending").click().run()
    assert not app.exception
    assert client.submit_command.call_count == 2
    assert client.submit_command.call_args == original
    assert app.session_state["game.command_pending"]["status"] == "SUCCEEDED"
    client.get_game.assert_called_once_with(GAME)


@pytest.mark.parametrize("mode,interval", [("active", 1), ("paused", None), ("saved", None),
                                          ("untimed", None), ("expired", None), ("save_dialog", None)])
def test_fragment_schedules_clock_and_status_tick_never_reprocesses_pending(panel_state, monkeypatch, mode, interval):
    snapshot = _action_snapshot()
    if mode == "paused":
        snapshot["action_window"]["paused"] = True
    elif mode == "saved":
        snapshot["game"]["status"] = "SAVED"
    elif mode == "untimed":
        snapshot["action_window"].update(deadline_at=None, remaining_ms=None)
    elif mode == "expired":
        snapshot["action_window"]["remaining_ms"] = 0
    if mode == "save_dialog":
        panel_state["game.save_dialog_game_id"] = GAME
    pending = Mock()
    panel = Mock()
    status = Mock()
    fragment = Mock(side_effect=lambda *, run_every: lambda function: function)
    monkeypatch.setattr(action_panel.st, "fragment", fragment)
    monkeypatch.setattr(action_panel.st, "markdown", Mock())
    monkeypatch.setattr(action_panel, "_process_pending", pending)
    monkeypatch.setattr(action_panel, "_render_speech_queue", Mock())
    monkeypatch.setattr(action_panel, "_render_vote_action", panel)
    monkeypatch.setattr(action_panel, "_render_action_status", status)
    monkeypatch.setattr(action_panel, "_mount_action_attention", Mock())
    monkeypatch.setattr(action_panel.st, "columns", Mock(return_value=[Mock(), Mock()]))
    client = Mock()
    action_panel.render(client=client, game_id=GAME, snapshot=snapshot)
    fragment.assert_not_called()
    action_panel.render_status_bar(game_id=GAME, snapshot=snapshot)
    fragment.assert_called_once_with(run_every=interval)
    pending.assert_called_once()
    if mode == "active":
        monkeypatch.setattr(action_panel, "monotonic", lambda: 1001.0)
        action_panel._render_status_bar_tick(game_id=GAME, snapshot=snapshot)
        assert status.call_args.kwargs["snapshot"]["action_window"]["remaining_ms"] == 29000
        assert snapshot["action_window"]["remaining_ms"] == 30000
        pending.assert_called_once()
        panel.assert_called_once()
    assert client.mock_calls == []


@pytest.mark.parametrize("change", ["game", "window"])
def test_new_game_or_window_does_not_reuse_previous_clock(panel_state, monkeypatch, change):
    snapshot = _action_snapshot()
    action_panel._countdown_remaining_ms(game_id=GAME, snapshot=snapshot)
    monkeypatch.setattr(action_panel, "monotonic", lambda: 1010.0)
    if change == "window":
        snapshot["action_window"]["window_id"] = OUTSIDER
    game_id = OUTSIDER if change == "game" else GAME
    assert action_panel._countdown_remaining_ms(game_id=game_id, snapshot=snapshot) == 30000


def test_free_discussion_chat_input_is_available_during_ai_slot():
    """AI 작업 예약 힌트가 Enter 제출용 자유 발언 입력을 숨기지 않는지 확인한다."""
    snapshot = _action_snapshot("DAY_DISCUSSION")
    snapshot["legal_actions"] = ["SPEAK", "SAVE_AND_EXIT"]
    snapshot["action_window"].update(kind="SPEECH", turn_player_id=TARGET,
        deadline_at="2026-09-07T00:01:45Z", remaining_ms=105_000,
        legal_actions=snapshot["legal_actions"], valid_targets=[], has_submitted=False)
    app = AppTest.from_function(_action_app, args=(snapshot, Mock())).run()
    assert not app.exception
    assert len(app.chat_input) == 1 and not app.chat_input[0].disabled
    assert app.chat_input[0].key == f"form.message.{GAME}"
    assert any("Enter로 발언" in item.value for item in app.caption)


def test_free_discussion_uses_same_draft_widget_after_another_player_speaks():
    """새 발언 window를 받아도 같은 game의 브라우저 초안 위젯을 유지한다."""

    snapshot = _action_snapshot("DAY_DISCUSSION")
    snapshot["legal_actions"] = ["SPEAK", "PASS"]
    snapshot["action_window"].update(
        kind="SPEECH",
        deadline_at="2026-09-07T00:01:45Z",
        remaining_ms=105_000,
        legal_actions=snapshot["legal_actions"],
    )
    app = AppTest.from_function(_action_app, args=(snapshot, Mock())).run()
    original_key = app.chat_input[0].key

    updated = deepcopy(snapshot)
    updated["action_window"]["window_id"] = OUTSIDER
    app.session_state["test.snapshot"] = updated
    app.run()

    assert not app.exception
    assert app.chat_input[0].key == original_key == f"form.message.{GAME}"


def test_free_discussion_chat_input_submits_message():
    """채팅 Enter가 비동기 큐에 정규화한 발언을 예약하고 정확히 한 번 전송한다."""

    snapshot = _speech_queue_snapshot()
    client = _SpeechQueueClient()
    app = AppTest.from_function(_action_app, args=(snapshot, client)).run()

    app.chat_input[0].set_value("  작성 중인 의견입니다.  ").run()

    assert not app.exception
    queue = app.session_state["game.speech_queue"]
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 1)
    assert len(client.calls) == 1
    assert client.calls[0][0]["message"] == "작성 중인 의견입니다."


@pytest.mark.parametrize("pass_allowed", [False, True])
def test_first_day_discussion_never_exposes_pass(pass_allowed):
    """첫날 PASS는 오래된 projection에 남아 있어도 숨기며 예약 입력은 유지한다."""

    snapshot = _speech_queue_snapshot()
    if not pass_allowed:
        snapshot["legal_actions"].remove("PASS")
        snapshot["action_window"]["legal_actions"].remove("PASS")
    app = AppTest.from_function(_action_app, args=(snapshot, _SpeechQueueClient())).run()

    assert not app.exception
    assert not any(button.key == "action.PASS" for button in app.button)
    assert not app.chat_input[0].disabled
    assert any("첫날에는 PASS할 수 없습니다" in item.value for item in app.caption)
    assert "PASS" not in app.session_state["test.attention_payload"]["announcement"]
    assert any("지금 할 일" in item.value and "발언을 입력하세요" in item.value for item in app.markdown)


def test_rate_limited_discussion_keeps_input_and_reservation_status():
    """서버 빈도 제한은 입력 예약을 막지 않으며 하단에서도 대기 사유를 정확히 안내한다."""

    snapshot = _speech_queue_snapshot()
    snapshot["legal_actions"] = ["SAVE_AND_EXIT"]
    snapshot["action_window"].update(legal_actions=["SAVE_AND_EXIT"], has_submitted=True)
    app = AppTest.from_function(_action_app, args=(snapshot, _SpeechQueueClient())).run()

    assert not app.exception
    assert not app.chat_input[0].disabled
    assert any("발언 제한이 풀리면 전송합니다" in item.value for item in app.markdown)


@pytest.mark.parametrize("enveloped", [False, True])
def test_legacy_speech_retry_uses_pre_submit_snapshot_to_clear_input(panel_state, enveloped):
    """기존 단일 발언의 재확인도 미정의 snapshot 없이 완료하고 이전 토론 입력을 정리한다."""

    snapshot = _speech_queue_snapshot()
    client = Mock()
    client.get_game.return_value = {"data": _action_snapshot("NIGHT_ACTION")}
    panel_state["game.latest_snapshot"] = {"data": snapshot} if enveloped else snapshot
    panel_state[f"form.message.{GAME}"] = "이미 보낸 발언"
    panel_state["game.command_pending"] = {
        "game_id": GAME, "status": "IN_FLIGHT", "idempotency_key": TARGET,
        "command": build_command(snapshot=snapshot, command_type="SPEAK", message="이미 보낸 발언"),
    }
    original = deepcopy(panel_state["game.command_pending"])

    action_panel._submit_pending(client=client, game_id=GAME)

    client.submit_command.assert_called_once_with(
        game_id=GAME, command=original["command"], idempotency_key=original["idempotency_key"],
    )
    assert panel_state["game.command_pending"]["status"] == "SUCCEEDED"
    assert panel_state[f"form.message.{GAME}"] == ""


@pytest.mark.parametrize("status", ["PENDING_TO_RENDER", "IN_FLIGHT", "RETRYABLE_UNKNOWN"])
def test_new_command_cannot_overwrite_unresolved_request(panel_state, status):
    """새 입력이 아직 확정되지 않은 요청의 본문·키를 교체하거나 추가 POST를 만들지 않는다."""

    pending = {"game_id": GAME, "status": status, "idempotency_key": TARGET,
               "command": {"type": "PASS", "expected_state_version": 12, "window_id": HUMAN}}
    panel_state["game.command_pending"] = deepcopy(pending)
    client = Mock()

    action_panel._queue_command(client=client, game_id=GAME, snapshot=_action_snapshot(),
                                command_type="SUBMIT_VOTE", target_player_id=TARGET)

    assert panel_state["game.command_pending"] == pending
    client.submit_command.assert_not_called()


def _speech_queue_snapshot(version=12, *, window_id=None):
    """서버 시각만으로 마감을 계산할 수 있는 자유 토론 합성 상태를 만든다."""

    snapshot = _action_snapshot("DAY_DISCUSSION")
    snapshot["game"].update(state_version=version, last_sequence=version + 30,
                            round=0, day_number=1)
    snapshot["legal_actions"] = ["SPEAK", "PASS"]
    snapshot["action_window"].update(
        kind="SPEECH", window_id=window_id or TARGET,
        deadline_at="2026-09-07T00:01:45Z", remaining_ms=105_000,
        legal_actions=["SPEAK", "PASS"],
    )
    return snapshot


class _SpeechQueueClient:
    """외부 API 없이 호출 순서·멱등 key·동시 전송 수를 관찰하는 fake client다."""

    user_id = HUMAN

    def __init__(self):
        self.snapshot = _speech_queue_snapshot()
        self.calls = []
        self.get_calls = 0
        self.active = 0
        self.max_active = 0
        self.on_get = None
        self.on_post = None

    def get_game(self, game_id):
        self.get_calls += 1
        if self.on_get:
            return self.on_get(self.get_calls)
        return deepcopy(self.snapshot)

    def submit_command(self, *, game_id, command, idempotency_key):
        self.calls.append((deepcopy(command), idempotency_key))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.on_post:
                return self.on_post(command, idempotency_key, len(self.calls))
            return self.receipt(command, idempotency_key)
        finally:
            self.active -= 1

    def receipt(self, command, key):
        self.snapshot = _speech_queue_snapshot(command["expected_state_version"] + 1)
        return {"data": {"command_id": key, "command_type": "SPEAK",
                         "accepted_state_version": command["expected_state_version"],
                         "result_state_version": command["expected_state_version"] + 1,
                         "sync_url": f"/api/v1/games/{GAME}/sync"}}


def _speech_queue_drive(queue, condition, *, timeout=3):
    """짧은 실제 스케줄링 양보만 사용하고 작업 완료 조건에 도달하면 즉시 끝낸다."""

    from threading import Event
    from time import monotonic

    deadline = monotonic() + timeout
    snapshots = []
    while monotonic() < deadline:
        result = queue.advance()
        if result is not None:
            snapshots.append(result)
        if condition():
            return snapshots
        Event().wait(0.002)
    pytest.fail(f"발언 큐 완료 조건을 만족하지 못했습니다: {queue.view()}")


def test_speech_queue_accepts_three_entries_during_post_and_preserves_fifo():
    """첫 HTTP가 막혀도 입력 예약은 즉시 끝나고 여러 runner가 POST를 중복 시작하지 않는다."""

    from threading import Event, Thread

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()
    started, release, added = Event(), Event(), Event()

    def post(command, key, count):
        if count == 1:
            started.set()
            assert release.wait(3)
        return client.receipt(command, key)

    client.on_post = post
    queue = SpeechQueue(client, client.snapshot)
    displayed = deepcopy(client.snapshot)
    client.snapshot = _speech_queue_snapshot(15)
    queue.enqueue("  첫   발언  ")
    queue.advance()
    assert started.wait(1)
    assert queue.matches(displayed, client.user_id)

    def enqueue_followers():
        for message in ["둘째", "셋째", "넷째"]:
            queue.enqueue(message)
            queue.advance()
        added.set()

    producer = Thread(target=enqueue_followers, daemon=True)
    producer.start()
    try:
        assert added.wait(0.5), "네트워크 대기가 입력 예약의 잠금을 점유했습니다."
        assert queue.view()["pending"] == ["첫 발언", "둘째", "셋째", "넷째"]
        runners = [Thread(target=queue.advance) for _ in range(6)]
        for runner in runners:
            runner.start()
        for runner in runners:
            runner.join(1)
        assert len(client.calls) == 1
    finally:
        release.set()
        producer.join(1)
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 4)
    assert [body["message"] for body, _ in client.calls] == ["첫 발언", "둘째", "셋째", "넷째"]
    assert client.max_active == 1
    assert not queue.busy


@pytest.mark.parametrize("code", ["STALE_STATE_VERSION", "WINDOW_CLOSED"])
def test_speech_queue_rebuilds_only_after_definite_conflict_and_fresh_get(code):
    """같은 deadline의 AI 창 교체는 원문을 유지한 새 body/key 시도로 이어진다."""

    from frontend_user.core.api_client import ApiResponseError
    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()

    def post(command, key, count):
        if count == 1:
            client.snapshot = _speech_queue_snapshot(15, window_id=OUTSIDER)
            raise ApiResponseError(status_code=409, code=code)
        return client.receipt(command, key)

    client.on_post = post
    queue = SpeechQueue(client, client.snapshot)
    queue.enqueue("그대로 보낼 의견")
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 1)
    first, second = client.calls
    assert first[1] != second[1]
    assert second[0]["expected_state_version"] == 15
    assert second[0]["window_id"] == OUTSIDER
    assert second[0]["message"] == first[0]["message"]
    assert client.get_calls >= 3


@pytest.mark.parametrize("failure", ["network", "malformed"])
def test_speech_queue_unknown_reuses_body_key_and_blocks_followers(failure):
    """응답 유실과 계약을 만족하지 않는 성공 응답 모두 같은 요청 재확인만 허용한다."""

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()
    now = [100.0]

    def post(command, key, count):
        if count == 1:
            client.snapshot = _speech_queue_snapshot(20, window_id=OUTSIDER)
            if failure == "malformed":
                return {"data": {"command_type": "SPEAK"}}
            raise ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")
        return client.receipt(command, key)

    client.on_post = post
    queue = SpeechQueue(client, client.snapshot, clock=lambda: now[0])
    queue.enqueue("첫째")
    queue.enqueue("둘째")
    _speech_queue_drive(queue, lambda: queue.view()["status"] == "UNKNOWN")
    for _ in range(5):
        queue.advance()
    assert len(client.calls) == 1
    now[0] += 2
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 2)
    assert client.calls[0] == client.calls[1]
    assert [body["message"] for body, _ in client.calls] == ["첫째", "첫째", "둘째"]


def test_speech_queue_rate_limit_waits_then_fetches_and_rebuilds():
    """발언 제한은 원문을 버리지 않고 monotonic 대기 뒤 새 상태로 다시 예약한다."""

    from frontend_user.core.api_client import ApiResponseError
    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()
    now = [100.0]

    def post(command, key, count):
        if count == 1:
            raise ApiResponseError(status_code=429, code="SPEECH_RATE_LIMITED")
        return client.receipt(command, key)

    client.on_post = post
    queue = SpeechQueue(client, client.snapshot, clock=lambda: now[0])
    queue.enqueue("대기할 발언")
    _speech_queue_drive(queue, lambda: queue.view()["status"] == "WAITING" and len(client.calls) == 1)
    now[0] += 1.9
    queue.advance()
    assert len(client.calls) == 1
    now[0] += 0.2
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 1)
    assert client.calls[0][1] != client.calls[1][1]


def test_speech_queue_success_is_not_replayed_when_followup_get_fails():
    """receipt가 검증된 성공은 조회 장애와 분리해 한 번만 완료 처리한다."""

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()

    def get(count):
        if count == 2:
            raise ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")
        return deepcopy(client.snapshot)

    client.on_get = get
    queue = SpeechQueue(client, client.snapshot)
    queue.enqueue("첫째")
    queue.enqueue("둘째")
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 2)
    assert [body["message"] for body, _ in client.calls] == ["첫째", "둘째"]


def test_speech_queue_cancel_during_get_prevents_post():
    """fresh GET이 반환되기 전에 취소되면 뒤따르는 POST는 시작되지 않는다."""

    from threading import Event

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()
    started, release = Event(), Event()

    def get(count):
        started.set()
        assert release.wait(3)
        return deepcopy(client.snapshot)

    client.on_get = get
    queue = SpeechQueue(client, client.snapshot)
    queue.enqueue("보내지 않을 발언")
    queue.advance()
    try:
        assert started.wait(1)
        queue.cancel("게임에서 나갔습니다.")
    finally:
        release.set()
    assert _speech_queue_drive(queue, lambda: not queue.busy) == []
    assert not client.calls
    assert queue.view()["status"] == "CANCELLED"


@pytest.mark.parametrize("change", ["user", "game", "phase", "saved", "dead", "deadline", "kind"])
def test_speech_queue_scope_change_discards_late_completion(change):
    """이미 시작된 POST가 끝나도 교체된 사용자·게임·토론에 결과를 적용하지 않는다."""

    from threading import Event

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()
    started, release = Event(), Event()

    def post(command, key, count):
        started.set()
        assert release.wait(3)
        return client.receipt(command, key)

    client.on_post = post
    queue = SpeechQueue(client, client.snapshot)
    queue.enqueue("전송 중인 발언")
    queue.enqueue("미전송 발언")
    queue.advance()
    try:
        assert started.wait(1)
        changed = deepcopy(client.snapshot)
        user_id = client.user_id
        if change == "user":
            user_id = OUTSIDER
        elif change == "game":
            changed["game"]["game_id"] = OUTSIDER
        elif change == "phase":
            changed["game"]["phase"] = "NIGHT_ACTION"
        elif change == "saved":
            changed["game"]["status"] = "SAVED"
        elif change == "dead":
            changed["me"]["alive"] = False
        elif change == "deadline":
            changed["action_window"]["deadline_at"] = "2026-09-07T00:02:00Z"
        else:
            changed["action_window"]["kind"] = "VOTE"
        queue.observe(changed, user_id)
    finally:
        release.set()
    assert _speech_queue_drive(queue, lambda: not queue.busy) == []
    assert len(client.calls) == 1
    assert queue.view()["pending"] == []
    assert queue.view()["status"] == "CANCELLED"


def test_speech_queue_ignores_regressing_observation_and_get():
    """cursor가 뒤로 간 GET은 POST 근거로 쓰지 않고 기존 최신 상태를 유지한다."""

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()
    now = [100.0]
    queue = SpeechQueue(client, client.snapshot, clock=lambda: now[0])
    newer = _speech_queue_snapshot(20)
    queue.observe(newer, client.user_id)
    assert queue.matches(client.snapshot, client.user_id)
    queue.observe(client.snapshot, client.user_id)
    assert queue.matches(newer, client.user_id)
    queue.enqueue("최신 상태를 기다릴 발언")
    _speech_queue_drive(queue, lambda: queue.view()["status"] == "WAITING" and client.get_calls > 0)
    assert not client.calls
    client.snapshot = newer
    now[0] += 2
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 1)
    assert client.calls[0][0]["expected_state_version"] == 20


def test_speech_queue_deadline_uses_server_remaining_and_monotonic():
    """예전 날짜의 합성 서버 시각에도 브라우저 시계와 무관하게 잔여 시간만 소진한다."""

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()
    now = [100.0]
    queue = SpeechQueue(client, client.snapshot, clock=lambda: now[0])
    queue.enqueue("마감 후 보내면 안 되는 발언")
    now[0] += 106
    queue.observe(client.snapshot, client.user_id)
    queue.advance()
    assert queue.view()["status"] == "CANCELLED"
    assert not client.calls


def test_speech_queue_permanent_rejection_removes_only_rejected_entry():
    """확정 영구 거부는 그 발언만 제거하고 뒤의 발언을 원래 순서로 처리한다."""

    from frontend_user.core.api_client import ApiResponseError
    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()

    def post(command, key, count):
        if count == 1:
            raise ApiResponseError(status_code=422, code="VALIDATION_ERROR")
        return client.receipt(command, key)

    client.on_post = post
    queue = SpeechQueue(client, client.snapshot)
    queue.enqueue("거부될 발언")
    queue.enqueue("다음 발언")
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 1)
    assert len(client.calls) == 2
    assert not queue.busy


@pytest.mark.parametrize("limited_at_creation", [False, True])
@pytest.mark.parametrize("next_state", ["allowed", "expired"])
def test_speech_queue_missing_speak_during_rate_limit_keeps_reservations(limited_at_creation, next_state):
    """7회 제한의 제출 완료 표시는 8·9번 예약을 유지하고, 실제 마감만 예약을 취소한다."""

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()
    limited = deepcopy(client.snapshot)
    limited["legal_actions"] = ["SAVE_AND_EXIT"]
    limited["action_window"].update(legal_actions=["SAVE_AND_EXIT"], has_submitted=True)
    if limited_at_creation:
        client.snapshot = limited
    now = [100.0]
    queue = SpeechQueue(client, client.snapshot, clock=lambda: now[0])
    messages = ["8번째 예약 발언", "9번째 예약 발언"]
    for message in messages:
        queue.enqueue(message)
    client.snapshot = limited
    queue.observe(client.snapshot, client.user_id)
    _speech_queue_drive(queue, lambda: client.get_calls == 1 and queue.view()["status"] == "WAITING")
    assert queue.view()["pending"] == messages
    assert not client.calls
    if next_state == "expired":
        client.snapshot["action_window"]["remaining_ms"] = 0
        queue.observe(client.snapshot, client.user_id)
        queue.advance()
        assert queue.view()["status"] == "CANCELLED"
        assert queue.view()["pending"] == []
        assert queue.view()["completed"] == 0
        assert not client.calls
        return
    client.snapshot["legal_actions"] = ["SPEAK", "PASS"]
    client.snapshot["action_window"].update(legal_actions=["SPEAK", "PASS"], has_submitted=False)
    now[0] += 2
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 2)
    assert [command["message"] for command, _ in client.calls] == messages
    assert len({key for _, key in client.calls}) == 2
    assert queue.view()["pending"] == []
    assert not queue.busy


def test_speech_queue_stale_phase_get_does_not_cancel_current_scope():
    """cursor가 오래된 이전 phase 응답은 실제 phase 전환으로 오해하지 않는다."""

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()
    initial = _speech_queue_snapshot(20)
    client.snapshot["game"]["phase"] = "NIGHT_ACTION"
    now = [100.0]
    queue = SpeechQueue(client, initial, clock=lambda: now[0])
    queue.enqueue("현재 토론의 발언")
    _speech_queue_drive(queue, lambda: client.get_calls == 1 and queue.view()["status"] == "WAITING")
    assert queue.matches(initial, client.user_id)
    assert not client.calls
    client.snapshot = initial
    now[0] += 2
    _speech_queue_drive(queue, lambda: queue.view()["completed"] == 1)


def test_speech_queue_success_then_new_phase_counts_success_and_cancels_followers():
    """성공 뒤 실제 phase 전환은 성공 receipt를 보존하고 미전송 follower만 없앤다."""

    from frontend_user.core.commands import SpeechQueue

    client = _SpeechQueueClient()

    def post(command, key, count):
        receipt = client.receipt(command, key)
        client.snapshot["game"]["phase"] = "NIGHT_ACTION"
        return receipt

    client.on_post = post
    queue = SpeechQueue(client, client.snapshot)
    queue.enqueue("이미 제출한 발언")
    queue.enqueue("취소할 예약")
    snapshots = _speech_queue_drive(queue, lambda: queue.view()["status"] == "CANCELLED")
    assert queue.view()["completed"] == 1
    assert queue.view()["pending"] == []
    assert len(client.calls) == 1
    assert snapshots[-1]["game"]["phase"] == "NIGHT_ACTION"


def _custom_night_snapshot():
    """B16 본인 능력별 대상 계약을 외부 Backend 없이 재현한다."""

    snapshot = _action_snapshot("NIGHT_ACTION")
    snapshot["game"]["mode"] = "CUSTOM_ROLE"
    snapshot["me"].update(role_name="감식관", faction="CITIZEN",
        ability_ids=["night.investigate.v1", "night.protect.v1"], ability_options=[
            {"ability_id": "night.investigate.v1", "label": "조사", "valid_targets": [{"player_id": TARGET, "display_name": "후보"}]},
            {"ability_id": "night.protect.v1", "label": "보호", "valid_targets": [{"player_id": HUMAN, "display_name": "나"}]},
        ])
    return snapshot


def test_custom_command_requires_owned_ability_and_standard_omits_it():
    snapshot = _custom_night_snapshot()
    with pytest.raises(ValueError):
        build_command(snapshot=snapshot, command_type="SUBMIT_NIGHT_ACTION", target_player_id=TARGET)
    with pytest.raises(ValueError):
        build_command(snapshot=snapshot, command_type="SUBMIT_NIGHT_ACTION", target_player_id=TARGET, ability_id="night.attack.v1")
    command = build_command(snapshot=snapshot, command_type="SUBMIT_NIGHT_ACTION", target_player_id=TARGET, ability_id="night.investigate.v1")
    assert command["ability_id"] == "night.investigate.v1"
    snapshot["game"]["mode"] = "STANDARD"
    assert "ability_id" not in build_command(snapshot=snapshot, command_type="SUBMIT_NIGHT_ACTION", target_player_id=TARGET, ability_id="night.investigate.v1")


def test_custom_night_switch_uses_only_selected_ability_targets_and_survives_rerun():
    app = AppTest.from_function(_action_app, args=(_custom_night_snapshot(), Mock())).run()
    assert not app.exception
    ability_key = app.radio[0].key
    app.radio(key=ability_key).set_value("night.investigate.v1").run()
    target_key = next(item.key for item in app.radio if item.label == "대상 선택")
    assert app.radio(key=target_key).options == ["🔵 후보"]
    app.radio(key=target_key).set_value(TARGET).run()
    app.run()
    assert app.radio(key=target_key).value == TARGET
    app.radio(key=ability_key).set_value("night.protect.v1").run()
    target = next(item for item in app.radio if item.label == "대상 선택")
    assert target.options == ["🔵 나"]
    assert target.value is None
    assert not app.exception


def test_custom_queue_rejects_target_from_other_ability(panel_state):
    snapshot = _custom_night_snapshot()
    action_panel._queue_command(game_id=GAME, snapshot=snapshot, command_type="SUBMIT_NIGHT_ACTION",
                                ability_id="night.investigate.v1", target_player_id=HUMAN, rerun=False)
    assert panel_state.get("game.command_pending", {}).get("status") != "PENDING_TO_RENDER"


@pytest.mark.parametrize("value", ["", "가" * 41, "줄\n바꿈", "숨김\x00"])
def test_custom_role_name_rejects_invalid_input(value):
    from frontend_user.core.commands import normalize_role_name

    with pytest.raises(ValueError):
        normalize_role_name(value)


def test_custom_night_save_resume_preserves_choice_and_submits_one_ability():
    """같은 행동 창의 저장·재개에서 선택을 유지하고 결과 불명 요청을 고정한다."""

    snapshot = _custom_night_snapshot()
    client = Mock()
    client.submit_command.side_effect = ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")
    app = AppTest.from_function(_action_app, args=(snapshot, client)).run()
    ability_key = app.radio[0].key
    app.radio(key=ability_key).set_value("night.protect.v1").run()
    target_key = next(item.key for item in app.radio if item.label == "대상 선택")
    app.radio(key=target_key).set_value(HUMAN).run()
    app.session_state["test.snapshot"]["game"]["status"] = "SAVED"
    app.run()
    assert app.radio(key=target_key).value == HUMAN
    assert app.radio(key=target_key).disabled
    app.session_state["test.snapshot"]["game"]["status"] = "IN_PROGRESS"
    app.run()
    assert app.radio(key=target_key).value == HUMAN
    app.button(key="action.SUBMIT_NIGHT_ACTION").click().run()
    assert client.submit_command.call_args.kwargs["command"]["ability_id"] == "night.protect.v1"
    assert client.submit_command.call_args.kwargs["command"]["target_player_id"] == HUMAN
    assert app.session_state["game.command_pending"]["status"] == "RETRYABLE_UNKNOWN"
    assert not app.exception


def _triple_snapshot(phase="DAY_VOTE"):
    """HUMAN 소유 능력과 공개 좌석을 같은 합성 snapshot에 묶는다."""

    snapshot = _action_snapshot(phase)
    snapshot["game"]["mode"] = "CUSTOM_ROLE"
    snapshot["me"].update(role_name="투표관", faction="CITIZEN", ability_ids=["vote.triple.v1"])
    snapshot["players"][0]["kind"] = "HUMAN"
    return snapshot


@pytest.mark.parametrize("phase", ["DAY_VOTE", "REVOTE"])
@pytest.mark.parametrize("choice", ["일반 1표", "능력 3표"])
def test_triple_vote_explicit_choice_rerun_and_unknown_request_lock(phase, choice):
    snapshot = _triple_snapshot(phase)
    client = Mock()
    client.submit_command.side_effect = ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")
    app = AppTest.from_function(_action_app, args=(snapshot, client)).run()
    target_key = f"form.vote_target.{GAME}.{snapshot['action_window']['window_id']}"
    ability_key = f"form.vote_ability.{GAME}.{snapshot['action_window']['window_id']}"
    app.radio(key=target_key).set_value(TARGET).run()
    assert app.button(key="action.SUBMIT_VOTE").disabled
    app.radio(key=ability_key).set_value(choice).run()
    app.run()
    assert app.radio(key=ability_key).value == choice
    app.button(key="action.SUBMIT_VOTE").click().run()
    assert not app.exception
    first = deepcopy(client.submit_command.call_args.kwargs)
    assert first["command"].get("ability_id") == ("vote.triple.v1" if choice == "능력 3표" else None)
    assert "weight" not in first["command"]
    assert app.radio(key=ability_key).disabled
    app.run()
    assert client.submit_command.call_count == 1
    retry = next(button for button in app.button if "다시" in button.label)
    retry.click().run()
    assert client.submit_command.call_args.kwargs == first


@pytest.mark.parametrize("denied", ["final", "standard", "unowned", "dead", "ai"])
def test_triple_vote_denied_ui_and_command(denied):
    snapshot = _triple_snapshot()
    if denied == "final":
        snapshot = _triple_snapshot("FINAL_ACCUSATION")
    elif denied == "standard":
        snapshot["game"]["mode"] = "STANDARD"
    elif denied == "unowned":
        snapshot["me"]["ability_ids"] = []
    elif denied == "dead":
        snapshot["me"]["alive"] = False
    else:
        snapshot["players"][0]["kind"] = "AI"
    app = AppTest.from_function(_action_app, args=(snapshot, Mock())).run()
    assert not app.exception
    assert all(radio.label != "투표 방식" for radio in app.radio)
    assert not any("투표 조작" in item.value or "vote.triple.v1" in item.value for item in app.text)
    with pytest.raises(ValueError):
        build_command(snapshot=snapshot, command_type="SUBMIT_VOTE", target_player_id=TARGET, ability_id="vote.triple.v1")
    assert "ability_id" not in build_command(snapshot=snapshot, command_type="SUBMIT_VOTE", target_player_id=TARGET)


def test_day_only_custom_has_no_night_ability_options():
    from frontend_user.core.commands import custom_ability_options

    snapshot = _triple_snapshot("NIGHT_ACTION")
    snapshot["me"]["ability_options"] = [{"ability_id": "vote.triple.v1", "label": "投票", "valid_targets": []}]
    assert custom_ability_options(snapshot) == []
