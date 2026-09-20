#!/usr/bin/env python3
import base64, json, os, sys, time, hashlib
from pathlib import Path
import requests

OPENSEA_SLUG = "yakkamon-590038504"
CONTRACT = "0x6d1bc5247ca99d917d91ec52dbbb5ef6c2435107".lower()
RONIN_RPC = os.getenv("RONIN_RPC", "https://api.roninchain.com/rpc")

OUT = Path(__file__).with_name("mismatches.json")
STATE_FILE = Path(__file__).with_name("baseline_state.json")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "yakkamon-mismatch-watcher/3.0"})
TIMEOUT = 25
BASELINE_RECHECK_AFTER = 12 * 60 * 60  # refresh verified baseline status twice daily

def _retry_after(resp, default=30):
    try:
        return max(1, int(float(resp.headers.get("Retry-After", default))))
    except Exception:
        return default

def get_opensea_key():
    env_key = os.getenv("OPENSEA_API_KEY")
    if env_key:
        return env_key.strip()

    url = "https://api.opensea.io/api/v2/auth/keys"
    last = None
    for attempt in range(8):
        r = SESSION.post(url, timeout=TIMEOUT)
        if r.status_code in (200, 201):
            key = r.json().get("api_key")
            if key:
                return key
            raise RuntimeError("OpenSea returned no api_key")
        if r.status_code == 429:
            wait = _retry_after(r, min(30 * (attempt + 1), 180))
            print(f"OpenSea key rate-limited; waiting {wait}s", flush=True)
            time.sleep(wait)
            last = f"429 after {wait}s"
            continue
        r.raise_for_status()
    raise RuntimeError(f"Could not obtain OpenSea API key: {last}")

def _paged_get(url, headers, params, item_keys):
    rows = []
    cursor = None
    for _ in range(50):
        q = dict(params)
        if cursor:
            q["next"] = cursor
        r = SESSION.get(url, headers=headers, params=q, timeout=TIMEOUT)
        if r.status_code == 429:
            wait = _retry_after(r, 30)
            time.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        batch = []
        for key in item_keys:
            if isinstance(data.get(key), list):
                batch = data[key]
                break
        rows.extend(batch)
        cursor = data.get("next")
        if not cursor or not batch:
            break
    return rows

def fetch_all_opensea_bad_eggs(api_key):
    # All OpenSea NFTs currently carrying Status=Bad Egg, listed or not.
    url = f"https://api.opensea.io/api/v2/collection/{OPENSEA_SLUG}/nfts"
    headers = {"x-api-key": api_key}
    params = {
        "traits": json.dumps([{"traitType":"Status","value":"Bad Egg"}], separators=(",",":")),
        "limit": 200,
    }
    return _paged_get(url, headers, params, ("nfts","items","results"))

def fetch_active_bad_egg_listings(api_key):
    # Lightweight recurring query. We only use it to see which baseline mismatches are for sale.
    url = f"https://api.opensea.io/api/v2/listings/collection/{OPENSEA_SLUG}/best"
    headers = {"x-api-key": api_key}
    params = {
        "traits": json.dumps([{"traitType":"Status","value":"Bad Egg"}], separators=(",",":")),
        "limit": 200,
    }
    return _paged_get(url, headers, params, ("listings","orders","results"))

def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)

def extract_token_id(row):
    for d in walk(row):
        addr = str(d.get("contract") or d.get("contract_address") or d.get("token_address") or "").lower()
        if isinstance(d.get("contract"), dict):
            addr = str(d["contract"].get("address") or "").lower()
        if addr == CONTRACT:
            for k in ("identifier","token_id","tokenId","id"):
                v = d.get(k)
                if v is not None and str(v).isdigit():
                    return str(v)
    for d in walk(row):
        for k in ("identifier","token_id","tokenId"):
            v = d.get(k)
            if v is not None and str(v).isdigit():
                return str(v)
    return None

def extract_price(row):
    symbol, decimals, raw = None, None, None
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
                if isinstance(v,(int,float)) or (isinstance(v,str) and v.isdigit()):
                    if k in ("current_price","currentPrice","startAmount","endAmount") or (k=="value" and len(str(v)) >= 6):
                        raw = str(v)
                        break
    if raw is None:
        return None
    try:
        n = int(raw)
        decimals = 18 if decimals is None else decimals
        return {"value": n/(10**decimals), "symbol": symbol or "RON", "raw": raw, "decimals": decimals}
    except Exception:
        return {"raw": raw, "symbol": symbol or "RON"}

def rpc_token_uri(token_id):
    selector = "c87b56dd"
    arg = hex(int(token_id))[2:].rjust(64, "0")
    payload = {"jsonrpc":"2.0","id":1,"method":"eth_call",
               "params":[{"to":CONTRACT,"data":"0x"+selector+arg},"latest"]}
    last = None
    for attempt in range(9):
        r = SESSION.post(RONIN_RPC, json=payload, timeout=TIMEOUT)
        if r.status_code == 429:
            wait = _retry_after(r, min(2 ** attempt, 30))
            time.sleep(wait)
            last = f"429 after {wait}s"
            continue
        r.raise_for_status()
        result = r.json().get("result")
        if not result or result == "0x":
            raise RuntimeError(f"empty tokenURI result for #{token_id}")
        b = bytes.fromhex(result[2:])
        offset = int.from_bytes(b[:32], "big")
        length = int.from_bytes(b[offset:offset+32], "big")
        return b[offset+32:offset+32+length].decode("utf-8")
    raise RuntimeError(f"Ronin RPC rate limit persisted: {last}")

def load_metadata(uri):
    if uri.startswith("data:application/json;base64,"):
        return json.loads(base64.b64decode(uri.split(",",1)[1]).decode())
    if uri.startswith("data:application/json,"):
        return json.loads(uri.split(",",1)[1])
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
        attrs = [{"trait_type":k,"value":v} for k,v in attrs.items()]
    for a in attrs:
        if not isinstance(a, dict):
            continue
        t = str(a.get("trait_type") or a.get("traitType") or a.get("type") or "").strip().lower()
        if t == "status":
            return str(a.get("value") or "").strip()
    return None

def load_state():
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def main():
    now = int(time.time())
    api_key = get_opensea_key()

    # PHASE A: build/refresh the master OpenSea-Bad / Ronin-Good baseline.
    all_bad_rows = fetch_all_opensea_bad_eggs(api_key)
    os_bad_ids = []
    for row in all_bad_rows:
        tid = extract_token_id(row)
        if tid:
            os_bad_ids.append(tid)
    os_bad_ids = sorted(set(os_bad_ids), key=int)

    state = load_state()
    state = {tid:rec for tid,rec in state.items() if tid in set(os_bad_ids)}

    retry_ids, new_ids, stale_ids, skipped_ids = [], [], [], []
    for tid in os_bad_ids:
        rec = state.get(tid)
        if rec is None:
            new_ids.append(tid)
        elif rec.get("verification_ok") is not True:
            retry_ids.append(tid)
        elif now - int(rec.get("last_checked",0)) >= BASELINE_RECHECK_AFTER:
            stale_ids.append(tid)
        else:
            skipped_ids.append(tid)

    # Finish incomplete baseline first, then new OpenSea Bad Eggs, then periodic safety refresh.
    todo = list(dict.fromkeys(retry_ids + new_ids + stale_ids))
    errors = []
    checked_this_run = 0

    for tid in todo:
        try:
            uri = rpc_token_uri(tid)
            meta = load_metadata(uri)
            ronin_status = status_from_metadata(meta)
            state[tid] = {
                "verification_ok": True,
                "last_checked": now,
                "ronin_status": ronin_status,
                "is_baseline_mismatch": (ronin_status or "").strip().lower() != "bad egg",
                "token_uri": uri,
            }
            checked_this_run += 1
        except Exception as e:
            prev = state.get(tid, {})
            prev.update({"verification_ok":False,"last_attempt":now,"error":str(e)[:300]})
            state[tid] = prev
            errors.append({"token_id":tid,"error":str(e)[:300]})
        time.sleep(0.35)

    baseline_mismatch_ids = sorted(
        [tid for tid in os_bad_ids if state.get(tid,{}).get("verification_ok") is True
         and state.get(tid,{}).get("is_baseline_mismatch") is True],
        key=int
    )
    pending_baseline_ids = sorted(
        [tid for tid in os_bad_ids if state.get(tid,{}).get("verification_ok") is not True],
        key=int
    )
    verified_baseline_count = len(os_bad_ids) - len(pending_baseline_ids)

    # PHASE B: recurring lightweight listing check.
    listing_rows = fetch_active_bad_egg_listings(api_key)
    listed_by_token = {}
    for row in listing_rows:
        tid = extract_token_id(row)
        if tid:
            listed_by_token.setdefault(tid, row)

    listed_mismatches = []
    baseline_set = set(baseline_mismatch_ids)
    for tid in sorted(set(listed_by_token) & baseline_set, key=int):
        row = listed_by_token[tid]
        rec = state.get(tid,{})
        listed_mismatches.append({
            "token_id": tid,
            "opensea_status": "Bad Egg",
            "ronin_live_metadata_status": rec.get("ronin_status"),
            "opensea_price": extract_price(row),
            "opensea_url": f"https://opensea.io/item/ronin/{CONTRACT}/{tid}",
            "ronin_url": f"https://marketplace.roninchain.com/collections/yakkamon/{tid}",
        })

    result = {
        "collection": "Yakkamon",
        "contract": CONTRACT,
        "architecture": "Build all OpenSea-Bad/Ronin-Good baseline first; recurring scans only intersect that baseline with active OpenSea listings.",
        "opensea_bad_egg_total_unique_tokens": len(os_bad_ids),
        "baseline_verified_count": verified_baseline_count,
        "baseline_verification_complete": len(pending_baseline_ids) == 0,
        "baseline_pending_count": len(pending_baseline_ids),
        "baseline_pending_token_ids": pending_baseline_ids[:200],
        "baseline_mismatch_count": len(baseline_mismatch_ids),
        "baseline_mismatch_token_ids": baseline_mismatch_ids,
        "baseline_checked_this_run": checked_this_run,
        "baseline_new_tokens_this_run": len(new_ids),
        "baseline_skipped_verified_this_run": len(skipped_ids),
        "baseline_periodic_rechecks_this_run": len(stale_ids),
        "baseline_errors_this_run": errors[:50],
        "opensea_bad_egg_active_listing_rows": len(listing_rows),
        "opensea_bad_egg_unique_active_tokens": len(listed_by_token),
        "currently_listed_mismatch_count": len(listed_mismatches),
        "currently_listed_mismatches": listed_mismatches,
    }

    old_state = STATE_FILE.read_text() if STATE_FILE.exists() else None
    old_out = OUT.read_text() if OUT.exists() else None
    new_state = json.dumps(state, indent=2, sort_keys=True) + "\n"
    new_out = json.dumps(result, indent=2, sort_keys=True) + "\n"

    STATE_FILE.write_text(new_state)
    OUT.write_text(new_out)
    print(new_out)

    return 2 if old_state != new_state or old_out != new_out else 0

if __name__ == "__main__":
    sys.exit(main())
