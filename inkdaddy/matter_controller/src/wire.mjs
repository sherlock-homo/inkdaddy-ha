import {
  Attribute, ClusterType, Command, Event, Priority, TlvByteString, TlvFabricIndex, TlvField, TlvObject,
  TlvOptionalField, TlvString,
  TlvUInt32, TlvUInt8, TlvVoid,
} from "@matter/main/types";
import { AttributeModel } from "@matter/main/model";

export const FRAME_BYTES = 192_000;
export const FRAME_PACKING_REVISION = 3;
export const PALETTE_PACKED_BYTES = 128_000;
export const FramePacking = Object.freeze({ Raw: 0, PackBits: 1, Palette: 2 });
export const MAX_BLOCK_BYTES = 1024;
export const FRAME_SCOPE = "inkdaddy";
export const FRAME_ENDPOINT = 1;
export const frameStates = ["unknown", "queued", "displayed", "failed"];
export const playbackActions = ["next_content", "previous_content", "play", "pause", "refresh_current"];
export const nodeUidPattern = /^[a-z0-9][a-z0-9-]{0,63}$/;

const frameSchema = ClusterType({
  id: 0xfff1fc00,
  name: "InkDaddyFrameTransfer",
  revision: FRAME_PACKING_REVISION,
  attributes: { nodeUid: Attribute(0, TlvString.bound({ maxLength: 64 })) },
  events: {
    playbackRequested: Event(0, Priority.Info, TlvObject({ action: TlvField(0, TlvUInt8) })),
    displayReceipt: Event(1, Priority.Info, TlvObject({
      transferId: TlvField(0, TlvString.bound({ minLength: 24, maxLength: 24 })),
      state: TlvField(1, TlvUInt8),
      battery: TlvOptionalField(2, TlvUInt8.bound({ max: 200 })),
      fabricIndex: TlvField(254, TlvFabricIndex),
    })),
  },
  commands: {
    queueFrame: Command(0, TlvObject({
      transferId: TlvField(0, TlvString.bound({ minLength: 24, maxLength: 24 })),
      sha256: TlvField(1, TlvByteString.bound({ minLength: 32, maxLength: 32 })),
      length: TlvField(2, TlvUInt32),
      packing: TlvOptionalField(3, TlvUInt8.bound({ min: FramePacking.PackBits, max: FramePacking.Palette })),
      encodedLength: TlvOptionalField(4, TlvUInt32.bound({ min: 1, max: FRAME_BYTES - 1 })),
    }), 0, TlvVoid),
    getReceipt: Command(1, TlvObject({
      transferId: TlvField(0, TlvString.bound({ minLength: 24, maxLength: 24 })),
    }), 2, TlvObject({
      transferId: TlvField(0, TlvString.bound({ minLength: 24, maxLength: 24 })),
      state: TlvField(1, TlvUInt8),
    })),
  },
}).schema.clone();

// Matter.js server behaviors only construct emitters for events marked operationally supported.
frameSchema.children.push(new AttributeModel({ id: 0xfffd, name: "ClusterRevision", type: "uint16", default: FRAME_PACKING_REVISION }));
frameSchema.events(0).conformance = "M";
frameSchema.events(1).conformance = "M";
frameSchema.events(1).access = "S V";

export const FrameCluster = ClusterType(frameSchema);
