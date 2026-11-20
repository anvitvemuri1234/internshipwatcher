#!/usr/bin/env python3
"""
Internship Watcher -- starter kit (single file, ~250 lines).

Polls company job boards + community tracker repos every hour (GitHub Actions),
filters for NEW internships you care about, emails you the moment one opens.

Run:   SMTP_USERNAME=... SMTP_PASSWORD=... EMAIL_TO=... python watcher.py
Dry:   DRY_RUN=1 python watcher.py        (prints, writes preview.html, no state)
Probe: python watcher.py probe "Jane Street" stripe figma   (find board tokens)
"""
import json, os, re, smtplib, sys, time
from concurrent.futures import ThreadPoolExecutor
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
import requests

CONFIG_FILE, SEEN_FILE, TIMEOUT = "config.json", "seen_jobs.json", 15
DRY_RUN = os.environ.get("DRY_RUN") == "1"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept": "application/json"}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except (OSError, json.JSONDecodeError): return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, indent=2, ensure_ascii=False)


# ------------------------------ board fetchers ------------------------------ #
# Each returns normalized dicts: {id, title, location, url, content, company?}.
# Token = the slug in the company's board URL (see `probe` at the bottom).
def fetch_greenhouse(firm):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{firm['token']}/jobs?content=true", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j.get("id")), "title": j.get("title") or "", "location": (j.get("location") or {}).get("name") or "",
             "url": j.get("absolute_url") or "", "content": (j.get("content") or "").lower()} for j in r.json().get("jobs", [])]


def fetch_lever(firm):
    r = requests.get(f"https://api.lever.co/v0/postings/{firm['token']}?mode=json", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j.get("id")), "title": j.get("text") or "", "location": (j.get("categories") or {}).get("location") or "",
             "url": j.get("hostedUrl") or "", "content": (j.get("descriptionPlain") or "").lower()} for j in r.json()]


def fetch_ashby(firm):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{firm['token']}?includeCompensation=false", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j.get("id")), "title": j.get("title") or "", "location": j.get("location") or "",
             "url": j.get("jobUrl") or j.get("applyUrl") or "", "content": (j.get("descriptionPlain") or "").lower()} for j in r.json().get("jobs", [])]


def fetch_workable(firm):
    r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{firm['token']}?details=true", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j.get("shortcode") or j.get("id")), "title": j.get("title") or "",
             "location": ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x),
             "url": j.get("url") or j.get("application_url") or "", "content": (j.get("description") or "").lower()} for j in r.json().get("jobs", [])]


TRACKER_LINKS = []


def fetch_github_json(firm):
    """Community tracker repos that publish listings.json (SimplifyJobs / vanshb03
    family). One source = hundreds of companies. `cycle_year` is injected so
    repo-scoped listings pass the year check even when the title has no year."""
    r = requests.get(firm["url"], headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    out, cy, seasons = [], str(firm.get("cycle_year", "")), [s.lower() for s in firm.get("seasons", [])]
    data = r.json()
    TRACKER_LINKS.extend((str(j.get("url") or j.get("application_link") or ""), str(j.get("company_name") or j.get("company") or ""))
                         for j in data if isinstance(j, dict))   # every link, for auto-discovery
    for j in data:
        if not isinstance(j, dict) or j.get("active") is False or j.get("is_visible") is False: continue
        terms = j.get("terms") or []
        season_text = ((" ".join(terms) if isinstance(terms, list) else str(terms)) + " " + str(j.get("season") or "")).lower()
        if seasons and not any(s in season_text for s in seasons): continue
        locs = j.get("locations") or j.get("location") or []
        link = j.get("url") or j.get("application_link") or ""
        title = (j.get("title") or "").strip()
        out.append({"id": str(j.get("id") or link or title), "title": title, "company": (j.get("company_name") or j.get("company") or "").strip(),
                    "location": ", ".join(map(str, locs)) if isinstance(locs, list) else str(locs), "url": link, "content": "",
                    "year_text": f"{title} {season_text} {cy}"})
    return out


def fetch_workday(firm):
    """Workday career sites (Boeing, Northrop, GE Aerospace, RTX...). Find host/tenant/site
    in the careers URL: https://<host>/en-US/<site>  -> tenant is the first part of host.
    Searches each term in firm['search'] and pages through results (20 per page).
    Workday rate-limits (429) bare API calls, so we load the careers page first for
    cookies, send the headers a browser would, and back off and retry on 429."""
    host, tenant, site = firm["host"], firm["tenant"], firm["site"]
    api, out, ids = f"https://{host}/wday/cxs/{tenant}/{site}/jobs", [], set()
    sess = requests.Session()
    sess.headers.update({"User-Agent": HEADERS["User-Agent"], "Accept": "application/json",
                         "Accept-Language": "en-US,en;q=0.9", "Content-Type": "application/json",
                         "Origin": f"https://{host}", "Referer": f"https://{host}/en-US/{site}"})
    try: sess.get(f"https://{host}/en-US/{site}", headers={"Accept": "text/html"}, timeout=TIMEOUT)
    except requests.RequestException: pass

    def post(body):
        for wait in (0, 4, 12, 30):
            time.sleep(wait)
            r = sess.post(api, json=body, timeout=TIMEOUT)
            if r.status_code != 429: break
        r.raise_for_status(); return r.json()

    for term in firm.get("search", ["intern", "co-op"]):
        offset, total = 0, None
        while offset < 600:
            d = post({"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            posts = d.get("jobPostings") or []
            if total is None: total = d.get("total") or 0   # Workday only reports total on page 1
            for p in posts:
                path = p.get("externalPath") or ""
                if not path or path in ids: continue
                ids.add(path)
                out.append({"id": path, "title": p.get("title") or "", "location": p.get("locationsText") or "",
                            "url": f"https://{host}/en-US/{site}{path}", "content": ""})
            offset += 20
            if not posts or offset >= total: break
            time.sleep(1)
    return out


# ------------------------------ auto-discovery ------------------------------ #
# Every apply link in the tracker repos names a company's job board. Poll ALL of
# those boards directly, so you see every engineering internship at those ~1,000
# companies (most of them small), not just the roles the tracker chose to list.
DISCOVER = {"greenhouse": r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)",
            "lever": r"jobs\.lever\.co/([A-Za-z0-9_.-]+)", "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)",
            "workable": r"apply\.workable\.com/([A-Za-z0-9_-]+)"}
SKIP_TOKENS = {"embed", "v1", "api", "jobs", "careers", "j", "job"}


def discover_boards(links, known, ats_list):
    boards = {}
    for url, company in links:
        for ats in ats_list:
            m = re.search(DISCOVER[ats], url)
            if not m: continue
            tok = m.group(1).lower()
            if tok in SKIP_TOKENS or (ats, tok) in known or (ats, tok) in boards: continue
            boards[(ats, tok)] = {"name": company or tok, "ats": ats, "token": tok, "auto": True}
    return list(boards.values())


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workable": fetch_workable, "github_json": fetch_github_json, "workday": fetch_workday}


# --------------------------------- filters ---------------------------------- #
DROPS = {}


def _drop(reason, title):
    DROPS.setdefault(reason, []).append(title); return False


def _title_is_internship(title, keywords):
    # Word-boundary: 'intern' matches Intern/Interns/Internship but NOT
    # Internal / International / Internet. Substring matching floods you with
    # "Internal Audit" directors.
    return any(re.search(r"\b" + re.escape(k.lower()) + r"(s|ship|ships)?\b", title) for k in keywords)


def _has_term(title, term):
    term = term.lower()
    return re.search((r"\b%s\b" if len(term) <= 3 else r"\b%s") % re.escape(term), title) is not None


def is_relevant(job, f):
    title = job["title"].lower()
    if f.get("title_keywords") and not _title_is_internship(title, f["title_keywords"]): return _drop("no-intern-word", title)
    if f.get("title_require_any") and not any(_has_term(title, t) for t in f["title_require_any"]): return _drop("no-domain-match", title)
    for bad in f.get("title_exclude", []):  # word-start match: "ai" must not hit "maintenance"
        b = bad.lower()
        if re.search(r"(?<!\w)" + re.escape(b) + (r"(?!\w)" if len(b) <= 3 else ""), title): return _drop(f"excluded:{bad}", title)
    # CYCLE CHECK. NEVER require the year in the title -- most companies omit it.
    # If the title names a year, it must be ours; else look in the body; if no
    # year appears anywhere, KEEP IT (a live intern posting is the next cycle).
    years = [str(y) for y in f.get("years", [])]
    ty = set(re.findall(r"\b(20\d{2})\b", title))
    if years and ty and not (ty & set(years)): return _drop("wrong-year-in-title", title)
    if years and not ty:
        hay = f"{job.get('year_text','')} {title} {(job.get('content') or '')[:4000]}".lower()
        if not any(y in hay for y in years) and any(p.lower() in hay for p in f.get("reject_cycle_phrases", [])):
            return _drop("wrong-cycle-phrase", title)
    loc = (job.get("location") or "").lower()
    if loc and f.get("location_exclude") and any(b.lower() in loc for b in f["location_exclude"]):
        # word-boundary like every other gate: "ca" must not match "Canada", "ny" not "Germany"
        if not any(_has_term(loc, m) for m in f.get("location_keep_markers", [])): return _drop("excluded-location", title)
    return True


# ---------------------------------- email ----------------------------------- #
# Email is grouped by TYPE OF ROLE, not company. First matching category wins.
ROLE_GROUPS = [
    ("Mechanical / Design", ["mechanical", "design engineer", "product design", "cad", "mechatronic"]),
    ("Aerospace / Propulsion / Space", ["aerospace", "aeronautic", "propulsion", "avionics", "space", "flight", "rocket", "launch", "satellite", "aircraft"]),
    ("Structural / Civil / Construction", ["structur", "stress", "civil", "construction", "facilities"]),
    ("Manufacturing / Industrial / Quality", ["manufactur", "industrial", "process engineer", "quality", "production", "operations", "supply"]),
    ("Energy / Nuclear / Power", ["nuclear", "energy", "power", "fusion", "battery", "grid", "solar", "renewable", "thermal", "hvac"]),
    ("Robotics / Automotive / Vehicles", ["robot", "automotive", "vehicle", "autonom", "drone", "uav"]),
    ("Test / Reliability / Validation", ["test", "reliability", "validation", "failure analysis", "integration"]),
    ("Systems Engineering", ["systems engineer", "system engineer", "systems engineering"]),
    ("Hardware", ["hardware", "electronics", "sensor"]),
]


def role_group(title):
    t = title.lower()
    for name, keys in ROLE_GROUPS:
        if any(k in t for k in keys): return name
    return "General Engineering"


def build_email(jobs, baseline=False):
    parts = ["<p>Baseline of currently-open roles. Future emails contain only <b>newly opened</b> postings.</p>" if baseline
             else "<p>These internship postings just opened:</p>"]
    groups = {}
    for j in jobs: groups.setdefault(role_group(j["title"]), []).append(j)
    order = [g for g, _ in ROLE_GROUPS] + ["General Engineering"]
    for g in sorted(groups, key=order.index):
        parts.append(f"<h3 style='margin:16px 0 4px'>{escape(g)} ({len(groups[g])})</h3><ul>")
        for j in sorted(groups[g], key=lambda x: x["title"].lower()):
            loc = f" &mdash; {escape(j['location'])}" if j.get("location") else ""
            parts.append(f"<li><a href='{escape(j['url'])}'>{escape(j['title'])}</a> &middot; <b>{escape(j.get('company',''))}</b>{loc}</li>")
        parts.append("</ul>")
    parts.append("<p style='color:#888;font-size:12px'>Sent automatically by your internship watcher.</p>")
    return "\n".join(parts)


def send_email(subject, html):
    if DRY_RUN:
        open("preview.html", "w", encoding="utf-8").write(html); print(f"DRY RUN: {subject!r} -> preview.html"); return
    user, pw, to = os.environ.get("SMTP_USERNAME"), os.environ.get("SMTP_PASSWORD"), os.environ.get("EMAIL_TO")
    if not (user and pw and to): sys.exit("set SMTP_USERNAME, SMTP_PASSWORD (Gmail App Password), EMAIL_TO")
    tos = [a.strip() for a in re.split(r"[,;\s]+", to) if a.strip()]
    msg = MIMEMultipart("alternative"); msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(tos)
    msg.attach(MIMEText(html, "html", "utf-8"))
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465")), timeout=TIMEOUT) as s:
                s.login(user, pw); s.sendmail(user, tos, msg.as_string())
            print(f"Email sent to {tos}: {subject}"); return
        except (smtplib.SMTPException, OSError) as e:
            print(f" ! SMTP attempt {attempt+1}: {e}"); time.sleep(5 * (attempt + 1))
    raise RuntimeError("SMTP failed 3x")


# ---------------------------------- main ------------------------------------ #
def job_key(url, fallback):
    """Same job can appear with different URL spellings (tracker vs board). Normalize."""
    u = (url or "").strip().lower().split("#")[0].split("?")[0].rstrip("/")
    if not u: return fallback.lower()
    u = re.sub(r"^https?://", "", u).replace("job-boards.greenhouse.io", "boards.greenhouse.io")
    u = re.sub(r"/(application|apply)$", "", u)
    return u


def main():
    cfg = load_json(CONFIG_FILE, None) or sys.exit("config.json missing")
    f = cfg.get("filters", {})
    seen = {job_key(k, k): v for k, v in load_json(SEEN_FILE, {}).items()}
    first_run, current, new = not seen, {}, []
    firms = [x for x in cfg["firms"] if x.get("enabled", True)]

    def poll(firm):
        try: return firm, FETCHERS[firm["ats"]](firm), None
        except Exception as e:  # one dead board never kills the run
            return firm, [], e

    def collect(results, verbose):
        failed = 0
        for firm, jobs, err in results:
            if err:
                failed += 1
                if verbose: print(f" x {firm.get('name')} skipped: {err}")
                continue
            rel = [j for j in jobs if is_relevant(j, f)]
            if verbose or rel: print(f" ok {firm['name']}: {len(jobs)} jobs, {len(rel)} relevant")
            for j in rel:
                key = job_key(j.get("url"), f"{firm['name']}:{j['id']}")
                if key in current: continue
                j.setdefault("company", firm["name"]); current[key] = j
                if key not in seen: new.append(j)
        return failed

    with ThreadPoolExecutor(max_workers=12) as pool:  # keep this parallel; 200 boards serially takes an hour
        results = list(pool.map(poll, firms))
    collect(results, verbose=True)

    ad = cfg.get("autodiscover", {})
    if ad.get("enabled"):
        known = {(x["ats"], str(x.get("token", "")).lower()) for x in cfg["firms"]}
        known |= {(a, str(t).lower()) for a, t in (tuple(x) for x in ad.get("exclude", []))}
        boards = discover_boards(TRACKER_LINKS, known, ad.get("ats", list(DISCOVER)))
        print(f"Auto-discovery: polling {len(boards)} more company boards found in the trackers...")
        with ThreadPoolExecutor(max_workers=24) as pool:
            failed = collect(list(pool.map(poll, boards)), verbose=False)
        print(f"Auto-discovery: {len(boards) - failed} boards ok, {failed} unreachable (normal; boards move or close)")

    if DROPS:  # log every drop -- silent filtering is the most dangerous bug class
        print("Filter drops: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(DROPS.items(), key=lambda kv: -len(kv[1]))[:10]))
    out = list(current.values()) if first_run else new
    if out:
        send_email(f"[Internship Watcher] {'Baseline: ' if first_run else ''}{len(out)} {'open' if first_run else 'new'} role(s)", build_email(out, first_run))
    else:
        print("No new roles this run.")
    if not DRY_RUN:
        for k, j in current.items(): seen[k] = {"title": j["title"], "url": j.get("url", "")}
        save_json(SEEN_FILE, seen)


def probe(names):
    """Guess board tokens from company names and tell you which ATS each uses."""
    for n in names:
        slug = re.sub(r"[^a-z0-9]", "", n.lower())
        for cands in ([slug, slug + "llc", slug + "inc", slug + "careers", slug + "trading", slug + "capital"],):
            for tok in cands:
                for ats in ("greenhouse", "lever", "ashby", "workable"):
                    try:
                        jobs = FETCHERS[ats]({"token": tok})
                        if jobs: print(f" {n}: {{\"name\": \"{n}\", \"ats\": \"{ats}\", \"token\": \"{tok}\"}} ({len(jobs)} postings)"); break
                    except Exception: pass
                else: continue
                break


if __name__ == "__main__":
    probe(sys.argv[2:]) if len(sys.argv) > 1 and sys.argv[1] == "probe" else main()
