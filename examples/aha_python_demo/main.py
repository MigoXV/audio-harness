"""内存文本发起一轮语音响应，不读写音频文件。"""

import asyncio
import os

from aha import AsyncAudioHarness


async def main() -> None:
    async with AsyncAudioHarness(
        base_url=os.environ["AHA_BASE_URL"], api_key=os.getenv("AHA_API_KEY")
    ) as client:
        async with client.realtime.connect(model=os.environ["AHA_MODEL"]) as session:
            await session.conversation.item.create(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "你好，请介绍一下你自己。"}
                    ],
                }
            )
            await session.response.create()
            async for event in session.events():
                if event.type == "response.output_text.delta":
                    print(event.delta, end="", flush=True)
                elif event.type == "response.output_audio.delta":
                    # event.delta 为 PCM bytes，交给应用自己的播放器。
                    print(f"收到音频: {len(event.delta)} bytes")
                elif event.type == "response.done":
                    print(f"\n响应状态: {event.response.status}")
                    break
                elif event.type == "error":
                    print(f"服务端错误: {event.error.code}")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
