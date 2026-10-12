"""AI caption engine: Claude first, OpenAI fallback.

Writes the caption BODY from a prompt (and optional images). Tries Claude
(Anthropic) when ANTHROPIC_API_KEY is set — Claude writes stronger, more natural
brand/marketing copy — then OpenAI when OPENAI_API_KEY works. Returns None when
no provider is available or all attempts fail, so the caller can fall back to a
safe brand template. This module never raises.
"""

import os
import re
import time
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
# Default to Opus 5 for the best copy; override with ANTHROPIC_MODEL (e.g.
# claude-sonnet-5 or claude-haiku-4-5) to trade quality for cost.
ANTHROPIC_MODEL = (os.getenv("ANTHROPIC_MODEL") or "claude-opus-5").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

_RETRY_DELAYS = [5, 10]


_BALANCE_ERROR_CODES = {
    "credit_balance_exhausted", "insufficient_quota",
    "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
}


def _caption_balance_error(error):
    """Explicit billing failures require account action, not another request."""
    details = getattr(error, "body", None)
    if not isinstance(details, dict):
        details = {}
    if isinstance(details.get("error"), dict):
        details = details["error"]
    code = getattr(error, "code", None) or details.get("code")
    kind = getattr(error, "type", None) or details.get("type")
    if isinstance(code, str) and code in _BALANCE_ERROR_CODES:
        return True
    if kind == "insufficient_quota":
        return True
    return (
        getattr(error, "status_code", None) == 400
        and kind == "invalid_request_error"
        and "credit balance is too low" in str(details.get("message", "")).lower()
    )


def _caption_retry_delay(error, fallback):
    """Honor Retry-After without waiting indefinitely on one provider."""
    headers = getattr(getattr(error, "response", None), "headers", {}) or {}
    value = headers.get("retry-after")
    if not value:
        return fallback
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            until = parsedate_to_datetime(str(value))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            delay = (until - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
    if not math.isfinite(delay) or delay < 0:
        return fallback
    if delay > 60:
        return None  # Defer this provider instead of retrying before its deadline.
    return max(fallback, delay)


def _claude_image_blocks(image_uris):
    """Convert caption image URIs (data: or http URLs) to Claude image blocks."""
    blocks = []
    for uri in image_uris or []:
        if not uri:
            continue
        m = re.match(r"data:(image/[\w.+-]+);base64,(.*)", uri, re.DOTALL)
        if m:
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)},
            })
        elif uri.startswith("http"):
            blocks.append({"type": "image", "source": {"type": "url", "url": uri}})
    return blocks


def _generate_claude(prompt, image_uris=None):
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=0)
    content = _claude_image_blocks(image_uris) + [{"type": "text", "text": prompt}]
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": content}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip()


def _generate_openai(prompt, image_uris=None):
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY, max_retries=0)
    if image_uris:
        content = [{"type": "input_text", "text": prompt}]
        for uri in image_uris:
            content.append({"type": "input_image", "image_url": uri})
        model_input = [{"role": "user", "content": content}]
    else:
        model_input = prompt
    resp = client.responses.create(model=OPENAI_MODEL, input=model_input)
    return resp.output_text.strip()


def generate_caption_body(prompt, image_uris=None):
    """Return an AI-written caption body, or None if unavailable.

    Order: Claude (if key) → OpenAI (if key), retrying transient failures only.
    Explicit balance/quota failures immediately use the next provider. Any failure is
    logged and the next provider (or None) is used — this never raises."""
    providers = []
    if ANTHROPIC_API_KEY:
        providers.append(("Claude", _generate_claude))
    if OPENAI_API_KEY:
        providers.append(("OpenAI", _generate_openai))

    if not providers:
        print("No caption provider configured (no ANTHROPIC_API_KEY or OPENAI_API_KEY).")
        return None

    for name, fn in providers:
        for attempt in range(1, 4):
            try:
                body = (fn(prompt, image_uris) or "").strip()
                if body:
                    print(f"Caption written by {name} ({ANTHROPIC_MODEL if name == 'Claude' else OPENAI_MODEL}).")
                    return body
                raise RuntimeError("empty response")
            except Exception as e:  # noqa: BLE001
                if _caption_balance_error(e):
                    print(f"FAIL: {name} caption requires credits or quota; skipping retries: {e}")
                    break
                if attempt < 3:
                    delay = _caption_retry_delay(e, _RETRY_DELAYS[attempt - 1])
                    if delay is None:
                        print(f"WARNING: {name} retry deadline exceeds 60s; deferring provider.")
                        break
                    print(f"WARNING: {name} caption attempt {attempt} failed: {e}; retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    print(f"FAIL: {name} caption unavailable after 3 attempts: {e}")
    return None
