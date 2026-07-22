import base64
import os
import random
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from msal import ConfidentialClientApplication
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

load_dotenv()


# =========================
# Config
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# `or` (not getenv default) so an unset secret injected as "" still falls back.
IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL") or "gpt-image-1"
IMAGE_QUALITY = os.getenv("OPENAI_IMAGE_QUALITY") or "medium"

MS_TENANT_ID = os.getenv("MS_TENANT_ID")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
ONEDRIVE_ROOT_PATH = os.getenv("ONEDRIVE_ROOT_PATH") or "IG Auto Publisher"
ONEDRIVE_DRAFTS_FOLDER_NAME = os.getenv("ONEDRIVE_DRAFTS_FOLDER_NAME") or "drafts"
ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL") or "info@rebornaesthetics.com.au"

AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]

REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"]


# =========================
# Brand system (from the Reborn Aesthetics AI Design Guideline)
# =========================
CANVAS_W, CANVAS_H = 1080, 1350

BG = (243, 236, 224)        # warm cream / ivory
CARD = (236, 227, 213)      # slightly deeper cream for panels
TEXT = (74, 56, 42)         # chocolate brown
MUTED = (128, 108, 88)      # soft warm brown
GOLD = (176, 141, 87)       # bronze / gold accent

FOOTER_LINES = [
    "Annerley  ·  470 Ipswich Road      Fortitude Valley  ·  27 Brunswick Street",
    "0410 415 415  ·  rebornaesthetics.com.au",
]

# Common photographic direction shared by every hero prompt.
BRAND_PHOTO_STYLE = (
    "premium medical aesthetics clinic, luxury day spa atmosphere, calm Australian woman "
    "aged 25 to 40 with natural healthy skin and a relaxed expression, eyes gently closed, "
    "no posing, no looking at camera, warm cream ivory and beige tones, soft golden window "
    "light, minimal elegant interior, editorial fashion photography, tasteful and understated, "
    "Aesop and Jo Malone campaign mood, photorealistic, no text, no logo, no watermark"
)

TOPICS = [
    {
        "key": "skin_needling",
        "headline": "Rebuild Your Glow",
        "subheadline": "Healthy skin. Visible results.",
        "services": ["Skin Needling", "RF Skin Tightening", "Hydra Glow", "From $199"],
        "scene": "a professional skin needling facial treatment on a treatment bed, gloved practitioner, medical yet serene",
    },
    {
        "key": "tattoo_removal",
        "headline": "A Fresh Canvas",
        "subheadline": "Advanced laser tattoo removal.",
        "services": ["PicoWay Laser", "Tattoo Removal", "Pigmentation", "From $99"],
        "scene": "a modern laser treatment room with premium medical laser device, calm client resting",
    },
    {
        "key": "hifu",
        "headline": "Lift, Naturally",
        "subheadline": "Non-surgical skin tightening.",
        "services": ["HIFU", "RF Skin Tightening", "Profhilo", "From $250"],
        "scene": "an elegant minimal clinic with subtle modern technology, relaxed facial treatment",
    },
    {
        "key": "facial",
        "headline": "Glow Starts Here",
        "subheadline": "Signature luxury facials.",
        "services": ["Signature Facial", "Hydra Glow", "Oxygen Boost", "From $129"],
        "scene": "a warm spa facial room with white towels, soft candles and relaxing light, gentle facial in progress",
    },
    {
        "key": "pigmentation",
        "headline": "Clear, Even, Radiant",
        "subheadline": "Target pigmentation and tone.",
        "services": ["PicoWay Laser", "Carbon Laser", "Photofacial", "From $149"],
        "scene": "a bright fresh modern clinic, professional laser pigmentation treatment, glowing skin",
    },
    {
        "key": "anti_wrinkle",
        "headline": "Refined, Not Frozen",
        "subheadline": "Subtle anti-wrinkle artistry.",
        "services": ["Anti Wrinkle", "Dermal Fillers", "Skin Boosters", "From $179"],
        "scene": "an elegant consultation room, refined injectable treatment moment, tasteful and clinical",
    },
    {
        "key": "massage",
        "headline": "Slow Down, Restore",
        "subheadline": "Luxury remedial massage.",
        "services": ["Remedial Massage", "Warm Stone", "Body Contouring", "From $99"],
        "scene": "a luxury spa massage room with warm stones and natural light, deeply relaxing",
    },
]


# =========================
# Validation
# =========================
def validate_env():
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


# =========================
# Fonts (downloaded once, with graceful fallbacks)
# =========================
FONT_DIR = os.path.join(os.getcwd(), ".fonts")
_RAW = "https://raw.githubusercontent.com/google/fonts/main/"
FONT_URLS = {
    # Cormorant Garamond is a variable font; weight is selected per use.
    "serif": _RAW + "ofl/cormorantgaramond/CormorantGaramond%5Bwght%5D.ttf",
    "sans_light": _RAW + "ofl/poppins/Poppins-Light.ttf",
    "sans_regular": _RAW + "ofl/poppins/Poppins-Regular.ttf",
    "sans_medium": _RAW + "ofl/poppins/Poppins-Medium.ttf",
}
# Serif logical name -> variable font weight axis value.
SERIF_WEIGHTS = {"serif_regular": 500, "serif_medium": 600}
_FONT_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _ensure_font(name):
    os.makedirs(FONT_DIR, exist_ok=True)
    path = os.path.join(FONT_DIR, f"{name}.ttf")
    if os.path.exists(path):
        return path
    try:
        resp = requests.get(FONT_URLS[name], timeout=30)
        resp.raise_for_status()
        with open(path, "wb") as f:
            f.write(resp.content)
        return path
    except Exception as e:
        print(f"WARNING: could not download font {name}: {e}")
        return None


def font(key, size):
    if key in SERIF_WEIGHTS:
        path = _ensure_font("serif")
        if path:
            try:
                fnt = ImageFont.truetype(path, size)
                try:
                    fnt.set_variation_by_axes([SERIF_WEIGHTS[key]])
                except Exception:
                    pass
                return fnt
            except Exception:
                pass
    else:
        path = _ensure_font(key)
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass

    for fallback in _FONT_FALLBACKS:
        if os.path.exists(fallback):
            try:
                return ImageFont.truetype(fallback, size)
            except Exception:
                continue
    return ImageFont.load_default()


# =========================
# Text helpers (with letter spacing)
# =========================
def text_width(draw, text, fnt, tracking=0):
    if not text:
        return 0
    return sum(draw.textlength(ch, font=fnt) + tracking for ch in text) - tracking


def draw_tracked(draw, x, y, text, fnt, fill, tracking=0):
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + tracking


def draw_center(draw, y, text, fnt, fill, tracking=0):
    w = text_width(draw, text, fnt, tracking)
    draw_tracked(draw, (CANVAS_W - w) / 2, y, text, fnt, fill, tracking)


def wrap_lines(draw, text, fnt, max_w, tracking=0):
    words = text.split()
    lines, cur = [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if text_width(draw, trial, fnt, tracking) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


# =========================
# Image helpers
# =========================
def fit_cover(img, w, h):
    src_ratio = img.width / img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_h, new_w = h, max(w, int(round(h * src_ratio)))
    else:
        new_w, new_h = w, max(h, int(round(w / src_ratio)))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left, top = (new_w - w) // 2, (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


def rounded(img, radius):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width, img.height], radius=radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


# =========================
# Composition
# =========================
def compose_ad(hero_bytes, topic):
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(canvas)

    # --- Wordmark ---
    draw_center(draw, 84, "REBORN", font("serif_medium", 60), TEXT, tracking=14)
    draw_center(draw, 158, "AESTHETICS", font("sans_light", 22), MUTED, tracking=10)
    draw.line([(CANVAS_W / 2 - 34, 208), (CANVAS_W / 2 + 34, 208)], fill=GOLD, width=2)

    # --- Headline ---
    headline_font = font("serif_medium", 76)
    lines = wrap_lines(draw, topic["headline"], headline_font, CANVAS_W - 200)
    y = 244
    for line in lines:
        draw_center(draw, y, line, headline_font, TEXT, tracking=1)
        y += 78
    draw_center(draw, y + 6, topic["subheadline"], font("sans_light", 27), MUTED, tracking=2)

    # --- Hero image ---
    hero_x, hero_w = 96, CANVAS_W - 192
    hero_y, hero_h = 430, 560
    hero = Image.open(_bytes_io(hero_bytes)).convert("RGB")
    hero = fit_cover(hero, hero_w, hero_h)
    canvas.paste(rounded(hero, 20), (hero_x, hero_y), rounded(hero, 20))

    # --- Services line ---
    services = "      ".join(topic["services"])
    services_font = font("sans_regular", 25)
    sy = hero_y + hero_h + 46
    if text_width(draw, services, services_font, 1) <= CANVAS_W - 140:
        draw_center(draw, sy, services, services_font, TEXT, tracking=1)
        sy += 44
    else:
        half = (len(topic["services"]) + 1) // 2
        for group in ("      ".join(topic["services"][:half]), "      ".join(topic["services"][half:])):
            draw_center(draw, sy, group, services_font, TEXT, tracking=1)
            sy += 40
        sy += 4

    # --- Call to action ---
    cta_font = font("sans_medium", 25)
    cta_y = sy + 14
    draw_center(draw, cta_y, "BOOK NOW", cta_font, GOLD, tracking=8)
    cta_w = text_width(draw, "BOOK NOW", cta_font, 8)
    draw.line(
        [((CANVAS_W - cta_w) / 2, cta_y + 40), ((CANVAS_W + cta_w) / 2, cta_y + 40)],
        fill=GOLD, width=2,
    )

    # --- Footer ---
    fy = CANVAS_H - 96
    footer_font = font("sans_light", 18)
    for line in FOOTER_LINES:
        draw_center(draw, fy, line, footer_font, MUTED, tracking=1)
        fy += 30

    return canvas


def _bytes_io(data):
    from io import BytesIO

    return BytesIO(data)


# =========================
# OpenAI hero image
# =========================
def build_hero_prompt(topic):
    return (
        f"A vertical editorial photograph for a premium aesthetics clinic. Scene: {topic['scene']}. "
        f"{BRAND_PHOTO_STYLE}. Leave calm negative space, framed for a portrait social media hero image."
    )


def generate_hero(topic):
    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = build_hero_prompt(topic)

    last_error = None
    for attempt in range(1, 4):
        try:
            result = client.images.generate(
                model=IMAGE_MODEL,
                prompt=prompt,
                size="1024x1536",
                quality=IMAGE_QUALITY,
                n=1,
            )
            return base64.b64decode(result.data[0].b64_json)
        except Exception as e:
            last_error = e
            print(f"WARNING: hero image generation attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep([5, 10][attempt - 1])
    raise last_error


# =========================
# OneDrive upload (drafts)
# =========================
def get_access_token():
    app = ConfidentialClientApplication(
        client_id=MS_CLIENT_ID, client_credential=MS_CLIENT_SECRET, authority=AUTHORITY
    )
    result = app.acquire_token_for_client(scopes=SCOPES)
    if "access_token" not in result:
        raise Exception(f"Failed to get token: {result}")
    return result["access_token"]


def _graph(method, url, token, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    resp.raise_for_status()
    return resp


def _find_folder(items, name):
    for item in items.get("value", []):
        if item.get("name") == name and "folder" in item:
            return item
    return None


def ensure_drafts_folder(token):
    root = _graph("GET", f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/root/children", token).json()
    project = _find_folder(root, ONEDRIVE_ROOT_PATH)
    if not project:
        raise Exception(f"Project folder not found: {ONEDRIVE_ROOT_PATH}")

    children_url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{project['id']}/children"
    children = _graph("GET", children_url, token).json()
    drafts = _find_folder(children, ONEDRIVE_DRAFTS_FOLDER_NAME)
    if drafts:
        return drafts["id"]

    created = _graph(
        "POST", children_url, token,
        json={"name": ONEDRIVE_DRAFTS_FOLDER_NAME, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
    ).json()
    return created["id"]


def upload_draft(token, folder_id, filename, image_bytes):
    url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{folder_id}:/{filename}:/content"
    resp = _graph("PUT", url, token, headers={"Content-Type": "image/jpeg"}, data=image_bytes)
    return resp.json()


# =========================
# Main
# =========================
def pick_topic():
    override = os.getenv("AD_TOPIC")
    if override:
        for topic in TOPICS:
            if topic["key"] == override:
                return topic
    return random.choice(TOPICS)


def main():
    try:
        validate_env()
        topic = pick_topic()
        print(f"Selected topic: {topic['key']} - {topic['headline']}")

        print("Step 1: Generating hero image with OpenAI...")
        hero_bytes = generate_hero(topic)
        print(f"Hero image generated ({len(hero_bytes)} bytes).")

        print("Step 2: Composing branded ad...")
        ad = compose_ad(hero_bytes, topic)
        from io import BytesIO

        buffer = BytesIO()
        ad.save(buffer, format="JPEG", quality=92)
        ad_bytes = buffer.getvalue()
        print(f"Composed ad ({len(ad_bytes)} bytes, {CANVAS_W}x{CANVAS_H}).")

        print("Step 3: Uploading to OneDrive drafts folder...")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{topic['key']}_{timestamp}.jpg"
        token = get_access_token()
        folder_id = ensure_drafts_folder(token)
        upload_draft(token, folder_id, filename, ad_bytes)
        print(f"Draft uploaded: {ONEDRIVE_DRAFTS_FOLDER_NAME}/{filename}")

        print("\nAd draft created. Review it and move it into the posts folder to publish.")
        sys.exit(0)

    except Exception as e:
        print("\nERROR:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
