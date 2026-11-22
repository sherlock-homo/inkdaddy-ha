import { Bytes, MemoryBlobStorageDriver, Millis } from "@matter/main";
import { BdxInitMessageSchema, BdxMessenger, BdxProtocol, Flow, NodeSession, ScopedStorage } from "@matter/main/protocol";
import { BdxMessageType, BdxStatusCode } from "@matter/main/types";
import { FRAME_BYTES, FRAME_SCOPE, FramePacking, MAX_BLOCK_BYTES, PALETTE_PACKED_BYTES } from "./wire.mjs";

const MAX_STAGED_FRAMES = 4;
const FRAME_TTL_MS = 15 * 60_000;
const designator = /^inkdaddy\/([a-f0-9]{24})\.(raw|packed)$/;

// Limit the upstream generic BDX handler to receiver-driven requests for an exact,
// temporarily offered frame on this CASE peer. Other BDX traffic never gets storage.
export class FrameBdxProtocol extends BdxProtocol {
  #peers = new Map();

  async onNewExchange(exchange, message) {
    let rejection = BdxStatusCode.UnexpectedMessage;
    let allowed = false;
    if (exchange.session instanceof NodeSession && !exchange.session.isPase &&
        message.payloadHeader.messageType === BdxMessageType.ReceiveInit) {
      try {
        const request = new BdxInitMessageSchema().decode(message.payload);
        const [, transferId, suffix] = designator.exec(Bytes.toString(request.fileDesignator)) ?? [];
        const peer = this.#peers.get(exchange.session.peerAddress.toString());
        const offered = transferId && peer?.frames.get(transferId);
        allowed = offered && suffix === offered.suffix && offered.expires > Date.now() &&
            request.transferProtocol.receiverDrive && !request.transferProtocol.asynchronousTransfer &&
            Number(request.startOffset ?? 0) === 0 && Number(request.maxLength) === offered.length &&
            !request.metaData?.length;
        rejection = BdxStatusCode.FileDesignatorUnknown;
      } catch {
        rejection = BdxStatusCode.BadMessageContent;
      }
    }
    if (allowed) return super.onNewExchange(exchange, message);
    await exchange.nextMessage();
    try {
      await new BdxMessenger(exchange).sendError(rejection);
    } finally {
      await exchange.close();
    }
  }

  stage(peerAddress, transferId, payload, sha256, packing) {
    const length = payload.length;
    if (!validStagedLength(packing, length)) return false;
    const suffix = packing === FramePacking.Raw ? "raw" : "packed";
    const peerKey = peerAddress.toString();
    let peer = this.#peers.get(peerKey);
    if (!peer) {
      const driver = new MemoryBlobStorageDriver();
      const storage = new ScopedStorage(driver, ["display-frames"], FRAME_SCOPE);
      this.enablePeerForScope(peerAddress, storage, {
        maxBlockSize: MAX_BLOCK_BYTES,
        maxTransferSize: FRAME_BYTES,
        preferredDriverModes: [Flow.DriverMode.ReceiverDrive],
        messageTimeout: Millis(30_000),
      });
      peer = { driver, frames: new Map() };
      this.#peers.set(peerKey, peer);
    }
    for (const [id, frame] of peer.frames) {
      if (frame.expires <= Date.now()) this.release(peerAddress, id);
    }
    const existing = peer.frames.get(transferId);
    if (existing && (existing.sha256 !== sha256 || existing.packing !== packing || existing.length !== length)) return false;
    if (!existing && peer.frames.size >= MAX_STAGED_FRAMES) return false;
    peer.driver.setBytes(["display-frames", transferId], suffix, payload);
    peer.frames.set(transferId, { sha256, packing, length, suffix, expires: Date.now() + FRAME_TTL_MS });
    return true;
  }

  release(peerAddress, transferId) {
    const peer = this.#peers.get(peerAddress.toString());
    const frame = peer?.frames.get(transferId);
    if (!frame) return;
    peer.driver.delete(["display-frames", transferId], frame.suffix);
    peer.frames.delete(transferId);
  }
}

function validStagedLength(packing, length) {
  switch (packing) {
    case FramePacking.Raw: return length === FRAME_BYTES;
    case FramePacking.PackBits: return length > 0 && length < FRAME_BYTES;
    case FramePacking.Palette: return length === PALETTE_PACKED_BYTES;
    default: return false;
  }
}
