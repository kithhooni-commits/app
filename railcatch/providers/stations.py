"""역 코드 테이블.

주의: 여기 하드코딩된 코드는 사업자가 언제든 바꿀 수 있다. 코레일은 역 목록을
서버에서 내려주므로 런타임에 받아 캐시하고, 이 표는 오프라인 폴백으로만 쓴다.
`python -m railcatch stations --provider korail --refresh` 로 최신화할 수 있다.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import Provider, Station

# SRT 정차역. SRT는 역 목록 API가 없어 표를 들고 있어야 한다.
SRT_STATION_CODES: dict[str, str] = {
    "수서": "0551",
    "동탄": "0552",
    "평택지제": "0553",
    "천안아산": "0502",
    "오송": "0297",
    "대전": "0010",
    "공주": "0514",
    "익산": "0030",
    "정읍": "0033",
    "광주송정": "0036",
    "나주": "0037",
    "목포": "0041",
    "김천(구미)": "0507",
    "서대구": "0506",
    "동대구": "0015",
    "신경주": "0508",
    "울산(통도사)": "0509",
    "포항": "0515",
    "밀양": "0017",
    "구포": "0018",
    "부산": "0020",
    "진영": "0056",
    "창원중앙": "0512",
    "창원": "0057",
    "마산": "0059",
    "진주": "0063",
    "남원": "0048",
    "곡성": "0049",
    "구례구": "0050",
    "순천": "0051",
    "여천": "0819",
    "여수EXPO": "0053",
}

# 코레일 주요역 폴백. 서버에서 받은 목록이 있으면 항상 그쪽이 우선한다.
KORAIL_FALLBACK_STATION_CODES: dict[str, str] = {
    "서울": "0001",
    "용산": "0104",
    "광명": "0032",
    "영등포": "0023",
    "수원": "0030",
    "천안아산": "0502",
    "오송": "0297",
    "대전": "0010",
    "김천(구미)": "0507",
    "동대구": "0015",
    "경주": "0508",
    "울산(통도사)": "0509",
    "포항": "0515",
    "밀양": "0017",
    "구포": "0018",
    "부산": "0020",
    "마산": "0059",
    "창원중앙": "0512",
    "진주": "0063",
    "익산": "0030",
    "정읍": "0033",
    "광주송정": "0036",
    "목포": "0041",
    "여수EXPO": "0053",
    "순천": "0051",
    "강릉": "0206",
    "청량리": "0002",
    "상봉": "0003",
    "만종": "0209",
    "평창": "0211",
    "진부(오대산)": "0212",
}

# 별칭: 사람들이 실제로 입력하는 이름 → 정식 명칭
ALIASES: dict[str, str] = {
    "구미": "김천(구미)",
    "김천구미": "김천(구미)",
    "울산": "울산(통도사)",
    "통도사": "울산(통도사)",
    "여수": "여수EXPO",
    "여수엑스포": "여수EXPO",
    "진부": "진부(오대산)",
    "오대산": "진부(오대산)",
    "송정": "광주송정",
}


def canonical(name: str) -> str:
    """사용자 입력 역 이름을 정식 명칭으로 정규화."""
    n = name.strip().replace(" ", "")
    n = n.removesuffix("역") if len(n) > 1 else n
    return ALIASES.get(n, n)


def bundled(provider: Provider) -> list[Station]:
    table = SRT_STATION_CODES if provider is Provider.SRT else KORAIL_FALLBACK_STATION_CODES
    return [Station(name=n, code=c, provider=provider) for n, c in table.items()]


def cache_path(provider: Provider, data_dir: Path) -> Path:
    return data_dir / f"stations_{provider.value}.json"


def load_cached(provider: Provider, data_dir: Path) -> list[Station] | None:
    path = cache_path(provider, data_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw:
        return None
    return [Station(name=n, code=str(c), provider=provider) for n, c in raw.items()]


def save_cache(provider: Provider, data_dir: Path, stations: list[Station]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {s.name: s.code for s in stations}
    cache_path(provider, data_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
