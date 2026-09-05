"""텔레그램 봇 알림.

봇 토큰과 chat_id가 필요하다. chat_id를 모르면
`python -m railcatch telegram-chatid --token <토큰>` 으로 찾을 수 있다
(봇에게 아무 메시지나 먼저 보낸 뒤 실행).
"""

from __future__ import annotations

from typing import Any

from ..errors import ConfigError, TransportError
from ..transport import Session
from .base import Notifier

API = "https://api.telegram.org"


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, *, timeout: float = 10.0) -> None:
        if not token or not chat_id:
            raise ConfigError("텔레그램 알림에는 봇 토큰과 chat_id가 모두 필요합니다.")
        self.token = token
        self.chat_id = str(chat_id)
        self.session = Session(timeout=timeout)

    def send(self, title: str, body: str) -> None:
        text = f"*{_escape(title)}*\n{_escape(body)}"
        resp = self.session.post(
            f"{API}/bot{self.token}/sendMessage",
            json_body={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
        )
        if not resp.ok:
            raise TransportError(f"텔레그램 API 오류 {resp.status}: {resp.text[:200]}")


def fetch_chat_ids(token: str) -> list[dict[str, Any]]:
    """getUpdates로 최근 대화 상대의 chat_id를 찾아준다."""
    session = Session(timeout=10.0)
    resp = session.get(f"{API}/bot{token}/getUpdates").raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise TransportError(f"텔레그램 API 오류: {data.get('description')}")
    seen: dict[str, dict[str, Any]] = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is not None:
            seen[str(cid)] = {
                "chat_id": str(cid),
                "type": chat.get("type"),
                "name": chat.get("title") or chat.get("username") or chat.get("first_name"),
            }
    return list(seen.values())


_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def _escape(text: str) -> str:
    return "".join("\\" + c if c in _MDV2_SPECIAL else c for c in text)
