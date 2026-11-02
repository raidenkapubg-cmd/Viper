"""Instruction ingestion: plain text, PDF, and chart screenshots.

* Text -> ``rules.extract_rules``.
* PDF  -> pypdf text extraction -> same parser. Scanned (image-only) PDFs fall
  back to Claude document understanding when ANTHROPIC_API_KEY is set.
* Image -> Claude vision (when ANTHROPIC_API_KEY is set) returns rules in the same
  schema plus annotated setups; otherwise pytesseract OCR (if installed) feeds the
  text parser; otherwise the image is stored with a "needs review" note.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import time

from viper.config import settings
from viper.learn.rules import extract_rules, parse_sentence
from viper.store.db import DB

log = logging.getLogger("viper.ingest")

RULE_KINDS = [
    "time_block", "open_buffer", "close_buffer", "direction_only", "direction_ban", "indicator_ban",
    "vwap_side", "regime_ban", "strategy_ban", "vix_max", "vix_min", "max_trades_day", "max_hold",
    "stop_pct", "min_confidence", "note",
]

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "One plain-English trading rule, phrased so a keyword parser can read it"},
                    "kind": {"type": "string", "enum": RULE_KINDS},
                },
                "required": ["text", "kind"],
                "additionalProperties": False,
            },
        },
        "setups": {"type": "array", "items": {"type": "string"}},
        "annotations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["rules", "setups", "annotations"],
    "additionalProperties": False,
}

PROMPT = (
    "You are reading material an Indian index-options trader uploaded to teach their trading system "
    "(NIFTY 50, NIFTY NEXT 50, SENSEX, BANKNIFTY; the system only buys CE/PE options intraday). "
    "Extract every actionable rule. Write each rule as one short English sentence using concrete numbers "
    "and these phrasings where they apply: 'no trades between HH:MM and HH:MM', 'avoid first N minutes', "
    "'avoid last N minutes', 'only buy calls', 'no puts', 'no calls when RSI above N', 'no trades when VIX above N', "
    "'only buy calls above VWAP', 'avoid chop' / 'avoid expiry day', 'disable breakout' (strategies: momentum, "
    "imbalance, breakout, mean reversion), 'max N trades per day', 'max hold N minutes', 'stop loss N%'. "
    "Pick the matching kind; use 'note' for anything that does not fit. Under setups, describe any price-action "
    "setups drawn or described (levels, patterns, entries/exits). Under annotations, transcribe handwritten or "
    "on-chart text. Return only what the material supports."
)


def _claude_extract(content_block: dict) -> dict | None:
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed; vision extraction unavailable")
        return None
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=settings.vision_model,
            max_tokens=16000,
            messages=[{"role": "user", "content": [content_block, {"type": "text", "text": PROMPT}]}],
            output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
        )
    except anthropic.APIStatusError as e:
        log.warning("Claude extraction failed: HTTP %s %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError as e:
        log.warning("Claude extraction unreachable: %s", e)
        return None
    if resp.stop_reason == "refusal":
        log.warning("Claude declined the extraction request")
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        return None


def _ocr(image_bytes: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        return pytesseract.image_to_string(Image.open(io.BytesIO(image_bytes)))
    except Exception as e:  # tesseract binary missing etc.
        log.info("OCR unavailable: %s", e)
        return ""


def ingest(db: DB, scope: str, method: str, text: str = "", filename: str | None = None,
           blob: bytes | None = None, media_type: str = "") -> dict:
    extracted: dict = {"rules": [], "setups": [], "annotations": [], "engine": method}
    raw_text = text or ""
    if method == "pdf" and blob:
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(blob))
            raw_text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:
            raw_text = ""
            extracted["error"] = f"PDF parse failed: {e}"
        if len(raw_text.strip()) < 20:
            got = _claude_extract({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                                   "data": base64.standard_b64encode(blob).decode()}})
            if got:
                extracted.update(got, engine="claude-document")
    elif method == "image" and blob:
        got = _claude_extract({"type": "image", "source": {"type": "base64", "media_type": media_type or "image/png",
                                                            "data": base64.standard_b64encode(blob).decode()}})
        if got:
            extracted.update(got, engine="claude-vision")
        else:
            raw_text = _ocr(blob)
            extracted["engine"] = "ocr" if raw_text else "stored"
            if not raw_text:
                extracted["annotations"].append(
                    "Image stored. No vision/OCR engine available: set ANTHROPIC_API_KEY or install tesseract to extract rules."
                )

    rules: list[dict] = []
    if extracted.get("rules"):
        # Re-parse Claude's sentences through the deterministic parser so params are ours, not free-form.
        for r in extracted["rules"]:
            parsed = parse_sentence(r["text"])
            rules.append(parsed)
    if raw_text:
        rules.extend(extract_rules(raw_text))

    status = "parsed" if rules else ("stored" if blob else "empty")
    iid = db.insert("instructions", {
        "created": time.time(), "scope": scope, "method": method, "filename": filename,
        "text": raw_text[:20000], "extracted": json.dumps(extracted), "status": status,
    })
    saved = []
    for r in rules:
        rid = db.insert("rules", {
            "created": time.time(), "scope": scope, "kind": r["kind"], "params": json.dumps(r["params"]),
            "text": r["text"][:500], "source": f"{method}:{filename or 'text'}",
            "enforced": 0 if r["kind"] == "note" else 1, "enabled": 1, "instruction_id": iid,
        })
        saved.append({**r, "id": rid, "enforced": r["kind"] != "note"})
    return {"instruction_id": iid, "status": status, "rules": saved,
            "setups": extracted.get("setups", []), "annotations": extracted.get("annotations", []),
            "engine": extracted.get("engine"), "error": extracted.get("error")}
