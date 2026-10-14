import argparse
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "power_probe", Path(__file__).parents[1] / "scripts" / "k3_power_loss_probe.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class PowerProbeSafetyTests(unittest.TestCase):
    @unittest.skipIf(probe.os.name == "nt", "Directory fsync requires POSIX")
    def test_report_replaces_existing_file_with_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "baseline.json"
            destination.write_text("old")
            with patch.object(probe.os, "fsync") as sync, patch.object(
                probe.os, "open", wraps=probe.os.open
            ):
                probe.durable_report(str(destination), {"run_id": "test"})
            self.assertEqual(json.loads(destination.read_text()), {"run_id": "test"})
            self.assertEqual(sync.call_count, 2)

    def test_health_is_conclusive_only_when_a_verdict_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            registration = Path(directory) / "1234.json"
            registration.write_text("{}")
            arguments = argparse.Namespace()
            cases = {
                ("ok", "ok"): 0,
                ("degraded", "malformed inverted index for fts5 table x"): 0,
                ("unknown", "unknown"): 1,
                ("unknown", "busy"): 1,
            }
            for (status, database), expected in cases.items():
                with self.subTest(status=status, database=database):
                    with patch.object(probe, "ensure_registration",
                                      return_value=registration), patch.object(
                        probe, "health_snapshot",
                        return_value={"status": status, "database": database,
                                      "database_check": {"state": "busy"}},
                    ), patch("sys.stdout", new_callable=io.StringIO) as output:
                        self.assertEqual(probe.command_health(arguments), expected)
                    self.assertIn(f'"conclusive": {"true" if expected == 0 else "false"}',
                                  output.getvalue())
            self.assertFalse(registration.exists())

    def test_health_reports_unreadable_service_instead_of_crashing(self):
        arguments = argparse.Namespace()
        with patch.object(probe, "ensure_registration",
                          side_effect=FileNotFoundError("/run/bianbu")), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            self.assertEqual(probe.command_health(arguments), 1)
        self.assertIn("FileNotFoundError", output.getvalue())

    def test_pre_refuses_unsupported_namespace_report_before_writes(self):
        arguments = argparse.Namespace(json="unused.json", markers=20, run_id="test")
        with patch.object(probe, "ensure_registration"), patch.object(
            probe, "boot_id", return_value="boot"
        ), patch.object(probe, "uptime_seconds", return_value=100), patch.object(
            probe, "versions", return_value={}
        ), patch.object(probe, "unit_properties", return_value={"ActiveState": "active"}), patch.object(
            probe, "storage_snapshot", return_value={}
        ), patch.object(probe, "health_snapshot", return_value={"status": "ok", "database": "ok"}), patch.object(
            probe, "namespace_report", return_value={"error": "unsupported option"}
        ), patch.object(probe, "temp_files", return_value=[]), patch.object(
            probe, "durable_report"
        ) as report, patch.object(probe, "write_markers") as markers:
            self.assertEqual(probe.command_pre(arguments), 1)
            markers.assert_not_called()
            self.assertEqual(report.call_args.args[1]["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
