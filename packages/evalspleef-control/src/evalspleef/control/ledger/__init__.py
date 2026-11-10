"""Cost ledger for batch cost accounting and vendor reconciliation.

Implements the TECH-DESIGN section 6.7 cost model on top of the relational
``cost_ledger`` table: token/GPU/sandbox/infra entries recorded by platform
components, billing import of provider-parsed bill lines (PRD FR-OPS-04),
per-kind reconciliation against a 5 % tolerance, and per-trial GPU allocation
via :func:`evalspleef.core.cost.allocate_gpu_cost`.
"""

from .entries import CostEntry, CostKind
from .ledger import BILL_SOURCE_PREFIX, CostLedger, KindReconciliation, ReconcileReport

__all__ = [
    "BILL_SOURCE_PREFIX",
    "CostEntry",
    "CostKind",
    "CostLedger",
    "KindReconciliation",
    "ReconcileReport",
]
