# inkDaddy Home Assistant repository checklist

## V1 release and bench acceptance

### Home Assistant installation and migration

- [x] Back up the local app, switch the installed App Store version to the existing private app data, and prevent both instances from claiming the Matter port.
- [x] Confirm Supervisor-managed Ingress, the existing Matter fabric and device identity, photos, dashboards, playback state, and all five cached frames. Prove a fresh authenticated frame receipt and inspect the panel.
- [ ] Verify the V1 version builds and updates from the repository without losing user data. Keep the local rollback copy private.

### App tabs

- [x] **Display.** Show confirmed artwork and separately identify pending or failed work. Verify native Next, Previous, Pause/Resume, and Refresh. Keep the next five visible and prepared. Make playlist selection, ordering, and unsaved feedback clear.
- [x] **Connect inkDaddy.** Use Home Assistant Matter discovery, distinguish unlinked, linked, offline and connecting states, and take people directly from a successful connection to their first frame. Verify the identity check and manual sharing fallback.
- [x] **Manage Photos.** Make batch results, upload progress and display-ready previews visible. Support photo selection, album creation and ordering, and explicit removal. Verify image orientation and the normal bulk flow.
- [x] **Dashboards.** Provide a no-code layout canvas, starter layouts, a widget gallery, constrained sizing and movement, per-display preview, and a short save path. Preserve existing layouts and native battery choices. Verify keyboard and mobile use.
- [x] **Dashboard data.** Offer only display-appropriate, recently reporting Home Assistant sensors that can be sampled at the display cadence. Never poll an entity because the user opened its picker. Show the time of the ePaper render clearly and handle a stale or missing reading honestly.
- [x] **Integrations.** Show actual managed Home Assistant, Matter and Thread connection state. Link to the native settings needed to resolve a problem.
- [x] **Devices.** Show the exact display binding, battery and last confirmed frame. Make source selection and controls agree with Display. Avoid claiming unsupported per-device scheduling or transport controls.
- [x] **Home Assistant.** Show the actual connection and entity catalog state with a clear refresh action. Distinguish Supervisor-managed access from standalone configuration and keep credentials out of responses.
- [x] **Settings.** Persist supported app preferences. Explain the panel refresh cadence and remaining capabilities using the active configuration.
- [x] **Updates.** Surface the app and firmware versions and the supported update path. Keep firmware updates signed and use the native Matter OTA flow where available.
- [x] **Diagnostics.** Provide live data and controller health, pending and confirmed transfer state, useful Thread guidance and actionable errors. Keep private values and network credentials out of the output.

### Render quality, power and release checks

- [x] Compare six-color image rendering on synthetic portraits, skin tones, smooth gradients and dark detail. Keep vector text and widget borders clear. Test the chosen renderer on the exact 800 × 480 panel format and record render time, transfer bytes and output checksums.
- [ ] Review worker, renderer and Matter performance. Keep the five-frame cache bounded, avoid repeated expensive work, and verify refresh timing and manual response after an idle period.
- [ ] Review battery behavior separately from server polling. Verify the LiPo-only refresh, measure idle and refresh current at the battery connection, and check Thread reception at the final mounting spot.
- [ ] Verify a clean device's QR-to-default-dashboard run and the four playback controls in Apple Home when an iPhone or unlocked Mirroring session is available. Verify platform limits on artwork separately.
- [x] Independently review authentication, authorization, in-memory and persisted credential use, transport, file access and package dependencies. Scan the final Git tree, history, and package for secrets. Re-run integration and browser checks against the same build before publishing the V1 candidate.

### V1 candidate benchmarks

- The pinned app build passed 134 Python tests, including four authenticated Matter CASE/BDX cases, and six controller tests. Chromium exercised managed app workflows at 393 px and the no-code editor at desktop, 390 px and 320 px. Preview rendering, keyboard editing and byte-for-byte YAML export passed.
- The six-color renderer averaged about 15 ms per synthetic color frame. Estimated color error dropped 79–93% on the synthetic fixtures. The color samples each used 128,000 transfer bytes. The grayscale sample used 102,486 bytes. Actual panel color has yet to be measured.
- An idle hub benchmark eliminated 48 MB of application-level frame reads per 250 checks. Median run time fell from 290 ms to 256 ms. The picker browsed 5,000 cached rows in 3.94 ms median and made zero Home Assistant requests. See the hardware acceptance items for LiPo and Thread measurements.
- Gitleaks found no secrets in the staged tree, reachable history or built application files. Bandit reported no medium or high findings. The Python and npm lock audits returned zero known vulnerabilities, and the npm registry signature checks passed. The app verifies Supervisor 2026.11.0 or later before enabling Ingress and the Matter controller.

## First public app release

- [x] Export an allowlist of app source and synthetic tests into a fresh Git repository. Exclude runtime data, hardware artifacts, and project history.
- [x] Add App Store metadata, installation instructions, and permission and data documentation.
- [x] Resolve the actionable findings from three independent reviews of export privacy, runtime security, and dependencies.
- [x] Scan the exported Git tree with Gitleaks and the Python runtime with Bandit.
- [x] Validate both manifests with the Home Assistant Supervisor schema. Build and test the exact amd64 app image on Home Assistant OS.
- [x] Publish the initial installable app to GitHub and verify an App Store installation on amd64.

## Security and verification results

- **Export.** The Git tree contains source, metadata, documentation, dependency locks, and synthetic tests. Bench identifiers were replaced in fixtures. The release build context uses an allowlist. Gitleaks reported zero findings, and Bandit reported no medium or high results.
- **Runtime.** The UI binds to the Supervisor gateway and checks the real peer. Browser writes require a same-origin request marker. Photo and frame access stays within managed storage, and requests, images, and YAML have size limits. Saved Home Assistant tokens stay bound to their configured origin. The Matter control API uses a private Unix socket, and native sharing verifies device identity before creating an app binding. A newly rejected peer that goes offline may require cleanup of the companion fabric.
- **Dependencies.** Updated the affected JavaScript and Python packages. The locked npm and Python audits returned zero findings. The image uses pinned base manifests, signed Debian snapshots, and hash-checked Python wheels. The [Debian Security Tracker](https://security-tracker.debian.org/tracker/) still matched 159 open package advisories and two undetermined cases in the chosen base. No matching advisory had a Bookworm fix available in the snapshot or a Debian-assigned medium or high severity. The package comparison does not establish runtime reachability.
- **Tests.** The staged app image passed 108 Python tests, including four authenticated CASE/BDX cases, and six JavaScript tests. An isolated Chromium run exercised dashboard preview, photo upload, and cross-origin denial. Tested the app image’s private Matter socket and Supervisor gateway selection. The Python wheel lock resolves for amd64 and arm64. App Store installation succeeded on amd64. Arm64 execution is still to be verified.
