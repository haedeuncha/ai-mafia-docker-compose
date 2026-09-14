"""Frontend 공개 Backend API client의 최소 연결 계약을 검증한다."""

from __future__ import annotations

import json
from urllib.error import URLError
from uuid import UUID

import pytest

from frontend_user.core.api_client import (
    ApiClient,
    ApiClientConfigurationError,
    ApiResponseError,
    ApiUnavailableError,
)

USER_ID = "83d40f36-e835-4a1d-88db-e59b6920b739"
REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")


def _header(headers: dict[str, str], name: str) -> str:
    """urllib 요청 객체의 header 표기 대소문자 차이를 흡수해 값을 조회한다."""

    return next(value for key, value in headers.items() if key.lower() == name.lower())


def test_health_and_ready_use_the_backend_public_paths() -> None:
    """상태 확인 메서드가 인증·게임 header 없이 공개 경계를 호출하는지 확인한다."""

    captured: list[tuple[str, str, dict[str, str]]] = []

    def transport(request, timeout):
        del timeout
        captured.append((request.method, request.full_url, dict(request.headers)))
        payload = {"status": "ok"} if request.full_url.endswith("/health") else {"status": "ready"}
        return 200, json.dumps(payload).encode()

    client = ApiClient(user_id=USER_ID, transport=transport, request_id_factory=lambda: REQUEST_ID)

    assert client.get_health() == {"status": "ok"}
    assert client.get_ready() == {"status": "ready"}
    assert [(method, url) for method, url, _ in captured] == [
        ("GET", "http://127.0.0.1:8000/health"),
        ("GET", "http://127.0.0.1:8000/ready"),
    ]
    assert all(_header(headers, "X-Request-Id") == str(REQUEST_ID) for _, _, headers in captured)
    assert all(_header(headers, "X-User-Id") == USER_ID for _, _, headers in captured)


def test_write_request_sends_uuid_idempotency_key_and_contract_body() -> None:
    """게임 생성 요청이 정본 body와 UUID v4 멱등키를 함께 보내는지 확인한다."""

    captured = {}

    def transport(request, timeout):
        del timeout
        captured["request"] = request
        return 201, b'{"data":{"game_id":"d9ae9b5d-1d17-4f80-8f1a-276bfe170412"}}'

    client = ApiClient(user_id=USER_ID, transport=transport)
    client.create_game(
        player_count=6,
        idempotency_key="2c2cb976-af58-4c90-a3aa-d98ee0bd0fde",
    )

    request = captured["request"]
    assert request.method == "POST"
    assert _header(request.headers, "Idempotency-Key") == "2c2cb976-af58-4c90-a3aa-d98ee0bd0fde"
    assert _header(request.headers, "X-User-Id") == USER_ID
    assert request.data == (
        b'{"player_count":6,"ruleset_version":"mystery-v1","scenario_version":"scenario-v1"}'
    )


def test_error_envelope_is_converted_without_exposing_message() -> None:
    """Backend error envelope의 code와 status만 Client 오류로 전달한다."""

    def transport(request, timeout):
        del request, timeout
        return 409, b'{"error":{"code":"STALE_STATE","message":"private detail"}}'

    client = ApiClient(user_id=USER_ID, transport=transport)
    with pytest.raises(ApiResponseError) as raised:
        client.get_games()

    assert raised.value.status_code == 409
    assert raised.value.code == "STALE_STATE"
    assert "private detail" not in str(raised.value)


def test_invalid_uuid_and_transport_failure_are_rejected() -> None:
    """잘못된 사용자 식별자와 네트워크 실패를 Backend 오류와 구분한다."""

    with pytest.raises(ValueError, match="UUID v4"):
        ApiClient(user_id="00000000-0000-0000-0000-000000000000")

    def broken_transport(request, timeout):
        del request, timeout
        raise URLError("synthetic outage")

    client = ApiClient(user_id=USER_ID, transport=broken_transport)
    with pytest.raises(ApiUnavailableError) as raised:
        client.get_health()
    assert raised.value.status_code == 503
    assert raised.value.code == "DEPENDENCY_UNAVAILABLE"


def test_backend_url_rejects_untrusted_http_endpoint() -> None:
    """개발 loopback 외의 평문 Backend endpoint를 차단한다."""

    with pytest.raises(ApiClientConfigurationError):
        ApiClient(user_id=USER_ID, api_url="http://backend.internal:8000")


def test_delete_game_sends_owner_and_confirmed_version() -> None:
    """삭제 대상과 확인 버전을 보내며 다른 게임이나 암묵적 최신 버전으로 바꾸지 않는다."""

    captured = []

    def transport(request, timeout):
        captured.append(request)
        return 200, b'{"data":{"deleted":true}}'

    client = ApiClient(user_id=USER_ID, transport=transport)
    client.delete_game(game_id=REQUEST_ID, expected_state_version=12)
    request = captured[0]
    assert request.method == "DELETE"
    assert request.full_url.endswith(f"/api/v1/games/{REQUEST_ID}?expected_state_version=12")
    assert _header(request.headers, "X-User-Id") == USER_ID
    assert request.data is None
    for version in (True, 0, -1, "12"):
        with pytest.raises(ValueError):
            client.delete_game(game_id=REQUEST_ID, expected_state_version=version)
    assert len(captured) == 1


def test_custom_catalog_and_create_preserve_public_contract():
    """catalog GET과 CUSTOM_ROLE body에 공개 versioned ID만 전달되는지 검증한다."""

    requests = []
    def transport(request, timeout):
        requests.append(request)
        return 200, b'{"data": {}}'
    client = ApiClient(user_id=USER_ID, transport=transport)
    client.get_custom_role_abilities()
    custom = {"name": "감식관", "faction": "CITIZEN", "catalog_version": "custom-role-v1",
              "ability_ids": ["night.investigate.v1"]}
    client.create_game(player_count=6, idempotency_key=REQUEST_ID, mode="CUSTOM_ROLE", custom_role=custom)
    assert requests[0].full_url.endswith("/api/v1/game-config/custom-role-abilities")
    body = json.loads(requests[1].data)
    assert body == {"player_count": 6, "ruleset_version": "mystery-v1", "scenario_version": "scenario-v1",
                    "mode": "CUSTOM_ROLE", "custom_role": custom}
    assert _header(dict(requests[1].headers), "Idempotency-Key") == str(REQUEST_ID)


def test_special_roles_uses_public_get_identity_header_without_actor_or_cache():
    requests = []

    def transport(request, timeout):
        requests.append(request)
        return 200, b'{"data": {"roles": []}}'

    client = ApiClient(user_id=USER_ID, transport=transport)
    client.get_special_roles(REQUEST_ID)
    client.get_special_roles(REQUEST_ID)
    assert len(requests) == 2
    assert requests[0].method == "GET"
    assert requests[0].full_url.endswith(f"/api/v1/games/{REQUEST_ID}/special-roles")
    assert _header(requests[0].headers, "X-User-Id") == USER_ID
    assert requests[0].data is None
