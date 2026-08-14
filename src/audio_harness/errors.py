class AudioHarnessError(Exception):
    """Audio Harness 可映射到外部协议的基础异常。"""


class InvalidAudioError(AudioHarnessError):
    """输入音频或配置不合法。"""


class CapacityExceededError(AudioHarnessError):
    """有界容量已经耗尽。"""


class EngineUnavailableError(AudioHarnessError):
    """下游引擎不可用。"""


class EngineDeadlineError(AudioHarnessError):
    """下游引擎请求超时。"""


class EngineConfigurationInvalidError(AudioHarnessError):
    """引擎快照与推理客户端能力不一致。"""

