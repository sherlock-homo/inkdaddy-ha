import { Environment, Millis, Seconds, ServerNode, StorageService } from "@matter/main";
import { OnOff } from "@matter/main/clusters/on-off";
import { ExchangeManager, Invoke, Read, ResultStatus, Subscribe } from "@matter/main/protocol";
import { QrPairingCodeCodec, Status, StatusResponseError } from "@matter/main/types";
import { mkdir } from "node:fs/promises";
import { FrameBdxProtocol } from "./frame-bdx.mjs";
import { encodeFrame, selectFrameEncoding } from "./frame-packing.mjs";
import { FRAME_BYTES, FRAME_ENDPOINT, FRAME_PACKING_REVISION, FrameCluster, FramePacking, frameStates, nodeUidPattern, playbackActions } from "./wire.mjs";

const MAX_RECEIPTS_PER_NODE = 32;
const MAX_ACTIONS_PER_NODE = 1024;
const MAX_ACTIONS_PER_REPLY = 128;
const INTERACTION_TIMEOUT_MS = 12_000;
const FRAME_CAPABILITY_TTL_MS = 5 * 60_000;
const PLAYBACK_TIMEOUT_MS = 25_000;
const PLAYBACK_ENDPOINT = 3;

export class ControllerError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function startMatterController({ storagePath, networkPort, network, nodeName = "inkdaddy-frame-controller" }) {
  await mkdir(storagePath, { recursive: true, mode: 0o700 });
  Environment.default.vars.set("storage.path", storagePath);
  const node = await ServerNode.create(ServerNode.RootEndpointWithoutGroupcast, {
    id: nodeName,
    network: { port: networkPort, tcp: false, autoStartCommissionedPeers: false, ...network },
    commissioning: { enabled: false },
  });
  try {
    await node.start();
    const bdx = new FrameBdxProtocol();
    node.env.get(ExchangeManager).addProtocolHandler(bdx);
    const manager = await node.env.get(StorageService).open(`${nodeName}-service`);
    return await InkDaddyController.open(node, bdx, manager);
  } catch (error) {
    await node.close();
    throw error;
  }
}

export class InkDaddyController {
  #commissioning = false;
  #inFlight = new Map();
  #frameCapabilities = new Map();
  #playbackChanges = new Map();
  #subscriptions = new Map();
  #writeTail = Promise.resolve();
  #node;
  #bdx;
  #storage;
  #manager;
  #records;

  static async open(node, bdx, manager) {
    const storage = manager.createContext("frameController");
    return new this(node, bdx, manager, storage, await storage.get("nodes", {}));
  }

  constructor(node, bdx, manager, storage, records) {
    this.#node = node;
    this.#bdx = bdx;
    this.#storage = storage;
    this.#manager = manager;
    this.#records = records;
  }

  async close() {
    for (const subscription of this.#subscriptions.values()) subscription.abort();
    await this.#bdx.close();
    await this.#node.close();
    await this.#writeTail;
    await this.#manager.close();
  }

  #persist() {
    const snapshot = structuredClone(this.#records);
    const write = this.#writeTail.then(() => this.#storage.set("nodes", snapshot));
    this.#writeTail = write.catch(() => {});
    return write;
  }

  #record(nodeId) {
    return this.#records[nodeId] ??= { device_uid: null, offers: [] };
  }

  #findPeer(nodeId) {
    const peer = this.#node.peers.commissioned.find((candidate) => candidate.peerAddress?.nodeId.toString() === nodeId);
    if (!peer) throw new ControllerError(404, "Matter node is not on the inkDaddy fabric.");
    return peer;
  }

  async #readAttribute(peer, path, abort = AbortSignal.timeout(INTERACTION_TIMEOUT_MS)) {
    const options = Read({
      fabricFilter: true,
      attributes: [path],
    });
    const read = { ...options, includeKnownVersions: false, abort };
    for await (const chunk of peer.interaction.read(read)) {
      for await (const report of chunk) {
        if (report.kind === "attr-value") return report.value;
      }
    }
    return null;
  }

  async #supportsFramePacking(peer, nodeId) {
    const cached = this.#frameCapabilities.get(nodeId);
    if (cached && cached.expiresAt > Date.now()) return cached.supported;
    let revision;
    try {
      revision = await this.#readAttribute(peer, {
        endpointId: FRAME_ENDPOINT, clusterId: FrameCluster.id, attributeId: 0xfffd,
      });
    } catch {
      return false;
    }
    if (!Number.isInteger(revision)) return false;
    const supported = revision >= FRAME_PACKING_REVISION;
    this.#frameCapabilities.set(nodeId, { supported, expiresAt: Date.now() + FRAME_CAPABILITY_TTL_MS });
    return supported;
  }

  async #identity(peer, refresh = false, expectedUid) {
    const node_id = peer.peerAddress.nodeId.toString();
    const record = this.#record(node_id);
    if (refresh) {
      try {
        const value = await this.#readAttribute(peer, { endpointId: FRAME_ENDPOINT, clusterId: FrameCluster.id, attributeId: 0 });
        const uid = typeof value === "string" && nodeUidPattern.test(value) ? value : null;
        if (expectedUid !== undefined && uid !== expectedUid) return { node_id, device_uid: null };
        if (uid !== record.device_uid) {
          record.device_uid = uid;
          await this.#persist();
        }
      } catch {
        if (expectedUid !== undefined) return { node_id, device_uid: null };
        // A sleeping or offline node can be inspected again when it becomes available.
      }
    }
    return { node_id, device_uid: record.device_uid };
  }

  async nodes(refresh = false) {
    return { nodes: await Promise.all(this.#node.peers.commissioned.map((peer) => this.#identity(peer, refresh))) };
  }

  async commission(code, expectedUid) {
    if (this.#commissioning) throw new ControllerError(409, "A share code is already being processed.");
    this.#commissioning = true;
    try {
      const commissioned = expectedUid === undefined ? [] : this.#node.peers.commissioned.map(
        (peer) => ({ peer, nodeId: peer.peerAddress?.nodeId.toString() }),
      );
      // This path assumes HA has already put the device on Thread and opened its
      // enhanced commissioning window. No network credentials are requested.
      const peer = await this.#node.peers.commission({
        ...pairingOptions(code),
        discoveryCapabilities: { onIpNetwork: true },
        autoSubscribe: false,
        timeout: Millis(30_000),
        abort: AbortSignal.timeout(40_000),
      });
      const identity = await this.#identity(peer, true, expectedUid);
      if (expectedUid !== undefined && identity.device_uid !== expectedUid) {
        const preExisting = commissioned.some((old) => old.peer === peer || old.nodeId === identity.node_id);
        if (!preExisting) {
          try {
            // matter.js removes the caller's fabric from this peer and deletes
            // its local entry only after the peer confirms removal.
            await peer.decommission();
            delete this.#records[identity.node_id];
            await this.#persist();
          } catch {
            throw new ControllerError(409, "The display identity was rejected and the new Matter peer could not be removed.");
          }
        }
        throw new ControllerError(409, "The Matter identity did not match the selected display.");
      }
      return identity;
    } catch (error) {
      if (error instanceof ControllerError) throw error;
      throw new ControllerError(502, "Matter commissioning did not complete. Check the share window and Thread reachability.");
    } finally {
      this.#commissioning = false;
    }
  }

  async #invoke(peer, cluster, endpoint, command, fields, abort = AbortSignal.timeout(INTERACTION_TIMEOUT_MS)) {
    const options = Invoke({ commands: [{ cluster, endpoint, command, fields }] });
    const request = { ...options, batchDuration: false, abort };
    for await (const chunk of peer.interaction.invoke(request)) {
      for (const result of chunk) {
        if (result.kind === "cmd-status") {
          if (result.status === Status.Busy) throw new ControllerError(409, "Display is handling another frame.");
          if (result.status !== Status.Success) throw ResultStatus.errorOf(result);
          return undefined;
        }
        if (result.kind === "cmd-response") return result.data;
      }
    }
    throw new ControllerError(502, "Matter returned no command response.");
  }

  queueFrame(nodeId, transferId, raw, digest) {
    const key = `${nodeId}:${transferId}`;
    const inFlight = this.#inFlight.get(key);
    if (inFlight) {
      if (inFlight.digest !== digest) throw new ControllerError(409, "The transfer ID already names different frame bytes.");
      return inFlight.result;
    }
    const flight = { digest, offered: false };
    this.#inFlight.set(key, flight);
    flight.result = this.#queueFrame(nodeId, transferId, raw, flight).finally(() => this.#inFlight.delete(key));
    return flight.result;
  }

  async #queueFrame(nodeId, transferId, raw, flight) {
    const { digest } = flight;
    const peer = this.#findPeer(nodeId);
    const record = this.#record(nodeId);
    if (!record.device_uid) throw new ControllerError(409, "This Matter node has no verified inkDaddy device UID.");
    let known = this.#findOffer(record, transferId);
    if (known && known.digest !== digest) throw new ControllerError(409, "The transfer ID already names different frame bytes.");
    if (isTerminal(known?.state)) {
      await this.#writeTail;
      return storedReceipt(transferId, known);
    }
    const firstOffer = !known;
    let packing, payload;
    if (known) {
      packing = known.packing ?? FramePacking.Raw;
      payload = encodeFrame(raw, packing);
      if (!payload || (known.encoded_length !== undefined && known.encoded_length !== payload.length)) {
        throw new ControllerError(409, "The frame no longer matches its stored transfer encoding.");
      }
    } else {
      ({ packing, payload } = selectFrameEncoding(raw, await this.#supportsFramePacking(peer, nodeId)));
    }
    if (!this.#bdx.stage(peer.peerAddress, transferId, payload, digest, packing)) {
      throw new ControllerError(409, "The display already has frames available for transfer.");
    }
    flight.packing = packing;
    flight.encoded_length = payload.length;
    if (firstOffer) {
      known = this.#addOffer(record, { id: transferId, digest, state: "unknown", packing, encoded_length: payload.length });
      try {
        // An interrupted invoke can leave a queued download on the device.
        await this.#persist();
      } catch (error) {
        this.#bdx.release(peer.peerAddress, transferId);
        record.offers = record.offers.filter(offer => offer !== known);
        throw error;
      }
    }
    this.#ensureSubscription(peer, nodeId);
    if (known.state === "queued") return receipt(transferId, "queued");
    const fields = { transferId, sha256: Buffer.from(digest, "hex"), length: FRAME_BYTES };
    if (packing !== FramePacking.Raw) Object.assign(fields, { packing, encodedLength: payload.length });
    flight.offered = true;
    try {
      await this.#invoke(peer, FrameCluster, FRAME_ENDPOINT, "queueFrame", fields);
    } catch (error) {
      const recorded = this.#findOffer(record, transferId);
      if (isTerminal(recorded?.state)) {
        await this.#writeTail;
        return storedReceipt(transferId, recorded);
      }
      if (error instanceof StatusResponseError || (error instanceof ControllerError && error.status === 409)) {
        // A first invoke explicitly refused by the peer has no download to recover.
        if (packing !== FramePacking.Raw) this.#frameCapabilities.delete(nodeId);
        if (firstOffer) {
          this.#bdx.release(peer.peerAddress, transferId);
          record.offers = record.offers.filter(offer => offer !== known);
          await this.#persist();
        }
      }
      throw error;
    }
    const recorded = this.#findOffer(record, transferId);
    if (!isTerminal(recorded?.state)) {
      if (recorded) recorded.state = "queued";
      else this.#addOffer(record, { id: transferId, digest, state: "queued", packing, encoded_length: payload.length });
      await this.#persist();
    }
    const confirmed = this.#findOffer(record, transferId);
    if (isTerminal(confirmed?.state)) await this.#writeTail;
    return storedReceipt(transferId, confirmed ?? { state: "queued" });
  }

  #findOffer(record, transferId) {
    return record.offers.find((offer) => offer.id === transferId);
  }

  #addOffer(record, offer) {
    record.offers.push(offer);
    if (record.offers.length > MAX_RECEIPTS_PER_NODE) record.offers.shift();
    return offer;
  }

  #hasActiveOffer(nodeId, transferId) {
    return this.#inFlight.get(`${nodeId}:${transferId}`)?.offered === true;
  }

  #recordTerminal(peer, nodeId, transferId, state, battery) {
    const record = this.#record(nodeId);
    let known = this.#findOffer(record, transferId);
    if (!known) {
      const flight = this.#inFlight.get(`${nodeId}:${transferId}`);
      if (!flight?.offered) return false;
      known = this.#addOffer(record, {
        id: transferId, digest: flight.digest, state: "unknown",
        packing: flight.packing, encoded_length: flight.encoded_length,
      });
    }
    if (isTerminal(known.state) && known.state !== state) return false;
    const hasNewBattery = battery !== undefined && known.battery_percent === undefined;
    const changed = known.state !== state || hasNewBattery;
    known.state = state;
    if (hasNewBattery) known.battery_percent = battery;
    this.#bdx.release(peer.peerAddress, transferId);
    return changed;
  }

  async getCachedReceipt(nodeId, transferId) {
    this.#findPeer(nodeId);
    const record = this.#record(nodeId);
    if (!record.device_uid) throw new ControllerError(409, "This Matter node has no verified inkDaddy device UID.");
    await this.#writeTail;
    return storedReceipt(transferId, this.#findOffer(record, transferId));
  }

  async getReceipt(nodeId, transferId) {
    const peer = this.#findPeer(nodeId);
    const record = this.#record(nodeId);
    if (!record.device_uid) throw new ControllerError(409, "This Matter node has no verified inkDaddy device UID.");
    await this.#writeTail;
    const prior = this.#findOffer(record, transferId);
    if (isTerminal(prior?.state) || (!prior && !this.#hasActiveOffer(nodeId, transferId))) {
      return storedReceipt(transferId, prior);
    }
    const report = await this.#invoke(peer, FrameCluster, FRAME_ENDPOINT, "getReceipt", { transferId });
    const state = frameStates[report?.state];
    if (report?.transferId !== transferId || !state) throw new ControllerError(502, "The display returned an invalid Matter receipt.");
    if (!isTerminal(state)) {
      const known = this.#findOffer(record, transferId);
      if (isTerminal(known?.state)) return storedReceipt(transferId, known);
      if (known && known.state !== state) {
        known.state = state;
        await this.#persist();
      }
      return storedReceipt(transferId, this.#findOffer(record, transferId) ?? { state });
    }
    const halfPercent = state === "displayed" ? await this.#readAttribute(peer,
      { endpointId: 0, clusterId: 0x002f, attributeId: 0x000c }, AbortSignal.timeout(2500)).catch(() => null) : null;
    if (this.#recordTerminal(peer, nodeId, transferId, state, batteryPercent(halfPercent))) await this.#persist();
    return storedReceipt(transferId, this.#findOffer(record, transferId));
  }

  setPlaying(nodeId, playing) {
    const peer = this.#findPeer(nodeId);
    if (!this.#record(nodeId).device_uid) throw new ControllerError(409, "This Matter node has no verified inkDaddy device UID.");
    const abort = AbortSignal.timeout(PLAYBACK_TIMEOUT_MS);
    const previous = this.#playbackChanges.get(nodeId) ?? Promise.resolve();
    const change = previous.catch(() => {}).then(async () => {
      abort.throwIfAborted();
      await this.#invoke(peer, OnOff, PLAYBACK_ENDPOINT, playing ? "on" : "off", undefined, abort);
      const observed = await this.#readAttribute(peer, {
        endpointId: PLAYBACK_ENDPOINT,
        clusterId: OnOff.id,
        attributeId: OnOff.attributes.onOff.id,
      }, abort);
      if (typeof observed !== "boolean") throw new ControllerError(502, "The display did not report its playback state.");
      if (observed !== playing) throw new ControllerError(409, "The display reported a different playback state.");
      return { playing: observed };
    }).finally(() => {
      if (this.#playbackChanges.get(nodeId) === change) this.#playbackChanges.delete(nodeId);
    });
    this.#playbackChanges.set(nodeId, change);
    return change;
  }

  async actions(nodeId, after) {
    const peer = this.#findPeer(nodeId);
    const record = this.#record(nodeId);
    if (!record.device_uid) throw new ControllerError(409, "This Matter node has no verified inkDaddy device UID.");
    this.#ensureSubscription(peer, nodeId);
    await this.#writeTail;
    return { actions: (record.actions ?? []).filter(({ event_number }) => BigInt(event_number) > after).slice(0, MAX_ACTIONS_PER_REPLY) };
  }

  #ensureSubscription(peer, nodeId) {
    if (this.#subscriptions.has(nodeId)) return;
    const controller = new AbortController();
    this.#subscriptions.set(nodeId, controller);
    const done = () => {
      if (this.#subscriptions.get(nodeId) !== controller) return;
      this.#subscriptions.delete(nodeId);
      this.#frameCapabilities.delete(nodeId);
    };
    const cursor = BigInt(this.#record(nodeId).last_event_number ?? 0);
    const options = Subscribe({
      events: [
        { endpointId: FRAME_ENDPOINT, clusterId: FrameCluster.id, eventId: 0, isUrgent: true },
        { endpointId: FRAME_ENDPOINT, clusterId: FrameCluster.id, eventId: 1, isUrgent: true },
      ],
      eventFilters: [{ eventMin: cursor < 0xffff_ffff_ffff_ffffn ? cursor + 1n : cursor }],
      fabricFilter: true,
      keepSubscriptions: true,
      minIntervalFloor: Seconds(0),
      maxIntervalCeiling: Seconds(3600),
    });
    void peer.interaction.subscribe({
      ...options,
      sustain: true,
      bootstrapWithRead: false,
      abort: controller.signal,
      closed: done,
      updated: (result) => this.#recordEvents(peer, nodeId, result),
    }).catch(done);
  }

  async #recordEvents(peer, nodeId, result) {
    const record = this.#record(nodeId);
    if (!record.device_uid) return;
    const cursor = BigInt(record.last_event_number ?? 0);
    const received = new Map();
    for await (const chunk of result) {
      for await (const report of chunk) {
        if (report.kind !== "event-value" || report.path.endpointId !== FRAME_ENDPOINT ||
            report.path.clusterId !== FrameCluster.id) continue;
        const number = BigInt(report.number);
        if (number > cursor) received.set(number, report);
      }
    }
    if (!received.size) return;
    const reports = [...received].sort(([left], [right]) => left < right ? -1 : 1);
    const previousActions = record.actions ?? [];
    const lastAction = BigInt(previousActions.at(-1)?.event_number ?? 0);
    const actions = [];
    const expectedFabric = peer.clientPeerContext?.fabricIndexOnPeer;
    for (const [number, { path, value }] of reports) {
      // Unknown vendor structures are decoded with numeric context tags by Matter.js.
      if (path.eventId === 0) {
        const code = value?.action ?? value?.[0];
        if (number > lastAction && Number.isInteger(code) && playbackActions[code]) {
          actions.push({ event_number: number.toString(), action: playbackActions[code] });
        }
      } else if (path.eventId === 1) {
        const transferId = value?.transferId ?? value?.[0];
        const state = frameStates[value?.state ?? value?.[1]];
        const fabricIndex = value?.fabricIndex ?? value?.[254];
        if (typeof transferId !== "string" || !/^[a-f0-9]{24}$/.test(transferId) || !isTerminal(state) ||
            !Number.isInteger(fabricIndex) || fabricIndex !== expectedFabric) continue;
        this.#recordTerminal(peer, nodeId, transferId, state, batteryPercent(value?.battery ?? value?.[2]));
      }
    }
    if (actions.length) record.actions = [...previousActions, ...actions].slice(-MAX_ACTIONS_PER_NODE);
    record.last_event_number = reports.at(-1)[0].toString();
    await this.#persist();
  }
}

function pairingOptions(code) {
  if (!code.startsWith("MT:")) return { pairingCode: code };
  const payloads = QrPairingCodeCodec.decode(code);
  if (payloads.length !== 1) throw new Error("Expected one Matter onboarding payload.");
  const { passcode, discriminator, vendorId, productId } = payloads[0];
  return { passcode, discriminator, vendorId: vendorId || undefined, productId: productId || undefined };
}

function receipt(transfer_id, state) {
  return { transfer_id, state };
}

function storedReceipt(transferId, offer) {
  const result = receipt(transferId, offer?.state ?? "unknown");
  if (offer?.battery_percent !== undefined) result.battery_percent = offer.battery_percent;
  return result;
}

function isTerminal(state) {
  return state === "displayed" || state === "failed";
}

function batteryPercent(halfPercent) {
  return Number.isInteger(halfPercent) && halfPercent >= 0 && halfPercent <= 200 ? halfPercent / 2 : undefined;
}
