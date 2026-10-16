# ComfyUI GPU Stall Meter

GPU utilization tells you the SM is busy. It does not tell you what the SM is busy *with*.

When a workflow stalls on host-memory paging, utilization stays pinned at 99-100% while the
GPU waits for pages to arrive. Every meter in the toolbar looks healthy, the progress bar does
not move, and the usual advice ("your GPU is at 100%, give it more work") is exactly backwards.
The two states are only distinguishable from signals other than utilization.

This pack docks a small readout inside the graph canvas with those signals: **GPU power
draw**, **SM clock**, **host RAM pressure**, and a **stall verdict** derived from them.

## What it shows

| readout | source | what it is for |
|---|---|---|
| `PWR` | NVML `nvmlDeviceGetPowerUsage` | watts. Collapses when the GPU is waiting rather than working |
| `CLK` | NVML `nvmlDeviceGetClockInfo(SM)` | MHz. Falls to a few hundred when the SM is starved |
| `RAM` | psutil | free host memory. A model larger than RAM is the usual cause |
| state | derived | `idle` / `ok` / `stalled` / `calibrating` |

The panel sits below ComfyUI's floating toolbar and above the graph. It is `pointer-events:
none`, so every click passes through to the nodes underneath — which also means it cannot be
hovered; the verdict reason, naming the actual wattage, clock and free RAM, arrives as a
notification when a stall begins.

## Why power and clock, and not utilization

Measured on one machine (RTX 5060 Laptop, 8 GB VRAM, 16 GB RAM) running MiniMax-H3, 534
samples taken alongside a real generation loop:

| signal | healthy | stalled on paging |
|---|---|---|
| `utilization.gpu` | 99-100% | **99-100% — no change** |
| board power | 64-98 W | **flat 34-36 W** |
| SM clock | ~2760 MHz | **as low as 180 MHz** |
| free host RAM | — | **median 1.92 GB** of 15.26 GiB |
| pagefile free | — | **median 6.89 GB** |

Median board power across the whole run was **32.8 W**, i.e. on that machine the stalled state
was the common state, not an edge case. The working set was ~34.5 GiB (19.5 GiB model +
14.96 GiB nvfp4 text encoder) against 15.26 GiB of usable RAM.

Raw samples: <https://huggingface.co/datasets/FlowForgeLabAi/minimax-h3-8gb-bench>
(`h3_monitor_log.csv`, 534 rows x 13 columns at 10 s).

## The stall verdict

The thresholds are relative to the card and the session, because absolute watts are not
comparable between a laptop 5060 and a desktop 4090:

1. While running, the meter tracks the **highest power** and **highest SM clock** it has seen
   for each GPU, plus free host RAM.
2. After a **warm-up of 30 samples** the verdict is enabled.
3. A sample is reported as `stalled` when all of these hold:
   - utilization >= 80%, **and**
   - power <= 50% of the session's observed maximum, **and**
   - free host RAM <= 10% of total, **and**
   - SM clock <= 60% of the session's observed maximum (when the clock is readable).
4. Otherwise `ok` while utilization is high, and `idle` when it is not.

The RAM condition is what keeps the verdict honest. A small workflow can legitimately sit at
99% utilization and low power without anything being wrong; what makes the state pathological
is that the GPU is waiting on the pagefile. All four thresholds are settings, not constants.

## Interrupting a stalled run (off by default)

A stalled run can sit there for hours producing nothing — on the machine above, the same
workflow took 34-44 minutes while healthy and about twelve hours while thrashing. The pack can
interrupt it, and is deliberately conservative about doing so:

- **Off by default.** A stall verdict can be wrong, and interrupting costs the work in progress.
- The verdict must hold **continuously** for a configurable number of seconds (default 60).
- Only while a prompt is **actually running**, and at most **once a minute**.
- The interrupt is announced with a notification naming the power, clock and free RAM that
  triggered it.

Enabling it also keeps the panel polling while the browser tab is in the background; otherwise
polling pauses on hidden tabs, because watching costs a little and nothing is gained by it.

## Install

Manual: clone or unzip into `ComfyUI/custom_nodes/` and restart ComfyUI. The only required
dependency is `psutil`, which most ComfyUI environments already have. NVML is read through
`pynvml` when it is installed, and through `ctypes` against the driver's own `nvml.dll` /
`libnvidia-ml.so.1` otherwise, so the pack installs nothing else.

Once the first registry release is published, ComfyUI Manager will also find it by searching
for `GPU Stall Meter`.

## Settings

Under `Settings > GPU Stall Meter`:

| setting | default | notes |
|---|---|---|
| readout | on | hides the panel; the sampler route stays available |
| sample interval | 1000 ms | one NVML read per poll |
| minimum utilization | 80% | below this the GPU is reported as idle, not judged |
| maximum power ratio | 0.5 | of the session's observed maximum |
| maximum clock ratio | 0.6 | of the session's observed maximum |
| maximum free RAM ratio | 0.10 | of total host RAM |
| interrupt the run when stalled | off | see above |
| interrupt after N seconds | 60 | how long the verdict must hold first |

## Limits — read this before filing an issue

- It **detects** the stall; it does not fix it. The fix is a smaller working set: shorter
  clips, lower resolution, a smaller text encoder, or more RAM.
- Absolute wattages are per-card. The verdict is self-calibrating, but the first 30 samples of
  a session are always `ok`/`idle` while it learns the card.
- `nvmlDeviceGetPowerUsage` is not available on every part (some vGPU and datacenter
  configurations raise from it). The `PWR` reading then reports unavailable, and the verdict
  falls back to RAM pressure plus clock.
- On AMD and Apple GPUs there is no NVML. The panel reports that no NVIDIA GPU was found, and
  nothing else happens.
- The numbers above come from **one** machine. They are a starting point for the thresholds,
  not a law.

## Tests

```
python -m pytest tests -q
node --check web/js/gpu_stall_meter.js
```

The Python tests inject fake NVML/psutil modules, so they run without a GPU, without ComfyUI
and without `pynvml` installed.

## License

MIT — see `LICENSE`.
