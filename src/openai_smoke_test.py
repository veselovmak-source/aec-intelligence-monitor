from __future__ import annotations

import os
import sys

import requests


API_URL = "https://api.openai.com/v1/responses"


def main() -> int:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")

    if not api_key:
        print("OpenAI smoke test FAILED: OPENAI_API_KEY is not configured.", file=sys.stderr)
        return 1
    if not model:
        print("OpenAI smoke test FAILED: OPENAI_MODEL is not configured.", file=sys.stderr)
        return 1

    try:
        response = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": "Reply with exactly the two letters: OK",
                "max_output_tokens": 32,
            },
            timeout=45,
        )
    except requests.RequestException as exc:
        print(
            f"OpenAI smoke test FAILED: network/request error ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1

    if not response.ok:
        message = ""
        try:
            error = response.json().get("error", {})
            message = error.get("message", "") if isinstance(error, dict) else ""
        except (ValueError, AttributeError):
            pass
        detail = f": {message[:500]}" if message else ""
        print(
            f"OpenAI smoke test FAILED: HTTP {response.status_code}{detail}",
            file=sys.stderr,
        )
        return 1

    try:
        payload = response.json()
    except ValueError:
        print("OpenAI smoke test FAILED: API returned invalid JSON.", file=sys.stderr)
        return 1

    output_text = "".join(
        part.get("text", "")
        for item in payload.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text" and isinstance(part.get("text"), str)
    ).strip()

    if not output_text:
        status = payload.get("status", "unknown")
        print(
            f"OpenAI smoke test FAILED: no text output (response status: {status}).",
            file=sys.stderr,
        )
        return 1

    print(f"OpenAI API smoke test PASSED; model={model}; response_id={payload.get('id', 'unknown')}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
