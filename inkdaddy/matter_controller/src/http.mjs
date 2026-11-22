import { createHash, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import { NodeId } from "@matter/main/types";
import { ControllerError } from "./controller.mjs";
import { FRAME_BYTES } from "./wire.mjs";

const FRAME_ROUTE = /^\/v1\/nodes\/([1-9][0-9]{0,19})\/frames\/([a-f0-9]{24})$/;
const ACTIONS_ROUTE = /^\/v1\/nodes\/([1-9][0-9]{0,19})\/actions$/;
const PLAYBACK_ROUTE = /^\/v1\/nodes\/([1-9][0-9]{0,19})\/playback$/;

export function createControllerServer(controller, token) {
  if (typeof token !== "string" || !token.length) throw new Error("INKDADDY_MATTER_TOKEN is required.");
  const bearer = Buffer.from(`Bearer ${token}`);
  return createServer({ requestTimeout: 45_000 }, async (request, response) => {
    try {
      const authorization = Buffer.from(request.headers.authorization ?? "");
      if (authorization.length !== bearer.length || !timingSafeEqual(authorization, bearer)) {
        throw new ControllerError(401, "Authorization required.");
      }
      const url = new URL(request.url, "http://127.0.0.1");
      const result = await route(controller, request, url);
      send(response, result.status, result.body);
    } catch (error) {
      request.resume();
      if (error instanceof ControllerError) send(response, error.status, { detail: error.message });
      else send(response, 502, { detail: "The Matter device could not complete this request." });
    }
  });
}

async function route(controller, request, url) {
  const { pathname } = url;
  if (request.method === "GET" && pathname === "/v1/health") return { status: 200, body: { ok: true } };
  if (request.method === "GET" && pathname === "/v1/nodes") {
    return { status: 200, body: await controller.nodes(url.searchParams.get("refresh") === "1") };
  }
  if (request.method === "POST" && pathname === "/v1/commission") {
    const input = await readBody(request, 2048);
    let code;
    let expectedUid;
    try {
      const payload = JSON.parse(input.toString("utf8"));
      code = normalizedShareCode(payload?.code);
      expectedUid = payload?.expected_uid;
    } catch {
      throw new ControllerError(422, "Enter a Matter share code or QR payload.");
    }
    if (expectedUid !== undefined && (typeof expectedUid !== "string" || !/^xiao-mg24-[0-9a-f]{8}$/.test(expectedUid))) {
      throw new ControllerError(422, "Select an inkDaddy display identity.");
    }
    return { status: 200, body: await controller.commission(code, expectedUid) };
  }
  const actions = ACTIONS_ROUTE.exec(pathname);
  if (actions && request.method === "GET") {
    validateNodeId(actions[1]);
    const after = url.searchParams.get("after") ?? "0";
    if (!/^(?:0|[1-9][0-9]{0,19})$/.test(after) || BigInt(after) > 0xffff_ffff_ffff_ffffn) {
      throw new ControllerError(422, "Invalid Matter event number.");
    }
    return { status: 200, body: await controller.actions(actions[1], BigInt(after)) };
  }
  const playback = PLAYBACK_ROUTE.exec(pathname);
  if (playback) {
    if (request.method !== "POST") throw new ControllerError(405, "Unsupported method.");
    validateNodeId(playback[1]);
    const input = await readBody(request, 256);
    let payload;
    try {
      payload = JSON.parse(input.toString("utf8"));
    } catch {
      throw new ControllerError(422, "Set playback with a boolean playing value.");
    }
    if (typeof payload?.playing !== "boolean") throw new ControllerError(422, "Set playback with a boolean playing value.");
    return { status: 200, body: await controller.setPlaying(playback[1], payload.playing) };
  }
  const match = FRAME_ROUTE.exec(pathname);
  if (!match) throw new ControllerError(404, "No such Matter controller route.");
  const [, nodeId, transferId] = match;
  validateNodeId(nodeId);
  if (request.method === "GET") {
    const body = url.searchParams.get("cached") === "1"
      ? await controller.getCachedReceipt(nodeId, transferId)
      : await controller.getReceipt(nodeId, transferId);
    return { status: 200, body };
  }
  if (request.method !== "POST") throw new ControllerError(405, "Unsupported method.");
  if (request.headers["content-type"] !== "application/octet-stream") {
    throw new ControllerError(415, "Send packed frame bytes as application/octet-stream.");
  }
  const digest = request.headers["x-frame-sha256"];
  if (typeof digest !== "string" || !/^[a-f0-9]{64}$/.test(digest)) throw new ControllerError(422, "X-Frame-SHA256 is required.");
  const raw = await readBody(request, FRAME_BYTES);
  if (raw.length !== FRAME_BYTES || createHash("sha256").update(raw).digest("hex") !== digest) {
    throw new ControllerError(422, "Frame length or SHA-256 does not match.");
  }
  return { status: 202, body: await controller.queueFrame(nodeId, transferId, raw, digest) };
}

function validateNodeId(nodeId) {
  if (!NodeId.isOperationalNodeId(BigInt(nodeId))) throw new ControllerError(422, "Invalid Matter node ID.");
}

function normalizedShareCode(code) {
  if (typeof code !== "string") throw new Error("Invalid share code.");
  const cleaned = code.trim();
  if (/^MT:[0-9A-Z.-]{10,512}$/.test(cleaned)) return cleaned;
  const manual = cleaned.replace(/[\s-]/g, "");
  if (/^(?:\d{11}|\d{21})$/.test(manual)) return manual;
  throw new Error("Invalid share code.");
}

async function readBody(request, limit) {
  if (Number(request.headers["content-length"]) > limit) throw new ControllerError(413, "Request body too large.");
  let length = 0;
  const chunks = [];
  for await (const chunk of request.iterator({ destroyOnReturn: false })) {
    length += chunk.length;
    if (length > limit) throw new ControllerError(413, "Request body too large.");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, length);
}

function send(response, status, body) {
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify(body));
}
