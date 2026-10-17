"""Evidence attribution, interrupted-run restoration and safe packaging."""
import argparse
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from experiments.phase4 import arguments, choose_target, join_attempts, run_condition, write_csv, write_json
from experiments.phase4_summary import analyze_run
from experiments.package_phase4 import package


class ExperimentTests(unittest.TestCase):
    def test_default_matrix_and_target_tie_break(self):
        args = arguments([])
        self.assertEqual(len(args.policies) * len(args.ft) * args.repetitions, 12)
        self.assertEqual((args.rate, args.stage_seconds), (20, 20))
        events = [{"time": t, "selected_server": name} for t, name in
                  [(0, "server-3"), (9, "server-2"), (8, "server-1")]]
        self.assertEqual(choose_target(events, 10)[0], "server-1")
        with self.assertRaises(RuntimeError):
            choose_target(events, 100)

    def test_retry_attribution_preserves_failure_and_final_backend(self):
        events = [dict(correlation_id="one", selected_server=name, outcome=outcome)
                  for name, outcome in [("server-1", "connect_failed"), ("server-2", "connected")]]
        rows = join_attempts([dict(request_id="one"), dict(request_id="two")], events)
        self.assertEqual(rows[0]["backend"], "server-2")
        self.assertEqual(len(json.loads(rows[0]["attempts"])), 2)
        self.assertEqual(rows[1]["backend"], "")

    def test_partial_kill_error_still_attempts_restore_and_saves_partial_data(self):
        calls = []
        class FakeController:
            def __init__(self, *args):
                pass
            def event(self, *args, **kwargs):
                pass
            def status(self, *args):
                pass
            def command(self, *args, **kwargs):
                calls.append(args)
                if args[0] == "config":
                    return json.dumps({"services": {"lb": {"environment": {
                        "SERVER_PORT": "18861", "LB_METRICS_PORT": "18860"}}}})
                if args[0] == "kill":
                    raise RuntimeError("simulated kill command failure after dispatch")
                return ""
        snapshot = dict(time=100, backends=[], health_events=[], selection_events=[
            dict(time=99, selected_server="server-2", correlation_id=None)])
        def producer(*args):
            args[-3].wait(1)  # stop Event: the controller must set it in finally
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            args = argparse.Namespace(rate=20, stage_seconds=20)
            with patch("experiments.phase4.Controller", FakeController), patch(
                "experiments.phase4.ready"
            ), patch("experiments.phase4.produce_load", producer), patch(
                "experiments.phase4.metrics", return_value=snapshot
            ), patch("experiments.phase4.time.monotonic", side_effect=[0, 20, 20]):
                with self.assertRaisesRegex(RuntimeError, "simulated kill"):
                    run_condition(args, directory, "lrt", "on", 1, None)
            self.assertIn(("start", "server-2"), calls)
            self.assertFalse(json.loads((directory / "run.json").read_text())["complete"])

    def test_summary_requires_observed_exclusion_and_returned_traffic(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "run.json", dict(policy="lrt", ft="on", repetition=1,
                       epoch=0, target="server-2", stage_seconds=20, complete=True,
                       expected_requests=3, sample_interval_s=1))
            rows = [dict(started_at=t, completed_at=t+.1, elapsed_s=t, latency_ms=100,
                         schedule_lag_ms=0, success=True, backend=backend, error="")
                    for t, backend in ((5, "server-2"), (25, "server-1"), (45, "server-2"))]
            write_csv(directory / "requests.csv", rows)
            (directory / "events.jsonl").write_text('\n'.join(json.dumps(e) for e in [
                dict(kind="kill_begin", time=20), dict(kind="restart_begin", time=40)]))
            write_json(directory / "final-metrics.json", dict(selection_events=[]))
            (directory / "metrics.jsonl").write_text('\n'.join(json.dumps(e) for e in [
                dict(time=22, observed_at=22.1, backends=[dict(host="server-2", health="unhealthy")]),
                dict(time=42, observed_at=42.1, backends=[dict(host="server-2", health="healthy")])]))
            summary = analyze_run(directory)[0]
            self.assertEqual(summary["acceptance"], "pass")
            self.assertAlmostEqual(summary["observed_detection_s"], 2.1)
            self.assertEqual(summary["stable_down_requests"], 1)
            rows[-1]["backend"] = "server-1"
            write_csv(directory / "requests.csv", rows)
            self.assertEqual(analyze_run(directory)[0]["acceptance"], "needs-review")


class PackagingTests(unittest.TestCase):
    def test_archive_excludes_env_venv_git_and_private_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (".env", ".env.example", "server/service.py", "server/.env",
                         "server/__pycache__/x.pyc", "server/.venv/token", ".git/config",
                         "docs/private-notes.md", "docs/phase4.md"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test fixture")
            (root / "server/leak.py").symlink_to(root / ".env")
            output = package(root, "TEST")
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertEqual(sorted(names), sorted([
                    "lab-TEST-phase4/.env.example", "lab-TEST-phase4/server/service.py",
                    "lab-TEST-phase4/docs/phase4.md", "lab-TEST-phase4/submission-manifest.json"]))
            with self.assertRaises(FileExistsError):
                package(root, "TEST")
            for group in ("", "../secret", "has space"):
                with self.assertRaises(ValueError):
                    package(root, group)
