/*
 * Auto-update check on launch.
 *
 * Runs the check ~3s after mount so the UI is visibly settled before we
 * pop a modal. Silent on any failure — updates are best-effort and users
 * on a spotty network shouldn't see "update check failed" spam.
 *
 * Manifest source: `latest.json` published on each GitHub Release
 * (endpoint configured in src-tauri/tauri.conf.json → plugins.updater).
 * The client verifies its minisign signature against the pubkey baked
 * into the same config before accepting an update, so nobody can serve
 * a malicious binary even if they compromised the GitHub Releases URL.
 */
import { check } from "@tauri-apps/plugin-updater";
import { ask } from "@tauri-apps/plugin-dialog";
import { relaunch } from "@tauri-apps/plugin-process";

export async function scheduleUpdateCheck() {
  setTimeout(async () => {
    try {
      const update = await check();
      if (!update?.available) return;

      const notes = update.body ? `\n\n${update.body}` : "";
      const yes = await ask(
        `Version ${update.version} is out. Install now?${notes}`,
        {
          title: "taiko-trainer uploader — update available",
          kind: "info",
          okLabel: "Install & restart",
          cancelLabel: "Later",
        }
      );
      if (!yes) return;

      // On Windows the plugin closes the app, runs the NSIS installer,
      // and relaunches — the plugin call itself resolves before the
      // installer takes over. We call relaunch() explicitly on the
      // path where the installer doesn't auto-launch (edge case for
      // some install modes).
      await update.downloadAndInstall();
      await relaunch();
    } catch (e) {
      // Swallow. Reasons: no network, running from a dev build, GitHub
      // rate-limited, manifest not yet published for this version, etc.
      // None of those are worth interrupting the user for.
      console.warn("[updater] check failed:", e);
    }
  }, 3000);
}
