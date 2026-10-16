"""Top-bar GPU meters that separate computing from paging.

GPU utilization answers "is the SM busy", which is not the same question as "is the
SM doing my work".  A workflow stalled on host-memory paging keeps utilization pinned
at 99-100% while the GPU waits for pages to arrive, so the two states are separable
only from signals other than utilization: GPU power draw, SM clock and host RAM
pressure.  The default thresholds come from measurements documented in README.md.

No GPU library is imported at module import time: `psutil` and `pynvml` are resolved
lazily, and the test suite injects fakes, so this file imports on a machine with no
GPU, no ComfyUI and no third-party packages at all.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Optional

_UNSET = object()
_ROUTE_REGISTERED = False
_SAMPLER = None

DEFAULT_SETTINGS = {
    "enabled": True,
    "interval_ms": 1000,
    "warmup_samples": 30,
    "min_utilization": 80.0,
    "max_power_ratio": 0.5,
    "max_clock_ratio": 0.6,
    "max_free_ram_ratio": 0.10,
}

# Bounds for the settings route.  Anything outside is clamped rather than rejected,
# so a bad value from the frontend can never disable the verdict silently.
_SETTING_BOUNDS = {
    "interval_ms": (100, 60000),
    "warmup_samples": (0, 1000),
    "min_utilization": (0.0, 100.0),
    "max_power_ratio": (0.01, 1.0),
    "max_clock_ratio": (0.01, 1.0),
    "max_free_ram_ratio": (0.0, 1.0),
}


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _integer(value: Any) -> Optional[int]:
    number = _number(value)
    return None if number is None else int(number)


def _decode_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


class GpuStallSampler:
    """Collect one resource snapshot on demand, and judge it against this session."""

    def __init__(self, psutil_module: Any = _UNSET, nvml_module: Any = _UNSET,
                 settings: Optional[dict] = None):
        self._lock = threading.Lock()
        self._psutil = self._load_psutil() if psutil_module is _UNSET else psutil_module
        self._nvml = self._load_nvml() if nvml_module is _UNSET else nvml_module
        self._nvml_ready = False
        self._nvml_attempted = False
        self._nvml_warning_logged = False

        self.settings = dict(DEFAULT_SETTINGS)
        if settings:
            self.settings.update(self._sanitise(settings))
        self.reset_calibration()

    # ------------------------------------------------------------------ loading

    @staticmethod
    def _load_psutil():
        try:
            import psutil

            return psutil
        except Exception as exc:
            logging.warning("[StallMeter] host RAM metrics unavailable: %s", exc)
            return None

    @staticmethod
    def _load_nvml():
        try:
            import pynvml

            return pynvml
        except Exception as exc:
            logging.warning("[StallMeter] NVIDIA metrics unavailable: %s", exc)
            return None

    def _ensure_nvml(self) -> bool:
        if self._nvml_ready:
            return True
        if self._nvml_attempted or self._nvml is None:
            return False

        self._nvml_attempted = True
        try:
            self._nvml.nvmlInit()
            self._nvml_ready = True
        except Exception as exc:
            if not self._nvml_warning_logged:
                logging.warning("[StallMeter] could not initialize NVML: %s", exc)
                self._nvml_warning_logged = True
        return self._nvml_ready

    # ----------------------------------------------------------------- settings

    def _sanitise(self, patch: dict) -> dict:
        clean = {}
        for key, value in (patch or {}).items():
            if key == "enabled":
                clean[key] = bool(value)
            elif key in _SETTING_BOUNDS:
                number = _number(value)
                if number is None:
                    continue
                low, high = _SETTING_BOUNDS[key]
                number = min(high, max(low, number))
                clean[key] = int(number) if key in ("interval_ms", "warmup_samples") else number
        return clean

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            if isinstance(patch, dict) and patch.get("reset_calibration"):
                self.reset_calibration()
            self.settings.update(self._sanitise(patch))
            return self._snapshot_locked()

    def settings_snapshot(self) -> dict:
        with self._lock:
            return dict(self.settings)

    def reset_calibration(self) -> None:
        self._samples = 0
        self._max_power_w = {}
        self._max_sm_clock_mhz = {}

    # ------------------------------------------------------------------- reading

    def _gpu_field(self, method_name: str, *args):
        try:
            return getattr(self._nvml, method_name)(*args)
        except Exception:
            return None

    def _host(self) -> dict:
        result = {
            "ram_total": None, "ram_available": None, "ram_percent": None,
            "swap_total": None, "swap_used": None, "swap_percent": None,
        }
        if self._psutil is None:
            return result

        try:
            memory = self._psutil.virtual_memory()
            result.update({
                "ram_total": _integer(getattr(memory, "total", None)),
                "ram_available": _integer(getattr(memory, "available", None)),
                "ram_percent": _number(getattr(memory, "percent", None)),
            })
        except Exception:
            pass

        try:
            swap = self._psutil.swap_memory()
            result.update({
                "swap_total": _integer(getattr(swap, "total", None)),
                "swap_used": _integer(getattr(swap, "used", None)),
                "swap_percent": _number(getattr(swap, "percent", None)),
            })
        except Exception:
            pass
        return result

    def _gpus(self) -> list:
        if not self._ensure_nvml():
            return []

        try:
            count = int(self._nvml.nvmlDeviceGetCount())
        except Exception:
            return []

        gpus = []
        for index in range(max(0, count)):
            try:
                handle = self._nvml.nvmlDeviceGetHandleByIndex(index)
            except Exception:
                continue

            name = _decode_text(self._gpu_field("nvmlDeviceGetName", handle))
            utilization = self._gpu_field("nvmlDeviceGetUtilizationRates", handle)
            memory = self._gpu_field("nvmlDeviceGetMemoryInfo", handle)
            temperature = self._gpu_field(
                "nvmlDeviceGetTemperature", handle, getattr(self._nvml, "NVML_TEMPERATURE_GPU", 0))
            # NVML reports power in milliwatts; parts without a power sensor raise here.
            power_milliwatts = _number(self._gpu_field("nvmlDeviceGetPowerUsage", handle))
            clock = _number(self._gpu_field(
                "nvmlDeviceGetClockInfo", handle, getattr(self._nvml, "NVML_CLOCK_SM", 1)))

            power_w = None if power_milliwatts is None else power_milliwatts / 1000.0

            total = _integer(getattr(memory, "total", None))
            used = _integer(getattr(memory, "used", None))
            vram_percent = None
            if total and used is not None:
                vram_percent = (used / total) * 100.0

            if power_w is not None:
                self._max_power_w[index] = max(self._max_power_w.get(index, 0.0), power_w)
            if clock is not None:
                self._max_sm_clock_mhz[index] = max(self._max_sm_clock_mhz.get(index, 0.0), clock)

            gpus.append({
                "index": index,
                "name": name or "GPU %d" % index,
                "utilization": _number(getattr(utilization, "gpu", None)),
                "power_w": power_w,
                "sm_clock_mhz": clock,
                "temperature": _number(temperature),
                "vram_total": total,
                "vram_used": used,
                "vram_percent": vram_percent,
            })
        return gpus

    # ------------------------------------------------------------------ verdict

    def _verdict(self, gpu: dict, host: dict) -> dict:
        """Judge one sample against this session's own maxima.

        Absolute watts are not comparable between cards, so every threshold is a
        ratio against the highest value this session has seen.  The RAM condition is
        what keeps the verdict honest: a light workflow can sit at 99% utilization and
        low power for perfectly good reasons, and what makes the state pathological is
        that the GPU is waiting on the pagefile.
        """
        settings = self.settings
        utilization = gpu.get("utilization")
        if utilization is None or utilization < settings["min_utilization"]:
            return {"state": "idle", "reason": "utilization below %.0f%%" % settings["min_utilization"]}

        if self._samples < settings["warmup_samples"]:
            return {"state": "ok", "reason": "calibrating (%d/%d samples)" % (
                self._samples, settings["warmup_samples"])}

        index = gpu.get("index")
        evidence = []
        signals = []

        power_w = gpu.get("power_w")
        power_max = self._max_power_w.get(index)
        if power_w is not None and power_max:
            depressed = power_w <= power_max * settings["max_power_ratio"]
            signals.append(depressed)
            if depressed:
                evidence.append("power is %.1f W against a session max of %.1f W" % (power_w, power_max))

        clock = gpu.get("sm_clock_mhz")
        clock_max = self._max_sm_clock_mhz.get(index)
        if clock is not None and clock_max:
            depressed = clock <= clock_max * settings["max_clock_ratio"]
            signals.append(depressed)
            if depressed:
                evidence.append("SM clock is %.0f MHz against a session max of %.0f MHz" % (clock, clock_max))

        ram_total = host.get("ram_total")
        ram_available = host.get("ram_available")
        ram_ratio = None
        if ram_total and ram_available is not None:
            ram_ratio = ram_available / ram_total
        ram_low = ram_ratio is not None and ram_ratio <= settings["max_free_ram_ratio"]
        if ram_low:
            evidence.append("only %.2f GiB of %.2f GiB host RAM is free" % (
                ram_available / 1024 ** 3, ram_total / 1024 ** 3))

        if not signals:
            return {"state": "ok", "reason":
                    "no GPU power or clock source; utilization and RAM alone cannot "
                    "separate working from waiting"}

        if all(signals) and ram_low:
            return {"state": "stalled", "reason":
                    "GPU utilization is %.0f%% but %s" % (utilization, ", ".join(evidence))}

        return {"state": "ok", "reason": "utilization %.0f%% with no stall evidence" % utilization}

    # ------------------------------------------------------------------- snapshot

    def sample(self) -> dict:
        # Overlapping requests from several open tabs are expected; serializing keeps
        # the NVML snapshot and the calibration counters consistent.
        with self._lock:
            self._samples += 1
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        host = self._host()
        gpus = self._gpus()
        for gpu in gpus:
            gpu["stall"] = self._verdict(gpu, host)
        return {
            "ok": True,
            "sampled_at": int(time.time() * 1000),
            "samples": self._samples,
            "calibrating": self._samples < self.settings["warmup_samples"],
            "settings": dict(self.settings),
            "host": host,
            "gpus": gpus,
        }


def get_sampler() -> GpuStallSampler:
    global _SAMPLER
    if _SAMPLER is None:
        _SAMPLER = GpuStallSampler()
    return _SAMPLER


def get_snapshot() -> dict:
    return get_sampler().sample()


def update_settings(patch: dict) -> dict:
    return get_sampler().update_settings(patch)


def register_gpu_stall_meter_routes() -> None:
    global _ROUTE_REGISTERED
    if _ROUTE_REGISTERED:
        return

    try:
        from aiohttp import web
        from server import PromptServer
    except Exception:
        return

    routes = getattr(getattr(PromptServer, "instance", None), "routes", None)
    if routes is None:
        return

    @routes.get("/gpu-stall-meter/sample")
    async def gpu_stall_meter_sample(_request):
        response = web.json_response(get_snapshot())
        response.headers["Cache-Control"] = "no-store"
        return response

    @routes.post("/gpu-stall-meter/settings")
    async def gpu_stall_meter_settings(request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return web.json_response(update_settings(payload if isinstance(payload, dict) else {}))

    _ROUTE_REGISTERED = True
