"""Tests for home-agent-watchdog."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import watchdog  # noqa: E402


def entry(ts, agent="home-assistant", action="lights.turn_on", tool="hue.lights",
          details="turned on living room lights", spend=0.0):
    return {"ts": ts, "agent": agent, "action": action, "tool": tool,
            "details": details, "spend_usd": spend}


def baseline_entries(n=30):
    """Normal behavior: daytime hours, known tools, tiny spends, known domains."""
    out = []
    for i in range(n):
        hour = 8 + (i % 13)  # 08:00-20:00, each hour repeats -> active
        out.append(entry(
            f"2026-10-16T{hour:02d}:{(i * 7) % 60:02d}:00",
            action="order.groceries" if i % 5 == 0 else "lights.turn_on",
            tool="instacart.order" if i % 5 == 0 else "hue.lights",
            details=("ordered from instacart.com" if i % 5 == 0
                     else "sent summary to owner@gmail.com"),
            spend=12.50 if i % 5 == 0 else 0.0,
        ))
    return out


class TestSimulator(unittest.TestCase):
    def test_output_parses_and_has_schema(self):
        entries = watchdog.simulate(50, seed=7)
        self.assertEqual(len(entries), 50)
        for e in entries:
            self.assertTrue(all(k in e for k in watchdog.LOG_SCHEMA),
                            f"missing keys in {e}")
            json.dumps(e)  # serializable
        # chronological order
        self.assertEqual([e["ts"] for e in entries],
                         sorted(e["ts"] for e in entries))

    def test_round_trip_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "activity.jsonl")
            watchdog.write_jsonl(watchdog.simulate(20, seed=1), path)
            loaded = watchdog.load_log(path)
            self.assertEqual(len(loaded), 20)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.base = baseline_entries()
        self.baseline = watchdog.learn_baseline(self.base)

    def rules_for(self, anomalous):
        return {f["rule"] for f in watchdog.detect(anomalous, self.baseline)}

    def test_new_tool_fires(self):
        e = entry("2026-10-16T12:00:00", tool="shell.exec",
                  action="run.command", details="ran a command")
        self.assertIn("new-tool", self.rules_for([e]))

    def test_unusual_hour_fires(self):
        e = entry("2026-10-16T03:14:00")  # 3am, never in baseline
        self.assertIn("unusual-hour", self.rules_for([e]))

    def test_spend_spike_fires(self):
        e = entry("2026-10-16T12:00:00", action="order.groceries",
                  tool="instacart.order", details="ordered from instacart.com",
                  spend=150.00)
        self.assertIn("spend-spike", self.rules_for([e]))

    def test_new_domain_fires(self):
        e = entry("2026-10-16T12:00:00",
                  details="uploaded photo to evil-exfil.net/gallery")
        self.assertIn("new-domain", self.rules_for([e]))

    def test_benign_stays_quiet(self):
        benign = [
            entry("2026-10-16T09:05:00", action="order.groceries",
                  tool="instacart.order", details="ordered from instacart.com",
                  spend=11.80),
            entry("2026-10-16T18:30:00", details="sent summary to owner@gmail.com"),
        ]
        self.assertEqual(watchdog.detect(benign, self.baseline), [])


class TestDashboard(unittest.TestCase):
    def test_dashboard_generates(self):
        base = baseline_entries()
        baseline = watchdog.learn_baseline(base)
        weird = [entry("2026-10-16T03:14:00")]
        findings = watchdog.detect(weird, baseline)
        self.assertTrue(findings)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dash.html")
            watchdog.render_dashboard(base + weird, findings, baseline, path)
            with open(path, encoding="utf-8") as fh:
                page = fh.read()
        self.assertIn("home-agent-watchdog", page)
        self.assertIn("unusual-hour", page)
        self.assertNotIn('src="http', page)
        self.assertNotIn("cdn", page.lower())


class TestCLI(unittest.TestCase):
    def test_simulate_analyze_dashboard_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "activity.jsonl")
            dash = os.path.join(d, "out.html")
            findings = os.path.join(d, "findings.jsonl")
            r = subprocess.run(
                [sys.executable, os.path.join(REPO, "watchdog.py"),
                 "--simulate", "120", "--log", log,
                 "--dashboard", dash, "--findings", findings,
                 "--baseline", "40"],
                capture_output=True, text=True, cwd=d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(dash))
            self.assertGreater(os.path.getsize(dash), 1000)
            # findings file may or may not exist (sim is mostly benign); if it
            # exists it must be valid JSONL
            if os.path.exists(findings):
                with open(findings) as fh:
                    for line in fh:
                        json.loads(line)


if __name__ == "__main__":
    unittest.main()


class TestMainEndToEnd(unittest.TestCase):
    """Drive watchdog.main() itself: simulate -> write JSONL -> main(argv)
    -> findings + dashboard. Fully deterministic (simulate(seed=123); the
    detection path uses no unseeded randomness)."""

    SEED = 123
    N_BASE = 60

    def crafted_anomalies(self, base_entries, baseline):
        active = set(baseline["active_hours"])
        hour = min(active)  # a guaranteed-active hour for the pinned ts
        stamp = "2026-10-16T%02d:05:00" % hour
        expected = []

        def entry(**kw):
            e = {"ts": stamp, "agent": "home-assistant",
                 "action": "lights.turn_on", "tool": "hue.lights",
                 "details": "turned on living room lights", "spend_usd": 0.0}
            e.update(kw)
            return e

        anomalies = []
        # new-tool: unknown tool, otherwise completely benign
        anomalies.append(entry(tool="shell.exec", action="run.command",
                              details="ran a diagnostic command"))
        expected.append("new-tool")
        # unusual-hour: known tool at an hour absent from the baseline
        odd = next((h for h in range(24) if h not in active), None)
        if odd is not None:
            anomalies.append(entry(ts="2026-10-16T%02d:14:00" % odd))
            expected.append("unusual-hour")
        # spend-spike: real baseline entry (known tool/action/hour),
        # spend far above the per-action threshold
        e0 = dict(base_entries[0])
        threshold = baseline["spend_max"].get(e0["action"], 0.0)
        e0["ts"] = stamp
        e0["spend_usd"] = max(threshold * 2.0, 50.0) + 100.0
        anomalies.append(e0)
        expected.append("spend-spike")
        # new-domain: real baseline entry with a never-seen domain
        d0 = dict(base_entries[1])
        d0["ts"] = stamp
        d0["details"] = "uploaded backup to evil-exfil.net/vault"
        anomalies.append(d0)
        expected.append("new-domain")
        return anomalies, expected

    def test_main_full_pipeline(self):
        sim = watchdog.simulate(120, seed=self.SEED)
        base_entries = sim[:self.N_BASE]
        baseline = watchdog.learn_baseline(base_entries)
        anomalies, expected_rules = self.crafted_anomalies(base_entries,
                                                           baseline)
        log_entries = sim + anomalies

        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "activity.jsonl")
            dash = os.path.join(d, "dashboard.html")
            findings_path = os.path.join(d, "findings.jsonl")
            watchdog.write_jsonl(log_entries, log)

            rc = watchdog.main(["--log", log,
                                "--dashboard", dash,
                                "--findings", findings_path,
                                "--baseline", str(self.N_BASE)])
            self.assertEqual(rc, 0)

            # findings file produced and parseable
            self.assertTrue(os.path.exists(findings_path),
                            "findings.jsonl not produced")
            rules = set()
            with open(findings_path, encoding="utf-8") as fh:
                lines = [ln for ln in fh if ln.strip()]
            self.assertGreaterEqual(len(lines), len(expected_rules))
            for ln in lines:
                obj = json.loads(ln)  # must be valid JSON per line
                rules.add(obj["rule"])
            for rule in expected_rules:
                self.assertIn(rule, rules,
                              f"expected rule {rule} missing from {sorted(rules)}")

            # dashboard produced with the findings surfaced
            self.assertTrue(os.path.exists(dash), "dashboard not produced")
            with open(dash, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("home-agent-watchdog", page)
            for rule in expected_rules:
                self.assertIn(rule, page,
                              f"rule {rule} missing from dashboard")

    def test_main_deterministic_same_seed(self):
        # Two identical runs must yield identical finding rules and counts.
        def one_run():
            with tempfile.TemporaryDirectory() as d:
                log = os.path.join(d, "a.jsonl")
                dash = os.path.join(d, "d.html")
                fp = os.path.join(d, "f.jsonl")
                sim = watchdog.simulate(120, seed=self.SEED)
                base = sim[:self.N_BASE]
                baseline = watchdog.learn_baseline(base)
                anomalies, _ = self.crafted_anomalies(base, baseline)
                watchdog.write_jsonl(sim + anomalies, log)
                self.assertEqual(
                    watchdog.main(["--log", log, "--dashboard", dash,
                                   "--findings", fp,
                                   "--baseline", str(self.N_BASE)]), 0)
                rules = []
                with open(fp, encoding="utf-8") as fh:
                    for ln in fh:
                        if ln.strip():
                            rules.append(json.loads(ln)["rule"])
                return sorted(rules)

        self.assertEqual(one_run(), one_run())


if __name__ == "__main__":
    unittest.main()
