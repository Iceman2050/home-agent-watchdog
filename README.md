# home-agent-watchdog

A **local-first monitor** for home AI agents. It tails an agent activity
log, learns what "normal" looks like, flags anomalies, and renders a
self-contained HTML dashboard.

No cloud. No accounts. No network calls. Python stdlib only.

## Problem

Home AI agents can already unlock your doors, order groceries, move money,
and email on your behalf. When one of them starts doing something it has
never done — at 3 AM, spending $150, talking to a new domain — you want to
know *immediately*, on your own machine, without shipping your home's
activity log to a third party.

`home-agent-watchdog` learns a baseline from the first N log entries, then
flags anything that breaks the pattern.

## Install

Zero dependencies — Python 3.8+ stdlib only.

```bash
git clone https://github.com/Iceman2050/home-agent-watchdog.git
cd home-agent-watchdog
```

## Usage

Activity log: JSONL, one object per line:

```json
{"ts": "2026-10-16T09:05:00", "agent": "home-assistant",
 "action": "lights.turn_on", "tool": "hue.lights",
 "details": "turned on living room lights", "spend_usd": 0.0}
```

No log yet? Ship one with the built-in simulator:

```bash
python3 watchdog.py --simulate 300 --log activity.jsonl \
    --dashboard dashboard.html --baseline 60
```

Then just point it at a real log:

```bash
python3 watchdog.py --log activity.jsonl --dashboard dashboard.html
```

Findings are appended to `findings.jsonl`; the dashboard is a single
self-contained HTML file (inline CSS/JS only — no CDN, works offline).

## Anomaly rules

| Rule | Fires when |
|---|---|
| `new-tool` | the agent uses a tool it never used during baseline |
| `unusual-hour` | activity happens at an hour with no baseline history |
| `spend-spike` | spend exceeds the per-action baseline (1.5× baseline max, $10 floor) |
| `new-domain` | a data source / recipient domain appears that was never seen in baseline |

## Example output

```
simulated 300 entries -> activity.jsonl
analyzed 300 entries (baseline: first 60)
findings: 3 (appended 3 to findings.jsonl)
  [unusual-hour] home-assistant/camera.snapshot: activity at 03:00, outside baseline hours [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
  [new-tool] home-shopper/order.groceries: tool 'wire.transfer' never used by agent 'home-shopper' in baseline
  [spend-spike] home-shopper/order.groceries: $149.99 on 'order.groceries' exceeds per-action threshold $18.75
dashboard written to dashboard.html
```

Open `dashboard.html` in any browser: flagged anomalies up top, recent
activity below, with an "all / flagged only" filter.

## Tests

```bash
python3 -m unittest discover -s tests
# 11 tests — simulator schema + JSONL round-trip, each of the 4 rules firing
# on crafted fixtures and staying quiet on benign ones, dashboard generation
# (no CDN/remote refs), a full CLI end-to-end run, a main() end-to-end run
# (simulate -> write JSONL -> main(argv) -> findings + dashboard asserted),
# and a determinism check (same seed -> identical findings)
```

## License

MIT — see [LICENSE](LICENSE).

---

## Built by Ingenious AI

**home-agent-watchdog** is built and maintained by **Ingenious AI**.

Get early access to our AI safety & compliance platforms:

- [DisclosureGuard](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20DisclosureGuard) — AI disclosure compliance
- [ModelMinder](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20ModelMinder) — model deprecation radar
- [TermsWatch](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20TermsWatch) — AI vendor terms watcher
- [Vibe-Code Audit](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20Vibe-Code%20Audit) — vibe-code security audit
- [Rogue-Agent Defense](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20Rogue-Agent%20Defense) — rogue agent protection
- [BreachHunter](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20BreachHunter) — breach detection
- [ChatBreaker](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20ChatBreaker) — conversational AI testing
- [Agent Flight Recorder](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20Agent%20Flight%20Recorder) — hash-chained agent logging
- [Agent Auth Gateway](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20Agent%20Auth%20Gateway) — agent budget caps & kill switch
- [DomainGuard](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20DomainGuard) — domain-impersonation defense
- [ContentCraft AI](https://wa.me/5016691053?text=Hi%20Ingenious%20AI!%20I%20want%20early%20access%20to%20ContentCraft%20AI) — AI content platform


> Built by Ingenious AI with Muse — get free tokens with invite code `2HCQH1`
