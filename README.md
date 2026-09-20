# PREdistribuce — Consumption Profiles

[Čeština / Czech version](README.cs.md)

[![GitHub Release](https://img.shields.io/github/release/hondzik/predistribuce-consumption-profiles.svg?style=for-the-badge)](https://github.com/hondzik/predistribuce-consumption-profiles/releases)
[![License](https://img.shields.io/github/license/hondzik/predistribuce-consumption-profiles.svg?style=for-the-badge)](LICENSE)
[![Project Maintenance](https://img.shields.io/badge/maintainer-hondzik-blue.svg?style=for-the-badge)](https://github.com/hondzik)
[![GitHub Activity](https://img.shields.io/github/last-commit/hondzik/predistribuce-consumption-profiles?style=for-the-badge)](https://github.com/hondzik/predistribuce-consumption-profiles/commits/main)

A Home Assistant custom integration that logs into the [PREdistribuce](https://www.predistribuce.cz/) customer portal (the electricity distributor for Prague, Czech Republic), downloads your smart meter's 15-minute consumption profile for the previous day, and imports it into Home Assistant as long-term **external statistics** — so it shows up in the **Energy dashboard** just like a native energy sensor.

![Energy dashboard with imported PREdistribuce consumption](docs/images/energy-dashboard.png)
*(placeholder  — screenshot of the Energy dashboard showing imported consumption)*

## Table of contents

- [PREdistribuce — Consumption Profiles](#predistribuce--consumption-profiles)
  - [Table of contents](#table-of-contents)
  - [How it works](#how-it-works)
  - [Requirements](#requirements)
  - [Installation](#installation)
    - [Via HACS (recommended)](#via-hacs-recommended)
    - [Manual](#manual)
  - [Configuration](#configuration)
    - [Initial setup](#initial-setup)
    - [Adding another metering point later](#adding-another-metering-point-later)
    - [Changing the import schedule](#changing-the-import-schedule)
    - [Manually importing historical data](#manually-importing-historical-data)
  - [Missing / not-yet-closed days](#missing--not-yet-closed-days)
  - [Known limitations](#known-limitations)
  - [Troubleshooting](#troubleshooting)
  - [Versioning \& releases](#versioning--releases)
  - [Disclaimer](#disclaimer)
  - [Credits](#credits)

## How it works

PREdistribuce doesn't offer a public API for consumption data — this integration authenticates against the same customer portal a person would use in a browser (`www.predistribuce.cz`), requests the "Profily spotřeby" (consumption profiles) report for the previous day, and parses the CSV export it returns.

Once a day, at a time you choose, the integration:

1. Logs in and downloads yesterday's 15-minute consumption values for each configured metering point (EAN).
2. Aggregates them into hourly totals and pushes them into Home Assistant's statistics database via `async_add_external_statistics`, so they appear as a long-term statistic (`predistribuce:<EAN>_consumption`) that the Energy dashboard can use as a grid consumption source.
3. If the portal hasn't finished closing out the requested day yet (consumption is still all-zero), the day is skipped and remembered — see [Missing / not-yet-closed days](#missing--not-yet-closed-days).

![Data flow: portal login → CSV export → statistics import](docs/images/data-flow-overview.png)
*(placeholder — simple diagram of portal → integration → Energy dashboard)*

## Requirements

- A Home Assistant instance where you can install custom integrations (HACS or manual copy into `custom_components/`).
- An active PREdistribuce customer-portal account (the same login you use on predistribuce.cz) with at least one consumption metering point.

## Installation

### Via HACS (recommended)

[![Add repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?repository=predistribuce-consumption-profiles&owner=hondzik&category=Integration)

If it isn't in the default HACS store yet, add this repository manually in HACS as a **custom repository** (category: *Integration*), then install "PREdistribuce" from the integration list and restart Home Assistant.

### Manual

Copy `custom_components/predistribuce` from this repository into your Home Assistant `config/custom_components/` directory, then restart Home Assistant.

## Configuration

Configuration is done entirely through the Home Assistant UI (Settings → Devices & Services → Add Integration → "PREdistribuce") — there is no YAML configuration.

### Initial setup

1. Enter your PREdistribuce portal username/e-mail and password.

   ![Config flow — login step](docs/images/config-flow-login.png)
   *(placeholder — login step screenshot)*

2. Pick which metering point(s) to import, and the time of day the daily import should run.

   ![Config flow — metering points and schedule step](docs/images/config-flow-eans.png)
   *(placeholder — metering point selection + schedule step screenshot)*

Your credentials are stored the standard Home Assistant way (inside the config entry, like any other cloud-polling integration) — nothing is written anywhere else.

### Adding another metering point later

Open the integration's **Configure** options, choose **Metering points**, and select any additional EAN(s) on the account. Your saved password is reused automatically — you won't be asked for it again.

![Options flow — add metering point](docs/images/options-metering-points.png)
*(placeholder — options flow "metering points" step screenshot)*

### Changing the import schedule

Open the integration's **Configure** options and choose **Schedule** to change the hour/minute the daily import runs at.

### Manually importing historical data

To (re)download a specific date range for a specific metering point — for example to backfill data from before the integration was set up, or to force a retry outside the daily schedule — open the integration's **Configure** options and choose **Import historical data**. Pick the metering point and the from/to dates and submit; the result tells you how many hourly records were imported (0 usually means the distributor hasn't closed out the requested day(s) yet).

The same operation is also available as the Home Assistant action/service `predistribuce.import_historical_data` (Developer Tools → Actions), which takes the config entry, the EAN, and a `date_from`/`date_to` range — handy for scripts and automations. The options-flow step above is just a thin form on top of this same action.

## Missing / not-yet-closed days

PREdistribuce doesn't publish a fixed time at which a day's consumption data becomes final — the meter appears to report it roughly once per day, but the exact cut-off isn't documented anywhere. To stay safe, this integration:

- Never requests today's data, only full days strictly in the past.
- Treats a day whose consumption column is *entirely zero* as "not closed yet" instead of importing zeros, and remembers it.
- Raises a **repair issue** ("PREdistribuce: missing data"), shown as a badge in Settings and listed under Settings → System → Repairs, describing which EAN/day is pending.
- The repair issue is fixable: clicking **Fix** re-attempts every pending day/EAN on demand. If the distributor still hasn't closed the day, the issue stays open; once the retry succeeds it's dismissed automatically. The next scheduled daily run will also retry automatically, independently of the repair issue.

![Repair issue for missing data](docs/images/pending-notification.png)
*(placeholder — repair issue screenshot)*

## Known limitations

- **Grid feed-in (`-A`, e.g. from solar) is not supported yet.** It hasn't been possible to verify against a real production metering point — support is planned once one is available.
- **DST transitions are implemented but not yet verified against real data.** The clock-change fold handling (`zoneinfo`, `Europe/Prague`) is expected to be correct but will only be confirmed against a real transition (next one: 2026-10-25).
- Address labels for metering points (shown next to the EAN when selecting them) are parsed best-effort from the portal's HTML and may fall back to showing just the EAN if the portal's markup doesn't match.
- This integration is unofficial and depends on the shape of a customer-facing web portal that PREdistribuce can change at any time without notice.

## Troubleshooting

- **Login keeps failing / integration asks to reauthenticate** — Home Assistant will prompt for reauthentication automatically (Settings → Devices & Services) if your password changes or the portal login starts failing. Enter your current portal password there.
- **No data shows up in the Energy dashboard** — make sure the `predistribuce:<EAN>_consumption` statistic is added as a grid consumption source under Settings → Dashboards → Energy, and check for a pending-data notification (see above).
- **Enable debug logging** — add to `configuration.yaml`:

  ```yaml
  logger:
    logs:
      custom_components.predistribuce: debug
  ```

## Versioning & releases

Releases and version numbers (following [Semantic Versioning](https://semver.org/)) are generated automatically from [Conventional Commits](https://www.conventionalcommits.org/) on `main` using [release-please](https://github.com/googleapis/release-please) — it opens a release pull request that bumps `manifest.json`'s `version` and `CHANGELOG.md`, and creates a tagged GitHub release once that PR is merged.

## Disclaimer

Not affiliated with or endorsed by PREdistribuce, a.s. Use at your own risk; review the portal's terms of service regarding automated access before relying on this in production.

## Credits

Inspired by [HACS_CEZD_PND](https://github.com/igracek/HACS_CEZD_PND) (the equivalent for ČEZ Distribuce).
