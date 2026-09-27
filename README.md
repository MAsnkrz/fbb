# 📦 Keepa Lead Finder — dashboard

Pick a category and filters, press **Start run**, close the tab. The run carries on in the background:

1. **Keepa Product Finder** (API) with your filters: category, BSR, Buy Box 90-day drop, offer count, FBM = 0, monthly sold, Buy Box £ range
2. **Keepa product data** for every match: Buy Box, 90-day average, FBA fee, referral %, EAN, pack/items
3. **Supplier catalogues** first: exact EAN match against CSVs you upload (Qogita exports, wholesaler lists, your monitor outputs). These need no web searches.
4. **Claude research agents**, run in parallel, for everything else. They use web search and page fetch with the marketplaces blocked (Amazon, eBay, AliExpress, Temu and others), and they are told to check the **pack size, width and variant**.
5. **Profit** with the same maths as `profit_engine.py` (inc-VAT cost, referral, FBA, 0.7% digital fee, net VAT ÷ 6)
6. **Results** table, Excel export with live formulas, CSV, and optional Discord alert

There are no browser permission prompts and no 200-search session cap. Your own API keys do the work.

## Statuses

| Status | Meaning |
|---|---|
| QUALIFIED | Passes your profit and ROI bar with no warnings |
| REVIEW | Passes the bar, but something needs checking: pack size unsure, ROI ≥150%, or low confidence |
| NEAR-MISS | Profitable, but under the bar |
| PACK MISMATCH | The agent found the product but in a different pack or variant |
| FAILED BUY BOX VARIANCE | Buy Box is too far from its 90-day average. Like `profit_engine`, this check is skipped when ROI ≥50% |
| NOT PROFITABLE / NOT SOURCED | No profit, or no legitimate UK source found |

## Run it on your laptop

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in the keys
export $(grep -v '^#' .env | xargs)
streamlit run app.py
```

## Deploy on Railway

1. Push this folder to a **private** GitHub repo and create a Railway service from it (`railway.json` sets the start command).
2. **Variables:** set `KEEPA_API_KEY`, `ANTHROPIC_API_KEY`, `APP_PASSWORD` (don't skip this, the URL is public), and optionally `DISCORD_WEBHOOK_URL` and `CLAUDE_MODEL`.
3. **Volume:** add one mounted at `/data` and set `DATA_DIR=/data`, so runs, presets and catalogues survive redeploys.
4. Open the generated domain and log in with `APP_PASSWORD`.

> Runs execute inside the web process. If Railway restarts the service mid-run, that run is marked **interrupted**. Use **Load settings into form**, then start it again.

## First-run check (5 minutes)

Keepa's API field names are used to build the query. Before a big run:

1. In Keepa's Product Finder, set the same filters as the dashboard and click **Show API query**.
2. In the dashboard, open **Keepa API query (advanced)** and compare. If any field name differs (especially the FBM offer-count filter), paste Keepa's JSON and tick **Use this JSON instead**. The dashboard sends exactly that.
3. Do a small test run (**Max products = 50**) and check the numbers against Keepa and SellerAmp for a couple of ASINs.

## Costs to expect

- **Keepa:** about 3 tokens per product with Buy Box data (1 without), plus about 10 per finder query. 1,000 products is about 3,000 tokens. Check your plan's refill rate on the **Status** page. The client waits automatically when tokens run out.
- **Claude API:** billed per token, plus per web search and fetch at Anthropic's current rates. The dashboard shows an upper bound for searches before you start. Each run's summary records the actual searches and tokens used. To cut cost, upload supplier catalogues (EAN matches skip the web step), lower "Searches per batch", or use a cheaper model.

## Files

```
app.py                    Streamlit UI (New run · Runs · Supplier catalogues · Status)
leadfinder/keepa_client.py  Keepa REST: finder, product, category search, tokens
leadfinder/sourcing.py      catalogue EAN matching + Claude web-research agents
leadfinder/profit.py        profit_engine maths (Keepa per-ASIN referral % or engine rule)
leadfinder/pipeline.py      background run, statuses, Discord alert
leadfinder/storage.py       SQLite (runs, leads, logs, presets)
leadfinder/export.py        results table + Excel with live formulas
tests/                      pytest: profit maths vs Run 5 numbers, full mocked run
```

After deploying, run `python scripts/live_check.py` once with your keys set — it checks the Keepa field names (FBM filter included) against the Run 5 numbers and makes one small Claude call.

`pytest -q` runs everything offline with a fake Keepa API and a fake Claude client.
