#!/usr/bin/env python3
"""
Commission Tracker Refresh Script
Fetches orders from Shopify and rebuilds the HTML commission tracker files.
Run via GitHub Actions daily, or manually with: python3 update_trackers.py
Requires env vars: SHOPIFY_STORE (e.g. mystore.myshopify.com), SHOPIFY_TOKEN
"""
import json, os, re, sys, time
from datetime import datetime, timezone, timedelta
import urllib.request, urllib.parse

STORE = os.environ.get("SHOPIFY_STORE", "")
TOKEN = os.environ.get("SHOPIFY_TOKEN", "")

if not STORE or not TOKEN:
    print("ERROR: Set SHOPIFY_STORE and SHOPIFY_TOKEN env vars", file=sys.stderr)
    sys.exit(1)

GRAPHQL_URL = f"https://{STORE}/admin/api/2024-10/graphql.json"
COMMISSION_RATE = 0.01

QUERY = """
query GetOrders($cursor: String) {
  orders(first: 250, after: $cursor, query: "(tag:Draft-Mike OR tag:Draft-Matt) AND processed_at:>=2025-09-01", sortKey: PROCESSED_AT, reverse: false) {
    edges {
      node {
        name
        id
        processedAt
        displayFinancialStatus
        tags
        subtotalPriceSet { shopMoney { amount } }
        currentSubtotalPriceSet { shopMoney { amount } }
        totalRefundedSet { shopMoney { amount } }
        customer { displayName }
        transactions(first: 10) {
          processedAt
          status
          kind
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

def shopify_gql(query, variables=None):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": TOKEN,
        }
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def fetch_all_orders():
    all_orders = []
    cursor = None
    page = 1
    while True:
        print(f"  Fetching page {page}...", file=sys.stderr)
        result = shopify_gql(QUERY, {"cursor": cursor})
        orders_data = result["data"]["orders"]
        edges = orders_data["edges"]
        for edge in edges:
            n = edge["node"]
            payment_date = n["processedAt"]
            for txn in n.get("transactions", []):
                if txn["kind"] == "SALE" and txn["status"] == "SUCCESS":
                    payment_date = txn["processedAt"]
                    break
            all_orders.append({
                "name": n["name"],
                "id": n["id"].split("/")[-1],
                "processedAt": n["processedAt"],
                "paymentDate": payment_date,
                "status": n["displayFinancialStatus"],
                "tags": n["tags"],
                "gross": float(n["subtotalPriceSet"]["shopMoney"]["amount"]),
                "net": float(n["currentSubtotalPriceSet"]["shopMoney"]["amount"]),
                "refunded": float(n["totalRefundedSet"]["shopMoney"]["amount"]),
                "customer": n["customer"]["displayName"] if n["customer"] else "Unknown"
            })
        page_info = orders_data["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        page += 1
        time.sleep(0.5)  # rate limit safety
    return all_orders

ET = timezone(timedelta(hours=-4))  # Eastern (approximate; -5 in winter but close enough)

def order_et_month(iso_str):
    """Return YYYY-MM of an ISO timestamp converted to ET."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(ET)
    return dt.strftime("%Y-%m")

def build_adjustments(orders, cur_month):
    """
    Detect prior-month orders that have been refunded/partially refunded.
    Returns a list of adjustment dicts ready for the ADJUSTMENTS JS constant.
    Only looks back 3 months so the list stays manageable.
    """
    cutoff_months = 3
    # Earliest month we care about
    from datetime import date
    y, m = int(cur_month[:4]), int(cur_month[5:])
    months_back = []
    for _ in range(cutoff_months):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        months_back.append(f"{y:04d}-{m:02d}")
    oldest = min(months_back)

    adjustments = []
    for o in orders:
        order_month = order_et_month(o["paymentDate"] or o["processedAt"])
        # Only prior months within our lookback window
        if order_month >= cur_month or order_month < oldest:
            continue
        refunded = o.get("refunded", 0.0)
        gross = o.get("gross", 0.0)
        net = o.get("net", 0.0)
        if refunded <= 0:
            continue  # no refund, no adjustment needed

        tags = o.get("tags", [])
        has_matt = any(t.lower().strip() == "draft-matt" for t in tags)
        has_mike = any(t.lower().strip() == "draft-mike" for t in tags)
        rep = "both" if (has_matt and has_mike) else ("matt" if has_matt else "mike")

        original_net = gross          # subtotal before refunds
        cur_net = net                 # currentSubtotal after refunds
        comm_delta = (cur_net - original_net) * COMMISSION_RATE  # negative for refunds

        # Map month to human label used in JS (YYYY-MM)
        # fmtMonth() in JS will render it nicely
        status_orig = "paid"          # assume was paid when month closed
        status_cur = o["status"].lower().replace("_", " ")

        adjustments.append({
            "id": o["id"],
            "name": o["name"],
            "customer": o["customer"],
            "month": order_month,        # YYYY-MM of original order
            "rep": rep,
            "originalStatus": status_orig,
            "curStatus": status_cur,
            "originalNet": round(original_net, 2),
            "curNet": round(cur_net, 2),
            "commDelta": round(comm_delta, 2),
        })

    return adjustments

def update_html(infile, orders_json, adjustments_json, as_of):
    with open(infile) as f:
        html = f.read()
    html = re.sub(r'const ORDERS = \[.*?\];', lambda m: f'const ORDERS = {orders_json};', html, flags=re.DOTALL)
    html = re.sub(r'const ADJUSTMENTS = \[.*?\];', lambda m: f'const ADJUSTMENTS = {adjustments_json};', html, flags=re.DOTALL)
    html = re.sub(r'const AS_OF = "[^"]*"', lambda m: f'const AS_OF = "{as_of}"', html)
    html = re.sub(r'Data as of [^·<&]*', lambda m: f'Data as of {as_of} ', html)
    with open(infile, 'w') as f:
        f.write(html)
    print(f"  Updated {infile}", file=sys.stderr)

def main():
    print("Fetching orders from Shopify...", file=sys.stderr)
    orders = fetch_all_orders()
    print(f"Fetched {len(orders)} orders", file=sys.stderr)

    now = datetime.now()
    # Format like "September 10, 2026 at 8:04 AM ET"
    as_of = now.strftime("%B %-d, %Y at %-I:%M %p ET")
    orders_json = json.dumps(orders, separators=(',', ':'))

    # Build prior-month adjustments
    cur_month = now.strftime("%Y-%m")
    adjustments = build_adjustments(orders, cur_month)
    adjustments_json = json.dumps(adjustments, separators=(',', ':'))
    print(f"Detected {len(adjustments)} prior-month adjustment(s)", file=sys.stderr)

    print("Updating HTML files...", file=sys.stderr)
    # GitHub Pages files (served at /, /matt, /mike via djlebert.github.io/commission-tracker/)
    # Cloudflare Pages files (served at /commission-tracker, /matt-commission-tracker, /mike-commission-tracker)
    for fname in [
        "index.html",
        "matt.html",
        "mike.html",
        "commission-tracker.html",
        "matt-commission-tracker.html",
        "mike-commission-tracker.html",
    ]:
        if os.path.exists(fname):
            update_html(fname, orders_json, adjustments_json, as_of)
        else:
            print(f"  WARNING: {fname} not found (skipping)", file=sys.stderr)

    print("Done!", file=sys.stderr)

if __name__ == "__main__":
    main()
