"""Streamlit Python을 점유하지 않는 browser SSE component 계약을 검증한다."""

import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from frontend_user.core.sync_policy import DEFAULT_SYNC_POLICY


def test_sse_component_uses_browser_fetch_and_abortable_lifecycle() -> None:
    """실제 JS의 요청 cursor와 unmount 중단을 검증해 내부 변수명 변경에 영향받지 않는다."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("브라우저 SSE lifecycle 검증에는 Node.js가 필요합니다.")
    source = (
        Path(__file__).parents[1] / "components" / "browser_components" / "sync" / "index.js"
    ).read_text(encoding="utf-8")
    # 재연결·backoff·no-op·scope 교체는 test_sync_f5.py의 가상 시계 테스트가 다룬다.
    # 이 경계에서는 응답 대기 중인 두 transport를 실제 cleanup이 중단하는지 확인한다.
    script = r"""
import assert from 'node:assert/strict';
const {default: render} = await import('data:text/javascript;base64,' + Buffer.from(SOURCE_TEXT).toString('base64'));
globalThis.document = Object.assign(new EventTarget(), {visibilityState: 'visible', querySelector: () => null});
globalThis.window = new EventTarget();
globalThis.requestAnimationFrame = fn => setTimeout(fn, 0);
Object.defineProperty(globalThis, 'navigator', {value: {onLine: true}, configurable: true});
const requests = [], outputs = [];
globalThis.fetch = (url, options) => {
  requests.push({url: new URL(url), options});
  return new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('테스트 요청 중단')), {once: true});
  });
};
const cleanup = render({
  data: {backend_url: 'http://127.0.0.1:8000', game_id: 'game-one', user_id: 'user-one',
    component_instance_id: 'component-one', scope_version: 'scope-one', schema_version: 1,
    last_sequence: 42, after_sequence: 42, after_state_version: 12, policy: POLICY_VALUE},
  parentElement: {ownerDocument: document}, setStateValue: (key, value) => outputs.push({key, value}),
});
assert.equal(typeof cleanup, 'function');
assert.equal(requests.length, 1);
const sse = requests[0];
assert.equal(sse.url.pathname, '/api/v1/games/game-one/events');
assert.equal(sse.options.headers.Accept, 'text/event-stream');
assert.equal(sse.options.headers['Last-Event-ID'], '42');
window.dispatchEvent(new Event('online'));
assert.equal(requests.length, 2);
const poll = requests[1];
assert.equal(poll.url.pathname, '/api/v1/games/game-one/sync');
assert.equal(poll.url.searchParams.get('after_sequence'), '42');
assert.equal(poll.url.searchParams.get('after_state_version'), '12');
assert.ok(requests.every(request => request.options.headers['X-User-Id'] === 'user-one'));
assert.ok(requests.every(request => request.options.signal instanceof AbortSignal && !request.options.signal.aborted));
cleanup();
await new Promise(resolve => setTimeout(resolve, 0));
assert.ok(requests.every(request => request.options.signal.aborted));
const outputCount = outputs.length;
window.dispatchEvent(new Event('online'));
window.dispatchEvent(new Event('offline'));
document.dispatchEvent(new Event('visibilitychange'));
await new Promise(resolve => setTimeout(resolve, 0));
assert.equal(requests.length, 2);
assert.equal(outputs.length, outputCount);
""".replace("SOURCE_TEXT", json.dumps(source)).replace(
        "POLICY_VALUE", json.dumps(asdict(DEFAULT_SYNC_POLICY))
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
