"""Tests for memo traceability verification.

The verifier is the guarantee behind every number in a memo, so its two
failure modes both matter: letting an invented figure through, and
rejecting a legitimate one. Several of these cases come from phrasing
that real model output actually produced.
"""

from bellwether.memo import normalize_memo, verify_memo

EVIDENCE = {
    "E1": {"ok": True, "shares": 743984, "value_usd": 7335000,
           "weight": 0.1734, "splits_in_window": {"2024-06-26": 50.0}},
    "E2": {"ok": True, "shares": 28815165, "pct_change": 8.9},
}


def test_traceable_figures_pass():
    memo = "Held 743,984 shares [E1] worth $7,335,000 [E1]."
    ok, problems = verify_memo(memo, EVIDENCE)
    assert ok, problems


def test_invented_figure_is_rejected():
    memo = "Held 999,999 shares [E1]."
    ok, problems = verify_memo(memo, EVIDENCE)
    assert not ok
    assert any("999999" in p for p in problems)


def test_unknown_evidence_tag_is_rejected():
    memo = "Held 743,984 shares [E9]."
    ok, problems = verify_memo(memo, EVIDENCE)
    assert not ok
    assert any("E9" in p for p in problems)


def test_percent_conversion_is_accepted():
    """17.34 percent in prose matches a stored weight of 0.1734."""
    ok, problems = verify_memo("The position was 17.34% of the book [E1].",
                               EVIDENCE)
    assert ok, problems


def test_ratio_phrasing_is_not_treated_as_a_figure():
    """The 1 in '50-for-1' is phrasing, not a figure to verify."""
    ok, problems = verify_memo("A 50-for-1 split on 2024-06-26 [E1].",
                               EVIDENCE)
    assert ok, problems


def test_top_five_label_is_not_treated_as_a_figure():
    ok, problems = verify_memo("Top-5 concentration rose to 17.34% [E1].",
                               EVIDENCE)
    assert ok, problems


def test_form_name_is_not_treated_as_a_figure():
    ok, problems = verify_memo("Per the 13F filing, 743,984 shares [E1].",
                               EVIDENCE)
    assert ok, problems


def test_inconclusive_memo_with_no_figures_passes():
    ok, problems = verify_memo(
        "LIKELY EXPLANATION: The cause could not be established.", {})
    assert ok, problems


def test_spaced_and_grouped_tags_are_recognised():
    memo = "Shares 743,984 [ E1 ] and 28,815,165 [E1, E2]."
    ok, problems = verify_memo(memo, EVIDENCE)
    assert ok, problems

    bad = "Shares 743,984 [ E1 , E7 ]."
    ok, problems = verify_memo(bad, EVIDENCE)
    assert not ok
    assert any("E7" in p for p in problems)


def test_normalisation_folds_model_typography():
    """Models emit full-width brackets and unicode dashes."""
    raw = "high \u2013 a 50\u2011for\u20111 split \u3010E1\u3011"
    clean = normalize_memo(raw)
    assert "[E1]" in clean
    assert "\u2013" not in clean and "\u2011" not in clean
    assert "-" in clean
