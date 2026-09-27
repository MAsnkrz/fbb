"""Sourcing: (1) exact EAN match against uploaded supplier catalogues, then
(2) Claude API research agents with server-side web search for everything else."""
from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Callable

from .storage import CATALOG_DIR

MARKETPLACE_DOMAINS = [
    "amazon.co.uk", "amazon.com", "amazon.de", "amazon.fr", "ebay.co.uk", "ebay.com",
    "aliexpress.com", "temu.com", "etsy.com", "wish.com", "onbuy.com", "fruugo.co.uk",
    "facebook.com", "gumtree.com", "manomano.co.uk",
]


# ============================================================ catalogues
def norm_ean(v) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    return d.lstrip("0")


_COLS = {
    "ean": ["ean", "barcode", "gtin", "upc", "ean13", "ean_code"],
    "price": ["price", "cost", "unit price", "unit_price", "buy price", "trade price", "price_gbp", "net price"],
    "title": ["title", "name", "product", "product name", "description"],
    "url": ["url", "link", "product url"],
    "pack": ["pack", "pack size", "qty", "quantity", "case qty", "units"],
    "vat": ["vat", "vat status", "vat_status"],
    "stock": ["stock", "stock qty", "available", "inventory"],
}


def _pick(header: list[str], key: str) -> str | None:
    low = {h.lower().strip(): h for h in header}
    for cand in _COLS[key]:
        if cand in low:
            return low[cand]
    for h in header:
        if any(c in h.lower() for c in _COLS[key]):
            return h
    return None


def save_catalogue(name: str, raw: bytes, prices_ex_vat: bool) -> dict:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    ean_c, price_c = _pick(header, "ean"), _pick(header, "price")
    if not ean_c or not price_c:
        raise ValueError(f"Couldn't find EAN and price columns in {header}")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    (CATALOG_DIR / safe).write_bytes(raw)
    meta = {"name": name, "file": safe, "prices_ex_vat": prices_ex_vat, "columns": header,
            "rows": sum(1 for _ in csv.DictReader(io.StringIO(text))), "uploaded": time.time()}
    (CATALOG_DIR / f"{safe}.meta.json").write_text(json.dumps(meta))
    return meta


def list_catalogues() -> list[dict]:
    out = []
    for m in sorted(CATALOG_DIR.glob("*.meta.json")):
        try:
            out.append(json.loads(m.read_text()))
        except Exception:
            pass
    return out


def delete_catalogue(file: str):
    for p in (CATALOG_DIR / file, CATALOG_DIR / f"{file}.meta.json"):
        if p.exists():
            p.unlink()


def load_catalogue_index() -> dict[str, list[dict]]:
    """EAN -> list of offers across all uploaded catalogues."""
    idx: dict[str, list[dict]] = {}
    for meta in list_catalogues():
        path = CATALOG_DIR / meta["file"]
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        header = reader.fieldnames or []
        cols = {k: _pick(header, k) for k in _COLS}
        for row in reader:
            e = norm_ean(row.get(cols["ean"]))
            if not e:
                continue
            try:
                price = float(str(row.get(cols["price"]) or "").replace("£", "").replace(",", ""))
            except ValueError:
                continue
            vat = (row.get(cols["vat"]) or "").strip().lower() if cols["vat"] else ""
            if vat not in ("inc", "ex"):
                vat = "ex" if meta.get("prices_ex_vat") else "inc"
            idx.setdefault(e, []).append({
                "supplier": meta["name"], "price": price, "vat": vat,
                "title": row.get(cols["title"]) if cols["title"] else "",
                "url": row.get(cols["url"]) if cols["url"] else "",
                "pack": row.get(cols["pack"]) if cols["pack"] else "",
                "stock": row.get(cols["stock"]) if cols["stock"] else "",
            })
    return idx


def match_catalogues(leads: list[dict], idx: dict[str, list[dict]]) -> dict[str, dict]:
    """Cheapest exact-EAN offer per ASIN."""
    out = {}
    for l in leads:
        best = None
        for e in l.get("ean_list") or []:
            for off in idx.get(norm_ean(e), []):
                eff = off["price"] * (1.2 if off["vat"] == "ex" else 1.0)
                if best is None or eff < best[0]:
                    best = (eff, off)
        if best:
            off = best[1]
            notes = f"Exact EAN match in supplier catalogue. {off.get('title') or ''}".strip()
            if off.get("pack"):
                notes += f" | supplier pack: {off['pack']} — check vs Amazon qty"
            if off.get("stock"):
                notes += f" | stock: {off['stock']}"
            out[l["asin"]] = {"found": True, "site": off["supplier"], "url": off.get("url") or "",
                              "price": off["price"], "vat": off["vat"], "confidence": "high",
                              "pack_match": "check" if off.get("pack") else "yes",
                              "notes": notes, "method": "catalogue"}
    return out


# ============================================================ web agents
PROMPT = """You are sourcing UK buy-prices for {n} Amazon UK products for an FBA online-arbitrage seller (VAT registered).

For EACH product find a live, buyable UK source: an independent UK retailer, trade/DIY merchant, pharmacy, wholesaler or the brand's own UK site, where it can be bought today at a real GBP price.
Not valid: Amazon, eBay, AliExpress, Temu, Etsy, Wish, OnBuy, Fruugo, Facebook, Gumtree, ManoMano, or price-comparison sites (use those only to discover the real retailer).

Efficiency: you have a limited number of web searches for the whole batch. Search the EAN, or brand + exact model/part number + "UK". Skip (not found, note "Amazon-only/private label") obvious Amazon-exclusive or generic private-label brands unless you have searches to spare. Use web fetch, if available, to open a candidate product page and confirm price, pack size and stock.

CRITICAL — variant check. Past runs had most false positives from variant mismatches (pack of 1 vs pack of 3; 18mm vs 50mm tape; 500g vs 1kg; single box vs 2 boxes). Only mark found=true if the source is the SAME pack size / quantity / size / width / colour as the Amazon listing (use the title and the Items value). If the source only sells singles and the listing is a multipack you may multiply the unit price, and say so in notes ("x3 for 3-pack"). If a trade case is the only option, give the per-unit price and state the case size and case price in notes.

Products (ASIN | EAN | Brand | Title | Items | Buy Box £):
{rows}

Reply with ONLY a JSON array (no prose, no code fences), one object per product in the same order:
[{{"asin": "...", "found": true/false, "site": "retailer name", "url": "product page url", "price": 12.34, "vat": "inc"|"ex"|"unknown", "pack_match": "yes"|"no"|"unsure", "confidence": "high"|"medium"|"low", "notes": "<=35 words: how matched (EAN/model/title), source pack size, stock, case size if any"}}]
"""


def _extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1:
        raise ValueError("no JSON array in reply")
    return json.loads(text[s:e + 1])


class WebSourcer:
    def __init__(self, api_key: str, model: str, searches_per_batch: int = 12, fetches_per_batch: int = 10,
                 use_fetch: bool = True, log: Callable[[str], None] = print, client=None):
        if client is None:
            import anthropic
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            import os
            headers = {}
            if os.environ.get("ANTHROPIC_WORKSPACE_ID"):
                headers["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
            client = anthropic.Anthropic(api_key=api_key, max_retries=4, default_headers=headers or None)
        self.client = client
        self.model = model
        self.searches = searches_per_batch
        self.fetches = fetches_per_batch
        self.use_fetch = use_fetch
        self.log = log
        self.searches_used = 0
        self.fetches_used = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def _tools(self):
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": self.searches,
                  "blocked_domains": MARKETPLACE_DOMAINS,
                  "user_location": {"type": "approximate", "country": "GB"}}]
        if self.use_fetch and self.fetches > 0:
            tools.append({"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": self.fetches})
        return tools

    def _call(self, messages):
        for attempt in range(6):
            try:
                return self.client.messages.create(model=self.model, max_tokens=8000,
                                                   tools=self._tools(), messages=messages)
            except Exception as e:  # anthropic.* errors
                msg = str(e)
                if self.use_fetch and "web_fetch" in msg and ("not" in msg or "invalid" in msg.lower()):
                    self.log("web_fetch tool not available for this model/account — continuing with search only")
                    self.use_fetch = False
                    continue
                status = getattr(e, "status_code", None)
                if status in (429, 500, 502, 503, 529) or "overloaded" in msg.lower() or "rate" in msg.lower():
                    wait = min(15 * (2 ** attempt), 300)
                    self.log(f"Claude API busy ({status or msg[:60]}); retrying in {wait}s")
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("Claude API: gave up after retries")

    def source_batch(self, leads: list[dict]) -> dict[str, dict]:
        rows = "\n".join(" | ".join(str(x) for x in [
            l["asin"], l.get("ean") or "none", l.get("brand") or "", (l.get("title") or "")[:140],
            l.get("num_items") or l.get("package_qty") or "?", l.get("buybox") or "?"]) for l in leads)
        messages = [{"role": "user", "content": PROMPT.format(n=len(leads), rows=rows)}]
        resp = None
        for _ in range(6):  # handle pause_turn for long server-tool turns
            resp = self._call(messages)
            self._account(resp)
            if getattr(resp, "stop_reason", None) == "pause_turn":
                messages = messages + [{"role": "assistant", "content": resp.content}]
                continue
            break
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        try:
            items = _extract_json(text)
        except Exception as e:
            self.log(f"Could not parse agent reply ({e}); marking batch as not found")
            items = []
        by = {str(i.get("asin", "")).strip(): i for i in items if isinstance(i, dict)}
        out = {}
        for l in leads:
            i = by.get(l["asin"], {})
            price = i.get("price")
            try:
                price = float(str(price).replace("£", "").replace(",", "")) if price not in (None, "") else None
            except ValueError:
                price = None
            found = bool(i.get("found")) and price is not None
            out[l["asin"]] = {
                "found": found, "site": i.get("site") or "", "url": i.get("url") or "",
                "price": price, "vat": (i.get("vat") or "unknown").lower(),
                "pack_match": (i.get("pack_match") or "unsure").lower(),
                "confidence": (i.get("confidence") or ("na" if not found else "low")).lower(),
                "notes": i.get("notes") or ("no reply for this ASIN" if not i else ""),
                "method": "web",
            }
        return out

    def _account(self, resp):
        u = getattr(resp, "usage", None)
        if not u:
            return
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu:
            self.searches_used += getattr(stu, "web_search_requests", 0) or 0
            self.fetches_used += getattr(stu, "web_fetch_requests", 0) or 0
