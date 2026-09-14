from copy import deepcopy

import pytest
from streamlit.testing.v1 import AppTest

from frontend_user.app_pages.result_page import (
    _display_timestamp,
    _player_status,
    _round_count,
    _winner_presentation,
)


PLAYER = "00000000-0000-4000-8000-000000000301"
TARGET = "00000000-0000-4000-8000-000000000302"
EVENT = "00000000-0000-4000-8000-000000000303"


def _snapshot():
    """종료 후에만 공개할 선택과 발언을 외부 API 없이 제공한다."""

    return {
        "game": {"status": "COMPLETED", "round": 1, "day_number": 2},
        "scenario": {"title": "합성 시나리오"},
        "result": {
            "winner": "CITIZEN", "win_reason": "ALL_MAFIA_ELIMINATED",
            "finished_at": "2026-09-07T01:02:03.123Z",
            "players": [
                {"player_id": PLAYER, "display_name": "하늘", "role": "DETECTIVE", "alive": True},
                {"player_id": TARGET, "display_name": "바다", "role": "MAFIA", "alive": False,
                 "eliminated_phase": "DAY_VOTE", "eliminated_round": 1},
            ],
            "nights": [{"round": 1,
                        "attack_choices": [{"actor_player_id": TARGET, "target_player_id": PLAYER, "is_auto": False}],
                        "resolved_attack_target_player_id": PLAYER, "protect_player_id": PLAYER,
                        "investigations": [{"actor_player_id": PLAYER, "target_player_id": TARGET,
                                            "is_auto": True, "is_mafia": True}], "killed_player_id": None}],
            "votes": [{"round": 1, "phase": "DAY_VOTE",
                       "ballots": [{"actor_player_id": PLAYER, "target_player_id": TARGET, "is_auto": False}],
                       "counts": [{"target_player_id": TARGET, "vote_count": 1}], "eliminated_player_id": TARGET}],
            "public_event_ids": [EVENT],
        },
        "public_events": [{"event_id": EVENT, "event_type": "PLAYER_SPOKE",
                           "created_at": "2026-09-07T00:00:00Z",
                           "data": {"player_id": PLAYER, "message": "공개 발언 기록입니다."}}],
    }


def _result_app(snapshot):
    """이동 뒤 다시 결과를 그리지 않는 실제 dispatcher 경계를 재현한다."""

    import streamlit as st
    from frontend_user.app_pages.result_page import render

    if st.session_state.get("navigation.page", "result") == "result":
        render(snapshot)
    else:
        st.text(st.session_state["navigation.page"])


def _text(app):
    """마크다운과 일반 텍스트를 모아 화면에 실제 출력된 정보만 검사한다."""

    return "\n".join(str(item.value) for kind in ("markdown", "text", "caption", "info", "error", "success")
                     for item in app.get(kind))


@pytest.mark.parametrize("status", ["FAILED", "IN_PROGRESS", "SAVED"])
def test_unfinished_result_does_not_reveal_roles_choices_or_public_replay(status):
    snapshot = _snapshot()
    snapshot["game"]["status"] = status
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    rendered = _text(app)
    assert "하늘" not in rendered and "바다" not in rendered
    assert "공개 발언 기록입니다." not in rendered and "전체 역할" not in rendered
    assert not any(button.key == "result.feedback" for button in app.button)


def test_winner_presentation_uses_backend_winner_enum() -> None:
    title, caption = _winner_presentation("CITIZEN")
    assert title == "시민 진영 승리"
    assert "마피아" in caption


def test_player_status_uses_eliminated_phase_without_role_inference() -> None:
    status, detail = _player_status(
        {"alive": False, "eliminated_phase": "DAY_VOTE", "eliminated_round": 2}
    )
    assert status == "처형됨"
    assert detail == "낮 투표 · 라운드 2"


def test_round_count_uses_only_confirmed_result_rounds() -> None:
    result = {
        "nights": [{"round": 1}, {"round": 2}],
        "votes": [{"round": 3}],
    }
    assert _round_count(game={"round": 2}, result=result) == 3


@pytest.mark.parametrize("timestamp, expected", [
    ("2026-09-07T01:02:03.123Z", "2026-09-07 10:02:03"),
    ("2026-09-07T10:02:03+09:00", "2026-09-07 10:02:03"),
    ("2026-09-07T23:02:03-04:00", "2026-09-08 12:02:03"),
])
def test_finished_timestamp_converts_to_seoul_without_fraction(timestamp, expected):
    assert _display_timestamp(timestamp) == expected


@pytest.mark.parametrize("timestamp", [
    None, 123, "", "<script>alert(1)</script>", "2026-09-07", "2026-09-07T01:02:03",
    "2026-02-30T01:02:03Z", "2026-09-07T25:00:00Z", "2026-09-07T01:02:03+24:00",
    "2026-09-07T01:02:03+00:99", "9999-12-31T23:59:59Z", "20260907T010203Z",
])
def test_invalid_finished_timestamp_is_not_echoed_or_assumed_utc(timestamp):
    assert _display_timestamp(timestamp) == "확인할 수 없음"


def test_result_replays_named_choices_investigations_ballots_and_public_speech():
    app = AppTest.from_function(_result_app, args=(_snapshot(),)).run()
    assert not app.exception
    rendered = _text(app)
    for expected in ("2026-09-07 10:02:03", "마피아 선택: 바다 → 하늘", "조사: 하늘 → 바다",
                     "마피아", "자동 선택", "투표: 하늘 → 바다", "바다: 1표", "공개 발언 기록입니다."):
        assert expected in rendered
    assert PLAYER not in rendered and TARGET not in rendered


def test_empty_records_are_not_invented_from_events_or_alive_players():
    snapshot = _snapshot()
    snapshot["result"]["nights"] = []
    snapshot["result"]["votes"] = []
    snapshot["public_events"] = []
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    rendered = _text(app)
    assert "확정된 밤 기록이 없습니다." in rendered
    assert "확정된 투표 기록이 없습니다." in rendered
    assert "표시할 공개 사건·발언 기록이 없습니다." in rendered
    assert "사망자: 없음" not in rendered and "공격:" not in rendered
    assert next(item.value for item in app.markdown if 'class="result-last-vote"' in item.value) == (
        '<div class="result-last-vote">기록 없음</div>'
    )


def test_last_vote_summary_does_not_claim_the_game_ended_at_that_vote():
    snapshot = _snapshot()
    snapshot["game"].update({"round": 4, "day_number": 4})
    snapshot["result"].update({"winner": "MAFIA", "win_reason": "MAFIA_PARITY"})
    snapshot["result"]["nights"] = [{"round": 4, "killed_player_id": PLAYER}]
    snapshot["result"]["votes"].append({"round": 3, "phase": "REVOTE"})
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    assert next(item.value for item in app.markdown if 'class="result-last-vote"' in item.value) == (
        '<div class="result-last-vote">라운드 3</div>'
    )
    assert "재투표 · 라운드 3" in _text(app)
    assert not any("결정 단계" in metric.label for metric in app.metric)
    assert "종료 이유: 마피아와 시민 수 동률" in _text(app)


def test_missing_fields_and_invalid_choices_are_not_reported_as_no_action():
    snapshot = _snapshot()
    snapshot["result"]["nights"] = [{"round": 1, "investigations": [
        {"actor_player_id": PLAYER, "target_player_id": TARGET, "is_auto": False, "is_mafia": "false"},
    ]}]
    snapshot["result"]["votes"] = [{"round": 1, "phase": "DAY_VOTE",
        "counts": [{"target_player_id": TARGET, "vote_count": True}]}]
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    rendered = _text(app)
    assert "공격 대상: 기록 없음" in rendered
    assert "사망자: 기록 없음" in rendered
    assert "탈락자: 기록 없음" in rendered
    assert "개별 조사 기록이 없습니다." in rendered
    assert "개별 투표 기록이 없습니다." in rendered
    assert "확정된 득표 기록이 없습니다." in rendered
    assert "True표" not in rendered and "마피아가 아닙니다" not in rendered


def test_result_escapes_player_and_speech_html_and_ignores_private_payloads():
    snapshot = _snapshot()
    snapshot["result"]["players"][0]["display_name"] = "<img src=x onerror=alert(1)>"
    snapshot["public_events"][0]["data"]["message"] = "<script>alert(2)</script>"
    snapshot["me"] = {"private_events": [{"message": "숨겨진조사원문"}]}
    snapshot["agent_activity"] = [{"summary": "내부판단원문"}]
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    html = "\n".join(item.value for item in app.markdown)
    assert "&lt;img" in html and "<img" not in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "숨겨진조사원문" not in _text(app) and "내부판단원문" not in _text(app)


@pytest.mark.parametrize("change", [
    {"event_id": "00000000-0000-4000-8000-000000000999"},
    {"event_type": "INVESTIGATION_RESULT"},
    {"event_type": "ACTION_RESOLVED"},
    {"event_type": []},
    {"created_at": "bad"},
    {"data": {"player_id": TARGET, "message": "유출금지", "rationale": "추론"}},
    {"data": {"player_id": "unknown", "message": "유출금지"}},
])
def test_public_replay_rejects_unlinked_private_or_invalid_events(change):
    snapshot = _snapshot()
    snapshot["public_events"][0]["data"]["message"] = "유출금지"
    snapshot["public_events"][0].update(change)
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    assert "유출금지" not in _text(app)
    assert "표시할 공개 사건·발언 기록이 없습니다." in _text(app)


def test_public_replay_keeps_server_order_and_deduplicates_event_ids():
    snapshot = _snapshot()
    first = snapshot["public_events"][0]
    second = deepcopy(first)
    second["event_id"] = "00000000-0000-4000-8000-000000000304"
    second["created_at"] = "2026-09-06T00:00:00Z"
    second["data"]["message"] = "서버 순서로 두 번째"
    snapshot["public_events"] = [first, second, first]
    snapshot["result"]["public_event_ids"].append(second["event_id"])
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    rendered = _text(app)
    assert not app.exception
    assert rendered.count("공개 발언 기록입니다.") == 1
    assert rendered.index("공개 발언 기록입니다.") < rendered.index("서버 순서로 두 번째")


def test_resolved_night_and_vote_events_show_named_public_outcomes():
    snapshot = _snapshot()
    records = [
        ("NIGHT_RESOLVED", {"round": 1, "killed_player_id": None}),
        ("VOTE_RESOLVED", {"round": 1, "phase": "DAY_VOTE", "tied": False,
                           "needs_revote": False, "counts": [{"target_player_id": TARGET, "vote_count": 1}]}),
        ("PLAYER_EXECUTED", {"player_id": TARGET, "revealed_role": "MAFIA"}),
    ]
    for index, (kind, data) in enumerate(records, start=310):
        identifier = f"00000000-0000-4000-8000-{index:012d}"
        snapshot["result"]["public_event_ids"].append(identifier)
        snapshot["public_events"].append({
            "event_id": identifier, "event_type": kind, "created_at": "2026-09-07T00:00:00Z", "data": data,
        })
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    rendered = _text(app)
    assert "밤 1 결과 · 사망자: 없음" in rendered
    assert "라운드 1 낮 투표 · 바다: 1표" in rendered
    assert "바다 처형 · 공개 역할: 마피아" in rendered


@pytest.mark.parametrize("phase, label", [("REVOTE", "재투표"), ("FINAL_ACCUSATION", "최종 지목")])
def test_later_ballots_keep_auto_selection_and_do_not_infer_elimination(phase, label):
    snapshot = _snapshot()
    vote = snapshot["result"]["votes"][0]
    vote["phase"] = phase
    vote["eliminated_player_id"] = None
    vote["ballots"][0]["is_auto"] = True
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    rendered = _text(app)
    assert f"{label} · 라운드 1" in rendered
    assert "투표: 하늘 → 바다 · 자동 선택" in rendered
    assert "탈락자: 없음" in rendered and "탈락자: 바다" not in rendered


@pytest.mark.parametrize("pending_status", ["SUCCEEDED", "RETRYABLE_UNKNOWN"])
def test_new_game_clears_old_snapshot_and_only_confirmed_creation_receipt(pending_status):
    pending = {"status": pending_status, "game_id": "previous", "idempotency_key": "synthetic-key"}
    app = AppTest.from_function(_result_app, args=(_snapshot(),))
    app.session_state["game.create_pending"] = deepcopy(pending)
    app.session_state["game.latest_snapshot"] = _snapshot()
    app.session_state["game.game_id"] = "previous"
    app.run().button(key="result.new_game").click().run()
    assert not app.exception
    assert app.session_state["navigation.page"] == "create"
    assert "game.game_id" not in app.session_state
    assert "game.latest_snapshot" not in app.session_state
    if pending_status == "SUCCEEDED":
        assert "game.create_pending" not in app.session_state
    else:
        assert app.session_state["game.create_pending"] == pending


def test_split_mafia_choices_explain_rng_only_with_valid_resolved_target():
    snapshot = _snapshot()
    night = snapshot["result"]["nights"][0]
    night["attack_choices"].append({"actor_player_id": PLAYER, "target_player_id": TARGET, "is_auto": False})
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    assert "마피아 투표가 갈려서" in _text(app)
    assert "최종 공격 대상: 하늘" in _text(app)
    night["resolved_attack_target_player_id"] = None
    app = AppTest.from_function(_result_app, args=(snapshot,)).run()
    assert not app.exception
    assert "마피아 투표가 갈려서" not in _text(app)


def test_public_chat_distinguishes_speech_and_actions_and_escapes_html():
    def chat_app():
        from frontend_user.app_pages.game_page import _render_public_chat_event
        names = {"speaker": "<b>합성 이름</b>"}
        presentations = {"speaker": ("🔵", "#2563eb")}
        _render_public_chat_event(event={"event_type": "PLAYER_SPOKE", "data": {
            "player_id": "speaker", "message": "<script>합성 발언</script>"}},
            player_names=names, presentations=presentations)
        _render_public_chat_event(event={"event_type": "PLAYER_PASSED", "data": {
            "player_id": "speaker"}}, player_names=names, presentations=presentations)
    app = AppTest.from_function(chat_app).run()
    assert not app.exception
    assert len(app.get("chat_message")) == 1
    rendered = _text(app)
    assert "&lt;script&gt;합성 발언&lt;/script&gt;" in rendered
    assert "<script>" not in rendered
    # PASS는 HTML을 허용하지 않는 caption이고 발언은 HTML 카드이므로, 두 렌더
    # 경로를 섞어 비교하면 안전한 일반 텍스트까지 escape 누락으로 잘못 판정한다.
    html = "\n".join(item.value for item in app.markdown)
    assert "&lt;b&gt;합성 이름&lt;/b&gt;" in html and "<b>합성 이름</b>" not in html
    assert "PASS했습니다" in rendered


def test_custom_result_prefers_plain_role_name_and_faction_and_discards_intel():
    snapshot = _snapshot()
    custom = {"player_id": PLAYER, "role": "DETECTIVE", "role_name": "<b>기록관</b>", "faction": "MAFIA"}
    snapshot["me"] = custom
    snapshot["result"]["players"][0].update(custom)
    app = AppTest.from_function(_result_app, args=(snapshot,))
    app.session_state["game.special_roles"] = {"roles": [{"secret": "유출 금지"}]}
    app.run()
    assert not app.exception
    assert any("내 역할 · <b>기록관</b> · 마피아 진영" == item.value for item in app.text)
    assert "<b>기록관</b>" not in "\n".join(item.value for item in app.markdown)
    assert "유출 금지" not in _text(app)
    assert "game.special_roles" not in app.session_state
