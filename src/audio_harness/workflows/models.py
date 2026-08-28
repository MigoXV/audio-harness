from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_IDENTIFIER = r"^[A-Za-z0-9_-]+$"
_OUTPUT_NAME = r"^[a-z][a-z0-9_]*$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FailurePolicy(str, Enum):
    FAIL_PIPELINE = "fail_pipeline"
    FAIL_OPEN = "fail_open"


class NodePosition(StrictModel):
    x: float = 0
    y: float = 0


class FireRedPostprocessingParameters(StrictModel):
    smooth_window_size: int = Field(default=5, gt=0, strict=True)
    speech_threshold: float = Field(
        default=0.5,
        ge=0,
        le=1,
        allow_inf_nan=False,
        strict=True,
    )
    pad_start_frame: int = Field(default=5, gt=0, strict=True)
    min_speech_frame: int = Field(default=8, gt=0, strict=True)
    max_speech_frame: int = Field(default=2000, gt=0, strict=True)
    min_silence_frame: int = Field(default=20, gt=0, strict=True)


class FireRedPostprocessingOptions(StrictModel):
    processor: Literal["firered.stream_vad"] = "firered.stream_vad"
    version: Literal[1] = 1
    parameters: FireRedPostprocessingParameters = Field(
        default_factory=FireRedPostprocessingParameters
    )


class PhysicalVadConfig(StrictModel):
    min_segment_seconds: float = Field(default=0.5, ge=0)
    max_segment_seconds: float = Field(default=60.0, gt=0)
    audio_buffer_seconds: float = Field(default=120.0, gt=0)
    speech_pre_roll_seconds: float = Field(default=0.3, ge=0)
    progress_interval_seconds: float = Field(
        default=0.2,
        ge=0,
        allow_inf_nan=False,
        strict=True,
    )
    postprocessing: FireRedPostprocessingOptions | None = None

    @model_validator(mode="after")
    def validate_buffer(self) -> "PhysicalVadConfig":
        required = self.max_segment_seconds + self.speech_pre_roll_seconds
        if self.audio_buffer_seconds < required:
            raise ValueError(
                "audio_buffer_seconds 不能小于 "
                "max_segment_seconds + speech_pre_roll_seconds"
            )
        return self


class SemanticVadConfig(StrictModel):
    max_buffer_seconds: float = Field(default=60.0, gt=0)


class PhysicalVadNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["physical_vad"] = "physical_vad"
    engine_id: str | None = None
    config: PhysicalVadConfig = Field(default_factory=PhysicalVadConfig)


class StreamingDenoiseConfig(StrictModel):
    denoised_weight: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
        strict=True,
    )


class StreamingDenoiseNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["streaming_denoise"] = "streaming_denoise"
    engine_id: str | None = None
    config: StreamingDenoiseConfig = Field(default_factory=StreamingDenoiseConfig)
    failure_policy: FailurePolicy = FailurePolicy.FAIL_OPEN


class AutomaticGainControlConfig(StrictModel):
    attack_seconds: float = Field(default=0, ge=0, le=10, allow_inf_nan=False)
    decay_seconds: float = Field(default=2.5, gt=0, le=60, allow_inf_nan=False)
    soft_knee_db: float = Field(default=6, ge=0.01, le=10, allow_inf_nan=False)
    initial_level_db: float = Field(default=-12, ge=-80, le=0, allow_inf_nan=False)
    output_gain_db: float = Field(default=4.5, ge=-24, le=24, allow_inf_nan=False)


class AutomaticGainControlNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["automatic_gain_control"] = "automatic_gain_control"
    engine_id: None = None
    config: AutomaticGainControlConfig = Field(
        default_factory=AutomaticGainControlConfig
    )


class SemanticVadNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["semantic_vad"] = "semantic_vad"
    engine_id: str | None = None
    config: SemanticVadConfig = Field(default_factory=SemanticVadConfig)
    failure_policy: FailurePolicy = FailurePolicy.FAIL_OPEN


class AsrNodeConfig(StrictModel):
    remove_punctuation: bool = False


class AsrNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["asr"] = "asr"
    engine_id: str | None = None
    config: AsrNodeConfig = Field(default_factory=AsrNodeConfig)


class AudioEmbeddingNodeConfig(StrictModel):
    algorithm: str = Field(default="eres2netv2", min_length=1, max_length=100)
    expected_dimension: int = Field(default=192, gt=0, le=65536)
    normalize: bool = True


class AudioEmbeddingNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["audio_embedding"] = "audio_embedding"
    engine_id: str | None = None
    config: AudioEmbeddingNodeConfig = Field(default_factory=AudioEmbeddingNodeConfig)


class SpeakerDiarizationPostprocessingOptions(StrictModel):
    processor: Literal[
        "audio_harness.diarization.vad_coverage"
    ] = "audio_harness.diarization.vad_coverage"
    version: Literal[1] = 1


class SpeakerDiarizationNodeConfig(StrictModel):
    min_batch_seconds: float = Field(default=60.0, ge=0)
    max_batch_seconds: float = Field(default=120.0, gt=0)
    min_asr_segment_seconds: float = Field(default=0.5, ge=0)
    postprocessing: SpeakerDiarizationPostprocessingOptions | None = Field(
        default_factory=SpeakerDiarizationPostprocessingOptions
    )

    @model_validator(mode="after")
    def validate_batch_range(self) -> "SpeakerDiarizationNodeConfig":
        if self.min_batch_seconds > self.max_batch_seconds:
            raise ValueError("min_batch_seconds 不能大于 max_batch_seconds")
        return self


class SpeakerDiarizationNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["speaker_diarization"] = "speaker_diarization"
    engine_id: str | None = None
    config: SpeakerDiarizationNodeConfig = Field(
        default_factory=SpeakerDiarizationNodeConfig
    )


class HdbscanClusteringParameters(StrictModel):
    min_cluster_size: int = Field(default=2, ge=2, strict=True)
    min_samples: int = Field(default=2, ge=1, strict=True)
    cluster_selection_epsilon: float = Field(
        default=0,
        ge=0,
        allow_inf_nan=False,
        strict=True,
    )
    allow_single_cluster: bool = Field(default=False, strict=True)


class SpeakerClusteringPostprocessingOptions(StrictModel):
    processor: Literal[
        "audio_harness.speaker_clustering.complete_link_merge"
    ] = "audio_harness.speaker_clustering.complete_link_merge"
    version: Literal[1] = 1
    high_similarity_threshold: float = Field(
        default=0.70,
        ge=0,
        le=1,
        allow_inf_nan=False,
        strict=True,
    )
    low_similarity_threshold: float = Field(
        default=0.50,
        ge=0,
        le=1,
        allow_inf_nan=False,
        strict=True,
    )

    @model_validator(mode="after")
    def validate_thresholds(
        self,
    ) -> "SpeakerClusteringPostprocessingOptions":
        if self.low_similarity_threshold > self.high_similarity_threshold:
            raise ValueError(
                "low_similarity_threshold 不能大于 high_similarity_threshold"
            )
        return self


class SpeakerClusteringNodeConfig(StrictModel):
    embedding_algorithm: str = Field(
        default="eres2netv2",
        min_length=1,
        max_length=100,
    )
    expected_dimension: int = Field(default=192, gt=0, le=65536)
    normalize: bool = True
    max_embedding_seconds: float = Field(default=60.0, gt=0, le=600)
    clustering_algorithm: Literal["hdbscan"] = "hdbscan"
    parameters: HdbscanClusteringParameters = Field(
        default_factory=HdbscanClusteringParameters
    )
    postprocessing: SpeakerClusteringPostprocessingOptions | None = Field(
        default_factory=SpeakerClusteringPostprocessingOptions
    )
    max_vectors: int = Field(default=10000, gt=0, le=1000000)


class SpeakerClusteringNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["speaker_clustering"] = "speaker_clustering"
    engine_id: str | None = None
    config: SpeakerClusteringNodeConfig = Field(
        default_factory=SpeakerClusteringNodeConfig
    )
    failure_policy: FailurePolicy = FailurePolicy.FAIL_OPEN


class VectorSearchNodeConfig(StrictModel):
    collection_name: str = Field(min_length=1, max_length=255)
    vector_name: str = Field(min_length=1, max_length=100)
    top_k: int = Field(default=10, ge=1, le=100)
    score_threshold: float | None = None
    filter: dict = Field(default_factory=dict)
    include_embedding: bool = False


class VectorSearchNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["vector_search"] = "vector_search"
    engine_id: None = None
    config: VectorSearchNodeConfig


class SpeakerFilterNodeConfig(StrictModel):
    allowed_speaker_ids: list[str] = Field(min_length=1, max_length=100)
    min_score: float = Field(default=0.75, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_speaker_ids(self) -> "SpeakerFilterNodeConfig":
        normalized = [value.strip() for value in self.allowed_speaker_ids]
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("allowed_speaker_ids 必须为长度 1..128 的非空字符串")
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_speaker_ids 不能重复")
        self.allowed_speaker_ids = normalized
        return self


class SpeakerFilterNode(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    kind: Literal["speaker_filter"] = "speaker_filter"
    engine_id: None = None
    config: SpeakerFilterNodeConfig


AudioNode = Annotated[
    PhysicalVadNode
    | StreamingDenoiseNode
    | AutomaticGainControlNode
    | SemanticVadNode
    | AsrNode
    | AudioEmbeddingNode
    | SpeakerDiarizationNode
    | SpeakerClusteringNode
    | VectorSearchNode
    | SpeakerFilterNode,
    Field(discriminator="kind"),
]


class GraphInputRef(StrictModel):
    kind: Literal["input"] = "input"
    name: Literal["audio"] = "audio"


class NodeOutputRef(StrictModel):
    kind: Literal["node"] = "node"
    node_id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    port: str = Field(min_length=1, max_length=100, pattern=_OUTPUT_NAME)


AudioSourceRef = Annotated[
    GraphInputRef | NodeOutputRef,
    Field(discriminator="kind"),
]


class NodeInputRef(StrictModel):
    node_id: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    port: str = Field(min_length=1, max_length=100, pattern=_OUTPUT_NAME)


class AudioEdge(StrictModel):
    id: str = Field(min_length=1, max_length=150, pattern=_IDENTIFIER)
    source: AudioSourceRef
    target: NodeInputRef


class NamedOutputBinding(StrictModel):
    name: str = Field(min_length=1, max_length=100, pattern=_OUTPUT_NAME)
    source: NodeOutputRef


class AudioWorkflowDocument(StrictModel):
    schema_version: Literal[1] = 1
    nodes: list[AudioNode]
    edges: list[AudioEdge]
    outputs: list[NamedOutputBinding]
    layout: dict[str, NodePosition] = Field(default_factory=dict)

    @model_validator(mode="after")
    def migrate_detection_audio_port(self) -> "AudioWorkflowDocument":
        """把旧版降噪到 VAD 的 audio 连线归一化到专用检测端口。"""

        node_kinds = {node.id: node.kind for node in self.nodes}
        migrated: list[AudioEdge] = []
        for edge in self.edges:
            source = edge.source
            if (
                edge.target.port == "audio"
                and node_kinds.get(edge.target.node_id) == "physical_vad"
                and isinstance(source, NodeOutputRef)
                and source.port == "audio"
                and node_kinds.get(source.node_id) == "streaming_denoise"
            ):
                edge = edge.model_copy(
                    update={
                        "target": edge.target.model_copy(
                            update={"port": "detection_audio"}
                        )
                    }
                )
            migrated.append(edge)
        self.edges = migrated
        return self

    @model_validator(mode="after")
    def validate_identifiers(self) -> "AudioWorkflowDocument":
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("节点 ID 不能重复")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("连线 ID 不能重复")
        output_names = [output.name for output in self.outputs]
        if len(output_names) != len(set(output_names)):
            raise ValueError("工作流出口名称不能重复")
        unknown_layout = set(self.layout).difference(node_ids)
        if unknown_layout:
            raise ValueError(
                "布局引用不存在的节点: " + ", ".join(sorted(unknown_layout))
            )
        return self


def default_audio_workflow() -> AudioWorkflowDocument:
    return AudioWorkflowDocument(
        nodes=[
            PhysicalVadNode(id="physical-vad"),
            AsrNode(id="asr"),
        ],
        edges=[
            AudioEdge(
                id="audio-to-physical-vad",
                source=GraphInputRef(),
                target=NodeInputRef(node_id="physical-vad", port="audio"),
            ),
            AudioEdge(
                id="physical-vad-to-asr",
                source=NodeOutputRef(node_id="physical-vad", port="segments"),
                target=NodeInputRef(node_id="asr", port="segments"),
            ),
        ],
        outputs=[
            NamedOutputBinding(
                name="primary_transcript",
                source=NodeOutputRef(node_id="asr", port="transcripts"),
            ),
            NamedOutputBinding(
                name="speech_started",
                source=NodeOutputRef(node_id="physical-vad", port="speech_started"),
            ),
            NamedOutputBinding(
                name="speech_stopped",
                source=NodeOutputRef(node_id="physical-vad", port="speech_stopped"),
            ),
        ],
        layout={
            "physical-vad": NodePosition(x=240, y=120),
            "asr": NodePosition(x=520, y=120),
        },
    )
