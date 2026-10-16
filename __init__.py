"""ComfyUI GPU Stall Meter.

Registers the sampler route and serves the top-bar readout.  Import failures are
logged rather than raised: a monitoring helper must never stop ComfyUI from starting.
"""

import logging

try:
    import comfy  # noqa: F401  (present only inside ComfyUI; used as a load-order hint)

    _INSIDE_COMFY = True
except Exception:
    _INSIDE_COMFY = False

try:
    try:
        from .gpu_stall_meter import register_gpu_stall_meter_routes
    except ImportError:
        from gpu_stall_meter import register_gpu_stall_meter_routes
    register_gpu_stall_meter_routes()
except Exception as exc:
    logging.warning("[StallMeter] route unavailable: %s: %s", type(exc).__name__, exc)

WEB_DIRECTORY = "./web/js"

__all__ = ["WEB_DIRECTORY"]
