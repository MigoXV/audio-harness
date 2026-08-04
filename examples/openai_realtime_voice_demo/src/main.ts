import { configFromEnv, loadVoiceDemoEnv } from "./config.js";
import { createVoiceDemoServer } from "./server.js";

loadVoiceDemoEnv();
const config = configFromEnv();
const server = createVoiceDemoServer(config);

server.listen(config.port, config.host, () => {
  console.log(
    `Realtime 语音对话 Demo 已启动：https://${config.host}:${config.port}`,
  );
  console.log(`上游模型：${config.model}，音色：${config.voice}`);
});

function shutdown(signal: string): void {
  console.log(`收到 ${signal}，正在关闭 Demo`);
  server.close((error) => {
    if (error) {
      console.error("Demo 关闭失败", error);
      process.exitCode = 1;
    }
  });
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
