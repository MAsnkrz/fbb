"""Keepa Lead Finder — Streamlit dashboard.

Run locally:  streamlit run app.py
Env vars:     KEEPA_API_KEY, ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL (optional),
              APP_PASSWORD (recommended when deployed), CLAUDE_MODEL, DATA_DIR
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import streamlit as st

from leadfinder.export import ORDER, to_excel, to_frame
from leadfinder.keepa_client import KeepaClient, KeepaError, build_selection
from leadfinder.pipeline import DEFAULTS, run_job
from leadfinder.sourcing import delete_catalogue, list_catalogues, save_catalogue
from leadfinder.storage import Store

st.set_page_config(page_title="Keepa Lead Finder", page_icon="📦", layout="wide")

QUICK_CATEGORIES = {
    "DIY & Tools": 79903031,
    "Health & Personal Care": 65801031,
    "Computers & Accessories": 340831031,
}
MODELS = [os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"), "claude-sonnet-4-5", "claude-sonnet-4-6",
          "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-5"]
MODELS = list(dict.fromkeys(MODELS))


def secrets() -> dict:
    return {k: os.environ.get(k, "") for k in ("KEEPA_API_KEY", "ANTHROPIC_API_KEY", "DISCORD_WEBHOOK_URL")}


@st.cache_resource
def get_store() -> Store:
    s = Store()
    s.mark_interrupted()
    return s


@st.cache_resource
def get_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=int(os.environ.get("MAX_CONCURRENT_RUNS", "2")))


store = get_store()


# ------------------------------------------------------------------ auth
def gate():
    pw = os.environ.get("APP_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return
    st.title("📦 Keepa Lead Finder")
    entered = st.text_input("Password", type="password")
    if entered and entered == pw:
        st.session_state.authed = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    st.stop()


gate()


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M") if ts else ""


# ------------------------------------------------------------- new run
def page_new_run():
    st.header("New run")
    presets = store.list_presets()
    c1, c2, c3 = st.columns([3, 1, 1])
    choice = c1.selectbox("Load preset", ["— defaults —"] + list(presets))
    if c2.button("Load", use_container_width=True):
        st.session_state.form = {**DEFAULTS, **(presets.get(choice) or {})} if choice in presets else dict(DEFAULTS)
        st.rerun()
    if choice in presets and c3.button("Delete preset", use_container_width=True):
        store.delete_preset(choice)
        st.rerun()
    f = st.session_state.setdefault("form", dict(DEFAULTS))

    # ---- category
    st.subheader("1 · Category")
    cc1, cc2 = st.columns(2)
    quick = cc1.selectbox("Quick pick", ["(custom / searched)"] + list(QUICK_CATEGORIES),
                          index=([None] + list(QUICK_CATEGORIES.values())).index(f.get("category_id"))
                          if f.get("category_id") in QUICK_CATEGORIES.values() else 0)
    term = cc2.text_input("…or search Keepa categories (1 token)", placeholder="e.g. garden, toys, pet")
    if term and cc2.button("Search"):
        try:
            st.session_state.cat_results = KeepaClient(secrets()["KEEPA_API_KEY"]).search_categories(term)
        except KeepaError as e:
            st.error(str(e))
    cat_id, cat_name = f.get("category_id"), f.get("category_name", "")
    if quick != "(custom / searched)":
        cat_id, cat_name = QUICK_CATEGORIES[quick], quick
    res = st.session_state.get("cat_results") or []
    if res:
        opts = {f"{c['name']} ({c['id']}){' · root' if c['root'] == c['id'] else ''}": c for c in res[:40]}
        pick = st.selectbox("Search results", list(opts))
        if st.button("Use this category"):
            c = opts[pick]
            f["category_id"], f["category_name"] = c["id"], c["name"]
            st.session_state.cat_results = []
            st.rerun()
    cat_id = st.number_input("Category ID (root category)", value=int(cat_id or 0), step=1,
                             help="Amazon UK browse-node ID. Root categories work best.")
    if cat_id:
        st.caption(f"Using: **{cat_name or 'custom'}** — {cat_id}")

    # ---- filters
    st.subheader("2 · Keepa filters")
    a, b, c, d = st.columns(4)
    bsr_min = a.number_input("BSR min", value=int(f["bsr_min"] or 0), step=100)
    bsr_max = b.number_input("BSR max", value=int(f["bsr_max"] or 0), step=1000)
    offers_min = c.number_input("Offers min", value=int(f["offers_min"] or 0), step=1)
    offers_max = d.number_input("Offers max", value=int(f["offers_max"] or 0), step=1)
    a, b, c, d = st.columns(4)
    bb_drop = a.number_input("Buy Box 90d drop ≤ %", value=int(f["bb_drop90_max"] or 0), step=1,
                             help="0 = no limit")
    bb_var = b.number_input("Buy Box variance vs 90d avg ≤ %", value=float(f["bb_variance_max"] or 0), step=1.0,
                            help="Checked after profit, like profit_engine (skipped when ROI ≥ 50%). 0 = off")
    monthly = c.number_input("Monthly sold ≥", value=int(f["monthly_sold_min"] or 0), step=10)
    fbm_zero = d.checkbox("FBM sellers out of stock (0 FBM offers)", value=bool(f["fbm_zero"]))
    a, b, c, d = st.columns(4)
    bb_min = a.number_input("Buy Box £ min", value=float(f["buybox_min"] or 0), step=1.0)
    bb_max = b.number_input("Buy Box £ max", value=float(f["buybox_max"] or 0), step=1.0)
    max_products = c.number_input("Max products to pull", value=int(f["max_products"]), step=100, min_value=10,
                                  max_value=10000)
    fetch_bb = d.checkbox("Fetch Buy Box data (3 tokens/product)", value=bool(f["fetch_buybox"]))
    excl = st.text_input("Exclude brands (comma separated)", value=", ".join(f.get("exclude_brands") or []))

    # ---- profit
    st.subheader("3 · Profit bar")
    a, b, c, d = st.columns(4)
    profit_min = a.number_input("Profit ≥ £", value=float(f["profit_min"]), step=0.5)
    roi_min = b.number_input("ROI ≥ %", value=float(f["roi_min"]), step=5.0)
    ref_mode = c.selectbox("Referral %", ["keepa", "engine"], index=0 if f["referral_mode"] == "keepa" else 1,
                           format_func=lambda x: {"keepa": "Keepa per-ASIN", "engine": "profit_engine rule"}[x])
    unk = d.selectbox("Unknown VAT on source price", ["inc", "ex"], index=0 if f["unknown_vat_as"] == "inc" else 1,
                      format_func=lambda x: {"inc": "Assume inc VAT", "ex": "Assume ex VAT (safer)"}[x])

    # ---- sourcing
    st.subheader("4 · Sourcing")
    a, b, c = st.columns(3)
    use_cat = a.checkbox("Match supplier catalogues by EAN first", value=bool(f["use_catalogues"]))
    use_web = b.checkbox("Web research agents (Claude)", value=bool(f["use_web"]))
    skip_web = c.checkbox("Skip web for catalogue matches", value=bool(f["skip_web_if_catalogue"]))
    a, b, c, d, e = st.columns(5)
    agents = a.number_input("Parallel agents", value=int(f["agents"]), min_value=1, max_value=50)
    batch = b.number_input("Products per agent", value=int(f["batch_size"]), min_value=1, max_value=40)
    searches = c.number_input("Searches per batch", value=int(f["searches_per_batch"]), min_value=1, max_value=50)
    fetches = d.number_input("Page fetches per batch", value=int(f["fetches_per_batch"]), min_value=0, max_value=50)
    model = e.selectbox("Model", MODELS, index=MODELS.index(f["model"]) if f["model"] in MODELS else 0)
    discord = st.checkbox("Post qualified leads to Discord", value=bool(f["discord"]),
                          disabled=not secrets()["DISCORD_WEBHOOK_URL"])

    params = {
        **f, "category_id": int(cat_id) or None, "category_name": cat_name,
        "bsr_min": bsr_min or None, "bsr_max": bsr_max or None,
        "offers_min": offers_min or None, "offers_max": offers_max or None,
        "bb_drop90_max": bb_drop or None, "bb_variance_max": bb_var or None,
        "monthly_sold_min": monthly or 0, "fbm_zero": fbm_zero,
        "buybox_min": bb_min or None, "buybox_max": bb_max or None,
        "max_products": int(max_products), "fetch_buybox": fetch_bb,
        "exclude_brands": [x.strip() for x in excl.split(",") if x.strip()],
        "profit_min": profit_min, "roi_min": roi_min, "referral_mode": ref_mode, "unknown_vat_as": unk,
        "use_catalogues": use_cat, "use_web": use_web, "skip_web_if_catalogue": skip_web,
        "agents": int(agents), "batch_size": int(batch), "searches_per_batch": int(searches),
        "fetches_per_batch": int(fetches), "use_fetch": int(fetches) > 0, "model": model, "discord": discord,
    }

    with st.expander("Keepa API query (advanced)"):
        st.caption("Tip: in Keepa's Product Finder click **Show API query** and paste it here to use "
                   "Keepa's exact filter JSON instead of the form above.")
        gen = json.dumps(build_selection(params), indent=1)
        txt = st.text_area("Query JSON", value=json.dumps(f["keepa_query_override"], indent=1)
                           if f.get("keepa_query_override") else gen, height=220)
        use_override = st.checkbox("Use this JSON instead of the form", value=bool(f.get("keepa_query_override")))
        if use_override:
            try:
                params["keepa_query_override"] = json.loads(txt)
            except ValueError as e:
                st.error(f"Invalid JSON: {e}")
        else:
            params["keepa_query_override"] = None

    n = int(max_products)
    per = 3 if fetch_bb else 1
    batches = -(-n // int(batch))
    st.info(f"Estimate for {n} products: ~{n * per + 15} Keepa tokens · up to {batches} agent batches · "
            f"≤{batches * int(searches)} web searches (only for products with no catalogue match).")

    st.subheader("5 · Go")
    a, b, c = st.columns([2, 1, 1])
    name = a.text_input("Run name", value=f.get("name") or f"{cat_name or 'Run'} {datetime.now():%d %b %H:%M}")
    if b.button("💾 Save as preset", use_container_width=True):
        st.session_state.form = params
        store.save_preset(name, {k: v for k, v in params.items() if k != "name"})
        st.success(f"Saved preset '{name}'")
    if c.button("🚀 Start run", type="primary", use_container_width=True):
        missing = [k for k in ("KEEPA_API_KEY",) if not secrets()[k]]
        if use_web and not secrets()["ANTHROPIC_API_KEY"]:
            missing.append("ANTHROPIC_API_KEY")
        if missing:
            st.error(f"Missing environment variables: {', '.join(missing)}")
        elif not params.get("category_id") and not params.get("keepa_query_override"):
            st.error("Pick a category first")
        else:
            params["name"] = name
            st.session_state.form = params
            job_id = store.create_job(name, params)
            get_executor().submit(run_job, store, job_id, secrets())
            st.session_state.view_job = job_id
            st.session_state.page = "Runs"
            st.rerun()


# ------------------------------------------------------------------ runs
STATUS_ICON = {"queued": "⏳", "running": "🔄", "cancelling": "🛑", "done": "✅", "failed": "❌",
               "cancelled": "⏹️", "interrupted": "⚠️"}


def page_runs():
    st.header("Runs")
    jobs = store.list_jobs()
    if not jobs:
        st.info("No runs yet — start one from **New run**.")
        return
    table = pd.DataFrame([{
        "ID": j["id"], "": STATUS_ICON.get(j["status"], ""), "Name": j["name"], "Status": j["status"],
        "Stage": j["stage"], "Progress": round((j["progress"] or 0) * 100),
        "Qualified": (j["summary"].get("counts") or {}).get("QUALIFIED", 0),
        "Review": (j["summary"].get("counts") or {}).get("REVIEW", 0),
        "Started": fmt_ts(j["created"]),
    } for j in jobs])
    st.dataframe(table, hide_index=True, use_container_width=True,
                 column_config={"Progress": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%")})
    ids = [j["id"] for j in jobs]
    default = st.session_state.get("view_job", ids[0])
    job_id = st.selectbox("Open run", ids, index=ids.index(default) if default in ids else 0,
                          format_func=lambda i: next(f"#{j['id']} · {j['name']}" for j in jobs if j["id"] == i))
    st.session_state.view_job = job_id
    run_detail(job_id)


@st.fragment(run_every=4)
def live_status(job_id: int):
    j = store.get_job(job_id)
    if not j:
        return
    st.progress(min(1.0, j["progress"] or 0), text=f"{STATUS_ICON.get(j['status'], '')} {j['status']} — {j['stage']}")
    if j.get("error"):
        st.error(j["error"])
    with st.expander("Log", expanded=j["status"] in ("running", "failed")):
        st.code("\n".join(f"{fmt_ts(r['ts'])}  {r['msg']}" for r in store.get_log(job_id, 60)) or "…")
    if j["status"] in ("running", "queued"):
        leads = store.get_leads(job_id)
        found = sum(1 for l in leads if l.get("source_found"))
        q = sum(1 for l in leads if l.get("status") in ("QUALIFIED", "REVIEW"))
        st.caption(f"{len(leads)} leads · {found} sourced so far · {q} qualified/review so far")


def run_detail(job_id: int):
    j = store.get_job(job_id)
    live_status(job_id)
    a, b, c, d = st.columns(4)
    if j["status"] in ("running", "queued") and a.button("🛑 Cancel run"):
        store.update_job(job_id, status="cancelling")
        st.rerun()
    if b.button("↩️ Load settings into form"):
        st.session_state.form = {**DEFAULTS, **j["params"]}
        st.session_state.page = "New run"
        st.rerun()
    if j["status"] not in ("running", "queued", "cancelling") and c.button("🗑️ Delete run"):
        store.delete_job(job_id)
        st.session_state.pop("view_job", None)
        st.rerun()
    if st.button("🔁 Refresh results"):
        st.rerun()

    s = j["summary"] or {}
    if s:
        m = st.columns(6)
        counts = s.get("counts") or {}
        m[0].metric("Keepa matches", s.get("keepa_total", "–"))
        m[1].metric("Pulled", s.get("taken", "–"))
        m[2].metric("Qualified", counts.get("QUALIFIED", 0))
        m[3].metric("Review", counts.get("REVIEW", 0))
        m[4].metric("Near-miss", counts.get("NEAR-MISS", 0))
        web = s.get("web") or {}
        m[5].metric("Web searches", web.get("searches", 0))

    leads = store.get_leads(job_id)
    if not leads:
        return
    df = to_frame(leads)
    present = [x for x in ORDER if x in set(df["Status"])]
    default = [x for x in present if x in ("QUALIFIED", "REVIEW", "NEAR-MISS")] or present
    show = st.multiselect("Show statuses", present, default=default)
    view = df[df["Status"].isin(show)]
    st.dataframe(view, hide_index=True, use_container_width=True, height=min(560, 36 * (len(view) + 1) + 4),
                 column_config={
        "Source URL": st.column_config.LinkColumn(display_text="open"),
        "Amazon": st.column_config.LinkColumn(display_text="amazon"),
        "Profit £": st.column_config.NumberColumn(format="£%.2f"),
        "Buy Box £": st.column_config.NumberColumn(format="£%.2f"),
        "Source price £": st.column_config.NumberColumn(format="£%.2f"),
        "ROI %": st.column_config.NumberColumn(format="%.1f%%"),
        "Title": st.column_config.TextColumn(width="large"),
        "Notes": st.column_config.TextColumn(width="large"),
    })
    st.caption("⚠️ REVIEW = passes the bar but flagged (pack size unsure, ROI ≥150%, or low confidence). "
               "Always check pack size / width / variant on the source page before buying.")
    a, b = st.columns(2)
    a.download_button("⬇️ Excel (live profit formulas)", to_excel(leads, j["params"], s),
                      file_name=f"keepa_run_{job_id}.xlsx", use_container_width=True)
    b.download_button("⬇️ CSV", df.to_csv(index=False).encode(), file_name=f"keepa_run_{job_id}.csv",
                      use_container_width=True)


# ------------------------------------------------------------ catalogues
def page_catalogues():
    st.header("Supplier catalogues")
    st.write("Upload supplier price lists (Qogita exports, wholesaler CSVs, your monitor outputs). Every run "
             "checks these by **exact EAN** first — no web searches needed for matches.")
    st.caption("Needs an EAN/barcode column and a price column. Optional: title, url, pack/qty, vat (inc/ex), stock.")
    up = st.file_uploader("CSV file", type=["csv"])
    ex_vat = st.checkbox("Prices in this file are ex VAT (e.g. Qogita)", value=True)
    if up and st.button("Upload"):
        try:
            meta = save_catalogue(up.name, up.getvalue(), ex_vat)
            st.success(f"Saved {meta['name']} — {meta['rows']} rows")
        except ValueError as e:
            st.error(str(e))
    cats = list_catalogues()
    for c in cats:
        a, b = st.columns([5, 1])
        a.write(f"**{c['name']}** · {c['rows']} rows · {'ex' if c['prices_ex_vat'] else 'inc'} VAT · "
                f"uploaded {fmt_ts(c['uploaded'])}")
        if b.button("Delete", key=f"del_{c['file']}"):
            delete_catalogue(c["file"])
            st.rerun()
    if not cats:
        st.info("No catalogues yet.")


# --------------------------------------------------------------- status
def page_status():
    st.header("Status & setup")
    s = secrets()
    for k, v in s.items():
        st.write(f"{'✅' if v else '❌'} `{k}` {'set' if v else 'not set'}")
    st.write(f"{'✅' if os.environ.get('APP_PASSWORD') else '⚠️'} `APP_PASSWORD` "
             f"{'set' if os.environ.get('APP_PASSWORD') else 'not set — anyone with the URL can use your API keys'}")
    st.write(f"Data folder: `{os.environ.get('DATA_DIR', './data')}`")
    if s["KEEPA_API_KEY"] and st.button("Check Keepa tokens"):
        try:
            t = KeepaClient(s["KEEPA_API_KEY"]).token_status()
            st.success(f"Tokens left: {t['tokensLeft']} · refill rate {t['refillRate']}/min")
        except KeepaError as e:
            st.error(str(e))


# ------------------------------------------------------------------ nav
pages = {"New run": page_new_run, "Runs": page_runs, "Supplier catalogues": page_catalogues,
         "Status": page_status}
st.sidebar.title("📦 Keepa Lead Finder")
current = st.session_state.get("page", "New run")
choice = st.sidebar.radio("Go to", list(pages), index=list(pages).index(current))
if choice != current:
    st.session_state.page = choice
    st.rerun()
running = [j for j in store.list_jobs(10) if j["status"] in ("running", "queued")]
if running:
    st.sidebar.info(f"🔄 {len(running)} run(s) in progress")
pages[st.session_state.get("page", "New run")]()
