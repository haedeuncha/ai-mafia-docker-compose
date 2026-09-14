import pytest
from streamlit.testing.v1 import AppTest

from frontend_user.app_pages.game_create_page import ROLE_COUNTS
from frontend_user.app_pages.home_page import should_load_games
from frontend_user.core.api_client import ApiClient, ApiUnavailableError


GAME_ID = "00000000-0000-4000-8000-000000000201"


class _Client:
    """외부 서비스 없이 화면의 요청 횟수와 재시도 body를 관찰한다."""

    def __init__(self):
        self.created = []
        self.loaded = []
        self.items = []
        self.create_error = False
        self.list_error = False
        self.list_response = None

    def create_game(self, **body):
        self.created.append(body)
        if self.create_error:
            raise ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")
        return {"data": {"game_id": GAME_ID}}

    def get_game(self, game_id):
        count = self.created[-1]["player_count"]
        return {"data": {
            "game": {"game_id": game_id, "player_count": count},
            "players": [{"display_name": f"참가자 {seat}"} for seat in range(1, count + 1)],
        }}

    def get_games(self, **query):
        self.loaded.append(query)
        if self.list_error:
            raise ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")
        if self.list_response is not None:
            return self.list_response
        return {"data": {"items": list(self.items)}}


def _create_app(client):
    """생성 성공 뒤 역할 공개 경로로 바로 전환되는지 확인한다."""

    import streamlit as st
    from frontend_user.app_pages import creation_complete_page, game_create_page

    if st.session_state.get("navigation.page", "create") == "create":
        game_create_page.render(client)
    elif st.session_state["navigation.page"] == "game":
        st.write("역할 공개 화면으로 이동")
    else:
        creation_complete_page.render(st.session_state["game.create_pending"])


def _home_app(client):
    """브라우저 UUID 초기화가 끝난 홈의 loading 단계와 목록 렌더링을 재현한다."""

    import streamlit as st
    from frontend_user.app_pages import home_page

    st.session_state.setdefault("identity.user_id", "00000000-0000-4000-8000-000000000202")
    if st.session_state.get("navigation.page", "home") != "home":
        st.write(st.session_state["navigation.page"])
        return
    if home_page.should_load_games(st.session_state):
        st.session_state["home.games_loading"] = True
        st.rerun()
    if st.session_state.get("home.games_loading"):
        home_page.load_games(client)
        st.rerun()
    home_page.render(client)


def _completed_app(pending):
    from frontend_user.app_pages.creation_complete_page import render

    render(pending)


def _home_render_app(client, identity_ready=True):
    """기본값은 합성 UUID를 준비하고, 초기화 전 화면 검증만 명시적으로 제외한다."""

    import streamlit as st
    from frontend_user.app_pages.home_page import render

    if identity_ready:
        st.session_state.setdefault("identity.user_id", "00000000-0000-4000-8000-000000000202")
    render(client)


def _html(app):
    return "\n".join(element.value for element in app.markdown)


def test_role_preview_matches_mystery_v1_for_six_to_nine_players() -> None:
    assert ROLE_COUNTS[6] == {"마피아": 1, "탐정": 1, "의사": 1, "시민": 3}
    assert ROLE_COUNTS[7]["시민"] == 4
    assert ROLE_COUNTS[8]["마피아"] == 2
    assert sum(ROLE_COUNTS[9].values()) == 9


def test_create_game_posts_contract_and_idempotency_header() -> None:
    # 팀 전달 사항: Backend 계약 테스트와 이 header/body fixture가 일치해야 한다.
    captured = {}

    def transport(request, timeout):
        captured["request"] = request
        return 201, b'{"data":{"game_id":"d9ae9b5d-1d17-4f80-8f1a-276bfe170412"}}'

    client = ApiClient(
        user_id="83d40f36-e835-4a1d-88db-e59b6920b739",
        transport=transport,
    )
    client.create_game(
        player_count=6,
        idempotency_key="2c2cb976-af58-4c90-a3aa-d98ee0bd0fde",
    )
    request = captured["request"]
    assert request.headers["Idempotency-key"] == "2c2cb976-af58-4c90-a3aa-d98ee0bd0fde"
    assert request.data == (
        b'{"player_count":6,"ruleset_version":"mystery-v1","scenario_version":"scenario-v1"}'
    )


def test_home_game_list_load_starts_without_fresh_data_or_error(monkeypatch) -> None:
    monkeypatch.setattr("frontend_user.app_pages.home_page.monotonic", lambda: 100)
    assert should_load_games({}) is True
    assert should_load_games({"home.games": []}) is True
    assert should_load_games({"home.games": [], "home.games_loaded_at": 100}) is False
    assert should_load_games({"home.games": [], "home.games_loaded_at": 70}) is True
    assert should_load_games({"home.games": [], "home.games_loaded_at": "invalid"}) is True
    assert should_load_games({"home.games_loading": True}) is False
    assert should_load_games({"home.games_error": "DEPENDENCY_UNAVAILABLE"}) is False


@pytest.mark.parametrize("count", [6, 7, 8, 9])
def test_create_success_moves_to_role_reveal_without_another_click(count):
    client = _Client()
    app = AppTest.from_function(_create_app, args=(client,)).run()
    app.button(key=f"game.player_count.{count}").click().run()
    app.button(key="game.create_submit").click().run()

    assert not app.exception
    assert app.session_state["navigation.page"] == "game"
    assert "역할 공개 화면으로 이동" in _html(app)
    assert len(client.created) == 1
    app.run()
    assert len(client.created) == 1


def test_new_create_entry_discards_previous_success():
    client = _Client()
    app = AppTest.from_function(_create_app, args=(client,))
    app.session_state["game.create_pending"] = {
        "status": "SUCCEEDED", "game_id": "previous-game", "snapshot": {},
    }
    app.session_state["navigation.page"] = "create"
    app.run()

    assert not app.exception
    assert app.session_state["navigation.page"] == "create"
    assert "game.create_pending" not in app.session_state
    assert not client.created
    app.button(key="game.create_submit").click().run()
    assert app.session_state["game.create_pending"]["game_id"] == GAME_ID
    assert len(client.created) == 1


def test_creation_complete_uses_snapshot_names_and_escapes_public_fields():
    pending = {
        "game_id": "<b>synthetic-id</b>",
        "snapshot": {"data": {
            "game": {"player_count": 6},
            "players": [{"display_name": "<script>참가자</script>"}],
        }},
    }
    app = AppTest.from_function(_completed_app, args=(pending,)).run()

    assert not app.exception
    html = _html(app)
    assert "&lt;script&gt;참가자&lt;/script&gt;" in html
    assert "&lt;b&gt;synthetic-id&lt;/b&gt;" in html
    assert "민수" not in html


def test_home_displays_three_resumable_and_completed_games():
    client = _Client()
    client.items = [
        {"game_id": f"{status}-{index}", "scenario_title": f"{status} 사건 {index}",
         "status": status, "can_resume": status == "SAVED"}
        for status in ("SAVED", "COMPLETED") for index in range(4)
    ]
    app = AppTest.from_function(_home_app, args=(client,)).run()

    assert not app.exception
    for status in ("SAVED", "COMPLETED"):
        for index in range(3):
            assert f"{status} 사건 {index}" in _html(app)
        assert f"{status} 사건 3" not in _html(app)
    assert app.button(key="home.card.COMPLETED-0").label == "결과 보기  ›"


def test_unknown_create_keeps_body_and_key_until_same_request_retry():
    client = _Client()
    client.create_error = True
    app = AppTest.from_function(_create_app, args=(client,)).run()
    app.button(key="game.player_count.8").click().run()
    app.button(key="game.create_submit").click().run()

    assert not app.exception
    assert app.session_state["game.create_pending"]["status"] == "RETRYABLE_UNKNOWN"
    for key in ("game.create_submit", "game.create_cancel", "game.player_count.6"):
        assert app.button(key=key).disabled
    app.run()
    assert len(client.created) == 1
    client.create_error = False
    app.button(key="game.create_retry").click().run()
    assert not app.exception
    assert client.created[0] == client.created[1]
    assert client.created[1]["player_count"] == 8
    assert "역할 공개 화면으로 이동" in _html(app)


@pytest.mark.parametrize("snapshot", [None, {"data": None}, {"data": {"players": []}}])
def test_creation_complete_does_not_fabricate_missing_players(snapshot):
    app = AppTest.from_function(
        _completed_app, args=({"game_id": GAME_ID, "snapshot": snapshot},),
    ).run()

    assert not app.exception
    assert app.warning
    assert "민수" not in _html(app)
    assert "6명의 플레이어" not in _html(app)


def test_home_empty_list_is_not_an_error():
    client = _Client()
    app = AppTest.from_function(_home_app, args=(client,)).run()

    assert not app.exception
    assert not app.error
    assert "아직 게임이 없어요" in app.info[0].value
    app.run()
    assert client.loaded == [{"limit": 20}]


def test_home_loading_state_does_not_show_empty_list():
    app = AppTest.from_function(_home_render_app, args=(_Client(),))
    app.session_state["home.games_loading"] = True
    app.run()

    assert not app.exception
    assert app.info[0].value == "게임 목록을 불러오는 중이에요."


def test_home_without_uuid_disables_start_and_hides_cached_games():
    """UUID 초기화 전에는 이전에 남은 모든 상태의 게임 카드도 노출하지 않는다."""

    client = _Client()
    games = [
        {"game_id": f"cached-{status}", "status": status, "scenario_title": f"캐시 사건 {status}"}
        for status in ("IN_PROGRESS", "SAVED", "COMPLETED", "FAILED")
    ]
    app = AppTest.from_function(
        _home_render_app, args=(client,), kwargs={"identity_ready": False},
    )
    app.session_state["home.games"] = games
    app.run()

    assert not app.exception
    assert "identity.user_id" not in app.session_state
    assert app.text_input(key="home.user_id").value == ""
    assert app.button(key="home.new_game").disabled
    home_tab = next(tab for tab in app.tabs if tab.label == "홈")
    assert [info.value for info in home_tab.info] == [
        "게임 식별자를 확인한 뒤 게임 목록을 불러올 수 있어요.",
    ]
    assert not home_tab.button
    assert all(game["scenario_title"] not in _html(app) for game in games)
    assert app.session_state["home.games"] == games
    assert not client.loaded
    assert not client.created


def test_home_error_waits_for_retry_and_then_displays_latest_list():
    client = _Client()
    client.list_error = True
    app = AppTest.from_function(_home_app, args=(client,)).run()

    assert not app.exception
    assert app.error
    app.run()
    assert len(client.loaded) == 1
    client.list_error = False
    client.items = [{"game_id": GAME_ID, "status": "COMPLETED", "scenario_title": "완료 사건"}]
    app.button(key="home.retry").click().run()
    assert not app.exception
    assert not app.error
    assert "완료 사건" in _html(app)
    assert "진행 중이거나 저장된 게임이 없습니다." in [c.value for c in app.caption]
    assert len(client.loaded) == 2


@pytest.mark.parametrize("response", [
    {}, {"data": {}}, {"data": {"items": "invalid"}},
    {"data": {"items": [{"game_id": GAME_ID, "status": []}]}},
    {"data": {"items": [{"game_id": "", "status": "SAVED"}]}},
])
def test_home_malformed_response_shows_error_instead_of_empty(response):
    client = _Client()
    client.list_response = response
    app = AppTest.from_function(_home_app, args=(client,)).run()

    assert not app.exception
    assert app.error
    # 커스텀 직업 탭의 상시 안내와 분리해 홈에 빈 목록 안내가 없는지 확인한다.
    home_tab = next(tab for tab in app.tabs if tab.label == "홈")
    assert not home_tab.info
    assert app.session_state["home.games_error"] == "INVALID_RESPONSE"
    assert "home.games" not in app.session_state


def test_home_manual_refresh_replaces_cached_list():
    client = _Client()
    app = AppTest.from_function(_home_app, args=(client,)).run()
    client.items = [{"game_id": GAME_ID, "status": "SAVED", "scenario_title": "저장 사건"}]
    app.button(key="home.refresh").click().run()

    assert not app.exception
    assert "저장 사건" in _html(app)
    assert len(client.loaded) == 2


@pytest.mark.parametrize("status", ["IN_PROGRESS", "SAVED", "COMPLETED", "FAILED"])
def test_home_game_navigation_invalidates_cache_for_next_home_entry(status):
    client = _Client()
    client.items = [{"game_id": GAME_ID, "status": status, "scenario_title": "변경 전 사건"}]
    app = AppTest.from_function(_home_app, args=(client,)).run()
    app.button(key=f"home.card.{GAME_ID}").click().run()

    assert not app.exception
    assert app.session_state["navigation.page"] == "game"
    assert app.session_state["game.game_id"] == GAME_ID
    assert "home.games" not in app.session_state
    client.items = [{"game_id": GAME_ID, "status": "SAVED", "scenario_title": "저장 후 사건"}]
    app.session_state["navigation.page"] = "home"
    app.run()
    assert not app.exception
    assert "저장 후 사건" in _html(app)
    assert "변경 전 사건" not in _html(app)
    assert len(client.loaded) == 2


@pytest.mark.parametrize("button, page", [("home.new_game", "create"), ("home.feedback", "feedback")])
def test_home_navigation_reaches_create_or_general_feedback(button, page):
    client = _Client()
    app = AppTest.from_function(_home_app, args=(client,)).run()
    app.button(key=button).click().run()

    assert not app.exception
    assert app.session_state["navigation.page"] == page
    assert "home.games" not in app.session_state


class _CustomClient(_Client):
    """B16 catalog envelope을 합성하여 Front만의 생성·재시도 계약을 검증한다."""

    def __init__(self):
        super().__init__()
        self.catalog = {"data": {"catalog_version": "custom-role-v1", "abilities": [
            {"id": "night.attack.v1", "label": "공격", "factions": ["MAFIA"]},
            {"id": "night.investigate.v1", "label": "조사", "factions": ["CITIZEN", "MAFIA"]},
            {"id": "night.protect.v1", "label": "보호", "factions": ["CITIZEN", "MAFIA"]},
            {"id": "vote.triple.v1", "label": "투표 조작", "factions": ["CITIZEN", "MAFIA"]},
            {"id": "intel.special_roles.v1", "label": "특수 직업 열람", "factions": ["CITIZEN", "MAFIA"]},
        ]}}

    def get_custom_role_abilities(self):
        """외부 API 없이 실패와 catalog 교체를 재현한다."""

        if isinstance(self.catalog, Exception):
            raise self.catalog
        return self.catalog


def test_custom_create_normalizes_and_retries_identical_payload():
    client = _CustomClient()
    client.create_error = True
    app = AppTest.from_function(_create_app, args=(client,)).run()
    app.radio(key="game.create_mode").set_value("CUSTOM_ROLE").run()
    app.text_input(key="game.create_role_name").set_value("  가   감식관 ").run()
    app.multiselect(key="game.create_abilities.CITIZEN").set_value(["night.investigate.v1", "night.protect.v1"]).run()
    app.button(key="game.create_submit").click().run()
    first = dict(client.created[-1])
    assert first["custom_role"] == {"name": "가 감식관", "faction": "CITIZEN",
        "catalog_version": "custom-role-v1", "ability_ids": ["night.investigate.v1", "night.protect.v1"]}
    assert app.radio(key="game.create_mode").disabled
    app.button(key="game.create_retry").click().run()
    assert client.created[-1] == first
    assert not app.exception


@pytest.mark.parametrize("catalog", [None, {"data": {"catalog_version": "old", "abilities": []}},
    ApiUnavailableError(status_code=503, code="DEPENDENCY_UNAVAILABLE")])
def test_catalog_failure_blocks_only_custom_and_has_retry(catalog):
    client = _CustomClient()
    client.catalog = catalog
    app = AppTest.from_function(_create_app, args=(client,)).run()
    app.radio(key="game.create_mode").set_value("CUSTOM_ROLE").run()
    assert app.button(key="game.create_submit").disabled
    assert app.button(key="game.catalog_retry")
    app.radio(key="game.create_mode").set_value("STANDARD").run()
    assert not app.button(key="game.create_submit").disabled
    assert not app.exception


def test_mafia_attack_mandatory_and_citizen_selection_survives_rerun():
    client = _CustomClient()
    app = AppTest.from_function(_create_app, args=(client,)).run()
    app.radio(key="game.create_mode").set_value("CUSTOM_ROLE").run()
    app.text_input(key="game.create_role_name").set_value("잠입자").run()
    app.multiselect(key="game.create_abilities.CITIZEN").set_value(["night.protect.v1"]).run()
    app.run()
    assert app.multiselect(key="game.create_abilities.CITIZEN").value == ["night.protect.v1"]
    app.radio(key="game.create_faction").set_value("MAFIA").run()
    assert app.multiselect(key="game.create_abilities.MAFIA").options == ["조사", "보호", "투표 조작", "특수 직업 열람"]
    app.button(key="game.create_submit").click().run()
    assert client.created[-1]["custom_role"]["ability_ids"] == ["night.attack.v1"]
    assert not app.exception


def test_validation_correction_creates_new_body_and_key():
    from frontend_user.core.api_client import ApiResponseError

    class RejectClient(_CustomClient):
        """명시적 거부는 결과 불명과 달리 새 요청으로 교정할 수 있어야 한다."""

        def create_game(self, **body):
            self.created.append(body)
            raise ApiResponseError(status_code=422, code="VALIDATION_ERROR")

    client = RejectClient()
    app = AppTest.from_function(_create_app, args=(client,)).run()
    app.radio(key="game.create_mode").set_value("CUSTOM_ROLE").run()
    app.radio(key="game.create_faction").set_value("MAFIA").run()
    app.text_input(key="game.create_role_name").set_value("첫 직업").run()
    app.button(key="game.create_submit").click().run()
    first = client.created[-1]
    app.text_input(key="game.create_role_name").set_value("새 직업").run()
    app.button(key="game.create_submit").click().run()
    assert client.created[-1]["idempotency_key"] != first["idempotency_key"]
    assert first["custom_role"]["name"] == "첫 직업"
    assert client.created[-1]["custom_role"]["name"] == "새 직업"
    assert not app.exception


def test_catalog_retry_recovers_custom_form_without_creating_game():
    client = _CustomClient()
    catalog = client.catalog
    client.catalog = {"data": {"catalog_version": "custom-role-v1", "abilities": []}}
    app = AppTest.from_function(_create_app, args=(client,)).run()
    app.radio(key="game.create_mode").set_value("CUSTOM_ROLE").run()
    assert app.button(key="game.create_submit").disabled
    client.catalog = catalog
    app.button(key="game.catalog_retry").click().run()
    assert len(app.multiselect) == 1
    assert not client.created
    assert not app.exception



@pytest.mark.parametrize("mutation", [
    "bare", "old", "missing", "duplicate", "unknown", "extra", "wrong_label", "label_type",
    "faction_type", "faction_order", "faction_duplicate", "attack_citizen", "data_extra", "descriptor_missing",
])
def test_catalog_exact_closed_descriptors_fail_closed(mutation):
    from copy import deepcopy
    from frontend_user.core.commands import validate_ability_catalog

    response = deepcopy(_CustomClient().catalog)
    data = response["data"]
    item = data["abilities"][1]
    if mutation == "bare":
        response = data
    elif mutation == "old":
        data["catalog_version"] = "old"
    elif mutation == "missing":
        data["abilities"].pop()
    elif mutation == "duplicate":
        data["abilities"][-1] = dict(item)
    elif mutation == "unknown":
        item["id"] = "unknown.v1"
    elif mutation == "extra":
        item["tool"] = "raw"
    elif mutation == "wrong_label":
        item["label"] = "다른 이름"
    elif mutation == "label_type":
        item["label"] = []
    elif mutation == "faction_type":
        item["factions"] = "CITIZEN"
    elif mutation == "faction_order":
        item["factions"].reverse()
    elif mutation == "faction_duplicate":
        item["factions"] = ["CITIZEN", "CITIZEN", "MAFIA"]
    elif mutation == "attack_citizen":
        data["abilities"][0]["factions"] = ["CITIZEN", "MAFIA"]
    elif mutation == "data_extra":
        data["extra"] = True
    else:
        item.pop("factions")
    assert validate_ability_catalog(response) is None


def test_custom_citizen_accepts_only_day_and_intel_abilities():
    client = _CustomClient()
    app = AppTest.from_function(_create_app, args=(client,)).run()
    app.radio(key="game.create_mode").set_value("CUSTOM_ROLE").run()
    app.text_input(key="game.create_role_name").set_value("기록관").run()
    app.multiselect(key="game.create_abilities.CITIZEN").set_value(["vote.triple.v1", "intel.special_roles.v1"]).run()
    app.button(key="game.create_submit").click().run()
    assert not app.exception
    assert client.created[-1]["custom_role"]["ability_ids"] == ["vote.triple.v1", "intel.special_roles.v1"]
