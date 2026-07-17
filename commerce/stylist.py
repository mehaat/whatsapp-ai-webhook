"""
commerce/stylist.py
--------------------
The **AI Stylist** knowledge base for ME-HAAT Fashion AI Bot v9.0.

A small, deterministic, offline styling engine for Indian ethnic wear. It knows
how to "complete the look" around an anchor garment (saree, lehenga, kurta …),
what to wear for a given occasion (wedding, reception, festive …), and how to
pair colours. When a Gemini key is configured, :func:`style_note` can optionally
enrich its templated tip; otherwise a hand-written template is used.

Design contract: **no public function ever raises.** Unknown inputs fall back to
sensible generic advice.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import config
from utils.logging import logger

# --------------------------------------------------------------------------
# Knowledge base
# --------------------------------------------------------------------------

# Anchor garment -> the pieces that complete the look.
_LOOK_RULES: Dict[str, Dict[str, Any]] = {
    "saree": {
        "suggestions": [
            {"category": "blouse", "reason": "A matching or contrast blouse defines the drape."},
            {"category": "petticoat", "reason": "A colour-matched petticoat keeps the pleats clean."},
            {"category": "statement jewellery", "reason": "Jhumkas and a choker elevate the silhouette."},
            {"category": "potli / clutch", "reason": "A potli or embellished clutch finishes the ensemble."},
            {"category": "heels", "reason": "Heels add height and let the pallu fall gracefully."},
        ],
        "colors": ["gold", "maroon", "emerald", "royal blue"],
        "note": "Anchor the saree with a contrast blouse and let one statement jewellery piece lead.",
    },
    "lehenga": {
        "suggestions": [
            {"category": "blouse", "reason": "A fitted choli balances the volume of the skirt."},
            {"category": "dupatta draping", "reason": "A double-dupatta drape adds a regal, layered look."},
            {"category": "jhumkas", "reason": "Oversized jhumkas frame the face against an open neckline."},
            {"category": "kaleeras / bangles", "reason": "Stacked bangles complete a bridal-festive feel."},
            {"category": "juttis or heels", "reason": "Juttis for comfort, heels for a taller silhouette."},
        ],
        "colors": ["red", "pink", "gold", "wine"],
        "note": "Let the lehenga be the hero; keep the blouse tonal and the dupatta drape structured.",
    },
    "kurta": {
        "suggestions": [
            {"category": "palazzo / leggings", "reason": "Palazzos read festive; leggings keep it daily-easy."},
            {"category": "dupatta", "reason": "A light dupatta instantly dresses a plain kurta up."},
            {"category": "juttis", "reason": "Embroidered juttis add ethnic charm without heels."},
            {"category": "oxidised jewellery", "reason": "Oxidised silver pairs beautifully with cotton kurtas."},
        ],
        "colors": ["indigo", "mustard", "white", "teal"],
        "note": "For a kurta, pick one accent — dupatta or juttis — and keep the rest understated.",
    },
    "gown": {
        "suggestions": [
            {"category": "earrings", "reason": "Statement earrings suit a covered neckline."},
            {"category": "clutch", "reason": "A slim clutch keeps a gown look sleek."},
            {"category": "heels", "reason": "Heels lengthen the line of a floor-length gown."},
        ],
        "colors": ["black", "wine", "emerald", "navy"],
        "note": "Keep gown accessories minimal — one bold piece, everything else quiet.",
    },
    "suit": {
        "suggestions": [
            {"category": "dupatta", "reason": "A contrast dupatta lifts a solid suit."},
            {"category": "juttis", "reason": "Juttis keep an Anarkali or straight suit grounded."},
            {"category": "jhumkas", "reason": "Jhumkas add festive polish to a salwar suit."},
        ],
        "colors": ["peach", "powder blue", "rani pink", "sage"],
        "note": "A well-chosen dupatta does most of the styling work for a suit.",
    },
}

# Occasion -> recommended categories, fabrics, colours and a note.
_OCCASION_RULES: Dict[str, Dict[str, Any]] = {
    "wedding": {
        "categories": ["lehenga", "heavy saree", "statement jewellery", "potli"],
        "fabrics": ["banarasi", "silk", "velvet", "organza"],
        "colors": ["red", "maroon", "gold", "wine"],
        "note": "Go for rich fabrics and heavier embellishment; jewel tones photograph best.",
    },
    "reception": {
        "categories": ["gown", "designer saree", "cocktail lehenga", "heels"],
        "fabrics": ["georgette", "satin", "silk", "net"],
        "colors": ["wine", "emerald", "navy", "gold"],
        "note": "Reception looks can be sleeker and more contemporary than the wedding day.",
    },
    "party": {
        "categories": ["gown", "indo-western", "cocktail saree", "clutch"],
        "fabrics": ["georgette", "chiffon", "satin"],
        "colors": ["black", "royal blue", "wine", "silver"],
        "note": "Keep it fun and modern — one bold colour with statement accessories.",
    },
    "festive": {
        "categories": ["saree", "lehenga", "anarkali suit", "juttis"],
        "fabrics": ["silk", "chanderi", "cotton silk", "organza"],
        "colors": ["mustard", "orange", "pink", "green"],
        "note": "Bright, warm tones suit festivals; mix traditional cuts with playful colour.",
    },
    "casual": {
        "categories": ["kurta", "cotton saree", "kurti set", "flats"],
        "fabrics": ["cotton", "linen", "chanderi"],
        "colors": ["white", "indigo", "pastel", "beige"],
        "note": "Breathable fabrics and easy silhouettes; keep jewellery oxidised and minimal.",
    },
    "office": {
        "categories": ["cotton kurta", "formal saree", "straight suit", "flats"],
        "fabrics": ["cotton", "linen", "chanderi", "silk blend"],
        "colors": ["navy", "grey", "beige", "muted green"],
        "note": "Structured, understated ethnic wear reads professional; skip heavy embellishment.",
    },
}

# Simple complementary colour pairings for quick suggestions.
_COLOR_PAIRS: Dict[str, List[str]] = {
    "red": ["gold", "cream", "green"],
    "maroon": ["gold", "beige", "mustard"],
    "blue": ["silver", "white", "peach"],
    "navy": ["gold", "coral", "cream"],
    "green": ["gold", "red", "cream"],
    "pink": ["gold", "green", "grey"],
    "yellow": ["red", "green", "navy"],
    "black": ["gold", "silver", "red"],
    "white": ["red", "gold", "navy"],
    "gold": ["maroon", "green", "royal blue"],
    "mustard": ["maroon", "teal", "brown"],
    "teal": ["gold", "coral", "cream"],
    "purple": ["gold", "silver", "mustard"],
    "orange": ["green", "pink", "blue"],
}

_DEFAULT_LOOK = {
    "suggestions": [
        {"category": "matching bottoms", "reason": "Ground the outfit with a tonal base."},
        {"category": "dupatta / drape", "reason": "A drape adds an ethnic finishing layer."},
        {"category": "jewellery", "reason": "One statement piece pulls the look together."},
        {"category": "footwear", "reason": "Juttis or heels complete the silhouette."},
    ],
    "colors": ["gold", "maroon", "cream"],
    "note": "Pick one hero piece and keep the supporting elements tonal.",
}


def _normalize(value: Optional[str]) -> str:
    """Lower-case and trim a possibly-``None`` string."""
    return (value or "").strip().lower()


def _resolve_look_key(product_type: str) -> Optional[str]:
    """Map a free-form product type onto a known anchor key."""
    if not product_type:
        return None
    for key in _LOOK_RULES:
        if key in product_type:
            return key
    # Handle common plural/variant spellings.
    aliases = {
        "sarees": "saree", "sari": "saree", "saris": "saree",
        "lehengas": "lehenga", "choli": "lehenga",
        "kurti": "kurta", "kurtis": "kurta", "kurtas": "kurta",
        "anarkali": "suit", "salwar": "suit", "suits": "suit",
        "gowns": "gown", "dress": "gown",
    }
    for alias, key in aliases.items():
        if alias in product_type:
            return key
    return None


def complete_the_look(
    *,
    product_type: Optional[str] = None,
    color: Optional[str] = None,
    occasion: Optional[str] = None,
) -> Dict[str, Any]:
    """Suggest complementary pieces that complete the look around an anchor.

    Args:
        product_type: The anchor garment (e.g. ``"saree"``, ``"lehenga"``).
        color: The anchor garment's colour, used for pairing suggestions.
        occasion: Optional occasion, used to enrich the styling note.

    Returns:
        ``{"anchor", "suggestions": [{"category", "reason"} ...], "colors",
        "note"}``. Always populated, even for unknown inputs.
    """
    try:
        ptype = _normalize(product_type)
        key = _resolve_look_key(ptype)
        rule = _LOOK_RULES.get(key) if key else None
        anchor = key or (ptype or "outfit")

        if rule is not None:
            suggestions = [dict(item) for item in rule["suggestions"]]
            colors = list(rule["colors"])
            note = rule["note"]
        else:
            suggestions = [dict(item) for item in _DEFAULT_LOOK["suggestions"]]
            colors = list(_DEFAULT_LOOK["colors"])
            note = _DEFAULT_LOOK["note"]

        # Colour pairing suggestions based on the anchor colour.
        anchor_color = _normalize(color)
        if anchor_color and anchor_color in _COLOR_PAIRS:
            pairs = _COLOR_PAIRS[anchor_color]
            colors = pairs + [c for c in colors if c not in pairs]
            note = (
                f"{note} With a {anchor_color} anchor, "
                f"pair with {', '.join(pairs)}."
            )

        occ = _normalize(occasion)
        if occ and occ in _OCCASION_RULES:
            note = f"{note} For a {occ} look, {_OCCASION_RULES[occ]['note'].lower()}"

        return {
            "anchor": anchor,
            "suggestions": suggestions,
            "colors": colors,
            "note": note,
        }
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        logger.error("STYLIST | complete_the_look failed: %s", exc)
        return {
            "anchor": _normalize(product_type) or "outfit",
            "suggestions": [dict(item) for item in _DEFAULT_LOOK["suggestions"]],
            "colors": list(_DEFAULT_LOOK["colors"]),
            "note": _DEFAULT_LOOK["note"],
        }


def suggest_for_occasion(occasion: str) -> Dict[str, Any]:
    """Recommend categories, fabrics, colours and a note for an occasion.

    Args:
        occasion: The occasion (e.g. ``"wedding"``, ``"office"``). Aliases such
            as ``"partywear"`` / ``"bridal"`` are recognised.

    Returns:
        ``{"occasion", "categories", "fabrics", "colors", "note"}``. Falls back
        to generic festive advice for unknown occasions.
    """
    try:
        occ = _normalize(occasion)
        aliases = {
            "partywear": "party", "cocktail": "party",
            "bridal": "wedding", "shaadi": "wedding", "engagement": "wedding",
            "festival": "festive", "diwali": "festive", "puja": "festive",
            "daily": "casual", "everyday": "casual",
            "work": "office", "formal": "office",
        }
        occ = aliases.get(occ, occ)
        rule = _OCCASION_RULES.get(occ)
        if rule is None:
            rule = _OCCASION_RULES["festive"]
            occ = occ or "festive"
        return {
            "occasion": occ,
            "categories": list(rule["categories"]),
            "fabrics": list(rule["fabrics"]),
            "colors": list(rule["colors"]),
            "note": rule["note"],
        }
    except Exception as exc:  # noqa: BLE001 - never raise to the caller
        logger.error("STYLIST | suggest_for_occasion failed: %s", exc)
        rule = _OCCASION_RULES["festive"]
        return {
            "occasion": "festive",
            "categories": list(rule["categories"]),
            "fabrics": list(rule["fabrics"]),
            "colors": list(rule["colors"]),
            "note": rule["note"],
        }


def style_note(
    *,
    product_name: Optional[str] = None,
    occasion: Optional[str] = None,
) -> str:
    """Return a short, WhatsApp-friendly styling tip.

    Uses a deterministic template. When ``config.ai_stylist_enabled`` and a
    Gemini key are configured, a best-effort Gemini enrichment is attempted; any
    failure falls back to the template. Never raises.

    Args:
        product_name: The product being styled (optional).
        occasion: The occasion the customer is shopping for (optional).

    Returns:
        A non-empty styling tip string.
    """
    template = _templated_note(product_name=product_name, occasion=occasion)
    try:
        if config.ai_stylist_enabled and config.gemini_api_key:
            enriched = _gemini_style_note(product_name=product_name, occasion=occasion)
            if enriched:
                return enriched
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort only
        logger.debug("STYLIST | gemini style note skipped: %s", exc)
    return template


def _templated_note(
    *,
    product_name: Optional[str] = None,
    occasion: Optional[str] = None,
) -> str:
    """Build a deterministic styling tip from the knowledge base."""
    name = (product_name or "this piece").strip() or "this piece"
    occ = _normalize(occasion)
    if occ:
        rule = suggest_for_occasion(occ)
        colors = ", ".join(rule["colors"][:2]) if rule["colors"] else "jewel tones"
        return (
            f"Style {name} for a {rule['occasion']} look: {rule['note']} "
            f"Lean into {colors} and keep accessories intentional."
        )
    key = _resolve_look_key(name.lower())
    if key and key in _LOOK_RULES:
        return f"Styling {name}: {_LOOK_RULES[key]['note']}"
    return (
        f"Styling {name}: choose one statement piece, keep the rest tonal, "
        "and match your footwear to the drape."
    )


def _gemini_style_note(
    *,
    product_name: Optional[str] = None,
    occasion: Optional[str] = None,
) -> Optional[str]:
    """Best-effort one-line Gemini styling tip (guarded; ``None`` on failure)."""
    if not config.gemini_api_key:
        return None
    try:
        import requests

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.gemini_model}:generateContent"
        )
        prompt = (
            "You are an Indian ethnic-wear stylist. In one short, friendly "
            "sentence, give a styling tip"
        )
        if product_name:
            prompt += f" for '{product_name}'"
        if occasion:
            prompt += f" suitable for a {occasion} occasion"
        prompt += ". Keep it under 30 words."
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 80},
        }
        resp = requests.post(
            url,
            params={"key": config.gemini_api_key},
            json=body,
            timeout=getattr(config, "request_timeout_seconds", 15),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - never raise; caller falls back
        logger.debug("STYLIST | gemini style note failed: %s", exc)
        return None
