import { strict as assert } from "node:assert";
import { createSocket } from "node:dgram";
import { once } from "node:events";
import { Millis } from "@matter/main";
import { BdxMessenger, ExchangeManager } from "@matter/main/protocol";
import { BDX_PROTOCOL_ID, BdxMessageType } from "@matter/main/types";
import { FRAME_BYTES } from "../src/wire.mjs";

export async function pullFrame(display, session, id, { length = FRAME_BYTES, suffix = "raw" } = {}) {
  const exchange = display.env.get(ExchangeManager).initiateExchangeForSession(session, BDX_PROTOCOL_ID);
  const bdx = new BdxMessenger(exchange, Millis(8_000));
  try {
    const accepted = await bdx.sendReceiveInit({
      transferProtocol: { version: 0, receiverDrive: true },
      maxBlockSize: 1024,
      maxLength: length,
      fileDesignator: Buffer.from(`inkdaddy/${id}.${suffix}`),
    });
    assert.equal(Number(accepted.length), length);
    const blocks = [];
    for (let blockCounter = 0; blockCounter < length; blockCounter++) {
      await bdx.sendBlockQuery({ blockCounter });
      const { kind, message } = await bdx.readBlock();
      assert.equal(message.blockCounter, blockCounter);
      blocks.push(Buffer.from(message.data));
      if (kind === BdxMessageType.BlockEof) {
        const result = Buffer.concat(blocks);
        assert.equal(result.length, length);
        await bdx.sendBlockAckEof({ blockCounter });
        return result;
      }
    }
    throw new Error("BDX did not send EOF.");
  } finally {
    await exchange.close();
  }
}

export async function localNetwork() {
  const socket = createSocket("udp6");
  socket.bind(0, "::1");
  await once(socket, "listening");
  const { port } = socket.address();
  await new Promise((resolve) => socket.close(resolve));
  return {
    port,
    listeningAddressIpv4: "127.0.0.1",
    listeningAddressIpv6: process.platform === "darwin" ? "fe80::1%lo0" : "::1",
    tcp: false,
    autoStartCommissionedPeers: false,
  };
}
