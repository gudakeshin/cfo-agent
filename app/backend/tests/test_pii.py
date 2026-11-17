"""PII masking: names never reach the model; outputs and tool inputs are restored."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from cfo_agent import llm
from cfo_agent.models import ErpCustomer
from cfo_agent.pii import AliasMap, StreamUnmasker, build_alias_map, invalidate
from cfo_agent.settings import get_settings


@pytest.fixture()
def masking(seeded_erp, db_session, monkeypatch):
    monkeypatch.setenv("PII_MASKING", "true")
    get_settings.cache_clear()
    invalidate()
    yield build_alias_map(db_session)
    invalidate()
    get_settings.cache_clear()


def test_alias_map_roundtrip():
    amap = AliasMap({"Apex Retail Pvt Ltd": "CUSTOMER_01", "Apex Retail": "CUSTOMER_02", "Orion Infra": "VENDOR_01"})
    s = "Apex Retail Pvt Ltd owes more than apex retail; Orion Infra unpaid. Apexian is not a name."
    m = amap.mask(s)
    assert "Apex" not in m.replace("Apexian", "") and "Orion" not in m
    assert "CUSTOMER_01" in m and "CUSTOMER_02" in m and "Apexian" in m
    assert amap.unmask(m).lower() == s.lower()


def test_stream_unmasker_handles_split_aliases():
    amap = AliasMap({"Apex Retail": "CUSTOMER_01"})
    u = StreamUnmasker(amap)
    chunks = ["Overdue at CUSTO", "MER_0", "1 is ₹11 Cr; CUSTOMER_01"]
    out = "".join(u.feed(c) for c in chunks) + u.flush()
    assert out == "Overdue at Apex Retail is ₹11 Cr; Apex Retail"


class Out(BaseModel):
    title: str
    notes: list[str]


def test_parse_masks_request_and_unmasks_result(masking, db_session, monkeypatch):
    name = db_session.query(ErpCustomer).order_by(ErpCustomer.business_key).first().name
    alias = masking.forward[name]
    captured = {}

    class FakeMessages:
        def parse(self, **kw):
            captured.update(kw)
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                parsed_output=Out(title=f"{alias} is late", notes=[f"chase {alias}"]),
            )

    monkeypatch.setattr(llm, "_client", lambda *a: SimpleNamespace(messages=FakeMessages()))
    out = llm.parse(Out, system=f"Context about {name}", user=f"Why is {name} late?", purpose="t")
    sent = json.dumps(captured, default=str)
    assert name not in sent and alias in sent
    assert out.title == f"{name} is late" and out.notes == [f"chase {name}"]


def test_stream_round_masks_and_unmasks(masking, db_session, monkeypatch):
    name = db_session.query(ErpCustomer).order_by(ErpCustomer.business_key).first().name
    alias = masking.forward[name]
    captured = {}

    class FakeStream:
        def __init__(self, **kw):
            captured.update(kw)
            self.text_stream = iter([f"{alias[:5]}", f"{alias[5:]} pays late."])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(type="text", text=f"{alias} pays late."),
                    SimpleNamespace(type="tool_use", id="t1", name="list_records",
                                    input={"table": "sales_invoices", "customer": alias}),
                ],
            )

    monkeypatch.setattr(llm, "_client", lambda *a: SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(**kw))))
    streamed = []
    blocks, stop = llm.stream_round(
        system=f"About {name}",
        messages=[
            {"role": "user", "content": f"Is {name} paying?"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": json.dumps({"customer": name})}]},
        ],
        tools=[], purpose="t", on_text=streamed.append,
    )
    assert name not in json.dumps(captured, default=str)
    assert "".join(streamed) == f"{name} pays late."
    assert blocks[1]["input"]["customer"] == name  # tool input restored for the local query
    assert blocks[0]["text"] == f"{name} pays late."


def test_masking_off_by_default(seeded_erp, db_session):
    from cfo_agent.pii import current_map

    get_settings.cache_clear()
    assert current_map() is None
