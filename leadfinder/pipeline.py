"""Background run: Keepa finder -> product data -> local filters -> sourcing -> profit -> alerts."""
from __future__ import annotations

import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from . import profit as P
from .keepa_client import KeepaClient, build_selection, parse_product
from .sourcing import WebSourcer, load_catalogue_index, match_catalogues
from .storage import Store

DEFAULTS = {
    "name": "", "category_id": None, "category_name": "",
    "bsr_min": 600, "bsr_max": 50000, "bb_drop90_max": 5, "bb_variance_max": None,
    "offers_min": 3, "offers_max": 17, "fbm_zero": False, "monthly_sold_min": 0,
    "buybox_min": None, "buybox_max": None, "exclude_brands": [],
    "max_products": 500, "fetch_buybox": True,
    "profit_min": 1.50, "roi_min": 20.0, "referral_mode": "keepa", "unknown_vat_as": "inc",
    "use_catalogues": True, "use_web": True, "skip_web_if_catalogue": True,
    "agents": 8, "batch_size": 10, "searches_per_batch": 12, "fetches_per_batch": 10, "use_fetch": True,
    "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"),
    "keepa_query_override": None, "discord": True,
}


class Cancelled(Exception):
    pass


def _status(l: dict, p: dict) -> tuple[str, str]:
    """Return (status, flag)."""
    if not l.get("source_found"):
        return "NOT SOURCED", ""
    calc = l.get("calc")
    if not calc:
        return "MISSING FEE DATA", ""
    profit, roi = calc["profit"], calc["roi_pct"] or 0
    flags = []
    if l.get("pack_match") in ("no", "unsure", "check"):
        flags.append("check pack size")
    if roi >= 150:
        flags.append("ROI looks too good — verify match")
    if l.get("confidence") == "low":
        flags.append("low confidence")
    vmax = p.get("bb_variance_max")
    var = l.get("bb_variance_pct")
    # same rule as profit_engine.passes_filters: skip variance check when ROI >= 50%
    if vmax not in (None, "", 0) and var is not None and var > float(vmax) and roi < 50:
        return "FAILED BUY BOX VARIANCE", f"Buy Box {var:.0f}% from 90d avg"
    if l.get("pack_match") == "no":
        return "PACK MISMATCH", "; ".join(flags)
    if profit >= float(p["profit_min"]) and roi >= float(p["roi_min"]):
        return ("REVIEW" if flags else "QUALIFIED"), "; ".join(flags)
    if profit > 0:
        return "NEAR-MISS", "; ".join(flags)
    return "NOT PROFITABLE", ""


def apply_profit(leads: list[dict], p: dict):
    for l in leads:
        if l.get("source_found"):
            l["calc"] = P.calculate(l.get("buybox"), l.get("source_price"), l.get("vat", "unknown"),
                                    l.get("fba_fee"), l.get("referral_pct"), l.get("category_tree"),
                                    p.get("referral_mode", "keepa"), p.get("unknown_vat_as", "inc"))
        else:
            l["calc"] = None
        l["status"], l["flag"] = _status(l, p)


def run_job(store: Store, job_id: int, secrets: dict):
    job = store.get_job(job_id)
    p = {**DEFAULTS, **job["params"]}
    log = lambda m: store.log(job_id, m)

    def check_cancel():
        j = store.get_job(job_id)
        if j and j["status"] == "cancelling":
            raise Cancelled()

    t0 = time.time()
    try:
        store.update_job(job_id, status="running", stage="keepa finder", progress=0.02)
        keepa = KeepaClient(secrets.get("KEEPA_API_KEY", ""), log=log)

        # ---------------- 1. Product Finder
        selection = p.get("keepa_query_override") or build_selection(p)
        log(f"Keepa query: {selection}")
        asins, total = keepa.find_products(selection, int(p["max_products"]))
        log(f"Keepa matched {total} products; taking {len(asins)}")
        store.update_job(job_id, summary={"keepa_total": total, "taken": len(asins)})
        if not asins:
            store.update_job(job_id, status="done", stage="done", progress=1.0, finished=time.time(),
                             summary={"keepa_total": total, "taken": 0})
            return

        # ---------------- 2. Product data
        store.update_job(job_id, stage="keepa product data", progress=0.05)
        est = len(asins) * (3 if p["fetch_buybox"] else 1)
        log(f"Fetching product data (~{est} Keepa tokens)")

        def prog(done, n):
            store.update_job(job_id, progress=0.05 + 0.15 * done / n)
            check_cancel()
        raw = keepa.get_products(asins, buybox=bool(p["fetch_buybox"]), progress=prog)
        leads = [parse_product(x) for x in raw]

        # local filters (belt and braces + things the finder can't do)
        excl = {b.strip().lower() for b in (p.get("exclude_brands") or []) if b.strip()}
        kept = []
        for l in leads:
            if excl and (l["brand"] or "").lower() in excl:
                continue
            l["bb_variance_pct"] = P.buybox_variance(l["buybox"], l["buybox_avg90"])
            kept.append(l)
        leads = kept
        log(f"{len(leads)} leads after local filters")
        for l in leads:
            l.update(source_found=False, status="PENDING", flag="")
        store.save_leads(job_id, leads)

        # ---------------- 3. Supplier catalogues
        sourced: dict[str, dict] = {}
        if p["use_catalogues"]:
            store.update_job(job_id, stage="supplier catalogues", progress=0.22)
            idx = load_catalogue_index()
            if idx:
                sourced = match_catalogues(leads, idx)
                log(f"Supplier catalogues: {len(sourced)} exact EAN matches")
            else:
                log("No supplier catalogues uploaded — skipping")

        by_asin = {l["asin"]: l for l in leads}

        def merge(asin, s):
            l = by_asin.get(asin)
            if l is not None:
                l.update(source_found=s["found"], site=s["site"], url=s["url"], source_price=s["price"],
                         vat=s["vat"], confidence=s["confidence"], pack_match=s.get("pack_match", ""),
                         notes=s["notes"], method=s.get("method", ""))
            return l

        for a, s in sourced.items():
            merge(a, s)

        # ---------------- 4. Web agents
        web_stats = {}
        if p["use_web"]:
            todo = [l for l in leads if not (p["skip_web_if_catalogue"] and l["asin"] in sourced)]
            todo = [l for l in todo if l.get("buybox")]  # nothing to compare against otherwise
            bs = max(1, int(p["batch_size"]))
            batches = [todo[i:i + bs] for i in range(0, len(todo), bs)]
            log(f"Web sourcing {len(todo)} leads in {len(batches)} batches with {p['agents']} parallel agents "
                f"(model {p['model']}, ≤{p['searches_per_batch']} searches per batch)")
            store.update_job(job_id, stage="web sourcing", progress=0.25)
            src = WebSourcer(secrets.get("ANTHROPIC_API_KEY", ""), p["model"], int(p["searches_per_batch"]),
                             int(p["fetches_per_batch"]), bool(p["use_fetch"]), log=log)
            done = 0
            with ThreadPoolExecutor(max_workers=max(1, int(p["agents"]))) as ex:
                futs = {ex.submit(src.source_batch, b): b for b in batches}
                for fut in as_completed(futs):
                    b = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        log(f"Batch failed: {e}")
                        res = {l["asin"]: {"found": False, "site": "", "url": "", "price": None, "vat": "",
                                           "confidence": "na", "notes": f"agent error: {e}"[:200],
                                           "method": "web"} for l in b}
                    changed = []
                    for a, s in res.items():
                        if a in sourced and sourced[a]["found"] and not s["found"]:
                            continue
                        l = merge(a, s)
                        if l:
                            changed.append(l)
                    apply_profit(changed, p)
                    store.save_leads(job_id, changed)
                    done += 1
                    store.update_job(job_id, progress=0.25 + 0.7 * done / max(1, len(batches)))
                    if done % 5 == 0 or done == len(batches):
                        log(f"{done}/{len(batches)} batches done · searches used {src.searches_used}")
                    j = store.get_job(job_id)
                    if j and j["status"] == "cancelling":
                        for f in futs:
                            f.cancel()
                        raise Cancelled()
            web_stats = {"searches": src.searches_used, "fetches": src.fetches_used,
                         "input_tokens": src.input_tokens, "output_tokens": src.output_tokens}

        # ---------------- 5. Profit + summary
        store.update_job(job_id, stage="profit", progress=0.97)
        apply_profit(leads, p)
        store.save_leads(job_id, leads)
        counts = {}
        for l in leads:
            counts[l["status"]] = counts.get(l["status"], 0) + 1
        summary = {"keepa_total": total, "taken": len(asins), "leads": len(leads), "counts": counts,
                   "catalogue_matches": len(sourced), "web": web_stats,
                   "keepa_tokens_left": keepa.tokens_left, "minutes": round((time.time() - t0) / 60, 1)}
        store.update_job(job_id, status="done", stage="done", progress=1.0, finished=time.time(), summary=summary)
        log(f"Done: {counts}")

        if p.get("discord") and secrets.get("DISCORD_WEBHOOK_URL"):
            notify_discord(secrets["DISCORD_WEBHOOK_URL"], job["name"], leads, summary, log)
    except Cancelled:
        store.update_job(job_id, status="cancelled", stage="cancelled", finished=time.time())
        log("Run cancelled")
    except Exception as e:
        store.update_job(job_id, status="failed", stage="failed", finished=time.time(), error=str(e)[:500])
        log(f"FAILED: {e}\n{traceback.format_exc()[-1500:]}")


def notify_discord(url: str, name: str, leads: list[dict], summary: dict, log):
    top = sorted([l for l in leads if l["status"] in ("QUALIFIED", "REVIEW")],
                 key=lambda l: -(l["calc"] or {}).get("profit", 0))[:10]
    lines = [f"**{name}** finished — {summary['counts']}"]
    for l in top:
        c = l["calc"]
        lines.append(f"• {l['status']}: [{l['title'][:60]}]({l['amazon_url']}) — £{c['profit']:.2f} / "
                     f"{c['roi_pct']:.0f}% ROI · {l.get('site')} £{l.get('source_price')}"
                     + (f" ⚠️ {l['flag']}" if l.get("flag") else ""))
    if not top:
        lines.append("No qualified leads this run.")
    try:
        requests.post(url, json={"content": "\n".join(lines)[:1900]}, timeout=20)
    except Exception as e:
        log(f"Discord post failed: {e}")
