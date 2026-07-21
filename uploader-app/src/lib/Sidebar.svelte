<script>
  import { currentScreen, whoami } from "./stores.js";

  const items = [
    { key: "home",     label: "Home"     },
    { key: "import",   label: "Import"   },
    { key: "uploads",  label: "Uploads"  },
    { key: "settings", label: "Settings" },
    { key: "about",    label: "About"    },
  ];

  let screen = $state("home");
  currentScreen.subscribe(v => (screen = v));

  let identity = $state(null);
  whoami.subscribe(v => (identity = v));
</script>

<aside class="sidebar">
  <div class="brand">
    <img src="/icon-32.png" alt="" class="brand-icon" />
    <div class="brand-text">
      <div class="brand-name">taiko-trainer</div>
      <div class="brand-sub">uploader</div>
    </div>
  </div>

  <nav class="nav">
    {#each items as it (it.key)}
      <button
        class="nav-item"
        class:active={screen === it.key}
        onclick={() => currentScreen.set(it.key)}
      >{it.label}</button>
    {/each}
  </nav>

  <div class="spacer"></div>

  {#if identity}
    <div class="identity">
      <div class="identity-name">{identity.username}</div>
      <div class="identity-sub">signed in · #{identity.user_id}</div>
    </div>
  {:else}
    <div class="identity">
      <div class="identity-name identity-name-anon">not signed in</div>
      <div class="identity-sub">set token in Settings</div>
    </div>
  {/if}

  <div class="version">v0.2.0 · tauri</div>
</aside>

<style>
  .sidebar {
    width: 200px;
    height: 100vh;
    background: var(--panel);
    border-right: 1px solid var(--rule);
    display: flex;
    flex-direction: column;
    padding: 18px 14px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    padding-bottom: 20px;
    margin-bottom: 16px;
    border-bottom: 1px solid var(--rule);
  }
  .brand-icon {
    width: 26px;
    height: 26px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .brand-name {
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 500;
    letter-spacing: -0.01em;
    color: var(--ink);
    line-height: 1.1;
  }
  .brand-sub {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .nav-item {
    background: none;
    border: none;
    text-align: left;
    padding: 8px 10px;
    border-radius: 4px;
    color: var(--ink-muted);
    font-family: var(--font-sans);
    font-size: 13px;
    cursor: pointer;
    transition: background 0.08s ease;
  }
  .nav-item:hover {
    background: color-mix(in oklab, var(--ink) 5%, transparent);
    color: var(--ink);
  }
  .nav-item.active {
    background: color-mix(in oklab, var(--accent) 15%, transparent);
    color: var(--ink);
    font-weight: 500;
  }
  .spacer { flex: 1; }

  .identity {
    padding: 10px 4px;
    border-top: 1px solid var(--rule);
  }
  .identity-name {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--ink);
    font-weight: 500;
  }
  .identity-name-anon { color: var(--ink-muted); }
  .identity-sub {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-top: 2px;
  }
  .version {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--ink-faint);
    padding: 6px 4px 0;
    letter-spacing: 0.06em;
  }
</style>
