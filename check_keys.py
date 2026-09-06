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
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "(too short)"


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
        return True, "authenticated, credit available"
    except Exception as exc:
        text = str(exc)
        if "invalid_api_key" in text or "Incorrect API key" in text:
            return False, "key is not valid"
        if "insufficient_quota" in text or "no credits" in text.lower():
            return False, "key is valid but the balance is 0 - add credit on the billing page"
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
            return True, "authenticated"
        if response.status_code in (401, 403):
            return False, "key is not valid"
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
            return True, "authenticated"
        if response.status_code in (401, 403):
            return False, "key is not valid"
        return False, f"HTTP {response.status_code}"
    except Exception as exc:
        return False, str(exc)[:110]


CHECKS = [
    ("OPENAI_API_KEY", "OpenAI", check_openai, True,
     "summaries, keywords, AI chat"),
    ("ASSEMBLYAI_API_KEY", "AssemblyAI", check_assemblyai, False,
     "meeting transcription + speaker separation"),
    ("GROQ_API_KEY", "Groq", check_groq, False,
     "lecture transcription (cheapest)"),
]


def main():
    print("\nChecking keys...\n")
    results = {}

    for env_name, label, check, required, purpose in CHECKS:
        key = (os.getenv(env_name) or "").strip()
        if not key:
            mark = FAIL if required else DIM + "–" + RESET
            note = "required but empty" if required else "not set (optional)"
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
        print(f"\n{RED}Summaries, keywords and chat will not work.{RESET}")
        print("Put OPENAI_API_KEY in .env.\n")
        return 1

    lecture = "Groq" if have_groq else ("AssemblyAI" if have_assembly else "OpenAI whisper")
    meeting = "AssemblyAI" if have_assembly else ("Groq" if have_groq else "OpenAI whisper")

    print(f"\nEngines the app will actually use:")
    print(f"  lectures    -> {lecture}")
    print(f"  meetings    -> {meeting}")
    print(f"  speakers    -> {'yes' if have_assembly else 'no (needs an AssemblyAI key)'}")
    print(f"  summary/chat-> OpenAI\n")

    if not have_assembly:
        print(f"{WARN} Adding an AssemblyAI key enables speaker separation for meetings and cuts transcription cost by more than half.\n")

    print(f"{GREEN}Ready.{RESET} Run with ./.venv/bin/python app.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
