"""Minimal NVML binding through ctypes, used when `pynvml` is not installed.

The driver's own NVML library is already present on any machine that can run a CUDA
workload, so binding it directly keeps this pack dependency-free.  Only the calls the
sampler needs are bound, and every method raises on a non-zero NVML status so the
caller's existing try/except degrades a single metric instead of the whole snapshot.

Verified on Windows against nvml.dll with an RTX 5060 Laptop: the `_v2` entry points
used here are the ones the driver exports.
"""

from __future__ import annotations

import ctypes
import platform
from typing import Optional

NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0
NVML_CLOCK_SM = 1

_NAME_BUFFER = 96


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Memory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


def _library_names():
    system = platform.system()
    if system == "Windows":
        return ["nvml.dll"]
    if system == "Darwin":
        return []
    return ["libnvidia-ml.so.1", "libnvidia-ml.so"]


class NvmlCtypes:
    """The subset of the pynvml interface this pack uses."""

    NVML_TEMPERATURE_GPU = NVML_TEMPERATURE_GPU
    NVML_CLOCK_SM = NVML_CLOCK_SM

    def __init__(self, library: Optional[ctypes.CDLL] = None):
        if library is not None:
            self._lib = library
            return

        last_error = None
        for name in _library_names():
            try:
                self._lib = ctypes.CDLL(name)
                return
            except OSError as exc:      # not installed under this name
                last_error = exc
        raise RuntimeError("NVML library not found (%s)" % (last_error or "no candidate names"))

    def _function(self, names, argtypes, restype=ctypes.c_int):
        for name in names:
            try:
                function = getattr(self._lib, name)
            except AttributeError:
                continue
            function.argtypes = argtypes
            function.restype = restype
            return function, name
        raise RuntimeError("NVML function missing: %s" % "/".join(names))

    @staticmethod
    def _check(name, status):
        if status != NVML_SUCCESS:
            raise RuntimeError("%s returned %d" % (name, status))

    # ---------------------------------------------------------------- entry points

    def nvmlInit(self):
        function, name = self._function(["nvmlInit_v2", "nvmlInit"], [])
        self._check(name, function())

    def nvmlDeviceGetCount(self):
        function, name = self._function(
            ["nvmlDeviceGetCount_v2", "nvmlDeviceGetCount"], [ctypes.POINTER(ctypes.c_uint)])
        count = ctypes.c_uint()
        self._check(name, function(ctypes.byref(count)))
        return count.value

    def nvmlDeviceGetHandleByIndex(self, index):
        function, name = self._function(
            ["nvmlDeviceGetHandleByIndex_v2", "nvmlDeviceGetHandleByIndex"],
            [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)])
        handle = ctypes.c_void_p()
        self._check(name, function(ctypes.c_uint(index), ctypes.byref(handle)))
        return handle

    def nvmlDeviceGetName(self, handle):
        function, name = self._function(
            ["nvmlDeviceGetName"], [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint])
        buffer = ctypes.create_string_buffer(_NAME_BUFFER)
        self._check(name, function(handle, buffer, ctypes.c_uint(_NAME_BUFFER)))
        return buffer.value.decode("utf-8", "replace")

    def nvmlDeviceGetUtilizationRates(self, handle):
        function, name = self._function(
            ["nvmlDeviceGetUtilizationRates"],
            [ctypes.c_void_p, ctypes.POINTER(_Utilization)])
        rates = _Utilization()
        self._check(name, function(handle, ctypes.byref(rates)))
        return rates

    def nvmlDeviceGetMemoryInfo(self, handle):
        function, name = self._function(
            ["nvmlDeviceGetMemoryInfo"], [ctypes.c_void_p, ctypes.POINTER(_Memory)])
        memory = _Memory()
        self._check(name, function(handle, ctypes.byref(memory)))
        return memory

    def nvmlDeviceGetTemperature(self, handle, sensor):
        function, name = self._function(
            ["nvmlDeviceGetTemperature"],
            [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)])
        value = ctypes.c_uint()
        self._check(name, function(handle, ctypes.c_uint(sensor), ctypes.byref(value)))
        return value.value

    def nvmlDeviceGetPowerUsage(self, handle):
        """Milliwatts, like pynvml.  Raises on parts without a power sensor."""
        function, name = self._function(
            ["nvmlDeviceGetPowerUsage"],
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)])
        value = ctypes.c_uint()
        self._check(name, function(handle, ctypes.byref(value)))
        return value.value

    def nvmlDeviceGetClockInfo(self, handle, clock_type):
        function, name = self._function(
            ["nvmlDeviceGetClockInfo"],
            [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)])
        value = ctypes.c_uint()
        self._check(name, function(handle, ctypes.c_uint(clock_type), ctypes.byref(value)))
        return value.value


def open_nvml() -> NvmlCtypes:
    return NvmlCtypes()
