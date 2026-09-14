from copy import deepcopy

import pytest
from streamlit.testing.v1 import AppTest

from frontend_user.app_pages import game_page
from frontend_user.components import theme
from frontend_user.core.api_client import ApiResponseError, ApiUnavailableError
from frontend_user.core.view_models import own_private_view, public_players

GAME = "00000000-0000-4000-8000-000000000201"
AI = "00000000-0000-4000-8000-000000000202"
HUMAN = "00000000-0000-4000-8000-000000000203"
RUN = "00000000-0000-4000-8000-000000000204"


@pytest.fixture(autouse=True)
def _isolate_action_attention_component(monkeypatch):
    """F3는 화면·저장 흐름을 검증하므로 브라우저 접근성 전송만 격리한다.

    공통 하단 시계는 그대로 실행하며 주의 안내 payload와 fragment 경계는 F4·F5가
    검증한다. 수집 때 등록된 component가 새 AppTest runtime에 남는다고 가정하지 않는다.
    """

    monkeypatch.setattr("frontend_user.components.action_panel.ACTION_ATTENTION_COMPONENT",
                        lambda **kwargs: {})


def _snapshot(status="IN_PROGRESS", phase="DAY_DISCUSSION", version=12):
    return {
        "game": {"game_id": GAME, "status": status, "phase": phase,
                 "state_version": version, "last_sequence": 0},
        "players": [
            {"player_id": AI, "display_name": "<img src=x onerror=alert(1)>",
             "kind": "AI", "alive": True, "seat": 2},
            {"player_id": HUMAN, "display_name": "사람", "kind": "HUMAN", "alive": True, "seat": 1},
        ],
        "me": {"player_id": HUMAN, "alive": True, "role": "CITIZEN"},
        "scenario": {},
        "action_window": {"turn_player_id": AI, "kind": "SPEECH", "paused": status == "SAVED"},
        "legal_actions": ["RESUME"] if status == "SAVED" else ["SAVE_AND_EXIT", "BEGIN_GAME"],
        "agent_activity": [_activity()],
    }


def _activity(**changes):
    return {"sequence": 1, "run_id": RUN, "created_at": "2026-09-07T01:00:00Z",
            "player_id": AI, "phase": "DAY_DISCUSSION", "state_version": 12,
            "stage": "DECIDING", "action": None, "summary": "허용된 행동을 선택하고 있습니다.",
            **changes}


@pytest.mark.parametrize("changes", [
    {"stage": "SECRET"}, {"stage": []}, {"action": "ATTACK"}, {"action": {}},
    {"player_id": HUMAN}, {"player_id": "00000000-0000-4000-8000-000000000999"},
    {"player_id": "bad"}, {"run_id": "bad"}, {"sequence": True}, {"sequence": -1},
    {"state_version": True}, {"state_version": 13}, {"phase": "NIGHT_ACTION"},
    {"phase": []}, {"created_at": "bad"}, {"created_at": "2026-09-07T10:00:00+09:00"},
    {"summary": "가" * 201}, {"rationale": "비공개 사고"},
])
def test_activity_rejects_invalid_contract(changes):
    snapshot = _snapshot()
    snapshot["agent_activity"] = [_activity(**changes)]
    assert game_page._validated_agent_activity(snapshot) == []


def test_activity_accepts_history_but_only_current_turn_version_is_live():
    snapshot = _snapshot()
    snapshot["agent_activity"] = [_activity(stage="APPLIED", action="PASS", state_version=11)]
    records = game_page._validated_agent_activity(snapshot)
    assert records[0]["stage"] == "APPLIED"
    assert not game_page._is_current_activity(records[0], snapshot)
    assert game_page._is_current_activity(_activity(), snapshot)
    snapshot["action_window"]["turn_player_id"] = HUMAN
    assert not game_page._is_current_activity(_activity(), snapshot)


def _activity_app(snapshot):
    from frontend_user.app_pages.game_page import _render_agent_activity
    _render_agent_activity(snapshot=snapshot)


@pytest.mark.parametrize("phase", ["NIGHT_ACTION", "NIGHT_RESOLUTION", "DAY_VOTE", "REVOTE", "FINAL_ACCUSATION"])
def test_private_phase_activity_has_no_actor_or_history(phase):
    app = AppTest.from_function(_activity_app, args=(_snapshot(phase=phase),)).run()
    assert not app.exception
    text = " ".join(item.value for item in app.markdown) + " ".join(item.value for item in app.info)
    assert "비공개" in text
    assert "img" not in text and "판단 중" not in text
    assert len(app.expander) == 1
    assert app.expander[0].label == "AI 판단과 실행"
    assert app.expander[0].proto.expanded is False


def test_activity_escapes_names_and_does_not_render_provider_summary():
    snapshot = _snapshot()
    snapshot["agent_activity"] = [_activity(summary="비공개 사고 <script>secret</script>")]
    app = AppTest.from_function(_activity_app, args=(snapshot,)).run()
    assert not app.exception
    text = " ".join(item.value for item in app.markdown)
    assert "&lt;img" in text and "<img" not in text
    assert "secret" not in text and "판단 중" in text
    assert app.expander[0].proto.expanded is False


def test_missing_activity_is_empty_and_eliminated_ai_is_distinct():
    snapshot = _snapshot()
    snapshot.pop("agent_activity")
    snapshot["players"][0]["alive"] = False
    assert game_page._validated_agent_activity(snapshot) == []
    app = AppTest.from_function(_activity_app, args=(snapshot,)).run()
    assert not app.exception
    assert "탈락" in " ".join(item.value for item in app.markdown)


class _Client:
    def __init__(self, snapshot, failures=(), refresh_failures=0):
        self.snapshot = deepcopy(snapshot)
        self.failures = list(failures)
        self.refresh_failures = refresh_failures
        self.calls = []
        self.reads = 0
        self.delete_calls = []

    def delete_game(self, **kwargs):
        """실제 게임 없이 삭제 성공·응답 유실·거부를 재현한다."""

        self.delete_calls.append(deepcopy(kwargs))
        if self.failures:
            raise self.failures.pop(0)
        return {"data": {"game_id": kwargs["game_id"], "deleted": True}}

    def submit_command(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if self.failures:
            raise self.failures.pop(0)
        kind = kwargs["command"]["type"]
        self.snapshot["game"]["status"] = "SAVED" if kind == "SAVE_AND_EXIT" else "IN_PROGRESS"
        self.snapshot["game"]["state_version"] += 1
        if kind == "BEGIN_GAME":
            self.snapshot["game"]["phase"] = "DAY_DISCUSSION"
        self.snapshot["legal_actions"] = ["RESUME"] if kind == "SAVE_AND_EXIT" else ["SAVE_AND_EXIT"]
        return {"data": {"result_state_version": self.snapshot["game"]["state_version"]}}

    def get_game(self, game_id):
        self.reads += 1
        assert game_id == GAME
        if self.refresh_failures:
            self.refresh_failures -= 1
            raise ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")
        return {"data": deepcopy(self.snapshot)}


def _command_app(client, snapshot, command_type):
    import streamlit as st
    from frontend_user.app_pages.game_page import _render_shell_command
    st.session_state.setdefault("game.game_id", snapshot["game"]["game_id"])
    st.session_state.setdefault("navigation.page", "game")
    current = st.session_state.get("game.latest_snapshot", snapshot)
    _render_shell_command(client=client, game_id=snapshot["game"]["game_id"],
                          snapshot=current, command_type=command_type)


@pytest.mark.parametrize("kind", ["SAVE_AND_EXIT", "RESUME", "BEGIN_GAME"])
def test_shell_command_unknown_retries_original_body_key(kind):
    snapshot = _snapshot(status="SAVED" if kind == "RESUME" else "IN_PROGRESS")
    client = _Client(snapshot, [ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")])
    app = AppTest.from_function(_command_app, args=(client, snapshot, kind)).run()
    app.session_state["home.games"] = ["stale"]
    app.session_state["home.games_loaded_at"] = 12
    key = game_page.SHELL_COMMANDS[kind][1]
    app.button(key=key).click().run()
    assert not app.exception and len(client.calls) == 1
    assert app.button(key=key).disabled
    snapshot["game"]["state_version"] = 99
    app.button(key=f"{key}_retry").click().run()
    assert not app.exception and len(client.calls) == 2
    assert client.calls[0] == client.calls[1]
    assert "home.games" not in app.session_state
    assert "home.games_loaded_at" not in app.session_state
    if kind == "SAVE_AND_EXIT":
        assert app.session_state["navigation.page"] == "home"
        assert "game.latest_snapshot" not in app.session_state
    else:
        assert app.session_state["game.latest_snapshot"]["game"]["status"] == "IN_PROGRESS"


def test_resume_refresh_failure_retries_get_without_resubmitting_command():
    snapshot = _snapshot(status="SAVED")
    client = _Client(snapshot, refresh_failures=1)
    app = AppTest.from_function(_command_app, args=(client, snapshot, "RESUME")).run()
    app.button(key="game.resume").click().run()
    assert not app.exception and len(client.calls) == 1
    app.button(key="game.resume_retry").click().run()
    assert not app.exception and len(client.calls) == 1 and client.reads == 2
    assert app.session_state["game.latest_snapshot"]["game"]["status"] == "IN_PROGRESS"


def test_stale_resume_reads_latest_state_without_automatic_new_command():
    snapshot = _snapshot(status="SAVED")
    client = _Client(_snapshot(version=13), [ApiResponseError(status_code=409, code="STALE_STATE_VERSION")])
    app = AppTest.from_function(_command_app, args=(client, snapshot, "RESUME")).run()
    app.button(key="game.resume").click().run()
    assert not app.exception and len(client.calls) == 1 and client.reads == 1
    assert app.session_state["game.latest_snapshot"]["game"]["status"] == "IN_PROGRESS"


def _page_app(client, snapshot, tick=None, no_envelope=False, hidden=False, component_failed=False,
              sync_envelopes=None):
    import streamlit as st
    from types import SimpleNamespace
    from unittest.mock import patch
    from frontend_user.app_pages import game_page, role_reveal_page
    st.session_state["game.client"] = client
    st.session_state.setdefault("navigation.page", "game")
    if st.session_state["navigation.page"] == "home":
        st.write("홈으로 이동됨")
        return
    st.session_state.setdefault("game.game_id", snapshot["game"]["game_id"])
    current = st.session_state.get("game.latest_snapshot", snapshot)
    if current["game"]["phase"] == "ROLE_REVEAL":
        role_reveal_page.render(current)
    else:
        client.config = SimpleNamespace(api_url="http://127.0.0.1:1")
        client.user_id = "00000000-0000-4000-8000-000000000203"
        def mount(**kwargs):
            assert kwargs["after_state_version"] == current["game"]["state_version"]
            st.session_state["game.sync_status"] = "LIVE"
            st.session_state["game.sync_hidden"] = hidden
            st.session_state["game.sync_component_failed"] = component_failed
            if tick is not None:
                st.session_state["game.sync_tick"] = tick
            if sync_envelopes is not None:
                return sync_envelopes.pop(0) if sync_envelopes else None
            return None if no_envelope else {"data": {"mode": "DELTA", "operations": []}}
        with patch.object(game_page, "mount_sse", side_effect=mount), patch.object(
            game_page, "apply_sync", side_effect=(game_page.apply_sync if sync_envelopes is not None
                                                 else lambda **kwargs: kwargs["snapshot"])
        ), patch.object(game_page, "render_action_panel") as action_panel:
            game_page.render(current)
            if current["game"]["status"] == "SAVED" or hidden:
                action_panel.assert_not_called()


@pytest.mark.parametrize("phase", ["ROLE_REVEAL", "DAY_DISCUSSION", "NIGHT_ACTION"])
def test_saved_page_exposes_resume_without_running_action_panel(phase):
    snapshot = _snapshot(status="SAVED", phase=phase)
    if phase == "ROLE_REVEAL":
        snapshot["action_window"] = None
    client = _Client(snapshot)
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    assert not app.exception
    assert len(client.calls) == 0
    assert app.button(key="game.resume").disabled is False
    app.button(key="game.resume").click().run()
    assert not app.exception and client.calls[0]["command"]["type"] == "RESUME"
    assert app.session_state["game.latest_snapshot"]["game"]["status"] == "IN_PROGRESS"
    assert app.session_state["game.latest_snapshot"]["game"]["phase"] == phase


def test_shell_tick_refreshes_activity_without_overwriting_connection_state():
    snapshot = _snapshot()
    updated = deepcopy(snapshot)
    updated["agent_activity"] = [_activity(stage="DECIDED", action="SPEAK")]
    client = _Client(updated)
    app = AppTest.from_function(_page_app, args=(client, snapshot, "tick-1")).run()
    assert not app.exception and client.reads == 1
    assert app.session_state["game.sync_status"] == "LIVE"
    assert "행동 선택 완료" in " ".join(item.value for item in app.markdown)
    app.run()
    assert not app.exception and client.reads == 1


@pytest.mark.parametrize("activity", [
    [_activity(stage="DECIDED", action="SPEAK", run_id=HUMAN)], [], None,
])
def test_snapshot_sync_same_tick_refreshes_activity_once_without_retaining_old_run(activity):
    """실제 reducer가 활동 없는 snapshot으로 교체해도 GET의 새 run·빈 기록을 따른다."""

    snapshot = _snapshot()
    replacement = deepcopy(snapshot)
    replacement.pop("agent_activity")
    replacement["game"].update(state_version=13, last_sequence=1)
    latest = deepcopy(replacement)
    if activity is not None:
        latest["agent_activity"] = activity
    client = _Client(latest)
    envelopes = [{"data": {"game_id": GAME, "mode": "SNAPSHOT", "snapshot": replacement}}]
    app = AppTest.from_function(_page_app, args=(client, snapshot, 1, False, False, False, envelopes))
    app.session_state["game.activity_tick"] = 1
    app.run()
    assert not app.exception and client.reads == 1
    assert app.session_state["game.latest_snapshot"] == latest
    assert app.session_state["game.sync_status"] == "LIVE"
    captions = " ".join(item.value for item in app.caption)
    assert ("최근 처리 기록이 없습니다" in captions) is (not bool(activity))
    app.run()
    assert not app.exception and client.reads == 1
    assert app.session_state["game.latest_snapshot"] == latest


@pytest.mark.parametrize("section,changes", [
    ("game", {"game_id": AI}), ("me", {"player_id": AI}),
    ("game", {"state_version": 12}), ("game", {"last_sequence": 0}),
    ("game", {"state_version": True}),
])
def test_snapshot_sync_rejects_get_with_different_scope_or_reversed_cursor(section, changes):
    """보강 GET의 다른 게임·본인 또는 역행 버전이 검증된 sync 상태를 덮지 않는다."""

    snapshot = _snapshot()
    replacement = deepcopy(snapshot)
    replacement.pop("agent_activity")
    replacement["game"].update(state_version=13, last_sequence=1)
    invalid = deepcopy(replacement)
    invalid[section].update(changes)
    invalid["agent_activity"] = [_activity(stage="APPLIED", action="PASS")]
    client = _Client(invalid)
    envelopes = [{"game_id": GAME, "mode": "SNAPSHOT", "snapshot": replacement}]
    app = AppTest.from_function(_page_app, args=(client, snapshot, 1, False, False, False, envelopes))
    app.session_state["game.activity_tick"] = 1
    app.run()
    assert not app.exception and client.reads == 1
    assert app.session_state["game.latest_snapshot"] == replacement


@pytest.mark.parametrize("spectating", [False, True])
def test_public_chat_excludes_saved_and_resumed_events(spectating):
    snapshot = _snapshot()
    snapshot["me"]["alive"] = not spectating
    snapshot["public_events"] = [{
        "event_id": event_id, "event_type": event_type,
        "created_at": "2026-09-07T01:00:00Z",
        "data": {"phase": "DAY_DISCUSSION", "round": 0},
    } for event_id, event_type in [(GAME, "GAME_SAVED"), (RUN, "GAME_RESUMED")]]
    app = AppTest.from_function(_page_app, args=(_Client(snapshot), snapshot)).run()
    assert not app.exception
    text = " ".join(item.value for item in [*app.markdown, *app.info])
    assert game_page._player_conversation_events(snapshot) == []
    assert "게임을 저장했습니다" not in text
    assert "게임을 재개했습니다" not in text


def test_public_chat_keeps_turn_speech_and_pass_in_order():
    snapshot = _snapshot()
    snapshot["public_events"] = [
        {"event_type": "GAME_BEGAN", "data": {"message": "GM 브리핑"}},
        {"event_type": "TURN_OPENED", "data": {"player_id": AI}},
        {"event_type": "PLAYER_SPOKE", "data": {"player_id": AI, "message": "공개 발언"}},
        {"event_type": "PLAYER_PASSED", "data": {"player_id": HUMAN}},
        {"event_type": "NIGHT_RESOLVED", "data": {"killed_player_id": AI}},
    ]
    assert [event["event_type"] for event in game_page._player_conversation_events(snapshot)] == [
        "TURN_OPENED",
        "PLAYER_SPOKE",
        "PLAYER_PASSED",
    ]


@pytest.mark.parametrize("event_type", ["GAME_SAVED", "GAME_RESUMED"])
def test_save_resume_copy_does_not_render_extra_payload_or_invalid_timestamp(event_type):
    event = {"event_type": event_type, "created_at": "<script>비공개 시각</script>",
             "data": {"phase": "DAY_DISCUSSION", "round": 0,
                      "message": "비공개 메시지", "target_player_id": AI}}
    text = game_page._event_text(event=event, player_names={})
    assert "비공개" not in text and "script" not in text and AI not in text
    assert "저장" in text


@pytest.mark.parametrize(
    ("phase", "tied", "needs_revote", "outcome"),
    [
        ("DAY_VOTE", False, False, "동률 없이 집계가 확정됐습니다."),
        ("DAY_VOTE", True, True, "최다 득표 동률 · 재투표를 진행합니다."),
        ("REVOTE", True, False, "최다 득표 동률 · 재투표 없이 다음 단계로 진행합니다."),
        ("FINAL_ACCUSATION", True, False, "최다 득표 동률 · 재투표 없이 다음 단계로 진행합니다."),
    ],
)
def test_vote_resolved_shows_public_counts_and_tie_outcome(phase, tied, needs_revote, outcome):
    counts = (
        [{"target_player_id": HUMAN, "vote_count": 2},
         {"target_player_id": AI, "vote_count": 0}]
        if not tied
        else [{"target_player_id": HUMAN, "vote_count": 1},
              {"target_player_id": AI, "vote_count": 1}]
    )
    text = game_page._event_text(
        event={"event_type": "VOTE_RESOLVED", "data": {
            "round": 1, "phase": phase, "counts": counts,
            "tied": tied, "needs_revote": needs_revote,
        }},
        player_names={HUMAN: "사람", AI: "플레이어 2"},
    )
    assert "사람 2표, 플레이어 2 0표" in text or "사람 1표, 플레이어 2 1표" in text
    assert outcome in text


@pytest.mark.parametrize(
    "changes",
    [
        {"ballots": []},
        {"message": "검증을 우회하려는 비공개 자유 문구"},
        {"round": True},
        {"phase": "NIGHT_ACTION"},
        {"tied": "false"},
        {"needs_revote": True},
        {"counts": [{"target_player_id": AI, "vote_count": -1}]},
        {"counts": [{"target_player_id": RUN, "vote_count": 1}]},
        {"counts": [{"target_player_id": AI, "vote_count": 1},
                    {"target_player_id": AI, "vote_count": 1}]},
    ],
)
def test_vote_resolved_rejects_noncanonical_or_private_payload(changes):
    data = {"round": 1, "phase": "DAY_VOTE", "counts": [
        {"target_player_id": HUMAN, "vote_count": 2},
        {"target_player_id": AI, "vote_count": 0},
    ], "tied": False, "needs_revote": False}
    data.update(changes)
    text = game_page._event_text(
        event={"event_type": "VOTE_RESOLVED", "data": data},
        player_names={HUMAN: "사람", AI: "플레이어 2"},
    )
    assert text == "공개 투표 집계를 확인할 수 없습니다."
    assert RUN not in text and "ballot" not in text


@pytest.mark.parametrize("phase", ["NIGHT_ACTION", "DAY_VOTE", "REVOTE", "FINAL_ACCUSATION"])
def test_every_active_phase_keeps_public_timeline_visible(phase):
    """GM 브리핑과 분리된 실제 공개 발언이 밤·각 투표 단계에서도 유지되는지 확인한다."""

    snapshot = _snapshot(phase=phase)
    snapshot["game"].update(
        day_number=6 if phase == "FINAL_ACCUSATION" else 1 if phase == "NIGHT_ACTION" else 2,
        round=5 if phase == "FINAL_ACCUSATION" else 1,
    )
    snapshot["public_events"] = [{
        "event_id": RUN,
        "event_type": "PLAYER_SPOKE",
        "created_at": "2026-09-07T01:00:00Z",
        "data": {"player_id": AI, "message": "공개 타임라인 유지 확인"},
    }]
    app = AppTest.from_function(_page_app, args=(_Client(snapshot), snapshot)).run()
    assert not app.exception
    assert "공개 타임라인 유지 확인" in " ".join(
        item.value for item in [*app.markdown, *app.info, *app.success]
    )


def test_selected_player_summary_uses_only_selected_public_speeches():
    snapshot = _snapshot()
    snapshot["public_events"] = [
        {"event_type": "PLAYER_SPOKE", "data": {"player_id": AI, "message": "AI 공개 발언"}},
        {"event_type": "PLAYER_SPOKE", "data": {"player_id": HUMAN, "message": "내 공개 발언"}},
        {"event_type": "NIGHT_RESOLVED", "data": {"killed_player_id": AI}},
    ]
    assert game_page._selected_player_speeches(
        snapshot=snapshot,
        selected_player_id=AI,
        player_names={AI: "AI", HUMAN: "사람"},
    ) == ["AI 공개 발언"]


def test_begin_unknown_retry_remains_visible_after_server_phase_change():
    snapshot = _snapshot()
    client = _Client(snapshot)
    app = AppTest.from_function(_page_app, args=(client, snapshot))
    app.session_state["game.begin_pending"] = {
        "game_id": GAME, "status": "RETRYABLE_UNKNOWN", "expected_state_version": 1,
        "idempotency_key": RUN,
    }
    app.run()
    assert not app.exception and app.button(key="game.begin").disabled
    app.button(key="game.begin_retry").click().run()
    assert not app.exception and len(client.calls) == 1
    assert client.calls[0]["idempotency_key"] == RUN
    assert client.calls[0]["command"]["expected_state_version"] == 1


def test_saved_page_keeps_unknown_save_request_and_locks_resume():
    snapshot = _snapshot(status="SAVED")
    client = _Client(snapshot)
    app = AppTest.from_function(_page_app, args=(client, snapshot))
    app.session_state["game.save_pending"] = {
        "game_id": GAME, "status": "RETRYABLE_UNKNOWN", "expected_state_version": 11,
        "idempotency_key": RUN,
    }
    app.run()
    assert not app.exception and app.button(key="game.resume").disabled
    app.button(key="game.save_exit_retry").click().run()
    assert not app.exception and len(client.calls) == 1
    assert app.session_state["navigation.page"] == "home"


@pytest.mark.parametrize("changes", [
    {"agent_activity": None},
    {"agent_activity": [_activity(), _activity(sequence=1)]},
    {"agent_activity": [_activity(), _activity(sequence=2, run_id=GAME)]},
])
def test_activity_rejects_invalid_collection_and_mixed_runs(changes):
    assert game_page._validated_agent_activity(_snapshot() | changes) == []


def test_activity_history_is_bounded_to_fifty_items():
    snapshot = _snapshot()
    snapshot["agent_activity"] = [_activity(sequence=n) for n in range(1, 70)]
    records = game_page._validated_agent_activity(snapshot)
    assert len(records) == 50 and records[0]["sequence"] == 20


@pytest.mark.parametrize("stage,action,summary,expected", [
    ("DECIDING", None, "더미 제공자의 고정 행동을 처리하고 있습니다.", True),
    ("DECIDED", "SPEAK", "더미 제공자의 고정 행동을 처리하고 있습니다. (발언)", True),
    ("DECIDED", "PASS", "더미 제공자의 고정 행동을 처리하고 있습니다. (차례 넘김)", True),
    ("DECIDED", "SPEAK", "더미 제공자의 고정 행동을 처리하고 있습니다. (차례 넘김)", False),
    ("DECIDING", None, "더미 제공자의 고정 행동을 처리하고 있습니다. 내부 사고 원문", False),
    ("APPLIED", "PASS", "더미 제공자의 고정 행동을 처리하고 있습니다.", False),
])
def test_only_exact_dummy_summary_is_projected_as_fixed_action(stage, action, summary, expected):
    snapshot = _snapshot()
    snapshot["agent_activity"] = [_activity(stage=stage, action=action, summary=summary)]
    record = game_page._validated_agent_activity(snapshot)[0]
    assert record["dummy"] is expected
    assert ("더미" in game_page._activity_label(record)) is expected
    assert "summary" not in record


@pytest.mark.parametrize("tick", [0, 1])
def test_python_sync_fallback_belongs_only_to_the_failed_component_refresh(monkeypatch, tick):
    """초기 tick 여부와 무관하게 현재 component가 실패한 갱신만 Python fallback을 쓴다.

    전체 페이지 fixture는 연결 라벨 초기화로 추가 rerun을 만들 수 있다. fallback
    정책은 live fragment가 호출하는 한 번의 동기화 경계에서 검증하고, 실패 뒤 정상
    복구 시에는 이전 실패 표시 때문에 Python polling이 계속되지 않는지도 확인한다.
    """

    from types import SimpleNamespace
    from unittest.mock import Mock

    snapshot = _snapshot()
    client = _Client(snapshot)
    client.config = SimpleNamespace(api_url="http://127.0.0.1:1")
    client.user_id = HUMAN
    client.get_sync = Mock(return_value={"data": {
        "game_id": GAME, "mode": "DELTA", "from_state_version": 12,
        "state_version": 12, "last_sequence": 0, "operations": [], "snapshot": None,
    }})
    # 진행 표시 GET의 tick 처리와 transport fallback을 분리해 현재 갱신만 관찰한다.
    state = {"game.sync_tick": tick, "game.activity_tick": tick, "game.client": client}
    monkeypatch.setattr(game_page.st, "session_state", state)

    for component_failed in (False, True, False, True):
        def mount(**kwargs):
            # 실제 bridge처럼 매 갱신에서 실패 표시를 다시 결정한다. 정상 component도
            # 새 envelope가 없는 동안에는 None을 반환하므로 이를 실패로 간주하지 않는다.
            state["game.sync_component_failed"] = component_failed
            return None

        mounted = Mock(side_effect=mount)
        monkeypatch.setattr(game_page, "mount_sse", mounted)
        client.get_sync.reset_mock()
        assert game_page._sync_snapshot(client=client, snapshot=snapshot) == snapshot
        mounted.assert_called_once_with(
            backend_url=client.config.api_url, game_id=GAME, user_id=HUMAN,
            last_sequence=0, after_state_version=12,
        )
        if component_failed:
            client.get_sync.assert_called_once_with(
                game_id=GAME, after_state_version=12, after_sequence=0,
            )
        else:
            client.get_sync.assert_not_called()
        assert state["game.latest_snapshot"] == snapshot
    assert client.reads == 0 and client.calls == []


def test_hidden_game_blocks_action_panel_and_shell_buttons():
    snapshot = _snapshot()
    app = AppTest.from_function(_page_app, args=(_Client(snapshot), snapshot, 1, False, True)).run()
    assert not app.exception
    assert app.button(key="game.save_exit").disabled


def test_hidden_spectator_cannot_enable_fast_forward():
    snapshot = _snapshot()
    snapshot["me"]["alive"] = False
    snapshot["legal_actions"].append("FAST_FORWARD")
    snapshot["game"]["fast_forward_enabled"] = False
    app = AppTest.from_function(_page_app, args=(_Client(snapshot), snapshot, 1, False, True)).run()
    assert not app.exception
    assert app.toggle(key=f"spectator.fast_forward.{GAME}").disabled
    assert app.button(key="game.save_exit").disabled


def test_public_players_excludes_private_fields() -> None:
    snapshot = {
        "players": [{
            "player_id": "player-1", "seat": 1, "display_name": "플레이어 1",
            "kind": "AI", "alive": True, "revealed_role": None,
            "alibi": "비공개 알리바이", "observation": "비공개 관찰", "attack": "private",
        }],
    }
    player = public_players(snapshot)[0]
    assert player["display_name"] == "플레이어 1"
    assert "alibi" not in player
    assert "observation" not in player
    assert "attack" not in player


def test_only_me_projection_is_available_to_private_panel() -> None:
    snapshot = {"me": {
        "player_id": "human-1", "role": "DETECTIVE", "alive": True,
        "alibi": "내 알리바이", "observation": "내 관찰",
        "private_events": [], "other_player_role": "MAFIA",
    }}
    me = own_private_view(snapshot)
    assert me["role"] == "DETECTIVE"
    assert "other_player_role" not in me


def _investigation_snapshot():
    snapshot = _snapshot()
    snapshot["game"].update(round=1, day_number=2)
    snapshot["players"][0]["display_name"] = "플레이어 2"
    snapshot["me"].update(role="DETECTIVE", private_events=[{
        "event_id": RUN, "event_type": "INVESTIGATION_RESULT",
        "created_at": "2026-09-07T01:00:00Z",
        "data": {"round": 1, "target_player_id": AI, "is_mafia": False},
    }])
    return snapshot


@pytest.mark.parametrize("spectating", [False, True])
@pytest.mark.parametrize("status", ["IN_PROGRESS", "SAVED"])
@pytest.mark.parametrize("is_mafia", [False, True])
def test_canonical_investigation_is_visible_in_own_panel_after_refresh(spectating, status, is_mafia):
    """B5 정본 결과가 낮 2·저장·관전에서 동일하게 복원되고 공개 패널에는 섞이지 않는다."""

    snapshot = _investigation_snapshot()
    snapshot["game"]["status"] = status
    snapshot["legal_actions"] = ["RESUME"] if status == "SAVED" else ["SAVE_AND_EXIT"]
    snapshot["me"]["alive"] = not spectating
    snapshot["players"][1]["alive"] = not spectating
    snapshot["me"]["private_events"][0]["data"]["is_mafia"] = is_mafia
    client = _Client(snapshot)
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    verdict = "마피아입니다" if is_mafia else "마피아가 아닙니다"
    expected = f"밤 1 조사 결과 · 플레이어 2: {verdict}."
    assert not app.exception
    assert [item.value for item in app.text] == [expected]
    assert "나에게만 공개된 결과" in " ".join(item.value for item in app.markdown)
    assert not any(expected in item.value for item in app.markdown)
    app.run()
    assert not app.exception and [item.value for item in app.text] == [expected]
    assert not client.calls


@pytest.mark.parametrize("section,changes", [
    ("event", {"event_type": "NIGHT_ACTION_ACCEPTED"}),
    ("event", {"event_type": "PLAYER_SPOKE"}),
    ("event", {"event_type": []}),
    ("event", {"event_id": "bad"}),
    ("event", {"event_id": RUN.replace("-", "")}),
    ("event", {"event_id": None}),
    ("event", {"created_at": "bad"}),
    ("event", {"created_at": "2026-02-30T01:00:00Z"}),
    ("event", {"created_at": "2026-09-07T10:00:00+09:00"}),
    ("event", {"created_at": []}),
    ("event", {"data": []}),
    ("event", {"message": "다른 actor의 비공개 자유 문구"}),
    ("event", {"actor_player_id": AI}),
    ("event", {"game_id": GAME}),
    ("event", {"game_id": RUN}),
    ("data", {"actor_player_id": AI}),
    ("data", {"player_id": AI}),
    ("data", {"message": "다른 actor의 비공개 자유 문구"}),
    ("data", {"target_player_id": "bad"}),
    ("data", {"target_player_id": AI.replace("-", "")}),
    ("data", {"target_player_id": []}),
    ("data", {"target_player_id": RUN}),
    ("data", {"target_player_id": HUMAN}),
    ("data", {"round": 0}), ("data", {"round": 2}),
    ("data", {"round": 6}), ("data", {"round": True}),
    ("data", {"round": "1"}), ("data", {"round": 1.0}),
    ("data", {"is_mafia": 0}), ("data", {"is_mafia": 1}),
    ("data", {"is_mafia": "false"}), ("data", {"is_mafia": "true"}),
    ("data", {"is_mafia": None}), ("data", {"is_mafia": []}),
])
def test_investigation_rejects_noncanonical_or_private_payload(section, changes):
    snapshot = _investigation_snapshot()
    event = snapshot["me"]["private_events"][0]
    (event if section == "event" else event["data"]).update(changes)
    assert game_page._validated_investigation_results(snapshot, expected_game_id=GAME) == []


@pytest.mark.parametrize("section,field", [
    ("event", "event_id"), ("event", "event_type"), ("event", "created_at"), ("event", "data"),
    ("data", "round"), ("data", "target_player_id"), ("data", "is_mafia"),
])
def test_investigation_requires_all_canonical_fields(section, field):
    snapshot = _investigation_snapshot()
    event = snapshot["me"]["private_events"][0]
    del (event if section == "event" else event["data"])[field]
    assert game_page._validated_investigation_results(snapshot, expected_game_id=GAME) == []


@pytest.mark.parametrize("section,changes", [
    ("me", {"role": "CITIZEN"}), ("me", {"role": "DOCTOR"}), ("me", {"role": "MAFIA"}),
    ("me", {"player_id": AI}), ("me", {"player_id": RUN}), ("me", {"player_id": "bad"}),
    ("me", {"private_events": None}), ("me", {"private_events": {}}),
    ("game", {"game_id": RUN}), ("game", {"game_id": "bad"}),
    ("game", {"round": 0}), ("game", {"round": True}), ("game", {"round": 6}),
])
def test_investigation_requires_current_game_and_own_human_detective(section, changes):
    snapshot = _investigation_snapshot()
    snapshot[section].update(changes)
    assert game_page._validated_investigation_results(snapshot, expected_game_id=GAME) == []


@pytest.mark.parametrize("players", [None, [], [None], [
    {"player_id": HUMAN, "kind": "HUMAN", "display_name": "사람"},
], [
    {"player_id": HUMAN, "kind": "AI", "display_name": "사람"},
    {"player_id": AI, "kind": "AI", "display_name": "플레이어 2"},
], [
    {"player_id": HUMAN, "kind": "HUMAN", "display_name": "사람"},
    {"player_id": AI, "kind": "HUMAN", "display_name": "플레이어 2"},
]])
def test_investigation_requires_own_and_target_public_membership(players):
    snapshot = _investigation_snapshot()
    snapshot["players"] = players
    assert game_page._validated_investigation_results(snapshot, expected_game_id=GAME) == []


def test_investigation_preserves_history_and_rejects_duplicate_event_or_player_ids():
    snapshot = _investigation_snapshot()
    snapshot["game"]["round"] = 5
    snapshot["me"]["alive"] = False
    snapshot["players"][0]["alive"] = False
    event = deepcopy(snapshot["me"]["private_events"][0])
    event.update(event_id=GAME, created_at="2026-09-07T02:00:00.123+00:00")
    event["data"].update(round=5, is_mafia=True)
    snapshot["me"]["private_events"].append(event)
    assert game_page._validated_investigation_results(snapshot, expected_game_id=GAME) == [
        "밤 1 조사 결과 · 플레이어 2: 마피아가 아닙니다.",
        "밤 5 조사 결과 · 플레이어 2: 마피아입니다.",
    ]
    snapshot["me"]["private_events"].append(deepcopy(event))
    assert game_page._validated_investigation_results(snapshot, expected_game_id=GAME) == []
    snapshot["me"]["private_events"].pop()
    snapshot["players"].append(deepcopy(snapshot["players"][0]))
    assert game_page._validated_investigation_results(snapshot, expected_game_id=GAME) == []


@pytest.mark.parametrize("spectating", [False, True])
def test_private_result_renderer_uses_plain_names_and_never_echoes_other_private_text(spectating):
    snapshot = _investigation_snapshot()
    snapshot["me"]["alive"] = not spectating
    snapshot["players"][0]["display_name"] = "[공개 이름](https://example.invalid) <img src=x>"
    snapshot["players"][0]["private_events"] = [{"message": "타인의 비공개 원문"}]
    snapshot["private_events"] = [{"message": "게임 전체의 비공개 원문"}]
    snapshot["me"]["private_events"].extend([
        {"message": "legacy 비공개 원문"}, None,
        {"event_id": GAME, "event_type": "NIGHT_ACTION_ACCEPTED",
         "created_at": "2026-09-07T01:00:00Z",
         "data": {"round": 1, "action_type": "INVESTIGATE", "target_player_id": AI}},
    ])
    app = AppTest.from_function(_page_app, args=(_Client(snapshot), snapshot)).run()
    assert not app.exception
    assert [item.value for item in app.text] == [
        "밤 1 조사 결과 · [공개 이름](https://example.invalid) <img src=x>: 마피아가 아닙니다.",
    ]
    assert "비공개 원문" not in " ".join(item.value for item in [*app.markdown, *app.text])


@pytest.mark.parametrize("foreign_game", [False, True])
def test_private_result_renderer_hides_empty_heading_and_foreign_game(foreign_game):
    snapshot = _investigation_snapshot()
    if not foreign_game:
        snapshot["me"]["private_events"] = [{"message": "허용되지 않은 비공개 원문"}]
    app = AppTest.from_function(_page_app, args=(_Client(snapshot), snapshot))
    app.session_state["game.game_id"] = RUN if foreign_game else GAME
    app.run()
    assert not app.exception and not app.text
    text = " ".join(item.value for item in app.markdown)
    assert "나에게만 공개된 결과" not in text and "비공개 원문" not in text


@pytest.mark.parametrize("changes", [
    {"decision_source": "secret"}, {"decision_source": []},
    {"reason_code": "raw error"}, {"reason_code": {}},
    {"decision_basis": "private reasoning"}, {"decision_basis": []},
])
def test_activity_rejects_unknown_diagnostic_codes(changes):
    snapshot = _snapshot()
    snapshot["agent_activity"] = [_activity(**changes)]
    assert game_page._validated_agent_activity(snapshot) == []


def test_activity_shows_applied_fallback_reason_and_public_basis_without_expanding():
    snapshot = _snapshot()
    snapshot["agent_activity"] = [_activity(stage="APPLIED", action="PASS",
        decision_source="FALLBACK", reason_code="PROVIDER_INCOMPLETE", decision_basis=None)]
    app = AppTest.from_function(_activity_app, args=(snapshot,)).run()
    assert not app.exception
    assert "응답 생성 미완료" in " ".join(item.value for item in app.warning)
    assert "규칙 대체" in " ".join(item.value for item in app.markdown)
    snapshot["agent_activity"] = [_activity(stage="APPLIED", action="SPEAK",
        decision_source="MODEL", reason_code=None, decision_basis="ASK_FOR_CLARIFICATION")]
    app = AppTest.from_function(_activity_app, args=(snapshot,)).run()
    assert not app.exception
    assert "확인 질문" in " ".join(item.value for item in app.markdown)
    assert "정보 확인 → 판단 → 선택 → 적용" in " ".join(item.value for item in app.caption)


def test_dummy_source_remains_visible_after_applied():
    snapshot = _snapshot()
    snapshot["agent_activity"] = [_activity(stage="APPLIED", action="PASS", decision_source="DUMMY")]
    app = AppTest.from_function(_activity_app, args=(snapshot,)).run()
    assert not app.exception
    assert "더미 모드" in " ".join(item.value for item in app.warning)


def _navigation_app(initial_page):
    """실제 dispatcher처럼 page 변경을 방문 기록과 상단 이동 제어에 연결한다."""

    import streamlit as st

    from frontend_user.components import theme

    page = st.session_state.setdefault("navigation.page", initial_page)
    page = theme.sync_page_navigation(page)
    theme.render_application_header(
        title="AI 마피아",
        action_renderer=lambda: theme.render_header_back_button(current_page=page),
    )
    st.text(page)


@pytest.mark.parametrize(
    "page",
    ["create", "creation_complete", "feedback", "game_feedback", "game"],
)
def test_every_non_home_route_has_only_one_header_back_button(page):
    app = AppTest.from_function(_navigation_app, args=(page,)).run()
    assert not app.exception
    assert app.button(key="header.back").label == "홈으로"
    assert len(app.button) == 1


def test_invalid_navigation_page_returns_to_home_without_rendering_duplicate_controls():
    app = AppTest.from_function(_navigation_app, args=([],)).run()
    assert not app.exception
    assert app.session_state["navigation.page"] == "home"
    assert not app.button


def test_home_button_clears_app_history_without_creating_a_navigation_loop():
    """이전 역할·생성 화면으로 되돌아가지 않고 홈 이동 시 방문 기록을 비우는지 확인한다."""

    app = AppTest.from_function(_navigation_app, args=("home",)).run()
    app.session_state["navigation.page"] = "create"
    app.run()
    app.session_state["navigation.page"] = "creation_complete"
    app.run()
    assert list(app.session_state[theme.NAVIGATION_HISTORY_KEY]) == ["home", "create"]

    app.button(key="header.back").click().run()
    assert app.session_state["navigation.page"] == "home"
    assert list(app.session_state[theme.NAVIGATION_HISTORY_KEY]) == []
    assert not app.button


def test_back_to_home_clears_history_and_only_invalidates_home_list_projection():
    app = AppTest.from_function(_navigation_app, args=("create",))
    app.session_state[theme.NAVIGATION_HISTORY_KEY] = ["home", None, "invalid"]
    app.session_state["game.game_id"] = GAME
    app.session_state["form.feedback.comment"] = "작성 중인 내용"
    app.session_state["home.games"] = [{"game_id": GAME}]
    app.run()

    app.button(key="header.back").click().run()
    assert app.session_state["navigation.page"] == "home"
    assert list(app.session_state[theme.NAVIGATION_HISTORY_KEY]) == []
    assert app.session_state["game.game_id"] == GAME
    assert app.session_state["form.feedback.comment"] == "작성 중인 내용"
    assert "home.games" not in app.session_state


@pytest.mark.parametrize("phase,alive", [
    ("ROLE_REVEAL", True), ("DAY_DISCUSSION", True), ("NIGHT_ACTION", True),
    ("DAY_VOTE", True), ("DAY_DISCUSSION", False),
])
def test_game_back_opens_exit_dialog_without_mutating_or_navigating(phase, alive):
    """역할 공개·진행·관전 모두 뒤로가기로 이탈하지 않고 선택 팝업을 유지한다."""

    snapshot = _snapshot(phase=phase)
    snapshot["me"]["alive"] = alive
    client = _Client(snapshot)
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    app.button(key="header.back").click().run()
    assert not app.exception
    assert app.session_state["navigation.page"] == "game"
    assert client.calls == client.delete_calls == []
    assert app.button(key="game.save_confirm").label == "저장하고 나가기"
    assert app.button(key="game.delete_confirm").label == "게임 삭제"
    app.run()
    assert app.button(key="game.save_cancel").label == "계속 플레이"
    app.button(key="game.save_cancel").click().run()
    assert not app.exception
    assert app.session_state["navigation.page"] == "game"
    assert "game.exit_dialog_id" not in app.session_state
    assert client.calls == client.delete_calls == []
    assert not any(button.key == "game.delete_confirm" for button in app.button)


@pytest.mark.parametrize("choice", ["save", "delete"])
def test_exit_dialog_only_mutates_after_explicit_choice(choice):
    """선택한 작업 하나만 실행하고 서버 성공 뒤 홈으로 이동한다."""

    snapshot = _snapshot()
    client = _Client(snapshot)
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    app.session_state["home.games"] = [GAME]
    app.session_state["identity.user_id"] = HUMAN
    app.button(key="header.back").click().run()
    app.button(key=f"game.{choice}_confirm").click().run()
    assert not app.exception
    assert app.session_state["navigation.page"] == "home"
    assert app.session_state["identity.user_id"] == HUMAN
    assert "game.game_id" not in app.session_state
    assert "home.games" not in app.session_state
    if choice == "save":
        assert client.calls[0]["command"]["type"] == "SAVE_AND_EXIT"
        assert client.delete_calls == []
    else:
        assert client.delete_calls == [{"game_id": GAME, "expected_state_version": 12}]
        assert client.calls == []


def test_exit_delete_unknown_keeps_game_and_retries_same_request():
    """응답 유실 이후 자동 재삭제하지 않고 최초 버전의 요청만 재확인한다."""

    snapshot = _snapshot()
    client = _Client(snapshot, [ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")])
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    app.button(key="header.back").click().run()
    app.button(key="game.delete_confirm").click().run()
    assert not app.exception and len(client.delete_calls) == 1
    assert app.session_state["navigation.page"] == "game"
    assert app.session_state["game.game_id"] == GAME
    assert app.button(key="game.save_confirm").disabled
    assert app.button(key="game.delete_confirm").label == "같은 삭제 요청 다시 확인"
    app.run()
    assert len(client.delete_calls) == 1
    app.button(key="game.delete_confirm").click().run()
    assert not app.exception
    assert client.delete_calls[0] == client.delete_calls[1]
    assert app.session_state["navigation.page"] == "home"


def test_exit_delete_stale_keeps_game_and_requires_new_confirmation():
    """진행 도중 버전이 바뀌면 삭제와 자동 재시도를 중단한다."""

    snapshot = _snapshot()
    client = _Client(snapshot, [ApiResponseError(status_code=409, code="STALE_STATE_VERSION")])
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    app.button(key="header.back").click().run()
    app.button(key="game.delete_confirm").click().run()
    assert not app.exception
    assert len(client.delete_calls) == 1
    assert app.session_state["navigation.page"] == "game"
    assert "game.delete_pending" not in app.session_state
    assert "game.exit_dialog_id" not in app.session_state
    assert any("게임 상태가 바뀌어" in message.value for message in app.warning)


def test_exit_delete_already_missing_returns_home():
    """삭제 응답을 잃은 뒤 같은 게임의 404를 받으면 접근 불가 상태를 홈에 반영한다."""

    snapshot = _snapshot()
    client = _Client(snapshot, [ApiResponseError(status_code=404, code="GAME_NOT_FOUND")])
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    app.button(key="header.back").click().run()
    app.button(key="game.delete_confirm").click().run()
    assert not app.exception
    assert app.session_state["navigation.page"] == "home"


def test_saved_game_back_returns_home_without_exit_dialog():
    """이미 저장된 게임은 다시 저장·삭제를 요구하지 않고 기존 뒤로가기를 따른다."""

    snapshot = _snapshot(status="SAVED")
    client = _Client(snapshot)
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    app.button(key="header.back").click().run()
    assert not app.exception
    assert app.session_state["navigation.page"] == "home"
    assert client.calls == client.delete_calls == []


@pytest.mark.parametrize("blocked", ["hidden", "save_pending", "disallowed_save"])
def test_exit_dialog_respects_sync_and_pending_command_locks(blocked):
    """최신 상태 미확인·결과 불명 요청은 저장과 삭제가 충돌하지 않도록 잠근다."""

    snapshot = _snapshot()
    if blocked == "disallowed_save":
        snapshot["legal_actions"] = []
    client = _Client(snapshot)
    app = AppTest.from_function(_page_app, args=(client, snapshot), kwargs={"hidden": blocked == "hidden"})
    if blocked == "save_pending":
        app.session_state["game.save_pending"] = {"game_id": GAME, "status": "RETRYABLE_UNKNOWN"}
    app.run()
    app.button(key="header.back").click().run()
    assert not app.exception
    assert app.button(key="game.save_confirm").disabled
    assert app.button(key="game.delete_confirm").disabled is (blocked != "disallowed_save")
    assert client.calls == client.delete_calls == []


def _missing_deleted_game_app(client):
    """실제 dispatcher의 첫 GET 실패를 합성 identity와 API로 재현한다."""

    from unittest.mock import patch
    from uuid import UUID
    import streamlit as st
    from frontend_user import app as application

    with patch.object(application, "load_identity", return_value=(UUID("00000000-0000-4000-8000-000000000203"), "LOCAL", None)), patch.object(
        application, "ApiClient", return_value=client,
    ), patch.object(application, "render_home", side_effect=lambda client: st.write("복구된 홈")), patch.object(
        application, "should_load_games", return_value=False,
    ):
        application.main()


@pytest.mark.parametrize("pending_game", [GAME, AI, None])
def test_dispatcher_recovers_missing_game_only_after_matching_delete_request(pending_game):
    """소유 게임 삭제 뒤 404는 홈으로 복구하고 일반 조회 실패와 타 게임 요청은 보존한다."""

    from unittest.mock import Mock
    client = Mock()
    client.get_game.side_effect = ApiResponseError(status_code=404, code="GAME_NOT_FOUND")
    app = AppTest.from_function(_missing_deleted_game_app, args=(client,))
    app.session_state["identity.user_id"] = HUMAN
    app.session_state["navigation.page"] = "game"
    app.session_state["game.game_id"] = GAME
    if pending_game:
        app.session_state["game.delete_pending"] = {"game_id": pending_game, "expected_state_version": 12}
    app.run()
    assert not app.exception
    if pending_game == GAME:
        assert app.session_state["navigation.page"] == "home"
        assert "game.game_id" not in app.session_state
        assert "game.delete_pending" not in app.session_state
    else:
        assert app.session_state["navigation.page"] == "game"
        assert app.button(key="game.load_home")


@pytest.mark.parametrize("phase", ["ROLE_REVEAL", "DAY_DISCUSSION"])
def test_exit_save_response_loss_keeps_retry_available(phase):
    """뒤로가기에서 저장을 선택한 뒤 응답이 유실돼도 원래 요청으로 재확인할 수 있다."""

    snapshot = _snapshot(phase=phase)
    client = _Client(snapshot, [ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")])
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    app.button(key="header.back").click().run()
    app.button(key="game.save_confirm").click().run()
    assert not app.exception
    assert app.session_state["navigation.page"] == "game"
    assert len(client.calls) == 1
    app.button(key="game.save_exit_retry").click().run()
    assert not app.exception
    assert app.session_state["navigation.page"] == "home"
    assert client.calls[0] == client.calls[1]


def test_exit_save_uses_server_confirmation_when_screen_version_is_older():
    """화면 갱신을 기다리지 않고 저장 의도를 제출하며 서버가 확정한 저장 성공으로 이동한다."""

    snapshot = _snapshot(version=12)
    client = _Client(_snapshot(version=15))
    app = AppTest.from_function(_page_app, args=(client, snapshot)).run()
    app.button(key="header.back").click().run()
    assert any("마지막으로 확정된 진행 상황" in caption.value for caption in app.caption)
    reads = client.reads
    app.button(key="game.save_confirm").click().run()
    assert not app.exception
    assert client.calls[0]["command"] == {"type": "SAVE_AND_EXIT", "expected_state_version": 12}
    assert client.snapshot["game"]["state_version"] == 16
    assert client.reads == reads + 1
    assert app.session_state["navigation.page"] == "home"


def test_custom_private_fields_never_enter_public_player_projection():
    """타인에게 섞인 비공개 직업·능력을 공개 목록이 복사하지 않는지 검증한다."""

    from frontend_user.core.view_models import custom_role_description

    private = {"role_name": "본인 감식관", "faction": "CITIZEN", "ability_ids": ["night.investigate.v1"],
               "ability_options": []}
    snapshot = {"me": private, "players": [{"player_id": AI, "seat": 1,
        "role_name": "타인 비밀", "faction": "MAFIA", "ability_ids": ["night.attack.v1"], "ability_options": []}]}
    assert own_private_view(snapshot) == private
    assert public_players(snapshot) == [{"player_id": AI, "seat": 1}]
    assert "조사" in custom_role_description(own_private_view(snapshot))
    assert "타인 비밀" not in custom_role_description(own_private_view(snapshot))


def test_custom_investigator_can_read_only_own_results():
    snapshot = _investigation_snapshot()
    snapshot["game"]["mode"] = "CUSTOM_ROLE"
    snapshot["me"].update(role="CITIZEN", role_name="감식관", faction="CITIZEN",
                          ability_ids=["night.investigate.v1"], ability_options=[])
    app = AppTest.from_function(_page_app, args=(_Client(snapshot), snapshot)).run()
    assert not app.exception
    assert any("밤 1 조사 결과" in item.value for item in app.text)
    assert any("시민 진영" in item.value for item in app.text)


def _intel_snapshot():
    """첫 밤 이후 본인 전용 능력이 해금된 최소 공개 snapshot이다."""

    snapshot = _snapshot()
    snapshot["game"].update(mode="CUSTOM_ROLE", day_number=2)
    snapshot["me"].update(ability_ids=["intel.special_roles.v1"], faction="CITIZEN", role_name="기록관")
    return snapshot


class _IntelClient:
    """동기 HTTP 도중의 identity·snapshot 변경도 재현하는 비공개 조회 대역이다."""

    user_id = HUMAN

    def __init__(self):
        self.snapshot = _intel_snapshot()
        self.calls = 0
        self.refreshes = 0
        self.change = None
        self.error = None
        self.response = {"data": {"game_id": GAME, "player_id": HUMAN,
            "ability_id": "intel.special_roles.v1", "state_version": 12,
            "roles": [{"player_id": AI, "display_name": "<b>비공개 탐정</b>", "role": "DETECTIVE", "alive": False}]}}

    def get_special_roles(self, game_id):
        import streamlit as st

        self.calls += 1
        if self.change:
            self.change(st.session_state)
        if self.error:
            raise self.error
        return deepcopy(self.response)

    def get_game(self, game_id):
        self.refreshes += 1
        return {"data": deepcopy(self.snapshot)}


def _intel_app(client):
    from copy import deepcopy
    import streamlit as st
    from frontend_user.app_pages.game_page import _render_special_roles

    st.session_state.setdefault("identity.user_id", client.user_id)
    st.session_state.setdefault("game.game_id", client.snapshot["game"]["game_id"])
    st.session_state.setdefault("game.latest_snapshot", deepcopy(client.snapshot))
    _render_special_roles(client=client, snapshot=st.session_state["game.latest_snapshot"])


def test_intel_private_panel_plain_text_rerun_error_discards_and_retries():
    client = _IntelClient()
    app = AppTest.from_function(_intel_app, args=(client,)).run()
    app.button(key="game.special_roles_query").click().run()
    assert not app.exception
    assert any("<b>비공개 탐정</b>" in item.value for item in app.text)
    assert "비공개 탐정" not in str(app.session_state["game.latest_snapshot"])
    app.run()
    assert client.calls == 1
    client.error = ApiResponseError(status_code=403, code="PRIVATE_RAW_ERROR")
    app.button(key="game.special_roles_query").click().run()
    assert "game.special_roles" not in app.session_state
    assert not any("비공개 탐정" in item.value for item in app.text)
    assert "PRIVATE_RAW_ERROR" not in str([item.value for item in app.warning])
    assert client.refreshes == 1
    client.error = None
    app.button(key="game.special_roles_query").click().run()
    assert app.session_state["game.special_roles"]["roles"]
    assert not app.exception


@pytest.mark.parametrize("change", ["identity", "game", "version", "phase", "dead", "ended", "ability", "logout", "player"])
def test_intel_scope_changes_discard_success_and_late_response(change):
    from frontend_user.core.session import maintain_special_roles, special_roles_scope

    def mutate(state):
        if change == "identity":
            state["identity.user_id"] = AI
        elif change == "logout":
            state.pop("identity.user_id", None)
        elif change == "game":
            state["game.game_id"] = AI
        else:
            snapshot = deepcopy(state["game.latest_snapshot"])
            if change == "version":
                snapshot["game"]["state_version"] += 1
            elif change == "phase":
                snapshot["game"]["phase"] = "NIGHT_ACTION"
            elif change == "dead":
                snapshot["me"]["alive"] = False
            elif change == "ended":
                snapshot["game"]["status"] = "COMPLETED"
            elif change == "ability":
                snapshot["me"]["ability_ids"] = []
            else:
                snapshot["me"]["player_id"] = AI
            state["game.latest_snapshot"] = snapshot

    snapshot = _intel_snapshot()
    state = {"identity.user_id": HUMAN, "game.game_id": GAME, "game.latest_snapshot": snapshot}
    state["game.special_roles"] = {"scope": special_roles_scope(state, snapshot), "roles": [{"secret": True}]}
    mutate(state)
    maintain_special_roles(state, state["game.latest_snapshot"])
    assert "game.special_roles" not in state
    client = _IntelClient()
    client.change = mutate
    app = AppTest.from_function(_intel_app, args=(client,)).run()
    app.button(key="game.special_roles_query").click().run()
    assert not app.exception
    assert "game.special_roles" not in app.session_state
    assert not any("비공개 탐정" in item.value for item in app.text)


@pytest.mark.parametrize("change", ["day", "standard", "ai", "dead", "saved", "ability"])
def test_intel_ineligible_never_calls_api(change):
    client = _IntelClient()
    if change == "day":
        client.snapshot["game"]["day_number"] = 1
    elif change == "standard":
        client.snapshot["game"]["mode"] = "STANDARD"
    elif change == "ai":
        client.snapshot["players"][1]["kind"] = "AI"
    elif change == "dead":
        client.snapshot["me"]["alive"] = False
    elif change == "saved":
        client.snapshot["game"]["status"] = "SAVED"
    else:
        client.snapshot["me"]["ability_ids"] = []
    app = AppTest.from_function(_intel_app, args=(client,)).run()
    assert not app.button
    assert client.calls == 0
    assert not app.exception


@pytest.mark.parametrize("field,value", [("game_id", AI), ("player_id", AI), ("state_version", 13),
    ("state_version", True), ("ability_id", "other"), ("roles", [{"role": "MAFIA"}])])
def test_intel_response_scope_and_schema_fail_closed(field, value):
    client = _IntelClient()
    client.response["data"][field] = value
    app = AppTest.from_function(_intel_app, args=(client,)).run()
    app.button(key="game.special_roles_query").click().run()
    assert "game.special_roles" not in app.session_state
    assert app.warning
    assert not app.exception


def test_intel_failed_snapshot_refresh_blocks_query_until_revalidated():
    class FailingRefreshClient(_IntelClient):
        """권한 오류 뒤 snapshot 의존성까지 실패하면 다음 클릭도 먼저 재검증한다."""

        fail_refresh = True

        def get_game(self, game_id):
            self.refreshes += 1
            if self.fail_refresh:
                raise ApiUnavailableError(status_code=503, code="PRIVATE_FAILURE")
            return {"data": deepcopy(self.snapshot)}

    client = FailingRefreshClient()
    client.error = ApiResponseError(status_code=409, code="ABILITY_NOT_AVAILABLE")
    app = AppTest.from_function(_intel_app, args=(client,)).run()
    app.button(key="game.special_roles_query").click().run()
    assert client.calls == 1
    app.button(key="game.special_roles_query").click().run()
    assert client.calls == 1
    assert "game.special_roles" not in app.session_state
    client.fail_refresh = False
    client.error = None
    app.button(key="game.special_roles_query").click().run()
    assert client.calls == 2
    assert app.session_state["game.special_roles"]["roles"]
    assert not app.exception
