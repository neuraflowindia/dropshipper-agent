"""The credit gate in front of paid quick-commerce checks (Plan R10 W5, W12).

Before each batch (one source pass, known by its pass time) the provider's
free balance check runs once through the spend watch
(``spend_watch_quickcommerce.quickcommerce_gate``); an unknown balance fails
closed. Within the pass the gate is asked again only if a search response
or a provider refusal has since used the credits up. A refused gate is a
calm wait with a recheck time, never a failed check; a later positive
balance clears the route-wide "credits exhausted" wait (after a provider
"no credits" refusal, only a risen balance or a new pack). Each fresh search
response records its remaining count, and a provider "no credits" refusal
(HTTP 402) records the exhausted credits. Saved replies are never counted
twice and nothing here repeats a search.
"""

from datetime import timedelta

from .models import SafetyError, timestamp
from .spend_watch_quickcommerce import (credit_wait, quickcommerce_gate, record_credits_exhausted,
                                        record_response_remaining)

EXHAUSTED_RECHECK_SECONDS = 3600
CHECK_RETRY_SECONDS = 600


class CreditGate:
    def __init__(self, spend_root, client):
        self.spend_root, self.client = spend_root, client
        self._decision = None  # (pass time, wait or None)

    def admit(self, as_of):
        """None when paid checks may run in this pass, else a calm wait {reason, next_retry_at}."""
        timestamp(as_of)
        if self._decision and self._decision[0] == as_of:
            wait = self._decision[1]
            if wait is not None or credit_wait(self.spend_root) is None:
                return dict(wait) if wait else None
        gate = quickcommerce_gate(self.spend_root, self.client, as_of=as_of)
        wait = None
        if not gate["allowed"]:
            delay = EXHAUSTED_RECHECK_SECONDS if gate["available"] is not None else CHECK_RETRY_SECONDS
            wait = {"reason": gate["reason"],
                    "next_retry_at": (timestamp(as_of) + timedelta(seconds=delay)).isoformat()}
        self._decision = (as_of, wait)
        return dict(wait) if wait else None

    def record(self, receipt):
        """Count one fresh provider reply; a spend-record problem never undoes the saved reply."""
        try:
            if receipt.get("http_status") == 402:
                record_credits_exhausted(self.spend_root, as_of=receipt["observed_at"])
            elif type(receipt.get("credits_remaining")) is int:
                record_response_remaining(self.spend_root, receipt["credits_remaining"], as_of=receipt["observed_at"])
        except (SafetyError, OSError) as error:
            from .operations import record_error
            record_error("spend_watch_refresh_failed", error, stage="acquire", route="quickcommerce")
