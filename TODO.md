# inkDaddy Home Assistant repository checklist

- [x] Export an allowlist of app source and synthetic tests into a fresh Git repository. Exclude runtime data, hardware artifacts, and project history.
- [x] Add App Store metadata, installation instructions, and permission and data documentation.
- [x] Resolve the actionable findings from three independent reviews of export privacy, runtime security, and dependencies.
- [x] Scan the exported Git tree with Gitleaks and the Python runtime with Bandit.
- [x] Validate both manifests with the Home Assistant Supervisor schema. Build and test the exact amd64 app image on Home Assistant OS.
- [x] Prepare the initial commit for publishing.

## Security and verification results

- **Export.** The Git tree contains source, metadata, documentation, dependency locks, and synthetic tests. Bench identifiers were replaced in fixtures. The release build context uses an allowlist. Gitleaks reported zero findings, and Bandit reported no medium or high results.
- **Runtime.** The UI binds to the Supervisor gateway and checks the real peer. Browser writes require a same-origin request marker. Photo and frame access stays within managed storage, and requests, images, and YAML have size limits. Saved Home Assistant tokens stay bound to their configured origin. The Matter control API uses a private Unix socket, and native sharing verifies device identity before creating an app binding. A newly rejected peer that goes offline may require cleanup of the companion fabric.
- **Dependencies.** Updated the affected JavaScript and Python packages. The locked npm and Python audits returned zero findings. The image uses pinned base manifests, signed Debian snapshots, and hash-checked Python wheels. The [Debian Security Tracker](https://security-tracker.debian.org/tracker/) still matched 159 open package advisories and two undetermined cases in the chosen base. No matching advisory had a Bookworm fix available in the snapshot or a Debian-assigned medium or high severity. The package comparison does not establish runtime reachability.
- **Tests.** The staged app image passed 108 Python tests, including four authenticated CASE/BDX cases, and six JavaScript tests. An isolated Chromium run exercised dashboard preview, photo upload, and cross-origin denial. Tested the app image’s private Matter socket and Supervisor gateway selection. The Python wheel lock resolves for amd64 and arm64. An App Store installation from the eventual Git URL and arm64 execution are still to be verified.
