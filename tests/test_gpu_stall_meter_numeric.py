import json
from types import SimpleNamespace

import pytest

import gpu_stall_meter


GB = 1024 ** 3


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), "Infinity", 10 ** 1000],
    ids=["nan", "positive-infinity", "negative-infinity", "infinity-text", "overflow"],
)
def test_nonfinite_readings_become_unavailable(value):
    assert gpu_stall_meter._number(value) is None
    assert gpu_stall_meter._integer(value) is None


def test_corrupt_gpu_readings_preserve_the_others_and_stay_valid_json():
    psutil = SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(total=16 * GB, available=float("inf"), percent=25),
        swap_memory=lambda: SimpleNamespace(total=8 * GB, used=float("nan"), percent=10),
    )
    nvml = SimpleNamespace(
        NVML_TEMPERATURE_GPU=0,
        NVML_CLOCK_SM=1,
        nvmlInit=lambda: None,
        nvmlDeviceGetCount=lambda: 2,
        nvmlDeviceGetHandleByIndex=lambda index: index,
        nvmlDeviceGetName=lambda handle: "GPU %d" % handle,
        nvmlDeviceGetUtilizationRates=lambda handle: SimpleNamespace(
            gpu=float("nan") if handle == 0 else 40),
        nvmlDeviceGetMemoryInfo=lambda handle: SimpleNamespace(
            total=float("inf") if handle == 0 else 1024, used=256),
        nvmlDeviceGetTemperature=lambda handle, _sensor: float("-inf") if handle == 0 else 55,
        nvmlDeviceGetPowerUsage=lambda handle: float("nan") if handle == 0 else 42500,
        nvmlDeviceGetClockInfo=lambda handle, _kind: float("inf") if handle == 0 else 1500,
    )

    snapshot = gpu_stall_meter.GpuStallSampler(psutil, nvml).sample()

    assert snapshot["ok"] is True
    assert snapshot["host"]["ram_available"] is None      # infinity is not a reading
    assert snapshot["host"]["swap_used"] is None          # neither is NaN
    assert snapshot["host"]["ram_total"] == 16 * GB

    broken = snapshot["gpus"][0]
    assert broken["utilization"] is None
    assert broken["power_w"] is None
    assert broken["sm_clock_mhz"] is None
    assert broken["temperature"] is None
    assert broken["vram_total"] is None

    healthy = snapshot["gpus"][1]
    assert healthy["utilization"] == 40
    assert healthy["power_w"] == 42.5
    assert healthy["sm_clock_mhz"] == 1500
    assert healthy["temperature"] == 55

    assert json.loads(json.dumps(snapshot, allow_nan=False)) == snapshot


def test_power_conversion_keeps_fractional_watts():
    nvml = SimpleNamespace(
        NVML_TEMPERATURE_GPU=0,
        NVML_CLOCK_SM=1,
        nvmlInit=lambda: None,
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda index: index,
        nvmlDeviceGetName=lambda _handle: "GPU",
        nvmlDeviceGetUtilizationRates=lambda _handle: SimpleNamespace(gpu=99),
        nvmlDeviceGetMemoryInfo=lambda _handle: SimpleNamespace(total=8 * GB, used=GB),
        nvmlDeviceGetTemperature=lambda _handle, _sensor: 60,
        nvmlDeviceGetPowerUsage=lambda _handle: 22750,
        nvmlDeviceGetClockInfo=lambda _handle, _kind: 2760,
    )

    gpu = gpu_stall_meter.GpuStallSampler(None, nvml).sample()["gpus"][0]

    assert gpu["power_w"] == pytest.approx(22.75)
    assert gpu["sm_clock_mhz"] == 2760
