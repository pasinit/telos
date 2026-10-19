"""Improvement subsystem — message-count trigger + deploy-via-Recordatio.

Pre-Phase-5, ``telos.services.improvement.ImprovementService`` subscribed
to the in-process ``TurnComplete`` event bus and mutated the local
``Agent`` directly on auto-deploy. Phase 5 of the Telos+Recordatio
migration replaces both halves:

- Trigger:  polls per-``(user_id, slug)`` chat-message count from the
  conversation log and fires when N new messages have accrued since the
  last improver run. Lives here in :mod:`telos.improvement.triggers`.
- Deploy:   instead of mutating the in-process ``Agent``, the improver
  writes the new ``system_prompt`` back to Recordatio via
  ``PUT /v1/agents/{user_id}/{slug}/config``. The optimistic
  concurrency contract (409 on version mismatch) is handled with one
  refetch+retry. Lives in :mod:`telos.improvement.deploy`.

See ``MIGRATION_PLAN.md`` §"Phase 5 — Improver migration" for the full
spec.
"""

from telos.improvement.deploy import (
    ConfigDeployFailed,
    RecordatioConfigClient,
    deploy_candidate_to_recordatio,
)
from telos.improvement.triggers import MessageCountTrigger

__all__ = [
    "ConfigDeployFailed",
    "RecordatioConfigClient",
    "MessageCountTrigger",
    "deploy_candidate_to_recordatio",
]
