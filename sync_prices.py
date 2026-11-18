#!/usr/bin/env python3
"""
Sync PRICES from the Mixin site (READ-ONLY) to your own Basalam shop.

- The Mixin side only uses GET requests. Nothing on the site is ever changed.
- DRY_RUN=1 (default): nothing is written to Basalam, only reports are produced.
- DRY_RUN=0: prices are written to Basalam, Telegram message only when something changed.

Environment variables
  MIXIN_BASE_URL        e.g. https://partzune.ir
  MIXIN_API_KEY         Mixin API key  (sent as "Authorization: Api-Key ...")
  BASALAM_TOKEN         Basalam personal token (edit-products scope)
  BASALAM_REFRESH_TOKEN optional
  BASALAM_VENDOR_ID     optional (found automatically from the token)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  DRY_RUN               "1" (default) or "0"
  MAX_CHANGE_PCT        default 40  -> bigger changes are NOT applied, only reported
  PRICE_MULTIPLIER      "" = detect automatically (10 if Basalam uses Rial, 1 if Toman)
  FUZZY_OK              "1" = also apply similar-name matches (default "0": only
                         identifier / exact-name matches or mapping_overrides.json)
  PRICE_MARKUP_PCT      default 0. Use 10 if Basalam prices should be site price +10%
  ROUND_RIAL            default 1. Use 10000 to round Basalam prices to 10,000 Rial steps
  SKIP_DISCOUNTED       default "1": items with an active Basalam discount are skipped and reported
  NOTIFY_ERRORS         "1" = also send a Telegram message on fatal errors (default "0")

Run modes (workflow runs every 5 minutes):
  - Telegram command: send "تغییر بده" (or /sync) to the bot -> sync runs now, always replies.
  - Automatic: once every 15 minutes (silent unless something changed / was blocked / failed).
  - Manual "Run workflow" button in GitHub: runs now.
"""
import asyncio
import csv
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests

# ----------------------------------------------------------------------------- config
MIXIN_BASE = os.environ.get("MIXIN_BASE_URL", "").rstrip("/")
MIXIN_KEY = os.environ.get("MIXIN_API_KEY", "")
B_TOKEN = os.environ.get("BASALAM_TOKEN", "")
B_REFRESH = os.environ.get("BASALAM_REFRESH_TOKEN", "")
VENDOR_ID = os.environ.get("BASALAM_VENDOR_ID", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY = os.environ.get("DRY_RUN", "1").strip() != "0"
MAX_PCT = float(os.environ.get("MAX_CHANGE_PCT", "40") or 40)
MULT_ENV = os.environ.get("PRICE_MULTIPLIER", "").strip()
FUZZY_OK = os.environ.get("FUZZY_OK", "0").strip() == "1"
NOTIFY_ERRORS = os.environ.get("NOTIFY_ERRORS", "0").strip() == "1"
MARKUP_PCT = float(os.environ.get("PRICE_MARKUP_PCT", "0") or 0)   # e.g. 10 -> Basalam = site price +10%
ROUND_RIAL = int(os.environ.get("ROUND_RIAL", "1") or 1)           # e.g. 10000 -> round to nearest 10,000 Rial
SKIP_DISCOUNTED = os.environ.get("SKIP_DISCOUNTED", "1").strip() != "0"  # skip items with an active Basalam discount
OVERRIDES_FILE = "mapping_overrides.json"


# ----------------------------------------------------------------------------- telegram
def tg_message(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[telegram not configured]\n" + text)
        return
    for i in range(0, len(text), 3800):
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": text[i:i + 3800]}, timeout=30)
        except Exception as e:  # never crash because of Telegram
            print("telegram error:", e)


def tg_file(path):
    if not (TG_TOKEN and TG_CHAT) or not os.path.exists(path):
        return
    try:
        with open(path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                          data={"chat_id": TG_CHAT}, files={"document": f}, timeout=60)
    except Exception as e:
        print("telegram error:", e)


def tg_check_command():
    """True if the owner sent the sync command since last check. Confirms (consumes) updates."""
    if not (TG_TOKEN and TG_CHAT):
        return False
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"timeout": 0, "allowed_updates": json.dumps(["message"])}, timeout=30)
        j = r.json()
        if not j.get("ok"):
            print("getUpdates failed:", j.get("description"))
            return False
        ups = j.get("result") or []
        if not ups:
            return False
        last = max(u["update_id"] for u in ups)
        requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                     params={"offset": last + 1, "timeout": 0}, timeout=30)  # mark as read
        for u in ups:
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != str(TG_CHAT).strip():
                continue  # ignore everyone except the owner
            t = norm(m.get("text") or "")
            if "تغییر بده" in t or t in ("sync", "/sync", "start sync"):
                return True
    except Exception as e:
        print("telegram poll error:", e)
    return False


# ----------------------------------------------------------------------------- mixin (GET only)
def mixin_get(path, params=None):
    r = requests.get(MIXIN_BASE + path, params=params, timeout=90,
                     headers={"Authorization": f"Api-Key {MIXIN_KEY}", "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def mixin_paged(path, params=None):
    out, page = [], 1
    while True:
        p = dict(params or {})
        p.update({"page": page, "page_size": 1000})
        j = mixin_get(path, p)
        out += j.get("data") or []
        if not (j.get("pagination") or {}).get("has_next"):
            break
        page += 1
    return out


def fetch_site():
    fields = "id,name,price,compare_at_price,has_variants,available,barcode,product_identifier"
    try:
        products = mixin_paged("/api/v4/products/", {"fields": fields})
    except requests.HTTPError:
        products = mixin_paged("/api/v4/products/")
    variants = {}
    try:
        for v in mixin_paged("/api/v4/product-variants/"):
            variants.setdefault(v.get("product_id"), []).append(v)
    except Exception as e:
        print("variants fetch failed:", e)
    return products, variants


# ----------------------------------------------------------------------------- text helpers
_FIX = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"})
_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def norm(s):
    s = str(s or "").translate(_FIX).translate(_DIG).lower().replace("\u200c", " ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def key(s):
    return norm(s).replace(" ", "")


def latin_digit_tokens(s):
    return sorted(re.findall(r"[a-z]*\d+[a-z]*|[a-z]+", norm(s)))


def position(s):
    n = norm(s)
    has_f, has_r = ("جلو" in n), ("عقب" in n)
    return ("F" if has_f else "") + ("R" if has_r else "")


def flat_strings(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("id", "image", "pricing_rule") or isinstance(v, (int, float, bool)) or v is None:
                continue
            flat_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            flat_strings(v, out)
    return out


def label_tokens(strings):
    return set(norm(" ".join(strings)).split())


# ----------------------------------------------------------------------------- matching
def match_products(site, bas, overrides):
    """returns {site_id: (bas_id, how)}   how in override|id|exact|fuzzy"""
    result, used_b = {}, set()
    bmap = {b["id"]: b for b in bas}
    smap = {s["id"]: s for s in site}

    for sid, bid in overrides.items():
        sid = int(sid)
        if bid is None:
            result[sid] = (None, "ignored")
        elif sid in smap and int(bid) in bmap:
            result[sid] = (int(bid), "override")
            used_b.add(int(bid))

    def unmatched_s():
        return [s for s in site if s["id"] not in result]

    def unmatched_b():
        return [b for b in bas if b["id"] not in used_b]

    # 1) identifiers (barcode / product_identifier  <->  sku / barcode)
    ids_b = {}
    for b in unmatched_b():
        for k in (b.get("sku"), b.get("barcode")):
            k = key(k)
            if len(k) >= 3:
                ids_b.setdefault(k, set()).add(b["id"])
    ids_s = {}
    for s in unmatched_s():
        for k in (s.get("product_identifier"), s.get("barcode")):
            k = key(k)
            if len(k) >= 3:
                ids_s.setdefault(k, set()).add(s["id"])
    for k, sset in ids_s.items():
        bset = ids_b.get(k)
        if bset and len(sset) == 1 and len(bset) == 1:
            sid, bid = next(iter(sset)), next(iter(bset))
            if sid not in result and bid not in used_b:
                result[sid] = (bid, "id")
                used_b.add(bid)

    # 2) exact normalised name
    nb = {}
    for b in unmatched_b():
        nb.setdefault(key(b.get("title")), []).append(b["id"])
    ns = {}
    for s in unmatched_s():
        ns.setdefault(key(s.get("name")), []).append(s["id"])
    for k, sids in ns.items():
        bids = nb.get(k)
        if k and bids and len(sids) == 1 and len(bids) == 1:
            result[sids[0]] = (bids[0], "exact")
            used_b.add(bids[0])

    # 3) similar names (same numbers/latin tokens, same front/rear, mutual best, unique)
    cand = {}
    for s in unmatched_s():
        sk, st, sp = key(s.get("name")), latin_digit_tokens(s.get("name")), position(s.get("name"))
        scores = []
        for b in unmatched_b():
            if latin_digit_tokens(b.get("title")) != st or position(b.get("title")) != sp:
                continue
            scores.append((SequenceMatcher(None, sk, key(b.get("title"))).ratio(), b["id"]))
        scores.sort(reverse=True)
        if scores and scores[0][0] >= 0.93 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 0.03):
            cand[s["id"]] = scores[0][1]
    rev = {}
    for sid, bid in cand.items():
        rev.setdefault(bid, []).append(sid)
    for sid, bid in cand.items():
        if len(rev[bid]) == 1:
            result[sid] = (bid, "fuzzy")
    return result


def detect_multiplier(site_by_id, bas_by_id, matches):
    if MULT_ENV:
        return int(MULT_ENV)
    ratios = []
    for sid, (bid, how) in matches.items():
        if how in ("id", "exact", "override") and bid:
            sp = site_by_id[sid].get("price") or 0
            bp = current_price(bas_by_id[bid]) if not is_discounted(bas_by_id[bid]) else 0
            if sp > 0 and bp > 0:
                ratios.append(bp / sp)
    if len(ratios) < 5:
        raise RuntimeError(f"cannot detect price unit: only {len(ratios)} reliable matches "
                           "(set PRICE_MULTIPLIER=10 for Rial or 1 for Toman)")
    med = statistics.median(ratios)
    if 4 <= med <= 25:
        return 10
    if 0.4 <= med <= 2.5:
        return 1
    raise RuntimeError(f"price unit unclear (median Basalam/site ratio = {med:.2f}); "
                       "set PRICE_MULTIPLIER")


def adjust(v):
    """site price (already x unit multiplier) -> target Basalam price"""
    v = v * (1 + MARKUP_PCT / 100.0)
    if ROUND_RIAL > 1:
        v = round(v / ROUND_RIAL) * ROUND_RIAL
    return int(round(v))


def current_price(o):
    """Basalam: 'primary_price' only exists when a discount is active; otherwise 'price' is the real price"""
    return o.get("primary_price") or o.get("price") or 0


def is_discounted(o):
    pp, pr = o.get("primary_price"), o.get("price")
    return bool(o.get("discount")) or bool(pp and pr and pp != pr)


# ----------------------------------------------------------------------------- planning
def plan(site, variants, bas, matches, mult):
    smap = {s["id"]: s for s in site}
    bmap = {b["id"]: b for b in bas}
    rows, changes = [], []
    for s in site:
        sid = s["id"]
        bid, how = matches.get(sid, (None, "none"))
        row = {"site_id": sid, "site_name": s.get("name"), "basalam_id": bid, "basalam_title": "",
               "match": how, "site_price": s.get("price"), "basalam_price": "", "action": "", "note": ""}
        if how == "ignored":
            row["action"] = "ignored (override)"
            rows.append(row)
            continue
        if not bid:
            row["action"] = "not on Basalam"
            rows.append(row)
            continue
        b = bmap[bid]
        row["basalam_title"] = b.get("title")
        row["basalam_price"] = b.get("primary_price")
        if how == "fuzzy" and not FUZZY_OK:
            row["action"] = "needs approval (similar name)"
            rows.append(row)
            continue
        row["basalam_price"] = current_price(b)
        if is_discounted(b) and SKIP_DISCOUNTED:
            row["action"] = "skipped (Basalam discount active)"
            rows.append(row)
            continue

        site_vars = variants.get(sid, []) if s.get("has_variants") else []
        bas_vars = b.get("variant") or [] if b.get("has_variation") else []

        if not site_vars and not bas_vars:
            price = int(round(s.get("price") or 0))
            if price <= 0:
                row["action"] = "skipped (no price on site)"
            else:
                target = adjust(price * mult)
                old = current_price(b)
                if old == target:
                    row["action"] = "same"
                elif old and abs(target - old) / old * 100 > MAX_PCT:
                    row["action"] = f"BLOCKED (change > {MAX_PCT:.0f}%)"
                else:
                    row["action"] = "change"
                    changes.append({"site_id": sid, "name": s.get("name"), "bid": bid,
                                    "old": old, "new": target, "variants": None})
            rows.append(row)
            continue

        if bool(site_vars) != bool(bas_vars):
            row["action"] = "skipped (variants differ between site and Basalam)"
            rows.append(row)
            continue

        # variants: pair by label tokens, must be a clean 1:1 mapping
        sv = [(v, label_tokens(flat_strings(v.get("attributes") or []))) for v in site_vars]
        bv = []
        for v in bas_vars:
            labels = []
            for p in v.get("properties") or []:
                val = (p.get("value") or {})
                labels.append(val.get("title") or val.get("value") or "")
            bv.append((v, label_tokens(labels)))
        pairs, ok = [], len(sv) == len(bv)
        used = set()
        if ok:
            for v, st in sv:
                sc = sorted(((len(st & bt) / max(len(st | bt), 1), i) for i, (_, bt) in enumerate(bv)),
                            reverse=True)
                if not sc or sc[0][0] == 0 or (len(sc) > 1 and sc[0][0] == sc[1][0]) or sc[0][1] in used:
                    ok = False
                    break
                used.add(sc[0][1])
                pairs.append((v, bv[sc[0][1]][0]))
        if not ok:
            row["action"] = "skipped (could not pair variants safely)"
            rows.append(row)
            continue
        vchg, notes = [], []
        blocked = False
        for v, bvv in pairs:
            price = int(round(v.get("price") or 0))
            if price <= 0:
                continue
            target, old = adjust(price * mult), current_price(bvv)
            if is_discounted(bvv) and SKIP_DISCOUNTED:
                blocked = True
                notes.append("discount")
                continue
            if old == target:
                continue
            if old and abs(target - old) / old * 100 > MAX_PCT:
                blocked = True
            vchg.append({"vid": bvv["id"], "old": old, "new": target})
        if blocked and "discount" in notes:
            row["action"] = "skipped (Basalam discount active on a variant)"
        elif blocked:
            row["action"] = f"BLOCKED (a variant changes > {MAX_PCT:.0f}%)"
        elif vchg:
            row["action"] = f"change ({len(vchg)} variants)"
            changes.append({"site_id": sid, "name": s.get("name"), "bid": bid, "old": None,
                            "new": None, "variants": vchg})
        else:
            row["action"] = "same"
        rows.append(row)
    return rows, changes


# ----------------------------------------------------------------------------- basalam
async def basalam_session():
    from basalam_sdk import BasalamClient, PersonalToken
    try:
        auth = PersonalToken(token=B_TOKEN, refresh_token=B_REFRESH)
    except TypeError:
        auth = PersonalToken(token=B_TOKEN)
    return BasalamClient(auth=auth)


async def fetch_basalam(client):
    from basalam_sdk.core.models import GetVendorProductsSchema
    vid = VENDOR_ID
    if not vid:
        me = await client.core.get_current_user()
        vid = me.vendor.id
    vid = int(vid)
    items, page, total = [], 1, None
    while True:
        r = await client.core.get_vendor_products(
            vid, query_params=GetVendorProductsSchema(page=page, per_page=100))
        total = r.total_count if r.total_count is not None else total
        data = r.data or []
        items += [d.model_dump(exclude_none=True) for d in data]
        if not data or page >= (r.total_page or 1):
            break
        page += 1
    return vid, items, total


async def apply_changes(client, vid, changes):
    from basalam_sdk.core.models import (BatchUpdateProductsRequest, UpdateProductRequestItem,
                                         UpdateVariantRequestItem)
    done, failed = [], []
    for i in range(0, len(changes), 40):
        chunk = changes[i:i + 40]
        data = []
        for c in chunk:
            if c["variants"]:
                data.append(UpdateProductRequestItem(
                    id=c["bid"],
                    variants=[UpdateVariantRequestItem(id=v["vid"], primary_price=v["new"])
                              for v in c["variants"]]))
            else:
                data.append(UpdateProductRequestItem(id=c["bid"], primary_price=c["new"]))
        try:
            res = await client.core.update_bulk_products(
                vid, BatchUpdateProductsRequest(data=data), continue_on_error=True)
        except Exception as e:
            for c in chunk:
                failed.append((c, str(e)[:150]))
            continue
        by_id = {r.id: r for r in (res or [])}
        for c in chunk:
            r = by_id.get(c["bid"])
            if r is not None and r.has_error:
                failed.append((c, (r.error_message or "error")[:150]))
            else:
                done.append(c)
    return done, failed


# ----------------------------------------------------------------------------- main
def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


MANUAL = False


async def main():
    global MANUAL
    now_min = datetime.now(timezone.utc).minute
    from_button = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    cmd = tg_check_command()
    auto_turn = now_min % 15 < 5
    if not (cmd or from_button or auto_turn):
        print("nothing to do this round")
        return
    MANUAL = cmd or from_button
    if cmd:
        tg_message("⏳ دستور گرفتم، دارم قیمت‌ها رو هماهنگ می‌کنم...")
    for name, val in (("MIXIN_BASE_URL", MIXIN_BASE), ("MIXIN_API_KEY", MIXIN_KEY), ("BASALAM_TOKEN", B_TOKEN)):
        if not val:
            raise RuntimeError(f"missing secret/variable: {name}")
    overrides = {}
    if os.path.exists(OVERRIDES_FILE):
        overrides = json.load(open(OVERRIDES_FILE, encoding="utf-8"))

    site, variants = fetch_site()
    client = await basalam_session()
    vid, bas, total = await fetch_basalam(client)
    print(f"site products: {len(site)} | basalam products: {len(bas)} (reported {total}) | vendor {vid}")

    matches = match_products(site, bas, overrides)
    mult = detect_multiplier({s['id']: s for s in site}, {b['id']: b for b in bas}, matches)
    print("price multiplier (site toman -> basalam unit):", mult)
    rows, changes = plan(site, variants, bas, matches, mult)

    write_csv("mapping_report.csv", rows)
    probe = {"site_product": site[:2], "site_variant": next(iter(variants.values()), [])[:2],
             "basalam_product": bas[:1],
             "basalam_variant_product": next((b for b in bas if b.get("variant")), None)}
    json.dump(probe, open("probe.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)

    stats = {}
    for r in rows:
        k = r["action"].split(" (")[0] if r["action"].startswith(("skipped", "BLOCKED", "ignored")) else r["action"].split(" (")[0]
        stats[k] = stats.get(k, 0) + 1
    print("summary:", stats)
    blocked = [r for r in rows if r["action"].startswith("BLOCKED")]

    def line(c):
        if c["variants"]:
            return f"• {c['name'][:42]} ({len(c['variants'])} تنوع)"
        return f"• {c['name'][:42]}: {c['old'] // mult:,} ← {c['new'] // mult:,}"

    if DRY:
        if not MANUAL:
            print("dry run (auto): report saved, no telegram message")
            return
        msg = ["🧪 اجرای آزمایشی (چیزی تو باسلام تغییر نکرد)",
               f"محصولات سایت: {len(site)} | باسلام: {len(bas)}",
               f"قیمت‌هایی که تغییر می‌کنن: {len(changes)}",
               f"جفت‌شده با شناسه/نام دقیق: {sum(1 for v in matches.values() if v[1] in ('id','exact','override'))}",
               f"جفت‌شده با نام مشابه (نیاز به تأیید): {sum(1 for v in matches.values() if v[1] == 'fuzzy')}",
               f"بدون جفت تو باسلام: {sum(1 for r in rows if r['action'] == 'not on Basalam')}",
               f"مسدود شده (تغییر بیش از {MAX_PCT:.0f}٪): {len(blocked)}"]
        tg_message("\n".join(msg))
        tg_file("mapping_report.csv")
        tg_file("probe.json")
        return

    done, failed = (await apply_changes(client, vid, changes)) if changes else ([], [])
    if done or failed or blocked:
        out = []
        if done:
            out.append(f"✅ قیمت {len(done)} محصول تو باسلام عوض شد:")
            out += [line(c) for c in done[:30]]
            if len(done) > 30:
                out.append(f"... و {len(done) - 30} مورد دیگه")
        if failed:
            out.append(f"⚠️ {len(failed)} محصول عوض نشد:")
            out += [f"• {c['name'][:40]} — {why}" for c, why in failed[:15]]
        if blocked:
            out.append(f"🛑 {len(blocked)} محصول به‌خاطر تغییر خیلی زیاد (بیش از {MAX_PCT:.0f}٪) دست نخورد:")
            out += [f"• {r['site_name'][:40]}: {r['basalam_price']} ← قیمت سایت {r['site_price']}" for r in blocked[:10]]
        tg_message("\n".join(out))
    elif MANUAL:
        tg_message("✅ قیمت‌ها از قبل با سایت یکی بود، تغییری لازم نشد.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("FATAL:", repr(e))
        if NOTIFY_ERRORS and (MANUAL or datetime.now(timezone.utc).minute < 15):
            tg_message(f"❌ خطا در هماهنگ‌سازی قیمت‌ها: {str(e)[:300]}")
        sys.exit(1)
