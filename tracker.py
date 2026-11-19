"""
Coachella Airbnb tracker.

Every run:
  1. Loads your saved Airbnb search (SEARCH_URL below).
  2. Compares the results to what it saw last time (state.json).
  3. Sends a push notification (ntfy) for new listings or price drops.
  4. Warns you if it breaks, tells you when it's fixed, and checks in once a day.

No packages to install: standard-library Python only.
"""

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# SETTINGS: to change your search, run it on airbnb.com and paste the new URL.
# ---------------------------------------------------------------------------
SEARCH_URL = (
    "https://www.airbnb.com/s/Coachella--CA/homes?adults=6&guests=6"
    "&checkin=2027-04-09&checkout=2027-04-12"
    "&ne_lat=33.75534135265516&sw_lat=33.610403848464436"
    "&ne_lng=-116.09677326224846&sw_lng=-116.4341018962707"
    "&search_by_map=true&search_type=user_map_move&zoom=11.85"
    "&refinement_paths%5B%5D=%2Fhomes&min_bedrooms=2"
    "&l2_property_type_ids%5B%5D=1&l2_property_type_ids%5B%5D=3"
    "&price_max=4500&price_filter_input_type=2&price_filter_num_nights=3"
    "&search_mode=regular_search&channel=EXPLORE"
)
HEARTBEAT_HOUR_PT = 10          # daily "still running" message around 10am Pacific
MAX_PAGES = 5                   # 18 listings per page, so up to 90 listings
FORGET_AFTER_HOURS = 6          # a listing must be gone this long before its return counts as NEW
FAILS_BEFORE_ALERT = 2          # failed checks in a row before you get a "broken" alert
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")   # set as a GitHub secret
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

PT = timezone(timedelta(hours=-8))  # close enough for a once-a-day check-in

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class TrackerBroken(Exception):
    """Raised when Airbnb's page doesn't look like we expect."""


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def notify(title, message, url=None, priority="default", tags=""):
    print(f"[notify] {title} | {message} | {url or ''}")
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC not set, so nothing was sent)")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
    )
    # ntfy headers must be latin-1, so keep emoji in tags instead of the title
    req.add_header("Title", title.encode("latin-1", "ignore").decode("latin-1"))
    req.add_header("Priority", priority)
    if tags:
        req.add_header("Tags", tags)
    if url:
        req.add_header("Click", url)
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # don't let a notification hiccup crash the run
        print(f"  ntfy send failed: {e}")


# ---------------------------------------------------------------------------
# Fetching + parsing Airbnb
# ---------------------------------------------------------------------------
def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        raise TrackerBroken(f"Couldn't reach Airbnb ({e.__class__.__name__})")


def extract_results(html):
    m = re.search(r'<script id="data-deferred-state-0"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        low = html.lower()
        if "captcha" in low or "px-captcha" in low or "access denied" in low:
            raise TrackerBroken("Airbnb served a bot check / captcha")
        raise TrackerBroken("Search data missing from page (Airbnb may have changed its site)")
    try:
        data = json.loads(m.group(1))
        for entry in data["niobeClientData"]:
            payload = entry[1]
            ss = payload.get("data", {}).get("presentation", {}).get("staysSearch")
            if ss and "results" in ss:
                return ss["results"]
    except Exception as e:
        raise TrackerBroken(f"Couldn't read search data ({e.__class__.__name__})")
    raise TrackerBroken("Search results block not found (Airbnb may have changed its site)")


def listing_id(r):
    raw = (r.get("demandStayListing") or {}).get("id") or ""
    try:
        decoded = base64.b64decode(raw).decode()   # "DemandStayListing:12345"
        return decoded.split(":")[-1]
    except Exception:
        return raw or None


def parse_price(r):
    line = (r.get("structuredDisplayPrice") or {}).get("primaryLine") or {}
    text = line.get("discountedPrice") or line.get("price") or line.get("accessibilityLabel") or ""
    m = re.search(r"\$([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else None


def parse_listing(r):
    lid = listing_id(r)
    if not lid:
        return None
    name = ((r.get("nameLocalized") or {}).get("localizedStringWithTranslationPreference")
            or r.get("title") or "Listing")
    beds = [x.get("body") for x in ((r.get("structuredContent") or {}).get("mapPrimaryLine") or [])
            if x.get("body")]
    return {
        "id": lid,
        "name": name,
        "type": r.get("title") or "",
        "details": " · ".join(beds),
        "price": parse_price(r),
        "rating": r.get("avgRatingLocalized") or "new",
    }


def search():
    listings, url, seen_cursors = {}, SEARCH_URL, set()
    for page in range(MAX_PAGES):
        status, html = fetch(url)
        if status != 200:
            raise TrackerBroken(f"Airbnb returned HTTP {status}" +
                                (" (likely blocked)" if status in (403, 429) else ""))
        results = extract_results(html)
        for r in results.get("searchResults") or []:
            l = parse_listing(r)
            if l:
                listings[l["id"]] = l
        cursors = ((results.get("paginationInfo") or {}).get("pageCursors")) or []
        next_cursor = next((c for c in cursors[page + 1:page + 2] if c not in seen_cursors), None)
        if not next_cursor:
            break
        seen_cursors.add(next_cursor)
        url = SEARCH_URL + "&cursor=" + urllib.parse.quote(next_cursor)
    return listings


def room_link(lid):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(SEARCH_URL).query)
    params = {
        "check_in": q.get("checkin", [""])[0],
        "check_out": q.get("checkout", [""])[0],
        "adults": q.get("adults", ["1"])[0],
    }
    return f"https://www.airbnb.com/rooms/{lid}?" + urllib.parse.urlencode(params)


def money(n):
    return f"${n:,}" if n is not None else "price ?"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"listings": {}, "broken": False, "last_heartbeat": "", "started": False}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def main():
    state = load_state()
    now_pt = datetime.now(PT)

    try:
        current = search()
    except TrackerBroken as e:
        print(f"BROKEN: {e}")
        state["fail_count"] = state.get("fail_count", 0) + 1
        # one failed check is usually a hiccup; only alert after 2 in a row, and only once
        if state["fail_count"] >= FAILS_BEFORE_ALERT and not state.get("broken"):
            notify("Coachella tracker is broken",
                   f"{e}. I'll keep retrying and tell you when it's back.",
                   priority="high", tags="warning")
            state["broken"] = True
        state["broken_reason"] = str(e)
        save_state(state)
        return 0   # exit cleanly so the state gets saved/committed

    if state.get("broken"):
        notify("Coachella tracker is working again",
               f"Back online, tracking {len(current)} listings.", tags="white_check_mark")
    state["broken"] = False
    state["fail_count"] = 0
    state.pop("broken_reason", None)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = state.get("listings", {})

    if not state.get("started"):
        cheapest = min((l["price"] for l in current.values() if l["price"]), default=None)
        lines = [f"- {l['name']} ({money(l['price'])})" for l in current.values()]
        notify("Coachella tracker started",
               f"Watching your search. {len(current)} listings right now"
               + (f", cheapest {money(cheapest)} total" if cheapest else "") + ".\n"
               + "\n".join(lines[:10]),
               url=SEARCH_URL, tags="tent")
        state["started"] = True
    else:
        for lid, l in current.items():
            if lid not in old:
                notify(f"NEW: {money(l['price'])} total - {l['type']}",
                       f"{l['name']}\n{l['details']} · ★ {l['rating']}\nTap to open in Airbnb.",
                       url=room_link(lid), priority="urgent", tags="rotating_light,house")
            elif l["price"] and old[lid].get("price") and l["price"] < old[lid]["price"]:
                notify(f"Price drop: {money(old[lid]['price'])} to {money(l['price'])}",
                       f"{l['name']}\n{l['details']}",
                       url=room_link(lid), priority="high", tags="chart_with_downwards_trend")

    # daily check-in
    today = now_pt.strftime("%Y-%m-%d")
    if state.get("last_heartbeat") != today and now_pt.hour >= HEARTBEAT_HOUR_PT:
        cheapest = min((l["price"] for l in current.values() if l["price"]), default=None)
        notify("Coachella tracker: still running",
               f"Tracking {len(current)} listings"
               + (f", cheapest {money(cheapest)} total." if cheapest else ". None match right now."),
               url=SEARCH_URL, priority="low", tags="white_check_mark")
        state["last_heartbeat"] = today

    # Remember listings for a while after they vanish. Airbnb sometimes hides a
    # listing for one check and shows it again, which shouldn't count as "new".
    memory = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FORGET_AFTER_HOURS)
    for lid, l in old.items():
        if lid not in current:
            seen = datetime.fromisoformat(l.get("last_seen", now_iso))
            if seen > cutoff:
                memory[lid] = l
    for lid, l in current.items():
        memory[lid] = {**l, "last_seen": now_iso}

    state["listings"] = memory
    state["last_run"] = now_iso
    save_state(state)
    print(f"OK: {len(current)} listings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
