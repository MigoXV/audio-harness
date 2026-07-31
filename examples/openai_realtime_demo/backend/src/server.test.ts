import { execFile } from "node:child_process";
import { once } from "node:events";
import { get } from "node:https";
import {
  mkdtemp,
  mkdir,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { promisify } from "node:util";

import { afterEach, describe, expect, it } from "vitest";
import WebSocket from "ws";

import type { DemoConfig } from "./config.js";
import { createDemoServer } from "./server.js";

const temporaryPaths: string[] = [];
const execFileAsync = promisify(execFile);

async function generateTls(root: string): Promise<{
  cert: string;
  key: string;
}> {
  const cert = join(root, "cert.pem");
  const key = join(root, "key.pem");
  await execFileAsync("openssl", [
    "req",
    "-x509",
    "-newkey",
    "rsa:2048",
    "-nodes",
    "-days",
    "1",
    "-keyout",
    key,
    "-out",
    cert,
    "-subj",
    "/CN=localhost",
    "-addext",
    "subjectAltName=DNS:localhost,IP:127.0.0.1",
  ]);
  return { cert, key };
}

async function httpsGet(
  url: string,
  ca: Buffer,
): Promise<{ status: number; body: Buffer }> {
  return new Promise((resolve, reject) => {
    get(url, { ca }, (response) => {
      const chunks: Buffer[] = [];
      response.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
      response.on("end", () => {
        resolve({
          status: response.statusCode || 0,
          body: Buffer.concat(chunks),
        });
      });
    }).on("error", reject);
  });
}

function silentWav(seconds: number, sampleRate = 16_000): Buffer {
  const dataSize = seconds * sampleRate * 2;
  const wav = Buffer.alloc(44 + dataSize);
  wav.write("RIFF", 0, "ascii");
  wav.writeUInt32LE(wav.length - 8, 4);
  wav.write("WAVE", 8, "ascii");
  wav.write("fmt ", 12, "ascii");
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(sampleRate, 24);
  wav.writeUInt32LE(sampleRate * 2, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write("data", 36, "ascii");
  wav.writeUInt32LE(dataSize, 40);
  return wav;
}

afterEach(async () => {
  await Promise.all(
    temporaryPaths.splice(0).map((path) =>
      rm(path, { recursive: true, force: true }),
    ),
  );
});

describe("Demo HTTP server", () => {
  it("serves safe config, UI, and a 30 second sample", async () => {
    const root = await mkdtemp(join(tmpdir(), "realtime-demo-"));
    temporaryPaths.push(root);
    const webDist = join(root, "web");
    const sampleFile = join(root, "sample.wav");
    const tls = await generateTls(root);
    await mkdir(webDist);
    await writeFile(join(webDist, "index.html"), "<h1>Demo UI</h1>");
    await writeFile(sampleFile, silentWav(35));
    const config: DemoConfig = {
      apiKey: "test-secret",
      baseURL: "https://api.openai.com/v1",
      model: "gpt-4o-transcribe",
      connectionMode: "intent",
      tlsCertFile: tls.cert,
      tlsKeyFile: tls.key,
      host: "127.0.0.1",
      port: 8090,
      sampleFile,
      webDist,
    };
    const server = createDemoServer(config);
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address() as AddressInfo;
    const origin = `https://127.0.0.1:${address.port}`;
    const ca = await readFile(tls.cert);

    try {
      const publicResponse = await httpsGet(`${origin}/api/config`, ca);
      const publicBody = publicResponse.body.toString("utf8");
      expect(publicResponse.status).toBe(200);
      expect(JSON.parse(publicBody)).toEqual({
        base_url: "https://api.openai.com/v1",
        model: "gpt-4o-transcribe",
        connection_mode: "intent",
      });
      expect(publicBody).not.toContain("test-secret");

      const indexResponse = await httpsGet(origin, ca);
      expect(indexResponse.body.toString("utf8")).toContain("Demo UI");

      const sampleResponse = await httpsGet(`${origin}/api/sample`, ca);
      const sample = sampleResponse.body;
      expect(sampleResponse.status).toBe(200);
      expect(sample.readUInt32LE(40)).toBe(30 * 16_000 * 2);
    } finally {
      server.close();
      await once(server, "close");
    }
  });

  it("returns a typed error instead of crashing on a missing CA", async () => {
    const root = await mkdtemp(join(tmpdir(), "realtime-demo-ca-"));
    temporaryPaths.push(root);
    const tls = await generateTls(root);
    const config: DemoConfig = {
      apiKey: "test-secret",
      baseURL: "https://127.0.0.1:8000/v1",
      model: "gpt-4o-transcribe",
      connectionMode: "model",
      caCertFile: join(root, "missing.pem"),
      tlsCertFile: tls.cert,
      tlsKeyFile: tls.key,
      host: "127.0.0.1",
      port: 8090,
      sampleFile: join(root, "sample.wav"),
      webDist: join(root, "web"),
    };
    const server = createDemoServer(config);
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address() as AddressInfo;
    const socket = new WebSocket(
      `wss://127.0.0.1:${address.port}/ws`,
      { ca: await readFile(tls.cert) },
    );

    try {
      const [data] = (await once(socket, "message")) as [Buffer];
      const event = JSON.parse(data.toString()) as {
        type: string;
        error: { code: string };
      };
      expect(event.type).toBe("error");
      expect(event.error.code).toBe("ca_cert_read_failed");
    } finally {
      socket.close();
      server.close();
      await once(server, "close");
    }
  });
});
