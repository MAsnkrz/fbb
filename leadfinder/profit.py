"""Profit calculator — same maths as your profit_engine.py (verified vs Sellerfuse).

  cost_inc_vat = source price (inc VAT); ex-VAT prices are grossed up x1.20
  referral     = max(sell x referral%, £0.30)
  digital      = round(sell x 0.7%, 2)
  total_fees   = referral + FBA pick&pack + digital
  net_vat      = (sell - cost_inc_vat) / 6        # output VAT - input VAT reclaim
  profit       = sell - cost_inc_vat - total_fees - net_vat
  roi          = profit / cost_inc_vat

(profit_engine.py also adds VAT on fees and then subtracts it again inside vat_due,
 so it cancels out — the net result is the line above.)

Referral % source:
  "keepa"  -> Keepa's per-ASIN referral % (falls back to the engine rule if missing)
  "engine" -> profit_engine.py rule: H&B 8% <= £10 / 15% above; everything else 15%
"""
from __future__ import annotations

MIN_REFERRAL = 0.30
DIGITAL_RATE = 0.007
VAT_DIVISOR = 6

HB_KEYWORDS = ["health", "beauty", "personal care", "cosmetic", "fragrance", "hair", "skin",
               "baby", "wellness", "hygiene", "grooming", "oral", "deodorant"]


def engine_referral_pct(sell_price: float, category_tree: list[str] | None = None) -> float:
    cats = " ".join(category_tree or []).lower()
    if any(k in cats for k in HB_KEYWORDS):
        return 0.08 if sell_price <= 10.0 else 0.15
    return 0.15


def cost_inc_vat(source_price: float, vat_status: str, unknown_as: str = "inc") -> float:
    status = (vat_status or "unknown").lower()
    if status == "unknown":
        status = unknown_as
    return source_price * 1.20 if status == "ex" else source_price


def calculate(sell_price: float | None, source_price: float | None, vat_status: str,
              fba_fee: float | None, referral_pct: float | None = None,
              category_tree: list[str] | None = None, referral_mode: str = "keepa",
              unknown_vat_as: str = "inc") -> dict | None:
    if not sell_price or source_price is None or fba_fee is None:
        return None
    cost = cost_inc_vat(float(source_price), vat_status, unknown_vat_as)
    if referral_mode == "keepa" and referral_pct:
        pct = float(referral_pct) / 100
    else:
        pct = engine_referral_pct(sell_price, category_tree)
    ref_fee = max(sell_price * pct, MIN_REFERRAL)
    dig_fee = round(sell_price * DIGITAL_RATE, 2)
    total_fees = ref_fee + fba_fee + dig_fee
    net_vat = (sell_price - cost) / VAT_DIVISOR
    profit = sell_price - cost - total_fees - net_vat
    roi = profit / cost * 100 if cost > 0 else None
    margin = profit / sell_price * 100 if sell_price else None
    return {
        "cost_inc_vat": round(cost, 2),
        "referral_pct_used": round(pct * 100, 2),
        "referral_fee": round(ref_fee, 2),
        "fba_fee": round(fba_fee, 2),
        "digital_fee": dig_fee,
        "total_fees": round(total_fees, 2),
        "net_vat": round(net_vat, 2),
        "profit": round(profit, 2),
        "roi_pct": round(roi, 1) if roi is not None else None,
        "margin_pct": round(margin, 1) if margin is not None else None,
    }


def buybox_variance(buybox: float | None, avg90: float | None) -> float | None:
    if not buybox or not avg90:
        return None
    return abs(buybox - avg90) / avg90 * 100
