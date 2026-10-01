"""Check Ginzan Onsen ryokan for vacancies and push new ones to ntfy.

Sources: Rakuten Travel (official API), Jalan (plan pages), and each
ryokan's own booking engine (nj-yoyaku.net, reserve.489ban.net).

Usage:
  python check.py            # check once, notify on newly bookable rooms
  python check.py --probe    # also save raw pages under probe/ for debugging
  python check.py --test-notify
"""
import json
import os
import re
import sys
import time
from datetime import date

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
STATE_PATH = os.path.join(ROOT, "state.json")
if os.environ.get("CHECK_DATES"):  # e.g. "2026-11-10,2026-11-11" for test runs
    from datetime import timedelta
    CONFIG["nights"] = [{"checkin": d, "checkout": str(date.fromisoformat(d) + timedelta(days=1))}
                        for d in os.environ["CHECK_DATES"].split(",")]
PROBE = "--probe" in sys.argv
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})


def save_probe(name, text):
    if not PROBE:
        return
    os.makedirs(os.path.join(ROOT, "probe"), exist_ok=True)
    with open(os.path.join(ROOT, "probe", name), "w", encoding="utf-8") as f:
        f.write(text)


def get(url, **kw):
    time.sleep(1.5)  # stay gentle: one request at a time with a pause
    r = session.get(url, timeout=30, **kw)
    return r


def text_of(r):
    """Decode a response, honouring the page's own charset (Jalan is Shift_JIS)."""
    head = r.content[:2000].decode("ascii", "ignore").lower()
    if "shift_jis" in head or "shift-jis" in head or "sjis" in head:
        return r.content.decode("cp932", "replace")
    if "euc-jp" in head:
        return r.content.decode("euc_jp", "replace")
    return r.content.decode("utf-8", "replace")


# ---------------------------------------------------------------- Rakuten

def check_rakuten(night):
    app_id = os.environ.get("RAKUTEN_APP_ID")
    if not app_id:
        return [], "RAKUTEN_APP_ID not set"
    hotels = {h["rakuten"]: h for h in CONFIG["hotels"] if h.get("rakuten")}
    params = {
        "applicationId": app_id,
        "format": "json",
        "formatVersion": 2,
        "hotelNo": ",".join(str(n) for n in hotels),
        "checkinDate": night["checkin"],
        "checkoutDate": night["checkout"],
        "adultNum": CONFIG["adults"],
        "roomNum": CONFIG["rooms"],
        "hits": 30,
    }
    if os.environ.get("RAKUTEN_ACCESS_KEY"):
        params["accessKey"] = os.environ["RAKUTEN_ACCESS_KEY"]
    headers = {}
    if os.environ.get("RAKUTEN_REFERER"):
        headers["Referer"] = os.environ["RAKUTEN_REFERER"]
    endpoints = [
        "https://openapi.rakuten.co.jp/engine/api/Travel/VacantHotelSearch/20170426",
        "https://app.rakuten.co.jp/services/api/Travel/VacantHotelSearch/20170426",
    ]
    last_err = None
    for ep in endpoints:
        time.sleep(1.5)
        r = session.get(ep, params=params, headers=headers, timeout=30)
        save_probe(f"rakuten_{night['checkin']}.json", r.text)
        if r.status_code == 404 and "not_found" in r.text:
            return [], None  # API answers 404 when nothing is vacant
        if r.ok:
            break
        last_err = f"HTTP {r.status_code}: {r.text[:200]}"
    else:
        return [], last_err

    found = []
    for basic in _find_key(r.json(), "hotelBasicInfo"):
        no = basic.get("hotelNo")
        h = hotels.get(no)
        if not h:
            continue
        found.append({
            "hotel": h["name"],
            "source": "楽天",
            "price": basic.get("hotelMinCharge"),
            "url": basic.get("planListUrl") or f"https://hotel.travel.rakuten.co.jp/hotelinfo/plan/{no}",
        })
    return found, None


def _find_key(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            else:
                yield from _find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_key(v, key)


# ------------------------------------------------------------------ Jalan

JALAN_FULL = ["ご利用できるプランがない", "条件に合う宿泊プランはありません"]


def check_jalan(night):
    d = date.fromisoformat(night["checkin"])
    found, errors = [], []
    for h in CONFIG["hotels"]:
        if not h.get("jalan"):
            continue
        url = (f"https://www.jalan.net/yad{h['jalan']}/plan/?stayYear={d.year}"
               f"&stayMonth={d.month}&stayDay={d.day}&stayCount=1"
               f"&roomCount={CONFIG['rooms']}&adultNum={CONFIG['adults']}")
        try:
            r = get(url)
        except requests.RequestException as e:
            errors.append(f"{h['name']}: {e}")
            continue
        html = text_of(r)
        save_probe(f"jalan_{h['jalan']}_{night['checkin']}.html", html)
        if not r.ok:
            errors.append(f"{h['name']}: HTTP {r.status_code}")
            continue
        if "料金・宿泊プラン" not in html:
            errors.append(f"{h['name']}: unexpected page (blocked?)")
            continue
        if jalan_has_rooms(html):
            found.append({"hotel": h["name"], "source": "Jalan", "url": url,
                          "price": _first_price(html)})
    return found, "; ".join(errors) or None


def jalan_has_rooms(html):
    if any(x in html for x in JALAN_FULL):
        return False
    return re.search(r"\d+\s*件の宿泊プランがありました", _plain(html)) is not None


def _plain(html):
    html = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", "", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _first_price(html):
    """Lowest per-adult price shown after the plan count (Jalan)."""
    t = _plain(html)
    t = t[t.find("件の宿泊プランがありました"):]
    m = re.search(r"([\d,]{5,})円", t)
    return m.group(1) if m else None


# -------------------------------------------------------- own booking sites

def check_own(night):
    found, errors = [], []
    for h in CONFIG["hotels"]:
        own = h.get("own")
        if not own:
            continue
        try:
            fn = {"nj": _own_nj, "489ban": _own_489ban}[own["type"]]
            hit = fn(own["id"], night)
        except Exception as e:  # keep the other sources running
            errors.append(f"{h['name']}: {e}")
            continue
        if hit:
            found.append({"hotel": h["name"], "source": "官網", **hit})
    return found, "; ".join(errors) or None


def _own_nj(slug, night):
    """nj-yoyaku.net month calendar: each day cell is "<day><br />mark", × = full."""
    d = date.fromisoformat(night["checkin"])
    url = (f"https://www.nj-yoyaku.net/{slug}/re_calendar.ashx"
           f"?TDT={d.year}{d.month:02d}01&NZ={CONFIG['adults']}")
    r = get(url)
    html = text_of(r)
    save_probe(f"nj_{slug}_{night['checkin']}_cal.html", html)
    r.raise_for_status()
    if f"{d.year}年{d.month}月" not in html:
        raise ValueError("calendar did not return the requested month")
    m = re.search(rf">{d.day}<br\s*/?>([^<]*)<", html)
    if not m:
        raise ValueError(f"day {d.day} not found in calendar")
    mark = m.group(1).strip().replace("\u3000", "")
    if not mark or mark in ("×", "✕", "-", "－"):
        return None
    return {"url": f"https://www.nj-yoyaku.net/{slug}/plan.aspx?PID=-1",
            "price": None, "note": f"官網日曆標示「{mark}」"}


def _own_489ban(slug, night):
    """489ban: /plan/novacancy lists the dates that still have rooms."""
    base = f"https://reserve.489ban.net/client/{slug}/0"
    r = get(f"{base}/plan/novacancy?planType=1",
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{base}/plan"})
    save_probe(f"489ban_{slug}_novacancy.json", r.text)
    r.raise_for_status()
    open_dates = r.json()
    if night["checkin"] not in open_dates:
        return None
    return {"url": (f"{base}/plan/search?date={night['checkin']}&numberOfNights=1"
                    f"&roomCount={CONFIG['rooms']}"),
            "price": None, "note": "官網空房日曆顯示可訂"}


# ------------------------------------------------------------- notifying

def notify(title, message, click=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or PROBE:
        print("NTFY_TOPIC not set; would notify:", title, message)
        return
    body = {"topic": topic, "title": title, "message": message,
            "priority": 5, "tags": ["hotsprings"]}
    if click:
        body["click"] = click
    requests.post(os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
                  data=json.dumps(body).encode("utf-8"), timeout=30)


def main():
    if "--test-notify" in sys.argv:
        notify("銀山溫泉監控測試", "如果你看到這則通知，手機推播已經設定好了。")
        return

    try:
        state = json.load(open(STATE_PATH, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"open": {}}

    now_open, errors = {}, []
    for night in CONFIG["nights"]:
        for name, fn in [("楽天", check_rakuten), ("Jalan", check_jalan), ("官網", check_own)]:
            try:
                found, err = fn(night)
            except Exception as e:
                found, err = [], repr(e)
            if err:
                errors.append(f"{name} {night['checkin']}: {err}")
            for f in found:
                key = f"{f['source']}|{f['hotel']}|{night['checkin']}"
                now_open[key] = {**f, "night": night}

    new = [v for k, v in now_open.items() if k not in state["open"]]
    for f in new:
        n = f["night"]
        price = f" 每人約 {f['price']} 円起" if f.get("price") else ""
        notify(f"有空房！{f['hotel']} {n['checkin'][5:]}→{n['checkout'][5:]}",
               f"{f['source']}{price}{('，' + f['note']) if f.get('note') else ''}，點開直接去訂。", f["url"])
        print("NEW:", f["hotel"], f["source"], n["checkin"], f["url"])

    for e in errors:
        print("ERROR:", e)
    print(f"checked at {time.strftime('%Y-%m-%d %H:%M:%S')}, open={len(now_open)}, new={len(new)}")

    state["open"] = {k: v["url"] for k, v in now_open.items()}
    # A weekly change keeps GitHub from pausing the schedule for inactivity
    state["week"] = date.today().strftime("%G-W%V")
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
