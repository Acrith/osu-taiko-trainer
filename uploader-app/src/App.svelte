<script>
  import Sidebar from "./lib/Sidebar.svelte";
  import Home from "./lib/screens/Home.svelte";
  import Uploads from "./lib/screens/Uploads.svelte";
  import Settings from "./lib/screens/Settings.svelte";
  import About from "./lib/screens/About.svelte";
  import { currentScreen } from "./lib/stores.js";

  // Track which screen is active so the switch below can pick a component.
  let screen = $state("home");
  currentScreen.subscribe(v => (screen = v));
</script>

<div class="app">
  <Sidebar />
  <main class="stage">
    {#if screen === "home"}
      <Home />
    {:else if screen === "uploads"}
      <Uploads />
    {:else if screen === "settings"}
      <Settings />
    {:else if screen === "about"}
      <About />
    {/if}
  </main>
</div>

<style>
  .app {
    display: grid;
    grid-template-columns: 200px 1fr;
    height: 100vh;
    overflow: hidden;
  }
  .stage {
    overflow-y: auto;
    padding: 24px 28px;
  }
</style>
