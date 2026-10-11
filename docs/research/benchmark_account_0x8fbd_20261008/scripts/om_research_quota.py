"""Research calls to Open-Meteo go through the production quota ledger.

10-11 incident: this folder's single-runs fetches spent the egress address's
whole day (10,000 calls, a multi-model request counting several), and the
production relay lanes (IFS 9 km / UKMO / CMA, charter 201) and the direction
physics refresh (previous-runs, charter 266) were refused from 09:41Z to the
next UTC midnight.  Every research request now charges the shared ledger under
its own lane ("research", the ledger's default 500 a day), and a daily-limit
answer marks the day exhausted for everyone instead of retrying into it.
"""
import sys

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import open_meteo_quota as quota  # noqa: E402

LANE = "research"
QuotaRefused = quota.QuotaRefused


def charge(weight: int) -> None:
    """Account the call before making it; raises QuotaRefused when research's share or the day is spent."""
    quota.charge(LANE, max(1, int(weight)))


def on_reply(payload) -> str:
    """Classify an Open-Meteo error answer; a daily-limit answer marks the shared ledger exhausted."""
    if isinstance(payload, dict) and payload.get("error"):
        kind = quota.classify(str(payload.get("reason")))
        if kind == "day":
            quota.mark_exhausted()
        return kind
    return ""
