"""Tests for the wall-clock IO guard (``gateway/bounded_io.py``) — place#241.

The defect being fixed is a call that **never returns**, so the tests have to
drive a call that never returns and assert the caller comes back anyway. A
``threading.Event`` that is never set is the right stand-in for a blocked NFS
syscall: neither can be cancelled or interrupted from Python, so a guard that
handles one handles the other. (A ``sleep`` would be a weaker stand-in — it
ends by itself.)

Every timeout test therefore asserts two things, not one: that the caller
returned within its budget, **and** that the blocked worker was still running
when it did. Without the second assertion the test would also pass if the work
had simply finished quickly, which is the shape of check that cannot fail.
"""

from __future__ import annotations

import threading
import time
import unittest

from gateway.bounded_io import IoGuard


class TestIoGuardHappyPath(unittest.TestCase):
    def test_returns_the_value(self):
        guard = IoGuard("t", timeout=1.0)
        outcome = guard.run(lambda a, b: a + b, 2, 3, default=-1)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.value, 5)
        self.assertEqual(outcome.status, "ok")
        self.assertFalse(guard.degraded)

    def test_kwargs_are_passed_through(self):
        guard = IoGuard("t", timeout=1.0)
        outcome = guard.run(lambda a, b=0: a * b, 4, b=5, default=None)
        self.assertEqual(outcome.value, 20)

    def test_exception_is_reported_not_raised(self):
        guard = IoGuard("t", timeout=1.0)

        def boom():
            raise OSError("no such file")

        outcome = guard.run(boom, default="fallback")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.value, "fallback")
        # An exception means the filesystem ANSWERED. That is not the failure
        # this guard exists for, so it must not write the resource off.
        self.assertFalse(guard.degraded)


class TestIoGuardTimeout(unittest.TestCase):
    """The case ``try/except`` cannot express: a call that does not return."""

    def setUp(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.finished = threading.Event()
        # Release any abandoned worker at teardown so the suite leaves no
        # threads parked on a never-set Event.
        self.addCleanup(self.release.set)

    def _blocker(self):
        self.entered.set()
        self.release.wait(30)  # bounded only so a failing test cannot hang CI
        self.finished.set()
        return "late"

    def test_caller_returns_while_the_work_is_still_blocked(self):
        guard = IoGuard("wedged", timeout=0.3, cooldown=10.0)
        started = time.monotonic()
        outcome = guard.run(self._blocker, default=[])
        elapsed = time.monotonic() - started

        self.assertTrue(self.entered.wait(1), "the work never started")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "timeout")
        self.assertEqual(outcome.value, [])
        self.assertLess(elapsed, 3.0, "the guard did not bound the wait")
        # The positive half: the work is STILL blocked, so the caller really
        # was released from an unfinished call rather than one that completed.
        self.assertFalse(self.finished.is_set())
        self.assertEqual(guard.stats()["abandoned_threads"], 1)

    def test_timeout_opens_the_breaker_and_later_calls_cost_nothing(self):
        guard = IoGuard("wedged", timeout=0.3, cooldown=30.0)
        guard.run(self._blocker, default=None)
        self.assertTrue(guard.degraded)

        touched = []
        started = time.monotonic()
        outcome = guard.run(lambda: touched.append(1), default="skipped")
        elapsed = time.monotonic() - started

        self.assertEqual(outcome.status, "tripped")
        self.assertEqual(outcome.value, "skipped")
        # Not merely fast — the filesystem was not touched at all.
        self.assertEqual(touched, [])
        self.assertLess(elapsed, 0.1)
        self.assertEqual(guard.stats()["short_circuits"], 1)

    def test_breaker_closes_once_the_resource_answers_again(self):
        guard = IoGuard("wedged", timeout=0.3, cooldown=0.2)
        guard.run(self._blocker, default=None)
        self.assertTrue(guard.degraded)

        time.sleep(0.25)  # cooldown expires → one probe is admitted (half-open)
        self.assertFalse(guard.degraded)
        outcome = guard.run(lambda: "healthy", default=None)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.value, "healthy")
        self.assertFalse(guard.degraded)

    def test_saturation_stops_creating_threads(self):
        guard = IoGuard("wedged", timeout=0.2, cooldown=0.0, max_abandoned=1)
        guard.run(self._blocker, default=None)          # abandons one thread
        self.assertEqual(guard.stats()["abandoned_threads"], 1)

        touched = []
        outcome = guard.run(lambda: touched.append(1), default=None)
        self.assertEqual(outcome.status, "saturated")
        self.assertEqual(touched, [], "a new probe was started past the cap")

    def test_abandoned_thread_is_accounted_back_when_it_unwedges(self):
        guard = IoGuard("wedged", timeout=0.2, cooldown=0.0)
        guard.run(self._blocker, default=None)
        self.assertEqual(guard.stats()["abandoned_threads"], 1)

        self.release.set()                      # the mount recovers
        self.assertTrue(self.finished.wait(5))
        deadline = time.monotonic() + 5
        while guard.stats()["abandoned_threads"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(guard.stats()["abandoned_threads"], 0)

    def test_guard_does_not_borrow_the_callers_thread_pool(self):
        """The abandoned worker must be a private daemon thread.

        ``asyncio.to_thread`` shares one bounded executor; abandoning threads
        from it would starve every unrelated caller in the gateway — the very
        symptom place#241 reports — so the guard has to own its threads.
        """
        guard = IoGuard("wedged", timeout=0.2, cooldown=0.0)
        guard.run(self._blocker, default=None)
        stuck = [t for t in threading.enumerate()
                 if t.name.startswith("bounded-io:")]
        self.assertEqual(len(stuck), 1)
        self.assertTrue(stuck[0].daemon)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
