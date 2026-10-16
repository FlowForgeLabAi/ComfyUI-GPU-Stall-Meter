from pathlib import Path
from types import SimpleNamespace

import gpu_stall_meter


REPO_ROOT = Path(__file__).resolve().parents[1]
GB = 1024 ** 3


def host_metrics(available_gb, total_gb=15.26, swap_used_gb=3.0):
    percent = 100.0 - (available_gb / total_gb * 100.0)
    return SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(
            total=int(total_gb * GB),
            available=int(available_gb * GB),
            percent=percent,
        ),
        swap_memory=lambda: SimpleNamespace(
            total=int(8 * GB),
            used=int(swap_used_gb * GB),
            percent=swap_used_gb / 8.0 * 100.0,
        ),
    )


def nvml(power_w=95.0, clock_mhz=2700.0, utilization=99.0, temperature=55.0,
         power_supported=True, clock_supported=True):
    utilization_rates = SimpleNamespace(gpu=utilization)
    memory = SimpleNamespace(total=8 * GB, used=7 * GB)

    def get_power(_handle):
        if not power_supported:
            raise RuntimeError("NotSupported")
        return power_w * 1000.0

    def get_clock(_handle, _kind):
        if not clock_supported:
            raise RuntimeError("NotSupported")
        return clock_mhz

    return SimpleNamespace(
        NVML_TEMPERATURE_GPU=0,
        NVML_CLOCK_SM=1,
        nvmlInit=lambda: None,
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda index: index,
        nvmlDeviceGetName=lambda _handle: b"Fixture GPU",
        nvmlDeviceGetUtilizationRates=lambda _handle: utilization_rates,
        nvmlDeviceGetMemoryInfo=lambda _handle: memory,
        nvmlDeviceGetTemperature=lambda _handle, _sensor: temperature,
        nvmlDeviceGetPowerUsage=get_power,
        nvmlDeviceGetClockInfo=get_clock,
    )


def calibrated(settings=None, power_w=95.0, clock_mhz=2700.0, available_gb=8.0, samples=3,
               power_supported=True, clock_supported=True):
    """A sampler that has already seen `samples` healthy snapshots."""
    settings = dict({"warmup_samples": samples}, **(settings or {}))
    sampler = gpu_stall_meter.GpuStallSampler(
        host_metrics(available_gb),
        nvml(power_w=power_w, clock_mhz=clock_mhz, power_supported=power_supported,
             clock_supported=clock_supported),
        settings)
    for _ in range(samples):
        sampler.sample()
    return sampler


def test_snapshot_reports_every_reading():
    sampler = gpu_stall_meter.GpuStallSampler(host_metrics(8.0), nvml())

    snapshot = sampler.sample()

    assert snapshot["ok"] is True
    assert snapshot["samples"] == 1
    assert snapshot["calibrating"] is True
    assert snapshot["host"]["ram_total"] == int(15.26 * GB)
    assert snapshot["host"]["ram_available"] == int(8 * GB)
    assert snapshot["host"]["swap_used"] == int(3 * GB)

    gpu = snapshot["gpus"][0]
    assert gpu["index"] == 0
    assert gpu["name"] == "Fixture GPU"
    assert gpu["utilization"] == 99.0
    assert gpu["power_w"] == 95.0          # NVML reports milliwatts; we publish watts
    assert gpu["sm_clock_mhz"] == 2700.0
    assert gpu["temperature"] == 55.0
    assert gpu["vram_percent"] == 87.5
    assert gpu["stall"]["state"] == "ok"   # busy, but calibration has not finished yet


class _BrokenNvml:
    @staticmethod
    def nvmlInit():
        raise RuntimeError("NVML unavailable")


def test_host_metrics_survive_when_nvml_is_unavailable():
    sampler = gpu_stall_meter.GpuStallSampler(host_metrics(8.0), _BrokenNvml())

    snapshot = sampler.sample()

    assert snapshot["ok"] is True
    assert snapshot["gpus"] == []
    assert snapshot["host"]["ram_available"] == int(8 * GB)


def test_verdict_turns_stalled_once_power_clock_and_ram_collapse():
    sampler = calibrated()

    healthy = sampler.sample()
    assert healthy["calibrating"] is False
    assert healthy["gpus"][0]["stall"]["state"] == "ok"

    # The pagefile stall: the GPU waits for pages, RAM is gone, and utilization still
    # reads 99%.  Only power and clock reveal it.
    sampler._psutil = host_metrics(1.0)
    sampler._nvml = nvml(power_w=34.0, clock_mhz=200.0)
    stalled = sampler.sample()

    gpu = stalled["gpus"][0]
    assert gpu["utilization"] == 99.0
    assert gpu["stall"]["state"] == "stalled"
    assert "34.0 W" in gpu["stall"]["reason"]
    assert "200 MHz" in gpu["stall"]["reason"]
    assert "host RAM is free" in gpu["stall"]["reason"]


def test_low_power_with_plenty_of_ram_is_not_stalled():
    """A small workflow may legitimately run at high utilization and low power."""
    sampler = calibrated()

    sampler._psutil = host_metrics(8.0)
    sampler._nvml = nvml(power_w=34.0, clock_mhz=200.0)
    snapshot = sampler.sample()

    assert snapshot["gpus"][0]["stall"]["state"] == "ok"


def test_low_ram_alone_is_not_a_verdict_without_power_or_clock():
    sampler = calibrated(power_supported=False, clock_supported=False)

    sampler._psutil = host_metrics(0.5)
    snapshot = sampler.sample()

    stall = snapshot["gpus"][0]["stall"]
    assert stall["state"] == "ok"
    assert "cannot" in stall["reason"]


def test_verdict_is_idle_when_the_gpu_is_not_busy():
    sampler = calibrated()

    sampler._psutil = host_metrics(0.5)
    sampler._nvml = nvml(utilization=12.0, power_w=34.0, clock_mhz=200.0)
    snapshot = sampler.sample()

    stall = snapshot["gpus"][0]["stall"]
    assert stall["state"] == "idle"
    assert "utilization below" in stall["reason"]


def test_settings_are_clamped_and_unknown_keys_ignored():
    sampler = gpu_stall_meter.GpuStallSampler(host_metrics(8.0), nvml())

    snapshot = sampler.update_settings({
        "min_utilization": 999,
        "warmup_samples": -5,
        "max_power_ratio": 0.0,
        "enabled": 0,
        "not_a_setting": "ignored",
    })

    settings = snapshot["settings"]
    assert settings["min_utilization"] == 100.0
    assert settings["warmup_samples"] == 0
    assert settings["max_power_ratio"] == 0.01
    assert settings["enabled"] is False
    assert "not_a_setting" not in settings


def test_reset_calibration_restarts_the_warmup():
    sampler = calibrated()

    snapshot = sampler.update_settings({"reset_calibration": True})

    assert snapshot["samples"] == 0
    assert snapshot["calibrating"] is True


def test_registering_routes_outside_comfyui_is_silent():
    # No ComfyUI, no aiohttp server: this must return quietly instead of raising.
    gpu_stall_meter.register_gpu_stall_meter_routes()


def test_monitor_is_on_demand_and_starts_no_thread():
    source = (REPO_ROOT / "gpu_stall_meter.py").read_text(encoding="utf-8")

    assert 'routes.get("/gpu-stall-meter/sample")' in source
    assert "send_sync" not in source
    assert "while True" not in source
    assert "Thread(" not in source
