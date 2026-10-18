from __future__ import annotations

from typing import Any

from curl_cffi.requests import AsyncSession


class HttpError(Exception):
    def __init__(self, status: int, body_snippet: str = ""):
        super().__init__(f"HTTP {status}: {body_snippet}")
        self.status = status
        self.body_snippet = body_snippet


class TooLarge(Exception):
    pass


def _snippet(data: bytes | str, n: int = 200) -> str:
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return data[:n]


class Http:
    def __init__(self, session: Any = None):
        self._session = session or AsyncSession(impersonate="chrome")

    async def _request(self, method: str, url: str, **kw):
        try:
            return await self._session.request(method, url, **kw)
        except HttpError:
            raise
        except Exception as exc:  # transport errors / timeouts
            raise HttpError(0, str(exc)[:200]) from exc

    @staticmethod
    def _json(resp) -> Any:
        if not 200 <= resp.status_code < 300:
            raise HttpError(resp.status_code, _snippet(resp.text))
        try:
            return resp.json()
        except Exception as exc:
            raise HttpError(resp.status_code, _snippet(resp.text)) from exc

    async def get_json(self, url: str, *, headers: dict | None = None, timeout: float = 20) -> Any:
        resp = await self._request("GET", url, headers=headers, timeout=timeout)
        return self._json(resp)

    async def post_json(
        self, url: str, payload: Any, *, headers: dict | None = None, timeout: float = 30
    ) -> tuple[int, dict, dict]:
        resp = await self._request("POST", url, json=payload, headers=headers, timeout=timeout)
        return resp.status_code, self._json(resp), dict(resp.headers)

    async def get_bytes(
        self, url: str, *, max_bytes: int, timeout: float = 30, headers: dict | None = None
    ) -> bytes:
        # Plain (non-streaming) download: curl_cffi's async stream was seen to end early on
        # multi-MB PDFs. Completeness is checked against Content-Length.
        resp = await self._request("GET", url, headers=headers, timeout=timeout)
        if not 200 <= resp.status_code < 300:
            raise HttpError(resp.status_code, _snippet(resp.content))
        body = resp.content
        if len(body) > max_bytes:
            raise TooLarge(f"body {len(body)} > {max_bytes} bytes")
        declared = resp.headers.get("content-length")
        compressed = resp.headers.get("content-encoding") not in (None, "", "identity")
        if declared and declared.isdigit() and not compressed and len(body) < int(declared):
            raise HttpError(0, f"truncated: got {len(body)} of {declared} bytes")
        return body

    async def close(self) -> None:
        await self._session.close()
