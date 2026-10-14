import argparse
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "power_loss_test", Path(__file__).parents[1] / "scripts" / "k3_power_loss_test.py"
)
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def arguments() -> argparse.Namespace:
    return argparse.Namespace(
        host="127.0.0.1", user="bianbu", password="bianbu",
        remote_dir="/home/example/mem-gateway-build",
    )


class _advancing_clock:
    """把 sleep 换成「把时钟拨到超时之后」，让等待循环一次迭代就结束。"""

    def __init__(self):
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += 10_000.0

    def __enter__(self):
        self._time = patch.object(driver.time, "time", side_effect=self.time)
        self._sleep = patch.object(driver.time, "sleep", side_effect=self.sleep)
        self._time.start()
        self._sleep.start()
        return self

    def __exit__(self, *exception):
        self._sleep.stop()
        self._time.stop()
        return False


class PowerLossDriverTests(unittest.TestCase):
    def test_service_state_ignores_ssh_noise_after_the_state_word(self):
        output = ("deactivating\nclose - IO is still pending on closed socket. "
                  "read:1, write:0, io:00000138D123AD10")
        self.assertEqual(driver.service_state(output), "deactivating")
        self.assertEqual(driver.service_state(""), "unknown")
        self.assertEqual(driver.service_state("active"), "active")

    def test_probe_health_reads_json_out_of_login_noise(self):
        output = ("Warning: Permanently added the host\n"
                  '{"conclusive": true, "health": {"status": "ok"}}\n'
                  "close - IO is still pending on closed socket")
        with patch.object(driver, "ssh_sudo", return_value=(0, output)):
            sample = driver.probe_health(arguments())
        self.assertTrue(sample["conclusive"])
        self.assertEqual(sample["health"]["status"], "ok")

    def test_probe_health_reports_missing_json_instead_of_raising(self):
        with patch.object(driver, "ssh_sudo",
                          return_value=(1, "python3: can't open file 'scripts/...'")):
            sample = driver.probe_health(arguments())
        self.assertIn("error", sample)

    def test_wait_for_ready_stops_at_the_first_conclusive_sample(self):
        samples = [
            {"health": {"status": "unknown", "database": "busy"}},
            {"health": {"status": "ok", "database": "ok",
                        "database_check": {"state": "checked"}}},
        ]
        with patch.object(driver, "ssh", return_value=(0, "active")), patch.object(
            driver, "probe_health", side_effect=samples
        ) as probe, patch.object(driver.time, "sleep"):
            ready = driver.wait_for_ready(arguments(), timeout=300)
        self.assertTrue(ready["conclusive"])
        self.assertEqual(ready["polls"], 2)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(ready["database_check"], {"state": "checked"})
        self.assertIn("checked", driver.ready_summary(ready))

    def test_wait_for_ready_does_not_poll_a_service_that_never_starts(self):
        with patch.object(driver, "ssh", return_value=(0, "inactive")), patch.object(
            driver, "probe_health"
        ) as probe, _advancing_clock():
            ready = driver.wait_for_ready(arguments(), timeout=300)
        self.assertFalse(ready["conclusive"])
        self.assertEqual(ready["service"], "inactive")
        probe.assert_not_called()

    def test_wait_for_ready_reports_a_timeout_without_masking_it(self):
        with patch.object(driver, "ssh", return_value=(0, "active")), patch.object(
            driver, "probe_health",
            return_value={"health": {"status": "unknown", "database": "busy"}},
        ), _advancing_clock():
            ready = driver.wait_for_ready(arguments(), timeout=300)
        self.assertFalse(ready["conclusive"])
        self.assertIn("还没验", ready["note"])
        self.assertEqual(ready["polls"], 1)
        self.assertIn("unknown/busy", driver.ready_summary(ready))

    def test_ready_summary_survives_a_missing_database_check(self):
        summary = driver.ready_summary(
            {"service": "active", "status": None, "database": None, "polls": 1, "waited_s": 0.0}
        )
        self.assertIn("state=None", summary)


if __name__ == "__main__":
    unittest.main()
