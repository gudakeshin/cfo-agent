"""ERP write-path interface — synthetic generator today; future treasury/GST connectors later.

Pipeline and API hold read-only access to ERP tables. All writes go through a SourceAdapter
so new feeds can add rows without touching the reasoning pipeline.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from cfo_agent.models import (
    ErpBankAccount,
    ErpCashDaily,
    ErpCreditNote,
    ErpEntity,
    ErpFxRate,
    ErpDebtBalance,
    ErpGlEntry,
    ErpInventoryBalance,
    ErpCovenant,
    ErpCustomer,
    ErpExpense,
    ErpGstEvent,
    ErpPayment,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
)

ERP_TABLES = [
    ErpDebtBalance,
    ErpInventoryBalance,
    ErpCreditNote,
    ErpGlEntry,
    ErpReceipt,
    ErpPayment,
    ErpSalesInvoice,
    ErpPurchaseInvoice,
    ErpExpense,
    ErpCashDaily,
    ErpRevenueDaily,
    ErpGstEvent,
    ErpCovenant,
    ErpCustomer,
    ErpBankAccount,
    ErpFxRate,
    ErpEntity,
]


@runtime_checkable
class SourceAdapter(Protocol):
    """Write path for ERP-shaped rows. Reads may use ``.session`` during generation."""

    @property
    def session(self) -> Session: ...

    def wipe(self) -> None: ...

    def add(self, obj: Any) -> None: ...

    def flush(self) -> None: ...


class SqlAlchemySourceAdapter:
    """Default adapter — persists via the app SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def wipe(self) -> None:
        for model in ERP_TABLES:
            self._session.query(model).delete()
        self._session.flush()

    def add(self, obj: Any) -> None:
        self._session.add(obj)

    def flush(self) -> None:
        self._session.flush()


class SubsetSourceAdapter:
    """Write path scoped to a connector's own owned tables (WS1 connector sync).

    Unlike :class:`SqlAlchemySourceAdapter`, ``wipe()`` only clears the given
    ``owned_models`` — never the full ERP surface — so a full-mode connector
    resync can't clobber tables another connector owns. Deletion follows the
    ``ERP_TABLES`` children-before-parents order so FK constraints hold; any
    owned model not in that fixed list (e.g. ``ErpPlanMetric``) is wiped last.

    Callers are responsible for not wiping a model that other, un-synced
    connectors' tables still hold live foreign keys into (see
    ``cfo_agent.connectors.sync`` for the safe-subset check) — SQLite FK
    enforcement will raise ``IntegrityError`` if that invariant is violated.
    """

    def __init__(self, session: Session, owned_models: list[Any]) -> None:
        self._session = session
        self._owned_models = list(owned_models)

    @property
    def session(self) -> Session:
        return self._session

    def wipe(self) -> None:
        ordered = [m for m in ERP_TABLES if m in self._owned_models]
        ordered += [m for m in self._owned_models if m not in ordered]
        for model in ordered:
            self._session.query(model).delete()
        self._session.flush()

    def add(self, obj: Any) -> None:
        self._session.add(obj)

    def flush(self) -> None:
        self._session.flush()
