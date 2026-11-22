import { FRAME_BYTES, FramePacking, PALETTE_PACKED_BYTES } from "./wire.mjs";

// PackBits packets have at most 128 literals or 128 copies of one byte.
function encodePackBits(raw) {
  const output = Buffer.allocUnsafe(raw.length);
  let input = 0;
  let written = 0;
  while (input < raw.length) {
    let run = 1;
    while (run < 128 && input + run < raw.length && raw[input + run] === raw[input]) run++;
    if (run >= 3) {
      if (written + 2 >= raw.length) return undefined;
      output[written++] = 257 - run;
      output[written++] = raw[input];
      input += run;
      continue;
    }
    const start = input++;
    while (input - start < 128 && input < raw.length) {
      if (input + 2 < raw.length && raw[input] === raw[input + 1] && raw[input] === raw[input + 2]) break;
      input++;
    }
    const count = input - start;
    if (written + 1 + count >= raw.length) return undefined;
    output[written++] = count - 1;
    output.set(raw.subarray(start, input), written);
    written += count;
  }
  return output.subarray(0, written);
}

const paletteDigits = [0, 1, 2, 3, -1, 4, 5, -1, -1, -1, -1, -1, -1, -1, -1, -1];

function encodePalette(raw) {
  const output = Buffer.allocUnsafe(PALETTE_PACKED_BYTES);
  for (let index = 0, offset = 0; index < raw.length; index += 3, offset += 2) {
    let word = 0;
    for (let i = index; i < index + 3; i++) {
      const high = paletteDigits[raw[i] >> 4];
      const low = paletteDigits[raw[i] & 0xf];
      if (high < 0 || low < 0) return undefined;
      word = word * 36 + high * 6 + low;
    }
    output.writeUInt16BE(word, offset);
  }
  return output;
}

export function encodeFrame(raw, packing) {
  if (raw.length !== FRAME_BYTES) return undefined;
  switch (packing) {
    case FramePacking.Raw: return raw;
    case FramePacking.PackBits: return encodePackBits(raw);
    case FramePacking.Palette: return encodePalette(raw);
    default: return undefined;
  }
}

export function selectFrameEncoding(raw, supportsPacking) {
  if (!supportsPacking) return { packing: FramePacking.Raw, payload: raw };
  const packBits = encodeFrame(raw, FramePacking.PackBits);
  if (!packBits || packBits.length > PALETTE_PACKED_BYTES) {
    const palette = encodeFrame(raw, FramePacking.Palette);
    if (palette) return { packing: FramePacking.Palette, payload: palette };
  }
  return packBits
    ? { packing: FramePacking.PackBits, payload: packBits }
    : { packing: FramePacking.Raw, payload: raw };
}
