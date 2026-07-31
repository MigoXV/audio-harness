from __future__ import annotations

from audio_harness.assets import EngineAssetData
from audio_harness.database.models import EngineAsset


def asset_data(model: EngineAsset) -> EngineAssetData:
    return EngineAssetData(
        id=model.id,
        slug=model.slug,
        name=model.name,
        kind=model.kind,
        api=model.api,
        protocol=model.protocol,
        target=model.target,
        connect_timeout_seconds=model.connect_timeout_seconds,
        request_timeout_seconds=model.request_timeout_seconds,
        enabled=model.enabled,
        status=model.status,
        image=model.image,
        description=model.description,
        status_basis=model.status_basis,
        status_observed_at=model.status_observed_at,
        source=model.source,
        details=dict(model.details or {}),
        revision=model.revision,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
