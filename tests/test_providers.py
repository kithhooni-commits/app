"""사업자 응답 파싱 테스트.

여기 쓰인 응답 샘플은 실제 서버 응답의 '형태'를 본뜬 것이다. 사업자가 필드를
바꾸면 이 테스트가 아니라 실제 호출이 먼저 깨진다 — `doctor --dump` 로 원본을
받아 이 샘플을 갱신하고 WIRE FORMAT 구역을 고치는 것이 정해진 절차다.
"""

import unittest

from railcatch.errors import BlockedError, ResponseError
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
