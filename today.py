"""
today.py - rebuilds card.svg for the Mperomishael profile README.

What it does every run:
  1. Works out your "uptime" (age) from your birth date and time.
  2. Asks GitHub's GraphQL API for repos, stars, followers, commits and lines of code.
  3. Writes everything, together with the ASCII portrait in art.txt, into card.svg.

Usage:
  python today.py              # real run (needs ACCESS_TOKEN env var)
  python today.py --offline    # no network; stats show "--" (for testing the layout)

Environment variables:
  ACCESS_TOKEN  GitHub personal access token (classic: read:user + repo, or a
                fine-grained token with read access to your repositories)
  USER_NAME     GitHub username (defaults to Mperomishael)
"""

import hashlib
import os
import sys
import time
from datetime import datetime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- settings --

USER_NAME = os.environ.get("USER_NAME", "Mperomishael")
TOKEN = os.environ.get("ACCESS_TOKEN", "")
API = "https://api.github.com/graphql"

TZ = ZoneInfo("Africa/Lagos")
BIRTH = datetime(2000, 6, 18, 2, 0, tzinfo=TZ)  # 18 June 2000, 2:00 am

HERE = os.path.dirname(os.path.abspath(__file__))
ART_FILE = os.path.join(HERE, "art.txt")
CACHE_DIR = os.path.join(HERE, "cache")
SVG_FILE = os.path.join(HERE, "card.svg")

# Card look. Colours follow GitHub's dark theme so it reads well in light mode too.
BG, BORDER = "#0d1117", "#30363d"
C_ART, C_KEY, C_VAL = "#c9d1d9", "#ffa657", "#a5d6ff"
C_DOT, C_HEAD, C_ADD, C_DEL = "#6e7681", "#8b949e", "#3fb950", "#f85149"
FONT = "ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace"

ART_FS, ART_CW, ART_LH = 7.0, 4.2, 8.4          # tiny font = more defined face
INFO_FS, INFO_CW, INFO_LH = 12.5, 7.5, 16.0
PANEL_CHARS = 62                                # width of the info panel in characters

# ------------------------------------------------------------------ uptime --


def add_months(dt, months):
    """Add whole months to a datetime, clamping to the last day of the month."""
    total = dt.year * 12 + (dt.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise ValueError("bad date")


def uptime(now=None):
    """Return (years, months, days) between BIRTH and now, in Lagos time."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    months = (now.year - BIRTH.year) * 12 + (now.month - BIRTH.month)
    while months > 0 and add_months(BIRTH, months) > now:
        months -= 1
    anchor = add_months(BIRTH, months)
    days = (now - anchor).days
    return months // 12, months % 12, days


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def uptime_text(now=None):
    now = (now or datetime.now(TZ)).astimezone(TZ)
    y, m, d = uptime(now)
    text = f"{plural(y, 'year')}, {plural(m, 'month')}, {plural(d, 'day')}"
    if (now.month, now.day) == (BIRTH.month, BIRTH.day):
        text += " \U0001F382"  # birthday cake
    return text


# ------------------------------------------------------------ GitHub stats --


def gql(query, variables):
    headers = {"Authorization": f"bearer {TOKEN}"}
    r = requests.post(API, json={"query": query, "variables": variables},
                      headers=headers, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GitHub API returned {r.status_code}: {r.text[:300]}")
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GitHub API error: {data['errors']}")
    return data["data"]


def get_user():
    q = """query($login: String!) {
      user(login: $login) {
        id
        followers { totalCount }
        repositories(ownerAffiliations: OWNER) { totalCount }
      }
    }"""
    u = gql(q, {"login": USER_NAME})["user"]
    return u["id"], u["followers"]["totalCount"], u["repositories"]["totalCount"]


def get_stars():
    q = """query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: OWNER) {
          pageInfo { hasNextPage endCursor }
          nodes { stargazerCount }
        }
      }
    }"""
    total, cursor = 0, None
    while True:
        repos = gql(q, {"login": USER_NAME, "cursor": cursor})["user"]["repositories"]
        total += sum(n["stargazerCount"] for n in repos["nodes"])
        if not repos["pageInfo"]["hasNextPage"]:
            return total
        cursor = repos["pageInfo"]["endCursor"]


def list_repos():
    """Every repo you own, collaborate on, or reach through an organisation."""
    q = """query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 60, after: $cursor,
          ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
          pageInfo { hasNextPage endCursor }
          nodes {
            nameWithOwner
            defaultBranchRef { target { ... on Commit { history { totalCount } } } }
          }
        }
      }
    }"""
    out, cursor = [], None
    while True:
        repos = gql(q, {"login": USER_NAME, "cursor": cursor})["user"]["repositories"]
        for node in repos["nodes"]:
            ref = node["defaultBranchRef"]
            total = ref["target"]["history"]["totalCount"] if ref else 0
            out.append((node["nameWithOwner"], total))
        if not repos["pageInfo"]["hasNextPage"]:
            return out
        cursor = repos["pageInfo"]["endCursor"]


def count_repo(full_name, my_id):
    """Walk one repo's history; return (my commits, lines added, lines deleted)."""
    owner, name = full_name.split("/", 1)
    q = """query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        defaultBranchRef { target { ... on Commit {
          history(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes { additions deletions author { user { id } } }
          }
        } } }
      }
    }"""
    mine = add = dele = 0
    cursor = None
    while True:
        repo = gql(q, {"owner": owner, "name": name, "cursor": cursor})["repository"]
        if not repo["defaultBranchRef"]:
            return 0, 0, 0
        hist = repo["defaultBranchRef"]["target"]["history"]
        for c in hist["nodes"]:
            user = (c["author"] or {}).get("user")
            if user and user["id"] == my_id:
                mine += 1
                add += c["additions"]
                dele += c["deletions"]
        if not hist["pageInfo"]["hasNextPage"]:
            return mine, add, dele
        cursor = hist["pageInfo"]["endCursor"]


def key_for(full_name):
    return hashlib.sha256(full_name.encode()).hexdigest()


def load_cache():
    path = os.path.join(CACHE_DIR, hashlib.sha256(USER_NAME.encode()).hexdigest() + ".txt")
    cache = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 5:
                    cache[parts[0]] = tuple(int(p) for p in parts[1:])
    return path, cache


def save_cache(path, cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        for k, (total, mine, add, dele) in cache.items():
            f.write(f"{k} {total} {mine} {add} {dele}\n")


def get_loc(my_id):
    """Commits and lines of code. Repos whose commit count has not changed are
    read from the cache, so only new activity costs API calls."""
    path, old = load_cache()
    new = {}
    try:
        for full_name, total in list_repos():
            k = key_for(full_name)
            if k in old and old[k][0] == total:
                new[k] = old[k]
            else:
                mine, add, dele = count_repo(full_name, my_id)
                new[k] = (total, mine, add, dele)
    finally:
        save_cache(path, new)  # keep partial progress even if a call fails
    commits = sum(v[1] for v in new.values())
    added = sum(v[2] for v in new.values())
    deleted = sum(v[3] for v in new.values())
    return commits, added, deleted


def fmt(n):
    return f"{n:,}"


def collect_stats(offline):
    if offline:
        return dict(repos="--", commits="--", stars="--", followers="--",
                    loc="--", add="--", dele="--")
    if not TOKEN:
        sys.exit("ACCESS_TOKEN is not set. Add it as a repository secret (see README).")
    t = time.time()
    my_id, followers, repos = get_user()
    stars = get_stars()
    commits, added, deleted = get_loc(my_id)
    print(f"stats collected in {time.time() - t:.1f}s")
    return dict(repos=fmt(repos), commits=fmt(commits), stars=fmt(stars),
                followers=fmt(followers), loc=fmt(added - deleted),
                add=fmt(added), dele=fmt(deleted))


# ------------------------------------------------------------------- card --


def build_info(stats):
    """Lines of the right-hand panel. Each line is a list of (text, colour)."""
    W = PANEL_CHARS

    def head(title):
        return [(f"- {title} " + "-" * (W - len(title) - 3), C_HEAD)]

    def kv(key, value):
        if isinstance(value, str):
            value = [(value, C_VAL)]
        vlen = sum(len(t) for t, _ in value)
        dots = max(2, W - (len(key) + 2) - 1 - vlen)
        return [(key + ":", C_KEY), (" " + "." * dots + " ", C_DOT)] + value

    title = "mishael@empire-digitals "
    loc_value = [(f"{stats['loc']} (", C_VAL), (f"{stats['add']}++", C_ADD),
                 (", ", C_VAL), (f"{stats['dele']}--", C_DEL), (")", C_VAL)]
    return [
        [(title + "-" * (W - len(title)), C_HEAD)],
        kv("Name", "Mishael Yakubu"),
        kv("Uptime", uptime_text()),
        kv("OS", "Android + Termux"),
        kv("Host", "Empire Digitals Worldwide"),
        kv("IDE", "VS Code"),
        kv("Location", "Delta State, Nigeria"),
        [],
        head("Stack"),
        kv("Languages", "JavaScript, TypeScript, Node.js"),
        kv("Frontend", "React, Next.js"),
        kv("Data", "Supabase, Firebase"),
        kv("Deploy", "Railway, Vercel, Render, Cloudflare"),
        kv("Automation", "AI Automation"),
        [],
        head("Contact"),
        kv("Email", "empiredigitalsworldwide@gmail.com"),
        kv("WhatsApp", "+234 814 265 6848"),
        kv("Telegram", "+234 814 265 6848"),
        kv("Community", "whatsapp.com/channel/0029VaI3OXiF6smuq5LxxN15"),
        [],
        head("GitHub Stats"),
        kv("Repos", stats["repos"]),
        kv("Commits", stats["commits"]),
        kv("Stars", stats["stars"]),
        kv("Followers", stats["followers"]),
        kv("Lines of Code", loc_value),
    ]


def text_el(x, y, segments, fs, cw):
    """One line of text. textLength pins the width so columns line up in any font."""
    n = sum(len(t) for t, _ in segments)
    spans = "".join(f'<tspan fill="{c}">{escape(t)}</tspan>' for t, c in segments)
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{fs}" xml:space="preserve" '
            f'style="white-space:pre" textLength="{n * cw:.1f}" '
            f'lengthAdjust="spacing">{spans}</text>')


def build_svg(stats):
    with open(ART_FILE) as f:
        art = f.read().rstrip("\n").split("\n")
    info = build_info(stats)

    pad = 22
    art_w = max(len(l) for l in art) * ART_CW
    art_h = len(art) * ART_LH
    info_h = len(info) * INFO_LH
    panel_x = pad + art_w + 28
    width = panel_x + PANEL_CHARS * INFO_CW + pad
    height = max(art_h, info_h) + pad * 2

    art_y0 = pad + (height - pad * 2 - art_h) / 2
    info_y0 = pad + (height - pad * 2 - info_h) / 2

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        f'aria-label="Mishael Yakubu GitHub profile card">',
        f'<rect x="0.5" y="0.5" width="{width - 1:.0f}" height="{height - 1:.0f}" rx="12" '
        f'fill="{BG}" stroke="{BORDER}"/>',
        f'<g font-family="{FONT}">',
    ]
    for i, line in enumerate(art):
        if line.strip():
            parts.append(text_el(pad, art_y0 + (i + 1) * ART_LH, [(line, C_ART)], ART_FS, ART_CW))
    for i, segs in enumerate(info):
        if segs:
            parts.append(text_el(panel_x, info_y0 + (i + 1) * INFO_LH - 3, segs, INFO_FS, INFO_CW))
    parts.append("</g></svg>")
    return "\n".join(parts)


def main():
    offline = "--offline" in sys.argv
    stats = collect_stats(offline)
    with open(SVG_FILE, "w", encoding="utf-8") as f:
        f.write(build_svg(stats))
    print(f"wrote {SVG_FILE}")


if __name__ == "__main__":
    main()
