"""코레일(KTX/ITX 등) 모바일 API 어댑터.

SRT와 달리 코레일 조회 API는 역 코드가 아니라 역 '이름'을 받는다. 덕분에
역 코드 표가 틀려도 조회는 동작한다.

⚠ WIRE FORMAT 구역은 사업자가 예고 없이 바꾼다. 파싱이 깨지면
`python -m railcatch doctor --provider korail --dump` 로 원본을 확인하고
이 구역만 고치면 된다.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from ..errors import BlockedError, LoginError, ResponseError, SoldOutError
from ..models import Availability, Provider, Reservation, SeatClass, Station, Train
from ..transport import RateLimiter, Session, html_message, looks_like_html
from . import stations as station_db
from .base import RailProvider

log = logging.getLogger(__name__)

# ─────────────────────────── WIRE FORMAT ───────────────────────────
BASE = "https://smart.letskorail.com:443/classes/com.korail.mobile."
EP_LOGIN = BASE + "login.Login"
EP_LOGOUT = BASE + "common.logout"
EP_SEARCH = BASE + "seatMovie.ScheduleView"
EP_RESERVE = BASE + "certification.TicketReservation"
EP_RESERVATIONS = BASE + "reservation.ReservationView"
EP_STATION_DB = BASE + "common.stationinfo"

USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 13; SM-S911N Build/TP1A.220624.014)"
DEVICE = "AD"          # Android

#: 앱 버전 문자열. 서버가 너무 낮은 버전을 거부하면 다음 후보로 넘어간다.
#: .env 의 KORAIL_VERSION 으로 덮어쓸 수 있다.
VERSION_CANDIDATES: tuple[str, ...] = ("250401001", "240401001", "231231001", "190617001")

#: 버전이 낮다고 거부당했음을 뜻하는 문구.
_VERSION_HINTS = ("버전", "업데이트", "업그레이드", "최신")

ID_TYPE_MEMBERSHIP = "1"
ID_TYPE_EMAIL = "5"
ID_TYPE_PHONE = "4"

TRAIN_GROUP_ALL = "109"
TRAIN_GROUP_KTX = "100"

SEAT_ATTR_GENERAL = "015"
SEAT_ATTR_SPECIAL = "011"

#: 좌석 상태 코드 → 의미. 확실치 않은 코드는 UNKNOWN으로 두고,
#: h_rsv_psb_flg 로 보정한다.
SEAT_STATE: dict[str, Availability] = {
    "11": Availability.AVAILABLE,
    "12": Availability.AVAILABLE,
    "13": Availability.SOLD_OUT,
    "00": Availability.SOLD_OUT,
    "0": Availability.SOLD_OUT,
    "": Availability.SOLD_OUT,
}

SUCCESS = "SUCC"

#: 자동화 탐지·차단을 뜻하는 코드. 재시도하면 안 된다.
#: "MACRO ERROR"는 코레일이 매크로를 탐지했을 때 주는 코드로, 본문에는
#: "앱을 최신 버전으로 업데이트하라"는 무관한 안내가 실려 온다. 본문 문구만
#: 보고 버전 문제로 오해하면 차단된 계정에 로그인을 반복하게 된다.
_BLOCK_CODES = {"MACRO ERROR", "IRZ000063", "WRG000000"}
_SOLDOUT_HINTS = ("잔여석", "매진", "좌석", "선택하신 열차")
# ───────────────────────── END WIRE FORMAT ─────────────────────────


class KorailProvider(RailProvider):
    provider = Provider.KORAIL
    display_name = "코레일"

    def __init__(
        self,
        *,
        interval: float = 3.0,
        timeout: float = 12.0,
        data_dir: Any = None,
        ktx_only: bool = False,
        version: str | None = None,
    ) -> None:
        self.session = Session(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"},
            timeout=timeout,
            limiter=RateLimiter(interval),
        )
        self._logged_in = False
        self._key: str | None = None       # 예약 API가 요구하는 세션 키
        self._member_no: str | None = None
        self._stations: list[Station] | None = None
        self.data_dir = data_dir
        self.ktx_only = ktx_only
        self.version = version or VERSION_CANDIDATES[0]
        self._version_locked = bool(version)   # 사용자가 지정했으면 바꾸지 않는다
        self.last_raw: Any = None
        self.last_url: str | None = None

    # ── 인증 ────────────────────────────────────────────────
    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def login(self, user_id: str, password: str) -> None:
        payload = {
            "Device": DEVICE,
            "Version": self.version,
            "txtInputFlg": _id_type(user_id),
            "txtMemberNo": user_id,
            "txtPwd": password,
        }
        data = self._call(EP_LOGIN, payload, action="로그인")

        # strResult가 SUCC라도 세션 키나 회원 식별자가 없으면 로그인된 것이 아니다.
        # '실패 문구가 없으면 성공'으로 처리하면 이후 진단이 전부 엉뚱해진다.
        self._key = data.get("Key") or data.get("key")
        profile = data.get("strCustNo") or data.get("strMbCrdNo") or data.get("strCustNm")
        self._member_no = str(profile) if profile else None
        if not self._key and not self._member_no:
            raise LoginError(
                "코레일 로그인 응답에서 세션 정보를 찾지 못했습니다. "
                "아이디 형식(회원번호/이메일/휴대폰)과 비밀번호를 확인하세요. "
                "--dump 로 원본 응답을 볼 수 있습니다."
            )
        self._logged_in = True
        log.info("코레일 로그인 성공%s", f" ({self._member_no})" if self._member_no else "")

    def logout(self) -> None:
        if self._logged_in:
            self.session.get(EP_LOGOUT)
        self.session.clear_cookies()
        self._logged_in = False
        self._key = None

    # ── 역 ──────────────────────────────────────────────────
    def stations(self) -> list[Station]:
        if self._stations is not None:
            return self._stations
        if self.data_dir is not None:
            cached = station_db.load_cached(Provider.KORAIL, self.data_dir)
            if cached:
                self._stations = cached
                return cached
        self._stations = station_db.bundled(Provider.KORAIL)
        return self._stations

    def refresh_stations(self) -> list[Station]:
        """서버에서 역 목록을 새로 받아 캐시한다."""
        resp = self.session.get(EP_STATION_DB, params={"device": DEVICE}).raise_for_status()
        data = resp.json()
        self.last_raw = data
        rows = data.get("stns", {}).get("stn", []) if isinstance(data, dict) else []
        out = [
            Station(name=str(r["stn_nm"]).strip(), code=str(r["stn_cd"]).strip(),
                    provider=Provider.KORAIL)
            for r in rows
            if isinstance(r, dict) and r.get("stn_nm") and r.get("stn_cd")
        ]
        if not out:
            raise ResponseError("코레일 역 목록 응답을 해석하지 못했습니다.")
        self._stations = out
        if self.data_dir is not None:
            station_db.save_cache(Provider.KORAIL, self.data_dir, out)
        log.info("코레일 역 %d개 갱신", len(out))
        return out

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
        # 코레일 조회는 역 이름을 그대로 받는다. 코드 표에 의존하지 않는다.
        dep_name = station_db.canonical(dep)
        arr_name = station_db.canonical(arr)
        payload = {
            "Device": DEVICE,
            "Version": self.version,
            "radJobId": "1",                 # 1 = 편도
            "selGoTrain": TRAIN_GROUP_KTX if self.ktx_only else "05",
            "txtGoAbrdDt": day.strftime("%Y%m%d"),
            "txtGoHour": after.strftime("%H%M%S"),
            "txtGoStart": dep_name,
            "txtGoEnd": arr_name,
            "txtPsgFlg_1": passengers,       # 어른
            "txtPsgFlg_2": 0,                # 어린이
            "txtPsgFlg_3": 0,                # 경로
            "txtPsgFlg_4": 0,                # 중증장애인
            "txtPsgFlg_5": 0,                # 경증장애인
            "txtMenuId": "11",
            "txtSeatAttCd_2": "000",
            "txtSeatAttCd_3": "000",
            "txtSeatAttCd_4": SEAT_ATTR_GENERAL,
            "txtTrnGpCd": TRAIN_GROUP_KTX if self.ktx_only else TRAIN_GROUP_ALL,
            "txtCardPsgCnt": "0",
            "txtGdNo": "",
            "txtJobDv": "",
        }
        data = self._call(EP_SEARCH, payload, action="열차 조회", allow_empty=True)
        rows = _train_rows(data)
        trains = [t for t in (self._to_train(r, dep_name, arr_name) for r in rows) if t]
        if not include_soldout:
            trains = [t for t in trains if t.general.is_catchable or t.special.is_catchable]
        return trains

    def _to_train(self, row: dict[str, Any], dep_fallback: str, arr_fallback: str) -> Train | None:
        try:
            dep_at = _dt(row["h_dpt_dt"], row["h_dpt_tm"])
            arr_at = _dt(row["h_arv_dt"], row["h_arv_tm"])
        except (KeyError, ValueError) as exc:
            log.warning("코레일 열차 행 파싱 실패: %s (%s)", exc, list(row)[:8])
            return None

        reservable = str(row.get("h_rsv_psb_flg", "")).upper() == "Y"
        general = _seat_state(row.get("h_gen_rsv_cd"), reservable)
        special = _seat_state(row.get("h_spe_rsv_cd"), reservable)
        # 좌석별 코드를 서버가 안 줬는데 예약은 가능하다고 하면, 일반실로 본다.
        if general is Availability.UNKNOWN and special is Availability.UNKNOWN:
            general = Availability.AVAILABLE if reservable else Availability.SOLD_OUT
            special = Availability.UNKNOWN

        return Train(
            provider=Provider.KORAIL,
            train_name=_train_name(row.get("h_trn_clsf_nm"), row.get("h_trn_clsf_cd")),
            train_number=str(row.get("h_trn_no", "")).lstrip("0") or "0",
            dep_station=row.get("h_dpt_rs_stn_nm") or dep_fallback,
            arr_station=row.get("h_arv_rs_stn_nm") or arr_fallback,
            dep_at=dep_at,
            arr_at=arr_at,
            general=general,
            special=special,
            raw=row,
        )

    # ── 선점 ────────────────────────────────────────────────
    def reserve(self, train: Train, seat_class: SeatClass, *, passengers: int = 1) -> Reservation:
        if seat_class is SeatClass.ANY:
            raise ValueError("reserve()에는 구체적인 좌석 등급을 넘겨야 합니다.")
        row = train.raw
        seat_attr = SEAT_ATTR_SPECIAL if seat_class is SeatClass.SPECIAL else SEAT_ATTR_GENERAL
        payload = {
            "Device": DEVICE,
            "Version": self.version,
            "Key": self._key or "",
            "txtGdNo": "",
            "txtJobId": "1101",              # 개인 예약
            "txtTotPsgCnt": passengers,
            "txtSeatAttCd1": "000",
            "txtSeatAttCd2": "000",
            "txtSeatAttCd3": "000",
            "txtSeatAttCd4": seat_attr,
            "txtSeatAttCd5": "000",
            "hidFreeFlg": "N",
            "txtStndFlg": "N",
            "txtMenuId": "11",
            "txtSrcarCnt": "0",
            "txtJrnyCnt": "1",
            # 여정 1
            "txtJrnySqno1": "001",
            "txtJrnyTpCd1": "11",
            "txtDptDt1": row.get("h_dpt_dt", train.dep_at.strftime("%Y%m%d")),
            "txtDptRsStnCd1": row.get("h_dpt_rs_stn_cd", ""),
            "txtDptTm1": row.get("h_dpt_tm", train.dep_at.strftime("%H%M%S")),
            "txtArvRsStnCd1": row.get("h_arv_rs_stn_cd", ""),
            "txtTrnNo1": f"{int(train.train_number):05d}",
            "txtRunDt1": row.get("h_run_dt", row.get("h_dpt_dt", "")),
            "txtTrnClsfCd1": row.get("h_trn_clsf_cd", ""),
            "txtTrnGpCd1": row.get("h_trn_gp_cd", ""),
            "txtChgFlg1": "",
            # 승객 그룹 1
            "txtPsgTpCd1": "1",              # 어른
            "txtDiscKndCd1": "000",
            "txtCompaCnt1": passengers,
            "txtCardCode_1": "",
            "txtCardNo_1": "",
            "txtCardPw_1": "",
        }
        try:
            data = self._call(EP_RESERVE, payload, action="좌석 선점")
        except ResponseError as exc:
            if any(hint in str(exc) for hint in _SOLDOUT_HINTS):
                raise SoldOutError(f"코레일: {exc}") from exc
            raise

        resv_no = str(data.get("h_pnr_no") or data.get("pnrNo") or "").strip()
        if not resv_no:
            raise ResponseError(f"코레일 예약번호를 찾지 못했습니다: {str(data)[:200]!r}")

        return Reservation(
            provider=Provider.KORAIL,
            reservation_number=resv_no,
            train=train,
            seat_class=seat_class,
            total_price=_int_or_none(data.get("h_rcvd_amt") or data.get("h_tot_rcvd_amt")),
            pay_deadline=_deadline(data.get("h_ntisu_lmt_dt"), data.get("h_ntisu_lmt_tm")),
            raw=data,
        )

    # ── 공통 호출 ───────────────────────────────────────────
    def _call(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        action: str,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        """요청을 보내고 코레일 공통 응답 규약에 따라 해석한다.

        서버가 앱 버전을 거부하면 다음 버전 후보로 한 번 더 시도한다. 사용자가
        .env 로 버전을 지정한 경우에는 임의로 바꾸지 않는다.
        """
        tried: list[str] = []
        while True:
            data = self._raw_call(url, payload, action=action)
            try:
                return self._handle_result(
                    data, action=action, is_login=(url == EP_LOGIN), allow_empty=allow_empty
                )
            except BlockedError:
                # 차단은 재시도 대상이 아니다. 조건을 바꿔 다시 두드리는 것이
                # 정확히 하지 말아야 할 행동이다.
                raise
            except (LoginError, ResponseError) as exc:
                if not self._should_retry_with_new_version(str(exc), tried):
                    raise
                payload = {**payload, "Version": self.version}
                log.info("코레일 앱 버전을 %s 로 바꿔 재시도합니다.", self.version)

    def _raw_call(self, url: str, payload: dict[str, Any], *, action: str) -> Any:
        """HTTP 호출과 JSON 파싱까지만. 오류 페이지를 JSON 파싱 실패로 뭉개지 않는다."""
        resp = self.session.post(url, data=payload).raise_for_status()
        self.last_url = url
        body = resp.text
        self.last_raw = body

        if looks_like_html(body):
            hint = html_message(body)
            raise ResponseError(
                f"코레일 {action}: JSON 대신 오류 페이지가 왔습니다"
                + (f" — {hint}" if hint else "")
                + f" (경로 {url.rsplit('.', 1)[-1]}). "
                "코레일이 API를 변경했을 수 있습니다 — providers/korail.py 의 "
                "WIRE FORMAT 구역을 확인하세요."
            )
        data = resp.json()
        self.last_raw = data
        return data

    def _should_retry_with_new_version(self, message: str, tried: list[str]) -> bool:
        """버전 거부로 보이면 다음 버전 후보를 고른다."""
        if self._version_locked:
            return False
        if not any(hint in message for hint in _VERSION_HINTS):
            return False
        tried.append(self.version)
        for candidate in VERSION_CANDIDATES:
            if candidate not in tried:
                self.version = candidate
                return True
        return False

    def _handle_result(
        self,
        data: Any,
        *,
        action: str,
        is_login: bool,
        allow_empty: bool,
    ) -> dict[str, Any]:
        """코레일 공통 응답 규약(strResult/h_msg_cd/h_msg_txt)을 해석한다.

        네트워크와 분리해 둔 이유는, 사업자가 오류 코드를 바꿨을 때 실제 호출
        없이 이 함수만 테스트로 고정할 수 있게 하기 위해서다.
        """
        if not isinstance(data, dict):
            raise ResponseError(f"코레일 {action}: 예상치 못한 응답 형태 {type(data).__name__}")

        if str(data.get("strResult", SUCCESS)).upper() == SUCCESS:
            return data

        code = str(data.get("h_msg_cd", "")).strip()
        msg = str(data.get("h_msg_txt", "")).strip() or "알 수 없는 오류"
        if code in _BLOCK_CODES:
            raise BlockedError(
                f"코레일이 자동화 프로그램으로 판단해 요청을 차단했습니다 "
                f"[{code}]: {msg}"
            )
        if is_login:
            raise LoginError(f"코레일 로그인 실패: {msg}")
        if "로그인" in msg or "세션" in msg:
            self._logged_in = False
            raise ResponseError(f"코레일 세션 만료: {msg}", code=code)
        if allow_empty and ("없" in msg or "조회" in msg):
            # "해당 조건의 열차가 없습니다" 는 오류가 아니라 빈 결과다.
            log.debug("코레일 %s: %s", action, msg)
            return {}
        raise ResponseError(f"코레일 {action} 실패: {msg}", code=code)


# ── 헬퍼 ────────────────────────────────────────────────────
def _id_type(user_id: str) -> str:
    import re as _re

    if "@" in user_id:
        return ID_TYPE_EMAIL
    digits = _re.sub(r"\D", "", user_id)
    if len(digits) in (10, 11) and digits.startswith("01"):
        return ID_TYPE_PHONE
    return ID_TYPE_MEMBERSHIP


def _train_rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    infos = data.get("trn_infos")
    if isinstance(infos, dict):
        rows = infos.get("trn_info")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        if isinstance(rows, dict):
            return [rows]
    return []


def _seat_state(code: Any, reservable: bool) -> Availability:
    if code is None:
        return Availability.UNKNOWN
    state = SEAT_STATE.get(str(code).strip())
    if state is None:
        return Availability.UNKNOWN
    if state is Availability.AVAILABLE and not reservable:
        # 좌석 코드와 예약가능 플래그가 어긋나면 보수적으로 판단한다.
        return Availability.SOLD_OUT
    return state


def _train_name(name: Any, code: Any) -> str:
    if name:
        return str(name).strip()
    return {"00": "KTX", "07": "KTX-산천", "08": "KTX-이음"}.get(str(code).strip(), "열차")


def _dt(day: str, tm: str) -> datetime:
    day, tm = str(day).strip(), str(tm).strip().ljust(6, "0")
    hour = int(tm[0:2])
    base = datetime.strptime(day, "%Y%m%d")
    return base.replace(minute=int(tm[2:4]), second=int(tm[4:6])) + timedelta(hours=hour)


def _int_or_none(v: Any) -> int | None:
    import re as _re

    try:
        return int(_re.sub(r"\D", "", str(v)))
    except (TypeError, ValueError):
        return None


def _deadline(day: Any, tm: Any) -> datetime | None:
    if not day or not tm:
        return None
    try:
        return _dt(str(day), str(tm))
    except ValueError:
        return None
