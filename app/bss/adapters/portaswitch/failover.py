"""Disaster-recovery (geographically dispersed PortaSwitch) failover support.

When the main PortaSwitch site goes down, the installation switches to standalone
(delta) mode: only the secondary site is operational and its API is read-only.

A PortaSwitch session is site-local, and the secondary-site session sync
(BA-47622) resolves a session used on both sites at once by last-write-wins, so
a refresh token minted on one site can be overwritten by the other site's older
row. Every adapter replica must therefore use the same site at the same time.

Two modes, chosen per pod by what the sites report:

* Mode-aware (the sites report `operating_mode`, BA-47641): a monitor polls both
  sites on wall-clock aligned ticks, so every replica asks at the same instant and
  gets the same answers. The pod moves to the secondary site when it reports
  `standalone`, or when the main site does not answer (or reports `secondary`)
  for several ticks in a row, and moves back once the main site reports `normal`
  for several ticks and the secondary site is no longer `standalone`. Requests go
  to the active site only: a failed request never retries on the other site and
  never switches the pod by itself, because that is what put one session on both
  sites.
* Reactive (the sites answer without `operating_mode`, or have not answered yet):
  the behaviour before the monitor. A connect error on the main site switches the
  pod and retries the request on the secondary site; a read timeout only has the
  main site probed out-of-band (WT-2201); switching back waits for `normal`.

The secondary site also identifies its mode through fault codes returned on calls
not allowed in the current mode (used for graceful write degradation).

This module holds the shared active-site state, the PortaBilling fault codes, and
the operating_mode values used to drive the failover.
"""
import asyncio
import enum
import logging
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

# Fault codes raised by a PortaBilling secondary site to signal its current mode.
STANDALONE_MODE_FAULT = "standalone_mode"
SECONDARY_SITE_FAULT = "secondary_site"
READ_ONLY_MODE_FAULT = "read_only_mode"
DELTA_CONNECTION_FAILED_FAULT = "pb_delta.connection_failed"

# Faults that mean the request reached a site that cannot service writes/updates
# right now (delta / read-only). They are the signal to degrade gracefully.
#
# DELTA_CONNECTION_FAILED_FAULT is intentionally NOT included: it signals that
# the delta DB itself is unreachable (an infrastructure failure), not that the
# method was rejected for being a write in read-only mode.
READ_ONLY_FAULTS = frozenset(
    {
        STANDALONE_MODE_FAULT,
        SECONDARY_SITE_FAULT,
        READ_ONLY_MODE_FAULT,
    }
)

# Faults returned when the site that answered does not accept the session token
# presented with the request. After a failover that is the normal outcome for a
# token the app is still holding: PortaSwitch sessions are site-local, so one
# minted by the main site means nothing to the standby (BA-47630 / BA-47622).
# The admin realm recovers by logging in again; the account realm cannot - the
# session belongs to the subscriber - so it reports an expired token (WT-1814).
SESSION_AUTH_FAULTS = frozenset(
    {
        "Server.Session.check_auth.auth_failed",
        "Client.Session.check_auth.failed_to_process_access_token",
    }
)

# operating_mode values reported by generic.get_session_data (BA-47641).
OPERATING_MODE_NORMAL = "normal"
OPERATING_MODE_SECONDARY = "secondary"
OPERATING_MODE_STANDALONE = "standalone"
OPERATING_MODE_READ_ONLY = "read_only"
# The site answered but did not report operating_mode (PortaSwitch without BA-47641).
OPERATING_MODE_UNKNOWN = "unknown"
# The modes a site reports when it implements BA-47641.
KNOWN_OPERATING_MODES = frozenset(
    {OPERATING_MODE_NORMAL, OPERATING_MODE_SECONDARY, OPERATING_MODE_STANDALONE, OPERATING_MODE_READ_ONLY}
)


class Site(enum.Enum):
    MAIN = "main"
    STANDBY = "standby"


class SiteState:
    """Active-site tracker shared by the Admin and Account API connectors of a
    single PortaSwitch adapter (both sites fail and recover together).

    Runs on the single asyncio event loop, so its flags are plain attributes with
    no locking (same rationale as the token storage in ``AsyncHTTPAPIConnector``).

    Mode-aware switching (both sites report ``operating_mode``): a monitor task
    polls both sites on wall-clock aligned ticks of ``poll_interval`` seconds
    (see the module docstring). It starts on the first request routed through
    this tracker, inside the running event loop, and only when both probes are
    set. Until a site reports a known mode the tracker stays reactive.

    Reactive switching (sites without ``operating_mode``):

    Forward switch (MAIN -> STANDBY): the request layer calls
    ``report_unreachable()`` when a main-site request fails to connect. A read
    timeout calls ``report_slow()`` instead, which probes the main site
    out-of-band and switches only if the probe confirms an outage (WT-2201).

    Backward switch (STANDBY -> MAIN): while on standby, user requests are routed
    straight to the standby (never to the dead main), and the main site is probed
    **out-of-band** via a fire-and-forget task (``probe``); we return to the main
    site only once it reports ``operating_mode == normal`` for
    ``switch_back_threshold`` consecutive probes (hysteresis to avoid flapping and
    to avoid returning while the site is still recovering / read-only).
    """

    def __init__(
        self,
        recheck_interval: int = 60,
        switch_back_threshold: int = 2,
        poll_interval: int = 10,
        main_down_threshold: int = 3,
    ):
        """
        Parameters:
            recheck_interval (int): Seconds between out-of-band main-site probes
                while running on standby (reactive mode).
            switch_back_threshold (int): Consecutive ``normal`` probe results
                required before switching back to the main site.
            poll_interval (int): Seconds between monitor ticks (mode-aware mode).
                Ticks are aligned to the wall clock, so replicas poll together.
            main_down_threshold (int): Consecutive monitor ticks on which the main
                site does not answer (or reports ``secondary``) before the pod
                moves to the secondary site (mode-aware mode).
        """
        self._active = Site.MAIN
        self._recheck_interval = timedelta(seconds=recheck_interval)
        self._switch_back_threshold = max(1, switch_back_threshold)
        self._last_probe: Optional[datetime] = None
        self._normal_streak = 0
        self._probe_in_flight = False
        self._probe_task: Optional[asyncio.Task] = None
        self._confirm_in_flight = False
        self._confirm_task: Optional[asyncio.Task] = None
        #: async callable returning the main site's operating_mode (or None if
        #: unreachable / not reported). Injected by the adapter after the Admin
        #: connector exists.
        self._probe: Optional[Callable[[], Awaitable[Optional[str]]]] = None
        #: the same for the secondary site; the monitor runs only when both are set.
        self._standby_probe: Optional[Callable[[], Awaitable[Optional[str]]]] = None
        self._poll_interval = max(1, poll_interval)
        self._main_down_threshold = max(1, main_down_threshold)
        self._mode_aware = False
        self._main_down_streak = 0
        self._last_modes: tuple = (None, None)
        self._monitor_task: Optional[asyncio.Task] = None

    @property
    def active(self) -> Site:
        return self._active

    @property
    def mode_aware(self) -> bool:
        """True while the sites report ``operating_mode`` and the monitor decides."""
        return self._mode_aware

    def set_probe(self, probe: Callable[[], Awaitable[Optional[str]]]) -> None:
        self._probe = probe

    def set_standby_probe(self, probe: Callable[[], Awaitable[Optional[str]]]) -> None:
        self._standby_probe = probe

    def next_targets(self, main_server: str, standby_server: str) -> list:
        """Ordered base server URLs to try for the next request.

        Mode-aware: the active site only. Reactive: on the main site try main and
        fall back to standby; on standby go straight to the standby (the main site
        is known-down; probing it is done out-of-band) and schedule a switch-back
        probe when due.
        """
        self._ensure_monitor()
        if self._mode_aware:
            return [main_server if self._active is Site.MAIN else standby_server]

        if self._active is Site.MAIN:
            return [main_server, standby_server]

        self._maybe_schedule_probe()
        return [standby_server]

    def report_unreachable(self) -> None:
        """Record that the main site failed to respond (timeout/connection error).

        Mode-aware, one failed request does not switch the pod: the monitor
        decides from what both sites report, the same way on every replica.
        """
        if self._mode_aware:
            logging.info("PortaSwitch DR: main site request failed to connect; the site monitor decides")
            return
        if self._active is not Site.STANDBY:
            logging.warning("PortaSwitch DR: main site unreachable, switching to standby site")
        self._active = Site.STANDBY
        self._normal_streak = 0

    def report_slow(self, path: str) -> None:
        """Record that a main-site request timed out after the site accepted it.

        Without a probe there is no way to tell a slow site from a dead one, so
        this falls back to ``report_unreachable()``. Mode-aware, the monitor
        already polls the main site, so nothing more is done.
        """
        if self._mode_aware:
            return
        if self._active is not Site.MAIN or self._confirm_in_flight:
            return
        if self._probe is None:
            self.report_unreachable()
            return
        logging.warning(f"PortaSwitch DR: main site timed out on {path}, checking whether it is up")
        self._confirm_in_flight = True
        try:
            self._confirm_task = asyncio.get_running_loop().create_task(self._confirm_outage())
        except RuntimeError:
            self._confirm_in_flight = False

    async def _confirm_outage(self) -> None:
        try:
            try:
                mode = await self._probe()
            except Exception as e:
                logging.warning(f"PortaSwitch DR: main site check failed: {e}")
                mode = None
            if mode is None or mode == OPERATING_MODE_SECONDARY:
                logging.warning(f"PortaSwitch DR: main site check got {mode or 'no answer'}")
                self.report_unreachable()
            else:
                logging.info(f"PortaSwitch DR: main site answers ({mode}), staying on it")
        finally:
            self._confirm_in_flight = False

    def _ensure_monitor(self) -> None:
        """Start the site monitor once both probes are set and a loop is running."""
        if self._monitor_task is not None or self._probe is None or self._standby_probe is None:
            return
        try:
            self._monitor_task = asyncio.get_running_loop().create_task(self._monitor())
        except RuntimeError:
            # No running loop (e.g. called outside the event loop in a test).
            self._monitor_task = None

    async def _monitor(self) -> None:
        """Poll both sites on wall-clock aligned ticks for the life of the process."""
        while True:
            await asyncio.sleep(self._poll_interval - (time.time() % self._poll_interval))
            try:
                await self.poll_once()
            except Exception as e:  # never let the monitor die
                logging.warning(f"PortaSwitch DR: site monitor tick failed: {e}")

    async def _ask(self, probe: Callable[[], Awaitable[Optional[str]]]) -> Optional[str]:
        """One probe, bounded by the poll interval; None when the site does not answer."""
        try:
            return await asyncio.wait_for(probe(), timeout=self._poll_interval)
        except Exception:
            return None

    async def poll_once(self) -> None:
        """One monitor tick: read both sites' operating_mode and decide."""
        main_mode, standby_mode = await asyncio.gather(self._ask(self._probe), self._ask(self._standby_probe))

        if (main_mode, standby_mode) != self._last_modes:
            logging.info(
                f"PortaSwitch DR: site modes main={main_mode or 'no answer'} "
                f"standby={standby_mode or 'no answer'} (active: {self._active.value})"
            )
            self._last_modes = (main_mode, standby_mode)

        answered = [m for m in (main_mode, standby_mode) if m is not None]
        if answered:
            mode_aware = any(m in KNOWN_OPERATING_MODES for m in answered)
            if mode_aware != self._mode_aware:
                logging.warning(
                    "PortaSwitch DR: the sites report operating_mode, site switching follows it"
                    if mode_aware
                    else "PortaSwitch DR: the sites do not report operating_mode, site switching is reactive"
                )
                self._mode_aware = mode_aware
                self._main_down_streak = 0
                self._normal_streak = 0
        if not self._mode_aware:
            return

        if self._active is Site.MAIN:
            if standby_mode == OPERATING_MODE_STANDALONE:
                self._switch(Site.STANDBY, "the secondary site reports standalone")
                return
            # Counted only while the secondary site answers: when neither site
            # answers, the fault is on this side, and a switch would only move the
            # sessions to the other site for nothing.
            if standby_mode is not None and (main_mode is None or main_mode == OPERATING_MODE_SECONDARY):
                self._main_down_streak += 1
                if self._main_down_streak >= self._main_down_threshold:
                    self._switch(
                        Site.STANDBY,
                        f"the main site reported {main_mode or 'no answer'} "
                        f"{self._main_down_streak} ticks in a row",
                    )
            else:
                self._main_down_streak = 0
            return

        if main_mode == OPERATING_MODE_NORMAL and standby_mode != OPERATING_MODE_STANDALONE:
            self._normal_streak += 1
            if self._normal_streak >= self._switch_back_threshold:
                self._switch(Site.MAIN, f"the main site reported normal {self._normal_streak} ticks in a row")
        else:
            self._normal_streak = 0

    def _switch(self, site: Site, reason: str) -> None:
        logging.warning(f"PortaSwitch DR: switching to the {site.value} site: {reason}")
        self._active = site
        self._main_down_streak = 0
        self._normal_streak = 0

    def _maybe_schedule_probe(self) -> None:
        """Kick off a single out-of-band main-site probe if one is due and none is
        already running. Never blocks the caller (fire-and-forget task)."""
        if self._probe is None or self._probe_in_flight:
            return
        now = datetime.now()
        if self._last_probe is not None and (now - self._last_probe) < self._recheck_interval:
            return
        self._last_probe = now
        self._probe_in_flight = True
        try:
            # Keep a reference so the task is not garbage-collected mid-flight.
            self._probe_task = asyncio.get_running_loop().create_task(self._run_probe())
        except RuntimeError:
            # No running loop (e.g. called outside the event loop in a test).
            self._probe_in_flight = False

    async def _run_probe(self) -> None:
        try:
            mode = await self._probe()
            if mode == OPERATING_MODE_NORMAL:
                self._normal_streak += 1
                logging.info(
                    "PortaSwitch DR: main site probe reported normal "
                    f"({self._normal_streak}/{self._switch_back_threshold})"
                )
                if self._normal_streak >= self._switch_back_threshold:
                    logging.warning("PortaSwitch DR: main site back to normal mode, switching back")
                    self._active = Site.MAIN
                    self._normal_streak = 0
            else:
                # Unreachable (None) or not yet normal (standalone/read_only/…):
                # stay on standby and reset the hysteresis streak.
                self._normal_streak = 0
        except Exception as e:  # never let a background probe crash the loop
            logging.warning(f"PortaSwitch DR: main site probe failed: {e}")
            self._normal_streak = 0
        finally:
            self._probe_in_flight = False
