# Integration request relay and review

The inkDaddy Integrations page saves requests on the app installation. Each request contains the integration name and a deterministic identifier derived from that name. The relay accepts those two fields. Network operators and proxies can still observe connection metadata such as source IP and timing. Configure proxy access logs accordingly. The relay omits sender addresses and request content from its own logs.

A delivery status of **Accepted by owner relay** means the relay acknowledged the exact integration identifier. An accepting relay returns HTTP 201 for a new request or HTTP 200 for a duplicate, with a JSON body containing `status: accepted` and the matching `integration_id`. Review and development are separate owner actions.

## Set up an owner relay

Choose a public DNS name with a trusted HTTPS certificate. The app connects on port 443 and accepts the configured path directly. Configure its reverse proxy to forward POST and GET at `/v1/integration-requests` to the loopback relay, preserve the Authorization header, apply request and connection limits, and avoid logging headers, bodies, and sender addresses. The app keeps requests local until a relay URL and ingest token are configured.

Keep the queue and credentials in a private local directory outside the repository. Generate two distinct tokens and store them in owner-only files.

```sh
umask 077
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

On the owner’s Mac mini, use `pickup` to copy the validated review queue into a separate private SQLite database. The owner can also inspect the relay’s local database directly when running the relay on the same machine.

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
