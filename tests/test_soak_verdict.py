import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch
import tempfile
import json

spec = importlib.util.spec_from_file_location(
    "soak", Path(__file__).parents[1] / "scripts" / "k3_memory_soak.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)


class SoakVerdictTests(unittest.TestCase):
    def test_busy_and_nonverdict_complaint_do_not_mean_corruption(self):
        ok, detail = soak.database_gate([
            {"service": {"database": "busy", "check_state": "busy"}},
            {"service": {"database": "ok", "check_verdict": "ok",
                         "check_state": "complaint"}},
        ])
        self.assertTrue(ok)
        self.assertEqual(detail["complaint_samples"], 1)

    def test_real_degradation_and_absent_conclusion_fail(self):
        for services in ([{"database": "busy"}],
                         [{"database": "ok"}, {"database": "degraded"}],
                         [{"database": "ok", "check_verdict": "corrupt"}]):
            self.assertFalse(soak.database_gate([{"service": s} for s in services])[0])

    def test_stopped_sampler_cannot_complete_window_by_waiting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "window.json").write_text(json.dumps({"started_at": 1000}))
            (path / "samples.jsonl").write_text(json.dumps({
                "ts": 1060, "service": {"database": "ok"}}) + "\n")
            with patch.object(soak.time, "time", return_value=1000000), \
                    patch.object(soak, "journal_lines", return_value=[]):
                soak.verdict(path, str(path / "result.json"))
            result = json.loads((path / "result.json").read_text())
            self.assertFalse(result["gates"]["seven_day_window"]["ok"])
            self.assertEqual(result["seven_day_window"]["window_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
