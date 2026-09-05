"""stdlib만으로 만든 세션 HTTP 클라이언트 + 요청 속도 제한.

외부 의존성을 두지 않는 이유: 이 도구는 사용자 PC에서 바로 돌아가야 하고,
pip 설치 단계가 하나라도 줄면 그만큼 덜 깨진다.

RateLimiter는 정책이 아니라 안전장치다. 예매 서버에 부담을 주면 차단되고,
차단되면 도구 자체가 무용지물이 되므로 최소 간격은 코드에서 강제한다.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import threading
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

from .errors import TransportError

log = logging.getLogger(__name__)

#: 어떤 설정을 하든 이보다 빠르게 같은 사업자를 조회하지 않는다.
MIN_INTERVAL_SEC = 2.0

DEFAULT_TIMEOUT = 12.0


class RateLimiter:
    """호출 간 최소 간격을 보장한다. 스레드 안전.

    지터를 섞는 이유는 서버 부담 분산과, 정확히 N초 주기로 찍히는
    기계적 패턴을 만들지 않기 위해서다.
    """

    def __init__(self, interval: float, jitter: float = 0.4) -> None:
        self.interval = max(float(interval), MIN_INTERVAL_SEC)
        self.jitter = max(0.0, float(jitter))
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> float:
        """다음 호출이 허용될 때까지 블록. 실제로 잔 시간을 반환."""
        with self._lock:
            now = _time.monotonic()
            slept = 0.0
            if now < self._next_at:
                slept = self._next_at - now
                _time.sleep(slept)
                now = _time.monotonic()
            self._next_at = now + self.interval + random.uniform(0, self.jitter)
            return slept

    def penalize(self, factor: float = 2.0, cap: float = 300.0) -> None:
        """오류 발생 시 다음 호출을 뒤로 미룬다 (백오프)."""
        with self._lock:
            delay = min(self.interval * factor, cap)
            self._next_at = max(self._next_at, _time.monotonic() + delay)


class Session:
    """쿠키를 유지하는 최소한의 HTTP 세션."""

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.cookies = CookieJar()
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.limiter = limiter
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            _NoRedirectOnPost(),
        )

    def clear_cookies(self) -> None:
        self.cookies.clear()

    def request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> "Response":
        if self.limiter is not None:
            self.limiter.wait()

        if params:
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = url + sep + urllib.parse.urlencode(_stringify(params), encoding="utf-8")

        body: bytes | None = None
        req_headers = {**self.headers, **(headers or {})}
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        elif data is not None:
            body = urllib.parse.urlencode(_stringify(data), encoding="utf-8").encode("utf-8")
            req_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"
            )

        req = urllib.request.Request(url, data=body, method=method.upper())
        for k, v in req_headers.items():
            req.add_header(k, v)

        log.debug("%s %s", method.upper(), url)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return Response(resp.status, dict(resp.headers), raw, resp.url)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            if self.limiter is not None:
                self.limiter.penalize()
            return Response(exc.code, dict(exc.headers), raw, url)
        except urllib.error.URLError as exc:
            if self.limiter is not None:
                self.limiter.penalize()
            raise TransportError(f"{method.upper()} {url} 실패: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            if self.limiter is not None:
                self.limiter.penalize()
            raise TransportError(f"{method.upper()} {url} 실패: {exc}") from exc

    def get(self, url: str, **kw: Any) -> "Response":
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> "Response":
        return self.request("POST", url, **kw)


class Response:
    def __init__(self, status: int, headers: dict[str, str], content: bytes, url: str) -> None:
        self.status = status
        self.headers = headers
        self.content = content
        self.url = url

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("Content-Type", "")
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            snippet = self.text[:200].replace("\n", " ")
            raise TransportError(
                f"JSON 응답을 기대했으나 파싱 실패 (status={self.status}): {snippet!r}"
            ) from exc

    def raise_for_status(self) -> "Response":
        if not self.ok:
            snippet = self.text[:200].replace("\n", " ")
            raise TransportError(f"HTTP {self.status} from {self.url}: {snippet!r}")
        return self


class _NoRedirectOnPost(urllib.request.HTTPRedirectHandler):
    """POST 리다이렉트를 GET으로 바꾸지 않고 그대로 따라간다.

    예매 API 일부가 302로 결과를 넘기는데, 기본 핸들러는 메서드를 바꿔버려
    세션이 끊긴 것처럼 보이는 응답을 준다.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        newreq = super().redirect_request(req, fp, code, msg, headers, newurl)
        if newreq is not None and req.get_method() == "POST" and code in (301, 302, 303):
            newreq.method = "GET"  # 표준 동작 유지, 명시적으로 남겨둔다
        return newreq


def _stringify(d: dict[str, Any]) -> dict[str, str]:
    return {k: ("" if v is None else str(v)) for k, v in d.items()}
