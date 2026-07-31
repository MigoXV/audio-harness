# OpenAI Realtime WebRTC 极简 Demo

这个 Demo 只做一件事：浏览器采集麦克风，通过 WebRTC RTP 音轨送到
Audio Harness，并在话音结束后显示 ASR 转写。`oai-events` DataChannel
承载兼容 OpenAI Realtime 的事件；服务端同时协商一条静音输出轨道，为以后
接入 TTS 保留方向。

API Key 只由 Vite 开发服务器读取，不会下发到浏览器。默认会复用项目根目录
`.env` 中的 `AUDIO_HARNESS_REALTIME_API_KEY`；也可以在 Demo 自己的 `.env`
中用 `OPENAI_API_KEY` 覆盖。启动：

```bash
cd examples/openai_webrtc_demo
cp .env.example .env
pnpm install
pnpm dev
```

Vite 默认监听 `0.0.0.0:5175`。打开 `https://127.0.0.1:5175`，点击
“连接麦克风”并允许浏览器使用麦克风。
本地服务默认使用 `https://127.0.0.1:8000/v1`。自签名证书由
`OPENAI_CA_CERT_FILE` 交给 Demo 后端校验，不需要关闭 TLS 验证。

Vite 页面本身也默认使用 `model-bin/realtime-tls/cert.pem` 与 `key.pem`。
从另一台 PC 访问时，需要先把 `cert.pem` 安装为受信任证书，再通过
`https://127.0.0.1:5175` 打开页面。

局域网其他设备访问时，浏览器麦克风要求 HTTPS 安全上下文。需要给 Demo
本身配置 HTTPS，且 Audio Harness 通常还需要在
`AUDIO_HARNESS_WEBRTC_ICE_SERVERS_JSON` 配置可达的 STUN/TURN；同机测试
可以保持空数组。Demo 默认把这份 ICE 配置提供给浏览器，也可以通过
`WEBRTC_DEMO_ICE_SERVERS_JSON` 单独覆盖。

`ssh -L` 只转发 TCP，不能转发 WebRTC 的 ICE/RTP UDP。通过 SSH 打开页面时，
PC 与服务器还必须有可直达的局域网/VPN 路由，或者双方配置可达的 TURN。
