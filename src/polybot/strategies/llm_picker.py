"""
LLM-driven opportunity picker.

The deterministic strategies (weather, crypto, sports) produce a list of
Opportunity candidates each scan. They already pass hard pre-filters:
min edge, min liquidity, time-to-close, structural sanity.

This node asks an LLM to look across those survivors and pick which ones
to actually open — applying intuition the rule engine can't:
  • "two of these are the same underlying event, only take the cheaper leg"
  • "this market's question is ambiguous, skip"
  • "edge is real but the category has been losing all week, pass"

The LLM is constrained:
  • cannot invent opportunities
  • can only PICK or SKIP from the provided list
  • must give a 3-4 sentence rationale per pick
  • must respect hard rules listed in the system prompt

Rationales are appended to data/trades/llm_decisions.jsonl for later review.

Backend: any OpenAI-compatible endpoint (Together AI / Writer / OpenAI /
local vLLM / llama.cpp server). Configured via settings.llm_base_url and
settings.llm_model. Default: Together AI hosting Palmyra-Fin-70B-32K.

If the LLM is disabled, missing credentials, or fails for any reason, the
picker falls back to passing all opportunities through unchanged — the bot
stays deterministic and operational.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from polybot.config import settings
from polybot.models import Opportunity


# ─── Output schema ────────────────────────────────────────────────────────────

class Pick(BaseModel):
    opportunity_id: str
    action: str         # "TAKE" or "SKIP"
    rationale: str


class PickerOutput(BaseModel):
    picks: list[Pick] = Field(default_factory=list)


# ─── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the final filter for a Polymarket prediction-market trading bot.

The deterministic strategy engine has already produced a list of candidate
opportunities. Each one has passed minimum edge, liquidity, and time-to-close
checks. Your job is to look at the WHOLE batch and decide which ones the bot
should actually open this scan.

HARD RULES (never break these):
  1. You may only TAKE or SKIP opportunities from the provided list.
     You cannot invent new ones, change prices, or modify sides.
  2. If two opportunities cover the same underlying event, prefer the one
     with the larger edge or cheaper entry price; SKIP the other.
  3. Skip any opportunity whose question is ambiguous, badly worded,
     or where the model probability looks suspicious vs the market price.
  4. When in doubt, SKIP. A missed trade costs nothing; a bad trade costs money.

OUTPUT FORMAT:
Return ONLY a JSON object, no prose before or after. Schema:

{
  "picks": [
    {
      "opportunity_id": "<exact id from input>",
      "action": "TAKE" | "SKIP",
      "rationale": "3-4 sentences. Be specific. Reference the question, the edge, and what decided it. No filler."
    },
    ...
  ]
}

Include one Pick per input opportunity. Tone: terse, informed, like a desk
trader explaining to a junior."""


def _format_opps_for_llm(opps: list[Opportunity]) -> str:
    lines = []
    for o in opps:
        lines.append(
            f"- id={o.id} | {o.market.question[:120]}\n"
            f"   side={o.side} market_price={o.market_price:.3f} "
            f"model_prob={o.model_probability:.3f} edge={o.edge_pct} "
            f"strategy={o.strategy} liquidity=${o.market.liquidity_usd:,.0f} "
            f"hours_left={o.market.hours_until_close:.1f}"
        )
    return "\n".join(lines)


# ─── JSON parsing (robust to model quirks) ────────────────────────────────────

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_picker_output(raw: str) -> PickerOutput | None:
    """
    Try to extract a PickerOutput from a possibly-noisy LLM response.

    Strategy:
      1. Strip markdown fences.
      2. Try direct json.loads.
      3. Fall back to the first {...} block in the text.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Drop leading fence + optional language tag, then trailing fence
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    for candidate in (text, _first_json_object(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        try:
            return PickerOutput.model_validate(data)
        except ValidationError as e:
            logger.warning(f"Picker output failed validation: {e}")
    return None


def _first_json_object(text: str) -> str | None:
    m = _JSON_OBJ_RE.search(text)
    return m.group(0) if m else None


# ─── Logging ──────────────────────────────────────────────────────────────────

_DECISIONS_PATH = Path("data") / "trades" / "llm_decisions.jsonl"


def _log_decision(scan_number: int, opp: Opportunity, pick: Pick) -> None:
    _DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "scan_number":   scan_number,
        "opportunity_id": opp.id,
        "market_id":     opp.market.id,
        "question":      opp.market.question,
        "side":          str(opp.side),
        "market_price":  opp.market_price,
        "model_prob":    opp.model_probability,
        "edge":          opp.edge,
        "strategy":      opp.strategy,
        "model":         settings.llm_model,
        "action":        pick.action,
        "rationale":     pick.rationale,
    }
    with _DECISIONS_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


# ─── Main entry point ─────────────────────────────────────────────────────────

async def pick_opportunities(
    opportunities: list[Opportunity],
    scan_number:   int = 0,
) -> list[Opportunity]:
    """
    Filter opportunities through the LLM picker.

    Returns the subset the LLM voted TAKE on. On any failure (no key,
    network error, parse error) returns the input list unchanged so the
    deterministic engine keeps running.
    """
    if not opportunities:
        return []

    if not settings.llm_picker_enabled:
        return opportunities

    if not settings.llm_api_key:
        logger.warning("LLM picker enabled but llm_api_key not set — passing through")
        return opportunities

    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.warning("langchain-openai not installed — passing through")
        return opportunities

    try:
        llm = ChatOpenAI(
            model       = settings.llm_model,
            api_key     = settings.llm_api_key,
            base_url    = settings.llm_base_url,
            temperature = 0.2,
            max_tokens  = 1500,
            timeout     = 30.0,
        )

        user_msg = (
            f"Scan #{scan_number}. {len(opportunities)} candidate opportunities:\n\n"
            f"{_format_opps_for_llm(opportunities)}\n\n"
            f"Return one Pick per opportunity as JSON only — no prose."
        )

        response = await llm.ainvoke([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ])

        raw = response.content if hasattr(response, "content") else str(response)
        if isinstance(raw, list):
            # Some providers return content as a list of parts
            raw = "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                          for part in raw)

        parsed = _parse_picker_output(raw)
        if parsed is None:
            logger.warning(
                f"LLM picker could not parse response — passing through. "
                f"raw[:200]={raw[:200]!r}"
            )
            return opportunities

        by_id = {o.id: o for o in opportunities}

        taken: list[Opportunity] = []
        skipped_count = 0
        for pick in parsed.picks:
            opp = by_id.get(pick.opportunity_id)
            if opp is None:
                logger.warning(f"LLM picked unknown opportunity_id={pick.opportunity_id}")
                continue
            _log_decision(scan_number, opp, pick)
            if pick.action.upper() == "TAKE":
                taken.append(opp)
                logger.info(f"🤖 TAKE {opp.market.question[:50]} — {pick.rationale[:120]}")
            else:
                skipped_count += 1
                logger.info(f"🤖 SKIP {opp.market.question[:50]} — {pick.rationale[:120]}")

        # Any opportunity the LLM forgot about: log a warning (defaults to SKIP)
        covered_ids = {p.opportunity_id for p in parsed.picks}
        for opp in opportunities:
            if opp.id not in covered_ids:
                logger.warning(f"LLM omitted opp {opp.id} — defaulting to SKIP")

        logger.info(
            f"LLM picker [{settings.llm_model}]: {len(taken)} taken, "
            f"{skipped_count} skipped of {len(opportunities)} candidates"
        )
        return taken

    except Exception as exc:
        logger.warning(f"LLM picker failed ({exc}) — passing all candidates through")
        return opportunities
