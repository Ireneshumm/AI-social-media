import base64
import math
import os
import random
import sys
import time
from datetime import datetime
from io import BytesIO

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
ONEDRIVE_STORIES_FOLDER_NAME = os.getenv("ONEDRIVE_STORIES_FOLDER_NAME") or "stories"
ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL") or "info@rebornaesthetics.com.au"

# Output format: "feed" = 1080x1350 post (campaign/hero layouts); "story" =
# 1080x1920 vertical story. Story generation defaults to the stories folder.
AD_FORMAT = (os.getenv("AD_FORMAT") or "feed").strip().lower()

# Where finished images land. Default is the review folder "drafts" (or "stories"
# for story format); the automated schedule overrides this to the live publish
# queue ("posts"/"stories") for hands-off posting.
_DEFAULT_TARGET = ONEDRIVE_STORIES_FOLDER_NAME if AD_FORMAT == "story" else ONEDRIVE_DRAFTS_FOLDER_NAME
GENERATE_TARGET_FOLDER = os.getenv("AD_TARGET_FOLDER") or _DEFAULT_TARGET


def _int_env(name, default, lo=1, hi=10):
    # `or` so an empty-string secret still falls back; clamp to a sane range so a
    # misconfigured value can never trigger a runaway number of OpenAI calls.
    raw = os.getenv(name) or str(default)
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default

AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]

REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"]


# =========================
# Brand system (from the Reborn Aesthetics AI Design Guideline)
# =========================
CANVAS_W, CANVAS_H = 1080, 1350

BG = (243, 236, 224)        # warm cream / ivory
CARD = (233, 223, 208)      # slightly deeper cream for panels
TEXT = (74, 56, 42)         # chocolate brown
DARK = (38, 30, 24)         # near-black brown (headline contrast)
MUTED = (128, 108, 88)      # soft warm brown
GOLD = (176, 141, 87)       # bronze / gold accent
DARKBAR = (54, 41, 31)      # deep brown trust bar
LIGHT = (250, 246, 238)     # near-white for text on gold/dark

# Footer facts (two Brisbane clinic locations).
FOOTER = {
    "tagline": "TWO BRISBANE LOCATIONS",
    "loc1": "Annerley  ·  69 Juliette Street QLD 4103",
    "loc2": "Fortitude Valley  ·  27 Brunswick Street QLD 4006",
    "contact": "0410 415 415   ·   rebornaesthetics.com.au",
}
TRUST_BAR = "EXPERT THERAPISTS   ·   ADVANCED TECHNOLOGY   ·   MEDICAL GRADE TREATMENTS"

# Photographic direction. Two moods: a calm treatment/person shot, and a
# device / technology close-up so the feed also showcases the clinic's gear.
BRAND_PHOTO_STYLE = (
    "premium medical aesthetics clinic, luxury day spa atmosphere, calm Australian woman "
    "aged 25 to 40 with natural healthy skin and a relaxed expression, eyes gently closed, "
    "no posing, no looking at camera, fully clothed in a clean white spa robe or clinic gown, "
    "modest, professional and strictly non-sexual clinical wellness context, warm cream ivory "
    "and beige tones, soft golden window light, minimal elegant interior, editorial fashion "
    "photography, tasteful and understated, Aesop and Jo Malone campaign mood, photorealistic, "
    "no text, no logo, no watermark"
)

DEVICE_PHOTO_STYLE = (
    "elegant close-up product photography of a premium medical aesthetics device, sleek modern "
    "clinical technology with a treatment handpiece, on a clean minimal bench in a luxury clinic, "
    "warm cream ivory and beige tones, soft golden light, shallow depth of field, high-end brand "
    "campaign look, Four Seasons spa mood, photorealistic, no text, no logo, no watermark, no people"
)

TOPICS = [
    {
        "key": "skin_needling",
        "headline_lines": ["Rebuild", "Your", "Glow"],
        "subheadline": "Collagen-stimulating skin renewal for firmer, fresher skin.",
        "tagline": "Healthy skin, visibly restored.",
        "price": "FROM $199*",
        "badge": ["PREMIUM", "CARE", "MEDICAL GRADE"],
        "services": [
            {"name": "Skin Needling", "desc": "Collagen induction therapy", "icon": "target"},
            {"name": "RF Tightening", "desc": "Firmer, lifted skin", "icon": "lift"},
            {"name": "Hydra Glow", "desc": "Deep hydration boost", "icon": "droplet"},
            {"name": "LED Therapy", "desc": "Calm & rejuvenate", "icon": "glow"},
        ],
        "scene": "a professional skin needling facial treatment on a treatment bed, gloved practitioner, medical yet serene",
    },
    {
        "key": "tattoo_removal",
        "headline_lines": ["A Fresh", "Clean", "Canvas"],
        "subheadline": "Advanced laser tattoo removal, safely fading the past.",
        "tagline": "Start again, beautifully.",
        "price": "FROM $99*",
        "badge": ["SAFE", "EFFECTIVE", "PROVEN RESULTS"],
        "services": [
            {"name": "PicoWay Laser", "desc": "Advanced pico technology", "icon": "target"},
            {"name": "Tattoo Removal", "desc": "Gradual, safe fading", "icon": "sparkle"},
            {"name": "Pigmentation", "desc": "Even, clear tone", "icon": "glow"},
            {"name": "Skin Repair", "desc": "Soothe & restore", "icon": "leaf"},
        ],
        "scene": "a modern laser treatment room with premium medical laser device, calm client resting",
    },
    {
        "key": "hifu",
        "headline_lines": ["Lift,", "Naturally", "Ageless"],
        "subheadline": "Non-surgical skin tightening that lifts and contours.",
        "tagline": "Turn back time, gently.",
        "price": "FROM $250*",
        "badge": ["PREMIUM", "CARE", "REAL RESULTS"],
        "services": [
            {"name": "HIFU", "desc": "Deep lifting energy", "icon": "lift"},
            {"name": "RF Tightening", "desc": "Firm & sculpt", "icon": "wave"},
            {"name": "LED Therapy", "desc": "Firm & renew", "icon": "glow"},
            {"name": "Contouring", "desc": "Defined jawline", "icon": "target"},
        ],
        "scene": "an elegant minimal clinic with subtle modern technology, relaxed facial treatment",
    },
    {
        "key": "facial",
        "headline_lines": ["Glow", "Starts", "Here"],
        "subheadline": "Signature luxury facials tailored to your skin.",
        "tagline": "A ritual, not a routine.",
        "price": "FROM $129*",
        "badge": ["SIGNATURE", "LUXURY", "FACIALS"],
        "services": [
            {"name": "Signature Facial", "desc": "Bespoke skin ritual", "icon": "sparkle"},
            {"name": "Hydra Glow", "desc": "Deep hydration boost", "icon": "droplet"},
            {"name": "Oxygen Boost", "desc": "Radiance & lift", "icon": "glow"},
            {"name": "LED Therapy", "desc": "Calm & renew", "icon": "leaf"},
        ],
        "scene": "a warm spa facial room with white towels, soft candles and relaxing light, gentle facial in progress",
    },
    {
        "key": "pigmentation",
        "headline_lines": ["Clear,", "Even,", "Radiant"],
        "subheadline": "Target pigmentation and uneven tone at the source.",
        "tagline": "Luminous, from within.",
        "price": "FROM $149*",
        "badge": ["ADVANCED", "LASER", "MEDICAL GRADE"],
        "services": [
            {"name": "PicoWay Laser", "desc": "Precision pigment care", "icon": "target"},
            {"name": "Carbon Laser", "desc": "Bright, refined skin", "icon": "sparkle"},
            {"name": "Photofacial", "desc": "Even skin tone", "icon": "glow"},
            {"name": "Brightening Facial", "desc": "Hydrate & restore", "icon": "droplet"},
        ],
        "scene": "a bright fresh modern clinic, professional laser pigmentation treatment, glowing skin",
    },
    # NOTE: Injectable topics (anti-wrinkle / botulinum toxin, dermal fillers,
    # skin boosters, bio-remodelling, unapproved peptides) are intentionally NOT
    # offered here. Advertising prescription-only cosmetic injectables to the
    # public — directly or indirectly — is prohibited in Australia (TGA), so
    # they must never enter the generated-ad rotation.
    {
        "key": "massage",
        "headline_lines": ["Slow", "Down,", "Restore"],
        "subheadline": "Luxury remedial massage to release and renew.",
        "tagline": "Rest is a treatment too.",
        "price": "FROM $99*",
        "badge": ["RELAX", "RESTORE", "RENEW"],
        "services": [
            {"name": "Remedial Massage", "desc": "Release deep tension", "icon": "wave"},
            {"name": "Warm Stone", "desc": "Soothing heat therapy", "icon": "glow"},
            {"name": "Body Contouring", "desc": "Sculpt & tone", "icon": "target"},
            {"name": "Lymphatic", "desc": "Detox & de-puff", "icon": "leaf"},
        ],
        "scene": "a luxury spa massage room with warm stones and natural light, deeply relaxing",
    },
    {
        "key": "laser",
        "headline_lines": ["Precision,", "Perfected"],
        "subheadline": "Medical-grade laser for skin, pigment and hair.",
        "tagline": "Technology you can trust.",
        "price": "FROM $89*",
        "badge": ["MEDICAL", "GRADE", "TECHNOLOGY"],
        "services": [
            {"name": "PicoWay Laser", "desc": "Advanced pico power", "icon": "target"},
            {"name": "Laser Hair", "desc": "Smooth, lasting results", "icon": "sparkle"},
            {"name": "Fractional", "desc": "Resurface & renew", "icon": "glow"},
            {"name": "Vascular", "desc": "Clear redness & veins", "icon": "wave"},
        ],
        "scene": "a modern laser suite, a calm client wearing protective eyewear during a laser session",
    },
    {
        "key": "ipl",
        "headline_lines": ["Even", "Every", "Tone"],
        "subheadline": "IPL photofacials for clearer, brighter skin.",
        "tagline": "Light that transforms.",
        "price": "FROM $119*",
        "badge": ["ADVANCED", "IPL", "TECHNOLOGY"],
        "services": [
            {"name": "IPL Photofacial", "desc": "Even skin tone", "icon": "glow"},
            {"name": "Rosacea Care", "desc": "Calm redness", "icon": "leaf"},
            {"name": "Sun Damage", "desc": "Fade pigment", "icon": "sparkle"},
            {"name": "Skin Rejuven.", "desc": "Fresh, clear glow", "icon": "droplet"},
        ],
        "scene": "a bright modern clinic, IPL photofacial treatment on relaxed skin, gentle and clinical",
    },
    {
        "key": "body_contouring",
        "headline_lines": ["Sculpt", "&", "Define"],
        "subheadline": "Non-invasive body contouring and fat reduction.",
        "tagline": "Confidence, reshaped.",
        "price": "FROM $149*",
        "badge": ["ADVANCED", "BODY", "TECHNOLOGY"],
        "services": [
            {"name": "Fat Freezing", "desc": "Target stubborn fat", "icon": "target"},
            {"name": "RF Contour", "desc": "Firm & tighten", "icon": "wave"},
            {"name": "Cavitation", "desc": "Smooth & sculpt", "icon": "lift"},
            {"name": "Lymphatic", "desc": "Detox & de-puff", "icon": "leaf"},
        ],
        "scene": "a sleek body-contouring treatment room with a modern device, calm client relaxing",
    },
    {
        "key": "hydrafacial",
        "headline_lines": ["The Deep", "Clean", "Glow"],
        "subheadline": "Cleanse, extract and hydrate in one treatment.",
        "tagline": "Skin, reset.",
        "price": "FROM $139*",
        "badge": ["SIGNATURE", "GLOW", "FACIAL"],
        "services": [
            {"name": "HydraFacial", "desc": "Deep cleanse & hydrate", "icon": "droplet"},
            {"name": "Extraction", "desc": "Clear congestion", "icon": "sparkle"},
            {"name": "Boosters", "desc": "Targeted serums", "icon": "glow"},
            {"name": "LED Finish", "desc": "Calm & glow", "icon": "leaf"},
        ],
        "scene": "a HydraFacial treatment with a modern device wand gliding over glowing skin, serene clinic",
    },
]

# Per-topic device / technology scene, used when a generation renders the
# machine-focused mood instead of a person. Falls back to a generic device shot.
DEVICE_SCENES = {
    "skin_needling": "a premium micro-needling / RF pen device resting on a clean clinical tray, handpiece in focus",
    "tattoo_removal": "a premium pico laser tattoo-removal machine with handpiece, sleek control screen glowing softly",
    "hifu": "a modern HIFU / ultrasound skin-tightening machine with handpiece and elegant control screen",
    "facial": "a luxury facial steamer and serum bottles on a clean minimal clinic bench, elegant tools",
    "pigmentation": "a pico / carbon laser device with handpiece, sleek dark panel and warm ambient light",
    "massage": "warm basalt massage stones, rolled towels and aromatherapy oils on a spa bench",
    "laser": "a high-end aesthetic laser platform with handpiece and glowing touchscreen, dark sleek housing",
    "ipl": "an IPL photofacial machine with handpiece and cooling tip, modern clinical technology",
    "body_contouring": "a body-contouring / fat-freezing machine with applicators, sleek modern medical design",
    "hydrafacial": "a HydraFacial machine with its spiral wand tip and serum vials, clean modern clinic bench",
}


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
    # Cormorant Garamond variable fonts; weight is selected per use.
    "serif": _RAW + "ofl/cormorantgaramond/CormorantGaramond%5Bwght%5D.ttf",
    "serif_italic": _RAW + "ofl/cormorantgaramond/CormorantGaramond-Italic%5Bwght%5D.ttf",
    "sans_light": _RAW + "ofl/poppins/Poppins-Light.ttf",
    "sans_regular": _RAW + "ofl/poppins/Poppins-Regular.ttf",
    "sans_medium": _RAW + "ofl/poppins/Poppins-Medium.ttf",
    "sans_semibold": _RAW + "ofl/poppins/Poppins-SemiBold.ttf",
}
# Serif logical name -> (base font key, variable weight axis value).
SERIF_WEIGHTS = {
    "serif_regular": ("serif", 500),
    "serif_medium": ("serif", 600),
    "serif_bold": ("serif", 700),
    "serif_italic": ("serif_italic", 500),
}
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
        base, weight = SERIF_WEIGHTS[key]
        path = _ensure_font(base)
        if path:
            try:
                fnt = ImageFont.truetype(path, size)
                try:
                    fnt.set_variation_by_axes([weight])
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


def draw_center_x(draw, cx, y, text, fnt, fill, tracking=0):
    w = text_width(draw, text, fnt, tracking)
    draw_tracked(draw, cx - w / 2, y, text, fnt, fill, tracking)


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


# =========================
# Line-art icons (drawn centered at cx, cy within radius r)
# =========================
def _icon_glow(draw, cx, cy, r, c, w):
    draw.ellipse([cx - r * 0.3, cy - r * 0.3, cx + r * 0.3, cy + r * 0.3], outline=c, width=w)
    for i in range(8):
        a = math.radians(i * 45)
        draw.line(
            [(cx + math.cos(a) * r * 0.5, cy + math.sin(a) * r * 0.5),
             (cx + math.cos(a) * r * 0.95, cy + math.sin(a) * r * 0.95)],
            fill=c, width=w,
        )


def _icon_droplet(draw, cx, cy, r, c, w):
    top = (cx, cy - r)
    draw.line([top, (cx - r * 0.66, cy + r * 0.08)], fill=c, width=w)
    draw.line([top, (cx + r * 0.66, cy + r * 0.08)], fill=c, width=w)
    draw.arc([cx - r * 0.66, cy - r * 0.55, cx + r * 0.66, cy + r * 0.9], start=0, end=180, fill=c, width=w)


def _icon_sparkle(draw, cx, cy, r, c, w):
    pts = []
    for i in range(8):
        a = math.radians(i * 45 - 90)
        rad = r if i % 2 == 0 else r * 0.4
        pts.append((cx + math.cos(a) * rad, cy + math.sin(a) * rad))
    draw.polygon(pts, outline=c, width=w)


def _icon_wave(draw, cx, cy, r, c, w):
    for off in (-r * 0.42, 0, r * 0.42):
        pts = []
        for t in range(0, 41):
            x = cx - r + (2 * r) * t / 40
            y = cy + off + math.sin(t / 40 * 2 * math.pi) * r * 0.22
            pts.append((x, y))
        draw.line(pts, fill=c, width=w, joint="curve")


def _icon_lift(draw, cx, cy, r, c, w):
    draw.line([(cx, cy + r * 0.75), (cx, cy - r * 0.55)], fill=c, width=w)
    draw.line([(cx - r * 0.55, cy - r * 0.02), (cx, cy - r * 0.62)], fill=c, width=w)
    draw.line([(cx + r * 0.55, cy - r * 0.02), (cx, cy - r * 0.62)], fill=c, width=w)


def _icon_target(draw, cx, cy, r, c, w):
    draw.ellipse([cx - r * 0.85, cy - r * 0.85, cx + r * 0.85, cy + r * 0.85], outline=c, width=w)
    draw.ellipse([cx - r * 0.4, cy - r * 0.4, cx + r * 0.4, cy + r * 0.4], outline=c, width=w)
    draw.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=c)


def _icon_leaf(draw, cx, cy, r, c, w):
    draw.arc([cx - r * 0.9, cy - r, cx + r * 0.5, cy + r], start=270, end=90, fill=c, width=w)
    draw.arc([cx - r * 0.5, cy - r, cx + r * 0.9, cy + r], start=90, end=270, fill=c, width=w)
    draw.line([(cx, cy - r * 0.8), (cx, cy + r * 0.8)], fill=c, width=w)


ICONS = {
    "glow": _icon_glow,
    "droplet": _icon_droplet,
    "sparkle": _icon_sparkle,
    "wave": _icon_wave,
    "lift": _icon_lift,
    "target": _icon_target,
    "leaf": _icon_leaf,
}


def _diamond(draw, cx, cy, r, c):
    draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=c)


# =========================
# Composition
# =========================
def compose_ad(hero_bytes, topic):
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(canvas)

    HERO_X = 560           # left edge of the hero column
    HERO_BOTTOM = 792      # hero photo extends from the top to here
    LM = 74                # left content margin

    # --- Hero photo (full-bleed, right column) ---
    hero = Image.open(BytesIO(hero_bytes)).convert("RGB")
    hero = fit_cover(hero, CANVAS_W - HERO_X, HERO_BOTTOM)
    canvas.paste(hero, (HERO_X, 0))
    # Soft cream gradient on the hero's left edge so the seam reads as intentional.
    seam = Image.new("L", (90, HERO_BOTTOM), 0)
    for x in range(90):
        for_alpha = int(210 * (1 - x / 90))
        seam.paste(for_alpha, (x, 0, x + 1, HERO_BOTTOM))
    overlay = Image.new("RGB", (90, HERO_BOTTOM), BG)
    canvas.paste(overlay, (HERO_X, 0), seam)

    # --- Wordmark (top-left) ---
    draw_tracked(draw, LM, 70, "REBORN", font("serif_bold", 46), TEXT, tracking=8)
    draw_tracked(draw, LM + 2, 128, "AESTHETICS", font("sans_light", 15), MUTED, tracking=7)
    draw.line([(LM + 2, 154), (LM + 96, 154)], fill=GOLD, width=2)

    # --- Headline (large serif, alternating brown / near-black) ---
    hf = font("serif_medium", 82)
    hy = 208
    line_colors = [TEXT, DARK, GOLD]
    for i, line in enumerate(topic["headline_lines"]):
        draw_tracked(draw, LM, hy, line, hf, line_colors[i % len(line_colors)], tracking=1)
        hy += 82

    # --- Subheadline + italic tagline ---
    sub_f = font("sans_light", 21)
    sy = hy + 14
    for line in wrap_lines(draw, topic["subheadline"], sub_f, HERO_X - LM - 30):
        draw_tracked(draw, LM, sy, line, sub_f, MUTED, tracking=1)
        sy += 30
    draw_tracked(draw, LM, sy + 8, topic["tagline"], font("serif_italic", 30), GOLD, tracking=1)
    sy += 8 + 44

    # --- Price box ---
    box_w, box_h = 292, 84
    bx, by = LM, sy + 6
    draw.rectangle([bx, by, bx + box_w, by + box_h], outline=GOLD, width=2)
    draw_tracked(draw, bx + 20, by + 16, "FIRST VISIT SPECIAL", font("sans_semibold", 12), GOLD, tracking=3)
    draw_tracked(draw, bx + 20, by + 38, topic["price"], font("serif_bold", 34), TEXT, tracking=1)

    # --- Circular seal badge (overlapping the seam, top-right) ---
    _draw_badge(draw, HERO_X + 6, 150, 74, topic["badge"])

    # --- Service row (four line-art icon columns) ---
    band_y = 838
    draw.line([(LM, band_y - 24), (CANVAS_W - LM, band_y - 24)], fill=(212, 200, 182), width=1)
    centers = [175, 410, 670, 905]
    for cx, service in zip(centers, topic["services"]):
        _draw_service(draw, cx, band_y, service)

    # --- CTA button (filled gold) ---
    _draw_button(draw, CANVAS_W / 2, 1076, 320, 62, "BOOK NOW")

    # --- Footer ---
    draw_center_x(draw, CANVAS_W / 2, 1150, FOOTER["tagline"], font("sans_semibold", 14), GOLD, tracking=5)
    draw_center_x(draw, CANVAS_W / 2, 1180, FOOTER["loc1"], font("sans_light", 17), TEXT, tracking=1)
    draw_center_x(draw, CANVAS_W / 2, 1208, FOOTER["loc2"], font("sans_light", 17), TEXT, tracking=1)
    draw_center_x(draw, CANVAS_W / 2, 1238, FOOTER["contact"], font("sans_regular", 17), TEXT, tracking=1)

    # --- Dark trust bar ---
    draw.rectangle([0, 1278, CANVAS_W, CANVAS_H], fill=DARKBAR)
    draw_center_x(draw, CANVAS_W / 2, 1306, TRUST_BAR, font("sans_light", 14), LIGHT, tracking=2)

    return canvas


def _fit_font(draw, text, keys_sizes, max_w, tracking):
    # Return the first (key, size) whose rendered width fits max_w, shrinking
    # the size down if needed so long words never spill past the ring.
    key, size = keys_sizes
    while size > 8:
        fnt = font(key, size)
        if text_width(draw, text, fnt, tracking) <= max_w:
            return fnt
        size -= 1
    return font(key, 8)


def _draw_badge(draw, cx, cy, r, lines):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BG, outline=GOLD, width=3)
    draw.ellipse([cx - r + 9, cy - r + 9, cx + r - 9, cy + r - 9], outline=GOLD, width=1)
    _icon_sparkle(draw, cx, cy - r + 30, 9, GOLD, 2)
    specs = [("sans_semibold", 15), ("serif_medium", 20), ("sans_semibold", 12)]
    ys = [cy - 16, cy + 2, cy + 30]
    max_w = 2 * (r - 15)
    for line, spec, y in zip(lines, specs, ys):
        fnt = _fit_font(draw, line, spec, max_w, tracking=2)
        draw_center_x(draw, cx, y, line, fnt, TEXT, tracking=2)


def _draw_service(draw, cx, top_y, service):
    r = 33
    cy = top_y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=GOLD, width=1)
    ICONS.get(service["icon"], _icon_sparkle)(draw, cx, cy, r * 0.52, TEXT, 3)
    draw_center_x(draw, cx, cy + r + 16, service["name"], font("sans_semibold", 18), TEXT, tracking=1)
    dy = cy + r + 42
    for line in wrap_lines(draw, service["desc"], font("sans_light", 14), 220):
        draw_center_x(draw, cx, dy, line, font("sans_light", 14), MUTED)
        dy += 20


def _draw_button(draw, cx, cy, w, h, label):
    x0, y0 = cx - w / 2, cy - h / 2
    draw.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=h / 2, fill=GOLD)
    lf = font("sans_semibold", 22)
    lw = text_width(draw, label, lf, 4)
    block = lw + 30
    tx = cx - block / 2
    _diamond(draw, tx + 8, cy + 1, 7, LIGHT)
    draw_tracked(draw, tx + 30, cy - 15, label, lf, LIGHT, tracking=4)


def _scrim(canvas, box_h, at_bottom, color, max_alpha, power=1.5):
    # A soft vertical gradient of `color` fading to transparent, so text stays
    # legible over any hero photo. Uses the canvas's own dimensions so it works
    # for both feed (1080x1350) and story (1080x1920) canvases.
    w, h = canvas.size
    grad = Image.new("L", (1, box_h), 0)
    for y in range(box_h):
        t = y / box_h
        edge = t if at_bottom else (1 - t)
        grad.putpixel((0, y), int(max_alpha * (edge ** power)))
    grad = grad.resize((w, box_h))
    layer = Image.new("RGB", (w, box_h), color)
    y0 = h - box_h if at_bottom else 0
    canvas.paste(layer, (0, y0), grad)


# =========================
# Composition B: photo-forward "big image" layout
# =========================
def compose_ad_hero(hero_bytes, topic):
    hero = Image.open(BytesIO(hero_bytes)).convert("RGB")
    canvas = fit_cover(hero, CANVAS_W, CANVAS_H).copy()

    dark = (26, 18, 12)
    _scrim(canvas, 760, True, dark, 220)     # bottom, for headline block
    _scrim(canvas, 300, False, dark, 120)    # top, for wordmark
    draw = ImageDraw.Draw(canvas, "RGBA")

    cream = (236, 227, 213)

    # --- Inset frame ---
    m = 42
    draw.rectangle([m, m, CANVAS_W - m, CANVAS_H - m], outline=(240, 232, 219, 150), width=2)

    # --- Wordmark (top-center) ---
    draw_center_x(draw, CANVAS_W / 2, 76, "REBORN", font("serif_medium", 46), LIGHT, tracking=12)
    draw_center_x(draw, CANVAS_W / 2, 132, "AESTHETICS", font("sans_light", 15), cream, tracking=8)

    # --- Bottom headline block (anchored near the base, built upward) ---
    hf = font("serif_medium", 84)
    lines = wrap_lines(draw, " ".join(topic["headline_lines"]), hf, CANVAS_W - 300)
    end_y = 1108
    hy = end_y - len(lines) * 84
    for line in lines:
        draw_center_x(draw, CANVAS_W / 2, hy, line, hf, LIGHT, tracking=1)
        hy += 84

    draw.line([(CANVAS_W / 2 - 38, end_y + 10), (CANVAS_W / 2 + 38, end_y + 10)], fill=GOLD, width=2)
    draw_center_x(draw, CANVAS_W / 2, end_y + 26, topic["tagline"], font("serif_italic", 31), cream, tracking=1)

    _draw_button(draw, CANVAS_W / 2, end_y + 108, 300, 60, "BOOK NOW")

    draw_center_x(
        draw, CANVAS_W / 2, CANVAS_H - 74,
        f"{FOOTER['tagline']}   ·   {FOOTER['contact'].split('·')[-1].strip()}",
        font("sans_light", 14), cream, tracking=2,
    )

    return canvas


# =========================
# Composition C: vertical 9:16 story
# =========================
STORY_W, STORY_H = 1080, 1920


def compose_story(hero_bytes, topic):
    hero = Image.open(BytesIO(hero_bytes)).convert("RGB")
    canvas = fit_cover(hero, STORY_W, STORY_H).copy()

    dark = (26, 18, 12)
    _scrim(canvas, 1040, True, dark, 225)    # bottom, for headline block
    _scrim(canvas, 420, False, dark, 130)    # top, for wordmark
    draw = ImageDraw.Draw(canvas, "RGBA")

    cream = (236, 227, 213)
    cx = STORY_W / 2

    # --- Inset frame ---
    m = 46
    draw.rectangle([m, m, STORY_W - m, STORY_H - m], outline=(240, 232, 219, 150), width=2)

    # --- Wordmark (top-center) ---
    draw_center_x(draw, cx, 122, "REBORN", font("serif_medium", 58), LIGHT, tracking=14)
    draw_center_x(draw, cx, 194, "AESTHETICS", font("sans_light", 18), cream, tracking=9)

    # --- Bottom headline block (anchored near the base, built upward) ---
    hf = font("serif_medium", 110)
    lines = wrap_lines(draw, " ".join(topic["headline_lines"]), hf, STORY_W - 300)
    end_y = 1508
    hy = end_y - len(lines) * 108
    for line in lines:
        draw_center_x(draw, cx, hy, line, hf, LIGHT, tracking=1)
        hy += 108

    draw.line([(cx - 46, end_y + 16), (cx + 46, end_y + 16)], fill=GOLD, width=2)
    draw_center_x(draw, cx, end_y + 38, topic["tagline"], font("serif_italic", 41), cream, tracking=1)
    draw_center_x(draw, cx, end_y + 104, topic["price"], font("sans_light", 26), LIGHT, tracking=3)

    _draw_button(draw, cx, end_y + 196, 360, 72, "BOOK NOW")

    draw_center_x(
        draw, cx, STORY_H - 118,
        f"{FOOTER['tagline']}   ·   {FOOTER['contact'].split('·')[-1].strip()}",
        font("sans_light", 17), cream, tracking=2,
    )

    return canvas


def compose(hero_bytes, topic, layout):
    if layout == "story":
        return compose_story(hero_bytes, topic)
    if layout == "hero":
        return compose_ad_hero(hero_bytes, topic)
    return compose_ad(hero_bytes, topic)


# =========================
# OpenAI hero image
# =========================
# Topics whose "relaxed person" scene risks tripping image moderation (e.g. a
# person on a massage table) lean toward device/technology shots instead.
_DEVICE_LEANING = {"massage", "body_contouring"}


def pick_photo_mode(topic):
    # AD_PHOTO can pin "person" or "device"; otherwise pick at random, biased
    # slightly toward people. Device shots showcase the clinic's technology.
    mode = (os.getenv("AD_PHOTO") or "").strip().lower()
    if mode in ("person", "device"):
        return mode
    if topic["key"] in _DEVICE_LEANING:
        return random.choice(["device", "device", "person"])
    return random.choice(["person", "person", "device"])


def build_hero_prompt(topic, mode):
    if mode == "device":
        scene = DEVICE_SCENES.get(topic["key"], topic["scene"])
        style = DEVICE_PHOTO_STYLE
    else:
        scene = topic["scene"]
        style = BRAND_PHOTO_STYLE
    return (
        f"A vertical editorial photograph for a premium aesthetics clinic. Scene: {scene}. "
        f"{style}. Leave calm negative space, framed for a tall portrait crop where the main "
        f"subject sits toward the right side of the frame."
    )


def generate_hero(topic, mode):
    """Generate a hero photo, returning (image_bytes, mode_used).

    If a person prompt is rejected by image moderation, the final attempt falls
    back to the device scene (no people), which reliably passes — so an
    unattended run still produces an image instead of losing the post."""
    client = OpenAI(api_key=OPENAI_API_KEY)
    # Last attempt forces device mode as a safe, people-free fallback.
    attempt_modes = [mode, mode, "device"]

    last_error = None
    for attempt, m in enumerate(attempt_modes, 1):
        try:
            result = client.images.generate(
                model=IMAGE_MODEL,
                prompt=build_hero_prompt(topic, m),
                size="1024x1536",
                quality=IMAGE_QUALITY,
                n=1,
            )
            return base64.b64decode(result.data[0].b64_json), m
        except Exception as e:
            last_error = e
            print(f"WARNING: hero generation attempt {attempt} (mode={m}) failed: {e}")
            if attempt < len(attempt_modes):
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


def ensure_target_folder(token, folder_name):
    root = _graph("GET", f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/root/children", token).json()
    project = _find_folder(root, ONEDRIVE_ROOT_PATH)
    if not project:
        raise Exception(f"Project folder not found: {ONEDRIVE_ROOT_PATH}")

    children_url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{project['id']}/children"
    children = _graph("GET", children_url, token).json()
    folder = _find_folder(children, folder_name)
    if folder:
        return folder["id"]

    created = _graph(
        "POST", children_url, token,
        json={"name": folder_name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
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


def pick_layout():
    # AD_LAYOUT can pin a layout ("campaign" = info version A, "hero" = big-image
    # version B, "both" = render both from one photo); anything else alternates
    # at random so the feed gets variety.
    layout = (os.getenv("AD_LAYOUT") or "").strip().lower()
    if layout in ("campaign", "hero", "both"):
        return layout
    return random.choice(["campaign", "hero"])


def build_and_upload(hero_bytes, topic, layout, mode, token, folder_id, target):
    ad = compose(hero_bytes, topic, layout)
    buffer = BytesIO()
    ad.save(buffer, format="JPEG", quality=92)
    ad_bytes = buffer.getvalue()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # A short random suffix keeps filenames unique even when several are produced
    # within the same second in one batch run. The "ai_" prefix marks the file as
    # auto-generated so cleanup only ever removes AI content, never user uploads.
    suffix = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
    filename = f"ai_{topic['key']}_{mode}_{layout}_{timestamp}_{suffix}.jpg"

    # Keep a local copy too, so CI can expose the render as an artifact for review.
    try:
        os.makedirs("out", exist_ok=True)
        with open(os.path.join("out", filename), "wb") as f:
            f.write(ad_bytes)
    except Exception as e:
        print(f"WARNING: could not write local copy: {e}")

    upload_draft(token, folder_id, filename, ad_bytes)
    print(f"Uploaded ({layout}, {len(ad_bytes)} bytes): {target}/{filename}")
    return filename


def generate_one(index, count, token, folder_id, target):
    topic = pick_topic()
    mode = pick_photo_mode(topic)
    if AD_FORMAT == "story":
        layouts = ["story"]
    else:
        layout = pick_layout()
        layouts = ["campaign", "hero"] if layout == "both" else [layout]
    print(f"\n[{index + 1}/{count}] topic={topic['key']} | format={AD_FORMAT} | layouts={layouts} | photo={mode}")

    hero_bytes, used_mode = generate_hero(topic, mode)
    print(f"  hero generated ({len(hero_bytes)} bytes, photo={used_mode})")
    return [build_and_upload(hero_bytes, topic, lay, used_mode, token, folder_id, target) for lay in layouts]


def main():
    try:
        validate_env()
        count = _int_env("AD_COUNT", 1)
        target = GENERATE_TARGET_FOLDER
        print(f"Generating {count} {AD_FORMAT} image(s) into '{target}'.")
        token = get_access_token()
        folder_id = ensure_target_folder(token, target)
    except Exception as e:
        print("\nERROR (setup):", str(e))
        sys.exit(1)

    # Each image is independent: a single moderation/API hiccup must not abandon
    # the rest of the batch, so per-item failures are caught and logged.
    made, failures = [], 0
    for i in range(count):
        try:
            made.extend(generate_one(i, count, token, folder_id, target))
        except Exception as e:
            failures += 1
            print(f"  [{i + 1}/{count}] FAILED (skipping): {e}")

    print(f"\nDone. Uploaded {len(made)} image(s) to '{target}'; {failures} item(s) failed.")
    # Succeed if at least one image was produced; only a total wipeout is a failure.
    sys.exit(0 if made else 1)


if __name__ == "__main__":
    main()
