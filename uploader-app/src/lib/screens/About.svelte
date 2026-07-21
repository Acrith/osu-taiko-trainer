<script>
  import { openUrl } from "@tauri-apps/plugin-opener";

  const VERSION = "0.2.0";
  const SITE = "https://taiko.umaladder.moe";
  const REPO = "https://github.com/Acrith/osu-taiko-trainer";
</script>

<div class="page">
  <div class="eyebrow">About</div>
  <h1 class="title">taiko-trainer uploader</h1>

  <p class="body">
    Companion app for
    <button class="link" onclick={() => openUrl(SITE)}>taiko.umaladder.moe</button>.
    It watches your osu! replays folder and uploads new taiko `.osr` files to
    the server so your skill dimensions stay up to date.
  </p>

  <div class="grid">
    <div class="cell">
      <div class="cell-k">Version</div>
      <div class="cell-v mono">v{VERSION}</div>
    </div>
    <div class="cell">
      <div class="cell-k">Runtime</div>
      <div class="cell-v mono">Tauri 2 · Svelte 5</div>
    </div>
    <div class="cell">
      <div class="cell-k">Site</div>
      <div class="cell-v mono">
        <button class="link" onclick={() => openUrl(SITE)}>taiko.umaladder.moe</button>
      </div>
    </div>
    <div class="cell">
      <div class="cell-k">Source</div>
      <div class="cell-v mono">
        <button class="link" onclick={() => openUrl(REPO)}>github.com/Acrith/osu-taiko-trainer</button>
      </div>
    </div>
  </div>
</div>

<style>
  .page { max-width: 720px; }
  .title {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 24px;
    margin: 4px 0 12px 0;
    color: var(--ink);
  }
  .body { color: var(--ink-muted); font-size: 14px; line-height: 1.6; }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 28px;
  }
  .cell {
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 6px;
    padding: 12px 14px;
  }
  .cell-k {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .cell-v {
    font-size: 13px;
    color: var(--ink);
    margin-top: 2px;
    word-break: break-all;
  }
  .link {
    background: none;
    border: none;
    padding: 0;
    color: var(--accent);
    font-family: inherit;
    font-size: inherit;
    cursor: pointer;
  }
  .link:hover { text-decoration: underline; }
</style>
