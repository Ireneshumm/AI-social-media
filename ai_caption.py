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

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
# Default to Opus 5 for the best copy; override with ANTHROPIC_MODEL (e.g.
# claude-sonnet-5 or claude-haiku-4-5) to trade quality for cost.
ANTHROPIC_MODEL = (os.getenv("ANTHROPIC_MODEL") or "claude-opus-5").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

_RETRY_DELAYS = [5, 10]


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

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
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

    client = OpenAI(api_key=OPENAI_API_KEY)
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

    Order: Claude (if key) → OpenAI (if key), each with retries. Any failure is
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
                if attempt < 3:
                    delay = _RETRY_DELAYS[attempt - 1]
                    print(f"WARNING: {name} caption attempt {attempt} failed: {e}; retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    print(f"FAIL: {name} caption unavailable after 3 attempts: {e}")
    return None
