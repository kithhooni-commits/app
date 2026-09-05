"""사업자 응답 파싱 테스트.

여기 쓰인 응답 샘플은 실제 서버 응답의 '형태'를 본뜬 것이다. 사업자가 필드를
바꾸면 이 테스트가 아니라 실제 호출이 먼저 깨진다 — `doctor --dump` 로 원본을
받아 이 샘플을 갱신하고 WIRE FORMAT 구역을 고치는 것이 정해진 절차다.
"""

import json
import unittest
from datetime import date, time

from railcatch.errors import BlockedError, LoginError, ResponseError
from railcatch.models import Availability, Provider, SeatClass
from railcatch.providers.korail import KorailProvider, _seat_state, _train_rows
from railcatch.providers.srt import SRTProvider, _availability, _dt, _guard_blocked, _rows

SRT_ROW = {
    "trnNo": "00301",
    "dptDt": "20260920", "dptTm": "080000",
    "arvDt": "20260920", "arvTm": "104000",
    "dptRsStnCdNm": "수서", "arvRsStnCdNm": "부산",
    "gnrmRsvPsbStr": "예약가능", "sprmRsvPsbStr": "매진",
    "trnGpCd": "109", "stlbTrnClsfCd": "05",
}

KORAIL_ROW = {
    "h_trn_no": "00101", "h_trn_clsf_nm": "KTX", "h_trn_clsf_cd": "00",
    "h_dpt_dt": "20260920", "h_dpt_tm": "090000",
    "h_arv_dt": "20260920", "h_arv_tm": "115000",
    "h_dpt_rs_stn_nm": "서울", "h_arv_rs_stn_nm": "부산",
    "h_rsv_psb_flg": "Y", "h_gen_rsv_cd": "11", "h_spe_rsv_cd": "13",
}


class TestSrtParsing(unittest.TestCase):
    def setUp(self):
        self.provider = SRTProvider()

    def test_parses_row_into_train(self):
        train = self.provider._to_train(SRT_ROW, "수서", "부산")
        self.assertEqual(train.provider, Provider.SRT)
        self.assertEqual(train.train_number, "301", "선행 0은 떼야 한다")
        self.assertEqual(train.dep_at.strftime("%Y-%m-%d %H:%M"), "2026-09-20 08:00")
        self.assertEqual(train.duration_min, 160)
        self.assertIs(train.general, Availability.AVAILABLE)
        self.assertIs(train.special, Availability.SOLD_OUT)

    def test_malformed_row_is_dropped_not_raised(self):
        self.assertIsNone(self.provider._to_train({"trnNo": "301"}, "수서", "부산"))

    def test_row_extraction_handles_both_envelopes(self):
        self.assertEqual(_rows({"outDataSets": {"dsOutput1": [SRT_ROW]}}), [SRT_ROW])
        self.assertEqual(_rows({"dsOutput1": [SRT_ROW]}), [SRT_ROW])
        self.assertEqual(_rows({"nothing": 1}), [])
        self.assertEqual(_rows("not a dict"), [])

    def test_times_past_midnight(self):
        self.assertEqual(_dt("20260920", "250500").strftime("%d %H:%M"), "21 01:05")

    def test_availability_tokens(self):
        self.assertIs(_availability("예약가능"), Availability.AVAILABLE)
        self.assertIs(_availability("예약대기"), Availability.WAITLIST)
        self.assertIs(_availability("매진"), Availability.SOLD_OUT)
        self.assertIs(_availability("-"), Availability.SOLD_OUT)
        self.assertIs(_availability(None), Availability.UNKNOWN)

    def test_block_message_raises(self):
        with self.assertRaises(BlockedError):
            _guard_blocked("비정상적인 접근이 감지되었습니다")
        _guard_blocked('{"outDataSets": {}}')  # 정상 응답은 통과


class TestKorailParsing(unittest.TestCase):
    def setUp(self):
        self.provider = KorailProvider()

    def test_parses_row_into_train(self):
        train = self.provider._to_train(KORAIL_ROW, "서울", "부산")
        self.assertEqual(train.train_name, "KTX")
        self.assertEqual(train.train_number, "101")
        self.assertIs(train.general, Availability.AVAILABLE)
        self.assertIs(train.special, Availability.SOLD_OUT)

    def test_seat_code_yields_to_reservation_flag(self):
        """좌석 코드가 '가능'인데 예약불가 플래그면 매진으로 본다."""
        row = {**KORAIL_ROW, "h_rsv_psb_flg": "N"}
        train = self.provider._to_train(row, "서울", "부산")
        self.assertIs(train.general, Availability.SOLD_OUT)
        self.assertEqual(train.catchable_classes(SeatClass.ANY), [])

    def test_missing_seat_codes_fall_back_to_flag(self):
        row = {k: v for k, v in KORAIL_ROW.items()
               if k not in ("h_gen_rsv_cd", "h_spe_rsv_cd")}
        train = self.provider._to_train(row, "서울", "부산")
        self.assertIs(train.general, Availability.AVAILABLE)

        train_shut = self.provider._to_train({**row, "h_rsv_psb_flg": "N"}, "서울", "부산")
        self.assertIs(train_shut.general, Availability.SOLD_OUT)

    def test_unknown_seat_code_is_unknown(self):
        self.assertIs(_seat_state("99", True), Availability.UNKNOWN)

    def test_row_extraction(self):
        self.assertEqual(_train_rows({"trn_infos": {"trn_info": [KORAIL_ROW]}}), [KORAIL_ROW])
        self.assertEqual(_train_rows({"trn_infos": {"trn_info": KORAIL_ROW}}), [KORAIL_ROW])
        self.assertEqual(_train_rows({}), [])

    def test_failed_response_raises_with_server_message(self):
        with self.assertRaises(ResponseError) as ctx:
            self.provider._handle_result(
                {"strResult": "FAIL", "h_msg_cd": "IRZ000001", "h_msg_txt": "조회 오류"},
                action="열차 조회",
                is_login=False,
                allow_empty=False,
            )
        self.assertIn("조회 오류", str(ctx.exception))

    def test_blocked_code_raises_blocked(self):
        with self.assertRaises(BlockedError):
            self.provider._handle_result(
                {"strResult": "FAIL", "h_msg_cd": "IRZ000063", "h_msg_txt": "과도한 요청"},
                action="열차 조회",
                is_login=False,
                allow_empty=False,
            )

    def test_no_trains_message_is_empty_not_error(self):
        out = self.provider._handle_result(
            {"strResult": "FAIL", "h_msg_cd": "P100", "h_msg_txt": "해당 열차가 없습니다."},
            action="열차 조회",
            is_login=False,
            allow_empty=True,
        )
        self.assertEqual(out, {})


class TestStationResolution(unittest.TestCase):
    def test_resolves_with_suffix_and_alias(self):
        provider = SRTProvider()
        self.assertEqual(provider.resolve_station("수서").code, "0551")
        self.assertEqual(provider.resolve_station("수서역").code, "0551")

    def test_unknown_station_raises(self):
        from railcatch.errors import ConfigError

        with self.assertRaises(ConfigError):
            SRTProvider().resolve_station("강남")


if __name__ == "__main__":
    unittest.main()


class FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self.status = status
        self.content = body.encode("utf-8")
        self.headers = {"Content-Type": "application/json"}
        self.url = "http://fake"

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        return self.content.decode("utf-8")

    def json(self):
        import json as _json

        return _json.loads(self.text)

    def raise_for_status(self):
        from railcatch.errors import TransportError

        if not self.ok:
            raise TransportError(f"HTTP {self.status}")
        return self


class FakeSession:
    """URL별로 정해진 응답을 돌려주는 세션. 호출된 URL을 기록한다."""

    def __init__(self, routes: dict, default: str | None = None):
        self.routes = routes
        self.default = default
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, **kw):
        self.calls.append((url, dict(data or {})))
        for fragment, body in self.routes.items():
            if fragment in url:
                return FakeResponse(body() if callable(body) else body)
        if self.default is None:
            raise AssertionError(f"예상치 못한 URL: {url}")
        return FakeResponse(self.default)

    def get(self, url, **kw):
        return self.post(url, **kw)

    def clear_cookies(self):
        pass


SRT_ERROR_PAGE = (
    '<!DOCTYPE html>\r\n<html lang="ko">\r\n<head><title>SR</title></head>\r\n'
    '<body><div class="errorBox3"><p class="errorbox_tit">이용에 불편을 드려 죄송합니다.</p>'
    '<p class="errorbox_sub">잠시 후 다시 이용해 주시기 바랍니다.</p></div></body></html>'
)


class TestSrtEndpointProbing(unittest.TestCase):
    """SRT가 경로별로 HTML 오류 페이지를 주는 상황을 다룬다.

    실제로 겪은 증상: 로그인은 통과한 것처럼 보이는데 조회에서만
    '잠시 후 다시 이용해 주시기 바랍니다' HTML이 돌아왔다.
    """

    def _provider(self, routes, default=None):
        provider = SRTProvider()
        provider.session = FakeSession(routes, default=default)
        return provider

    def test_falls_back_to_second_candidate_when_first_is_error_page(self):
        provider = self._provider({
            "selectListAra10007_n.do": SRT_ERROR_PAGE,
            "selectListAra10007.do": json.dumps({"dsOutput1": [SRT_ROW]}),
        })
        trains = provider.search("수서", "부산", date(2026, 9, 20), time(0, 0))
        self.assertEqual(len(trains), 1)
        self.assertIn("selectListAra10007.do", provider.resolved["search"])

    def test_remembers_working_endpoint(self):
        provider = self._provider({
            "selectListAra10007_n.do": SRT_ERROR_PAGE,
            "selectListAra10007.do": json.dumps({"dsOutput1": [SRT_ROW]}),
        })
        provider.search("수서", "부산", date(2026, 9, 20), time(0, 0))
        first_round = len(provider.session.calls)
        provider.search("수서", "부산", date(2026, 9, 20), time(0, 0))
        self.assertEqual(
            len(provider.session.calls) - first_round, 1,
            "확정된 경로가 있으면 실패한 후보를 다시 두드리면 안 된다",
        )

    def test_all_candidates_failing_reports_page_message(self):
        provider = self._provider({"selectListAra10007": SRT_ERROR_PAGE})
        with self.assertRaises(ResponseError) as ctx:
            provider.search("수서", "부산", date(2026, 9, 20), time(0, 0))
        self.assertIn("잠시 후 다시 이용해", str(ctx.exception))

    def test_error_page_is_not_accepted_as_login(self):
        """이전 버전은 실패 문구가 없다는 이유로 오류 페이지를 성공 처리했다."""
        provider = self._provider({"selectListApb01080": SRT_ERROR_PAGE})
        with self.assertRaises(Exception) as ctx:
            provider.login("01012345678", "pw")
        self.assertFalse(provider.logged_in)
        self.assertNotIsInstance(ctx.exception, AssertionError)

    def test_login_requires_success_evidence(self):
        provider = self._provider({"selectListApb01080": json.dumps({"strResult": "???"})})
        with self.assertRaises(LoginError):
            provider.login("01012345678", "pw")

    def test_login_accepts_member_number(self):
        provider = self._provider({
            "selectListApb01080": json.dumps({"userMap": {"MB_CRD_NO": "1234567890"}}),
        })
        provider.login("01012345678", "pw")
        self.assertTrue(provider.logged_in)
        self.assertEqual(provider._member_no, "1234567890")

    def test_login_detects_wrong_password(self):
        provider = self._provider({
            "selectListApb01080": json.dumps({"MSG": "존재하지 않는 회원입니다."}),
        })
        with self.assertRaises(LoginError):
            provider.login("01012345678", "pw")


class TestKorailErrorHandling(unittest.TestCase):
    def _provider(self, routes, **kw):
        provider = KorailProvider(**kw)
        provider.session = FakeSession(routes)
        return provider

    def test_html_page_reports_readable_reason(self):
        provider = self._provider({"login.Login": SRT_ERROR_PAGE})
        with self.assertRaises(ResponseError) as ctx:
            provider.login("01012345678", "pw")
        message = str(ctx.exception)
        self.assertIn("오류 페이지", message)
        self.assertIn("잠시 후 다시 이용해", message)

    def test_login_without_session_key_fails(self):
        provider = self._provider({"login.Login": json.dumps({"strResult": "SUCC"})})
        with self.assertRaises(LoginError):
            provider.login("01012345678", "pw")
        self.assertFalse(provider.logged_in)

    def test_login_succeeds_with_key(self):
        provider = self._provider({
            "login.Login": json.dumps({"strResult": "SUCC", "Key": "abc", "strCustNo": "77"}),
        })
        provider.login("01012345678", "pw")
        self.assertTrue(provider.logged_in)
        self.assertEqual(provider._key, "abc")

    def test_version_rejection_retries_with_next_candidate(self):
        from railcatch.providers.korail import VERSION_CANDIDATES

        state = {"n": 0}

        def body():
            state["n"] += 1
            if state["n"] == 1:
                return json.dumps({
                    "strResult": "FAIL", "h_msg_cd": "IRZ000001",
                    "h_msg_txt": "최신 버전으로 업데이트 후 이용해 주세요.",
                })
            return json.dumps({"strResult": "SUCC", "Key": "k", "strCustNo": "1"})

        provider = self._provider({"login.Login": body})
        provider.login("01012345678", "pw")

        self.assertTrue(provider.logged_in)
        self.assertEqual(provider.version, VERSION_CANDIDATES[1])
        self.assertEqual(provider.session.calls[0][1]["Version"], VERSION_CANDIDATES[0])
        self.assertEqual(provider.session.calls[1][1]["Version"], VERSION_CANDIDATES[1])

    def test_pinned_version_is_not_changed(self):
        provider = self._provider({
            "login.Login": json.dumps({
                "strResult": "FAIL", "h_msg_cd": "X",
                "h_msg_txt": "버전이 낮습니다.",
            }),
        }, version="999")
        with self.assertRaises(LoginError):
            provider.login("01012345678", "pw")
        self.assertEqual(provider.version, "999", "사용자가 고정한 버전은 바꾸지 않는다")
        self.assertEqual(len(provider.session.calls), 1)

    def test_macro_detection_is_not_retried_as_version_problem(self):
        """MACRO ERROR 본문에는 '업데이트' 안내가 실려 오지만 버전 문제가 아니다.

        본문 문구만 보고 재시도하면 차단된 계정에 로그인을 반복하게 된다.
        """
        provider = self._provider({
            "login.Login": json.dumps({
                "strResult": "FAIL",
                "h_msg_cd": "MACRO ERROR",
                "h_msg_txt": "원활한 서비스 이용을 위해 앱을 최신 버전으로 "
                             "업데이트한 뒤 재실행 후 안정적인 환경에서 사용해 주시기 바랍니다.",
            }),
        })
        from railcatch.errors import BlockedError

        with self.assertRaises(BlockedError):
            provider.login("01012345678", "pw")
        self.assertEqual(len(provider.session.calls), 1, "차단 응답에 재시도하면 안 된다")
        self.assertFalse(provider.logged_in)
