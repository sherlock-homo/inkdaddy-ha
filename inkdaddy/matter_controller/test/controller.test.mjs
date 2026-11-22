import { strict as assert } from "node:assert";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { ClusterBehavior, Endpoint, Environment, Logger, LogLevel, Millis, ServerNode } from "@matter/main";
import { PowerSourceServer } from "@matter/main/behaviors/power-source";
import { OnOffPlugInUnitDevice } from "@matter/main/devices/on-off-plug-in-unit";
import { NodeSession } from "@matter/main/protocol";
import { VendorId } from "@matter/main/types";
import { localNetwork, pullFrame } from "../fixtures/loopback.mjs";
import { startMatterController } from "../src/controller.mjs";
import { createControllerServer } from "../src/http.mjs";
import { FRAME_BYTES, FRAME_ENDPOINT, FrameCluster } from "../src/wire.mjs";

const DISCOVERY_TIME = Millis(8_000);
const token = "inkdaddy-test-only-token";
const transferId = "0123456789abcdef01234567";
const retryId = "76543210fedcba9876543210";
const raceId = "888877776666555544443333";
const deviceUid = "xiao-mg24-test-fixture";

// This uses two isolated controller fabrics and a software display. The display
// requests every block with the same initial framing as the pinned SiSDK client.
// The test never advertises or commissions outside the OS loopback interface.
test("shares from one Matter fabric and transfers authenticated packed pixels by BDX", { timeout: 55_000 }, async (t) => {
  Logger.defaultLogLevel = process.env.INKDADDY_TEST_DEBUG ? LogLevel.DEBUG : LogLevel.ERROR;
  const storagePath = await mkdtemp(join(tmpdir(), "inkdaddy-bdx-test-"));
  Environment.default.vars.set("storage.path", storagePath);
  Environment.default.vars.set("mdns.networkinterface", process.platform === "darwin" ? "lo0" : "lo");
  const display = await ServerNode.create(ServerNode.RootEndpointWithoutGroupcast.with(PowerSourceServer.with("Battery")), {
    id: "bdx-test-display",
    network: await localNetwork(),
    commissioning: { passcode: 20_202_021, discriminator: 0x123 },
    productDescription: { name: "inkDaddy BDX fixture", deviceType: 0x010a },
    basicInformation: { vendorId: VendorId(0xfff1), productId: 0x8005, serialNumber: "bdx-fixture" },
    powerSource: { batPercentRemaining: 163 },
  });
  let ha;
  let controller;
  let server;
  t.after(async () => {
    server?.close();
    server?.closeAllConnections();
    await controller?.close();
    await ha?.close();
    await display.close();
    await rm(storagePath, { recursive: true, force: true });
  });
  let reservedId;
  let displayed = false;
  let acceptanceCount = 0;
  let receiptReadCount = 0;
  let download;
  let senderSession;
  const raw = Buffer.from(Array.from({ length: FRAME_BYTES }, (_, i) => i % 251));
  const digest = sha256(raw);
  const releaseReply = Promise.withResolvers();
  const downloadedRaceFrame = Promise.withResolvers();
  class FixtureFrameServer extends ClusterBehavior.for(FrameCluster) {
    static lockOnInvoke = false;

    async queueFrame(offer) {
      acceptanceCount++;
      reservedId = offer.transferId;
      const session = this.context.session;
      assert(session instanceof NodeSession && !session.isPase);
      senderSession = session;
      assert.equal(offer.length, FRAME_BYTES);
      assert.equal(Buffer.from(offer.sha256).toString("hex"), digest);
      download = pullFrame(display, session, offer.transferId);
      // Awaited by the test below, including rejected exchanges.
      download.catch(() => {});
      if (offer.transferId === raceId) {
        await download;
        downloadedRaceFrame.resolve(session.fabric.fabricIndex);
        await releaseReply.promise;
      }
    }

    getReceipt({ transferId: id }) {
      receiptReadCount++;
      return { transferId: id, state: reservedId !== id ? 0 : displayed ? 2 : 1 };
    }
  }
  const panel = new Endpoint(OnOffPlugInUnitDevice.with(FixtureFrameServer), {
    id: "display", number: FRAME_ENDPOINT, [FixtureFrameServer.id]: { nodeUid: deviceUid },
  });
  await display.add(panel);
  const playbackSwitch = new Endpoint(OnOffPlugInUnitDevice, {
    id: "playback", number: 3, onOff: { onOff: false },
  });
  await display.add(playbackSwitch);
  await display.start();

  ha = await ServerNode.create(ServerNode.RootEndpointWithoutGroupcast, {
    id: "bdx-test-ha", network: await localNetwork(), commissioning: { enabled: false },
  });
  await ha.start();
  const haPeer = await ha.peers.commission({
    pairingCode: display.state.commissioning.pairingCodes.manualPairingCode,
    timeout: DISCOVERY_TIME,
    discoveryCapabilities: { onIpNetwork: true },
    autoSubscribe: false,
  });
  const share = await haPeer.openEnhancedCommissioningWindow();

  const inkNetwork = await localNetwork();
  controller = await startMatterController({ storagePath, network: inkNetwork });
  server = createControllerServer(controller, token);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = (path, init = {}) => fetch(`${base}${path}`, {
    ...init,
    headers: { ...init.headers, "Authorization": `Bearer ${token}` },
  });
  assert.equal((await fetch(`${base}/v1/nodes`)).status, 401);
  const pairedResponse = await call("/v1/commission", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: share.qrPairingCode }),
  });
  const paired = await pairedResponse.json();
  assert.equal(pairedResponse.status, 200, JSON.stringify(paired));
  assert.equal(paired.device_uid, deviceUid);
  assert.deepEqual(await (await call("/v1/nodes")).json(), { nodes: [paired] });

  const playbackPath = `/v1/nodes/${paired.node_id}/playback`;
  const setPlayback = (playing) => call(playbackPath, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ playing }),
  });
  assert.equal((await setPlayback(1)).status, 422);
  assert.equal(playbackSwitch.state.onOff.onOff, false);
  const playResult = await setPlayback(true);
  assert.equal(playResult.status, 200);
  assert.deepEqual(await playResult.json(), { playing: true });
  assert.equal(playbackSwitch.state.onOff.onOff, true);
  const pauseResult = await setPlayback(false);
  assert.equal(pauseResult.status, 200);
  assert.deepEqual(await pauseResult.json(), { playing: false });
  assert.equal(playbackSwitch.state.onOff.onOff, false);

  const path = `/v1/nodes/${paired.node_id}/frames/${transferId}`;
  const headers = { "Content-Type": "application/octet-stream", "X-Frame-SHA256": digest };
  const offered = await call(path, { method: "POST", body: raw, headers });
  const result = await offered.json();
  assert.equal(offered.status, 202, JSON.stringify(result));
  assert.deepEqual(result, { transfer_id: transferId, state: "queued" });
  const duplicate = await call(path, { method: "POST", body: raw, headers });
  assert.equal(duplicate.status, 202);
  assert.equal(acceptanceCount, 1);
  const other = Buffer.alloc(FRAME_BYTES, 3);
  assert.equal((await call(path, { method: "POST", body: other, headers })).status, 422);
  assert.equal((await call(path, { method: "POST", body: other, headers: { ...headers, "X-Frame-SHA256": sha256(other) } })).status, 409);

  assert.deepEqual(await download, raw);
  await assert.rejects(pullFrame(display, senderSession, "ffffffffffffffffffffffff"), /FileDesignatorUnknown|StatusReport/);
  assert.deepEqual(await (await call(`${path}?cached=1`)).json(), { transfer_id: transferId, state: "queued" });
  assert.equal(receiptReadCount, 0);
  assert.deepEqual(await (await call(path)).json(), { transfer_id: transferId, state: "queued" });
  displayed = true;
  assert.deepEqual(await (await call(path)).json(), { transfer_id: transferId, state: "displayed", battery_percent: 81.5 });
  assert.equal(receiptReadCount, 2);
  assert.deepEqual(await (await call(path, { method: "POST", body: raw, headers })).json(), { transfer_id: transferId, state: "displayed", battery_percent: 81.5 });
  assert.equal(acceptanceCount, 1);

  displayed = false;
  const retryPath = `/v1/nodes/${paired.node_id}/frames/${retryId}`;
  assert.equal((await call(retryPath, { method: "POST", body: raw, headers })).status, 202);
  assert.deepEqual(await download, raw);
  reservedId = undefined; // A reboot forgets the pending frame before refreshing the panel.
  assert.deepEqual(await (await call(retryPath)).json(), { transfer_id: retryId, state: "unknown" });
  assert.equal((await call(retryPath, { method: "POST", body: raw, headers })).status, 202);
  assert.deepEqual(await download, raw);
  assert.equal(acceptanceCount, 3);

  const actionsPath = `/v1/nodes/${paired.node_id}/actions`;
  assert.deepEqual(await (await call(actionsPath)).json(), { actions: [] });
  await panel.act(async (agent) => {
    const frame = agent.get(FixtureFrameServer);
    await frame.events.playbackRequested.emit({ action: 0 }, frame.context);
    await frame.events.playbackRequested.emit({ action: 3 }, frame.context);
  });
  let actions = [];
  for (let attempt = 0; attempt < 50 && actions.length < 2; attempt++) {
    await delay(100);
    ({ actions } = await (await call(actionsPath)).json());
  }
  assert.deepEqual(actions.map(({ action }) => action), ["next_content", "pause"]);
  assert.deepEqual(await (await call(`${actionsPath}?after=${actions[0].event_number}`)).json(), { actions: [actions[1]] });

  const racePath = `/v1/nodes/${paired.node_id}/frames/${raceId}`;
  let didReply = false;
  const earlyOffer = call(racePath, { method: "POST", body: raw, headers }).then((response) => {
    didReply = true;
    return response;
  });
  const priorReads = receiptReadCount;
  const raceReceipt = { transfer_id: raceId, state: "displayed", battery_percent: 49 };
  try {
    const fabricIndex = await downloadedRaceFrame.promise;
    await panel.act(async (agent) => {
      const frame = agent.get(FixtureFrameServer);
      await frame.events.displayReceipt.emit({ transferId: raceId, state: 2, battery: 98, fabricIndex }, frame.context);
    });
    let result;
    for (let attempt = 0; attempt < 50; attempt++) {
      result = await (await call(`${racePath}?cached=1`)).json();
      if (result.state === "displayed") break;
      await delay(75);
    }
    assert.deepEqual(result, raceReceipt);
    assert.equal(didReply, false);
    assert.equal(receiptReadCount, priorReads);
  } finally {
    releaseReply.resolve();
  }
  const posted = await earlyOffer;
  assert.equal(posted.status, 202);
  assert.deepEqual(await posted.json(), raceReceipt);

  const retired = new Promise((resolve) => server.close(resolve));
  server.closeAllConnections();
  await retired;
  await controller.close();
  controller = await startMatterController({ storagePath, network: inkNetwork });
  assert.deepEqual(await controller.nodes(), { nodes: [paired] });
  assert.deepEqual(await controller.actions(paired.node_id, 0n), { actions });
  assert.deepEqual(await controller.getCachedReceipt(paired.node_id, raceId), raceReceipt);
  assert.deepEqual(await controller.queueFrame(paired.node_id, transferId, raw, digest), { transfer_id: transferId, state: "displayed", battery_percent: 81.5 });
  assert.equal(acceptanceCount, 4);
});

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}
