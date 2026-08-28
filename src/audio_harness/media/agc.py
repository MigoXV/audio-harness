from __future__ import annotations

from fractions import Fraction

import av
import numpy as np


_POINTS = "-80/-80|-55/-42|-40/-28|-30/-18|-20/-13|-10/-9|0/-6"


def _number(value: float) -> str:
    return format(value, ".17g")


class RealtimeAutomaticGainControl:
    """基于 FFmpeg libavfilter 的无前瞻流式自动增益控制器。"""

    def __init__(
        self,
        *,
        sample_rate: int,
        attack_seconds: float,
        decay_seconds: float,
        soft_knee_db: float,
        initial_level_db: float,
        output_gain_db: float,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("AGC 采样率必须大于 0")
        if attack_seconds < 0:
            raise ValueError("AGC attack_seconds 不能小于 0")
        if decay_seconds <= 0:
            raise ValueError("AGC decay_seconds 必须大于 0")
        if soft_knee_db < 0.01:
            raise ValueError("AGC soft_knee_db 不能小于 0.01")

        self._sample_rate = sample_rate
        self._time_base = Fraction(1, sample_rate)
        self._next_pts = 0
        self._closed = False
        self._graph = av.filter.Graph()
        self._source = self._graph.add_abuffer(
            sample_rate=sample_rate,
            format="s16",
            layout="mono",
            time_base=self._time_base,
        )
        compand = self._graph.add(
            "compand",
            ":".join(
                (
                    f"attacks={_number(attack_seconds)}",
                    f"decays={_number(decay_seconds)}",
                    f"points={_POINTS}",
                    f"soft-knee={_number(soft_knee_db)}",
                    "gain=0",
                    f"volume={_number(initial_level_db)}",
                    "delay=0",
                )
            ),
        )
        volume = self._graph.add("volume", f"{_number(output_gain_db)}dB")
        output_format = self._graph.add(
            "aformat",
            f"sample_fmts=s16:sample_rates={sample_rate}:channel_layouts=mono",
        )
        self._sink = self._graph.add("abuffersink")
        self._graph.link_nodes(
            self._source,
            compand,
            volume,
            output_format,
            self._sink,
        )
        self._graph.configure()

    def process(self, pcm_s16le: bytes) -> bytes:
        if self._closed:
            raise RuntimeError("AGC 已经关闭")
        if len(pcm_s16le) % 2:
            raise ValueError("AGC 输入必须是完整的 PCM S16LE 采样")
        if not pcm_s16le:
            return b""

        samples = np.frombuffer(pcm_s16le, dtype="<i2").reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = self._sample_rate
        frame.time_base = self._time_base
        frame.pts = self._next_pts
        self._next_pts += frame.samples
        self._graph.push(frame)

        output = bytearray()
        while True:
            try:
                filtered = self._graph.pull()
            except av.error.BlockingIOError:
                break
            output.extend(
                filtered.to_ndarray().astype("<i2", copy=False).tobytes()
            )
        if len(output) != len(pcm_s16le):
            raise RuntimeError(
                "FFmpeg AGC 未同步返回等长 PCM: "
                f"input={len(pcm_s16le)}, output={len(output)}"
            )
        return bytes(output)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._graph.push(None)
        trailing_samples = 0
        while True:
            try:
                trailing_samples += self._graph.pull().samples
            except (av.error.BlockingIOError, av.error.EOFError):
                break
        if trailing_samples:
            raise RuntimeError(
                f"delay=0 的 FFmpeg AGC 意外缓存了 {trailing_samples} 个采样"
            )
