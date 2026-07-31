import { configFromEnv, loadDemoEnv } from "./config.js";
import { createDemoServer } from "./server.js";

loadDemoEnv();
const config = configFromEnv();
const server = createDemoServer(config);

server.listen(config.port, config.host, () => {
  console.log(
    `Realtime Demo 已启动：https://${config.host}:${config.port}`,
  );
  console.log(
    `上游：${config.baseURL} / ${config.model} / ${config.connectionMode}`,
  );
});

const shutdown = (signal: string) => {
  console.log(`收到 ${signal}，正在关闭 Realtime Demo`);
  server.close((error) => {
    if (error) {
      console.error("Realtime Demo 关闭失败", error);
      process.exitCode = 1;
    }
  });
};

process.once("SIGINT", () => shutdown("SIGINT"));
process.once("SIGTERM", () => shutdown("SIGTERM"));
