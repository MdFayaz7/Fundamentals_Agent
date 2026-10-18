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
        resp = await self._request("GET", url, headers=headers, timeout=timeout, stream=True)
        try:
            if not 200 <= resp.status_code < 300:
                body = b""
                async for chunk in resp.aiter_content():
                    body += chunk
                    if len(body) >= 200:
                        break
                raise HttpError(resp.status_code, _snippet(body))
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise TooLarge(f"content-length {declared} > {max_bytes}")
            buf = bytearray()
            try:
                async for chunk in resp.aiter_content():
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise TooLarge(f"body exceeds {max_bytes} bytes")
            except TooLarge:
                raise
            except HttpError:
                raise
            except Exception as exc:
                raise HttpError(0, str(exc)[:200]) from exc
            return bytes(buf)
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass

    async def close(self) -> None:
        await self._session.close()
