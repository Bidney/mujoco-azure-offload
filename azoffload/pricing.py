"""Live VM pricing via Azure's public, unauthenticated Retail Prices API.

Falls back to config-supplied prices if the API is unreachable so the tool
never blocks on a network hiccup, but a real number is preferred because the
whole point of the pre-launch estimate is accuracy.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://prices.azure.com/api/retail/prices"


def _get(url: str):
    """GET + parse JSON, with a short backoff on HTTP 429 (the API rate-limits)."""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise


def _fetch(filter_str: str) -> list:
    items = []
    url = f"{API}?$filter={urllib.parse.quote(filter_str)}&currencyCode='USD'"
    for _ in range(8):  # bounded paging
        data = _get(url)
        items += data.get("Items", [])
        nxt = data.get("NextPageLink")
        if not nxt:
            break
        url = nxt
    return items


def get_vm_price(region: str, vm_size: str, spot: bool):
    """Return (usd_per_hour, source). Raises on no match / network error.

    Picks the cheapest Linux meter that matches the requested priority:
      - spot=True  -> a 'Spot' meter (but not 'Low Priority')
      - spot=False -> a regular meter (neither 'Spot' nor 'Low Priority')
    """
    flt = (
        f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
        f"and armSkuName eq '{vm_size}' and priceType eq 'Consumption'"
    )
    items = _fetch(flt)
    cands = []
    for it in items:
        blob = f"{it.get('productName','')} {it.get('meterName','')} {it.get('skuName','')}"
        if "Windows" in blob:
            continue
        is_spot = "Spot" in blob
        is_low = "Low Priority" in blob
        price = it.get("retailPrice", it.get("unitPrice"))
        if price is None:
            continue
        if spot and is_spot and not is_low:
            cands.append(price)
        elif (not spot) and (not is_spot) and (not is_low):
            cands.append(price)
    if not cands:
        raise RuntimeError(
            f"No matching {'spot' if spot else 'on-demand'} price for "
            f"{vm_size} in {region} from Retail Prices API."
        )
    return min(cands), "azure-retail-prices-api"
