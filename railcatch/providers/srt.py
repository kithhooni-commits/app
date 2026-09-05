"""SRT (수서고속철도) 모바일 API 어댑터.

⚠ 이 파일의 WIRE FORMAT 구역은 사업자가 예고 없이 바꾼다. 응답 파싱이
깨지면 `python -m railcatch doctor --provider srt --dump` 로 원본 응답을
받아 이 구역만 고치면 된다. 나머지 코드는 손댈 필요가 없다.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from ..errors import BlockedError, LoginError, ResponseError, SoldOutError
from ..models import Availability, Provider, Reservation, SeatClass, Station, Train
from ..transport import RateLimiter, Session
from . import stations as station_db
from .base import RailProvider

log = logging.getLogger(__name__)

# ─────────────────────────── WIRE FORMAT ───────────────────────────
BASE = "https://app.srail.or.kr:443"
EP_MAIN = f"{BASE}/main/main.do"
EP_LOGIN = f"{BASE}/apb/selectListApb01080.do"
EP_LOGOUT = f"{BASE}/login/loginOut.do"
EP_SEARCH = f"{BASE}/ara/selectListAra10007.do"
EP_RESERVE = f"{BASE}/arc/selectListArc05013.do"
EP_RESERVATIONS = f"{BASE}/atc/selectListAtc14016.do"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)

# 로그인 식별자 종류
ID_TYPE_MEMBERSHIP = "1"   # 회원번호
ID_TYPE_EMAIL = "2"        # 이메일
ID_TYPE_PHONE = "3"        # 휴대폰번호

# 좌석 속성 코드
SEAT_ATTR_GENERAL = "015"
SEAT_ATTR_SPECIAL = "011"

# 서버가 좌석 상태를 사람이 읽는 문자열로 준다.
_AVAILABLE_TOKENS = ("예약가능", "예약하기", "座席", "가능")
_WAITLIST_TOKENS = ("예약대기",)
_SOLDOUT_TOKENS = ("매진", "-", "＊", "*", "")

# 차단/점검을 뜻하는 응답 문구
_BLOCK_TOKENS = ("일시적으로", "비정상적인", "과도한", "차단", "점검")
# ───────────────────────── END WIRE FORMAT ─────────────────────────


class SRTProvider(RailProvider):
    provider = Provider.SRT
    display_name = "SRT"

    def __init__(self, *, interval: float = 3.0, timeout: float = 12.0) -> None:
        self.session = Session(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ko-KR,ko;q=0.9",
                "Referer": EP_MAIN,
            },
            timeout=timeout,
            limiter=RateLimiter(interval),
        )
        self._logged_in = False
        self._member_no: str | None = None
        self.last_raw: Any = None  # doctor 명령이 들여다본다

    # ── 인증 ────────────────────────────────────────────────
    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def login(self, user_id: str, password: str) -> None:
        payload = {
            "auto": "Y",
            "check": "Y",
            "page": "menu",
            "deviceKey": "-",
            "customerYn": "",
            "login_referer": EP_MAIN,
            "srchDvCd": _id_type(user_id),
            "srchDvNm": user_id,
            "hmpgPwdCphd": password,
        }
        resp = self.session.post(EP_LOGIN, data=payload)
        body = resp.text
        self.last_raw = body

        if not resp.ok:
            raise LoginError(f"SRT 로그인 요청 실패 (HTTP {resp.status})")
        if "존재하지 않는 회원" in body or "비밀번호" in body and "확인" in body:
            raise LoginError("SRT 로그인 실패: 아이디 또는 비밀번호를 확인하세요.")
        _guard_blocked(body)

        m = re.search(r'"MB_CRD_NO"\s*:\s*"(\d+)"', body)
        self._member_no = m.group(1) if m else None
        self._logged_in = True
        log.info("SRT 로그인 성공%s", f" (회원번호 {self._member_no})" if self._member_no else "")

    def logout(self) -> None:
        if self._logged_in:
            self.session.post(EP_LOGOUT)
        self.session.clear_cookies()
        self._logged_in = False

    # ── 역 ──────────────────────────────────────────────────
    def stations(self) -> list[Station]:
        return station_db.bundled(Provider.SRT)

    # ── 조회 ────────────────────────────────────────────────
    def search(
        self,
        dep: str,
        arr: str,
        day: date,
        after: time,
        *,
        passengers: int = 1,
        include_soldout: bool = True,
    ) -> list[Train]:
        dep_st = self.resolve_station(station_db.canonical(dep))
        arr_st = self.resolve_station(station_db.canonical(arr))
        payload = {
            "chtnDvCd": "1",
            "arriveTime": "N",
            "seatAttCd": SEAT_ATTR_GENERAL,
            "psgNum": passengers,
            "trnGpCd": "109",           # SRT만
            "stlbTrnClsfCd": "05",
            "dptDt": day.strftime("%Y%m%d"),
            "dptTm": after.strftime("%H%M%S"),
            "dptRsStnCd": dep_st.code,
            "arvRsStnCd": arr_st.code,
        }
        resp = self.session.post(EP_SEARCH, data=payload).raise_for_status()
        _guard_blocked(resp.text)
        data = resp.json()
        self.last_raw = data

        rows = _rows(data)
        trains = [t for t in (self._to_train(r, dep_st.name, arr_st.name) for r in rows) if t]
        if not include_soldout:
            trains = [
                t for t in trains
                if t.general.is_catchable or t.special.is_catchable
            ]
        return trains

    def _to_train(self, row: dict[str, Any], dep_fallback: str, arr_fallback: str) -> Train | None:
        try:
            dep_at = _dt(row["dptDt"], row["dptTm"])
            arr_at = _dt(row["arvDt"], row["arvTm"])
        except (KeyError, ValueError) as exc:
            log.warning("SRT 열차 행 파싱 실패: %s (%s)", exc, list(row)[:8])
            return None
        return Train(
            provider=Provider.SRT,
            train_name="SRT",
            train_number=str(row.get("trnNo", "")).lstrip("0") or "0",
            dep_station=row.get("dptRsStnCdNm") or dep_fallback,
            arr_station=row.get("arvRsStnCdNm") or arr_fallback,
            dep_at=dep_at,
            arr_at=arr_at,
            general=_availability(row.get("gnrmRsvPsbStr")),
            special=_availability(row.get("sprmRsvPsbStr")),
            raw=row,
        )

    # ── 선점 ────────────────────────────────────────────────
    def reserve(self, train: Train, seat_class: SeatClass, *, passengers: int = 1) -> Reservation:
        if seat_class is SeatClass.ANY:
            raise ValueError("reserve()에는 구체적인 좌석 등급을 넘겨야 합니다.")
        row = train.raw
        seat_attr = SEAT_ATTR_SPECIAL if seat_class is SeatClass.SPECIAL else SEAT_ATTR_GENERAL
        payload = {
            "reserveType": "11",
            "jobId": "1101",           # 개인 예약
            "jrnyCnt": "1",
            "jrnyTpCd": "11",
            "jrnySqno1": "001",
            "stndFlg": "N",
            "trnGpCd1": row.get("trnGpCd", "109"),
            "stlbTrnClsfCd1": row.get("stlbTrnClsfCd", "05"),
            "dptDt1": row.get("dptDt", train.dep_at.strftime("%Y%m%d")),
            "dptTm1": row.get("dptTm", train.dep_at.strftime("%H%M%S")),
            "runDt1": row.get("runDt", row.get("dptDt", train.dep_at.strftime("%Y%m%d"))),
            "trnNo1": f"{int(train.train_number):05d}",
            "dptRsStnCd1": row.get("dptRsStnCd", ""),
            "dptRsStnCdNm1": train.dep_station,
            "arvRsStnCd1": row.get("arvRsStnCd", ""),
            "arvRsStnCdNm1": train.arr_station,
            "totPrnb": passengers,
            "psgGpNo1": "1",
            "psgTpCd1": "1",           # 어른
            "psgInfoPerPrnb1": passengers,
            "rqSeatAttCd1": seat_attr,
            "smkSeatAttCd1": "000",
            "dirSeatAttCd1": "009",
            "locSeatAttCd1": "000",
            "etcSeatAttCd1": "000",
        }
        resp = self.session.post(EP_RESERVE, data=payload).raise_for_status()
        body = resp.text
        self.last_raw = body
        _guard_blocked(body)

        if any(tok in body for tok in ("잔여석", "매진", "좌석이 없", "지정할 수 없")):
            raise SoldOutError("SRT: 선점 직전 좌석이 소진되었습니다.")
        if "로그인" in body and "세션" in body:
            self._logged_in = False
            raise ResponseError("SRT 세션이 만료되었습니다. 재로그인이 필요합니다.")

        data = resp.json()
        rows = _rows(data)
        if not rows:
            raise ResponseError(f"SRT 예약 응답을 해석하지 못했습니다: {body[:200]!r}")
        head = rows[0]
        resv_no = str(head.get("pnrNo") or head.get("rsvNo") or "").strip()
        if not resv_no:
            raise ResponseError(f"SRT 예약번호를 찾지 못했습니다: {body[:200]!r}")

        return Reservation(
            provider=Provider.SRT,
            reservation_number=resv_no,
            train=train,
            seat_class=seat_class,
            total_price=_int_or_none(head.get("rcvdAmt") or head.get("totRcvdAmt")),
            pay_deadline=_deadline(head.get("ntisuLmtDt"), head.get("ntisuLmtTm")),
            raw=head,
        )


# ── 헬퍼 ────────────────────────────────────────────────────
def _id_type(user_id: str) -> str:
    digits = re.sub(r"\D", "", user_id)
    if "@" in user_id:
        return ID_TYPE_EMAIL
    if len(digits) == len(user_id.strip()) and len(digits) in (10, 11) and digits.startswith("01"):
        return ID_TYPE_PHONE
    return ID_TYPE_MEMBERSHIP


def _rows(data: Any) -> list[dict[str, Any]]:
    """SRT 응답에서 데이터 행 리스트를 꺼낸다.

    응답이 {"outDataSets": {"dsOutput1": [...]}} 또는 {"dsOutput1": [...]}
    두 형태로 모두 관측되어 둘 다 받는다.
    """
    if not isinstance(data, dict):
        return []
    container = data.get("outDataSets")
    if isinstance(container, dict):
        for value in container.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    for key, value in data.items():
        if key.startswith("dsOutput") and isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def _availability(text: str | None) -> Availability:
    if text is None:
        return Availability.UNKNOWN
    s = str(text).strip()
    if any(tok and tok in s for tok in _WAITLIST_TOKENS):
        return Availability.WAITLIST
    if any(tok and tok in s for tok in _AVAILABLE_TOKENS):
        return Availability.AVAILABLE
    if s in _SOLDOUT_TOKENS or "매진" in s:
        return Availability.SOLD_OUT
    return Availability.UNKNOWN


def _dt(day: str, tm: str) -> datetime:
    """'20260920' + '143000' → datetime. 24시 이후 표기('250000')도 처리."""
    day, tm = str(day).strip(), str(tm).strip().ljust(6, "0")
    hour = int(tm[0:2])
    base = datetime.strptime(day, "%Y%m%d")
    return base.replace(minute=int(tm[2:4]), second=int(tm[4:6])) + timedelta(hours=hour)


def _int_or_none(v: Any) -> int | None:
    try:
        return int(re.sub(r"\D", "", str(v)))
    except (TypeError, ValueError):
        return None


def _deadline(day: Any, tm: Any) -> datetime | None:
    if not day or not tm:
        return None
    try:
        return _dt(str(day), str(tm))
    except ValueError:
        return None


def _guard_blocked(body: str) -> None:
    if any(tok in body for tok in _BLOCK_TOKENS) and len(body) < 4000:
        raise BlockedError(f"SRT가 요청을 거부했습니다. 감시를 중단합니다: {body[:160]!r}")
