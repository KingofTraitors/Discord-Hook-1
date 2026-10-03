"""Posts MTGGoldfish Challenge Top 8 + metagame to Discord webhooks.

Reads config.json, remembers processed tournaments in state/<server-name>.json.
"""
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.mtggoldfish.com"
STATE_DIR = Path("state")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
TOURNAMENT_HREF = re.compile(r"/tournament/(\d+)(?:[#?].*)?$")
PCT_COUNT = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*\((\d+)\)")


# ---------------------------------------------------------------- helpers
def fetch(session, url, method="GET", **kwargs):
    """HTTP request with a few retries. Raises RuntimeError on failure."""
    last = "unknown error"
    for attempt in range(3):
        try:
            r = session.request(method, url, timeout=30, **kwargs)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code} for {url}: {r.text[:200]!r}"
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(last)


def esc(text):
    """Escape Discord markdown characters (e.g. _must_be_nice)."""
    return re.sub(r"([\\*_`~|>\[\]()])", r"\\\1", text)


def send_discord(webhook, content):
    content = content[:1990]
    for _ in range(3):
        r = requests.post(
            webhook, json={"content": content, "flags": 4}, timeout=30
        )  # flags 4 = no link preview boxes
        if r.status_code in (200, 204):
            return
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        raise RuntimeError(f"Discord HTTP {r.status_code}: {r.text[:200]}")
    raise RuntimeError("Discord kept rate-limiting us")


def load_state(path):
    if path.exists():
        return json.loads(path.read_text())
    return {"processed": [], "signatures": []}


def save_state(path, state):
    STATE_DIR.mkdir(exist_ok=True)
    state["processed"] = state["processed"][-300:]
    state["signatures"] = state["signatures"][-300:]
    path.write_text(json.dumps(state, indent=1))


# ------------------------------------------------- finding new Challenges
def find_challenges(session, fmt):
    """Return {tournament_id: (name, url)} for Challenges, newest = highest id."""
    sources = [
        f"{BASE}/tournament_searches/new?mformat={fmt}",
        f"{BASE}/metagame/{fmt}",  # fallback: its 'Recent Decks' section
    ]
    for url in sources:
        soup = BeautifulSoup(fetch(session, url).text, "html.parser")
        found = {}
        for a in soup.find_all("a", href=TOURNAMENT_HREF):
            name = a.get_text(strip=True)
            if "challenge" in name.lower():
                tid = int(TOURNAMENT_HREF.search(a["href"]).group(1))
                found[tid] = (name, urljoin(BASE, a["href"].split("#")[0]))
        if found:
            return found
    return {}


# ------------------------------------------------ parsing a tournament page
def parse_tournament(session, url):
    soup = BeautifulSoup(fetch(session, url).text, "html.parser")
    text = soup.get_text("\n")
    head = soup.find("h2") or soup.find("h1")
    title = head.get_text(strip=True) if head else "Tournament"
    fmt_m = re.search(r"Format:\s*([^\n]+)", text)
    date_m = re.search(r"Date:\s*(\d{4}-\d{2}-\d{2})", text)

    rows = []
    for tr in soup.find_all("tr"):
        deck_a = tr.find("a", href=re.compile(r"/deck/\d+"))
        player_a = tr.find("a", href=re.compile(r"/player/"))
        if not deck_a or not player_a:
            continue  # skips spinner rows and other tables
        cell = tr.find(["td", "th"])
        rows.append(
            {
                "place": cell.get_text(strip=True) if cell else "?",
                "deck": deck_a.get_text(strip=True),
                "deck_url": urljoin(BASE, deck_a["href"]),
                "pilot": player_a.get_text(strip=True),
            }
        )
        if len(rows) == 8:
            break
    return {
        "title": title,
        "format": fmt_m.group(1).strip() if fmt_m else "",
        "date": date_m.group(1) if date_m else "",
        "top8": rows,
    }


def format_top8(info, url):
    lines = [
        f"**{info['format']} Challenge Top 8 - {info['date']}**",
        f"[{esc(info['title'])}]({url})",
        "",
    ]
    for r in info["top8"]:
        lines.append(f"{r['place']}. [{esc(r['deck'])}]({r['deck_url']}) - {esc(r['pilot'])}")
    return "\n".join(lines)


# ------------------------------------------------------------- metagame
def parse_metagame(html):
    soup = BeautifulSoup(html, "html.parser")
    anchors = {}
    for a in soup.find_all("a", href=re.compile(r"/archetype/")):
        anchors.setdefault(a["href"].split("#")[0], []).append(a)

    entries = []
    for base, links in anchors.items():
        named = [a for a in links if "#" in a["href"]]
        name = (named or links)[0].get_text(strip=True)
        node = links[0]
        match = None
        for _ in range(8):  # climb up to the tile that holds the numbers
            if node is None:
                break
            bases = {
                x["href"].split("#")[0]
                for x in node.find_all("a", href=re.compile(r"/archetype/"))
            }
            m = PCT_COUNT.search(node.get_text(" ", strip=True))
            if m and len(bases) == 1:
                match = m
                break
            node = node.parent
        if match and name:
            entries.append((name, match.group(1) + "%", match.group(2)))
    return entries


def candidate_html(text):
    """The response may be plain HTML or JavaScript containing an HTML string."""
    yield text
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', text, re.S)
    for lit in sorted(literals, key=len, reverse=True)[:3]:
        try:
            yield json.loads('"' + lit.replace("\\'", "'") + '"')
        except Exception:
            continue


def fetch_metagame(session, fmt, days):
    page_url = f"{BASE}/metagame/{fmt}"
    page = fetch(session, page_url)
    token_tag = BeautifulSoup(page.text, "html.parser").find(
        "meta", attrs={"name": "csrf-token"}
    )
    if not token_tag:
        raise RuntimeError("Could not find Goldfish security token on metagame page")
    data = {
        "authenticity_token": token_tag["content"],
        "period": str(days),
        "mformat": fmt,
        "subformat": "",
        "page": "",
        "type": "paper",
    }
    headers = {
        "X-CSRF-Token": token_tag["content"],
        "X-Requested-With": "XMLHttpRequest",
        "Referer": page_url,
        "Origin": BASE,
        "Accept": "text/html, application/javascript, */*; q=0.01",
    }
    r = fetch(session, f"{BASE}/metagame/re_sort", method="POST", data=data, headers=headers)
    for html in candidate_html(r.text):
        entries = parse_metagame(html)
        if entries:
            return entries
    print("DEBUG metagame response (first 600 chars):", r.text[:600])
    raise RuntimeError("Could not read metagame entries from Goldfish response")


def format_metagame(fmt_name, days, entries):
    today = datetime.date.today().isoformat()
    lines = [f"**{fmt_name} {days}-Day Metagame - {today}**", ""]
    for i, (name, pct, count) in enumerate(entries, 1):
        lines.append(f"{i}. {esc(name)} - {pct} ({count})")
    return "\n".join(lines)


# ----------------------------------------------------------------- main
def run_server(server):
    name = server["name"]
    fmt = server["format"].lower().replace(" ", "_")
    fmt_name = fmt.replace("_", " ").title()
    webhook = os.environ.get(server["webhook_env"], "").strip()
    if not webhook:
        raise RuntimeError(f"Secret {server['webhook_env']} is empty or missing")

    session = requests.Session()
    session.headers.update(HEADERS)
    state_path = STATE_DIR / f"{name}.json"
    first_run = not state_path.exists()
    state = load_state(state_path)

    challenges = find_challenges(session, fmt)
    if not challenges:
        print(f"[{name}] No Challenges found on Goldfish.")
        return
    new_ids = sorted(i for i in challenges if i not in state["processed"])
    if first_run and new_ids:
        # Don't spam old results: post only the newest, mark the rest as seen.
        state["processed"].extend(new_ids[:-1])
        new_ids = new_ids[-1:]
    if not new_ids:
        print(f"[{name}] Nothing new.")
        return

    posted_any = False
    for tid in new_ids:
        _, url = challenges[tid]
        info = parse_tournament(session, url)
        if info["format"].lower() != fmt.replace("_", " "):
            print(f"[{name}] Skipping {tid}: format is '{info['format']}'")
            state["processed"].append(tid)
            continue
        if len(info["top8"]) < 8:
            print(f"[{name}] {tid} has only {len(info['top8'])} rows yet; will retry.")
            continue
        sig = info["date"] + "|" + "|".join(r["pilot"] for r in info["top8"][:4])
        if sig in state["signatures"]:
            print(f"[{name}] Skipping {tid}: duplicate of an event already posted.")
            state["processed"].append(tid)
            continue
        if server.get("post_challenges", True):
            send_discord(webhook, format_top8(info, url))
            posted_any = True
            print(f"[{name}] Posted Top 8 for {tid}: {info['title']}")
        state["processed"].append(tid)
        state["signatures"].append(sig)
        save_state(state_path, state)  # save right away so we never double-post
        time.sleep(1)
    save_state(state_path, state)

    if posted_any and server.get("post_metagame", True):
        days = server.get("metagame_days", 7)
        entries = fetch_metagame(session, fmt, days)[: server.get("metagame_top", 15)]
        send_discord(webhook, format_metagame(fmt_name, days, entries))
        print(f"[{name}] Posted {days}-day metagame.")


def main():
    config = json.loads(Path("config.json").read_text())
    failures = 0
    for server in config["servers"]:
        try:
            run_server(server)
        except Exception as e:  # keep going with other servers
            print(f"[{server.get('name', '?')}] ERROR: {e}")
            failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
