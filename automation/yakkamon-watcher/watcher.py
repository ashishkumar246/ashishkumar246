#!/usr/bin/env python3
import base64, json, os, sys, time, hashlib
from pathlib import Path
import requests

OPENSEA_SLUG = "yakkamon-590038504"
CONTRACT = "0x6d1bc5247ca99d917d91ec52dbbb5ef6c2435107".lower()
RONIN_RPC = os.getenv("RONIN_RPC", "https://api.roninchain.com/rpc")
OUT = Path(__file__).with_name("mismatches.json")
STATE_FILE = Path(__file__).with_name("scan_state.json")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "yakkamon-mismatch-watcher/2.0"})
TIMEOUT = 25
RECHECK_AFTER = 6 * 60 * 60  # periodically recheck old verified tokens every 6h

def _retry_after(resp, default=30):
    try:
        return max(1, int(float(resp.headers.get("Retry-After", default))))
    except Exception:
        return default

def get_opensea_key():
    # Preferred: add OPENSEA_API_KEY as a GitHub Actions secret.
    env_key = os.getenv("OPENSEA_API_KEY")
    if env_key:
        return env_key.strip()

    # Fallback: temporary free key. This can be rate-limited, so a repo secret is better.
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

def fetch_bad_egg_listings(api_key):
    url = f"https://api.opensea.io/api/v2/listings/collection/{OPENSEA_SLUG}/best"
    headers = {"x-api-key": api_key}
    params = {
        "traits": json.dumps([{"traitType":"Status","value":"Bad Egg"}], separators=(",",":")),
        "limit": 200,
    }
    rows, cursor = [], None
    for _ in range(20):
        if cursor:
            params["next"] = cursor
        elif "next" in params:
            del params["next"]
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
        for k in ("token_id","tokenId","identifier"):
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
                if isinstance(v, (int,float)) or (isinstance(v,str) and v.isdigit()):
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

def listing_fingerprint(row):
    # Tracks meaningful listing changes without persisting the entire OpenSea response.
    price = extract_price(row)
    payload = json.dumps(price, sort_keys=True, separators=(",",":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]

def rpc_token_uri(token_id):
    selector = "c87b56dd"
    arg = hex(int(token_id))[2:].rjust(64, "0")
    payload = {"jsonrpc":"2.0","id":1,"method":"eth_call",
               "params":[{"to":CONTRACT,"data":"0x"+selector+arg},"latest"]}
    last = None
    for attempt in range(8):
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
    state = load_state()
    api_key = get_opensea_key()
    listings = fetch_bad_egg_listings(api_key)

    by_token, unparsed = {}, 0
    for row in listings:
        tid = extract_token_id(row)
        if not tid:
            unparsed += 1
            continue
        by_token.setdefault(tid, row)

    active_ids = set(by_token)
    # Remove old state for NFTs no longer actively listed as Bad Egg on OpenSea.
    state = {tid:rec for tid,rec in state.items() if tid in active_ids}

    new_ids, changed_ids, retry_ids, stale_ids, skipped_ids = [], [], [], [], []
    for tid, row in by_token.items():
        fp = listing_fingerprint(row)
        rec = state.get(tid)
        if rec is None:
            new_ids.append(tid)
        elif rec.get("verification_ok") is not True:
            retry_ids.append(tid)
        elif rec.get("listing_fingerprint") != fp:
            changed_ids.append(tid)
        elif now - int(rec.get("last_checked",0)) >= RECHECK_AFTER:
            stale_ids.append(tid)
        else:
            skipped_ids.append(tid)

    # Priority: unfinished work first, then newly listed, listing changes, then 6h safety rechecks.
    todo = retry_ids + new_ids + changed_ids + stale_ids
    # De-duplicate while preserving priority.
    todo = list(dict.fromkeys(todo))

    errors = []
    checked_this_run = 0
    for tid in todo:
        row = by_token[tid]
        fp = listing_fingerprint(row)
        try:
            uri = rpc_token_uri(tid)
            meta = load_metadata(uri)
            status = status_from_metadata(meta)
            state[tid] = {
                "verification_ok": True,
                "last_checked": now,
                "listing_fingerprint": fp,
                "ronin_status": status,
                "is_mismatch": (status or "").strip().lower() != "bad egg",
                "price": extract_price(row),
                "token_uri": uri,
            }
            checked_this_run += 1
        except Exception as e:
            prev = state.get(tid, {})
            prev.update({
                "verification_ok": False,
                "last_attempt": now,
                "listing_fingerprint": fp,
                "error": str(e)[:300],
            })
            state[tid] = prev
            errors.append({"token_id":tid,"error":str(e)[:300]})
        time.sleep(0.35)

    mismatches = []
    verified_active = 0
    for tid, row in by_token.items():
        rec = state.get(tid, {})
        if rec.get("verification_ok") is True:
            verified_active += 1
            if rec.get("is_mismatch") is True:
                mismatches.append({
                    "token_id": tid,
                    "opensea_status": "Bad Egg",
                    "ronin_live_metadata_status": rec.get("ronin_status"),
                    "opensea_price": extract_price(row),
                    "opensea_url": f"https://opensea.io/item/ronin/{CONTRACT}/{tid}",
                    "ronin_url": f"https://marketplace.roninchain.com/collections/yakkamon/{tid}",
                })

    pending = sorted([tid for tid in active_ids if state.get(tid,{}).get("verification_ok") is not True], key=int)
    result = {
        "collection": "Yakkamon",
        "contract": CONTRACT,
        "rule": "Actively listed on OpenSea with Status=Bad Egg, but current Ronin-chain metadata is not Bad Egg",
        "opensea_bad_egg_active_listing_rows": len(listings),
        "opensea_bad_egg_unique_active_tokens": len(by_token),
        "verified_active_tokens": verified_active,
        "verification_complete": verified_active == len(by_token),
        "pending_verification_count": len(pending),
        "pending_token_ids": pending[:100],
        "checked_this_run": checked_this_run,
        "new_tokens_found_this_run": len(new_ids),
        "changed_listings_this_run": len(changed_ids),
        "previously_verified_skipped_this_run": len(skipped_ids),
        "periodic_rechecks_this_run": len(stale_ids),
        "verification_error_count_this_run": len(errors),
        "verification_errors_this_run": errors[:50],
        "unparsed_listing_rows": unparsed,
        "mismatch_count": len(mismatches),
        "mismatches": sorted(mismatches, key=lambda x:int(x["token_id"])),
    }

    state_text = json.dumps(state, indent=2, sort_keys=True) + "\n"
    out_text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    old_state = STATE_FILE.read_text() if STATE_FILE.exists() else None
    old_out = OUT.read_text() if OUT.exists() else None
    STATE_FILE.write_text(state_text)
    OUT.write_text(out_text)
    print(out_text)
    return 2 if old_state != state_text or old_out != out_text else 0

if __name__ == "__main__":
    sys.exit(main())
