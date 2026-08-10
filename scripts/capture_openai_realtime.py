#!/usr/bin/env python3
"""抓取并归一化 OpenAI Realtime WebSocket 协议事件。

原始事件可能包含音频、文本和服务端生成的标识，因此只允许写入仓库外目录，
或仓库内已被 Git 忽略的目录。提交到测试目录的 JSON 会稳定替换这些易变内容。

合成语音依赖带 ``flite`` 滤镜的 ffmpeg。可用下面的命令检查：

    ffmpeg -hide_banner -filters | grep flite

如果系统包没有该滤镜，请安装包含 libflite 支持的 ffmpeg 后再运行；抓取器不会
回退到用户录音，以免误把私人音频写入抓取文件。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal, cast

from openai import AsyncOpenAI
from openai.types.realtime import RealtimeServerEvent
from pydantic import TypeAdapter, ValidationError

CaptureKind = Literal["transcription", "realtime"]
Direction = Literal["client", "server"]
JsonObject = dict[str, Any]

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_TRANSCRIPTION_MODEL = "gpt-live-transcribe"
DEFAULT_REALTIME_MODEL = "gpt-realtime"
SYNTHETIC_SPEECH_TEXT = "Hello from Audio Harness."
SYNTHETIC_SAMPLE_RATE = 24_000

_API_KEY_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*(.*?)\s*$"
)
_SAFE_API_KEY = re.compile(r"^[A-Za-z0-9_.-]{16,}$")
_SERVER_EVENT_ADAPTER = TypeAdapter(RealtimeServerEvent)
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|secret)", re.IGNORECASE
)
_TEXT_KEYS = frozenset(
    {
        "arguments",
        "instructions",
        "message",
        "output",
        "text",
        "transcript",
    }
)
_VOLATILE_INTEGER_KEYS = frozenset(
    {
        "audio_tokens",
        "cached_tokens",
        "expires_at",
        "image_tokens",
        "input_tokens",
        "limit",
        "output_tokens",
        "remaining",
        "text_tokens",
        "total_tokens",
    }
)


class CaptureError(RuntimeError):
    """抓取无法安全或完整完成。"""


@dataclass(frozen=True)
class LoadedApiKey:
    """API 密钥及其来源；repr 有意隐藏密钥值。"""

    value: str = field(repr=False)
    source: str


@dataclass(frozen=True)
class TimelineEntry:
    direction: Direction
    payload: JsonObject


class RawCaptureWriter:
    """以 JSONL 保存未经归一化的语义帧，不保存认证头。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        self._stream = os.fdopen(descriptor, "w", encoding="utf-8")

    def record(self, entry: TimelineEntry) -> None:
        envelope = {
            "direction": entry.direction,
            "payload": entry.payload,
        }
        json.dump(
            envelope,
            self._stream,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._stream.write("\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> RawCaptureWriter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _parse_static_assignment(path: Path) -> str | None:
    """静态读取最后一条安全赋值；绝不执行配置文件。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError, UnicodeError, OSError):
        return None

    candidate: str | None = None
    for line in lines:
        match = _API_KEY_ASSIGNMENT.match(line)
        if match is None:
            continue
        candidate = _safe_assignment_value(match.group(1))
    return candidate


def _safe_assignment_value(raw_value: str) -> str | None:
    value = raw_value.strip()
    if not value:
        return None

    if value[0] in {"'", '"'}:
        quote = value[0]
        closing = value.find(quote, 1)
        if closing < 0:
            return None
        suffix = value[closing + 1 :].strip()
        if suffix and not suffix.startswith("#"):
            return None
        value = value[1:closing]
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()

    if not _SAFE_API_KEY.fullmatch(value):
        return None
    return value


def load_openai_api_key(
    *,
    repo_root: Path,
    environ: Mapping[str, str] | None = None,
) -> LoadedApiKey:
    """按环境变量、项目 .env 的顺序安全加载密钥。"""

    selected_environ = os.environ if environ is None else environ
    environment_value = selected_environ.get("OPENAI_API_KEY", "").strip()
    if _SAFE_API_KEY.fullmatch(environment_value):
        return LoadedApiKey(value=environment_value, source="process environment")

    dotenv_value = _parse_static_assignment(repo_root / ".env")
    if dotenv_value is not None:
        return LoadedApiKey(value=dotenv_value, source="project .env")

    raise CaptureError(
        "未找到安全的 OPENAI_API_KEY；请设置进程环境变量或项目 .env"
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _git_ignores(path: Path, repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", str(path)],
        cwd=repo_root,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def prepare_raw_directory(path: Path | None, *, repo_root: Path) -> Path:
    """创建原始抓取目录，并拒绝仓库内未被忽略的位置。"""

    resolved_repo = repo_root.resolve()
    if path is None:
        resolved = Path(
            tempfile.mkdtemp(prefix="audio-harness-realtime-capture-")
        ).resolve()
        if _is_relative_to(resolved, resolved_repo) and not _git_ignores(
            resolved, resolved_repo
        ):
            resolved.rmdir()
            raise CaptureError(
                "系统临时目录位于仓库内且未被 Git 忽略；请显式指定安全的 --raw-dir"
            )
        return resolved

    resolved = path.expanduser().resolve()
    if _is_relative_to(resolved, resolved_repo) and not _git_ignores(
        resolved, resolved_repo
    ):
        raise CaptureError(
            "原始抓取目录位于仓库内且未被 Git 忽略；请改用 /tmp 或 model-bin"
        )
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    return resolved


def generate_synthetic_speech_pcm() -> bytes:
    """用 ffmpeg/flite 生成确定性的 24 kHz 单声道 PCM16 测试语音。"""

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise CaptureError(
            "未找到 ffmpeg；请安装带 flite 滤镜的 ffmpeg 后重试"
        )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"flite=text='{SYNTHETIC_SPEECH_TEXT}':voice=slt",
        "-af",
        "apad=pad_dur=0.25",
        "-ar",
        str(SYNTHETIC_SAMPLE_RATE),
        "-ac",
        "1",
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise CaptureError(
            "ffmpeg 无法使用 flite 生成测试语音；请检查 flite 滤镜是否可用"
        ) from exc
    if len(result.stdout) < SYNTHETIC_SAMPLE_RATE // 5:
        raise CaptureError("ffmpeg 生成的测试语音为空或过短")
    return result.stdout


class CaptureNormalizer:
    """按首次出现顺序稳定替换标识、音频、图像和自然语言文本。"""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}
        self._audio: dict[str, str] = {}
        self._images: dict[str, str] = {}
        self._opaque: dict[str, str] = {}
        self._text: dict[str, str] = {}
        self._id_counts: dict[str, int] = {}

    def normalize(
        self,
        value: Any,
        *,
        event_type: str = "",
        path: tuple[str, ...] = (),
    ) -> Any:
        if isinstance(value, dict):
            local_event_type = cast(str, value.get("type", event_type))
            return {
                key: self._normalize_field(
                    key,
                    child,
                    event_type=local_event_type,
                    path=(*path, key),
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                self.normalize(child, event_type=event_type, path=(*path, "[]"))
                for child in value
            ]
        return value

    def _normalize_field(
        self,
        key: str,
        value: Any,
        *,
        event_type: str,
        path: tuple[str, ...],
    ) -> Any:
        if _SENSITIVE_KEY.search(key):
            return "<redacted>"
        if (
            isinstance(value, str)
            and (key == "id" or key.endswith("_id"))
            and value not in {
                "auto",
                "none",
                "root",
            }
        ):
            return self._normalize_id(value, key=key, path=path)
        if key in _VOLATILE_INTEGER_KEYS and isinstance(value, int):
            return 0
        if key == "reset_seconds" and isinstance(value, (int, float)):
            return 0.0
        if key == "obfuscation" and isinstance(value, str):
            return self._placeholder(self._opaque, value, "opaque")
        if isinstance(value, str) and self._is_audio(key, event_type):
            return self._placeholder(self._audio, value, "audio")
        if isinstance(value, str) and key == "image_url":
            return self._placeholder(self._images, value, "image")
        if isinstance(value, str) and (
            key in _TEXT_KEYS or (key == "delta" and ".audio." not in event_type)
        ):
            return self._placeholder(self._text, value, "text")
        return self.normalize(value, event_type=event_type, path=path)

    @staticmethod
    def _is_audio(key: str, event_type: str) -> bool:
        if key == "audio":
            return True
        return key == "delta" and ".audio." in event_type

    def _normalize_id(self, value: str, *, key: str, path: tuple[str, ...]) -> str:
        existing = self._ids.get(value)
        if existing is not None:
            return existing

        kind = self._id_kind(value, key=key, path=path)
        number = self._id_counts.get(kind, 0) + 1
        self._id_counts[kind] = number
        placeholder = f"<{kind}_id_{number}>"
        self._ids[value] = placeholder
        return placeholder

    @staticmethod
    def _id_kind(value: str, *, key: str, path: tuple[str, ...]) -> str:
        prefixes = {
            "call_": "call",
            "conv_": "conversation",
            "event_": "event",
            "item_": "item",
            "resp_": "response",
            "sess_": "session",
        }
        for prefix, kind in prefixes.items():
            if value.startswith(prefix):
                return kind
        if key != "id":
            return key.removesuffix("_id")
        for component in reversed(path[:-1]):
            singular = component.removesuffix("s")
            if singular in {"conversation", "event", "item", "response", "session"}:
                return singular
        return "object"

    @staticmethod
    def _placeholder(values: dict[str, str], value: str, kind: str) -> str:
        existing = values.get(value)
        if existing is not None:
            return existing
        placeholder = f"<{kind}_{len(values) + 1}>"
        values[value] = placeholder
        return placeholder


def normalize_capture(
    *,
    kind: CaptureKind,
    model: str,
    timeline: Sequence[TimelineEntry],
    sdk_version: str,
) -> JsonObject:
    normalizer = CaptureNormalizer()
    normalized_timeline = [
        {
            "sequence": sequence,
            "direction": entry.direction,
            "payload": normalizer.normalize(entry.payload),
        }
        for sequence, entry in enumerate(timeline, start=1)
    ]
    return {
        "schema_version": 1,
        "source": {
            "service": "OpenAI Realtime API",
            "endpoint": OPENAI_REALTIME_URL,
            "transport": "websocket",
            "sdk": {"package": "openai", "version": sdk_version},
            "server_events_validated": True,
        },
        "capture": {
            "kind": kind,
            "model": model,
            "query": (
                {"intent": "transcription"}
                if kind == "transcription"
                else {"model": model}
            ),
            "synthetic_audio": kind == "transcription",
            "sample_rate": SYNTHETIC_SAMPLE_RATE if kind == "transcription" else None,
        },
        "normalization": {
            "ids": "first-seen placeholders preserve equality and references",
            "audio": "base64 payloads replaced in first-seen order",
            "text": "natural-language payloads replaced in first-seen order",
            "secrets": "credential-shaped fields always redacted",
        },
        "timeline": normalized_timeline,
    }


async def _send_event(
    connection: Any,
    event: JsonObject,
    *,
    timeline: list[TimelineEntry],
    raw: RawCaptureWriter,
) -> None:
    entry = TimelineEntry(direction="client", payload=event)
    timeline.append(entry)
    raw.record(entry)
    await connection.send(event)


async def _receive_event(
    connection: Any,
    *,
    timeline: list[TimelineEntry],
    raw: RawCaptureWriter,
    timeout: float,
) -> JsonObject:
    try:
        data = await asyncio.wait_for(connection.recv_bytes(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise CaptureError("等待 OpenAI Realtime 服务端事件超时") from exc
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CaptureError("OpenAI Realtime 返回了非 JSON 事件") from exc
    if not isinstance(payload, dict):
        raise CaptureError("OpenAI Realtime 返回的事件不是 JSON 对象")
    try:
        _SERVER_EVENT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        event_type = payload.get("type", "<missing>")
        raise CaptureError(f"SDK 无法校验服务端事件类型：{event_type}") from exc

    entry = TimelineEntry(direction="server", payload=payload)
    timeline.append(entry)
    raw.record(entry)
    return payload


async def _receive_until(
    connection: Any,
    predicate: Callable[[JsonObject], bool],
    *,
    timeline: list[TimelineEntry],
    raw: RawCaptureWriter,
    timeout: float,
    fail_on_error: bool = True,
) -> JsonObject:
    deadline = asyncio.get_running_loop().time() + timeout
    for _ in range(256):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise CaptureError("等待目标 OpenAI Realtime 事件超时")
        payload = await _receive_event(
            connection,
            timeline=timeline,
            raw=raw,
            timeout=remaining,
        )
        if predicate(payload):
            return payload
        if fail_on_error and payload.get("type") == "error":
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise CaptureError(f"OpenAI Realtime 返回错误事件，code={code!r}")
    raise CaptureError("目标事件之前收到过多 OpenAI Realtime 事件")


async def capture_transcription(
    client: AsyncOpenAI,
    *,
    model: str,
    raw: RawCaptureWriter,
    timeout: float,
) -> list[TimelineEntry]:
    timeline: list[TimelineEntry] = []
    pcm = generate_synthetic_speech_pcm()
    manager = client.realtime.connect(
        extra_query={"intent": "transcription"},
        websocket_connection_options={"open_timeout": timeout},
    )
    async with manager as connection:
        await _receive_until(
            connection,
            lambda event: event.get("type") == "session.created",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
        await _send_event(
            connection,
            {
                "type": "session.update",
                "event_id": "capture_transcription_session",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "format": {
                                "type": "audio/pcm",
                                "rate": SYNTHETIC_SAMPLE_RATE,
                            },
                            "transcription": {"model": model},
                            "turn_detection": None,
                        }
                    },
                },
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: event.get("type") == "session.updated",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
        await _send_event(
            connection,
            {
                "type": "input_audio_buffer.clear",
                "event_id": "capture_transcription_clear",
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: event.get("type") == "input_audio_buffer.cleared",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
        await _send_event(
            connection,
            {
                "type": "input_audio_buffer.append",
                "event_id": "capture_transcription_audio",
                "audio": base64.b64encode(pcm).decode("ascii"),
            },
            timeline=timeline,
            raw=raw,
        )
        await _send_event(
            connection,
            {
                "type": "input_audio_buffer.commit",
                "event_id": "capture_transcription_commit",
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: event.get("type")
            in {
                "conversation.item.input_audio_transcription.completed",
                "conversation.item.input_audio_transcription.failed",
            },
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
    return timeline


async def capture_realtime(
    client: AsyncOpenAI,
    *,
    model: str,
    raw: RawCaptureWriter,
    timeout: float,
) -> list[TimelineEntry]:
    timeline: list[TimelineEntry] = []
    manager = client.realtime.connect(
        model=model,
        websocket_connection_options={"open_timeout": timeout},
    )
    async with manager as connection:
        await _receive_until(
            connection,
            lambda event: event.get("type") == "session.created",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
        await _send_event(
            connection,
            {
                "type": "session.update",
                "event_id": "capture_realtime_session",
                "session": {
                    "type": "realtime",
                    "instructions": "Reply with exactly OK.",
                    "output_modalities": ["text"],
                    "max_output_tokens": 8,
                },
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: event.get("type") == "session.updated",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
        await _send_event(
            connection,
            {
                "type": "conversation.item.create",
                "event_id": "capture_realtime_item",
                "item": {
                    "id": "item_capture_user",
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Reply with exactly OK."}
                    ],
                },
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: event.get("type") == "conversation.item.done",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
        await _send_event(
            connection,
            {
                "type": "conversation.item.retrieve",
                "event_id": "capture_realtime_retrieve",
                "item_id": "item_capture_user",
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: event.get("type") == "conversation.item.retrieved",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
        await _send_event(
            connection,
            {
                "type": "response.create",
                "event_id": "capture_realtime_response",
                "response": {
                    "output_modalities": ["text"],
                    "max_output_tokens": 8,
                },
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: event.get("type") == "response.done",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
        await _send_event(
            connection,
            {
                "type": "response.create",
                "event_id": "capture_realtime_oob_response",
                "response": {
                    "conversation": "none",
                    "input": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Count from one to one hundred slowly.",
                                }
                            ],
                        }
                    ],
                    "instructions": "Keep counting until cancelled.",
                    "output_modalities": ["text"],
                    "max_output_tokens": 256,
                },
            },
            timeline=timeline,
            raw=raw,
        )
        oob_created = await _receive_until(
            connection,
            lambda event: event.get("type") == "response.created",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
        response = oob_created.get("response")
        response_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(response_id, str):
            raise CaptureError("OOB response.created 缺少 response.id")
        cancel_event_id = "capture_realtime_cancel"
        await _send_event(
            connection,
            {
                "type": "response.cancel",
                "event_id": cancel_event_id,
                "response_id": response_id,
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: (
                event.get("type") == "response.done"
                and isinstance(event.get("response"), dict)
                and event["response"].get("id") == response_id
            )
            or (
                event.get("type") == "error"
                and isinstance(event.get("error"), dict)
                and event["error"].get("event_id") == cancel_event_id
            ),
            timeline=timeline,
            raw=raw,
            timeout=timeout,
            fail_on_error=False,
        )
        clear_event_id = "capture_realtime_output_clear"
        await _send_event(
            connection,
            {
                "type": "output_audio_buffer.clear",
                "event_id": clear_event_id,
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: (
                event.get("type") == "error"
                and isinstance(event.get("error"), dict)
                and event["error"].get("event_id") == clear_event_id
            ),
            timeline=timeline,
            raw=raw,
            timeout=timeout,
            fail_on_error=False,
        )
        await _send_event(
            connection,
            {
                "type": "conversation.item.delete",
                "event_id": "capture_realtime_delete",
                "item_id": "item_capture_user",
            },
            timeline=timeline,
            raw=raw,
        )
        await _receive_until(
            connection,
            lambda event: event.get("type") == "conversation.item.deleted",
            timeline=timeline,
            raw=raw,
            timeout=timeout,
        )
    return timeline


def write_normalized_fixture(path: Path, document: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    path.write_text(serialized, encoding="utf-8")


async def run_capture(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    loaded_key = load_openai_api_key(repo_root=repo_root)
    raw_dir = prepare_raw_directory(args.raw_dir, repo_root=repo_root)
    fixture_dir = args.fixture_dir.expanduser().resolve()
    sdk_version = version("openai")
    modes: tuple[CaptureKind, ...]
    if args.mode == "all":
        modes = ("transcription", "realtime")
    else:
        modes = (cast(CaptureKind, args.mode),)

    print(f"API key source: {loaded_key.source}")
    print(f"Raw captures: {raw_dir}")
    async with AsyncOpenAI(api_key=loaded_key.value) as client:
        for kind in modes:
            raw_path = raw_dir / f"openai-{kind}.raw.jsonl"
            with RawCaptureWriter(raw_path) as raw:
                if kind == "transcription":
                    timeline = await capture_transcription(
                        client,
                        model=args.transcription_model,
                        raw=raw,
                        timeout=args.timeout,
                    )
                    model = args.transcription_model
                else:
                    timeline = await capture_realtime(
                        client,
                        model=args.realtime_model,
                        raw=raw,
                        timeout=args.timeout,
                    )
                    model = args.realtime_model
            document = normalize_capture(
                kind=kind,
                model=model,
                timeline=timeline,
                sdk_version=sdk_version,
            )
            fixture_path = fixture_dir / f"openai-{kind}.normalized.json"
            write_normalized_fixture(fixture_path, document)
            server_events = sum(
                entry.direction == "server" for entry in timeline
            )
            print(
                f"Captured {kind}: {server_events} server events; "
                f"normalized fixture: {fixture_path}"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("all", "transcription", "realtime"),
        default="all",
        help="抓取模式（默认两种都抓取）",
    )
    parser.add_argument(
        "--transcription-model",
        default=DEFAULT_TRANSCRIPTION_MODEL,
        help="转写会话模型",
    )
    parser.add_argument(
        "--realtime-model",
        default=DEFAULT_REALTIME_MODEL,
        help="普通 Realtime 会话模型",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=None,
        help="原始抓取目录；省略时使用 /tmp，仓库内目录必须已被 Git 忽略",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=repo_root / "tests/fixtures/openai_realtime",
        help="去标识化 fixture 输出目录",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="每段协议交互的超时秒数",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run_capture(args))
    except (CaptureError, OSError) as exc:
        print(f"Capture failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
