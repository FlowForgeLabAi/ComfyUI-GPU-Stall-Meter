/**
 * GPU Stall Meter — a small readout docked inside the graph canvas.
 *
 * ComfyUI's tab bar and toolbar float *over* the graph canvas: `#graph-canvas-container`
 * starts at the top of the app area (measured: top ~= 85 px while the visible toolbar
 * ends around 180 px).  So "just above the canvas" is not empty space — it is where
 * ComfyUI's own controls live.  Docking at the container's top edge therefore lands under
 * the tab bar, which is exactly what two earlier builds did.
 *
 * This version measures the floating bars and sits below the lowest one, with a hard
 * floor: a result closer than MIN_DOCK_TOP to the container's top edge means the bars
 * could not be measured, and the panel falls back to the bottom of the canvas, a place no
 * overlay occupies.  It also reports the measured geometry on every poll (inside the
 * build parameter) so the numbers are knowable without another guessing round.
 *
 * The panel is not draggable (a drag version was laggy), never goes in the top toolbar
 * (that displaced the queue button), and is pointer-events: none so clicks pass through.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EXTENSION_NAME = "GPU.StallMeter";
const BUILD = "2026-10-16k";
const ROOT_ID = "gpu-stall-meter-root";
const STYLE_ID = "gpu-stall-meter-style";
const CANVAS_CONTAINER_ID = "graph-canvas-container";
const BAR_SELECTORS = [".comfyui-body-top", ".p-toolbar", ".workflow-tabs"];
const WINDOW_CLAIM = "__gpuStallMeterClaimed";
const SETTING_CATEGORY = ["GPU Stall Meter", "Readout"];

const SETTING_ENABLED = "GPU.StallMeter.Enabled";
const SETTING_INTERVAL = "GPU.StallMeter.IntervalMs";
const SETTING_MIN_UTILIZATION = "GPU.StallMeter.MinUtilization";
const SETTING_MAX_POWER_RATIO = "GPU.StallMeter.MaxPowerRatio";
const SETTING_MAX_CLOCK_RATIO = "GPU.StallMeter.MaxClockRatio";
const SETTING_MAX_FREE_RAM_RATIO = "GPU.StallMeter.MaxFreeRamRatio";
const SETTING_AUTO_INTERRUPT = "GPU.StallMeter.AutoInterrupt";
const SETTING_STALL_SECONDS = "GPU.StallMeter.StallSeconds";

const DEFAULT_INTERVAL_MS = 1000;
const MIN_INTERVAL_MS = 250;
const DEFAULT_STALL_SECONDS = 60;
const INTERRUPT_COOLDOWN_MS = 60000;
const TOP_INSET = 10;
const MIN_DOCK_TOP = 64;
const BOTTOM_FALLBACK = 12;

const THRESHOLD_SETTINGS = [
    [SETTING_MIN_UTILIZATION, "min_utilization"],
    [SETTING_MAX_POWER_RATIO, "max_power_ratio"],
    [SETTING_MAX_CLOCK_RATIO, "max_clock_ratio"],
    [SETTING_MAX_FREE_RAM_RATIO, "max_free_ram_ratio"],
];

let rootEl = null;
let fields = null;
let pollTimer = null;
let inFlight = false;
let dockedTop = null;
let stalledSince = null;
let stallNotified = false;
let interruptCooldownUntil = 0;

function claimWidget() {
    try {
        if (window[WINDOW_CLAIM]) return false;
        window[WINDOW_CLAIM] = BUILD;
        return true;
    } catch (error) {
        return true;
    }
}

function getSettingValue(id, fallback) {
    try {
        const value = app?.ui?.settings?.getSettingValue?.(id);
        return value === undefined || value === null ? fallback : value;
    } catch (error) {
        return fallback;
    }
}

function readoutEnabled() {
    return getSettingValue(SETTING_ENABLED, true) !== false;
}

function autoInterruptEnabled() {
    return getSettingValue(SETTING_AUTO_INTERRUPT, false) === true;
}

function intervalMs() {
    const value = Number(getSettingValue(SETTING_INTERVAL, DEFAULT_INTERVAL_MS));
    return Number.isFinite(value) && value >= MIN_INTERVAL_MS ? value : DEFAULT_INTERVAL_MS;
}

function stallSeconds() {
    const value = Number(getSettingValue(SETTING_STALL_SECONDS, DEFAULT_STALL_SECONDS));
    return Number.isFinite(value) && value >= 1 ? value : DEFAULT_STALL_SECONDS;
}

function gib(bytes) {
    const value = Number(bytes);
    return Number.isFinite(value) ? value / 1024 ** 3 : null;
}

function showToast(severity, detail) {
    try {
        app?.extensionManager?.toast?.add?.({
            severity,
            summary: "GPU Stall Meter",
            detail,
            life: 8000,
        });
    } catch (error) {
        // Frontends without toasts still get the panel and the interrupt.
    }
}

function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = [
        `#${ROOT_ID} { position: absolute; left: 50%; bottom: ${BOTTOM_FALLBACK}px;`,
        "  transform: translateX(-50%); z-index: 1000;",
        "  display: grid; grid-template-columns: auto auto; gap: 0 10px;",
        "  padding: 6px 10px; border: 1px solid var(--border-color, #444);",
        "  border-radius: 6px; background: var(--comfy-menu-bg, rgba(24,24,28,0.86));",
        "  color: var(--input-text, #ddd); font-size: 11px; line-height: 1.35;",
        "  font-variant-numeric: tabular-nums; user-select: none; pointer-events: none; }",
        `#${ROOT_ID} .gsm-field { opacity: 0.75; }`,
        `#${ROOT_ID} .gsm-state { font-weight: 500; }`,
        `#${ROOT_ID}.gsm-stalled { border-color: var(--p-warning-text, #d9822b); }`,
        `#${ROOT_ID}.gsm-stalled .gsm-state { color: var(--p-warning-text, #d9822b); }`,
        `#${ROOT_ID}.gsm-idle .gsm-field, #${ROOT_ID}.gsm-idle .gsm-state { opacity: 0.45; }`,
        `@media (max-width: 1280px) { #${ROOT_ID} { font-size: 10px; padding: 4px 8px; gap: 0 8px; } }`,
        `@media (max-width: 900px) { #${ROOT_ID} .gsm-field[data-field="clk"],`,
        `  #${ROOT_ID} .gsm-field[data-field="ram"] { display: none; } }`,
    ].join("\n");
    document.head.appendChild(style);
}

function rectOf(selector) {
    return document.querySelector(selector)?.getBoundingClientRect?.() ?? null;
}

function lowestBarBottom() {
    let lowest = 0;
    for (const selector of BAR_SELECTORS) {
        const rect = rectOf(selector);
        if (rect && rect.height > 0 && rect.bottom > lowest) lowest = rect.bottom;
    }
    return lowest;
}

function dockTop(container) {
    const lowest = lowestBarBottom();
    if (!lowest || lowest <= container.top) return null;
    const relative = lowest - container.top + TOP_INSET;
    // A bar bottom this close to the container's top edge means the floating bars were
    // not measurable, so the top of the canvas is not actually clear.
    return relative >= MIN_DOCK_TOP && lowest < container.bottom ? relative : null;
}

function placePanel() {
    if (!rootEl) return;
    const container = document.getElementById(CANVAS_CONTAINER_ID)?.getBoundingClientRect?.();
    const relative = container ? dockTop(container) : null;
    if (relative === dockedTop) return;

    dockedTop = relative;
    if (relative === null) {
        rootEl.style.top = "auto";
        rootEl.style.bottom = `${BOTTOM_FALLBACK}px`;
    } else {
        rootEl.style.bottom = "auto";
        rootEl.style.top = `${relative}px`;
    }
}

function geometryReport() {
    const parts = BAR_SELECTORS.map((selector) => {
        const rect = rectOf(selector);
        return rect ? `${Math.round(rect.top)},${Math.round(rect.bottom)}` : "-";
    });
    const container = document.getElementById(CANVAS_CONTAINER_ID)?.getBoundingClientRect?.();
    parts.push(container ? `${Math.round(container.top)},${Math.round(container.bottom)}` : "-");
    return parts.join(";");
}

function bindExisting(found) {
    const next = {};
    for (const span of found.querySelectorAll("[data-field]")) next[span.dataset.field] = span;
    if (!next.pwr || !next.clk || !next.ram || !next.state) return false;
    rootEl = found;
    fields = next;
    return true;
}

function buildPanel(host) {
    rootEl = document.createElement("div");
    rootEl.id = ROOT_ID;

    fields = {};
    for (const key of ["pwr", "clk", "ram", "state"]) {
        const span = document.createElement("span");
        span.className = key === "state" ? "gsm-state" : "gsm-field";
        span.dataset.field = key;
        span.textContent = "--";
        rootEl.appendChild(span);
        fields[key] = span;
    }

    host.appendChild(rootEl);
    placePanel();
    return true;
}

function ensureRoot() {
    const host = document.getElementById(CANVAS_CONTAINER_ID) ?? document.body;
    if (!host) return false;

    const existing = document.getElementById(ROOT_ID);
    if (existing) {
        if (existing.parentElement !== host) {
            host.appendChild(existing);
            dockedTop = null;
        }
        return bindExisting(existing);
    }
    return buildPanel(host);
}

function render(snapshot) {
    if (!ensureRoot()) return;

    const host = snapshot?.host ?? {};
    const gpus = Array.isArray(snapshot?.gpus) ? snapshot.gpus : [];
    const gpu = gpus[0] ?? null;
    const freeRam = gib(host.ram_available);

    fields.ram.textContent = freeRam === null ? "RAM --" : `RAM ${freeRam.toFixed(1)}G`;

    if (!gpu) {
        fields.pwr.textContent = "PWR --";
        fields.clk.textContent = "CLK --";
        fields.state.textContent = "no NVIDIA GPU";
        rootEl.classList.remove("gsm-stalled");
        rootEl.classList.add("gsm-idle");
        return;
    }

    const power = Number(gpu.power_w);
    const clock = Number(gpu.sm_clock_mhz);
    fields.pwr.textContent = Number.isFinite(power) ? `PWR ${power.toFixed(1)}W` : "PWR --";
    fields.clk.textContent = Number.isFinite(clock) ? `CLK ${Math.round(clock)}MHz` : "CLK --";

    const state = gpu?.stall?.state ?? "ok";
    fields.state.textContent = snapshot?.calibrating ? "calibrating" : state;
    rootEl.classList.toggle("gsm-stalled", state === "stalled");
    rootEl.classList.toggle("gsm-idle", state === "idle" && !snapshot?.calibrating);
}

async function queueRunning() {
    try {
        const response = await api.fetchApi("/queue", { cache: "no-store" });
        if (response?.status !== 200) return false;
        const queue = await response.json();
        return Array.isArray(queue?.queue_running) && queue.queue_running.length > 0;
    } catch (error) {
        return false;
    }
}

async function interruptRun(reason) {
    interruptCooldownUntil = Date.now() + INTERRUPT_COOLDOWN_MS;
    try {
        const response = await api.fetchApi("/interrupt", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
        });
        if (response?.status === 200) {
            showToast("error", `Run interrupted after ${stallSeconds()}s of stalling. ${reason}`);
        } else {
            showToast("warning", `Interrupt request failed (HTTP ${response?.status}). ${reason}`);
        }
    } catch (error) {
        showToast("warning", `Interrupt request failed. ${reason}`);
    }
}

async function handleStall(snapshot) {
    const gpu = (snapshot?.gpus ?? [])[0];
    if (gpu?.stall?.state !== "stalled") {
        stalledSince = null;
        stallNotified = false;
        return;
    }

    const now = Date.now();
    const reason = gpu.stall.reason || "the GPU is waiting on the pagefile";

    if (stalledSince === null) stalledSince = now;
    if (!stallNotified) {
        stallNotified = true;
        showToast("warning", reason);
    }

    if (!autoInterruptEnabled()) return;
    if (now - stalledSince < stallSeconds() * 1000) return;
    if (now < interruptCooldownUntil) return;
    if (!(await queueRunning())) return;
    await interruptRun(reason);
}

async function poll() {
    if (inFlight || !readoutEnabled()) return;
    // A hidden tab stops polling to stay free — unless the interrupt is armed, because
    // that only has value while the user is looking at something else.
    if (document.visibilityState === "hidden" && !autoInterruptEnabled()) return;

    inFlight = true;
    try {
        placePanel();
        const tag = `${BUILD}|${geometryReport()}`;
        const response = await api.fetchApi(
            `/gpu-stall-meter/sample?build=${encodeURIComponent(tag)}`,
            { cache: "no-store" },
        );
        if (response?.status === 200) {
            const snapshot = await response.json();
            render(snapshot);
            void handleStall(snapshot);
        }
    } catch (error) {
        // Keep the previous reading; a monitoring helper must never break the page.
    } finally {
        inFlight = false;
    }
}

function stopPolling() {
    if (pollTimer === null) return;
    clearInterval(pollTimer);
    pollTimer = null;
}

function startPolling() {
    stopPolling();
    if (!readoutEnabled()) {
        rootEl?.classList.add("gsm-idle");
        return;
    }
    if (!ensureRoot()) return;
    void poll();
    pollTimer = setInterval(() => void poll(), intervalMs());
}

async function pushThresholds() {
    const patch = {};
    for (const [settingId, key] of THRESHOLD_SETTINGS) {
        const value = Number(getSettingValue(settingId, undefined));
        if (Number.isFinite(value)) patch[key] = value;
    }
    if (!Object.keys(patch).length) return;
    try {
        await api.fetchApi("/gpu-stall-meter/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(patch),
        });
    } catch (error) {
        // The next snapshot reports whatever the backend kept.
    }
    void poll();
}

if (claimWidget()) {
    app.registerExtension({
        name: EXTENSION_NAME,
        settings: [
            {
                id: SETTING_ENABLED,
                name: "GPU Stall Meter readout",
                category: SETTING_CATEGORY,
                tooltip: "Show the GPU power / SM clock / free RAM panel inside the graph canvas. Off hides the panel; the sampler route stays available.",
                type: "boolean",
                defaultValue: true,
                onChange() {
                    startPolling();
                },
            },
            {
                id: SETTING_INTERVAL,
                name: "GPU Stall Meter sample interval (ms)",
                category: SETTING_CATEGORY,
                tooltip: "How often the panel asks for a snapshot. Each poll is one NVML read; 1000 ms costs well under a tenth of a percent of the GPU.",
                type: "number",
                defaultValue: DEFAULT_INTERVAL_MS,
                onChange() {
                    startPolling();
                },
            },
            {
                id: SETTING_MIN_UTILIZATION,
                name: "Stall verdict: minimum utilization (%)",
                category: SETTING_CATEGORY,
                tooltip: "Below this the GPU is reported as idle instead of being judged.",
                type: "number",
                defaultValue: 80,
                onChange: pushThresholds,
            },
            {
                id: SETTING_MAX_POWER_RATIO,
                name: "Stall verdict: maximum power ratio",
                category: SETTING_CATEGORY,
                tooltip: "Power at or below this fraction of the highest value seen this session counts as evidence of waiting. 1.0 makes every reading count as evidence, which is only useful for testing the interrupt.",
                type: "number",
                defaultValue: 0.5,
                onChange: pushThresholds,
            },
            {
                id: SETTING_MAX_CLOCK_RATIO,
                name: "Stall verdict: maximum SM clock ratio",
                category: SETTING_CATEGORY,
                tooltip: "SM clock at or below this fraction of the highest value seen this session counts as evidence of waiting.",
                type: "number",
                defaultValue: 0.6,
                onChange: pushThresholds,
            },
            {
                id: SETTING_MAX_FREE_RAM_RATIO,
                name: "Stall verdict: maximum free host RAM ratio",
                category: SETTING_CATEGORY,
                tooltip: "The verdict also requires free host RAM at or below this fraction of total. Without memory pressure, low power at high utilization is not a stall. 1.0 makes every reading count, which is only useful for testing the interrupt.",
                type: "number",
                defaultValue: 0.1,
                onChange: pushThresholds,
            },
            {
                id: SETTING_AUTO_INTERRUPT,
                name: "Interrupt the run when the GPU is stalled",
                category: SETTING_CATEGORY,
                tooltip: "Off by default, and deliberately so: a stall verdict can be wrong, and interrupting costs the work in progress. When on, the run is interrupted only after the verdict has held continuously for the configured number of seconds, only while a prompt is actually running, and at most once a minute. It also keeps polling while the tab is in the background.",
                type: "boolean",
                defaultValue: false,
            },
            {
                id: SETTING_STALL_SECONDS,
                name: "Interrupt after this many seconds of stalling",
                category: SETTING_CATEGORY,
                tooltip: "How long the stalled verdict must hold before the run is interrupted. Ignored while the interrupt setting is off.",
                type: "number",
                defaultValue: DEFAULT_STALL_SECONDS,
            },
        ],
        async setup() {
            try {
                ensureStyle();
                ensureRoot();
                startPolling();
                void pushThresholds();
            } catch (error) {
                console.warn("[StallMeter] setup failed:", error);
            }
        },
    });
} else {
    console.debug("[StallMeter] panel already claimed by another evaluation of this file; standing down.");
}
