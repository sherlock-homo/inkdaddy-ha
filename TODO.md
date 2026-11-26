# inkDaddy Home Assistant repository checklist

## Photo caption colors and place names

- [x] Render the capture date, elapsed time and location in three distinguishable inks. Preserve older frame receipts.
- [x] Resolve coordinate-only photos to reliable place labels and cache results for existing and future photos.
- [x] Make location lookup controls and data use clear in Photos.
- [ ] Check caption previews, existing photo upgrades and delivery on the paired frame.

## Simpler app navigation

- [x] Gather playback, photos, dashboards, integrations, devices and settings into six main tabs with working links to the moved tools.
- [x] Bring the integration request form in line with the other cards and remove test records and delivery details from the everyday view.
- [x] Trim redundant copy and preserve onboarding, feedback, retry actions and troubleshooting.
- [x] Verify navigation, deep links and mobile layout, publish the app and check the installed version.

Dev.27 passed 308 Python tests and six controller tests locally and in the Home Assistant package. Desktop Chrome and mobile WebKit verified navigation, old bookmarks, browser history, display selection, and the request form. Home Assistant backed up and installed the tested build. The installed form fetched no past requests, and the frame kept its current receipt, playlist and alternating schedule.

## Color dashboard and weekly forecast

- [x] Design the local date, condition-specific weather tiles and proportional battery graphic for the six-color panel. Check text contrast and small tiles.
- [x] Add a configurable seven-day forecast, defaulting to Atlanta, with local dates, units, caching and missing-data handling.
- [x] Add per-dashboard primary and accent colors, preserve editable layouts and remove redundant status copy.
- [x] Set the starter layout to date and weather above battery and forecast. Apply it to the owner's saved dashboard without changing the photo overlay or schedule.
- [x] Review and test the renderer, editor, old frame recovery and upgrade. Publish the app and verify the actual dashboard sent over Matter.

Dev.26 passed 307 Python tests and six controller tests on the Mac and in the Home Assistant package. Desktop Chrome and mobile WebKit verified the editor. All 15 weather scenes passed the 4.5:1 text contrast check. The installed app matched the tested package. The paired v7 frame acknowledged its redesigned dashboard over Thread, and its decoded frame matched the reference rendering pixel for pixel across all six inks. The frame then acknowledged the original photo with its metadata overlay. Pairing, playlist, and the alternating schedule matched their pre-upgrade state.

## Dashboard date, weather, and photo overlay repair

- [x] Restore the local date in the legacy starter dashboard and check older frame receipts.
- [x] Keep dashboard weather current and verify the reading against Home Assistant.
- [x] Make capture date, age and location easy to assign to the photo half of alternating playback.
- [x] Review, test and publish the app, then verify the dashboard and photo overlay on the paired frame.

Dev.25 passed 291 Python tests and six controller tests on the Mac and in the Home Assistant OS app image. An isolated dev.24 upgrade recovered the date tile, preserved the photo library and receipt, and delivered five alternating frames. Desktop Chromium and mobile WebKit verified the photo overlay selector. The backed-up App Store installation matched the tested package. On the paired frame, the acknowledged dashboard bitmap matched the Home Assistant date and weather. The acknowledged photo bitmap matched a fresh render with capture date, age, and location read from its original.

## Alternating photos and dashboards and Home Assistant controls

- [x] Add a per-display mode that alternates a playlist photo and a full dashboard, each for half of the configured cycle. Preserve existing playlists, overlays, and receipts.
- [x] Add display settings for the mode and dashboard and verify the half-cycle times and photo order.
- [x] Show playback commands as Home Assistant buttons, preserve the working Matter pairing, and explain the endpoint Identify buttons.
- [x] Resize dashboard widgets by dragging a side on a phone or desktop. Keep keyboard size controls, preserve neighboring tiles, and verify save/reopen.
- [x] Remove clocks from active layouts and the picker. Add configurable Home Assistant source selection for relevant existing and new widgets.
- [x] Check panel partial refresh support. Verify the signed firmware image, deliver it to the paired frame over Thread, and confirm its version and a frame receipt.
- [x] Test migration, scheduling, manual commands, and the installed app. Publish the release and verify it in Home Assistant.

Dev.24 passed 281 Python tests and six Node tests on the Mac and in the amd64 Home Assistant app package. In an isolated dev.23 upgrade the playlist, queue and current receipt survived, and an 11-minute cycle produced five confirmed test slots spaced exactly 330 seconds apart. Desktop Chrome, mobile Chrome and mobile WebKit exercised widget resizing, collision checks, source selection and save/reopen. The installed app rendered 3 × 2 previews with real Home Assistant sources. Its runtime matched the tested package, and release scans found zero secrets or medium/high Bandit findings.

On the paired battery-powered frame, signed v7 installed over Thread from v6. Home Assistant read back version 7 with all existing fabrics. After activating the 60-minute cycle, Home Assistant’s Next button delivered the selected full-screen dashboard and the frame acknowledged it. Previous returned to the exact original photo, which the frame also confirmed. The upcoming queue resumed with the saved dashboard on the half hour and the next photo on the hour.

## Photo metadata overlays

- [x] Read supported capture dates and locations from saved originals, including older uploads. Keep metadata local and handle missing, malformed, and oversized data.
- [x] Add no-code overlay widgets for the capture date, elapsed years, and photo location. Use the selected photo for the preview and the scheduled photo for playback.
- [x] Test mixed albums, anniversaries, missing tags, original retention and privacy. Publish and verify the updated app in Home Assistant.
- [x] Publish the approved HTTPS request endpoint, configure the installed app, and verify delivery to the Mac mini inbox.

Dev.23 passed 268 Python and six Node tests on the Mac and in the amd64 app image. In the mobile editor, tagged and untagged photos produced the expected previews for all three fields. A dev.22 library with identical photo pixels and different capture dates produced distinct overlays after upgrade. Home Assistant created a partial backup and installed the published app. The installed Python, frontend, and Matter controller matched the tested package. The original library photo produced three distinct 800 × 480 previews. The app preserved the binding, device preferences, dashboard, library, receipt, and scheduled slots. Source, history, and packaged app scans found zero secrets. Bandit reported zero medium or high findings.

## V1 follow-up

- [x] Composite transparent uploads onto panel white before Intelligent Crop and photo conversion. Check editor previews, saved raw frames and existing receipts.
- [x] Verify the transparent-photo fix in the packaged app and release it through the Home Assistant Apps repository.
- [x] Keep the last confirmed pixels on the panel when a scheduled slot repeats the same photo. Verify that manual refresh still sends, that changed artwork still sends, and that a restart preserves the receipt.
- [x] Label scheduled dashboard clocks clearly even when the owner gives the widget a custom title. Verify the editor's behavior when navigating with unsaved changes.
- [x] Show the installed app’s native update page, release and auto-update status, plus registered displays’ reported firmware. Verify recovery when Supervisor details are unavailable and refresh the app entry page after upgrades.
- [x] Exercise the packaged update, run the secret, dependency, performance and browser checks.
- [x] Verify the new store version through live Home Assistant Ingress, observe a scheduled repeat, then request a fresh receipt for the same photo.

The dev.21 Mac test suite passed 214 Python tests and six Node tests. The amd64 package passed 213 Python tests and six Node tests, including an authenticated Matter transfer after an explicit refresh. In a 24-hour one-photo schedule the hub skipped all 24 repeated transfers and kept the original receipt. The renderer reproduced all five reference bitmaps. Package browser checks covered the no-code editor at 390 and 1100 px, the update links, and the cached shell upgrade. Gitleaks scans of the source, history and packaged app found zero secrets. Bandit reported zero medium or high findings, and the Python and npm dependency audits reported zero known vulnerabilities.

Dev.22 passed 248 Python and six Node tests on the Mac and in the amd64 app package. A 390 px browser saved a dev.21 transparent original at its existing crop and verified both the old and corrected raw frame. In a 12-megapixel synthetic benchmark the corrected frame matched an opaque white-backed reference byte for byte. Home Assistant backed up and updated the installed app. Its Python, frontend and controller matched the tested package, and the upgrade preserved the library, device preferences, pairing, receipt, dashboard and five upcoming frames. Source, history and packaged application secret scans found zero secrets. Bandit found zero medium or high issues.

Native ARM64 Linux passed 247 Python and six Node tests on the published dev.22 source, including authenticated Matter transfer and a managed app upload with exact 800 × 480 preview and raw pixel checks. The offline ARM64 guest lacked Git for one owner-tool test covered on amd64. The stock ARM64 Home Assistant OS store-install check was last completed on dev.21.

Home Assistant backed up the installed app and updated it to dev.21 through Apps on amd64. The installed Python, Matter controller and frontend files matched the tested build. The upgrade preserved the existing binding, library, dashboard, playback receipt and scheduled slots. On November 23 at 12:00 UTC the scheduler kept the confirmed photo on screen, retained the 11:01 UTC receipt and prepared five more slots. An explicit refresh then received a new authenticated receipt for the same frame after 83.7 seconds. All five upcoming frames kept their order. Power source and physical current were unverified.

An isolated native ARM64 Linux guest passed 213 Python and six Node tests. Stock ARM64 Home Assistant OS 18.4 installed the published app through its Apps catalog. The first build timed out fetching a Debian snapshot and the same release installed on retry. A new Home Assistant owner opened authenticated Ingress, uploaded a synthetic Intelligent Crop image, and retrieved its 800 × 480 preview and 192,000-byte raw frame. Both architectures served the same frontend. A fresh comparison of the deployed amd64 base against the Debian tracker found 159 open matches (one low, 43 unimportant and 115 awaiting urgency), two undetermined entries, and zero available Bookworm fixes.

## Photo playback customization

- [x] Add Intelligent Crop and an editor for existing photos. Preserve the original and previous processed, preview, and raw versions.
- [x] Build reusable photo overlays for each display, including Home Assistant sensors, date, and opt-in upcoming calendar details.
- [x] Make the refresh interval configurable per display. Preserve installed playback state, confirmed frames, and the five-frame queue.
- [x] Add anonymous integration requests and a private relay with a durable queue. Provide a Mac mini workflow for development and independent review.
- [x] Exercise an upgrade from the existing app, test the photo and overlay editor in the browser, verify the schedule and integration request flow, and review the release contents for sensitive data.
- [x] Install the private Mac mini relay and its five-minute notification job. Keep request names and device metadata out of desktop notifications.
- [x] Implement dispatch of owner-approved requests to a developer and independent reviewer. Preserve reports across restarts and prevent duplicate runs.
- [x] Test the queue, notification delivery state, dispatcher, permissions and upgrade behavior with synthetic requests. Check the installed Mac service without adding a fake request to the owner's inbox.
- [x] Send a labeled request through the installed app and the public HTTPS endpoint. Match its ID in the Mac mini inbox and confirm Notification Center stored the notification. The test request was declined.
- [ ] Confirm the owner sees a notification banner. macOS muted the acceptance-test banner.
- [x] Provision an isolated worker on the Mac mini with local inference, a reviewed source revision and separate production and test queues.
- [x] Make the public review launcher verify that the review report identifies the exact retained patch.
- [ ] Verify development and independent review of a real owner-approved integration request.

The owner tools passed the Mac suite of 230 Python and six Node tests. An isolated HA app sent a synthetic request over certificate-validated HTTPS, and a second pickup kept a single pending entry. The installed Mac relay answered an authenticated request on loopback. Both launchd jobs loaded, the empty-inbox notification check exited successfully, and a repeat installation preserved the queue and credentials. The dispatcher produced a patch and a separate review report using stand-in agent processes. Gitleaks found zero secrets in the staged source and reachable history, and Bandit found zero medium or high issues.

A subsequent worker test used two independent sessions of a real local model. The isolated fixture completed in 117 seconds. The developer proposed a one-file patch and the reviewer described the mapping and identified the exact retained diff. In a fresh offline checkout the regression test failed before applying the patch and passed afterward. The Mac login service recovered after a cold stop of the guest. The live queues were empty at verification. Failed attempts retain their reports for owner inspection and explicit retry.

The dev.20 local suite passed 208 Python tests, including authenticated loopback Matter delivery at a shortened interval, and six controller tests. The amd64 Home Assistant package passed 207 Python tests and six controller tests. The Mac mini runner’s Git test passed on the Mac. The packaged migration from dev.19 preserved the original photo digest, the confirmed transfer, and five scheduled frames. Chromium verified the 390–393 px photo, overlay, interval, and request workflows. The reviews covered cadence changes, exact bitmap recovery, relay data and acknowledgment, and the Mac mini runner’s Git and sandbox isolation. Gitleaks found no secrets in the source, reachable history, or packaged application files. Bandit reported no medium or high findings.

## V1 release and bench acceptance

### Home Assistant installation and migration

- [x] Back up the local app, switch the installed App Store version to the existing private app data, and prevent both instances from claiming the Matter port.
- [x] Confirm Supervisor-managed Ingress, the existing Matter fabric and device identity, photos, dashboards, playback state, and all five cached frames. Prove a fresh authenticated frame receipt and inspect the panel.
- [x] Verify the V1 version builds and updates from the repository without losing user data. Keep the local rollback copy private.

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
- [x] Review worker, renderer and Matter performance. Keep the five-frame cache bounded, avoid repeated expensive work, and verify refresh timing and manual response after an idle period.
- [x] Verify an authenticated refresh on owner-confirmed battery power. On November 24 the display returned a fresh receipt after 123 seconds for the same photo and preserved all five upcoming frames.
- [ ] Measure idle and refresh current at the battery connection.
- [ ] Verify Thread reception and a confirmed frame at the final mounting spot.
- [ ] Verify a clean device's QR-to-default-dashboard run.
- [ ] Verify playback controls and artwork visibility in Apple Home. The signed-in Mac Home app showed the owner's rooms but did not list the frame during the November 23 check.
- [x] Independently review authentication, authorization, in-memory and persisted credential use, transport, file access and package dependencies. Scan the final Git tree, history, and package for secrets. Re-run integration and browser checks against the same build before publishing the V1 candidate.

### V1 candidate benchmarks

- The pinned app build passed 134 Python tests, including four authenticated Matter CASE/BDX cases, and six controller tests. Chromium exercised managed app workflows at 393 px and the no-code editor at desktop, 390 px and 320 px. Preview rendering, keyboard editing and byte-for-byte YAML export passed.
- An amd64 Home Assistant App Store update preserved the data and Matter pairing. The panel confirmed a manually refreshed photo in about 62 seconds, and a camera check verified the physical image. The app retained all five upcoming slots.
- The six-color renderer averaged about 15 ms per synthetic color frame. Estimated color error dropped 79–93% on the synthetic fixtures. The color samples each used 128,000 transfer bytes. The grayscale sample used 102,486 bytes. Actual panel color has yet to be measured.
- An idle hub benchmark eliminated 48 MB of application-level frame reads per 250 checks. Median run time fell from 290 ms to 256 ms. The picker browsed 5,000 cached rows in 3.94 ms median and made zero Home Assistant requests. See the hardware acceptance items for LiPo and Thread measurements.
- Gitleaks found no secrets in the staged tree, reachable history or built application files. Bandit reported no medium or high findings. The Python and npm lock audits returned zero known vulnerabilities, and the npm registry signature checks passed. The app verifies Supervisor 2026.11.0 or later before enabling Ingress and the Matter controller.

### Cache and credential follow-up

- A 48-hour playback simulation preserved five upcoming slots and the exact pending frame across restart. With two unchanged photo sources, the hub converted each photo once, down from 21 conversions, and retained at most seven raw assets (1.344 MB). A separate 12-hour run with a damaged photo preserved the queue and left no orphan files. Playback previews and managed Matter transfers verify the stored frame digest.
- The packaged follow-up passed 143 Python tests, including four authenticated Matter transfers, and six controller tests. The same package passed the desktop and mobile browser workflows and the built application scan.
- Standalone Home Assistant token replacement preserves the current token across failed writes and removes superseded tokens after a successful commit. The managed connection uses Supervisor credentials and bypasses the standalone store.
- The installed App Store build offered a distinct, prepared photo 0.16 seconds after the hour. The matching Matter receipt arrived 65.7 seconds after the hour, the camera confirmed the panel changed, and all five future slots were prepared. This scheduled run used the USB-connected bench setup.

### Photo color and dashboard freshness

- The RGB pipeline normalizes Display P3 and other supported ICC profiles to sRGB before six-color dithering. Older processed photos can recover the original profile without changing their crop. Derived PNGs carry an sRGB marker and omit inherited EXIF and XMP. Previously offered frames still require the exact original digest for repair.
- Dashboard clocks distinguish a scheduled hour, a manual request, and a preview. The sensor picker expires old readings while open and keeps its reads local to the app cache. The display battery tile identifies its last reported value.
- Native Linux ARM64 smoke runs of dev.18 and dev.19 verified the hash-locked Python environment, frontend build, private Matter socket, managed Ingress, five-slot playback, and authenticated loopback CASE/BDX. Dev.19 also passed 21 targeted color, clock, and sensor picker tests in the offline guest. The V1 follow-up records the subsequent dev.21 Apps installation on ARM64 Home Assistant OS.
- The dev.19 App Store image passed 169 Python tests and six controller tests. The packaged frontend passed the desktop and mobile browser workflows, and Gitleaks found no secrets in the built application files.
- An amd64 App Store update preserved the existing Matter binding and five upcoming frames. Every upcoming photo matched the color-managed digest, and the physical panel confirmed the corrected current photo on a same-photo refresh.
- The running dev.19 App Store build offered its prepared photo 0.38 seconds after the hour and received the matching Matter receipt 42.8 seconds after the hour. A camera check confirmed the physical change. Five future slots stayed prepared.
- On the physical frame, the native Home Assistant Matter controls passed Pause, Next, Previous, Refresh, and Play. Refresh received a new transfer receipt for the same photo bytes. Playback resumed at the original photo with the original upcoming order.

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
- **Tests.** The staged app image passed 108 Python tests, including four authenticated CASE/BDX cases, and six JavaScript tests. An isolated Chromium run exercised dashboard preview, photo upload, and cross-origin denial. Tested the app image’s private Matter socket and Supervisor gateway selection. The Python wheel lock resolves for amd64 and arm64. App Store installation succeeded on amd64.
