# Pi + Orca on Unraid

Official upstream Pi, standard Orca integration, and a small Debian-stable/Node-LTS image for `linux/amd64`.

- Image: `ghcr.io/elmakus/pi-orca:latest`; immutable deployment reference: `ghcr.io/elmakus/pi-orca@sha256:…`.
- Runtime: non-root `agent` (`1000:1000`), `dumb-init` → official `orca-ide serve` → Pi on demand.
- Pi runs in its normal full-access/YOLO mode. This is not host root access. There is no extra permission gate.
- Orca's normal managed hooks/extensions remain enabled. No OMP, extra Bun CLI, LSP bundle, skills or agent framework is installed.
- Application updates happen in the image. User-installed packages in HOME have their own update lifecycle.

## Persistent state and isolation

Exactly two main read/write bind mounts:

| Unraid | Container |
| --- | --- |
| `/mnt/user/appdata/pi-orca` | `/home/agent` |
| `/mnt/cachedl/omp-workspace` | `/workspace` |

The whole HOME persists: Pi sessions/settings/provider configuration, Orca profile, device grants and keys, `.config`, `.local`, `.ssh`, and user tools. Orca normally uses `~/.config/orca`, but additional keys can live under `~/.orca`; backing up only the profile is insufficient. Orca can select an account-specific `PI_CODING_AGENT_DIR`.

The shared workspace is intentional. Use the agents alternately. Project `AGENTS.md` and `.pi` configuration remain visible. Do not recursively chown the shared workspace or copy OMP's HOME, credentials, sessions, extensions or skills into this HOME.

The container drops all capabilities and uses `no-new-privileges`. It has no Docker socket, privileged flag, host-wide mounts or desktop-control grant. Full access applies to the container's available files and network. This project does not configure Pi to administer Unraid, SSH hosts or Home Assistant.

## Install on Unraid

1. Check that `ibraproxy` exists, `192.168.2.104:6769` is unused, and UID/GID1000 can write the workspace.
2. Create **only the new, absent** `/mnt/user/appdata/pi-orca` with owner `1000:1000`, mode `0700`. If the directory exists, inspect it first; never replace populated appdata.
3. Keep the LB secret in the existing root-readable host env-file, `/mnt/user/appdata/codex-lb-clean/client-secrets/pi-unraid.env`. Its required variable is `CODEX_LB_API_KEY`. Never commit its contents or put it in Docker build arguments, CI secrets, template variables or image layers.
4. Copy `templates/my-pi-orca.xml` to `/boot/config/plugins/dockerMan/templates-user/my-pi-orca.xml`, then select this user template in Unraid's Add Container screen.
5. Check name/hostname `pi-orca`, network `ibraproxy`, the two mounts, `unless-stopped`, and a 30-second stop timeout. The only published port is `192.168.2.104:6769:6769/tcp`, not `0.0.0.0`.
   Orca 1.4.248 has a worst-case 35-second internal shutdown grace. The 30-second container deadline can truncate a stalled shutdown; smoke verifies ordinary shutdown with a live child finishes without SIGKILL. Saved-session persistence does not preserve a running process.
6. Start the container, then check Docker health and `docker exec pi-orca timeout 10 orca-ide status`. Do not assume a listening TCP port alone proves readiness.
7. Turn on Unraid's separate **Autostart** toggle for `pi-orca` if it must start after an array/server restart. Docker's `unless-stopped` policy alone does not replace DockerMan's autostart list.

The template starts with `--no-pairing`. Pair each real device using the foreground procedure below. Keep access on LAN or UniFi Teleport VPN. Do not add an Internet port forward, Cloudflare Tunnel or mandatory Tailscale.

Equivalent initial command, after the preparation above:

```sh
docker run -d --name pi-orca --hostname pi-orca \
  --label net.unraid.docker.managed=dockerman \
  --network ibraproxy --restart unless-stopped --stop-timeout 30 \
  --cap-drop ALL --security-opt no-new-privileges \
  --env-file /mnt/user/appdata/codex-lb-clean/client-secrets/pi-unraid.env \
  -e ORCA_PAIRING_ADDRESS=192.168.2.104 \
  -p 192.168.2.104:6769:6769/tcp \
  -v /mnt/user/appdata/pi-orca:/home/agent \
  -v /mnt/cachedl/omp-workspace:/workspace \
  ghcr.io/elmakus/pi-orca:latest --no-pairing
```

DockerMan adds its management label when it creates a container from the template. The equivalent CLI command must supply that label explicitly; otherwise the installed Unraid UI treats it as **3rd Party**, without its normal update controls or Autostart toggle.

## Pair Linux and Android privately

A pairing URL/QR is a credential. Display it only in your private administrative terminal. Do not paste it into issues, Actions logs/artifacts or chat reports.

Generate Linux and Android offers **sequentially in the same persistent HOME**, never with two Orca servers sharing a profile. Complete each client's pairing before generating the next offer; do not assume two unused offers remain valid together.

1. Record the immutable registry reference with `docker image inspect "$(docker inspect pi-orca --format '{{.Image}}')" --format '{{range .RepoDigests}}{{println .}}{{end}}'`. Then stop only the new service: `docker stop -t 30 pi-orca`.
2. Start a foreground pairing instance. Use the recorded image reference instead of a moving `latest`:

   ```sh
   docker run --rm -it --log-driver=none --name pi-orca-pair --hostname pi-orca \
     --network ibraproxy --stop-timeout 30 \
     --cap-drop ALL --security-opt no-new-privileges \
     --env-file /mnt/user/appdata/codex-lb-clean/client-secrets/pi-unraid.env \
     -e ORCA_PAIRING_ADDRESS=192.168.2.104 \
     -p 192.168.2.104:6769:6769/tcp \
     -v /mnt/user/appdata/pi-orca:/home/agent \
     -v /mnt/cachedl/omp-workspace:/workspace \
     ghcr.io/elmakus/pi-orca@sha256:REPLACE_WITH_RECORDED_DIGEST
   ```

3. Pair the actual Orca Linux client using its normal remote-server flow. Check workspace access and opening a Pi session. End the foreground server with Ctrl+C.
4. Repeat the same foreground command with `--mobile-pairing` after the image reference. Pair the actual Android app using its QR/link. Check a terminal and a Pi session, then end this server.
5. Run `docker start pi-orca`. Its `--no-pairing` mode keeps saved grants. Check both clients reconnect, first on LAN, then through UniFi Teleport.

There is no assumed live-server CLI command to generate a new offer. If another device needs pairing, use this same stopped-service procedure. Never delete profile locks automatically.

## Codex LB: native Pi configuration

Create `/home/agent/.pi/agent/models.json` in the container (host path `/mnt/user/appdata/pi-orca/.pi/agent/models.json`):

```json
{
  "providers": {
    "codex_lb": {
      "baseUrl": "http://codex-lb-clean:2455/v1",
      "api": "openai-responses",
      "apiKey": "${CODEX_LB_API_KEY}",
      "models": [
        { "id": "gpt-6.1-sol", "reasoning": true }
      ]
    }
  }
}
```

`${CODEX_LB_API_KEY}` is environment interpolation. A bare `CODEX_LB_API_KEY` string is a literal key. For the HTTP fallback use `openai-responses`; the patched native WebSocket provider uses `openai-codex-responses` on `/backend-api`. If Orca selects another agent directory, configure that directory's native Pi file too, without copying credentials into it.

Start with only `codex_lb/gpt-6.1-sol` and medium reasoning. Verify actual streaming generation, a tool call and tool result followed by another answer, and history resume. `/v1/models` alone does not verify compatibility. `reasoning: true` is a client declaration, not proof of provider capabilities. Do not invent prices/context limits or set compatibility workarounds without a demonstrated incompatibility. Generative tests belong on Unraid, never in CI. Add other LB models only after this model passes.

## User packages and tools

Core Pi, Orca and GitHub CLI are root-owned outside HOME. The image puts core paths before HOME tools to avoid accidentally shadowing those commands. npm's prefix is `~/.local/npm`; user binaries remain on PATH. Use Python's `python3 -m venv …`, then that environment's pip.

Use native `pi install`, `pi list`, and `pi update --extensions` for optional Pi packages. Do not update core Pi from the entrypoint. User packages, venvs and tools persist in HOME, but native modules or venvs can need repair after a Node/Python/Debian ABI change. Persistence is not a promise of unlimited binary compatibility.

## Native Codex WebSocket and automatic patch updates

The built-in `codex_lb` provider may use `openai-codex-responses` with
`baseUrl: "http://codex-lb-clean:2455/backend-api"`, because the Pi native
Codex provider appends `/codex/responses`. A `codex_lb_http` provider retains
`openai-responses` on `http://codex-lb-clean:2455/v1` for rollback.
Both use the same `CODEX_LB_API_KEY` interpolation, never an embedded secret.
Set `transport: "websocket"` to prefer the native WebSocket lane.

On **every scheduled or manual CI run**, the resolver reads the current
`lkraider/pi-codex-lb-patches` default-branch commit. That SHA participates in
the image fingerprint; any upstream patch change triggers a new build, even
if Pi/Orca releases did not change. The *resolved* SHA is checked out for the
individual build to avoid a moving-target race, but is not pinned between runs.
The build applies the three upstream patches (`bearer-auth`, `transient-retry`,
`owner-migration`), rebases only the known Pi 1.5 identity-header difference,
rebuilds Pi with esbuild, and runs the upstream provider and bundled-code tests.
A newly incompatible upstream patch or Pi source **fails the build** and never
promotes `latest`. Do not silently skip a patch or publish an untested image.

This is upstream code automatically downloaded and executed during the build;
it entails supply-chain risk. Pin a revision instead if source integrity or
review before deployment becomes a higher priority.

Full HOME, including sessions and saved Orca pairing, stays in the existing
persistent bind mount; rebuilds never replace it. Images only update after
Unraid's explicit container update. The original official Pi binary remains
available at `/opt/pi/pi`, while the default `pi` points to the tested npm
bundle at `/opt/pi-patched/dist/bundle/cli.js`.

## Build, test and publication

`.github/workflows/publish.yml` runs on image-affecting pushes to `main`, manual dispatch, and `17 */6 * * *` UTC. It never deploys to Unraid.

The resolver uses official sources and resolves once:

- latest stable Pi and its complete standalone payload, with official `SHA256SUMS`;
- latest stable Orca `.deb`, with the official Releases API SHA-256 asset digest;
- latest stable GitHub CLI and its official checksum list;
- latest Node LTS on current Debian stable, using an official matching Node image variant pinned by digest.

Release selection excludes drafts, prereleases and unrelated product tags, compares numeric SemVer, and paginates. Missing assets/checksums, integrity mismatches, registry/auth failures or an unavailable official Node/current-Debian variant fail the run without replacing `latest`. Checksums from the same publisher are integrity checks, not independent signature verification.

The fingerprint includes source/build files, platform, every managed version/checksum, base digest and UTC date. Every day invalidates the OS stage and runs `apt-get update` plus upgrade of installed packages. Manual dispatch always rebuilds; the Docker build pulls the pinned base. No runtime APT upgrades or application-latest downloads happen in the entrypoint. Normal Orca setup of its own required runtime remains enabled.

CI builds once, loads and exercises that image with a fresh HOME/workspace and production hardening, then pushes a unique version tag. It verifies the published config digest against the tested local image ID before promoting `latest`. Publications serialize, and an older run/re-run cannot overwrite a newer run's `latest`. `fail_before_promotion` deliberately proves failure isolation; it leaves the tested version tag available but fails before changing `latest`.

Versioned tags are retained and never reused. Save their registry digest; the digest is the immutable reference. The image build manifest, `/usr/local/share/pi-orca/build.json`, records application/base versions, checksums, base digest and build identity. OCI labels record source/revision, Pi/Orca version, fingerprint, run number and creation time. Each Actions run uploads a small audit artifact containing the manifest, package versions and published digest, not HOME or Orca logs.

Actions are pinned to full commit SHAs; Dependabot tracks their updates. Application/base versions are resolved automatically, not permanently frozen. CI uses repository `GITHUB_TOKEN`, with `packages: write` only in the publication job. It has no model key or production HOME.

### GHCR visibility

Do not infer package visibility from repository visibility. Check the package metadata reports `public`. If it is private, use [package settings](https://github.com/users/elmakus/packages/container/pi-orca/settings) to change visibility to **Public** and confirm `pi-orca`; public visibility is irreversible. This deployment's package was observed public. Verify real anonymous pulls by tag and digest using a fresh empty Docker credential configuration. An authenticated pull is not proof of public availability. The image contains no runtime secrets or user HOME.

### External schedule monitoring

GitHub can disable a public repository's schedule after 60 days without activity. Runs may also be delayed or dropped. Publishing GHCR images is not a documented reset of that inactivity timer. Do not create fake keepalive commits or a second GitHub cron to monitor this cron.

Connect two external Healthchecks.io or equivalent checks:

1. Poll check: six-hour period plus a suitable delay allowance (for example, two hours). Store its private ping URL in repository Actions secret `HEALTHCHECKS_POLL_URL`.
2. Daily OS-refresh check: 24-hour period plus a suitable delay allowance (for example, eight hours). Store its separate private ping URL in `HEALTHCHECKS_DAILY_URL`.
3. Connect a real notification channel. Force a failure/test alert and confirm delivery. Never put ping URLs in source or reports.

Successful full polls, including no-ops, ping the first check. Only a successfully refreshed/promoted image pings the daily check. Failed runs send `/fail`, never unconditional success. Missing secrets produce explicit warnings: heartbeat wiring alone is **not active monitoring**.

After an alert, inspect the workflow state, enable it if `disabled_inactivity`, and manually dispatch it. Example: `gh workflow enable publish.yml --repo elmakus/pi-orca-unraid`, then `gh workflow run publish.yml --repo elmakus/pi-orca-unraid`. Automatic repair of GitHub is not part of this project.

## Manual update, backup and rollback

Image publication is automatic. Updating the running service remains **manual**: Unraid **Docker → Check for Updates → Update**. A restart interrupts live tasks; saved data/session history persists.

Before an update:

1. Record `docker inspect pi-orca --format '{{.Config.Image}} {{.Image}}'` and the registry digest/version tag. Do not use a moving tag as your only rollback reference.
2. Stop only `pi-orca` with its 30-second timeout. Do not stop `pinkcollab-omp`.
3. Make a consistent full-HOME backup while Orca is stopped. Store it outside appdata with access restricted to its administrator. For example, as host root: `umask 077; tar --acls --xattrs --numeric-owner -C /mnt/user/appdata -cpf /PATH/OUTSIDE/APPDATA/pi-orca-HOME-BEFORE-UPDATE.tar pi-orca`. The archive contains device keys and must stay private.
4. Apply the selected image through Unraid, keeping the same hostname, env-file, two mounts, user and hardening. Check native status, sessions, settings, saved device grants and installed user packages.

To roll back, stop only `pi-orca`, choose the retained old version/digest in its Unraid repository field, and apply it with the same HOME. Verify it, not just Docker startup. A test must use two different real image digests, A → B → A; restarting the same digest is not an update/rollback test.

If a newer Orca version migrates its data incompatibly, do not let the older version repeatedly modify it. Stop the service, preserve the entire newer HOME separately, and restore the matching pre-update full-HOME backup to a **new** directory. Point the old image at that restored directory. Keep both copies until the data is reconciled; never delete newer data or promise arbitrary backward migration compatibility. A file/workspace backup is separate from this HOME backup.

## Verification commands

```sh
python3 -m unittest discover -s tests -v
python3 scripts/resolve.py --output build/resolved.json
# Supply the manifest values as the uppercase Docker build arguments documented
# in publish.yml, then run the same hardened smoke used by Actions:
bash scripts/smoke.sh ghcr.io/elmakus/pi-orca:YOUR_VERSION_TAG
```

The smoke uses no model key and must not dump Orca pairing output, raw logs, environment or runtime tokens. Physical Linux/Android/Teleport acceptance and real model generation require the deployed server and actual clients; do not infer those results from CI.

## Recorded deployment acceptance (2026-11-04)

The final Unraid container `pi-orca` is running the image from [Actions run 41826316133](https://github.com/elmakus/pi-orca-unraid/actions/runs/41826316133). The continuation check confirmed Docker `healthy`, successful native `orca-ide status`, the expected image/config digest, non-root user, both mounts, LAN-only port and hardening. Installed versions are Pi **1.5.0**, Orca **1.4.248**, Node **26.13.0**, GitHub CLI **2.103.3**, and Debian **13.7 (trixie)**.

Completed during deployment:

- **Resolver:** 25 deterministic boundary tests passed, along with live upstream resolution and an unchanged-input no-op poll. Daily date changes were covered by tests, not by waiting for a later day.
- **Runtime:** real hardened smoke passed locally, in Actions before publication, and after pulling from GHCR on Unraid. It exercised fresh writable binds, tools/venv, core PATH precedence, positive/negative native health, PTY, managed Pi/RPC without generation, and graceful stop with a live child without SIGKILL.
- **Publication:** anonymous tag/digest pulls succeeded. Published config digests matched the tested image IDs. [Run 41824595067](https://github.com/elmakus/pi-orca-unraid/actions/runs/41824595067) deliberately failed after publishing B but before promotion, leaving A as `latest`. The [older run's second attempt](https://github.com/elmakus/pi-orca-unraid/actions/runs/41823713764/attempts/2) refused to replace the newer final `latest`.
- **Model:** two minimal real prompts on the new service verified `codex_lb/gpt-6.1-sol`, `openai-responses`, medium effort, streamed text, a bash tool call/result followed by the final answer, and saved-history resume after image replacement. Orca's managed Pi session separately reported the same model and medium effort.
- **Persistence:** A → B → A preserved history/settings, Orca identity keys and a pinned test package. A stopped-service full-HOME backup was restored to a separate directory and read in an isolated offline container. The test package was removed from the final HOME; private backups and the sandbox/session evidence remain.
- **Unraid:** the template and DockerMan management label are installed, and the separate Autostart entry is enabled. Existing autostart entries were preserved. Container updates remain manual. `pinkcollab-omp` stayed healthy with its original ID, image and start time; no OMP or Home Assistant configuration was changed.

The following retained tags belong to `ghcr.io/elmakus/pi-orca`. Use the registry digest, not a moving tag, for an immutable reference:

| Role | Version tag | Registry digest |
| --- | --- | --- |
| A: rollback baseline | `pi-1.5.0-orca-1.4.248-rev-8d3e6632-20261104-41824240633-1` | `sha256:857f9202c5760fc2f85953ba9d62f717ae428db9065e322d9e330526e5eb301c` |
| B: deliberate pre-promotion failure | `pi-1.5.0-orca-1.4.248-rev-8d3e6632-20261104-41824595067-1` | `sha256:90ca65e76dd0857a7eb9b389bd755677878d525ecbe2f9d3374b5a9df732f0ac` |
| Final deployed `latest` | `pi-1.5.0-orca-1.4.248-rev-8d3e6632-20261104-41826316133-1` | `sha256:0a2533023856e6f3ce7c3eda636a3d0a2564d5420e15cc2e57b494f0dbf116fe` |

### Remaining acceptance limits

- **Real clients:** no physical Orca Linux or Android client was available or paired. Real device grants, LAN/Teleport reconnect and client-side Pi use remain unverified. Private sequential runtime/mobile/no-pairing bootstrap did pass and retained the server identity; this does not prove client acceptance.
- **Reasoning:** neither model probe emitted `thinking_delta`. Medium effort and text/tool transport passed, but visible reasoning-summary streaming is not verified.
- **Rollback:** A and B are different published registry/config digests with the **same Pi/Orca versions**. This proves image replacement and data persistence, not migration between application versions or compatibility with future Orca schema changes.
- **Monitoring:** the workflow is active, but external monitoring is **not active**: `HEALTHCHECKS_POLL_URL` and `HEALTHCHECKS_DAILY_URL` are absent. Create both external checks, add their private ping URLs as Actions secrets, and confirm delivery of a test alert as described above. Future scheduled/daily runs and GitHub's inactivity-disable behavior were not observed.
- **Unraid UI/restart:** DockerMan label/template and enabled Autostart were verified, but no logged-in Unraid browser was available to exercise **Docker → Check for Updates → Update**. No update was triggered just for that test, and no host/array restart was performed.
