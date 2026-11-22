import { strict as assert } from "node:assert";
import test from "node:test";
import { InkDaddyController } from "../src/controller.mjs";

const expectedUid = "xiao-mg24-abcdef01";
const otherUid = "xiao-mg24-abcdef02";

async function fakeController({ value, existing, readError = false, removalError = false }) {
  let removed = 0;
  const peerAddress = { nodeId: 41n };
  const peer = {
    peerAddress,
    interaction: {
      async *read() {
        if (readError) throw new Error("unavailable demo peer");
        yield [{ kind: "attr-value", value }];
      },
    },
    async decommission() {
      removed++;
      if (removalError) throw new Error("unavailable demo peer");
    },
  };
  const node = {
    peers: {
      commissioned: existing ? [{ peerAddress }] : [],
      async commission() { return peer; },
    },
  };
  const storage = {
    async get() {
      return existing ? { 41: { device_uid: expectedUid, offers: [] } } : {};
    },
    async set() {},
  };
  const controller = await InkDaddyController.open(node, null, { createContext: () => storage });
  return { controller, removals: () => removed };
}

test("native commissioning removes only a newly created peer with an unverified identity", async () => {
  for (const value of [otherUid, null]) {
    const { controller, removals } = await fakeController({ value });
    await assert.rejects(controller.commission("demo", expectedUid), { status: 409 });
    assert.equal(removals(), 1);
  }
});

test("native commissioning requires a fresh identity and preserves an existing peer for the same node ID", async () => {
  const { controller, removals } = await fakeController({ existing: true, readError: true });
  await assert.rejects(controller.commission("demo", expectedUid), { status: 409 });
  assert.equal(removals(), 0);
});

test("native commissioning retains a correctly identified peer", async () => {
  const { controller, removals } = await fakeController({ value: expectedUid });
  assert.deepEqual(await controller.commission("demo", expectedUid), { node_id: "41", device_uid: expectedUid });
  assert.equal(removals(), 0);
});

test("manual sharing preserves an unidentified peer for a late identity read", async () => {
  const { controller, removals } = await fakeController({ readError: true });
  assert.deepEqual(await controller.commission("demo"), { node_id: "41", device_uid: null });
  assert.equal(removals(), 0);
});

test("native commissioning reports that a rejected unreachable peer could not be removed", async () => {
  const { controller, removals } = await fakeController({ value: otherUid, removalError: true });
  await assert.rejects(controller.commission("demo", expectedUid), { status: 409, message: /could not be removed/ });
  assert.equal(removals(), 1);
});
