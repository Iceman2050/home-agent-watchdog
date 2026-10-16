#!/usr/bin/env python3
"""home-agent-watchdog — local-first monitor for home AI agents.

Tails an agent activity log (JSONL), learns a baseline from the first N
entries, flags anomalies, appends findings to findings.jsonl, and renders a
self-contained local HTML dashboard.

No cloud, no accounts, no network calls. Stdlib only.
"""
import argparse
import datetime as _dt
import html
import json
import os
import random
import re
import sys

LOG_SCHEMA = ("ts", "agent", "action", "tool", "details", "spend_usd")

# ---------------------------------------------------------------------------
# Log simulator
# ---------------------------------------------------------------------------

_AGENT_PROFILES = {
    "home-assistant": [
        ("lights.turn_on", "hue.lights", "turned on living room lights", 0.0),
        ("lights.turn_off", "hue.lights", "turned off bedroom lights", 0.0),
        ("thermostat.set", "nest.thermostat", "set thermostat to 72F", 0.0),
        ("vacuum.start_clean", "roborock.vacuum", "started downstairs clean", 0.0),
        ("camera.snapshot", "unifi.camera", "saved front-door snapshot", 0.0),
        ("lock.doors", "august.lock", "locked all doors", 0.0),
        ("play.music", "spotify.play", "playing evening playlist", 0.0),
        ("send.summary_email", "gmail.send",
         "sent daily summary to owner@gmail.com", 0.0),
    ],
    "home-shopper": [
        ("order.groceries", "instacart.order",
         "ordered weekly groceries from instacart.com", 0.0),
        ("reorder.coffee", "amazon.order",
         "reordered coffee pods from amazon.com", 12.99),
        ("check.price", "google.shopping", "compared air-filter prices", 0.0),
        ("pay.bill", "utility.pay",
         "paid electric bill via powerco.com", 84.20),
    ],
}

_EMAIL_RE = re.compile(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})")
_URL_RE = re.compile(r"https?://([\w.-]+\.[A-Za-z]{2,})")
# bare domains like evil-exfil.net (no scheme) — TLD allowlist avoids matching
# version numbers or random dotted words
_BARE_DOMAIN_RE = re.compile(
    r"\b((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|io|dev|app|co|ai|xyz|info|biz|us|uk|de|fr|ca|au|nl|se))\b")


def simulate(num_entries=200, seed=42, start=None):
    """Generate realistic home-agent activity. Returns a list of dicts."""
    rng = random.Random(seed)
    if start is None:
        start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)
    entries = []
    ts = start
    agents = list(_AGENT_PROFILES)
    for _ in range(num_entries):
        # realistic cadence: mostly daytime, a few evening hours
        hour = rng.choices(
            population=list(range(24)),
            weights=[0.1]*5 + [1]*2 + [3]*10 + [2]*4 + [1]*2 + [0.3],
            k=1,
        )[0]
        ts = ts + _dt.timedelta(minutes=rng.randint(5, 90))
        ts = ts.replace(hour=hour, minute=rng.randint(0, 59))
        agent = rng.choice(agents)
        action, tool, details, spend = rng.choice(_AGENT_PROFILES[agent])
        jitter = round(rng.uniform(-0.15, 0.15) * max(spend, 1.0), 2)
        entries.append({
            "ts": ts.isoformat(timespec="seconds"),
            "agent": agent,
            "action": action,
            "tool": tool,
            "details": details,
            "spend_usd": round(max(spend + jitter, 0.0), 2),
        })
    entries.sort(key=lambda e: e["ts"])
    return entries


def write_jsonl(entries, path):
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def load_log(path):
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and all(k in obj for k in LOG_SCHEMA):
                entries.append(obj)
    return entries


# ---------------------------------------------------------------------------
# Baseline + anomaly detection
# ---------------------------------------------------------------------------

def _hour_of(ts):
    try:
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).hour
    except (ValueError, TypeError):
        return -1


def domains_in(entry):
    text = json.dumps(entry.get("details", ""))
    found = (set(_EMAIL_RE.findall(text)) | set(_URL_RE.findall(text))
             | set(_BARE_DOMAIN_RE.findall(text)))
    return {d.lower().lstrip("www.") for d in found}


def learn_baseline(entries):
    """Learn normal behavior: per-agent tools, active hours, per-action spend,
    and known data-source / recipient domains."""
    tools = {}        # agent -> set(tool)
    hour_counts = {}  # hour -> count
    spend = {}        # action -> [amounts]
    domains = set()
    for e in entries:
        tools.setdefault(e["agent"], set()).add(e["tool"])
        h = _hour_of(e["ts"])
        hour_counts[h] = hour_counts.get(h, 0) + 1
        spend.setdefault(e["action"], []).append(float(e.get("spend_usd", 0) or 0))
        domains |= domains_in(e)
    active_hours = {h for h, c in hour_counts.items() if c >= 2 and h >= 0}
    spend_max = {a: max(v) for a, v in spend.items()}
    return {
        "tools": {a: sorted(t) for a, t in tools.items()},
        "active_hours": sorted(active_hours),
        "spend_max": spend_max,
        "domains": sorted(domains),
    }


def detect(entries, baseline, spend_floor=10.0, spend_mult=1.5):
    """Flag anomalies against the baseline. Returns a list of findings."""
    known_tools = {a: set(t) for a, t in baseline["tools"].items()}
    active_hours = set(baseline["active_hours"])
    spend_max = baseline["spend_max"]
    known_domains = set(baseline["domains"])
    findings = []
    for e in entries:
        # (a) tool never seen before for this agent
        if e["tool"] not in known_tools.get(e["agent"], set()):
            findings.append(_finding(e, "new-tool",
                f"tool '{e['tool']}' never used by agent '{e['agent']}' in baseline"))
        # (b) action at an unusual hour
        h = _hour_of(e["ts"])
        if active_hours and h not in active_hours:
            findings.append(_finding(e, "unusual-hour",
                f"activity at {h:02d}:00, outside baseline hours {sorted(active_hours)}"))
        # (c) spend above per-action threshold
        amount = float(e.get("spend_usd", 0) or 0)
        threshold = max(spend_max.get(e["action"], 0.0) * spend_mult, spend_floor)
        if amount > threshold:
            findings.append(_finding(e, "spend-spike",
                f"${amount:.2f} on '{e['action']}' exceeds per-action threshold ${threshold:.2f}"))
        # (d) new data source / recipient domain
        for d in sorted(domains_in(e) - known_domains):
            findings.append(_finding(e, "new-domain",
                f"first contact with domain '{d}'"))
    return findings


def _finding(entry, rule, detail):
    return {
        "detected_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "ts": entry.get("ts", ""),
        "agent": entry.get("agent", ""),
        "action": entry.get("action", ""),
        "tool": entry.get("tool", ""),
        "rule": rule,
        "detail": detail,
    }


def append_findings(findings, path="findings.jsonl"):
    if not findings:
        return 0
    with open(path, "a", encoding="utf-8") as fh:
        for f in findings:
            fh.write(json.dumps(f) + "\n")
    return len(findings)


# ---------------------------------------------------------------------------
# Dashboard (single self-contained HTML file, inline JS only, no CDN)
# ---------------------------------------------------------------------------

DASHBOARD_CSS = """
body{font-family:system-ui,-apple-system,sans-serif;max-width:1100px;margin:2rem auto;
padding:0 1rem;color:#1a1a1a;background:#fafafa}
h1{font-size:1.6rem}h2{margin-top:2rem;font-size:1.2rem}
.badge{display:inline-block;padding:.2rem .6rem;border-radius:1rem;font-size:.8rem;margin-right:.4rem}
.ok{background:#d4edda}.warn{background:#f8d7da}.rule{background:#e2e3f1}
table{border-collapse:collapse;width:100%;font-size:.85rem;background:#fff}
th,td{border:1px solid #ddd;padding:.45rem .6rem;text-align:left}
th{background:#f0f0f0}tr.flagged{background:#fff3f3}
.toolbar{margin:1rem 0}button{padding:.4rem .9rem;margin-right:.5rem;cursor:pointer}
.meta{color:#555;font-size:.85rem}
"""

DASHBOARD_JS = """
function filterRows(mode){
  document.querySelectorAll('#activity tbody tr').forEach(function(tr){
    tr.style.display = (mode==='all' || (mode==='flagged' && tr.classList.contains('flagged'))) ? '' : 'none';
  });
  document.querySelectorAll('.toolbar button').forEach(function(b){b.disabled=false;});
  document.getElementById('btn-'+mode).disabled=true;
}
"""


def render_dashboard(entries, findings, baseline, path):
    flagged_idx = set()
    # mark activity rows that triggered any finding (match on ts+agent+action)
    keys = {(f["ts"], f["agent"], f["action"]) for f in findings}
    recent = entries[-50:]
    for i, e in enumerate(recent):
        if (e["ts"], e["agent"], e["action"]) in keys:
            flagged_idx.add(i)

    def row(i, e):
        cls = ' class="flagged"' if i in flagged_idx else ""
        return (
            f"<tr{cls}><td>{html.escape(str(e.get('ts','')))}</td>"
            f"<td>{html.escape(str(e.get('agent','')))}</td>"
            f"<td>{html.escape(str(e.get('action','')))}</td>"
            f"<td>{html.escape(str(e.get('tool','')))}</td>"
            f"<td>{html.escape(str(e.get('details','')))}</td>"
            f"<td>${float(e.get('spend_usd',0) or 0):.2f}</td></tr>"
        )

    activity_rows = "\n".join(row(i, e) for i, e in enumerate(recent))
    finding_rows = "\n".join(
        f"<tr><td>{html.escape(f['detected_at'])}</td>"
        f"<td>{html.escape(f['agent'])}</td>"
        f"<td>{html.escape(f['action'])}</td>"
        f"<td><span class='badge rule'>{html.escape(f['rule'])}</span></td>"
        f"<td>{html.escape(f['detail'])}</td></tr>"
        for f in findings
    ) or "<tr><td colspan='5'>No anomalies detected. 🎉</td></tr>"

    status = (f"<span class='badge warn'>{len(findings)} anomalies</span>"
              if findings else "<span class='badge ok'>all clear</span>")

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>home-agent-watchdog dashboard</title>
<style>{DASHBOARD_CSS}</style></head>
<body>
<h1>🏠 home-agent-watchdog</h1>
<p class="meta">Local-first home AI agent monitor · {html.escape(str(len(entries)))} activity entries analyzed · {status}</p>
<p class="meta">Baseline: {html.escape(str(len(baseline['tools'])))} agents, hours {html.escape(str(baseline['active_hours']))}, {html.escape(str(len(baseline['domains'])))} known domains.</p>

<h2>🚨 Flagged anomalies ({len(findings)})</h2>
<table><thead><tr><th>Detected</th><th>Agent</th><th>Action</th><th>Rule</th><th>Detail</th></tr></thead>
<tbody>{finding_rows}</tbody></table>

<h2>📋 Recent activity (latest {len(recent)})</h2>
<div class="toolbar">
<button id="btn-all" onclick="filterRows('all')" disabled>Show all</button>
<button id="btn-flagged" onclick="filterRows('flagged')">Flagged only</button>
</div>
<table id="activity"><thead><tr><th>Time</th><th>Agent</th><th>Action</th><th>Tool</th><th>Details</th><th>Spend</th></tr></thead>
<tbody>{activity_rows}</tbody></table>
<script>{DASHBOARD_JS}</script>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="Local-first monitor for home AI agents.")
    p.add_argument("--log", default="activity.jsonl",
                   help="activity log to analyze (JSONL)")
    p.add_argument("--dashboard", default="dashboard.html",
                   help="HTML dashboard output path")
    p.add_argument("--findings", default="findings.jsonl",
                   help="findings log (appended)")
    p.add_argument("--baseline", type=int, default=50,
                   help="entries used to learn the baseline")
    p.add_argument("--simulate", nargs="?", const=200, type=int, metavar="N",
                   help="generate N simulated entries into --log first")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.simulate:
        entries = simulate(args.simulate)
        write_jsonl(entries, args.log)
        print(f"simulated {len(entries)} entries -> {args.log}")

    if not os.path.exists(args.log):
        print(f"error: log not found: {args.log}", file=sys.stderr)
        return 1

    entries = load_log(args.log)
    if not entries:
        print("no valid entries in log", file=sys.stderr)
        return 1

    n_base = max(1, min(args.baseline, len(entries) - 1)) if len(entries) > 1 else 1
    baseline = learn_baseline(entries[:n_base])
    findings = detect(entries[n_base:], baseline)
    wrote = append_findings(findings, args.findings)
    render_dashboard(entries, findings, baseline, args.dashboard)

    print(f"analyzed {len(entries)} entries (baseline: first {n_base})")
    print(f"findings: {len(findings)} (appended {wrote} to {args.findings})")
    for f in findings:
        print(f"  [{f['rule']}] {f['agent']}/{f['action']}: {f['detail']}")
    print(f"dashboard written to {args.dashboard}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
