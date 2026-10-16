/**
 * GPU Stall Meter — top-bar readout.
 *
 * Shows GPU power draw, SM clock and free host RAM, plus the verdict from the
 * backend.  Everything is defensive: a missing toolbar, a failed request or an absent
 * GPU must leave the page exactly as it was.  The metrics come from
 * /gpu-stall-meter/sample; the settings are pushed back to the same module.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EXTENSION_NAME = "GPU.StallMeter";
const ROOT_ID = "gpu-stall-meter-root";
const STYLE_ID = "gpu-stall-meter-style";
const SETTING_CATEGORY = ["GPU Stall Meter", "Readout"];

const SETTING_ENABLED = "GPU.StallMeter.Enabled";
const SETTING_INTERVAL = "GPU.StallMeter.IntervalMs";
const SETTING_MIN_UTILIZATION = "GPU.StallMeter.MinUtilization";
const SETTING_MAX_POWER_RATIO = "GPU.StallMeter.MaxPowerRatio";
const SETTING_MAX_CLOCK_RATIO = "GPU.StallMeter.MaxClockRatio";
const SETTING_MAX_FREE_RAM_RATIO = "GPU.StallMeter.MaxFreeRamRatio";

const DEFAULT_INTERVAL_MS = 1000;
const MIN_INTERVAL_MS = 250;
const ATTACH_ATTEMPTS = 40;
const ATTACH_DELAY_MS = 250;

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
let attachTimer = null;
let attachAttempts = 0;

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

function intervalMs() {
    const value = Number(getSettingValue(SETTING_INTERVAL, DEFAULT_INTERVAL_MS));
    return Number.isFinite(value) && value >= MIN_INTERVAL_MS ? value : DEFAULT_INTERVAL_MS;
}

function gib(bytes) {
    const value = Number(bytes);
    return Number.isFinite(value) ? value / 1024 ** 3 : null;
}

function ensureStyle() {
    if (!document?.head || document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = [
        `#${ROOT_ID} { display: flex; align-items: center; gap: 10px; margin-right: 8px;`,
        "  font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap;",
        "  user-select: none; }",
        `#${ROOT_ID} .gsm-field { opacity: 0.7; }`,
        `#${ROOT_ID} .gsm-state { font-weight: 500; }`,
        `#${ROOT_ID}.gsm-stalled .gsm-state { color: var(--p-warning-text, #d9822b); }`,
        `#${ROOT_ID}.gsm-stalled .gsm-field { opacity: 1; }`,
        `#${ROOT_ID}.gsm-idle .gsm-field, #${ROOT_ID}.gsm-idle .gsm-state { opacity: 0.4; }`,
    ].join("\n");
    document.head.appendChild(style);
}

function ensureRoot() {
    if (rootEl?.isConnected) return true;

    const mount = app?.menu?.element;
    if (!mount) return false;

    rootEl = document.createElement("div");
    rootEl.id = ROOT_ID;
    rootEl.title = "GPU Stall Meter";

    fields = {};
    for (const key of ["pwr", "clk", "ram", "state"]) {
        const span = document.createElement("span");
        span.className = key === "state" ? "gsm-state" : "gsm-field";
        span.textContent = key === "state" ? "" : "--";
        rootEl.appendChild(span);
        fields[key] = span;
    }

    mount.appendChild(rootEl);
    return true;
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

    const reason = gpu?.stall?.reason ?? "";
    rootEl.title = `${gpu.name ?? "GPU"} — utilization ${gpu.utilization ?? "--"}%\n${reason}`;
}

async function poll() {
    if (inFlight || !readoutEnabled()) return;
    if (typeof document.visibilityState === "string" && document.visibilityState === "hidden") return;

    inFlight = true;
    try {
        const response = await api.fetchApi("/gpu-stall-meter/sample", { cache: "no-store" });
        if (response?.status === 200) render(await response.json());
    } catch (error) {
        // Keep the previous reading. A monitoring helper must never break the page.
    } finally {
        inFlight = false;
    }
}

function stopPolling() {
    if (pollTimer !== null) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

function startPolling() {
    stopPolling();
    attachAttempts = 0;

    if (!readoutEnabled()) {
        rootEl?.classList.add("gsm-idle");
        return;
    }
    if (!ensureRoot()) {
        scheduleAttach();
        return;
    }

    void poll();
    pollTimer = setInterval(() => void poll(), intervalMs());
}

function scheduleAttach() {
    if (attachTimer !== null) return;
    if (attachAttempts >= ATTACH_ATTEMPTS) return;

    attachAttempts += 1;
    attachTimer = setTimeout(() => {
        attachTimer = null;
        if (ensureRoot()) startPolling();
        else scheduleAttach();
    }, ATTACH_DELAY_MS);
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

app.registerExtension({
    name: EXTENSION_NAME,
    settings: [
        {
            id: SETTING_ENABLED,
            name: "GPU Stall Meter readout",
            category: SETTING_CATEGORY,
            tooltip: "Show GPU power draw, SM clock and free host RAM in the top bar, with a stall verdict. Off removes the readout only; the sampler route stays available.",
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
            tooltip: "How often the readout asks for a snapshot. Each poll is one NVML read; the default of 1000 ms costs well under a tenth of a percent of the GPU.",
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
            tooltip: "Power at or below this fraction of the highest value seen this session counts as evidence of waiting.",
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
            tooltip: "The verdict also requires free host RAM at or below this fraction of total. Without memory pressure, low power at high utilization is not a stall.",
            type: "number",
            defaultValue: 0.1,
            onChange: pushThresholds,
        },
    ],
    async setup() {
        ensureStyle();
        startPolling();
        void pushThresholds();
    },
});
