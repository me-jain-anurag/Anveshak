"""Independent re-verification of every transfer that a reported path rests on (ADR-0009)."""

from __future__ import annotations

from .chains.base import ChainSource, Verification, VerificationStatus
from .domain import EndpointKind
from .tracer import TraceResult

# Paths to these endpoints are what investigators act on, so every step is re-checked.
VERIFIED_ENDPOINT_KINDS = frozenset(
    {
        EndpointKind.VASP,
        EndpointKind.SERVICE,
        EndpointKind.DORMANT,
        EndpointKind.COINJOIN_LIKE,
        EndpointKind.UNLABELED_CONTRACT,
        EndpointKind.HIGH_ACTIVITY,
    }
)


def verify_paths(result: TraceResult, source: ChainSource, cap: int = 400, already: dict[str, Verification] | None = None) -> dict[str, Verification]:
    by_id = {t.id: t for t in result.transfers}
    ordered: list[str] = []
    seen: set[str] = set()
    for endpoint in result.endpoints:
        if endpoint.kind not in VERIFIED_ENDPOINT_KINDS:
            continue
        for tid in endpoint.path:
            if tid not in seen:
                seen.add(tid)
                ordered.append(tid)
    out: dict[str, Verification] = {}
    for n, tid in enumerate(ordered):
        if already and tid in already:
            out[tid] = already[tid]
        elif n >= cap:
            out[tid] = Verification(
                transfer_id=tid,
                status=VerificationStatus.UNVERIFIABLE,
                method="none",
                detail=f"verification cap of {cap} transfers reached — not checked",
            )
        else:
            out[tid] = source.verify(by_id[tid])
    return out
