#!/usr/bin/env python3
"""Check that the keys in .env actually work.

Run it after editing .env:

    ./.venv/bin/python check_keys.py

Key values are never printed — only whether each one authenticates, and what
the app will do with it.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
OK, FAIL, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"


def masked(key):
    """Enough to tell two keys apart, not enough to use one."""
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "(너무 짧음)"


def check_openai(key):
    """Verify the key authenticates and the account has credit."""
    try:
        import openai

        client = openai.OpenAI(api_key=key)
        client.chat.completions.create(
            model=os.getenv("CHAT_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        return True, "인증 성공, 잔액 있음"
    except Exception as exc:
        text = str(exc)
        if "invalid_api_key" in text or "Incorrect API key" in text:
            return False, "키가 올바르지 않습니다"
        if "insufficient_quota" in text or "no credits" in text.lower():
            return False, "키는 맞지만 잔액이 0입니다 — 결제 페이지에서 충전하세요"
        return False, text[:110]


def check_assemblyai(key):
    """AssemblyAI has no ping endpoint; listing transcripts is the cheapest call."""
    try:
        response = httpx.get(
            "https://api.assemblyai.com/v2/transcript?limit=1",
            headers={"authorization": key},
            timeout=20,
        )
        if response.status_code == 200:
            return True, "인증 성공"
        if response.status_code in (401, 403):
            return False, "키가 올바르지 않습니다"
        return False, f"HTTP {response.status_code}: {response.text[:90]}"
    except Exception as exc:
        return False, str(exc)[:110]


def check_groq(key):
    try:
        response = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )
        if response.status_code == 200:
            return True, "인증 성공"
        if response.status_code in (401, 403):
            return False, "키가 올바르지 않습니다"
        return False, f"HTTP {response.status_code}"
    except Exception as exc:
        return False, str(exc)[:110]


CHECKS = [
    ("OPENAI_API_KEY", "OpenAI", check_openai, True,
     "요약 · 키워드 · AI 챗"),
    ("ASSEMBLYAI_API_KEY", "AssemblyAI", check_assemblyai, False,
     "미팅 전사 + 화자 분리"),
    ("GROQ_API_KEY", "Groq", check_groq, False,
     "강의 전사 (가장 저렴)"),
]


def main():
    print("\n키 확인 중…\n")
    results = {}

    for env_name, label, check, required, purpose in CHECKS:
        key = (os.getenv(env_name) or "").strip()
        if not key:
            mark = FAIL if required else DIM + "–" + RESET
            note = "필수인데 비어 있습니다" if required else "없음 (선택)"
            print(f"  {mark} {label:<12} {DIM}{purpose}{RESET}")
            print(f"      {note}\n")
            results[env_name] = False
            continue

        ok, detail = check(key)
        print(f"  {OK if ok else FAIL} {label:<12} {DIM}{purpose}{RESET}")
        print(f"      {masked(key)} — {detail}\n")
        results[env_name] = ok

    # What the app will actually do with what is configured.
    print("─" * 58)
    have_openai = results.get("OPENAI_API_KEY")
    have_assembly = results.get("ASSEMBLYAI_API_KEY")
    have_groq = results.get("GROQ_API_KEY")

    if not have_openai:
        print(f"\n{RED}요약 · 키워드 · 챗이 동작하지 않습니다.{RESET}")
        print("OPENAI_API_KEY 를 .env 에 넣어주세요.\n")
        return 1

    lecture = "Groq" if have_groq else ("AssemblyAI" if have_assembly else "OpenAI whisper")
    meeting = "AssemblyAI" if have_assembly else ("Groq" if have_groq else "OpenAI whisper")

    print(f"\n앱이 실제로 쓸 엔진:")
    print(f"  강의 전사   → {lecture}")
    print(f"  미팅 전사   → {meeting}")
    print(f"  화자 분리   → {'가능' if have_assembly else '불가 (AssemblyAI 키 필요)'}")
    print(f"  요약·챗     → OpenAI\n")

    if not have_assembly:
        print(f"{WARN} AssemblyAI 키를 넣으면 미팅 화자 분리가 켜지고 전사 비용이 절반 이하로 줄어듭니다.\n")

    print(f"{GREEN}준비 완료.{RESET} ./.venv/bin/python app.py 로 실행하세요.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
