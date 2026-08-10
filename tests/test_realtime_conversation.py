from __future__ import annotations

import base64
import io
import wave

import numpy as np
import pytest

from audio_harness.serving.realtime.conversation import (
    ConversationMutationError,
    RealtimeConversation,
    decode_wire_audio,
    encode_wire_audio,
)


def test_conversation_preserves_order_and_supports_crud() -> None:
    conversation = RealtimeConversation(max_items=8)
    first, previous = conversation.create(
        {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": "system"}],
        }
    )
    second, second_previous = conversation.create(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "question"}],
        }
    )
    inserted, inserted_previous = conversation.create(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "answer"}],
        },
        previous_item_id=first["id"],
    )

    assert previous is None
    assert second_previous == first["id"]
    assert inserted_previous == first["id"]
    assert conversation.retrieve(inserted["id"])["status"] == "completed"
    assert [item["role"] for item in conversation.responses_input()] == [
        "system",
        "assistant",
        "user",
    ]

    conversation.delete(inserted["id"])
    with pytest.raises(ConversationMutationError) as captured:
        conversation.retrieve(inserted["id"])
    assert captured.value.code == "item_not_found"


def test_conversation_projects_text_image_audio_and_tool_items() -> None:
    pcm = b"\0\0" * 240
    conversation = RealtimeConversation(max_items=8)
    conversation.create(
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look"},
                {
                    "type": "input_image",
                    "detail": "low",
                    "image_url": "data:image/png;base64,AA==",
                },
                {
                    "type": "input_audio",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                },
            ],
        }
    )
    conversation.create(
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "ok",
        }
    )

    projected = conversation.responses_input()
    assert [part["type"] for part in projected[0]["content"]] == [
        "input_text",
        "input_image",
        "input_audio",
    ]
    wav_data = base64.b64decode(
        projected[0]["content"][2]["input_audio"]["data"]
    )
    with wave.open(io.BytesIO(wav_data), "rb") as audio:
        assert audio.getframerate() == 24_000
        assert audio.getnchannels() == 1
        assert audio.readframes(audio.getnframes()) == pcm
    assert projected[1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "ok",
    }


def test_conversation_truncates_assistant_audio_and_removes_transcript() -> None:
    conversation = RealtimeConversation(max_items=4)
    item, _ = conversation.create(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_audio",
                    "audio": "AA==",
                    "transcript": "unheard",
                }
            ],
        }
    )

    truncated = conversation.truncate(
        item["id"],
        content_index=0,
        audio_end_ms=0,
    )

    assert truncated["content"] == [{"type": "output_audio"}]


@pytest.mark.parametrize("format_type", ["audio/pcmu", "audio/pcma"])
def test_companded_wire_audio_decodes_to_pcm(format_type: str) -> None:
    pcm = b"\0\0" * 160
    encoded = encode_wire_audio(pcm, format_type=format_type)

    assert len(encoded) == len(pcm) // 2
    decoded = np.frombuffer(
        decode_wire_audio(encoded, format_type=format_type),
        dtype="<i2",
    )
    assert np.max(np.abs(decoded)) <= 8
