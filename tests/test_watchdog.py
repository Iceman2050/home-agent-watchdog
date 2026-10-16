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
