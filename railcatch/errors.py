"""railcatch 전역 예외."""


class RailCatchError(Exception):
    """모든 railcatch 예외의 최상위."""


class ConfigError(RailCatchError):
    """설정값이 없거나 잘못됨."""


class TransportError(RailCatchError):
    """네트워크 계층 실패 (연결 불가, 타임아웃, 5xx)."""


class LoginError(RailCatchError):
    """로그인 실패. 자격증명 문제이므로 재시도해도 소용없다."""


class ResponseError(RailCatchError):
    """서버가 응답은 했지만 실패를 알림.

    Attributes:
        code: 사업자가 준 오류 코드 (있으면).
        retryable: 잠시 후 재시도할 가치가 있는지.
    """

    def __init__(self, message: str, code: str | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SoldOutError(ResponseError):
    """예약 시도했으나 그 사이 좌석이 나감. 흔한 정상 상황."""

    def __init__(self, message: str = "이미 매진되었습니다") -> None:
        super().__init__(message, code="SOLD_OUT", retryable=True)


class BlockedError(ResponseError):
    """사업자가 과도한 요청 등으로 차단. 즉시 감시를 멈춰야 한다."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="BLOCKED", retryable=False)
