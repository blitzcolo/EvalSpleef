"""Cost ledger for batch cost accounting and vendor reconciliation.

Implements the TECH-DESIGN section 6.7 cost model on top of the relational
``cost_ledger`` table: token/GPU/sandbox/infra entries recorded by platform
components, Novita billing import keyed by ``product_category`` (PRD
FR-OPS-04), per-kind reconciliation against a 5 % tolerance, and per-trial GPU
allocation via :func:`evalspleef.core.cost.allocate_gpu_cost`.
"""

from .entries import CostEntry, CostKind
from .ledger import NOVITA_SOURCE, CostLedger, KindReconciliation, ReconcileReport

__all__ = [
    "NOVITA_SOURCE",
    "CostEntry",
    "CostKind",
    "CostLedger",
    "KindReconciliation",
    "ReconcileReport",
]
