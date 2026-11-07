import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time

ACCOUNT_CHECK_TTL = 300
ACCOUNT_SOURCE_TTL = 3600

POSITION_FIELDS = tuple("proxy_wallet token_id condition_id opposite_token_id outcome_index outcome title slug event_slug current_size current_price current_value avg_price entry_cost_usdc unrealized_pnl realized_pnl status redeemable first_entry_at last_event_at verified_at verification_pending missing balance_confirmed hedge_complete last_change change_at end_date opposite_size opposite_verified_at".split())


def compact_position(row):
    return {key: row[key] for key in POSITION_FIELDS if key in row}


def compact_market(row):
    keys = ("conditionId", "active", "closed", "archived", "acceptingOrders", "enableOrderBook", "umaResolutionStatus",
            "endDate", "checked_at", "category", "version", "clobTokenIds", "positionIds", "feeSchedule", "feesEnabled")
    result = {key: row[key] for key in keys if key in row}
    if not result.get("category"):
        tags = row.get("tags") or row.get("categories") or []
        result["category"] = next((t.get("label") or t.get("name") for t in tags if isinstance(t, dict) and (t.get("label") or t.get("name"))), None)
        if not result["category"] and row.get("sportsMarketType"):
            result["category"] = "Sports"
        result["category"] = result["category"] or "Other"
    return result


def account_status(profile, now=None):
    now = time.time() if now is None else now
    if profile.get("disqualified"):
        return "excluded"
    pnl = profile.get("account_pnl")
    if pnl is None or not profile.get("pnl_checked_at") or not profile.get("pnl_observed_at"):
        return "pending"
    if now - profile["pnl_checked_at"] > ACCOUNT_CHECK_TTL or now - profile["pnl_observed_at"] > ACCOUNT_SOURCE_TTL:
        return "pending"
    return "eligible" if pnl >= 0 else "excluded"


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite API amount")
    return result


def number(value):
    return None if value is None else float(decimal(value))


@dataclass(frozen=True)
class Trade:
    key: str
    wallet: str
    timestamp: int
    amount: float
    raw: dict

    @classmethod
    def parse(cls, raw):
        wallet = str(raw["proxy_wallet"]).lower()
        size, price = decimal(raw["size"]), decimal(raw["price"])
        if size < 0 or not 0 <= price <= 1 or raw["side"] not in ("BUY", "SELL"):
            raise ValueError("Invalid trade")
        # The public feed exposes no log index/fill id. Preserve distinct wallets,
        # outcomes, sides, sizes and prices within a settling transaction.
        identity = [raw["transaction_hash"], wallet, raw["token_id"], raw["side"],
                    str(size.normalize()), str(price.normalize()), int(raw["timestamp"])]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return cls(key, wallet, int(raw["timestamp"]), float(size * price), raw)


def closed_summary(positions, complete):
    # Group outcomes of one condition: two sides of a hedge aren't two wins.
    markets = {}
    for p in positions:
        pnl = number(p.get("realized_pnl"))
        if pnl is None:
            complete = False
            continue
        key = p["condition_id"]
        markets[key] = markets.get(key, 0) + pnl
    wins = sum(v > .01 for v in markets.values())
    losses = sum(v < -.01 for v in markets.values())
    return {"closed_markets": len(markets), "wins": wins, "losses": losses,
            "win_rate": wins / (wins + losses) if wins + losses else None,
            "closed_complete": complete}


def consistency(profile):
    """A disclosed screening rule, not a prediction or opaque score."""
    return bool(profile.get("closed_complete") and profile.get("closed_markets", 0) >= 30
                and (profile.get("all_pnl") or 0) > 0 and (profile.get("month_pnl") or 0) > 0
                and profile.get("active_days", 0) >= 10 and (profile.get("positive_days") or 0) >= .6
                and profile.get("unrealized_pnl") is not None
                and profile["all_pnl"] + profile["unrealized_pnl"] > 0)
