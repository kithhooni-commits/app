"""railcatch CLI.

  python -m railcatch serve                     웹 UI 실행 (기본)
  python -m railcatch watch 수서 부산 09-20      터미널에서 바로 감시
  python -m railcatch search 수서 부산 09-20     한 번만 조회
  python -m railcatch doctor                    로그인/조회 점검 (원본 응답 확인)
  python -m railcatch stations --refresh        코레일 역 목록 갱신
  python -m railcatch telegram-chatid           텔레그램 chat_id 찾기
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .errors import RailCatchError
from .manager import WatchManager, build_notifier
from .models import Availability, Provider, SeatClass, TimeWindow, parse_date
from .providers import build_provider
from .watcher import Watch, WatchSpec, WatchState

log = logging.getLogger("railcatch")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    settings = Settings.load(Path.cwd())
    if getattr(args, "interval", None):
        settings.poll_interval = max(args.interval, settings.poll_interval)

    try:
        return args.func(args, settings)
    except RailCatchError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n중단했습니다.", file=sys.stderr)
        return 130


# ── serve ───────────────────────────────────────────────────
def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    from .web.server import serve

    host = args.host or settings.web_host
    port = args.port or settings.web_port
    manager = WatchManager(settings)
    httpd = serve(manager, host, port)
    url = f"http://{host}:{port}/"
    print(f"railcatch 웹 UI: {url}  (Ctrl+C 로 종료)")
    print(f"조회 간격 {settings.poll_interval:.1f}초 · "
          f"텔레그램 {'설정됨' if settings.telegram_enabled else '미설정'}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    stop = _install_sigint()
    try:
        while not stop.is_set():
            time.sleep(0.4)
    finally:
        print("\n종료 중…", file=sys.stderr)
        httpd.shutdown()
        manager.shutdown()
    return 0


# ── watch ───────────────────────────────────────────────────
def cmd_watch(args: argparse.Namespace, settings: Settings) -> int:
    spec = _spec_from_args(args)
    creds = settings.require_credentials(spec.provider)
    provider = build_provider(
        spec.provider,
        interval=settings.poll_interval,
        data_dir=settings.data_dir,
        version=settings.korail_version or None,
    )
    notifier = build_notifier(settings)
    watch = Watch(spec, provider, notifier, (creds.user_id, creds.password))

    print(f"감시 시작: {spec.title}")
    print(f"자동 선점: {'켜짐' if spec.auto_reserve else '꺼짐(알림만)'} · "
          f"조회 간격 {settings.poll_interval:.1f}초 · Ctrl+C 로 중단")
    watch.start()

    stop = _install_sigint()
    last = ""
    try:
        while watch.running and not stop.is_set():
            line = f"[{watch.status.attempts:>4}회] {watch.status.last_message}"
            if line != last:
                print(f"\r{line[:110]:<110}", end="", flush=True)
                last = line
            time.sleep(0.3)
    finally:
        print()
        watch.stop()
        watch.join(timeout=5.0)
        provider.close()

    status = watch.status
    print(f"\n{status.state}: {status.last_message}")
    return 0 if status.state == WatchState.SUCCEEDED else 1


# ── search ──────────────────────────────────────────────────
def cmd_search(args: argparse.Namespace, settings: Settings) -> int:
    day = parse_date(args.day)
    window = TimeWindow.parse(args.window)
    provider_enum = Provider(args.provider)
    creds = settings.require_credentials(provider_enum)
    provider = build_provider(
        provider_enum,
        interval=settings.poll_interval,
        data_dir=settings.data_dir,
        version=settings.korail_version or None,
    )
    try:
        provider.login(creds.user_id, creds.password)
        trains = provider.search(
            args.dep, args.arr, day, window.start, passengers=args.passengers
        )
    finally:
        provider.close()

    trains = [t for t in trains if window.contains(t.dep_at.time())]
    if not trains:
        print("조건에 맞는 열차가 없습니다.")
        return 1

    print(f"{'열차':<14}{'출발':<8}{'도착':<8}{'소요':<10}{'일반실':<10}{'특실':<10}")
    print("─" * 62)
    for t in trains:
        name = f"{t.train_name} {t.train_number}"
        dep = t.dep_at.strftime("%H:%M")
        arr = t.arr_at.strftime("%H:%M")
        dur = f"{t.duration_min // 60}시간 {t.duration_min % 60:02d}분"
        print(f"{name:<14}{dep:<8}{arr:<8}{dur:<10}{_mark(t.general):<10}{_mark(t.special):<10}")
    return 0


def _mark(a: Availability) -> str:
    return {
        Availability.AVAILABLE: "○ 가능",
        Availability.WAITLIST: "△ 대기",
        Availability.SOLD_OUT: "✕ 매진",
        Availability.UNKNOWN: "· -",
    }[a]


# ── doctor ──────────────────────────────────────────────────
def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    """로그인과 조회가 실제로 동작하는지 점검하고, 실패하면 원본 응답을 보여준다.

    사업자가 API를 바꿨을 때 어디가 깨졌는지 바로 알 수 있게 하는 것이 목적이다.
    """
    provider_enum = Provider(args.provider)
    creds = settings.credentials(provider_enum)
    print(f"■ {provider_enum.value} 점검")
    print(f"  계정 설정      : {'OK' if creds.present else '없음 (.env 확인)'}")
    if not creds.present:
        return 1

    provider = build_provider(
        provider_enum,
        interval=settings.poll_interval,
        data_dir=settings.data_dir,
        version=settings.korail_version or None,
    )
    ok = True
    try:
        try:
            provider.login(creds.user_id, creds.password)
            print(f"  로그인         : OK{_detail(provider)}")
        except RailCatchError as exc:
            print(f"  로그인         : 실패 — {exc}")
            print(f"  마지막 호출    : {getattr(provider, 'last_url', None) or '-'}")
            _dump(args, provider)
            return 1

        day = parse_date(args.day) if args.day else (datetime.now() + timedelta(days=1)).date()
        try:
            trains = provider.search(args.dep, args.arr, day, TimeWindow().start)
            print(f"  조회           : OK — {day} {args.dep}→{args.arr} {len(trains)}편"
                  f"{_detail(provider)}")
            if trains:
                t = trains[0]
                print(f"  첫 열차        : {t.summary()} "
                      f"[일반 {t.general.value} / 특실 {t.special.value}]")
                unknown = [c for c in ("general", "special")
                           if getattr(t, c) is Availability.UNKNOWN]
                if unknown:
                    print(f"  ⚠ 좌석 상태 미해석: {', '.join(unknown)} "
                          f"— --dump 로 원본을 확인하고 WIRE FORMAT 구역을 고치세요.")
                    ok = False
            else:
                print("  ⚠ 열차가 0편입니다. 역 이름/날짜를 확인하거나 --dump 를 보세요.")
                ok = False
        except RailCatchError as exc:
            print(f"  조회           : 실패 — {exc}")
            print(f"  마지막 호출    : {getattr(provider, 'last_url', None) or '-'}")
            ok = False
        _dump(args, provider)
    finally:
        provider.close()
    return 0 if ok else 1


def _detail(provider) -> str:  # type: ignore[no-untyped-def]
    """확정된 경로/버전처럼, 문제 생겼을 때 알아야 할 정보를 한 줄로."""
    bits = []
    resolved = getattr(provider, "resolved", None)
    if resolved:
        bits.append("경로 " + ", ".join(f"{k}={v.rsplit('/', 1)[-1]}" for k, v in resolved.items()))
    version = getattr(provider, "version", None)
    if version:
        bits.append(f"앱버전 {version}")
    return f"  ({' · '.join(bits)})" if bits else ""


def _dump(args: argparse.Namespace, provider) -> None:  # type: ignore[no-untyped-def]
    if not args.dump:
        return
    raw = getattr(provider, "last_raw", None)
    print("\n── 마지막 원본 응답 ──")
    if isinstance(raw, (dict, list)):
        print(json.dumps(raw, ensure_ascii=False, indent=2)[:6000])
    else:
        print(str(raw)[:6000])


# ── stations ────────────────────────────────────────────────
def cmd_stations(args: argparse.Namespace, settings: Settings) -> int:
    provider_enum = Provider(args.provider)
    provider = build_provider(
        provider_enum,
        interval=settings.poll_interval,
        data_dir=settings.data_dir,
        version=settings.korail_version or None,
    )
    try:
        if args.refresh:
            if provider_enum is not Provider.KORAIL:
                print("SRT는 역 목록 API가 없어 갱신할 수 없습니다.", file=sys.stderr)
                return 1
            creds = settings.require_credentials(provider_enum)
            provider.login(creds.user_id, creds.password)
            stations = provider.refresh_stations()  # type: ignore[attr-defined]
        else:
            stations = provider.stations()
        for s in sorted(stations, key=lambda x: x.name):
            print(f"{s.code:>6}  {s.name}")
        print(f"\n총 {len(stations)}개")
    finally:
        provider.close()
    return 0


# ── telegram-chatid ─────────────────────────────────────────
def cmd_telegram_chatid(args: argparse.Namespace, settings: Settings) -> int:
    from .notify import fetch_chat_ids

    token = args.token or settings.telegram_token
    if not token:
        print("봇 토큰이 없습니다. --token 으로 주거나 .env 의 TELEGRAM_TOKEN 을 채우세요.",
              file=sys.stderr)
        return 1
    chats = fetch_chat_ids(token)
    if not chats:
        print("최근 대화가 없습니다. 봇에게 아무 메시지나 먼저 보낸 뒤 다시 실행하세요.")
        return 1
    for c in chats:
        print(f"{c['chat_id']}\t{c['type']}\t{c['name']}")
    print("\n위 chat_id 를 .env 의 TELEGRAM_CHAT_ID 에 넣으세요.")
    return 0


# ── 공통 ────────────────────────────────────────────────────
def _spec_from_args(args: argparse.Namespace) -> WatchSpec:
    return WatchSpec(
        provider=Provider(args.provider),
        dep=args.dep,
        arr=args.arr,
        day=parse_date(args.day),
        window=TimeWindow.parse(args.window),
        seat_class=SeatClass(args.seat),
        passengers=args.passengers,
        auto_reserve=not args.notify_only,
        train_numbers=tuple(args.trains or ()),
        expires_at=(
            datetime.now() + timedelta(minutes=args.timeout) if args.timeout else None
        ),
    )


def _install_sigint() -> "threading.Event":
    stop = threading.Event()

    def handler(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handler)
    return stop


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING if verbose == 0 else (logging.INFO if verbose == 1 else logging.DEBUG)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="railcatch", description="SRT/코레일 빈자리 감시·자동 선점")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="로그 상세도 (-v, -vv)")
    parser.add_argument("--interval", type=float, help="조회 간격(초). 2초 미만은 무시됩니다.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="웹 UI 실행")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    p.set_defaults(func=cmd_serve)

    def add_route_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("dep", help="출발역 (예: 수서)")
        sp.add_argument("arr", help="도착역 (예: 부산)")
        sp.add_argument("day", help="날짜 (2026-09-20 또는 9/20)")
        sp.add_argument("--provider", choices=[p.value for p in Provider], default="korail")
        sp.add_argument("--window", default="00:00-23:59", help="출발 시각 구간 (예: 08:00-12:30)")
        sp.add_argument("--passengers", type=int, default=1, help="인원 (기본 1)")

    p = sub.add_parser("watch", help="터미널에서 감시")
    add_route_args(p)
    p.add_argument("--seat", choices=[s.value for s in SeatClass], default="any")
    p.add_argument("--trains", nargs="*", help="특정 열차번호만 감시")
    p.add_argument("--notify-only", action="store_true", help="선점하지 않고 알림만")
    p.add_argument("--timeout", type=int, help="이 분(分) 뒤 자동 종료")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("search", help="한 번만 조회")
    add_route_args(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("doctor", help="로그인/조회 점검")
    p.add_argument("--provider", choices=[p.value for p in Provider], default="korail")
    p.add_argument("--dep", default="서울")
    p.add_argument("--arr", default="부산")
    p.add_argument("--day", help="기본: 내일")
    p.add_argument("--dump", action="store_true", help="마지막 원본 응답 출력")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("stations", help="역 목록")
    p.add_argument("--provider", choices=[p.value for p in Provider], default="korail")
    p.add_argument("--refresh", action="store_true", help="서버에서 새로 받기 (코레일만)")
    p.set_defaults(func=cmd_stations)

    p = sub.add_parser("telegram-chatid", help="텔레그램 chat_id 찾기")
    p.add_argument("--token")
    p.set_defaults(func=cmd_telegram_chatid)

    parser.set_defaults(func=cmd_serve, host=None, port=None, no_browser=False)
    return parser


if __name__ == "__main__":
    sys.exit(main())
