# railcatch

SRT · 코레일(KTX) **빈자리 감시 및 자동 선점** 도구.
빈자리가 나면 자동으로 좌석을 잡아두고 텔레그램으로 알려줍니다. **결제는 하지 않습니다** —
선점된 예약을 기한 내에 앱/홈페이지에서 직접 결제하셔야 합니다.

파이썬 3.11+ 만 있으면 되고, **설치할 외부 패키지가 없습니다** (전부 표준 라이브러리).

---

## ⚠ 먼저 읽어주세요

- 예매 사이트 이용약관은 자동화 프로그램(매크로) 사용을 **금지**합니다. 계정 이용정지 등
  불이익이 생길 수 있으며, 그 책임은 사용하는 사람에게 있습니다.
- 이 도구는 **본인 승차권을 구하려는 개인 용도**를 전제로 만들었습니다. 되팔이(암표)에
  쓰지 마세요. 표를 웃돈 받고 되파는 행위는 철도사업법 위반입니다.
- 조회 간격에는 **2초 하한이 코드로 강제**되어 있고, 오류가 나면 자동으로 간격을 늘립니다.
  서버에 부담을 주면 차단되고, 차단되면 아무것도 못 잡습니다. 하한을 낮추지 마세요.
- 서버가 요청을 거부하면(“비정상적인 접근” 등) 감시를 **즉시 중단**합니다. 계속 두드리는
  것보다 잠시 쉬는 편이 계정에 안전합니다.

## 코레일+ 통합에 대해

2025년 SRT·KTX 예매가 `코레일+` 앱으로 통합되면서 기존 SRT 트리거류 도구가 동작하지 않게
되었습니다. 다만 통합 이후에도 **기존 SRT/코레일 모바일 API는 한동안 유지되는 경우가 많아**,
이 도구는 먼저 그 경로를 사용합니다.

실제로 살아있는지는 **직접 확인해야 합니다**:

```bash
python -m railcatch doctor --provider srt --dump
python -m railcatch doctor --provider korail --dep 서울 --arr 부산 --dump
```

`doctor` 는 로그인 → 조회를 순서대로 시도하고, 실패하면 **서버 원본 응답을 그대로 출력**합니다.
사업자가 API를 바꿨다면 각 어댑터 파일 상단의 `WIRE FORMAT` 구역만 고치면 됩니다.
그 구역 밖의 코드는 손댈 필요가 없도록 격리해 두었습니다.

- `railcatch/providers/srt.py` — 엔드포인트, 요청 필드, 좌석 상태 문자열
- `railcatch/providers/korail.py` — 엔드포인트, 요청 필드, 좌석 상태 코드

기존 경로가 완전히 막히면 `RailProvider` 인터페이스(`providers/base.py`)를 구현한 어댑터를
하나 더 추가하면 됩니다. 감시 엔진·웹 UI·알림은 전혀 바꿀 필요가 없습니다.

---

## 설치

```bash
git clone https://github.com/kithhooni-commits/app.git railcatch
cd railcatch
cp .env.example .env
```

`.env` 를 열어 계정과 텔레그램 정보를 채웁니다.

```ini
SRT_ID=01012345678        # 회원번호 / 이메일 / 휴대폰번호 아무거나
SRT_PW=비밀번호
TELEGRAM_TOKEN=8123...:AAF...
TELEGRAM_CHAT_ID=123456789
POLL_INTERVAL=3
```

### 텔레그램 봇 만들기

1. 텔레그램에서 `@BotFather` 에게 `/newbot` → 봇 토큰을 받습니다.
2. 만든 봇과의 대화창에서 아무 메시지나 한 번 보냅니다. (이걸 해야 chat_id가 잡힙니다)
3. 다음 명령으로 chat_id 를 확인해 `.env` 에 넣습니다.

```bash
python -m railcatch telegram-chatid --token <봇토큰>
```

---

## 사용법

### 웹 UI (권장)

```bash
python -m railcatch serve
```

브라우저에서 `http://127.0.0.1:8777` 이 열립니다. 출발/도착역, 날짜, 시간대, 좌석,
인원을 넣고 **감시 시작**을 누르면 됩니다. 여러 조건을 동시에 감시할 수 있고,
목록에서 진행 상황이 2초마다 갱신됩니다.

> 웹 UI에는 인증이 없습니다. 기본값인 `127.0.0.1` 로만 띄우세요.

### 터미널에서 바로

```bash
# 9월 20일 수서→부산, 오전 8시~낮 12시 30분 사이, 2명
python -m railcatch watch 수서 부산 9/20 --window 08:00-12:30 --passengers 2

# 코레일(KTX), 특실만, 특정 열차번호만
python -m railcatch watch 서울 부산 9/20 --provider korail --seat special --trains 101 105

# 선점하지 않고 알림만 받기
python -m railcatch watch 수서 부산 9/20 --notify-only

# 30분 동안만 감시
python -m railcatch watch 수서 부산 9/20 --timeout 30
```

### 그 밖의 명령

```bash
python -m railcatch search 수서 부산 9/20      # 지금 좌석 현황만 한 번 조회
python -m railcatch stations --provider srt     # 역 목록 보기
python -m railcatch stations --provider korail --refresh   # 코레일 역 목록 서버에서 갱신
python -m railcatch doctor --provider srt --dump           # 동작 점검
```

`-v` / `-vv` 를 붙이면 로그가 자세해집니다.

---

## 동작 방식

```
WatchSpec (조건)
   │
   ├─ Watch (스레드 1개)  ── 조회 ──> RailProvider ──> 사업자 API
   │        │                              ↑
   │        │                        RateLimiter (2초 하한 + 지터 + 백오프)
   │        │
   │        ├─ 조건에 맞는 빈자리 발견?
   │        │     └─ 예 → reserve() → 성공 시 감시 종료 + 알림
   │        └─ 아니오 → 계속
   │
   └─ WatchManager: 사업자별 세션 1개를 여러 감시가 공유
```

- **선점 성공 시 해당 감시는 즉시 종료됩니다.** 중복 예약은 사용자 손해입니다.
- 사업자별로 로그인 세션을 하나만 두고 공유합니다. 같은 계정으로 세션을 여럿 만들면
  서버가 이전 세션을 끊는 경우가 있습니다.
- 감시가 늘어도 사업자당 총 요청 속도는 `RateLimiter` 상한을 넘지 않습니다.
- 연속 오류가 12회 쌓이거나, 출발일이 지나거나, `--timeout` 이 지나면 자동 종료됩니다.

---

## 테스트

```bash
python -m unittest discover -s tests -t . -v
```

네트워크 없이 전부 돕니다. 감시 엔진은 가짜 사업자(`tests/fakes.py`)로
선점 성공/직전 매진/차단/로그인 실패 등 시나리오를 검증합니다.

---

## 자주 겪는 문제

| 증상 | 확인할 것 |
|---|---|
| 로그인 실패 | 아이디 형식(회원번호/이메일/휴대폰) 확인. 홈페이지에서 직접 로그인해 비밀번호 만료·휴면 상태가 아닌지 확인 |
| 조회는 되는데 열차가 0편 | 역 이름 확인 (`stations` 명령). 코레일은 `--refresh` 로 역 목록 갱신 |
| 좌석 상태가 전부 `unknown` | 사업자가 응답 필드를 바꿨습니다. `doctor --dump` 후 `WIRE FORMAT` 구역 수정 |
| “요청이 거부되었습니다” | 조회 간격을 늘리고(`POLL_INTERVAL=5`) 한동안 쉬세요 |
| 텔레그램 알림이 안 옴 | 봇에게 먼저 메시지를 보냈는지, `telegram-chatid` 로 나온 값을 넣었는지 확인 |

## 라이선스

개인 용도로 자유롭게 쓰세요. 사용에 따른 책임은 사용자 본인에게 있습니다.
