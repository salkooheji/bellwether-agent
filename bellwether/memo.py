"""Memo traceability verification.

The project's grounding rule is structural: every figure in a memo must
have been produced by a tool during the same investigation. This module
enforces it by extracting every number from the memo and requiring each
one to exist in the union of the run's evidence, allowing percent
conversions (17.34% matches a stored 0.1734) and small rounding
differences. Evidence tags must reference ids that exist.

This is a strong guard, not a proof: it verifies existence of figures in
evidence, while attribution to the right piece of evidence rests on the
model's tags. Memos that fail are rejected with a list of untraceable
figures, which the loop feeds back to the model for one revision.
"""

from __future__ import annotations

import json
import re

TAG_RE = re.compile(r"\[(E\d+)\]")
NUM_RE = re.compile(r"(?<![A-Za-z0-9_])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
                    r"(?![A-Za-z0-9_])")
# Tokens whose digits are names, not figures, e.g. the form called 13F.
_NOISE = re.compile(r"13[\u2010-\u2015\-]?F", re.IGNORECASE)

# Ratio phrases like "50-for-1": the digits are phrasing, not figures.
# Keep the leading number, which is the actual ratio, drop the rest.
_RATIO = re.compile(
    r"(\d+(?:\.\d+)?)\s*[\u2010-\u2015\-]?\s*for\s*"
    r"[\u2010-\u2015\-]?\s*\d+(?:\.\d+)?",
    re.IGNORECASE,
)
# "top-5" is our own metric's name, not a figure.
_TOP5 = re.compile(r"top[\s\u2010-\u2015\-]*(?:5|five)", re.IGNORECASE)

_DASHES = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
})


def normalize_memo(memo: str) -> str:
    """Fold model typography into plain ASCII.

    Models emit full-width brackets around evidence tags and unicode
    dashes of several widths, including em dashes, en dashes, and
    non-breaking hyphens. Both are folded here so verification sees one
    form and memos read consistently wherever they are displayed.
    """
    return (memo.replace("\u3010", "[").replace("\u3011", "]")
            .translate(_DASHES))


def _numbers_in(text: str) -> list[float]:
    cleaned = _NOISE.sub(" ", text)
    cleaned = _RATIO.sub(r"\1", cleaned)
    cleaned = _TOP5.sub(" top-five ", cleaned)
    out = []
    for m in NUM_RE.finditer(cleaned):
        try:
            out.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return out


def _evidence_numbers(evidence: dict[str, dict]) -> list[float]:
    return _numbers_in(json.dumps(evidence))


def _traceable(value: float, pool: list[float]) -> bool:
    for cand in (value, value / 100.0, value * 100.0):
        tol = max(0.005, 0.005 * abs(cand))
        for e in pool:
            if abs(e - cand) <= tol:
                return True
    return False


def verify_memo(memo: str, evidence: dict[str, dict]) -> tuple[bool, list[str]]:
    """Check a memo against the evidence store.

    Returns (ok, problems). Problems are written for the model to act
    on, because rejected memos are sent back for revision.
    """
    problems: list[str] = []

    for tag in set(TAG_RE.findall(memo)):
        if tag not in evidence:
            problems.append(
                f"the tag [{tag}] references evidence that does not exist; "
                f"valid ids are: {', '.join(sorted(evidence)) or 'none'}"
            )

    pool = _evidence_numbers(evidence)
    for value in set(_numbers_in(memo)):
        if not _traceable(value, pool):
            problems.append(
                f"the figure {value:g} does not appear in any tool result "
                "from this investigation; remove it or replace it with a "
                "figure from the evidence"
            )

    return (len(problems) == 0), problems
