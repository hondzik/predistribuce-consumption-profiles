# PREdistribuce → Home Assistant: profil spotřeby

Cíl: stáhnout z portálu PREdistribuce naměřená data o spotřebě za předchozí den
a naimportovat je do Home Assistantu (Energy dashboard) jako dlouhodobé statistiky.

## Stav: `pre_api.py` FUNGUJE end-to-end (login → sestava → CSV → parser)

Ověřeno `2026-09-18`: `python3 pre_api.py --date-from 16.09.2026 --date-to 17.09.2026`
načte 192 intervalů (2 dny × 96 čtvrthodin) — čísla odpovídají.

---

## Co je ověřené

### Přihlášení — FUNGUJE

Nette formulář na `https://www.predistribuce.cz/cs/muj-ucet/prihlaseni-uzivatele/`,
metoda POST, **žádný CSRF token**. Session drží na cookie.

| pole | hodnota |
|---|---|
| `alias` | e-mail / číslo zákazníka |
| `password` | heslo |
| `login` | `Přihlásit` (submit, musí se poslat) |
| `redirect` | prázdné |
| `recaptcha_response` | prázdné — **server ho nevynucuje** (ověřeno) |
| `_form_` | `PREdi\ClientPortal\LogIn\LogIn::createLogInForm` |

Po úspěchu: HTTP 200, redirect na `/cs/muj-ucet/`, v HTML je „Odhlásit".
Viz `pre_test.sh` (kroky 1–2) — ten je funkční, používat jako výchozí bod.

Pozn.: reCAPTCHA v HTML je, jen se neověřuje. Může se to kdykoli změnit →
integrace musí po loginu kontrolovat přítomnost „Odhlásit" a při selhání
vyhodit `ConfigEntryAuthFailed`.

### Dotaz na sestavu — FUNGUJE

**Kořen původního problému:** špatná URL. Portál používá plurál
`/cs/muj-ucet/profily-spotreby/`, ne `/cs/muj-ucet/profil-spotreby/` (singulár,
jak jsme zkoušeli původně). Na singulár server tiše odklání na
„Můj účet / Identifikační údaje" — proto se zdálo, že formulář nefunguje,
i když payload byl od začátku správně.

Ověřeno živě (2026-09-18) přes Chrome DevTools/fetch v autentizované session:

**1. Nastavení sestavy** — POST na `/cs/muj-ucet/profily-spotreby/`,
formulář `setForm` (přesně payload, co byl už v `pre_test.sh`, jen na
správnou URL):

```
cumulative=on&eans%5B%5D=<EAN>&select-all=P%C5%99idat+v%C5%A1e&deselect-all=Odebrat+v%C5%A1e&date_from=16.09.2026&date_to=17.09.2026&show_set=Zobrazit+sestavu&set_name=&_form_=setForm
```

Odpověď: HTTP 200, HTML obsahuje sestavu („Online") a formulář `exportCSVForm`.
Žádný CSRF token.

**Důležitá záludnost — výběr EAN (`ajaxSelectEan`) je toggle uložený u
ÚČTU, ne per-session.** Checkbox `eans[]` na `GET /profily-spotreby/` je
`checked="checked"`, pokud byl EAN vybraný při posledním volání (z
libovolné předchozí session/prohlížeče/skriptu) — není to výchozí stav
formuláře. Když EAN vybraný není, `setForm` POST tiše neuspěje (v odpovědi
chybí `exportCSVForm`, HTML vypadá jako běžná stránka). Proto se musí:
1. GETnout `/profily-spotreby/` a regexem zjistit, jestli je checkbox pro
   náš EAN `checked` (`_is_ean_selected` v `pre_api.py`).
2. Jen když NENÍ, zavolat `ajaxSelectEan` (endpoint
   `/com/PREdi/ClientPortal/MyAccount/ConsumptionProfiles:ajaxSelectEan`,
   POST `ean=<EAN>`, hlavičky `Content-Type: application/x-www-form-urlencoded; charset=UTF-8`
   a `Accept: application/json, text/javascript, */*; q=0.01` — bez nich
   volání vrátí HTTP 200, ale výběr se tiše nepropíše).
   Nikdy volat nepodmíněně — je to toggle, opakované volání by EAN zase
   odselektovalo.

**2. Export CSV** — POST na **stejnou** URL `/cs/muj-ucet/profily-spotreby/`,
formulář `exportCSVForm`:

```
eans_to_export%5B%5D=<EAN>&cinna%5B%5D=<EAN>&induktivni%5B%5D=<EAN>&kapacitni%5B%5D=<EAN>&cumulative2=on&date_from=<stejné jako v setForm>&date_to=<stejné jako v setForm>&id_formation=&export_CSV=Exportovat+CSV&_form_=exportCSVForm
```

**Kriticky důležité:** `exportCSVForm` má skrytá pole `date_from`, `date_to`
a `id_formation` — bez nich server neví, který report exportovat, a vrátí
zpět konfigurační stránku (HTML) místo CSV. Datum/rozsah se tedy MUSÍ
poslat znovu, i když byl už jednou v `setForm` — portál ho nedrží čistě
server-side, echoje si ho zpátky přes tato skrytá pole formuláře.

Odpověď: HTTP 200, tělo je přímo CSV (žádný mezikrok s odkazem — potvrzeno).
Pole `cinna[]` / `induktivni[]` / `kapacitni[]` odpovídají složkám el.
energie (Činná/Induktivní/Kapacitní) — pro spotřebu k HA stačí `cinna[]`.

**Pořadí kroků nutné pro automatizaci** (implementováno v `fetch_report_csv`):
1. Login (`prihlaseni-uzivatele`, beze změny).
2. GET `/cs/muj-ucet/profily-spotreby/` — inicializace + zjištění stavu výběru EAN.
3. Podmíněně `ajaxSelectEan`, pokud EAN není vybraný (viz výše).
4. POST `setForm` s `date_from`/`date_to`/`eans[]` — nastaví rozsah sestavy.
5. POST `exportCSVForm` se STEJNÝM `date_from`/`date_to` — vrátí CSV tělo přímo v response.

---

## Formát CSV (ověřeno na reálném exportu)

- Kódování **CP1250** (Windows-1250), řádky CRLF.
- Oddělovač `;`, desetinná čárka.
- Interval 15 minut. Sloupce (při `cumulative`/`cumulative2=on`, 1 EAN):
  `Počátek intervalu;Konec intervalu;<EAN> - Činná - spotřeba [kWh];<EAN> - Činný - výkon [kW]`
- Bez `cumulative` (obecný název sloupců, ne EAN-specifický):
  `Počátek intervalu;Konec intervalu;Spotřeba [kWh];Výkon [kW]`
- Časy ve formátu `d.m.yyyy H:MM`, lokální čas (ne UTC) — zacházení s
  přechodem letní/zimní čas ještě NEOVĚŘENO (žádný přechod v testovacích
  datech).
- **Spotřeba (kWh) a výkon (kW) se plní s různým zpožděním — NEJSOU nulové
  současně.** Ověřeno `18.09.2026` v ~10:00 dotazem na `--date-from
  18.09.2026 --date-to 18.09.2026`:
  - sloupec **spotřeba** je `0,000` za celý dnešní den, i za hodiny dávno
    minulé (00:00–10:00) — vypadá, že se plní až po uzavření celého dne
    (odpovídá hypotéze, že elektroměr odesílá naměřená data 1× denně,
    pravděpodobně krátce po půlnoci; přesný okamžik uzavření dne ještě
    NEOVĚŘEN, jen že v 10:00 ještě nic není).
  - sloupec **výkon** má reálné nenulové hodnoty až do aktuálního času
    (do ~10:00), pak taky spadne na `0,000` (řádky bez dat vůbec).
  - **Důsledek pro `coordinator.py`:** výkon NELZE použít jako indikátor
    „jsou už data kompletní" — jen sloupec spotřeba. Nejjednodušší a
    bezpečné pravidlo: nikdy nežádat/importovat aktuální den, vždy jen dny
    striktně před dneškem (lokální datum). Případně navíc ověřit
    `spotřeba > 0` alespoň pro poslední interval dne před importem.

---

## Identifikátory účtu

Doplnit z portálu / z dumpu `/cs/muj-ucet/` (nekomitovat veřejně):

```
EAN     = 859182400306885078
OM      = 8110311219
vertrag = ...
anlage  = ...
vkont   = ...
```

Přihlašovací údaje **nikdy do tohoto souboru** — přes env proměnné
(`PRE_USER`, `PRE_PASS`).

---

## Další kroky

1. ~~Zjistit správnou URL/payload sestavy~~ — HOTOVO, viz výše.
2. ~~Zjistit formát CSV~~ — HOTOVO, viz výše.
3. ~~Napsat `pre_api.py`~~ — HOTOVO, ověřeno end-to-end (192 intervalů za 2 dny).
4. **`-A` (dodávka do sítě) — BLOKOVÁNO, nelze ověřit dokud nebude FVE.**
   Účet má jedno OM, typ měření `C`, kategorie `3` — čistá spotřeba, žádná
   výroba (ověřeno na `/moje-odberna-mista/`). Formulář sestav má filtr
   Typ „Spotřeba/Výroba" jen pro vyhledávání jiných OM, ne pole exportu.
   Po instalaci FVE bude nejspíš nutné nové/přeregistrované OM typu
   „Výroba" a zjištění formátu jeho exportu znovu od začátku.
5. **Přesný okamžik uzavření dne — NELZE spolehlivě zjistit** (portál
   nepublikuje SLA/timestamp poslední aktualizace, nejde to zpětně
   dohledat). Řešení: `coordinator.py` nebude spoléhat na konkrétní čas —
   po naplánovaném stažení zvaliduje, jestli je `spotřeba` za celý
   požadovaný den nenulová; pokud je celá `0` (den ještě neuzavřen),
   import se přeskočí a zkusí znovu při dalším běhu / manuálně přes
   tlačítko reimportu. Potvrzeno: `18.09.2026` v 10:00 byla spotřeba za
   dnešek celá `0`, výkon byl reálný jen do aktuálního času (viz sekce
   Formát CSV výše) — spotřeba a výkon se plní s různým zpožděním, výkon
   NENÍ spolehlivý indikátor uzavření dne.
6. **Přechod letní/zimní čas — NELZE ověřit teď, ODLOŽENO na `25.10.2026`.**
   Historická data jsou dostupná jen od `24.08.2026`, poslední přechod
   (na letní čas) byl v březnu, mimo dostupný rozsah; příští (na zimní čas)
   je až `25.10.2026`, ještě nenastal. Očekávané chování k ověření tehdy:
   - přechod na zimní čas (`25.10.2026`): den má 25 h → 100 intervalů,
     hodina `2:00–3:00` se v CSV pravděpodobně objeví dvakrát (nejdřív
     ještě CEST, pak CET) se stejným textovým labelem — nutné rozlišit
     přes `zoneinfo.ZoneInfo("Europe/Prague")` a `fold=0/1`, ne naivním
     parsováním.
   - (pro budoucí referenci, přechod na letní čas): den má 23 h →
     92 intervalů, hodina `2:00–3:00` v CSV chybí úplně.
   - Do té doby implementovat converzi na UTC v `coordinator.py` přes
     `zoneinfo`, ale explicitně OZNAČIT jako neověřené a naplánovat
     rychlou kontrolu hned `25.–26.10.2026`.
7. ~~Napsat `coordinator.py`~~ — HOTOVO (kód napsán, syntax ověřen; logika
   NEOVĚŘENÁ proti reálnému HA — `homeassistant.*` tady nejde importovat,
   nutné vyzkoušet až v běžící instanci).
8. ~~Napsat `config_flow.py`, `manifest.json`, `__init__.py`~~ — HOTOVO,
   kód napsán a syntax ověřen (`ast.parse`). Logika proti živému
   portálu/HA NEOVĚŘENA (nejde importovat `homeassistant.*` lokálně).
   - `config_flow.py`: krok `user` (přihlášení + `pre_api.list_
     metering_points`) → krok `eans` (checkboxy odběrných míst + čas
     importu, validace "vybráno alespoň jedno"). Options flow je menu:
     "schedule" (změna času, heslo se nezadává znovu) a
     "metering_points" (přidání dalšího odběrného místa později —
     přihlašovací údaje se znovu použijí z `entry.data`).
     `async_step_reauth`/`async_step_reauth_confirm` doplněno (řeší
     `ConfigEntryAuthFailed` z `coordinator.py` — nabídne jen nové heslo,
     username zůstává, přes `async_update_reload_and_abort`).
   - `manifest.json`: `domain=predistribuce`, `integration_type=hub`,
     `iot_class=cloud_polling`, `requirements=["requests"]`.
     `codeowners=["@hondzik"]`, `documentation` ukazuje na
     `github.com/hondzik/predistribuce-consumption-profiles` — DOPLNĚNO
     (projekt teď žije v repozitáři `hondzik/predistribuce-consumption-
     profiles`, jeden commit `Initial commit` s `LICENSE`, zbytek souborů
     zatím untracked, ještě chybí `.gitignore`).
   - `__init__.py`: `async_setup_entry` vytvoří coordinator, uloží do
     `entry.runtime_data`, zavolá `async_config_entry_first_refresh()`
     (dotáhne zameškaný den po výpadku HA, nečeká na další naplánovaný
     čas) a forwarduje na `button` platformu.
   - **Adresa odběrného místa je NEOVĚŘENÁ.** `pre_api.list_metering_
     points`/`_extract_label_value` parsují `/moje-odberna-mista/` podle
     labelů „Ulice, č.p. / č.o.", „Místní část", „PSČ, město" naslepo
     (bez živého HTML dumpu s tagy) — zkouší víc obvyklých tvarů (dt/dd,
     th/td, "label: hodnota"), ale žádný nebyl ověřen proti reálné
     stránce. Když nesedí, degraduje na zobrazení jen EAN (funkčně OK,
     jen bez popisku). Ověřit živě při prvním testu config_flow.
   - **Budoucí tlačítko pro manuální import historických dat** (zmíněné
     dřív v konverzaci, ještě nenapsané) musí umožnit vybrat KONKRÉTNÍ
     odběrné místo/EAN (a rozsah dat) — na rozdíl od `button.py`
     (`async_retry_pending`), který jede přes všechny EANy v `_pending`
     najednou. Nepřidávat EAN-selektor do stávajícího retry-tlačítka,
     je to jiný use-case (plánovaný reimport vs. ruční historický import).
9. ~~`strings.json` + `translations/cs.json`~~ — HOTOVO (anglicky jako
   zdroj/fallback v `strings.json`, česky v `translations/cs.json`;
   `button.py` teď používá `_attr_translation_key = "retry_pending"`
   místo natvrdo zapsaného textu, aby šlo přes lokalizaci jako zbytek
   integrace). JSON syntax ověřena.
10. **Nasazení a test proti reálné HA instanci — DALŠÍ KROK.** K
   dispozici je teď HA MCP server (`mcp__home-assistant__*`) napojený na
   uživatelovu instanci (HAOS/Supervisor, verze `2026.9.2`, `predistribuce`
   integrace tam ještě NENÍ nainstalovaná — ověřeno). **Důležité omezení:**
   `ha_write_file`/`ha_read_file` NEUMÍ zapisovat do `custom_components/`
   (jen `.py` čtení je povoleno, zápis vůbec) — ověřeno živě (`ha_write_file`
   na `custom_components/predistribuce/manifest.json` selhalo na
   `BACKUP_CAPTURE_FAILED`/"Path not allowed"). Nasazení souborů integrace
   (`__init__.py`, `manifest.json`, `const.py`, `pre_api.py`,
   `coordinator.py`, `button.py`, `config_flow.py`) do
   `/config/custom_components/predistribuce/` musí udělat UŽIVATEL sám
   (Samba/Studio Code Server/SCP apod.) — pak restart a projít config_flow
   v HA UI (heslo tam MUSÍ zadat uživatel, nikdy ne asistent). Po nasazení
   lze přes MCP server ověřit: `ha_get_logs` (chyby při setupu/importu),
   `ha_get_integration` (stav config entry), `ha_get_state`/`ha_search`
   (vzniklé entity/statistiky) — a doladit `-A` adresu, reauth flow atd.
   podle toho, co se v logu objeví.
11. ~~Repozitář připraven pro GitHub/HACS~~ — HOTOVO (`2026-09-18`).
   - **Restrukturalizováno pro HACS**: veškerý kód integrace (`__init__.py`,
     `manifest.json`, `const.py`, `pre_api.py`, `coordinator.py`,
     `button.py`, `config_flow.py`, `strings.json`, `translations/`)
     přesunut z rootu do `custom_components/predistribuce/` — HACS/HA to
     jinak nenajde. Zároveň opraveny všechny vnitřní importy z absolutních
     (`from coordinator import ...`, `import pre_api`) na relativní
     (`from .coordinator import ...`, `from . import pre_api`) — bez toho
     by integrace v balíčkové struktuře vůbec nenaběhla (import error hned
     při startu HA). Toto NEBYLO odhaleno dřívějším `ast.parse` syntax
     checkem, protože ten kontroluje jen syntax, ne resolvovatelnost
     importů v balíčku — ověřit znovu při prvním živém testu (bod 10).
   - `manifest.json` doplněno o `"version": "0.1.0"` (HACS to vyžaduje) a
     `issue_tracker`.
   - `hacs.json` (root) — `render_readme: true`, `homeassistant:
     "2024.6.0"` (minimální verze kvůli `entry.runtime_data`).
   - `README.md` (anglicky, primární) + `README.cs.md` (česky), vzájemné
     odkazy nahoře. Obrázky jsou jen placeholdery (`docs/images/*.png` v
     `docs/images/.gitkeep`, samotné PNG soubory ještě NEEXISTUJÍ) —
     DOPLNIT až budou reálné screenshoty z běžící instance.
   - `.github/workflows/validate.yml` — `hassfest`, `hacs/action`
     (category `integration`) a `python -m compileall` na push/PR/týdně/
     manuálně.
   - `.github/workflows/release.yml` + `release-please-config.json` +
     `.release-please-manifest.json` — automatické sémantické verzování
     (`release-please`, `release-type: simple`) podle Conventional Commits
     na `main`; při mergi „release PR" zvýší `manifest.json`'s `version`
     (přes `extra-files`/`jsonpath`) i `CHANGELOG.md` a vytvoří tag +
     GitHub Release. **Vyžaduje, aby commity na `main` používaly
     Conventional Commits formát (`feat:`, `fix:`, `chore:`, ...)** — jinak
     release-please nepozná, že má verzi zvednout.
   - `.gitignore` už dřív ošetřil `.mcp.json` (obsahuje privátní token k HA
     MCP serveru) a testovací CSV s reálným EAN/spotřebou
     (`raw_export.csv`, `today.csv`, `Profily_spotreby_*.csv`) —
     nekomitovat.
   - **NEUDĚLÁNO/nevyžádáno**: žádný `CONTRIBUTING.md`, issue/PR šablony,
     `CODEOWNERS` ani `CHANGELOG.md` (ten vytvoří až první běh
     `release-please`) — nebylo explicitně požadováno, nedoplňovat bez
     domluvy.

## Cílová architektura

- `pre_api.py` — login + stažení + parser, testovatelné bez HA
- `coordinator.py` — `DataUpdateCoordinator`, běh 1× denně v čase, který si
  uživatel nastaví v config entry (options); `async_import_range()` jako
  sdílená metoda pro denní běh i manuální (re)import.
- **Chybějící/neuzavřená data:** pokud `async_import_range` narazí na den
  s celou nulovou spotřebou, NEhlásí chybu — zapamatuje si ho v
  `coordinator._pending[ean]` a vytvoří `persistent_notification`
  (Nastavení → Notifikace) s výpisem, co chybí. `persistent_notification`
  neumí klikací tlačítko uvnitř sebe (to jen přes HA Companion actionable
  notifications, které v tomto projektu nepoužíváme) — proto je navíc
  entita `button.py` „Zkusit znovu stáhnout data" na stránce zařízení
  integrace, která zavolá `coordinator.async_retry_pending()`. Po úspěchu
  se notifikace automaticky zruší (`persistent_notification.async_dismiss`).
- import přes `async_add_external_statistics` (historická data nelze cpát
  přes běžný senzor), hodinová UTC agregace, kumulativní `sum`
- **backfill/reimport `sum`:** `async_add_external_statistics` dělá upsert
  podle `(statistic_id, start)` — lze tedy bezpečně přepisovat i už
  existující hodiny. Pravidlo: `sum` se vždy počítá jako running total od
  nejstaršího bodu, kterého se import dotýká, dál dopředu (ne izolovaně pro
  nový úsek) — jinak vznikne na hranici skok. Pro čistě první backfill
  (žádná data pro `statistic_id` ještě neexistují) je to degenerovaný
  případ: baseline `sum = 0` na nejstarším řádku.
- **přihlašovací údaje:** standardní HA cestou — `ConfigFlow` →
  `config_entry.data` (`username`/`password`, v UI `selector: password`),
  persistuje HA core do `.storage/core.config_entries`. Žádné vlastní
  úložiště. Při chybě přihlášení integrace vyhodí `ConfigEntryAuthFailed`
  → HA nabídne reauth flow.
- **naming (víc odběrných míst do budoucna, + `-A` pro FVE):**
  `statistic_id` = `predistribuce:<EAN>_consumption` (`+A`, spotřeba) a
  `predistribuce:<EAN>_return` (`-A`, dodávka do sítě) — anglická slova
  odpovídají terminologii HA Energy dashboardu („Grid consumption" /
  „Return to grid"). Config entry umožní nakonfigurovat víc EANů.
- **název (finalizováno):** HA `domain` (manifest.json, kód) zůstává
  krátké `predistribuce`. Název repozitáře/HACS: `predistribuce-
  consumption-profiles` (přesný anglický překlad názvu funkce portálu
  „Profily spotřeby", odpovídá `_consumption`/`_return` konvenci výše).
  Termín „metering point" (odběrné místo) zůstává jen jako UI slovník
  (např. popisky v `config_flow.py`), ne jako název repa.
- inspirace: https://github.com/igracek/HACS_CEZD_PND (totéž pro ČEZ PND)

## Poznámky

- Existující PRE integrace ([slesinger](https://github.com/slesinger/homeassistant-predistribuce),
  [gadamcz](https://github.com/gadamcz/homeassistant-predistribuce)) řeší jen HDO, spotřebu ne.
- Zvážit alternativu: lokální měření (smart meter střídače FVE, Shelly Pro 3EM).
  Data v reálném čase, bez scrapingu, bez závislosti na portálu.
- Ověřit podmínky užití portálu ohledně automatizovaného stahování.
