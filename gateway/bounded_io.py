# gateway/bounded_io.py
"""Wall-clock-bounded filesystem access for the gateway's serving path.

Why this exists (place#241)
---------------------------
``/ix1`` is a **hard** NFS mount. When it wedges, a ``stat()`` or ``open()``
against it does not fail — it blocks in uninterruptible sleep, indefinitely.
Every guard in the hard-link readers was written for the failure modes that
were *anticipated* — "missing / mid-swap / locked", all of which raise — so
``try/except`` never fired and the request simply never returned.

A hang is not a rarer version of "missing"; it is a different failure, and an
exception handler cannot express it. Bounding it needs a **clock**.

Why a private daemon thread, and not ``asyncio.to_thread``
----------------------------------------------------------
A blocked NFS syscall cannot be cancelled or interrupted from Python: the
thread executing it stays blocked until the mount recovers, whatever the caller
does. The only thing available is to stop *waiting* for it. So each call runs
on its own **daemon** thread joined with a deadline; on expiry the thread is
abandoned (it costs a stack, and it exits by itself when the mount recovers)
and the caller degrades to its fallback.

It must not be an ``asyncio.to_thread`` pool thread. That pool is shared and
bounded, so abandoning pool threads on a wedged mount permanently removes them
from the pool — a few dozen requests and every unrelated ``to_thread`` in the
gateway starves behind the wedge. That starvation is the production symptom in
place#241, so the fix must not reproduce it in a slower form.

Circuit breaker
---------------
Paying the timeout on *every* request while the mount is down would still make
the gateway slow, and would abandon one thread per request. So a timeout
**opens** the breaker for a cooldown: further calls return their fallback
immediately, touching no filesystem at all, until the cooldown expires and one
call is admitted to probe (half-open). Any answer — success *or* an ordinary
exception, both of which prove the filesystem responded — closes it again.

``max_abandoned`` is the backstop for the pathological case where probes keep
being admitted and none ever return: once that many threads are still stuck,
no further ones are created and every call short-circuits.

Testability
-----------
The point of the class is to make a failure that previously *could not be
observed* observable, so it has to be observable in a test too:
:meth:`IoGuard.stats` reports the counters and :meth:`IoGuard.reset` clears
them. ``tests/test_bounded_io.py`` drives a real blocked call (an
``threading.Event`` that is never set — the same shape as a blocked syscall,
since neither can be interrupted) and asserts the caller returns anyway.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, NamedTuple

logger = logging.getLogger("gateway.bounded_io")


# Whole-operation budget. Generous enough that an ordinary cold read from
# healthy shared storage cannot trip the breaker, tight enough that a wedged
# mount costs one request a few seconds and everyone else nothing.
DEFAULT_TIMEOUT_S = float(os.getenv("GATEWAY_IO_TIMEOUT_S", "3.0"))
# How long the store stays written off after a timeout, before one probe is let
# through. A wedged NFS mount is typically minutes-to-hours, not milliseconds.
DEFAULT_COOLDOWN_S = float(os.getenv("GATEWAY_IO_COOLDOWN_S", "60.0"))
# Hard ceiling on threads abandoned inside a wedge.
DEFAULT_MAX_ABANDONED = int(os.getenv("GATEWAY_IO_MAX_ABANDONED", "8"))


class Outcome(NamedTuple):
    """Result of a guarded call.

    ``ok`` is True only when ``fn`` ran to completion; ``value`` is then its
    return value and otherwise the caller's ``default``. ``status`` says which
    way it failed, which is the whole point — "the store said no" and "the store
    never answered" must not look alike again.
    """
    ok: bool
    value: Any
    status: str        # ok | timeout | tripped | saturated | error
    elapsed: float


class IoGuard:
    """A wall-clock + circuit-breaker guard around one filesystem resource."""

    def __init__(
        self,
        label: str,
        *,
        timeout: float | None = None,
        cooldown: float | None = None,
        max_abandoned: int | None = None,
    ) -> None:
        self.label = label
        self.timeout = DEFAULT_TIMEOUT_S if timeout is None else float(timeout)
        self.cooldown = DEFAULT_COOLDOWN_S if cooldown is None else float(cooldown)
        self.max_abandoned = (DEFAULT_MAX_ABANDONED if max_abandoned is None
                              else int(max_abandoned))
        self._lock = threading.Lock()
        self._open_until = 0.0      # monotonic deadline; breaker open until then
        self._abandoned = 0         # threads still stuck in the wedge
        self._timeouts = 0
        self._short_circuits = 0
        self._calls = 0

    # -- state -----------------------------------------------------------

    @property
    def degraded(self) -> bool:
        """True while the breaker is open — i.e. this store is being skipped.

        Callers use it to *describe* the degradation to the client rather than
        report a store that could not be read as a store that held nothing.
        """
        with self._lock:
            return time.monotonic() < self._open_until

    def stats(self) -> dict:
        with self._lock:
            return {
                "label": self.label,
                "timeout_s": self.timeout,
                "cooldown_s": self.cooldown,
                "degraded": time.monotonic() < self._open_until,
                "calls": self._calls,
                "timeouts": self._timeouts,
                "short_circuits": self._short_circuits,
                "abandoned_threads": self._abandoned,
            }

    def reset(self) -> None:
        """Close the breaker and clear the counters (tests; manual recovery)."""
        with self._lock:
            self._open_until = 0.0
            self._abandoned = 0
            self._timeouts = 0
            self._short_circuits = 0
            self._calls = 0

    # -- the guarded call ------------------------------------------------

    def run(self, fn: Callable[..., Any], *args: Any,
            default: Any = None, **kwargs: Any) -> Outcome:
        """Run ``fn`` with a deadline; degrade to ``default`` if it overruns.

        Never raises: an exception inside ``fn`` is logged and reported as
        ``status="error"`` (the filesystem answered, so the breaker closes).
        """
        with self._lock:
            self._calls += 1
            now = time.monotonic()
            if now < self._open_until:
                self._short_circuits += 1
                remaining = self._open_until - now
                skipped = self._short_circuits
            else:
                remaining = 0.0
                skipped = 0
            abandoned = self._abandoned
        if remaining > 0:
            logger.debug("%s: skipped, breaker open for another %.1fs (%d skipped)",
                         self.label, remaining, skipped)
            return Outcome(False, default, "tripped", 0.0)
        if abandoned >= self.max_abandoned:
            logger.warning("%s: skipped, %d probe thread(s) still stuck",
                           self.label, abandoned)
            return Outcome(False, default, "saturated", 0.0)

        box: dict[str, Any] = {}
        state = {"done": False, "abandoned": False}

        def _target() -> None:
            try:
                box["value"] = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 — reported, never raised out
                box["error"] = exc
            finally:
                with self._lock:
                    state["done"] = True
                    if state["abandoned"]:
                        # Came back after we stopped waiting: the wedge cleared.
                        self._abandoned = max(0, self._abandoned - 1)

        started = time.monotonic()
        thread = threading.Thread(target=_target, daemon=True,
                                  name=f"bounded-io:{self.label}")
        thread.start()
        thread.join(self.timeout)
        elapsed = time.monotonic() - started

        with self._lock:
            if not state["done"]:
                state["abandoned"] = True
                self._abandoned += 1
                self._timeouts += 1
                self._open_until = time.monotonic() + self.cooldown
                timed_out = True
                stuck = self._abandoned
            else:
                timed_out = False
                # It answered — success or ordinary error, either way the
                # filesystem is responding, so close the breaker.
                recovered = self._open_until > 0.0
                self._open_until = 0.0

        if timed_out:
            logger.warning(
                "%s: no answer in %.1fs — degrading and skipping this store for "
                "%.0fs (%d thread(s) still stuck). A hard-mounted filesystem that "
                "hangs cannot raise, so this is a timeout, not an error.",
                self.label, self.timeout, self.cooldown, stuck)
            return Outcome(False, default, "timeout", elapsed)

        if recovered:
            logger.info("%s: responding again (%d call(s) skipped in total "
                        "while degraded)", self.label, self._short_circuits)

        if "error" in box:
            logger.warning("%s: failed after %.3fs: %s",
                           self.label, elapsed, box["error"])
            return Outcome(False, default, "error", elapsed)

        if elapsed > self.timeout / 2:
            # Not a failure, but the margin is thin enough to be worth seeing
            # before it becomes one.
            logger.info("%s: slow read, %.3fs of a %.1fs budget",
                        self.label, elapsed, self.timeout)
        return Outcome(True, box["value"], "ok", elapsed)
