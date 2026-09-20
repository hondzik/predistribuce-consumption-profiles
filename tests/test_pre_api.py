"""Testy pro pre_api.py — čistý Python, žádná závislost na Home Assistantu.

`pre_api` se importuje jako samostatný top-level modul (ne přes balíček
`custom_components.predistribuce`, jehož `__init__.py` importuje
`homeassistant.*`) — viz `pyproject.toml` (`tool.pytest.ini_options.pythonpath`).
Síťové volání (`requests.Session`) se mockuje přes `requests_mock`.
"""

from __future__ import annotations

import datetime as dt

import pytest
import requests

import pre_api


# ---------------------------------------------------------------------------
# parse_csv / _parse_decimal
# ---------------------------------------------------------------------------


def _csv_bytes(text: str) -> bytes:
    return text.replace("\n", "\r\n").encode("cp1250")


def test_parse_csv_cumulative_header():
    raw = _csv_bytes(
        "Počátek intervalu;Konec intervalu;859182400306885078 - Činná - spotřeba [kWh];859182400306885078 - Činný - výkon [kW]\n"
        "16.09.2026 0:00;16.09.2026 0:15;0,123;0,492\n"
        "16.09.2026 0:15;16.09.2026 0:30;0,045;0,180\n"
    )
    readings = pre_api.parse_csv(raw)
    assert len(readings) == 2
    assert readings[0].start == dt.datetime(2026, 9, 16, 0, 0)
    assert readings[0].end == dt.datetime(2026, 9, 16, 0, 15)
    assert readings[0].consumption_kwh == pytest.approx(0.123)
    assert readings[0].power_kw == pytest.approx(0.492)


def test_parse_csv_general_header_same_columns():
    raw = _csv_bytes(
        "Počátek intervalu;Konec intervalu;Spotřeba [kWh];Výkon [kW]\n"
        "16.09.2026 0:00;16.09.2026 0:15;1,5;2,25\n"
    )
    readings = pre_api.parse_csv(raw)
    assert len(readings) == 1
    assert readings[0].consumption_kwh == 1.5
    assert readings[0].power_kw == 2.25


def test_parse_csv_empty_value_is_none_not_zero():
    raw = _csv_bytes(
        "Počátek intervalu;Konec intervalu;Spotřeba [kWh];Výkon [kW]\n"
        "16.09.2026 0:00;16.09.2026 0:15;;0,000\n"
    )
    readings = pre_api.parse_csv(raw)
    assert readings[0].consumption_kwh is None
    assert readings[0].power_kw == 0.0


def test_parse_csv_zero_consumption_is_zero_float_not_none():
    raw = _csv_bytes(
        "Počátek intervalu;Konec intervalu;Spotřeba [kWh];Výkon [kW]\n"
        "16.09.2026 0:00;16.09.2026 0:15;0,000;0,000\n"
    )
    readings = pre_api.parse_csv(raw)
    assert readings[0].consumption_kwh == 0.0


def test_parse_csv_skips_short_rows_and_blank_lines():
    raw = _csv_bytes(
        "Počátek intervalu;Konec intervalu;Spotřeba [kWh];Výkon [kW]\n"
        "\n"
        "16.09.2026 0:00;16.09.2026 0:15\n"  # míň než 4 sloupce
        "16.09.2026 0:15;16.09.2026 0:30;0,1;0,2\n"
    )
    readings = pre_api.parse_csv(raw)
    assert len(readings) == 1
    assert readings[0].start == dt.datetime(2026, 9, 16, 0, 15)


def test_parse_csv_empty_input():
    assert pre_api.parse_csv(b"") == []


def test_parse_csv_header_only():
    raw = _csv_bytes("Počátek intervalu;Konec intervalu;Spotřeba [kWh];Výkon [kW]\n")
    assert pre_api.parse_csv(raw) == []


def test_parse_decimal_comma_to_dot():
    assert pre_api._parse_decimal("0,123") == pytest.approx(0.123)


def test_parse_decimal_empty_is_none():
    assert pre_api._parse_decimal("") is None
    assert pre_api._parse_decimal("   ") is None


def test_parse_decimal_strips_whitespace():
    assert pre_api._parse_decimal("  1,5 ") == 1.5


# ---------------------------------------------------------------------------
# _is_ean_selected
# ---------------------------------------------------------------------------


def test_is_ean_selected_checked_name_before_value():
    html = '<input type="checkbox" name="eans[]" value="859182400306885078" checked="checked">'
    assert pre_api._is_ean_selected(html, "859182400306885078") is True


def test_is_ean_selected_checked_value_before_name():
    html = '<input type="checkbox" value="859182400306885078" name="eans[]" checked="checked">'
    assert pre_api._is_ean_selected(html, "859182400306885078") is True


def test_is_ean_selected_not_checked():
    html = '<input type="checkbox" name="eans[]" value="859182400306885078">'
    assert pre_api._is_ean_selected(html, "859182400306885078") is False


def test_is_ean_selected_ean_not_present():
    html = '<input type="checkbox" name="eans[]" value="111111111111111111">'
    assert pre_api._is_ean_selected(html, "859182400306885078") is False


def test_is_ean_selected_does_not_match_other_eans_checked_state():
    html = (
        '<input type="checkbox" name="eans[]" value="111111111111111111" checked="checked">'
        '<input type="checkbox" name="eans[]" value="859182400306885078">'
    )
    assert pre_api._is_ean_selected(html, "859182400306885078") is False


# ---------------------------------------------------------------------------
# _extract_label_value
# ---------------------------------------------------------------------------


def test_extract_label_value_dt_dd():
    html = "<dt>Ulice, č.p. / č.o.</dt><dd>Testovací 123</dd>"
    assert pre_api._extract_label_value(html, "Ulice, č.p. / č.o.") == "Testovací 123"


def test_extract_label_value_th_td():
    html = "<th>Místní část</th><td>Centrum</td>"
    assert pre_api._extract_label_value(html, "Místní část") == "Centrum"


def test_extract_label_value_label_colon_value():
    html = "<span>PSČ, město:</span> 100 00 Praha<br>"
    assert pre_api._extract_label_value(html, "PSČ, město") == "100 00 Praha"


def test_extract_label_value_strips_inner_tags():
    html = "<dt>Ulice, č.p. / č.o.</dt><dd><strong>Testovací 123</strong></dd>"
    assert pre_api._extract_label_value(html, "Ulice, č.p. / č.o.") == "Testovací 123"


def test_extract_label_value_not_found_returns_none():
    html = "<p>nic tady není</p>"
    assert pre_api._extract_label_value(html, "Místní část") is None


def test_extract_label_value_empty_match_returns_none():
    html = "<dt>Místní část</dt><dd></dd>"
    assert pre_api._extract_label_value(html, "Místní část") is None


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_success(requests_mock):
    requests_mock.get(pre_api.LOGIN_URL, text="přihlašovací formulář")
    requests_mock.post(pre_api.LOGIN_URL, text="vítejte, Odhlásit")
    session = requests.Session()
    pre_api.login(session, "user@example.cz", "heslo")  # nesmí vyhodit výjimku


def test_login_invalid_credentials_raises_login_error(requests_mock):
    requests_mock.get(pre_api.LOGIN_URL, text="přihlašovací formulář")
    requests_mock.post(pre_api.LOGIN_URL, text="neplatné přihlašovací údaje")
    session = requests.Session()
    with pytest.raises(pre_api.LoginError):
        pre_api.login(session, "user@example.cz", "spatne-heslo")


def test_login_http_error_propagates(requests_mock):
    requests_mock.get(pre_api.LOGIN_URL, text="přihlašovací formulář")
    requests_mock.post(pre_api.LOGIN_URL, status_code=500)
    session = requests.Session()
    with pytest.raises(requests.HTTPError):
        pre_api.login(session, "user@example.cz", "heslo")


def test_login_sends_expected_form_fields(requests_mock):
    requests_mock.get(pre_api.LOGIN_URL, text="ok")
    requests_mock.post(pre_api.LOGIN_URL, text="Odhlásit")
    session = requests.Session()
    pre_api.login(session, "user@example.cz", "heslo123")

    sent = requests_mock.request_history[-1]
    body = sent.text
    assert "alias=user%40example.cz" in body
    assert "password=heslo123" in body
    assert "recaptcha_response=" in body


# ---------------------------------------------------------------------------
# fetch_report_csv (přes _is_ean_selected / _select_ean / _set_report_range / _export_csv)
# ---------------------------------------------------------------------------


EAN = "859182400306885078"


def _report_page(*, checked: bool) -> str:
    checked_attr = ' checked="checked"' if checked else ""
    return f'<input type="checkbox" name="eans[]" value="{EAN}"{checked_attr}>'


def test_fetch_report_csv_selects_ean_when_not_selected(requests_mock):
    requests_mock.get(pre_api.REPORT_URL, text=_report_page(checked=False))
    select_mock = requests_mock.post(pre_api.SELECT_EAN_URL, json={"ok": True})
    requests_mock.post(
        pre_api.REPORT_URL,
        [
            {"text": "...exportCSVForm..."},  # setForm
            {"content": b"csv-bytes"},  # exportCSVForm
        ],
    )
    session = requests.Session()

    raw = pre_api.fetch_report_csv(session, EAN, "16.09.2026", "17.09.2026")

    assert raw == b"csv-bytes"
    assert select_mock.called_once


def test_fetch_report_csv_skips_select_when_already_selected(requests_mock):
    requests_mock.get(pre_api.REPORT_URL, text=_report_page(checked=True))
    select_mock = requests_mock.post(pre_api.SELECT_EAN_URL, json={"ok": True})
    requests_mock.post(
        pre_api.REPORT_URL,
        [
            {"text": "...exportCSVForm..."},
            {"content": b"csv-bytes"},
        ],
    )
    session = requests.Session()

    pre_api.fetch_report_csv(session, EAN, "16.09.2026", "17.09.2026")

    assert not select_mock.called


def test_set_report_range_raises_when_export_form_missing(requests_mock):
    requests_mock.post(pre_api.REPORT_URL, text="jen běžná stránka, žádný formulář pro export")
    session = requests.Session()
    with pytest.raises(RuntimeError, match="exportCSVForm"):
        pre_api._set_report_range(session, EAN, "16.09.2026", "17.09.2026")


def test_set_report_range_writes_debug_dump_when_configured(requests_mock, tmp_path, monkeypatch):
    dump_path = tmp_path / "dump.html"
    monkeypatch.setenv("PRE_DEBUG_DUMP", str(dump_path))
    requests_mock.post(pre_api.REPORT_URL, text="stránka bez formuláře pro export")
    session = requests.Session()

    with pytest.raises(RuntimeError):
        pre_api._set_report_range(session, EAN, "16.09.2026", "17.09.2026")

    assert dump_path.read_text(encoding="utf-8") == "stránka bez formuláře pro export"


def test_export_csv_sends_expected_hidden_fields(requests_mock):
    requests_mock.post(pre_api.REPORT_URL, content=b"raw-csv")
    session = requests.Session()

    raw = pre_api._export_csv(session, EAN, "16.09.2026", "17.09.2026")

    assert raw == b"raw-csv"
    sent = requests_mock.request_history[-1].text
    assert "date_from=16.09.2026" in sent
    assert "date_to=17.09.2026" in sent
    assert f"cinna%5B%5D={EAN}" in sent


# ---------------------------------------------------------------------------
# list_metering_points
# ---------------------------------------------------------------------------


def test_list_metering_points_extracts_eans_and_addresses(requests_mock):
    report_html = (
        f'<input type="checkbox" name="eans[]" value="{EAN}">'
        '<input type="checkbox" name="eans[]" value="111111111111111111">'
    )
    detail_html = (
        f"<p>{EAN}</p>"
        "<dt>Ulice, č.p. / č.o.</dt><dd>Testovací 123</dd>"
        "<dt>Místní část</dt><dd>Centrum</dd>"
        "<dt>PSČ, město</dt><dd>100 00 Praha</dd>"
    )
    requests_mock.get(pre_api.REPORT_URL, text=report_html)
    requests_mock.get(pre_api.MOJE_ODBERNA_MISTA_URL, text=detail_html)
    session = requests.Session()

    points = pre_api.list_metering_points(session)

    assert [p.ean for p in points] == [EAN, "111111111111111111"]
    match = next(p for p in points if p.ean == EAN)
    assert match.address == "Testovací 123, Centrum, 100 00 Praha"
    other = next(p for p in points if p.ean == "111111111111111111")
    assert other.address == ""


def test_list_metering_points_no_eans_returns_empty_list(requests_mock):
    requests_mock.get(pre_api.REPORT_URL, text="<p>žádné checkboxy</p>")
    requests_mock.get(pre_api.MOJE_ODBERNA_MISTA_URL, text="")
    session = requests.Session()
    assert pre_api.list_metering_points(session) == []


def test_list_metering_points_survives_detail_page_failure(requests_mock):
    requests_mock.get(pre_api.REPORT_URL, text=_report_page(checked=True))
    requests_mock.get(pre_api.MOJE_ODBERNA_MISTA_URL, exc=requests.ConnectionError)
    session = requests.Session()

    points = pre_api.list_metering_points(session)

    assert [p.ean for p in points] == [EAN]
    assert points[0].address == ""


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults_to_yesterday_and_day_before():
    yesterday = dt.date.today() - dt.timedelta(days=1)
    day_before = yesterday - dt.timedelta(days=1)
    args = pre_api._parse_args([])
    assert args.date_from == day_before.strftime("%d.%m.%Y")
    assert args.date_to == yesterday.strftime("%d.%m.%Y")
    assert args.ean is None


def test_parse_args_explicit_values_override_defaults():
    args = pre_api._parse_args(
        ["--ean", EAN, "--date-from", "01.01.2026", "--date-to", "02.01.2026"]
    )
    assert args.ean == EAN
    assert args.date_from == "01.01.2026"
    assert args.date_to == "02.01.2026"


def test_parse_args_reads_ean_from_env(monkeypatch):
    monkeypatch.setenv("PRE_EAN", EAN)
    args = pre_api._parse_args([])
    assert args.ean == EAN


# ---------------------------------------------------------------------------
# main — jen validace vstupů (žádné síťové volání)
# ---------------------------------------------------------------------------


def test_main_fails_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("PRE_USER", raising=False)
    monkeypatch.delenv("PRE_PASS", raising=False)
    assert pre_api.main(["--ean", EAN]) == 1
    assert "PRE_USER" in capsys.readouterr().err


def test_main_fails_without_ean(monkeypatch, capsys):
    monkeypatch.setenv("PRE_USER", "user@example.cz")
    monkeypatch.setenv("PRE_PASS", "heslo")
    monkeypatch.delenv("PRE_EAN", raising=False)
    assert pre_api.main([]) == 1
    assert "--ean" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main — celý běh (login + fetch_report_csv + parse_csv), síť mockovaná
# ---------------------------------------------------------------------------


def _mock_full_flow(requests_mock, *, readings: int) -> bytes:
    """Namockuje login + fetch_report_csv a vrátí odpovídající syrové CSV."""
    requests_mock.get(pre_api.LOGIN_URL, text="přihlašovací formulář")
    requests_mock.post(pre_api.LOGIN_URL, text="vítejte, Odhlásit")
    requests_mock.get(pre_api.REPORT_URL, text=_report_page(checked=True))

    lines = ["Počátek intervalu;Konec intervalu;Spotřeba [kWh];Výkon [kW]"]
    for i in range(readings):
        start = dt.datetime(2026, 9, 16) + dt.timedelta(minutes=15 * i)
        end = start + dt.timedelta(minutes=15)
        lines.append(f"{start:%d.%m.%Y %H:%M};{end:%d.%m.%Y %H:%M};0,100;0,400")
    raw = _csv_bytes("\n".join(lines) + "\n")

    requests_mock.post(
        pre_api.REPORT_URL,
        [
            {"text": "...exportCSVForm..."},  # setForm
            {"content": raw},  # exportCSVForm
        ],
    )
    return raw


def _set_main_env(monkeypatch) -> None:
    monkeypatch.setenv("PRE_USER", "user@example.cz")
    monkeypatch.setenv("PRE_PASS", "heslo")
    monkeypatch.delenv("PRE_EAN", raising=False)


def test_main_downloads_and_prints_readings(requests_mock, monkeypatch, capsys):
    _set_main_env(monkeypatch)
    _mock_full_flow(requests_mock, readings=2)

    result = pre_api.main(
        ["--ean", EAN, "--date-from", "16.09.2026", "--date-to", "16.09.2026"]
    )

    assert result == 0
    out = capsys.readouterr().out
    assert "načteno 2 intervalů" in out
    assert "..." not in out


def test_main_prints_ellipsis_when_more_than_five_readings(
    requests_mock, monkeypatch, capsys
):
    _set_main_env(monkeypatch)
    _mock_full_flow(requests_mock, readings=6)

    result = pre_api.main(
        ["--ean", EAN, "--date-from", "16.09.2026", "--date-to", "16.09.2026"]
    )

    assert result == 0
    out = capsys.readouterr().out
    assert "načteno 6 intervalů" in out
    assert "..." in out


def test_main_writes_raw_csv_to_out_file(requests_mock, monkeypatch, tmp_path):
    _set_main_env(monkeypatch)
    raw = _mock_full_flow(requests_mock, readings=1)
    out_file = tmp_path / "export.csv"

    result = pre_api.main(
        [
            "--ean",
            EAN,
            "--date-from",
            "16.09.2026",
            "--date-to",
            "16.09.2026",
            "--out",
            str(out_file),
        ]
    )

    assert result == 0
    assert out_file.read_bytes() == raw
