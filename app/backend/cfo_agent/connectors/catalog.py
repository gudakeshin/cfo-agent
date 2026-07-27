"""Static catalog of simulated ERP/EPM connectors.

Each connector "owns" one or more ERP tables — the demo analog of a real source
system being the system-of-record for that data. ``ErpPlanMetric`` is the one
exception: both Anaplan and SAP Analytics Cloud write into it, disambiguated by
``kind`` ('plan' vs 'analytics'), so it is the only table with two owners.

Colors below are generic brand-adjacent accents chosen for the demo UI — not
trademarked logos/marks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cfo_agent.models import (
    Base,
    ErpCashDaily,
    ErpCovenant,
    ErpCustomer,
    ErpExpense,
    ErpGstEvent,
    ErpPayment,
    ErpPlanMetric,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
)


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    secret: bool = False


@dataclass(frozen=True)
class ConnectorDef:
    id: str
    vendor: str
    display_name: str
    auth_type: str  # oauth2|basic|api_key
    config_fields: list[ConfigField]
    owned_models: list[type[Base]]
    brand_initials: str
    brand_color: str
    # Only set for the two EPM connectors that share ErpPlanMetric ('plan'|'analytics')
    plan_kind: str | None = field(default=None)


CONNECTORS: list[ConnectorDef] = [
    ConnectorDef(
        id="sap_s4hana",
        vendor="SAP",
        display_name="SAP S/4HANA",
        auth_type="oauth2",
        config_fields=[
            ConfigField("client_id", "Client ID"),
            ConfigField("tenant_url", "Tenant URL"),
            ConfigField("client_secret", "Client Secret", secret=True),
        ],
        owned_models=[ErpSalesInvoice, ErpReceipt, ErpPurchaseInvoice, ErpPayment, ErpGstEvent],
        brand_initials="S4",
        brand_color="#0FAAFF",
    ),
    ConnectorDef(
        id="oracle_fusion",
        vendor="Oracle",
        display_name="Oracle Fusion Cloud ERP",
        auth_type="basic",
        config_fields=[
            ConfigField("username", "Username"),
            ConfigField("instance_url", "Instance URL"),
            ConfigField("password", "Password", secret=True),
        ],
        owned_models=[ErpCashDaily, ErpCovenant],
        brand_initials="OF",
        brand_color="#F80000",
    ),
    ConnectorDef(
        id="ms_dynamics_365",
        vendor="Microsoft",
        display_name="Dynamics 365 Finance",
        auth_type="oauth2",
        config_fields=[
            ConfigField("client_id", "Client ID"),
            ConfigField("tenant_id", "Tenant ID"),
            ConfigField("client_secret", "Client Secret", secret=True),
        ],
        owned_models=[ErpExpense],
        brand_initials="D365",
        brand_color="#00A4EF",
    ),
    ConnectorDef(
        id="salesforce",
        vendor="Salesforce",
        display_name="Salesforce Revenue Cloud",
        auth_type="oauth2",
        config_fields=[
            ConfigField("client_id", "Client ID"),
            ConfigField("instance_url", "Instance URL"),
            ConfigField("client_secret", "Client Secret", secret=True),
        ],
        owned_models=[ErpCustomer, ErpRevenueDaily],
        brand_initials="SF",
        brand_color="#00A1E0",
    ),
    ConnectorDef(
        id="anaplan",
        vendor="Anaplan",
        display_name="Anaplan",
        auth_type="api_key",
        config_fields=[
            ConfigField("workspace_id", "Workspace ID"),
            ConfigField("api_key", "API Key", secret=True),
        ],
        owned_models=[ErpPlanMetric],
        brand_initials="AN",
        brand_color="#4A1FB8",
        plan_kind="plan",
    ),
    ConnectorDef(
        id="sap_sac",
        vendor="SAP",
        display_name="SAP Analytics Cloud",
        auth_type="oauth2",
        config_fields=[
            ConfigField("client_id", "Client ID"),
            ConfigField("tenant_url", "Tenant URL"),
            ConfigField("client_secret", "Client Secret", secret=True),
        ],
        owned_models=[ErpPlanMetric],
        brand_initials="SAC",
        brand_color="#354A5F",
        plan_kind="analytics",
    ),
]

CATALOG_BY_ID: dict[str, ConnectorDef] = {c.id: c for c in CONNECTORS}

# table name -> model class, across every owned table in the catalog
MODEL_BY_TABLE: dict[str, type[Base]] = {
    m.__tablename__: m for c in CONNECTORS for m in c.owned_models
}

# table name -> [connector ids that own it]. Every ERP table should have exactly
# one owner, except erp_plan_metrics (anaplan + sap_sac, split by kind — see
# tests/test_connectors.py::test_catalog_ownership_integrity).
OWNERSHIP: dict[str, list[str]] = {}
for _c in CONNECTORS:
    for _m in _c.owned_models:
        OWNERSHIP.setdefault(_m.__tablename__, []).append(_c.id)


def get_connector(connector_id: str) -> ConnectorDef:
    if connector_id not in CATALOG_BY_ID:
        raise KeyError(connector_id)
    return CATALOG_BY_ID[connector_id]


def list_connectors() -> list[ConnectorDef]:
    return list(CONNECTORS)
