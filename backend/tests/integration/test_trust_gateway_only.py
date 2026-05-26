"""BACKEND_TRUST_GATEWAY_ONLY 行為測試。

v1.1.13 起 docker-compose 預設啟用此旗標:啟用後 backend 對 /api/* 強制要求
合法 X-Gateway-Verified HMAC,任何繞 gateway 的直接呼叫一律 401(不退回
獨立 JWT 驗證)。Public path(/healthz、/pics、/results、recorder upload)
不受影響,因 middleware 在 trust-gateway 檢查之前就放行了。
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid as _uuid

import pytest

pytestmark = pytest.mark.integration


_TEST_SECRET = "test-gateway-shared-secret-very-long-32bytes-or-more"


def _sign(method: str, path: str, sub: str, ts: int) -> str:
    """跟 gateway/app/auth.py:sign_gateway_request 同樣的簽章邏輯。"""
    msg = f"{method.upper()}\n{path}\n{sub}\n{ts}".encode("utf-8")
    return hmac.new(_TEST_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@pytest.fixture
def trust_gateway_only(monkeypatch):
    """把 middleware 切到 TRUST_GATEWAY_ONLY=True + 注入 shared secret。

    要 patch module-level 變數(import 時就讀)而非 env,因為 env 只在 module
    第一次載入時取用。
    """
    import app.middleware as mw

    monkeypatch.setattr(mw, "_GW_TRUST_GATEWAY_ONLY", True)
    monkeypatch.setattr(mw, "_GW_SHARED_SECRET", _TEST_SECRET)
    yield


async def test_api_without_gateway_hmac_is_rejected(client, viewer_in_a, trust_gateway_only) -> None:
    """啟用 TRUST_GATEWAY_ONLY 後,即使帶合法 JWT 也要 401(沒帶 gateway HMAC)。"""
    resp = await client.get("/api/projects", headers=viewer_in_a.headers)
    assert resp.status_code == 401
    assert "gateway" in resp.json()["detail"].lower()


async def test_api_with_valid_gateway_hmac_passes(client, viewer_in_a, trust_gateway_only) -> None:
    """帶合法 X-Gateway-Verified HMAC → 通過驗章,backend 信任 gateway 給的身份。"""
    sub = viewer_in_a.username
    ts = int(time.time())
    sig = _sign("GET", "/api/projects", sub, ts)
    headers = {
        **viewer_in_a.headers,
        "X-Gateway-Verified": sig,
        "X-Gateway-Timestamp": str(ts),
        "X-Gateway-Sub": sub,
        "X-Gateway-User": sub,
        "X-Gateway-Org": viewer_in_a.org.org_id,
    }
    resp = await client.get("/api/projects", headers=headers)
    assert resp.status_code == 200


async def test_api_with_tampered_hmac_rejected(client, viewer_in_a, trust_gateway_only) -> None:
    """簽章對不上 → 視為沒簽,TRUST_GATEWAY_ONLY 下 401。"""
    sub = viewer_in_a.username
    ts = int(time.time())
    headers = {
        **viewer_in_a.headers,
        "X-Gateway-Verified": "0" * 64,
        "X-Gateway-Timestamp": str(ts),
        "X-Gateway-Sub": sub,
        "X-Gateway-User": sub,
    }
    resp = await client.get("/api/projects", headers=headers)
    assert resp.status_code == 401


async def test_api_with_stale_timestamp_rejected(client, viewer_in_a, trust_gateway_only) -> None:
    """timestamp 超過 30 秒容差 → 視為沒簽,401(防 replay)。"""
    sub = viewer_in_a.username
    ts = int(time.time()) - 120
    sig = _sign("GET", "/api/projects", sub, ts)
    headers = {
        **viewer_in_a.headers,
        "X-Gateway-Verified": sig,
        "X-Gateway-Timestamp": str(ts),
        "X-Gateway-Sub": sub,
    }
    resp = await client.get("/api/projects", headers=headers)
    assert resp.status_code == 401


async def test_healthz_still_public(client, trust_gateway_only) -> None:
    """/healthz 不在 /api/* 下,完全繞過 AuthMiddleware,即使開啟旗標也要能用
    (compose healthcheck 走 127.0.0.1:8000/healthz 依賴這條)。"""
    resp = await client.get("/healthz")
    assert resp.status_code == 200


async def test_recorder_upload_public_path_passes_middleware(client, trust_gateway_only) -> None:
    """/api/recordings/.../upload 是 public path,middleware 在 trust-gateway
    檢查之前已放行;recorder 容器走 RECORDER_INTERNAL_BASE_URL 直接打 backend
    (不經 gateway)能正常上傳。

    這裡只驗 middleware 沒擋(沒回 401);實際 sessionid 對不上會由 route handler
    回 404,但**不會** 401。
    """
    fake_session_id = _uuid.uuid4().hex
    # 故意不帶任何 token / HMAC,模擬容器內 anonymous curl
    resp = await client.post(f"/api/recordings/{fake_session_id}/upload")
    # 預期 404(session 不存在)或 400/422(缺檔案),不應是 401。
    assert resp.status_code != 401, (
        f"recorder upload public path 在 TRUST_GATEWAY_ONLY 下被誤擋為 401:"
        f"middleware public-path 放行邏輯壞了"
    )
