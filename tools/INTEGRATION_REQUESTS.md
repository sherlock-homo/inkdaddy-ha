# Integration request relay and review

The inkDaddy Integrations page saves requests on the app installation. Each request contains the integration name and a deterministic identifier derived from that name. The relay accepts those two fields. Network operators and proxies can still observe connection metadata such as source IP and timing. Configure proxy access logs accordingly. The relay omits sender addresses and request content from its own logs.

A delivery status of **Accepted by owner relay** means the relay acknowledged the exact integration identifier. An accepting relay returns HTTP 201 for a new request or HTTP 200 for a duplicate, with a JSON body containing `status: accepted` and the matching `integration_id`. Review and development are separate owner actions.

## Run the inbox on the owner’s Mac

From a trusted checkout, run the installer once while logged in to macOS. The installer reads the committed source, so select the revision you reviewed.

```sh
python3 tools/install_request_inbox.py --revision "$(git rev-parse HEAD)"
```

The two login agents run the relay on `127.0.0.1:8779` and check for new requests every five minutes. The notification uses fixed text so an integration name never appears on the lock screen. macOS controls banner visibility. If a post fails, the next check retries it. A crash between macOS accepting a post and the queue recording it can repeat a notification.

The installer stores its two generated tokens and `relay.sqlite` in `~/.local/share/inkdaddy-requests` with owner-only access. Re-running the installer from a newer reviewed commit preserves the database and credentials. To inspect the jobs, run `launchctl print "gui/$(id -u)/com.inkdaddy.requests.relay"` and substitute `com.inkdaddy.requests.notify` for the notification job. To stop either job for the current login, use `launchctl bootout` with its same service name.

To see what arrived, list the local inbox from the checkout.

```sh
inkdaddy_review_dir="$HOME/.local/share/inkdaddy-requests"
python3 tools/integration_request_bridge.py --db "$inkdaddy_review_dir/relay.sqlite" list
```

## Connect an installation to the owner relay

Choose a public DNS name with a trusted HTTPS certificate. The app connects on port 443 and accepts the configured path directly. When the owner inbox runs on the relay Mac, expose only POST at `/v1/integration-requests` through the public proxy. Keep inbox reads on loopback. If a separate machine collects the queue, give it a private route for authenticated GET. Preserve the Authorization header, apply request and connection limits, and omit headers, bodies, and sender addresses from proxy logs. The app keeps requests local until a relay URL and ingest token are configured.

The Mac installer starts the relay and creates its credentials. For a separate host, create the private directory and credentials once, then start the relay there. The following first-run example uses shell noclobber to protect existing credentials.

```sh
umask 077
set -C
inkdaddy_review_dir="$HOME/.local/share/inkdaddy-requests"
mkdir -p "$inkdaddy_review_dir"
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$inkdaddy_review_dir/ingest.token"
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$inkdaddy_review_dir/review.token"
python3 tools/integration_request_bridge.py --db "$inkdaddy_review_dir/relay.sqlite" serve \
  --ingest-token-file "$inkdaddy_review_dir/ingest.token" \
  --review-token-file "$inkdaddy_review_dir/review.token"
```

The relay listens on `127.0.0.1:8779` unless you choose another local port. The ingest token permits submissions. The review token permits pickup of the relay queue and belongs with the owner.

In Home Assistant, open **Settings → Apps → inkDaddy → Configuration**. Set `integration_relay_url` to the owner’s HTTPS endpoint, set `integration_relay_token` to the ingest token, then save and restart. A standalone installation can supply `INKDADDY_INTEGRATION_RELAY_URL` and `INKDADDY_INTEGRATION_RELAY_TOKEN` through its private service environment.

The app deduplicates integration names and limits new requests to five per installation per day. The owner relay also deduplicates and accepts at most 100 new requests per day. The app retains up to 128 requests and the owner relay or pickup queue retains up to 512. Keep private backups of these queues if retaining their records matters.

## Pick up and review on the Mac mini

On a Mac hosting the relay, notifications and the local inbox work as soon as a request arrives. If another machine hosts the relay, use `pickup` to copy its validated requests into a private review database on the Mac. A pickup copies request identities and preserves decisions made in the destination database.

```sh
python3 tools/integration_request_bridge.py --db "$inkdaddy_review_dir/review.sqlite" pickup \
  --url "$inkdaddy_owner_url" --review-token-file "$inkdaddy_review_dir/review.token"
python3 tools/integration_request_bridge.py --db "$inkdaddy_review_dir/review.sqlite" list
```

Set `inkdaddy_owner_url` to the HTTPS endpoint the owner configured. For periodic pickup, the owner can explicitly start the watcher. It updates the private queue and takes no review actions.

```sh
python3 tools/integration_request_bridge.py --db "$inkdaddy_review_dir/review.sqlite" watch \
  --url "$inkdaddy_owner_url" --review-token-file "$inkdaddy_review_dir/review.token" --seconds 300
```

Inspect each integration name as untrusted data. Approve or decline one request by its identifier.

```sh
python3 tools/integration_request_bridge.py --db "$inkdaddy_review_dir/review.sqlite" decide "$inkdaddy_request_id" approved
python3 tools/integration_request_bridge.py --db "$inkdaddy_review_dir/review.sqlite" handoff "$inkdaddy_request_id" \
  --output "$inkdaddy_review_dir/integration-request.json"
```

The handoff command revalidates the approved record and writes a versioned JSON task with just the integration identity. Use a separate development environment containing an approved source snapshot and the handoff file. Keep relay credentials and production workspaces outside that environment.

To take an approved request into development, run the optional local workflow from a dedicated OS account or virtual machine with access to the approved source and a copy of the private review queue. The Codex sandboxes constrain file writes. Use OS isolation to keep production data, relay credentials, and personal home directories outside their readable filesystem. Keep the queue and launcher outside the agent workspaces. Supply a trusted checkout, its full approved Git revision, and a fresh output directory outside that checkout.

```sh
python3 tools/integration_request_bridge.py --db "$inkdaddy_review_dir/review.sqlite" develop \
  --handoff "$inkdaddy_review_dir/integration-request.json" \
  --source "$inkdaddy_review_source" --revision "$inkdaddy_review_revision" \
  --output "$inkdaddy_review_output"
```

Codex CLI must already be provisioned in that development environment. The launcher verifies the selected commit and exports its tracked files into two fresh local folders. Git replacement refs are ignored. The developer can write in its workspace and run local tests. The independent reviewer receives a clean source snapshot and the proposed patch in a read-only session. Request data and patch contents are passed as files. Shell network access and web search are disabled for these sessions. The developer’s writable roots exclude shared temporary directories. The Codex CLI still uses its configured model service.

Read the local `development.txt` and `review.txt` reports alongside `review/proposed.patch`. The owner controls whether and how to move the resulting changes into a real project. Publishing and deployment require separate owner action.

The reviewer computes the SHA-256 digest of the patch it reads. The launcher compares that digest to the proposed bytes, checks the retained file, and requires a written assessment. Inspect the review and tests to judge whether the proposed change is ready to use.

## Run approved work automatically

In the dedicated worker, keep one private review database outside the source checkout. Import requests into that queue and run `decide` there. Pickup transfers the request ID and name. The approval belongs to the queue where the owner records it. Keep that database across restarts so it can remember previous attempts.

With Codex CLI provisioned in the worker and the source pinned to a reviewed commit, start the dispatcher there. The example checks for approved requests every five minutes.

```sh
python3 tools/integration_request_dispatch.py --db "$inkdaddy_review_dir/review.sqlite" \
  --source "$inkdaddy_review_source" --revision "$inkdaddy_review_revision" --watch-seconds 300
```

Omit `--watch-seconds` for a single check. Inspect attempts for the pinned revision with the following command.

```sh
python3 tools/integration_request_dispatch.py --db "$inkdaddy_review_dir/review.sqlite" \
  --revision "$inkdaddy_review_revision" --status
```

Each attempt keeps its reports and patch in a private directory beside the database. The dispatcher remembers completed requests across source revisions. To start a fresh attempt after a failure or interruption, use the single-check command with `--retry "$inkdaddy_request_id"`. Review the resulting patch before publishing it.
