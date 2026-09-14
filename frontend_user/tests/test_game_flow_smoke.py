"""Frontend API client가 Backend 정본 게임 흐름을 소비하는지 검증한다."""

import json
from urllib.request import Request
from uuid import UUID

from frontend_user.core.api_client import ApiClient
from frontend_user.core.sync import apply_envelope


USER = UUID("00000000-0000-4000-8000-000000000101")
GAME = "00000000-0000-4000-8000-000000000201"


def test_frontend_consumes_create_snapshot_command_and_sync_contract() -> None:
    """실제 DB 없이도 Front가 Backend 응답만으로 cursor를 갱신하는지 확인한다."""

    def transport(request: Request, _timeout: float) -> tuple[int, bytes]:
        path = request.full_url
        if request.method == "POST" and path.endswith("/api/v1/games"):
            body = {"data": {"game_id": GAME, "state_version": 1}}
        elif request.method == "GET" and path.endswith(f"/api/v1/games/{GAME}"):
            body = {"data": {"game": {"game_id": GAME, "state_version": 1, "last_sequence": 0}}}
        elif request.method == "POST" and path.endswith(f"/api/v1/games/{GAME}/commands"):
            body = {"data": {"command_type": "BEGIN_GAME", "result_state_version": 2}}
        elif request.method == "GET" and "/sync?" in path:
            body = {
                "data": {
                    "game_id": GAME,
                    "mode": "DELTA",
                    "from_state_version": 1,
                    "state_version": 2,
                    "last_sequence": 1,
                    "snapshot": None,
                    "operations": [
                        {
                            "schema_version": 1,
                            "front_sequence": 1,
                            "operation_index": 0,
                            "state_version": 2,
                            "type": "SET_GAME_STATE",
                            "payload": {
                                "status": "IN_PROGRESS",
                                "phase": "DAY_DISCUSSION",
                                "round": 1,
                                "day_number": 1,
                                "state_version": 2,
                                "fast_forward_enabled": False,
                            },
                        }
                    ],
                }
            }
        else:
            raise AssertionError(f"unexpected request: {request.method} {path}")
        return 200 if request.method != "POST" or path.endswith("commands") else 201, json.dumps(body).encode()

    client = ApiClient(user_id=USER, transport=transport)
    created = client.create_game(player_count=6, idempotency_key=UUID(int=301))
    game_id = created["data"]["game_id"]
    snapshot = client.get_game(game_id)["data"]
    command = client.submit_command(
        game_id=game_id,
        command={"type": "BEGIN_GAME", "expected_state_version": 1},
        idempotency_key=UUID(int=302),
    )
    envelope = client.get_sync(game_id=game_id, after_state_version=1, after_sequence=0)
    updated, mode = apply_envelope(snapshot=snapshot, envelope=envelope)

    assert command["data"]["result_state_version"] == 2
    assert mode == "DELTA"
    assert updated["game"]["state_version"] == 2
    assert updated["game"]["last_sequence"] == 1
    assert updated["game"]["phase"] == "DAY_DISCUSSION"
    assert snapshot["game"]["state_version"] == 1
    assert snapshot["game"]["last_sequence"] == 0
