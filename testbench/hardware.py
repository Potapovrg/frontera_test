"""USB port resolution for the Frontera units and the frequency shifter.

All three units on the bus report the *same* USB identity — VID:PID 0483:5740,
and both Fronteras even share serial 00000000029A — so neither by-id matching
nor a plain VID:PID scan can tell them apart. Two mechanisms cover that:

* udev symlinks keyed on the physical USB port (ID_PATH), created by
  frontera_ml/deploy/orangepi/setup_frontera_usb_ports.sh -> /dev/frontera1,
  /dev/frontera2, /dev/switcher. This is the reliable path on the OrangePi.
* behavioural probing as the fallback, for a USB hub, a unit moved to another
  port, or a bench that isn't the Pi: the shifter answers ``ping`` with the
  magic 0xAC1B3C55, a Frontera answers a ``scn20`` probe.

The shifter is always resolved *first* so its port can be handed to
``find_sa6(exclude=...)`` — otherwise the SDR probe would fire scn20 at the
STM32 (mirrors the exclude wiring in frontera_ml/services/detector.py:119).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from frontera_interface import find_sa6

from .switcher import DEFAULT_SYMLINK as SWITCHER_SYMLINK
from .switcher import resolve_switcher_port

logger = logging.getLogger(__name__)

FRONTERA1_SYMLINK = "/dev/frontera1"
FRONTERA2_SYMLINK = "/dev/frontera2"

SETUP_SCRIPT = "frontera_ml/deploy/orangepi/setup_frontera_usb_ports.sh"


def _realpaths(ports) -> set:
    return {os.path.realpath(p) for p in ports if p}


def resolve_frontera(explicit: str = "", prefer: str = "", exclude=()) -> tuple:
    """Locate one Frontera. Returns ``(port | None, source)``.

    ``source`` is one of ``"explicit" | "symlink" | "probe" | "none"`` so the
    UI can tell the operator *how* the unit was found — a probed port in
    2_device mode means "which of the two this is, is anyone's guess".

    ``prefer`` is a udev symlink tried before probing, and only if it actually
    exists (same guard as frontera_ml/services/detector.py:42-52 — off-Pi runs
    must not get a hardcoded path that isn't there).
    """
    if explicit:
        return explicit, "explicit"
    if prefer and Path(prefer).exists() and os.path.realpath(prefer) not in _realpaths(exclude):
        return prefer, "symlink"
    found = find_sa6(exclude=exclude)
    if found:
        return found, "probe"
    return None, "none"


def resolve_switcher(explicit: str = "", exclude=()) -> tuple:
    """Locate the frequency shifter. Returns ``(port | None, source)``."""
    if explicit:
        return explicit, "explicit"
    if Path(SWITCHER_SYMLINK).exists():
        return SWITCHER_SYMLINK, "symlink"
    found = resolve_switcher_port("", exclude=exclude)
    if found:
        return found, "probe"
    return None, "none"


def resolve_topology(cfg) -> dict:
    """Resolve every port the current ``cfg.mode()`` needs, in dependency order.

    Returns a dict the DeviceManager consumes and the UI renders::

        {"mode": str,
         "switcher": {"port": str|None, "source": str},
         "frontera1": {...}, "frontera2": {...},
         "warnings": [str, ...]}

    Never raises and never opens a sweep session — this is discovery only.
    """
    mode = cfg.mode()
    warnings: list = []
    result = {
        "mode": mode,
        "switcher": {"port": None, "source": "disabled"},
        "frontera1": {"port": None, "source": "disabled"},
        "frontera2": {"port": None, "source": "disabled"},
        "warnings": warnings,
    }

    if cfg.mock:
        # Only fake the units this mode actually uses, so the UI shows the same
        # shape it would with real hardware.
        if cfg.switcher_enabled():
            result["switcher"] = {"port": "mock", "source": "mock"}
        if mode == "1_device_full_range":
            result["frontera2"] = {"port": "mock", "source": "mock"}
        else:
            result["frontera1"] = {"port": "mock", "source": "mock"}
            if cfg.uses_second_device():
                result["frontera2"] = {"port": "mock", "source": "mock"}
        return result

    taken: list = []

    # 1. shifter first, so its port is excluded from every scn20 probe below.
    if cfg.switcher_enabled():
        port, source = resolve_switcher(cfg.switcher_port,
                                        exclude=[cfg.device_port, cfg.device_port2,
                                                 FRONTERA1_SYMLINK, FRONTERA2_SYMLINK])
        result["switcher"] = {"port": port, "source": source}
        if port:
            taken.append(port)
        else:
            warnings.append(
                "Shifter not found: check /dev/switcher, the USB connection "
                f"or set --switcher-port. Mode '{cfg.mode_label()}' does not work without it."
            )

    # 2. the low-band / only Frontera.
    if mode == "1_device_full_range":
        # A single unit sitting behind the shifter — whichever symlink it
        # landed on is fine, there is nothing to confuse it with.
        prefer = next((s for s in (FRONTERA2_SYMLINK, FRONTERA1_SYMLINK)
                       if Path(s).exists()), "")
        port, source = resolve_frontera(cfg.device_port2 or cfg.device_port,
                                        prefer=prefer, exclude=taken)
        result["frontera2"] = {"port": port, "source": source}
        if port:
            taken.append(port)
        else:
            warnings.append("Frontera not found: check the USB connection.")
        return result

    port1, source1 = resolve_frontera(cfg.device_port, prefer=FRONTERA1_SYMLINK,
                                      exclude=taken)
    result["frontera1"] = {"port": port1, "source": source1}
    if port1:
        taken.append(port1)
    else:
        warnings.append("Frontera 1 not found: check the USB connection.")

    # 3. the shifted Frontera, excluding everything already claimed.
    if cfg.uses_second_device():
        port2, source2 = resolve_frontera(cfg.device_port2, prefer=FRONTERA2_SYMLINK,
                                          exclude=taken)
        result["frontera2"] = {"port": port2, "source": source2}
        if not port2:
            warnings.append("Frontera 2 (behind the shifter) not found.")
        elif "probe" in (source1, source2):
            # Both units answer scn20 identically; without ID_PATH symlinks the
            # low/high assignment is whatever order the probe happened to hit.
            warnings.append(
                "Two Fronteras were found by auto-probe — it cannot be told which one "
                f"is behind the shifter. Run {SETUP_SCRIPT} "
                "(symlinks /dev/frontera1, /dev/frontera2) or set "
                "--device-port / --device-port2 explicitly."
            )

    return result
