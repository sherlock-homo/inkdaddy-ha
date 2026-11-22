import { strict as assert } from "node:assert";
import { createHash, randomBytes } from "node:crypto";
import { once } from "node:events";
import { createInterface } from "node:readline";
import { stdin, stdout } from "node:process";
import { ClusterBehavior, Endpoint, Environment, Logger, LogLevel, Millis, ServerNode } from "@matter/main";
import { PowerSourceServer } from "@matter/main/behaviors/power-source";
import { OnOffPlugInUnitDevice } from "@matter/main/devices/on-off-plug-in-unit";
import { NodeSession } from "@matter/main/protocol";
import { ClusterType, VendorId } from "@matter/main/types";
import { startMatterController } from "../src/controller.mjs";
import { createControllerServer } from "../src/http.mjs";
import { FRAME_BYTES, FRAME_ENDPOINT, FrameCluster } from "../src/wire.mjs";
import { localNetwork, pullFrame } from "./loopback.mjs";

const uid = "xiao-mg24-01234abc";
const storagePath = process.argv[2];
const revision = Number(process.argv[3] ?? 2);
const token = randomBytes(24).toString("hex");
const send = (data) => stdout.write(`INKDADDY_E2E ${JSON.stringify(data)}\n`);
assert(storagePath, "Provide an isolated matter.js store.");
assert([2, 3].includes(revision), "Provide a supported frame cluster revision.");
Logger.defaultLogLevel = LogLevel.ERROR;
Environment.default.vars.set("storage.path", storagePath);
Environment.default.vars.set("mdns.networkinterface", process.platform === "darwin" ? "lo0" : "lo");

// The controller must discover the revision advertised by this actual Matter endpoint.
const peerSchema = FrameCluster.schema.clone();
const revisionAttribute = peerSchema.attributes(0xfffd);
revisionAttribute.default = revision;
revisionAttribute.conformance = "M";
const PeerCluster = ClusterType(peerSchema);

const panelNibbles = [0, 1, 2, 3, 5, 6];

function unpackPackBits(encoded) {
  const raw = Buffer.alloc(FRAME_BYTES);
  let input = 0;
  let output = 0;
  while (input < encoded.length) {
    const control = encoded[input++];
    assert.notEqual(control, 128, "Reserved PackBits control byte");
    const literal = control < 128;
    const run = literal ? control + 1 : 257 - control;
    const consumed = literal ? run : 1;
    assert(input + consumed <= encoded.length, "Incomplete PackBits packet");
    assert(output + run <= raw.length, "PackBits packet overruns the frame");
    if (literal) {
      encoded.copy(raw, output, input, input + run);
    } else {
      raw.fill(encoded[input], output, output + run);
    }
    input += consumed;
    output += run;
  }
  assert.equal(output, raw.length);
  return raw;
}

function unpackPalette(encoded) {
  assert.equal(encoded.length, FRAME_BYTES * 2 / 3);
  const raw = Buffer.alloc(FRAME_BYTES);
  for (let input = 0, output = 0; input < encoded.length; input += 2, output += 3) {
    let value = encoded.readUInt16BE(input);
    assert(value < panelNibbles.length ** 6, "Invalid packed palette word");
    for (let byte = output + 2; byte >= output; byte--) {
      const lower = panelNibbles[value % 6];
      value = Math.floor(value / 6);
      const upper = panelNibbles[value % 6];
      value = Math.floor(value / 6);
      raw[byte] = (upper << 4) | lower;
    }
  }
  return raw;
}

let display;
let ha;
let controller;
let http;
let offer;
let acceptanceCount = 0;
let receiptReads = 0;
class PlaybackPeer extends ClusterBehavior.for(PeerCluster) {
  queueFrame({ transferId, length, sha256, packing, encodedLength }) {
    const session = this.context.session;
    assert(session instanceof NodeSession && !session.isPase);
    assert.equal(length, FRAME_BYTES);
    if (packing === undefined) {
      assert.equal(encodedLength, undefined, "Raw offers must omit both packing fields");
    } else {
      assert.equal(revision, 3, "A legacy peer must receive raw offers");
      assert([1, 2].includes(packing), "Unknown frame packing");
      assert(Number.isInteger(encodedLength) && encodedLength > 0 && encodedLength < length);
    }
    acceptanceCount++;
    const expected = Buffer.from(sha256).toString("hex");
    const suffix = packing === undefined ? "raw" : "packed";
    const download = pullFrame(display, session, transferId, {
      length: encodedLength ?? length,
      suffix,
    }).then((payload) => {
      let canonical = payload;
      if (packing === 1) canonical = unpackPackBits(payload);
      if (packing === 2) canonical = unpackPalette(payload);
      assert.equal(canonical.length, length);
      const digest = createHash("sha256").update(canonical).digest("hex");
      assert.equal(digest, expected);
      return {
        byte_count: canonical.length,
        wire_byte_count: payload.length,
        packing: packing ?? null,
        offered_encoded_length: encodedLength ?? null,
        designator_suffix: suffix,
        sha256: digest,
        transport: "CASE/BDX",
      };
    });
    download.catch(() => {});
    const fabricIndex = session.fabric?.fabricIndex;
    assert(fabricIndex);
    offer = { transferId, download, displayed: false, fabricIndex };
  }

  getReceipt({ transferId }) {
    receiptReads++;
    return { transferId, state: offer?.transferId !== transferId ? 0 : offer.displayed ? 2 : 1 };
  }
}

async function emitReceipt(panel, transferId, state, battery, fabricIndex) {
  await panel.act(async (agent) => {
    const peer = agent.get(PlaybackPeer);
    await peer.events.displayReceipt.emit({ transferId, state, battery, fabricIndex }, peer.context);
  });
}

try {
  display = await ServerNode.create(ServerNode.RootEndpointWithoutGroupcast.with(PowerSourceServer.with("Battery")), {
    id: "playback-peer", network: await localNetwork(),
    commissioning: { passcode: 20_202_021, discriminator: 0x8b7 },
    productDescription: { name: "inkDaddy", deviceType: 0x010a },
    basicInformation: { vendorId: VendorId(0xfff1), productId: 0x8005, serialNumber: uid },
    powerSource: { batPercentRemaining: 40 },
  });
  const panel = new Endpoint(OnOffPlugInUnitDevice.with(PlaybackPeer), {
    id: "display", number: FRAME_ENDPOINT, [PlaybackPeer.id]: { nodeUid: uid },
  });
  await display.add(panel);
  const playbackSwitch = new Endpoint(OnOffPlugInUnitDevice, {
    id: "playback", number: 3, onOff: { onOff: false },
  });
  await display.add(playbackSwitch);
  await display.start();
  ha = await ServerNode.create(ServerNode.RootEndpointWithoutGroupcast, {
    id: "first-fabric", network: await localNetwork(), commissioning: { enabled: false },
  });
  await ha.start();
  const firstFabricPeer = await ha.peers.commission({
    pairingCode: display.state.commissioning.pairingCodes.manualPairingCode,
    timeout: Millis(8_000), discoveryCapabilities: { onIpNetwork: true }, autoSubscribe: false,
  });
  const share = await firstFabricPeer.openEnhancedCommissioningWindow();
  controller = await startMatterController({ storagePath, network: await localNetwork() });
  http = createControllerServer(controller, token);
  http.listen(0, "127.0.0.1");
  await once(http, "listening");
  send({ port: http.address().port, token, share_code: share.qrPairingCode, device_uid: uid });

  for await (const line of createInterface({ input: stdin, terminal: false })) {
    try {
      const request = JSON.parse(line);
      if (request.action === "emit_next") {
        await panel.act(async (agent) => {
          const peer = agent.get(PlaybackPeer);
          await peer.events.playbackRequested.emit({ action: 0 }, peer.context);
        });
        send({ ok: true });
      } else if (request.action === "counts") {
        send({ acceptance_count: acceptanceCount, receipt_reads: receiptReads });
      } else if (request.action === "playback_state") {
        send({ playing: playbackSwitch.state.onOff.onOff });
      } else if (request.action === "emit_receipt") {
        assert(offer);
        const index = request.other_fabric ? (offer.fabricIndex === 1 ? 2 : 1) : offer.fabricIndex;
        await emitReceipt(panel, request.transfer_id, request.state, request.battery, index);
        send({ ok: true });
      } else {
        assert.equal(offer?.transferId, request.transfer_id);
        const verified = await offer.download;
        if (request.action === "display") {
          offer.displayed = true;
          await emitReceipt(panel, offer.transferId, 2, 163, offer.fabricIndex);
        } else {
          assert.equal(request.action, "download");
        }
        send({ ...verified, displayed: offer.displayed });
      }
    } catch (error) {
      send({ error: String(error) });
    }
  }
} finally {
  if (http) {
    const stopped = new Promise((resolve) => http.close(resolve));
    http.closeAllConnections();
    await stopped;
  }
  await controller?.close();
  await ha?.close();
  await display?.close();
}
