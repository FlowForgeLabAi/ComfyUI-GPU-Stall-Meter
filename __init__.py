"""ComfyUI GPU Stall Meter.

Registers the sampler route and serves the top-bar readout.  Import failures are
logged rather than raised: a monitoring helper must never stop ComfyUI from starting.

The pack contributes no nodes, but ComfyUI's loader still requires a mappings export
before it keeps a custom node directory, so both mappings are declared empty.
"""

import logging

# Declared empty on purpose: this pack adds one route and one web extension, no nodes.
# Without these names ComfyUI logs "Skip ... due to the lack of NODE_CLASS_MAPPINGS or
# comfy_entrypoint" and lists the pack as an import failure, even though the route and
# the web assets do load.  Verified against ComfyUI 0.35.0 on 2026-10-16.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

try:
    try:
        from .gpu_stall_meter import register_gpu_stall_meter_routes
    except ImportError:
        from gpu_stall_meter import register_gpu_stall_meter_routes
    register_gpu_stall_meter_routes()
except Exception as exc:
    logging.warning("[StallMeter] route unavailable: %s: %s", type(exc).__name__, exc)

WEB_DIRECTORY = "./web/js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
