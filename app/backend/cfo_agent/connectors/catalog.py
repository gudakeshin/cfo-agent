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
    multiline: bool = False  # textarea (e.g. a JSON map of SQL queries)
    placeholder: str = ""


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
    # False for real connectors (e.g. the CSV file drop) that load into the same
    # tables as the simulated ones; they are excluded from the demo ownership map.
    simulated: bool = field(default=True)


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
        owned_models=[ErpEntity, ErpSalesInvoice, ErpReceipt, ErpCreditNote, ErpPurchaseInvoice, ErpPayment, ErpGstEvent, ErpGlEntry, ErpInventoryBalance],
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
        owned_models=[ErpBankAccount, ErpFxRate, ErpCashDaily, ErpCovenant, ErpDebtBalance],
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

# Real connector: ERP exports dropped as CSV files (works with SAP, Oracle, Tally…).
CSV_DROP = ConnectorDef(
    id="csv_drop",
    vendor="Any ERP",
    display_name="File drop (CSV exports)",
    auth_type="file",
    config_fields=[ConfigField("folder", "Inbox folder name")],
    owned_models=[
        ErpEntity, ErpBankAccount, ErpFxRate, ErpCustomer, ErpCovenant, ErpSalesInvoice, ErpReceipt,
        ErpCreditNote, ErpPurchaseInvoice, ErpPayment, ErpExpense, ErpCashDaily, ErpRevenueDaily,
        ErpGstEvent, ErpGlEntry, ErpPlanMetric, ErpInventoryBalance, ErpDebtBalance,
    ],
    brand_initials="CSV",
    brand_color="#2F6B4F",
    simulated=False,
)
CONNECTORS.append(CSV_DROP)

# Real connector: Tally Prime / ERP 9 over its XML HTTP server (read-only exports).
TALLY = ConnectorDef(
    id="tally",
    vendor="Tally Solutions",
    display_name="Tally Prime",
    auth_type="none",
    config_fields=[
        ConfigField("url", "Tally XML server URL", placeholder="http://tally-host:9000"),
        ConfigField("company", "Company name (as open in Tally)"),
        ConfigField("entity", "Entity code in this app", placeholder="MAIN"),
        ConfigField("default_terms_days", "Default credit days", placeholder="45"),
    ],
    owned_models=[ErpCustomer, ErpSalesInvoice, ErpReceipt, ErpCreditNote, ErpPurchaseInvoice, ErpPayment],
    brand_initials="TP",
    brand_color="#1F5AA6",
    simulated=False,
)
CONNECTORS.append(TALLY)

# Real connector: the client's own SELECTs against a reporting replica, read-only login.
SQL_READONLY = ConnectorDef(
    id="sql_readonly",
    vendor="Any SQL database",
    display_name="Database (read-only SQL)",
    auth_type="db_url",
    config_fields=[
        ConfigField("queries", "Queries — JSON {table: SELECT …}", multiline=True,
                    placeholder='{"sales_invoices": "SELECT inv_no AS business_key, … FROM ar_invoices WHERE updated_at > :since"}'),
        ConfigField("database_url", "Database URL (read-only user)", secret=True,
                    placeholder="postgresql+psycopg://cfo_reader:…@replica:5432/erp"),
    ],
    owned_models=[
        ErpEntity, ErpBankAccount, ErpFxRate, ErpCustomer, ErpCovenant, ErpSalesInvoice, ErpReceipt,
        ErpCreditNote, ErpPurchaseInvoice, ErpPayment, ErpExpense, ErpCashDaily, ErpRevenueDaily,
        ErpGstEvent, ErpGlEntry, ErpPlanMetric, ErpInventoryBalance, ErpDebtBalance,
    ],
    brand_initials="SQL",
    brand_color="#5B4B8A",
    simulated=False,
)
CONNECTORS.append(SQL_READONLY)
EXTRACTOR_IDS = {"tally", "sql_readonly"}

CATALOG_BY_ID: dict[str, ConnectorDef] = {c.id: c for c in CONNECTORS}

# table name -> model class, across every owned table in the catalog
MODEL_BY_TABLE: dict[str, type[Base]] = {
    m.__tablename__: m for c in CONNECTORS if c.simulated for m in c.owned_models
}

# table name -> [connector ids that own it]. Every ERP table should have exactly
# one owner, except erp_plan_metrics (anaplan + sap_sac, split by kind — see
# tests/test_connectors.py::test_catalog_ownership_integrity).
OWNERSHIP: dict[str, list[str]] = {}
for _c in CONNECTORS:
    if not _c.simulated:
        continue
    for _m in _c.owned_models:
        OWNERSHIP.setdefault(_m.__tablename__, []).append(_c.id)


def get_connector(connector_id: str) -> ConnectorDef:
    if connector_id not in CATALOG_BY_ID:
        raise KeyError(connector_id)
    return CATALOG_BY_ID[connector_id]


def list_connectors() -> list[ConnectorDef]:
    return list(CONNECTORS)
