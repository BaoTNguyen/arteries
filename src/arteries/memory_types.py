"""Memory frame contract types.

The canonical definitions live in capillaries (`capillaries.agent.memory_types`),
the consumer of the frames; arteries re-exports them so both sides operate on
the same classes instead of drifting copies. Cheap to import: capillaries'
package init is lazy, so this pulls dataclasses only, not the retrieval stack.
"""

from capillaries.agent.memory_types import (
    CachedRetrieval,
    EphemeralMemory,
    EvergreenMemory,
    Insight,
    MemoryFrame,
    PersistentMemory,
)

__all__ = [
    "CachedRetrieval",
    "EphemeralMemory",
    "EvergreenMemory",
    "Insight",
    "MemoryFrame",
    "PersistentMemory",
]
