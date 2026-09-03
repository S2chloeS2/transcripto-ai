"""Plans and monthly allowances.

Transcription is the one thing that costs real money per minute, so that is
what a plan meters. Everything else (summaries, keywords, chat) rides along.

Limits are minutes of audio per calendar month, UTC. There is no payment
integration yet: `set_plan` exists so a plan can be assigned by hand while
that is being wired up, and the account page is honest about it.
"""

from datetime import datetime, timezone

import db

PLANS = {
    "free": {
        "name": "무료",
        "minutes": 5 * 60,
        "price": 0,
        "blurb": "맛보기",
        "features": ["모든 입력 방식", "요약 · 키워드 · AI 챗", "폴더로 묶어 한꺼번에 질문",
                     "기록 30일 보관"],
    },
    "standard": {
        "name": "스탠다드",
        "minutes": 25 * 60,
        "price": 11900,
        "blurb": "매일 수업 듣는 학생",
        "features": ["무료의 모든 기능", "기록 무제한 보관", "회의 화자 분리",
                     "마크다운 내보내기"],
    },
    "pro": {
        "name": "프로",
        "minutes": 60 * 60,
        "price": 24900,
        "blurb": "회의가 잦은 팀",
        "features": ["스탠다드의 모든 기능", "긴 회의 우선 처리", "개인정보 자동 가리기"],
    },
}

ORDER = ["free", "standard", "pro"]


def plan_key(user):
    key = (user or {}).get("plan") or "free"
    return key if key in PLANS else "free"


def plan_of(user):
    return {"key": plan_key(user), **PLANS[plan_key(user)]}


def month_start():
    """ISO timestamp for the first instant of this month, UTC — usage before
    this does not count against the current allowance."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def allowance(user_id, user=None):
    user = user or db.get_user(user_id) or {}
    plan = plan_of(user)
    limit_s = plan["minutes"] * 60
    used_s = db.usage_seconds(user_id, since=month_start())
    remaining_s = max(0, limit_s - used_s)
    return {
        "plan": plan,
        "limit_s": limit_s,
        "used_s": used_s,
        "remaining_s": remaining_s,
        "used_pct": min(100, round(used_s / limit_s * 100)) if limit_s else 100,
        "remaining_label": _label(remaining_s),
        "limit_label": _label(limit_s),
        "used_label": _label(used_s),
    }


def check(user_id, needed_s, user=None):
    """(ok, message, allowance). Refuses when the request would overrun."""
    a = allowance(user_id, user)
    if needed_s <= a["remaining_s"]:
        return True, "", a
    if a["remaining_s"] <= 0:
        msg = (f"이번 달 {a['plan']['name']} 플랜의 {a['limit_label']}을 다 썼습니다. "
               f"다음 달 1일에 초기화되거나, 플랜을 올리면 바로 이어서 쓸 수 있습니다.")
    else:
        msg = (f"이 오디오는 {_label(needed_s)}인데 남은 시간이 {a['remaining_label']}입니다. "
               f"더 짧은 파일을 올리거나 플랜을 올려주세요.")
    return False, msg, a


def _label(seconds):
    seconds = int(round(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h and m:
        return f"{h}시간 {m}분"
    if h:
        return f"{h}시간"
    if m:
        return f"{m}분"
    return f"{seconds}초" if seconds else "0분"
