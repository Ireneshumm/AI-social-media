"""Australian advertising-compliance guardrails for AI-generated content.

Advertising prescription-only cosmetic injectables to the public is prohibited
in Australia. The Therapeutic Goods Act bans advertising of Schedule 4
(prescription-only) medicines, which covers botulinum toxin ("anti-wrinkle"
injections) and dermal fillers, and the TGA has extended its compliance focus
to unapproved peptide products. This applies to DIRECT and INDIRECT promotion.

Every AI text/caption prompt in this project includes COMPLIANCE_RULES so the
model never writes copy that promotes or hints at these treatments. A final
scrub (scrub_caption) removes any banned wording that slips through.
"""
import re

# Injected verbatim into every caption/copy generation prompt.
COMPLIANCE_RULES = """
AUSTRALIAN ADVERTISING COMPLIANCE (TGA) — MANDATORY, applies to every single word:
- Do NOT advertise, name, reference, promote, or imply any prescription-only
  cosmetic injectable, either directly OR indirectly. This includes:
  * botulinum toxin / "anti-wrinkle" / "wrinkle relaxing" injections and any
    brand names (Botox, Dysport, Xeomin, etc.);
  * dermal fillers of any kind (lip filler, cheek filler, hyaluronic acid
    fillers, Juvederm, Restylane, etc.);
  * injectable "skin boosters", bio-remodelling (e.g. Profhilo), and any
    unapproved peptide products or peptide therapy.
- Do NOT use words or phrases such as: "anti-wrinkle", "wrinkle injections",
  "tox", "baby botox", "filler", "lip filler", "injectable", "injector",
  "units", "skin booster", "bio-remodel", "peptide", or any before/after or
  results language tied to these treatments.
- If the image or filename appears to show an injectable, filler, or peptide
  treatment, write a GENERIC caption about skin health, confidence, self-care,
  or the clinic experience WITHOUT naming or promoting the treatment.
- Only write about permitted content: general skincare, facials, laser and skin
  treatments that are not prescription-only, wellbeing, and clinic atmosphere.
""".strip()

# Case-insensitive patterns removed from finished captions as a safety net.
_BANNED_PATTERNS = [
    r"anti[\s-]*wrinkle",
    r"wrinkle[\s-]*(?:relax\w*|injection\w*|treatment\w*)",
    r"\bbotox\b",
    r"\bdysport\b",
    r"\bxeomin\b",
    r"\bbotulinum\b",
    r"baby\s*botox",
    r"\btox\b",
    r"dermal\s*filler\w*",
    r"\bfillers?\b",
    r"lip\s*filler\w*",
    r"\binjectable\w*",
    r"\binjector\w*",
    r"\binjection\w*",
    r"skin\s*booster\w*",
    r"bio[\s-]*remodel\w*",
    r"\bprofhilo\b",
    r"\bpeptide\w*",
]

_BANNED_RE = re.compile("|".join(_BANNED_PATTERNS), re.IGNORECASE)


def contains_banned_terms(text):
    """True if the text references a prohibited injectable/peptide treatment."""
    if not text:
        return False
    return bool(_BANNED_RE.search(text))


def scrub_caption(text):
    """Last-resort safety net: strip banned wording from a finished caption and
    tidy the whitespace/punctuation left behind. Prompts should prevent this
    from ever triggering, but a public medical-advertising breach is costly, so
    we never let banned terms reach a published post."""
    if not text:
        return text
    cleaned = _BANNED_RE.sub("", text)
    # Collapse artefacts left by the removal (double spaces, stranded
    # punctuation, blank lines).
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,.!?;:])\1+", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines())
    return cleaned.strip()
