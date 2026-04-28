"""mitmproxy addon:邊跑邊把 flows 寫成 HAR。

簡化自 mitmproxy 官方 examples/contrib/har_dump.py,只保留我們需要的欄位。
mitmproxy 載入時會 auto-instantiate addon class;done() 在 mitmproxy 結束時呼叫,
SIGTERM 觸發 entrypoint 收尾邏輯前,本 addon 已把每個 response 寫入 entries。

寫成 HAR 1.2 格式,backend 解析時用 json.load 即可,不需要 mitmproxy 套件。
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import http
from mitmproxy.utils import strutils


HAR_OUTPUT = os.environ.get("HAR_OUTPUT", "/work/flows.har")


class HarDump:
    def __init__(self) -> None:
        self.har: dict = {
            "log": {
                "version": "1.2",
                "creator": {"name": "autotest-recorder-api", "version": "1.0"},
                "entries": [],
            }
        }

    @staticmethod
    def _format_iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat()

    def _request_to_har(self, req: http.Request) -> dict:
        return {
            "method": req.method,
            "url": req.pretty_url,
            "httpVersion": req.http_version or "HTTP/1.1",
            "headers": [{"name": k, "value": v} for k, v in req.headers.items(multi=True)],
            "queryString": [{"name": k, "value": v} for k, v in req.query.items(multi=True)],
            "headersSize": -1,
            "bodySize": len(req.raw_content or b""),
            "postData": (
                {
                    "mimeType": req.headers.get("content-type", ""),
                    "text": (
                        req.get_text(strict=False)
                        if req.raw_content and len(req.raw_content) <= 1_000_000
                        else ""
                    ),
                }
                if req.raw_content
                else {}
            ),
        }

    def _response_to_har(self, resp: http.Response) -> dict:
        body_text = ""
        encoding = ""
        if resp.raw_content and len(resp.raw_content) <= 1_000_000:
            try:
                body_text = resp.get_text(strict=False)
            except Exception:
                body_text = base64.b64encode(resp.raw_content).decode("ascii")
                encoding = "base64"
        return {
            "status": resp.status_code,
            "statusText": resp.reason or "",
            "httpVersion": resp.http_version or "HTTP/1.1",
            "headers": [{"name": k, "value": v} for k, v in resp.headers.items(multi=True)],
            "cookies": [],
            "content": {
                "size": len(resp.raw_content or b""),
                "mimeType": resp.headers.get("content-type", ""),
                "text": body_text,
                **({"encoding": encoding} if encoding else {}),
            },
            "redirectURL": resp.headers.get("location", ""),
            "headersSize": -1,
            "bodySize": len(resp.raw_content or b""),
        }

    def response(self, flow: http.HTTPFlow) -> None:
        if not flow.response or not flow.request:
            return
        try:
            started = (
                datetime.fromtimestamp(flow.request.timestamp_start, tz=timezone.utc)
                if flow.request.timestamp_start
                else datetime.now(timezone.utc)
            )
        except Exception:
            started = datetime.now(timezone.utc)

        time_ms = 0
        try:
            if flow.response.timestamp_end and flow.request.timestamp_start:
                time_ms = int(
                    (flow.response.timestamp_end - flow.request.timestamp_start) * 1000
                )
        except Exception:
            pass

        self.har["log"]["entries"].append({
            "startedDateTime": self._format_iso(started),
            "time": time_ms,
            "request": self._request_to_har(flow.request),
            "response": self._response_to_har(flow.response),
            "cache": {},
            "timings": {"send": 0, "wait": time_ms, "receive": 0},
        })
        # 即時寫入 — 容器被 SIGKILL 時也能保住已擷取的部分
        self._flush()

    def done(self) -> None:
        """mitmproxy shutdown 時呼叫;最後 flush 一次。"""
        self._flush()

    def _flush(self) -> None:
        try:
            tmp = Path(HAR_OUTPUT).with_suffix(".har.tmp")
            tmp.write_text(json.dumps(self.har, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, HAR_OUTPUT)
        except Exception:
            pass


addons = [HarDump()]
