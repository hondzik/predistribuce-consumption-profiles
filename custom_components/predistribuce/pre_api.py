"""Klient pro portál PREdistribuce: přihlášení + stažení profilu spotřeby.

Testovatelné samostatně, bez Home Assistantu:
    PRE_USER=mail@example.cz PRE_PASS=heslo PRE_EAN=859182400306885078 \
        python pre_api.py --date-from 16.09.2026 --date-to 17.09.2026

Přihlašovací údaje se čtou jen z env proměnných PRE_USER / PRE_PASS,
nikdy z argumentů příkazové řádky ani ze zdrojáku.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from dataclasses import dataclass

import requests

BASE = "https://www.predistribuce.cz"
LOGIN_URL = f"{BASE}/cs/muj-ucet/prihlaseni-uzivatele/"
REPORT_URL = f"{BASE}/cs/muj-ucet/profily-spotreby/"
SELECT_EAN_URL = f"{BASE}/com/PREdi/ClientPortal/MyAccount/ConsumptionProfiles:ajaxSelectEan"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/128.0"


class LoginError(RuntimeError):
    pass


@dataclass
class IntervalReading:
    start: dt.datetime
    end: dt.datetime
    consumption_kwh: float | None
    power_kw: float | None


def login(session: requests.Session, user: str, password: str) -> None:
    """Přihlásí session do portálu. Vyhodí LoginError při neúspěchu.

    Server nevynucuje CSRF token ani reCAPTCHU (ověřeno) — může se to
    kdykoli změnit, proto se úspěch ověřuje přítomností „Odhlásit" v HTML.
    """
    session.get(LOGIN_URL, timeout=30)
    resp = session.post(
        LOGIN_URL,
        data={
            "alias": user,
            "password": password,
            "login": "Přihlásit",
            "recaptcha_response": "",
            "redirect": "",
            "_form_": r"PREdi\ClientPortal\LogIn\LogIn::createLogInForm",
        },
        timeout=30,
    )
    resp.raise_for_status()
    if "Odhlásit" not in resp.text:
        raise LoginError("přihlášení neprošlo (v odpovědi chybí 'Odhlásit')")


def _is_ean_selected(html: str, ean: str) -> bool:
    """Zjistí ze stránky sestav, jestli je EAN aktuálně vybraný.

    Checkbox `eans[]` pro daný EAN má atribut `checked="checked"` — hledá se
    v okolí `value="<EAN>"`, protože pořadí atributů v HTML není garantováno.
    """
    m = re.search(rf'name="eans\[\]"[^>]*value="{re.escape(ean)}"|value="{re.escape(ean)}"[^>]*name="eans\[\]"', html)
    if not m:
        return False
    snippet = html[m.start():m.end() + 40]
    return "checked" in snippet


def _select_ean(session: requests.Session, ean: str) -> None:
    """Přepne výběr EAN pro sestavy (signál `ajaxSelectEan`).

    Výběr je uložený u účtu (perzistentně, ne per-session) a toto volání
    je toggle — proto se má volat jen když `_is_ean_selected` vrátí False.
    """
    resp = session.post(
        SELECT_EAN_URL,
        data={"ean": ean},
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
    )
    resp.raise_for_status()


def _set_report_range(session: requests.Session, ean: str, date_from: str, date_to: str) -> None:
    """Nastaví v session rozsah/EAN sestavy (formulář setForm).

    date_from/date_to ve formátu d.m.yyyy (např. 16.09.2026).
    """
    resp = session.post(
        REPORT_URL,
        data={
            "cumulative": "on",
            "eans[]": ean,
            "select-all": "Přidat vše",
            "deselect-all": "Odebrat vše",
            "date_from": date_from,
            "date_to": date_to,
            "show_set": "Zobrazit sestavu",
            "set_name": "",
            "_form_": "setForm",
        },
        timeout=30,
    )
    resp.raise_for_status()
    if "exportCSVForm" not in resp.text:
        debug_path = os.environ.get("PRE_DEBUG_DUMP")
        if debug_path:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(resp.text)
        raise RuntimeError(
            "nastavení sestavy neprošlo (v odpovědi chybí formulář exportCSVForm); "
            f"status={resp.status_code} url={resp.url} "
            f"identifikacni_udaje={'Identifikační údaje' in resp.text} "
            f"frm-setForm={'frm-setForm' in resp.text} "
            f"délka={len(resp.text)}"
            + (f"; dump uložen do {debug_path}" if debug_path else "")
        )


def _export_csv(session: requests.Session, ean: str, date_from: str, date_to: str) -> bytes:
    """Stáhne CSV pro rozsah nastavený předchozím _set_report_range.

    `date_from`/`date_to` (a prázdné `id_formation`) jsou skrytá pole
    formuláře `exportCSVForm` — bez nich server neví, který report
    exportovat, a vrátí zpět konfigurační stránku místo CSV.
    """
    resp = session.post(
        REPORT_URL,
        data={
            "eans_to_export[]": ean,
            "cinna[]": ean,
            "induktivni[]": ean,
            "kapacitni[]": ean,
            "cumulative2": "on",
            "date_from": date_from,
            "date_to": date_to,
            "id_formation": "",
            "export_CSV": "Exportovat CSV",
            "_form_": "exportCSVForm",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


MOJE_ODBERNA_MISTA_URL = f"{BASE}/cs/muj-ucet/moje-odberna-mista/"


def _extract_label_value(html: str, label: str) -> str | None:
    """Vytažení hodnoty za daným labelem z `/moje-odberna-mista/`.

    Ověřeno živě (2026-09-20) proti reálnému markupu detailu odběrného
    místa — dvojice sesterských `<div class="t-cell ...">`, label
    (s dvojtečkou uvnitř) a hodnota:

        <div class="t-row">
            <div class="t-cell w50">Ulice, č.p. / č.o.:</div>
            <div class="t-cell w50">Mokrá 1282 / 11</div>
        </div>

    Stránka obsahuje tuhle trojici labelů dvakrát („Adresa odběrného
    místa" i „Zasílací adresa" mají stejné labely) — `re.search` vrátí
    první výskyt, což je v pořádku, protože „Adresa odběrného místa"
    (skutečná adresa OM) je v HTML vždy první. dt/dd a th/td patterny se
    nechávají jako fallback pro případ jiného rozvržení stránky.
    """
    patterns = [
        rf'<div[^>]*class="[^"]*t-cell[^"]*"[^>]*>\s*{re.escape(label)}:?\s*</div>\s*<div[^>]*class="[^"]*t-cell[^"]*"[^>]*>\s*(.*?)\s*</div>',
        rf'<dt[^>]*>\s*{re.escape(label)}\s*</dt>\s*<dd[^>]*>\s*(.*?)\s*</dd>',
        rf'<th[^>]*>\s*{re.escape(label)}\s*</th>\s*<td[^>]*>\s*(.*?)\s*</td>',
        rf'{re.escape(label)}\s*:?\s*</[^>]+>\s*(.*?)\s*<',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            value = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if value:
                return value
    return None


@dataclass
class MeteringPoint:
    ean: str
    address: str  # "Ulice ..., Místní část, PSČ město" — best-effort, může být ""


def list_metering_points(session: requests.Session) -> list[MeteringPoint]:
    """Vrátí všechna odběrná místa (EAN + adresa) na přihlášeném účtu.

    EAN se čte spolehlivě ze stránky sestav (checkboxy `eans[]`, ověřeno
    živě). Adresa se čte best-effort ze `/moje-odberna-mista/` podle
    labelů „Ulice, č.p. / č.o.", „Místní část", „PSČ, město" — tohle
    NENÍ ověřeno proti reálnému HTML (viz `_extract_label_value` a
    CLAUDE.md). Když se adresu nepodaří najít, `address` je "" a
    config_flow zobrazí v nabídce jen EAN.
    """
    resp = session.get(REPORT_URL, timeout=30)
    resp.raise_for_status()
    eans = []
    for m in re.finditer(r'name="eans\[\]"[^>]*value="(\d+)"|value="(\d+)"[^>]*name="eans\[\]"', resp.text):
        eans.append(m.group(1) or m.group(2))

    addresses: dict[str, str] = {}
    try:
        detail_resp = session.get(MOJE_ODBERNA_MISTA_URL, timeout=30)
        detail_resp.raise_for_status()
        for ean in eans:
            idx = detail_resp.text.find(ean)
            if idx == -1:
                continue
            window = detail_resp.text[max(0, idx - 200):idx + 3000]
            parts = [
                _extract_label_value(window, "Ulice, č.p. / č.o."),
                _extract_label_value(window, "Místní část"),
                _extract_label_value(window, "PSČ, město"),
            ]
            addresses[ean] = ", ".join(p for p in parts if p)
    except requests.RequestException:
        pass  # adresa je jen pro zobrazení v UI, EAN samotný stačí k fungování

    return [MeteringPoint(ean=ean, address=addresses.get(ean, "")) for ean in eans]


def fetch_report_csv(
    session: requests.Session, ean: str, date_from: str, date_to: str
) -> bytes:
    """Vrátí syrové CSV (bytes, CP1250) pro daný EAN a rozsah dat.

    Výběr EANu (`ajaxSelectEan`) je perzistentní u účtu, ne per-session —
    proto se nejdřív GETem zjistí aktuální stav a `ajaxSelectEan` (toggle)
    se zavolá jen když EAN ještě není vybraný.
    """
    resp = session.get(REPORT_URL, timeout=30)
    resp.raise_for_status()
    if not _is_ean_selected(resp.text, ean):
        _select_ean(session, ean)
    _set_report_range(session, ean, date_from, date_to)
    return _export_csv(session, ean, date_from, date_to)


def parse_csv(raw: bytes) -> list[IntervalReading]:
    """Rozparsuje CSV export (CP1250, ';', desetinná čárka, 15min intervaly).

    Formát sloupců je stabilní bez ohledu na to, jestli je hlavička
    EAN-specifická (`<EAN> - Činná - spotřeba [kWh]`) nebo obecná
    (`Spotřeba [kWh]`) — vždy: začátek;konec;spotřeba;výkon.
    """
    text = raw.decode("cp1250")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    readings: list[IntervalReading] = []
    for line in lines[1:]:  # první řádek je hlavička
        cols = line.split(";")
        if len(cols) < 4:
            continue
        start = dt.datetime.strptime(cols[0].strip(), "%d.%m.%Y %H:%M")
        end = dt.datetime.strptime(cols[1].strip(), "%d.%m.%Y %H:%M")
        consumption = _parse_decimal(cols[2])
        power = _parse_decimal(cols[3])
        readings.append(IntervalReading(start, end, consumption, power))
    return readings


def _parse_decimal(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    return float(raw.replace(",", "."))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    yesterday = dt.date.today() - dt.timedelta(days=1)
    day_before = yesterday - dt.timedelta(days=1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ean", default=os.environ.get("PRE_EAN"))
    parser.add_argument("--date-from", default=day_before.strftime("%d.%m.%Y"))
    parser.add_argument("--date-to", default=yesterday.strftime("%d.%m.%Y"))
    parser.add_argument("--out", help="uložit syrové CSV do souboru (CP1250)")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    user = os.environ.get("PRE_USER")
    password = os.environ.get("PRE_PASS")
    if not user or not password:
        print("chyba: nastav env proměnné PRE_USER a PRE_PASS", file=sys.stderr)
        return 1
    if not args.ean:
        print("chyba: chybí --ean nebo env PRE_EAN", file=sys.stderr)
        return 1

    session = requests.Session()
    session.headers["User-Agent"] = UA

    login(session, user, password)
    raw = fetch_report_csv(session, args.ean, args.date_from, args.date_to)

    if args.out:
        with open(args.out, "wb") as f:
            f.write(raw)

    readings = parse_csv(raw)
    print(f"načteno {len(readings)} intervalů ({args.date_from} - {args.date_to})")
    for r in readings[:5]:
        print(f"  {r.start} - {r.end}: {r.consumption_kwh} kWh, {r.power_kw} kW")
    if len(readings) > 5:
        print("  ...")
    return 0


if __name__ == "__main__":  # pragma: no cover — jen entrypoint, testuje se main() přímo
    raise SystemExit(main(sys.argv[1:]))
