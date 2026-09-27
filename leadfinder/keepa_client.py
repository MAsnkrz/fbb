"""Thin Keepa REST client (Product Finder, Product, category search, token status).

Uses the REST API directly (not the `keepa` package) so request/response shapes are
explicit and don't change between library versions.

Token costs (Keepa, at time of writing):
  Product Finder  : ~10 tokens + 1 per 100 ASINs returned
  Product lookup  : 1 token per product, +2 per product with buybox=1
  Category search : 1 token
"""
from __future__ import annotations

import json
import time
from typing import Callable, Iterable

import requests

KEEPA_BASE = "https://api.keepa.com"
DOMAIN_UK = 2

# csv index constants from Keepa's CsvType
CSV_NEW = 1
CSV_SALES = 3
CSV_COUNT_NEW = 11
CSV_BUY_BOX_SHIPPING = 18


class KeepaError(RuntimeError):
    pass


class KeepaClient:
    def __init__(self, api_key: str, domain: int = DOMAIN_UK, log: Callable[[str], None] = print,
                 session: requests.Session | None = None):
        if not api_key:
            raise KeepaError("KEEPA_API_KEY is not set")
        self.key = api_key
        self.domain = domain
        self.log = log
        self.http = session or requests.Session()
        self.tokens_left: int | None = None

    # ------------------------------------------------------------------ core
    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                 timeout: int = 180) -> dict:
        q = {"key": self.key, "domain": self.domain, **(params or {})}
        for attempt in range(12):
            try:
                r = self.http.request(method, f"{KEEPA_BASE}/{path}", params=q,
                                      json=body, timeout=timeout)
            except requests.RequestException as e:
                wait = min(5 * (attempt + 1), 60)
                self.log(f"Keepa network error ({e}); retrying in {wait}s")
                time.sleep(wait)
                continue
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code == 429:
                refill_ms = data.get("refillIn") or 60000
                wait = min(max(refill_ms / 1000, 5) + 2, 600)
                self.tokens_left = data.get("tokensLeft", self.tokens_left)
                self.log(f"Keepa tokens exhausted (left: {self.tokens_left}); waiting {wait:.0f}s for refill")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                wait = min(10 * (attempt + 1), 120)
                self.log(f"Keepa server error {r.status_code}; retrying in {wait}s")
                time.sleep(wait)
                continue
            if r.status_code != 200 or data.get("error"):
                err = data.get("error") or r.text[:300]
                raise KeepaError(f"Keepa {path} failed ({r.status_code}): {err}")
            self.tokens_left = data.get("tokensLeft", self.tokens_left)
            return data
        raise KeepaError(f"Keepa {path}: gave up after repeated retries")

    # --------------------------------------------------------------- helpers
    def token_status(self) -> dict:
        data = self._request("GET", "token")
        return {"tokensLeft": data.get("tokensLeft"), "refillRate": data.get("refillRate"),
                "refillIn": data.get("refillIn")}

    def search_categories(self, term: str) -> list[dict]:
        data = self._request("GET", "search", {"type": "category", "term": term})
        cats = data.get("categories") or {}
        out = []
        for cid, c in cats.items():
            out.append({"id": int(c.get("catId", cid)), "name": c.get("name", ""),
                        "root": c.get("rootCat"), "products": c.get("productCount")})
        out.sort(key=lambda c: (c["root"] != c["id"], -(c["products"] or 0)))
        return out

    def find_products(self, selection: dict, max_results: int = 1000) -> tuple[list[str], int]:
        """Run a Product Finder query. Returns (asins, total_matching)."""
        sel = dict(selection)
        per_page = max(50, min(int(max_results), 10000))
        sel["perPage"] = per_page
        asins: list[str] = []
        total = 0
        page = 0
        while len(asins) < max_results:
            sel["page"] = page
            data = self._request("GET", "query", {"selection": json.dumps(sel)})
            batch = data.get("asinList") or []
            total = data.get("totalResults", total) or total
            asins.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        seen, uniq = set(), []
        for a in asins:
            if a not in seen:
                seen.add(a)
                uniq.append(a)
        return uniq[:max_results], total

    def get_products(self, asins: Iterable[str], buybox: bool = True,
                     progress: Callable[[int, int], None] | None = None) -> list[dict]:
        asins = list(asins)
        out: list[dict] = []
        for i in range(0, len(asins), 100):
            chunk = asins[i:i + 100]
            params = {"asin": ",".join(chunk), "stats": 90}
            if buybox:
                params["buybox"] = 1
            data = self._request("GET", "product", params)
            out.extend(data.get("products") or [])
            if progress:
                progress(min(i + 100, len(asins)), len(asins))
        return out


# ------------------------------------------------------------------ parsing
def _cur(stats: dict, key: str, idx: int):
    arr = stats.get(key) or []
    if len(arr) <= idx:
        return None
    v = arr[idx]
    if v is None or (isinstance(v, (int, float)) and v < 0):
        return None
    return v


def parse_product(p: dict) -> dict:
    """Flatten a Keepa product object into the fields the pipeline needs."""
    st = p.get("stats") or {}
    bb = st.get("buyBoxPrice")
    if bb is not None and bb > 0:
        ship = st.get("buyBoxShipping") or 0
        bb = bb + (ship if ship > 0 else 0)
    else:
        bb = _cur(st, "current", CSV_BUY_BOX_SHIPPING)
    bb_avg90 = _cur(st, "avg90", CSV_BUY_BOX_SHIPPING)
    new_price = _cur(st, "current", CSV_NEW)

    fees = p.get("fbaFees") or {}
    fba = fees.get("pickAndPackFee")
    ref = p.get("referralFeePercent")
    if ref is None:
        ref = p.get("referralFeePercentage")

    tree = [c.get("name", "") for c in (p.get("categoryTree") or [])]
    return {
        "asin": p.get("asin"),
        "title": (p.get("title") or "").strip(),
        "brand": p.get("brand") or "",
        "ean": ", ".join((p.get("eanList") or [])[:3]),
        "ean_list": p.get("eanList") or [],
        "bsr": _cur(st, "current", CSV_SALES),
        "monthly_sold": p.get("monthlySold"),
        "buybox": round(bb / 100, 2) if bb else None,
        "buybox_avg90": round(bb_avg90 / 100, 2) if bb_avg90 else None,
        "new_price": round(new_price / 100, 2) if new_price else None,
        "fba_fee": round(fba / 100, 2) if fba and fba > 0 else None,
        "referral_pct": float(ref) if ref not in (None, -1) else None,
        "offers": _cur(st, "current", CSV_COUNT_NEW),
        "num_items": p.get("numberOfItems") if (p.get("numberOfItems") or 0) > 0 else None,
        "package_qty": p.get("packageQuantity") if (p.get("packageQuantity") or 0) > 0 else None,
        "category_tree": tree,
        "amazon_url": f"https://www.amazon.co.uk/dp/{p.get('asin')}",
    }


# ----------------------------------------------------------- query builder
def build_selection(f: dict) -> dict:
    """Map dashboard filters to a Keepa Product Finder selection.

    Field names follow Keepa's Product Finder API. If Keepa ever renames one, the
    dashboard lets you paste the JSON from Keepa's own "Show API query" button instead.
    """
    sel: dict = {"productType": [0], "sort": [["current_SALES", "asc"]]}
    if f.get("category_id"):
        sel["rootCategory"] = [int(f["category_id"])]
    if f.get("bsr_min") is not None:
        sel["current_SALES_gte"] = int(f["bsr_min"])
    if f.get("bsr_max") is not None:
        sel["current_SALES_lte"] = int(f["bsr_max"])
    if f.get("bb_drop90_max") is not None:
        sel["deltaPercent90_BUY_BOX_SHIPPING_lte"] = int(f["bb_drop90_max"])
    if f.get("offers_min") is not None:
        sel["totalOfferCount_gte"] = int(f["offers_min"])
    if f.get("offers_max") is not None:
        sel["totalOfferCount_lte"] = int(f["offers_max"])
    if f.get("fbm_zero"):
        sel["current_COUNT_NEW_FBM_gte"] = 0
        sel["current_COUNT_NEW_FBM_lte"] = 0
    if f.get("monthly_sold_min"):
        sel["monthlySold_gte"] = int(f["monthly_sold_min"])
    if f.get("buybox_min"):
        sel["current_BUY_BOX_SHIPPING_gte"] = int(round(float(f["buybox_min"]) * 100))
    if f.get("buybox_max"):
        sel["current_BUY_BOX_SHIPPING_lte"] = int(round(float(f["buybox_max"]) * 100))
    # brand exclusions are applied locally after the product lookup (see pipeline)
    return sel
