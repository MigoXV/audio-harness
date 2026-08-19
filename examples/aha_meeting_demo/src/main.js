import { configFromEnv, loadDemoEnv } from "./config.js";
import { realtimeURL } from "./realtime.js";
import { createMeetingDemoServer } from "./server.js";

loadDemoEnv();
const config = configFromEnv();
const server = createMeetingDemoServer(config);
server.listen(config.port, config.host, () => {
  console.log(`AHA 会议转写 Demo 已启动：https://${config.host}:${config.port}`);
  console.log(`上游 Route：${config.model}`);
  console.log(`上游 WebSocket：${realtimeURL(config)}`);
});

function shutdown(signal) {
  console.log(`收到 ${signal}，正在关闭 Demo`);
  server.close((error) => { if (error) process.exitCode = 1; });
}
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
