"""Mobile-first email: escaped content, a plain-text part, links into the app for the
right client, and a readable red-line alert."""

from __future__ import annotations

from datetime import date, datetime

import pytest

import cfo_agent.digest.email as mail
from cfo_agent.models import Alert, Digest, DigestItem, WeightingVersion
from cfo_agent.settings import get_settings
from cfo_agent.tenancy import _load, tenant_context


@pytest.fixture()
def digest(db_session, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://cfo.client.test")
    get_settings.cache_clear()
    s = db_session
    wv = s.query(WeightingVersion).first()
    d = Digest(week="2026-W28", weighting_version_id=wv.id, headline="Cash slips below the buffer",
               weighting_label="Q2 lens", created_at=datetime(2026, 7, 13, 8))
    s.add(d)
    s.flush()
    s.add_all([
        DigestItem(
            digest_id=d.id, rank=1, title="Cash <b>below</b> buffer & more", is_red_line=True, priority_score=999,
            what="First sentence. Second sentence. Third sentence. Fourth sentence that must be cut.",
            action="Do one. Do two. Do three.",
            score_decomp_json={"continuity": {"status": "continuing", "since_week": "2026-W26"},
                               "drivers": {"rows": [{"label": "Apex", "current_paise": 12_400_000_00},
                                                    {"label": "Nexus", "current_paise": 11_600_000_00},
                                                    {"label": "Third", "current_paise": 1}]}}),
        DigestItem(digest_id=d.id, rank=2, title="DSO", what="DSO up.", action="", priority_score=1.5,
                   score_decomp_json={"continuity": {"status": "new"}}),
        DigestItem(digest_id=d.id, rank=3, title="Opex", what="Opex up.", action="Look.", priority_score=0.9),
    ])
    s.commit()
    yield d
    get_settings.cache_clear()


def test_week_label_and_priority_match_the_app():
    assert mail.week_label("2026-W28") == "Week 28 (6–12 Jul)"
    assert mail.week_label("2026-W27") == "Week 27 (29 Jun – 5 Jul)"
    assert [mail.priority_label(x, False) for x in (2.0, 1.2, 0.8)] == ["High priority", "Medium priority", "Watch"]
    assert mail.priority_label(0.1, True) == "Red line"


def test_digest_email_is_a_short_escaped_mobile_layout_with_links(db_session, digest):
    html = mail.render_digest_html(db_session, digest)
    # AI-written text can never inject markup
    assert "<b>below</b>" not in html and "Cash &lt;b&gt;below&lt;/b&gt; buffer &amp; more" in html
    # phone layout and dark mode
    assert 'name="viewport"' in html and "prefers-color-scheme: dark" in html and "max-width:600px" in html
    # only the opening sentences; why / connects / sources are left to the app
    assert "Third sentence." in html and "Fourth sentence" not in html
    assert "Do two." in html and "Do three" not in html
    assert "Moved by: Apex ₹1.24 Cr · Nexus ₹1.16 Cr" in html and "Third ₹" not in html
    # deep links go to the client's own app
    assert 'href="https://cfo.client.test/digests/%d"' % digest.id in html
    assert "https://cfo.client.test/ask?digest=%d" % digest.id in html
    item_ids = [i.id for i in db_session.query(DigestItem).filter_by(digest_id=digest.id)]
    assert all(f"/digests/{digest.id}#item-{i}" in html for i in item_ids)
    assert "Continuing since W26" in html and "New this week" in html
    assert "At a glance" in html  # 3 items


def test_text_part_has_the_same_content_unescaped_and_no_markup(db_session, digest):
    text = mail.render_digest_text(db_session, digest)
    assert "Cash <b>below</b> buffer & more" in text  # plain text is not HTML-escaped
    assert "<" not in text.replace("<b>below</b>", "")
    assert "RED LINE · Continuing since W26" in text and f"https://cfo.client.test/digests/{digest.id}" in text


def test_empty_briefing_says_so(db_session):
    wv = db_session.query(WeightingVersion).first()
    d = Digest(week="2026-W28", weighting_version_id=wv.id, headline="Nothing", empty_bar=True)
    db_session.add(d)
    db_session.commit()
    assert "Nothing met the surfacing bar" in mail.render_digest_html(db_session, d)
    assert "At a glance" not in mail.render_digest_html(db_session, d)


def test_links_use_the_tenants_own_host(tmp_path, monkeypatch):
    cfg = tmp_path / "t.yaml"
    cfg.write_text("tenants:\n  - id: acme\n    database_url: sqlite:///x.db\n    hosts: [acme.cfo.test]\n"
                   "  - id: zenith\n    database_url: sqlite:///y.db\n    hosts: [zenith.cfo.test]\n")
    monkeypatch.setenv("TENANTS_FILE", str(cfg))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://cfo.example")
    get_settings.cache_clear()
    _load.cache_clear()
    try:
        with tenant_context("zenith"):
            assert mail.app_base_url() == "https://zenith.cfo.test"
        with tenant_context("acme"):
            assert mail.app_base_url() == "https://acme.cfo.test"
    finally:
        monkeypatch.delenv("TENANTS_FILE")
        get_settings.cache_clear()
        _load.cache_clear()


def test_smtp_message_carries_plain_and_html_parts(db_session, digest, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("EMAIL_TO", "cfo@client.test")
    get_settings.cache_clear()
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, sender, to, body): sent.append((to, body))

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    mail.send_digest_email(db_session, digest)
    assert digest.email_status == "sent"
    (to, body), = sent
    assert to == ["cfo@client.test"]

    from email import message_from_string
    from email.header import decode_header, make_header

    msg = message_from_string(body)
    assert str(make_header(decode_header(msg["Subject"]))) == "CFO briefing · Week 28: Cash slips below the buffer"
    parts = msg.get_payload()
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]  # clients show the last they understand
    assert "RED LINE" in parts[0].get_payload(decode=True).decode("utf-8")
    assert "<table" in parts[1].get_payload(decode=True).decode("utf-8")


def test_alert_email_shows_figures_not_a_dict_dump(db_session):
    level = Alert(rule_id="RL-1", episode_key="RL-1:2026-07-12", week="2026-W28", as_of_date=date(2026, 7, 12),
                  title="Cash below buffered covenant of ₹22 Cr", description="d",
                  evidence_json={"metric": "cash_balance_cr", "value": 21.4, "threshold": 22.0, "lineage": ["x"]},
                  synthesis_json={"narrative": "Cash closed below the buffer."})
    event = Alert(rule_id="RL-2", episode_key="RL-2:2026-07-12", week="2026-W28", as_of_date=date(2026, 7, 12),
                  title="Any new GST show-cause notice", description="d",
                  evidence_json={"metric": None, "value": 1.0, "threshold": None,
                                 "events": [{"business_key": "GST-SCN-1", "amount_paise": 3_20_00_00_000}]})
    db_session.add_all([level, event])
    db_session.commit()
    html = mail.render_alert_html(db_session, level)
    assert "₹21.4 Cr" in html and "₹22 Cr" in html and "Cash closed below the buffer." in html
    assert "{'" not in html and "lineage" not in html  # no raw dict
    text = mail.render_alert_text(db_session, event)
    assert "GST-SCN-1 · ₹3.20 Cr" in text and "Open in the app: " in text
