"""Runtime Collector and Normalizer Package.

Provides integration, normalization, and aggregation safety for runtime observations:
- Kilo session exports & aggregate statistics
- Faigate gateway observations (pure execution truth)

Strict PRD Invariant Adherence:
- US-10.a: Import documented versions (Kilo 7.0.46, Faigate 2.10.2). Missing fields
           are never invented, only explicit derived metrics with stated formulas.
- US-10.b: Kilo-Stats as aggregates and session requests covering the same scope
           are never double-summed; origin/provenance and scope level remain distinct.
- US-10.c: Gateway observation is pure execution truth (billing_assertion=False).
           Estimated/list prices never masquerade as billed subscription costs.
- US-10.d: Identifier preservation (session_id, project_id, task_id, council_id).
           Missing IDs remain None/empty without synthetic guessing.
"""

from .collector import (
    RuntimeCollector,
    RuntimeObservation,
    RuntimeScopeError,
    RuntimeAggregationError,
    RuntimeValidationError,
    aggregate_runtime_observations,
    reconcile_runtime_with_provider,
)

__all__ = [
    "RuntimeCollector",
    "RuntimeObservation",
    "RuntimeScopeError",
    "RuntimeAggregationError",
    "RuntimeValidationError",
    "aggregate_runtime_observations",
    "reconcile_runtime_with_provider",
]
