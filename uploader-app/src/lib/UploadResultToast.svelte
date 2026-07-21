<script>
  import { fly } from "svelte/transition";
  import { cubicOut } from "svelte/easing";
  import { lastGain } from "./stores.js";

  const HOLD_MS = 9000;

  let gain = $state(null);
  let dismissTimer = null;

  lastGain.subscribe(v => {
    // A brand-new gain restarts the auto-dismiss timer. If the toast
    // was already showing something, we replace its contents.
    gain = v;
    clearTimeout(dismissTimer);
    if (v) {
      dismissTimer = setTimeout(() => {
        lastGain.set(null);
      }, HOLD_MS);
    }
  });

  function dismiss() {
    clearTimeout(dismissTimer);
    lastGain.set(null);
  }

  const DIMS = [
    { k: "speed",       label: "SPE" },
    { k: "stamina",     label: "STA" },
    { k: "gimmick",     label: "GIM" },
    { k: "technical",   label: "TEC" },
    { k: "consistency", label: "CON" },
    { k: "reading",     label: "REA" },
  ];

  function fmtDelta(n) {
    if (typeof n !== "number") return "—";
    if (n === 0) return "±0";
    return n > 0 ? `+${n.toLocaleString()}` : `${n.toLocaleString()}`;
  }
  function deltaClass(n) {
    if (typeof n !== "number") return "";
    if (n > 0) return "up";
    if (n < 0) return "down";
    return "zero";
  }
</script>

{#if gain}
  <div class="toast"
       transition:fly={{ y: 60, duration: 260, easing: cubicOut }}
       role="status"
       aria-live="polite">
    <div class="toast-head">
      <div class="head-left">
        <span class="head-eyebrow mono">Skill gained</span>
        <span class="head-map mono">
          {gain.map_title ?? "map"}
          {#if gain.mods && gain.mods !== "NM"}
            <span class="head-mods">+{gain.mods}</span>
          {/if}
          {#if typeof gain.accuracy === "number"}
            <span class="head-acc">{(gain.accuracy * 100).toFixed(2)}%</span>
          {/if}
        </span>
      </div>
      <button class="close-btn" onclick={dismiss} aria-label="Dismiss">×</button>
    </div>

    <div class="toast-body">
      <div class="total-cell mono">
        <span class={"total-v " + deltaClass(gain.total_delta)}>
          {fmtDelta(gain.total_delta)}
        </span>
        <span class="total-k">total</span>
      </div>
      <div class="dims-grid">
        {#each DIMS as d (d.k)}
          {@const v = gain.dims_delta?.[d.k]}
          <div class="dim">
            <span class="dim-k mono">{d.label}</span>
            <span class={"dim-v mono " + deltaClass(v)}>{fmtDelta(v)}</span>
          </div>
        {/each}
      </div>
    </div>
  </div>
{/if}

<style>
  .toast {
    position: fixed;
    right: 24px;
    bottom: 24px;
    z-index: 100;
    width: 420px;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-top: 2px solid var(--accent);
    border-radius: 8px;
    box-shadow:
      0 10px 30px rgba(0, 0, 0, 0.45),
      0 2px 6px rgba(0, 0, 0, 0.25);
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .toast-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
    min-width: 0;
  }
  .head-left { min-width: 0; flex: 1; }
  .head-eyebrow {
    display: block;
    font-size: 9px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--accent);
    font-weight: 600;
  }
  .head-map {
    display: block;
    margin-top: 3px;
    font-size: 13px;
    color: var(--ink);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .head-mods { color: var(--accent); margin-left: 4px; }
  .head-acc  { color: var(--ink-muted); margin-left: 6px; font-size: 11px; }

  .close-btn {
    background: none;
    border: none;
    color: var(--ink-faint);
    font-size: 20px;
    line-height: 1;
    padding: 0 4px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .close-btn:hover { color: var(--ink); }

  .toast-body {
    display: grid;
    grid-template-columns: 100px 1fr;
    gap: 12px;
    align-items: center;
  }

  .total-cell {
    text-align: right;
    padding: 6px 10px 6px 0;
    border-right: 1px solid var(--rule);
  }
  .total-v {
    display: block;
    font-size: 26px;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    line-height: 1;
    letter-spacing: -0.01em;
  }
  .total-v.up   { color: var(--accent); }
  .total-v.down { color: var(--miss); }
  .total-v.zero { color: var(--ink-muted); }
  .total-k {
    display: block;
    font-size: 9px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-top: 4px;
  }

  .dims-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    grid-auto-rows: 1fr;
    gap: 3px;
  }
  .dim {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 4px;
    padding: 3px 6px;
    border: 1px solid var(--rule);
    border-radius: 3px;
    background: rgba(0, 0, 0, 0.2);
  }
  .dim-k {
    font-size: 9px;
    letter-spacing: 0.14em;
    color: var(--ink-faint);
    text-transform: uppercase;
    font-weight: 600;
  }
  .dim-v {
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    font-weight: 500;
  }
  .dim-v.up   { color: var(--great); }
  .dim-v.down { color: var(--miss); }
  .dim-v.zero { color: var(--ink-muted); }
</style>
