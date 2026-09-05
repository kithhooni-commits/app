"""테스트 패키지.

파싱 실패 경고는 일부러 유발하는 테스트가 있으므로 로그를 잠재운다.
"""

import logging

logging.getLogger("railcatch").setLevel(logging.CRITICAL)
logging.disable(logging.WARNING)
