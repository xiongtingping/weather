def test_a_refused_tx_peak_keeps_the_report_that_was_refused() -> None:
    """A TX filtered out by local-day attribution must stay diagnosable.

    A report that carried no TX and a report whose TX belongs to another
    local day both land as tx_available=0 with nothing retained, and after
    the fact they are indistinguishable -- the raw report is hashed, never
    stored.  Measured 2026-08-15: ZUUU had 2026-08-16 captures with an empty
    tx_c and no way to tell which case it was.  When the peak filter is what
    refused, the receipt keeps the report and the parser's own peaks.
    """

    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "copy_follow_official_taf.py"
    ).read_text(encoding="utf-8")
    body = source[source.index('"tx_peak_missing"'):]
    body = body[: body.index("peak = max(")]
    assert "raw_taf=raw_taf" in body
    assert "parsed_peaks=list(parsed.get(\"forecast_peaks\")" in body
    assert "requested_event_date=day" in body
    # The evidence is only kept on the refusal that needs it; a transport
    # failure has no report to keep and must not pretend otherwise.
    transport = source[: source.index('"tx_peak_missing"')]
    assert "raw_taf=raw_taf" not in transport
