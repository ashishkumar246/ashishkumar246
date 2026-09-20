#!/usr/bin/env python3
import base64, json, os, re, sys, time
from pathlib import Path
from urllib.parse import quote

import requests

OPENSEA_SLUG = "yakkamon-590038504"
CONTRACT = "0x6d1bc5247ca99d917d91ec52dbbb5ef6c2435107".lower()
RONIN_RPC = os.getenv("RONIN_RPC", "https://api.roninchain.com/rpc")
OUT = Path(__file__).with_name("mismatches.json")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "yakkamon-mismatch-watcher/1.0"})
TIMEOUT = 25

def get_opensea_key():
    r = SESSION.post("https://api.opensea.io/api/v2/auth/keys", timeout=TIMEOUT)
    r.raise_for_status()
    key = r.json().get("api_key")
    if not key:
        raise RuntimeError("OpenSea did not return an api_key")
    return key

def fetch_bad_egg_listings(api_key):
    url = f"https://api.opensea.io/api/v2/listings/collection/{OPENSEA_SLUG}/best"
    headers = {"x-api-key": api_key}
    params = {
        "traits": json.dumps([{"traitType":"Status","value":"Bad Egg"}], separators=(",",":")),
        "limit": 200,
    }
    rows = []
    cursor = None
    for _ in range(20):
        if cursor:
            params["next"] = cursor
        r = SESSION.get(url, headers=headers, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        batch = data.get("listings") or data.get("orders") or data.get("results") or []
        rows.extend(batch)
        cursor = data.get("next")
        if not cursor or not batch:
            break
    return rows

def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)

def extract_token_id(row):
    # Prefer objects that explicitly identify our NFT contract.
    for d in walk(row):
        addr = str(d.get("contract") or d.get("contract_address") or d.get("token_address") or "").lower()
        if isinstance(d.get("contract"), dict):
            addr = str(d["contract"].get("address") or "").lower()
        if addr == CONTRACT:
            for k in ("identifier","token_id","tokenId","id"):
                v = d.get(k)
                if v is not None and str(v).isdigit():
                    return str(v)
    # OpenSea collection listing responses commonly expose an NFT identifier.
    for d in walk(row):
        for k in ("token_id","tokenId","identifier"):
            v = d.get(k)
            if v is not None and str(v).isdigit():
                return str(v)
    return None

def extract_price(row):
    symbol = None
    decimals = None
    raw = None
    for d in walk(row):
        if symbol is None and isinstance(d.get("currency"), str):
            symbol = d.get("currency")
        if symbol is None and isinstance(d.get("symbol"), str):
            symbol = d.get("symbol")
        if decimals is None and isinstance(d.get("decimals"), int):
            decimals = d.get("decimals")
        if raw is None:
            for k in ("value","current_price","currentPrice","startAmount","endAmount"):
                v = d.get(k)
                if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
                    # Prefer realistically large integer amounts; avoid random IDs.
                    if k in ("current_price","currentPrice","startAmount","endAmount") or (k=="value" and len(str(v)) >= 6):
                        raw = str(v)
                        break
    if raw is None:
        return None
    try:
        n = int(raw)
        if decimals is None:
            decimals = 18
        value = n / (10 ** decimals)
        return {"value": value, "symbol": symbol or "RON", "raw": raw, "decimals": decimals}
    except Exception:
        return {"raw": raw, "symbol": symbol or "RON"}

def rpc_token_uri(token_id):
    selector = "c87b56dd"  # tokenURI(uint256)
    arg = hex(int(token_id))[2:].rjust(64, "0")
    payload = {"jsonrpc":"2.0","id":1,"method":"eth_call",
               "params":[{"to":CONTRACT,"data":"0x"+selector+arg},"latest"]}
    r = SESSION.post(RONIN_RPC, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    result = r.json().get("result")
    if not result or result == "0x":
        raise RuntimeError(f"empty tokenURI result for #{token_id}")
    b = bytes.fromhex(result[2:])
    if len(b) < 64:
        raise RuntimeError("short ABI response")
    offset = int.from_bytes(b[:32], "big")
    length = int.from_bytes(b[offset:offset+32], "big")
    return b[offset+32:offset+32+length].decode("utf-8")

def load_metadata(uri):
    if uri.startswith("data:application/json;base64,"):
        return json.loads(base64.b64decode(uri.split(",",1)[1]).decode())
    if uri.startswith("data:application/json,"):
        return json.loads(uri.split(",",1)[1])
    urls = []
    if uri.startswith("ipfs://"):
        p = uri[len("ipfs://"):].lstrip("/")
        urls = [f"https://ipfs.io/ipfs/{p}", f"https://cloudflare-ipfs.com/ipfs/{p}"]
    else:
        urls = [uri]
    last = None
    for url in urls:
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise RuntimeError(f"metadata fetch failed: {last}")

def status_from_metadata(meta):
    attrs = meta.get("attributes") or meta.get("traits") or []
    if isinstance(attrs, dict):
        attrs = [{"trait_type": k, "value": v} for k,v in attrs.items()]
    for a in attrs:
        if not isinstance(a, dict):
            continue
        t = str(a.get("trait_type") or a.get("traitType") or a.get("type") or "").strip().lower()
        if t == "status":
            return str(a.get("value") or "").strip()
    return None

def main():
    api_key = get_opensea_key()
    listings = fetch_bad_egg_listings(api_key)

    by_token = {}
    unparsed = 0
    for row in listings:
        tid = extract_token_id(row)
        if not tid:
            unparsed += 1
            continue
        # Keep one listing per token; endpoint is price-sorted, so first is best.
        by_token.setdefault(tid, row)

    mismatches = []
    verification_errors = []
    for i, (tid, row) in enumerate(sorted(by_token.items(), key=lambda x: int(x[0]))):
        try:
            uri = rpc_token_uri(tid)
            meta = load_metadata(uri)
            current_status = status_from_metadata(meta)
            if (current_status or "").strip().lower() != "bad egg":
                mismatches.append({
                    "token_id": tid,
                    "opensea_status": "Bad Egg",
                    "ronin_live_metadata_status": current_status,
                    "opensea_price": extract_price(row),
                    "opensea_url": f"https://opensea.io/item/ronin/{CONTRACT}/{tid}",
                    "ronin_url": f"https://marketplace.roninchain.com/collections/yakkamon/{tid}",
                    "token_uri": uri,
                })
        except Exception as e:
            verification_errors.append({"token_id": tid, "error": str(e)[:300]})
        if i and i % 50 == 0:
            time.sleep(0.2)

    result = {
        "collection": "Yakkamon",
        "contract": CONTRACT,
        "rule": "Active OpenSea listing is tagged Bad Egg, but live Ronin token metadata is not Bad Egg",
        "opensea_bad_egg_active_listing_rows": len(listings),
        "unique_tokens_checked": len(by_token),
        "unparsed_listing_rows": unparsed,
        "verification_error_count": len(verification_errors),
        "verification_errors": verification_errors[:50],
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    previous = OUT.read_text() if OUT.exists() else None
    OUT.write_text(text)
    print(text)
    return 0 if previous == text else 2

if __name__ == "__main__":
    sys.exit(main())
