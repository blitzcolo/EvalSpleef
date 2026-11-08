"""End-of-batch check of the frozen Hugging Face mirrors (TECH-DESIGN 6.5 step 5).

A model registered with a private mirror ran on the weights of the mirror's recorded commit. If
the mirror no longer resolves to that commit when the batch ends, the results cannot be tied to
frozen weights any more, so an incident is opened for the batch.
"""

from __future__ import annotations

from collections.abc import Sequence

from evalspleef.core.events import EventEnvelope, EventType, new_event_id
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import HFSource, ModelSpec

from ..events import EventStore

_ACTOR = "batch-mirror-check"


async def verify_mirrors(
    models: Sequence[ModelSpec],
    hf: HFAccessChecker,
    events: EventStore,
    *,
    batch_id: str,
    ts: str,
) -> list[str]:
    """Slugs whose mirror moved off its recorded commit; one incident event for each."""
    changed: list[str] = []
    for spec in models:
        source = spec.source
        if not isinstance(source, HFSource) or source.mirror_repo is None:
            continue
        if await hf.verify_mirror(source):
            continue
        changed.append(spec.slug)
        await events.append(
            EventEnvelope(
                event_id=new_event_id(),
                ts=ts,
                batch_id=batch_id,
                type=EventType.INCIDENT_OPENED,
                actor=_ACTOR,
                payload={
                    "kind": "hf_mirror_changed",
                    "model": spec.slug,
                    "mirror_repo": source.mirror_repo,
                    "revision": source.revision,
                },
            )
        )
    return changed
