import { Logger, LogLevel } from "@matter/main";
import { chmodSync } from "node:fs";
import { resolve } from "node:path";
import { startMatterController } from "./controller.mjs";
import { createControllerServer } from "./http.mjs";

process.umask(0o077);
Logger.defaultLogLevel = LogLevel.WARN;

const token = process.env.INKDADDY_MATTER_TOKEN;
const store = process.env.INKDADDY_MATTER_STORE;
if (!token || !store) throw new Error("INKDADDY_MATTER_TOKEN and INKDADDY_MATTER_STORE are required.");
const socketPath = process.env.INKDADDY_MATTER_SOCKET;
const httpPort = socketPath ? undefined : portFromEnv("INKDADDY_MATTER_PORT", 8791);
const networkPort = portFromEnv("INKDADDY_MATTER_NETWORK_PORT", 8792);
const controller = await startMatterController({ storagePath: resolve(store), networkPort });
const server = createControllerServer(controller, token);
let closing = false;
async function close() {
  if (closing) return;
  closing = true;
  server.close();
  server.closeAllConnections();
  await controller.close();
}
process.once("SIGINT", () => void close());
process.once("SIGTERM", () => void close());
server.on("error", () => {
  console.error("inkDaddy Matter controller could not start its local API.");
  process.exitCode = 1;
  void close();
});
if (socketPath) {
  server.listen(socketPath, () => {
    try {
      chmodSync(socketPath, 0o600);
    } catch (error) {
      server.emit("error", error);
    }
  });
} else {
  server.listen(httpPort, "127.0.0.1");
}

function portFromEnv(name, defaultValue) {
  const port = Number(process.env[name] ?? defaultValue);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error(`${name} must be a TCP or UDP port number.`);
  return port;
}
