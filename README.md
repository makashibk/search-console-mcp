# Google Search Console MCP Server

**A Google Search Console MCP server for Claude, built for real SEO work.**

search-console-mcp connects Claude Desktop to Google Search Console and gives it 44 tools: performance analysis, a rebuilt Page indexing report, robots.txt monitoring, redirect and HTTPS checks, Core Web Vitals, and Crawl stats from your own server logs. It runs entirely on your machine, talks only to Google's official APIs and the sites you check (plus a public DNS resolver for `check_dns`), and cannot change anything in Search Console.

Built and maintained by [MakashibK](https://github.com/makashibk).

Example (illustrative numbers):

```
You:     Which pages lost traffic on example.com this month, and why?

Claude:  14 pages lost 1,862 clicks (1-28 Sep vs 4-31 Aug, complete days only).
         /women/dresses/        -412 clicks  ranking_loss  avg position 3.1 -> 7.4
         /sale/                 -390 clicks  ctr_drop      position 2.0 -> 2.1, CTR 18.2% -> 9.6%
         /blog/linen-care/      -244 clicks  demand_drop   impressions -48%, position stable
         ...
```

---

## Quick start

1. Create a Google OAuth client and sign in with the read-only scope ([steps 1 and 2](#1-create-an-oauth-client)).
2. Install the server:
   ```
   pipx install git+https://github.com/makashibk/search-console-mcp.git
   ```
3. Add it to Claude Desktop under the name `search-console-mcp` ([step 4](#4-add-it-to-claude-desktop)).
4. Quit and reopen Claude Desktop. **Settings → Developer** shows `search-console-mcp` as **Running**.
5. In a new chat, ask: `Use search-console-mcp: call server_status, then list_properties.`

Want it gone again? See [Uninstall](#uninstall).

---

## Contents

- [Why another Search Console MCP server](#why-another-search-console-mcp-server)
- [What it can do](#what-it-can-do)
- [What Google's API can't do](#what-googles-api-cant-do)
- [How the numbers are calculated](#how-the-numbers-are-calculated)
- [Read-only by design](#read-only-by-design)
- [Requirements](#requirements)
- [Installation](#installation)
- [Keep these private](#keep-these-private)
- [Uninstall](#uninstall)
- [Configuration](#configuration)
- [Usage](#usage)
- [Page indexing workflow](#page-indexing-workflow)
- [Crawl stats from server logs](#crawl-stats-from-server-logs)
- [Quotas and limits](#quotas-and-limits)
- [Where data is stored](#where-data-is-stored)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Changelog](#changelog)
- [License](#license)

---

## Why another Search Console MCP server

Most Search Console MCP servers wrap the same four API endpoints and stop there. This one was built for agency work on Magento and other e-commerce sites, where the questions are rarely "what were my top queries" and usually "why did this category lose traffic", "which of these 40,000 URLs aren't indexed and why", or "is Googlebot wasting crawl budget on filter URLs".

What's different:

- **Strictly read-only.** It asks Google for the `webmasters.readonly` scope only, has no write tools, and blocks any non-read request in code. See [Read-only by design](#read-only-by-design).
- **Accurate comparisons.** Period comparisons stop at the last *complete* day reported by Google, so fresh, still-processing data doesn't make the recent period look worse than it is.
- **Traceable numbers.** Every performance result carries a `_source` block (property, dates, filters, data state), and `verify_metric` re-queries Google to confirm a number before you put it in a client report.
- **Page indexing and Crawl stats, rebuilt.** Google has no API for either report. search-console-mcp rebuilds the first from URL Inspection results stored locally, and the second from your web server's access logs.
- **Local only.** No hosted service, no third-party server between you and Google, no telemetry. Credentials and data stay on your machine. The one outside call is `check_dns`, which looks DNS records up over DNS-over-HTTPS at `dns.google` (falling back to `cloudflare-dns.com`), so that resolver sees the domain you ask about.
- **Current MCP SDK.** Built on the MCP Python SDK 2.3.

---

## What it can do

44 tools, grouped from basic to advanced. You don't call them by name; you ask Claude in plain language and it picks the right tool. Every tool with its parameters, types and defaults is in the **[tool reference](docs/tools.md)**.

### Properties and performance

| Tool | What it does |
| --- | --- |
| `server_status` | Version, read-only mode, credentials file, local database, queues, background jobs, today's inspection quota |
| `list_properties` | Every property the signed-in account can access, with permission level |
| `property_overview` | Web and Discover performance vs the previous period, sitemap health, local indexing summary, robots.txt status |
| `performance_overview` | Totals vs the previous period, daily trend, top queries and pages. Works for web, image, video, news, Discover and Google News |
| `performance_query` | Any combination of `date`, `hour`, `query`, `page`, `country`, `device`, `searchAppearance`, with filters. Up to 16 months and 100,000 rows, with CSV export |
| `compare_periods` | Winners and losers between two periods, plus new and lost queries or pages |
| `section_performance` | Performance per folder (`/blog/`, `/women/dresses/`) or custom prefixes, with share of clicks and change. Domain properties are split per host |
| `query_segments` | Branded vs non-branded, question queries (English, Dutch, Swedish, Portuguese, French, German), query length, results page |
| `multi_site_dashboard` | Every property in one table, flagged as dropping, growing or stable |
| `verify_metric` | Re-queries Google to confirm a specific number |
| `list_sitemaps`, `get_sitemap` | Submitted sitemaps with errors, warnings, last download and pending status |
| `property_notes` | Context saved with a property: brand terms, migration dates, decisions |

### Analysis

| Tool | What it does |
| --- | --- |
| `traffic_drops` | Pages or queries that lost clicks, each diagnosed as `ranking_loss`, `ctr_drop`, `demand_drop` or `disappeared`, with the loss split exactly into fewer impressions vs lower CTR, and the queries that lost the most |
| `content_decay` | Pages whose clicks fell in every one of N consecutive periods (default: three 30-day periods) |
| `check_alerts` | Severity-rated alerts: site-wide drop, queries falling off page one, CTR collapses at the same position, page click losses, pages that vanished |
| `find_opportunities` | Striking-distance keywords, low CTR at top positions, and content gaps (impressions but ranking below 20). Web or image search |
| `find_cannibalization` | Queries where several of your URLs split impressions. URL variants (www/non-www, http/https, trailing slash, parameters, case) are reported separately from genuinely competing pages |
| `weekly_report` | All of the above in one Markdown report saved to disk |

### Page indexing

| Tool | What it does |
| --- | --- |
| `queue_from_sitemaps` | Downloads every submitted sitemap (including indexes and `.gz`) and queues each URL, remembering which sitemap it came from |
| `queue_from_performance` | Queues every page that had impressions |
| `queue_urls` | Queues your own list of URLs |
| `run_inspections` | Inspects the queue, a specific list, or your top pages by clicks. Can also report whether Google recrawled each URL after a given time |
| `inspection_job` | Background inspection within the daily quota while Claude Desktop is open; resumes after restarts |
| `indexing_report` | Indexed vs not indexed, every coverage reason with its source (Website / Google systems), sample URLs, what to check and how to fix, canonical conflicts, fetch and robots states, crawl age, rich-result issues. Filter by sitemap or URL pattern |
| `list_inspected_urls` | URL-level drill-down with filters (reason, rich result type, crawl age), sorting, paging, CSV export, and optional clicks per URL |
| `indexing_changes` | URLs whose coverage reason changed between inspections: dropped out of the index, newly indexed |
| `indexing_fix_guide` | What each Page indexing reason means, what to check and how to fix it |
| `inspect_url` | Google's view of one URL |
| `diagnose_url` | Google's view next to a live fetch: redirect chain, status, noindex (meta and header), canonicals, the robots.txt rule that applies, thin content |

### Technical checks

| Tool | What it does |
| --- | --- |
| `robots_txt_check` | Fetches robots.txt like Googlebot: status and Google's interpretation of it, size vs the 500 KiB limit, groups, Googlebot rules, sitemaps, syntax warnings, diff vs the last check |
| `robots_txt_check_property` | Checks robots.txt on every host that had search traffic (every subdomain of a Domain property) |
| `robots_txt_test` | Is a URL allowed for a given crawler? Returns the deciding rule and line number |
| `robots_txt_history`, `robots_txt_diff` | Every stored check, each distinct version, and diffs between versions |
| `robots_txt_watch` | Re-checks robots.txt in the background to build a version history |
| `check_redirects` | Bulk redirect tracing (up to 500 URLs): chains, loops, temporary redirects, https→http. Validates migration maps (`old → expected new`) |
| `check_https` | Certificate validity, expiry and names, http→https redirect, HSTS, mixed content |
| `check_dns` | A, AAAA, CNAME, MX, NS, TXT and CAA records, plus Google site-verification tokens in DNS and on the home page |
| `image_audit` | Alt text, file names, width/height, lazy loading near the top, formats, file size, broken images, og:image and structured-data images |
| `pagespeed_audit` | PageSpeed Insights: Lighthouse scores, lab metrics, top opportunities by savings, failing SEO audits, real-user field data |
| `core_web_vitals` | LCP, INP, CLS, FCP and TTFB at p75 from the Chrome UX Report (the data behind Search Console's Core Web Vitals report), latest or weekly history |

### Crawl stats

| Tool | What it does |
| --- | --- |
| `import_access_logs` | Imports Apache, Nginx or Varnish access logs (plain or `.gz`) in the background and verifies Googlebot against Google's published IP ranges |
| `crawl_stats` | Requests, download size and response time per day; breakdowns by response, file type, Googlebot type, purpose and host; robots.txt fetches; example and most-crawled URLs; fake Googlebots counted separately |

---

## What Google's API can't do

The Search Console API has four parts: Search Analytics, Sitemaps, Sites and URL Inspection. Everything else in the Search Console interface has no API, so no MCP server can offer it directly.

| Search Console report | Status in search-console-mcp |
| --- | --- |
| Performance, Discover, Google News | Full API access |
| URL Inspection | Full API access, 2,000 URLs per property per day |
| Sitemaps | Read only (by choice) |
| Page indexing | Rebuilt from stored URL Inspection results |
| Crawl stats, Host status | Rebuilt from your server logs (Google-side DNS and connection failures can't be seen in logs) |
| Core Web Vitals | Chrome UX Report API, same field data |
| HTTPS | Checked directly with `check_https` |
| Shopping and Enhancements (Product snippets, Merchant listings, Breadcrumbs, Review snippets...) | Per URL, from URL Inspection rich results; no site-wide totals |
| robots.txt report | Own fetch and version history; Google's own fetch history isn't available |
| Video indexing, Links, Removals, Manual actions, Security issues, Achievements, Settings | Not available through any API |
| Request indexing | Not available. Google's Indexing API is limited to job-posting and livestream pages, so it is deliberately not included |

---

## How the numbers are calculated

**Complete days only.** When you request recent data, Google marks where still-processing data starts (`metadata.firstIncompleteDate`). Comparison tools end on the day before that, and say so in `end_basis`. If you pass explicit dates, they're used as given.

**Traffic drop diagnosis.** Clicks = impressions × CTR, so any click change splits exactly into two parts:

```
Δclicks = (I₁ − I₀) × CTR₀   +   I₁ × (CTR₁ − CTR₀)
          fewer impressions      lower CTR
```

Each drop is labelled `ranking_loss` (average position worsened by ≥ 2, configurable), `ctr_drop` (CTR part dominates at a stable position), `demand_drop` (impressions part dominates at a stable position) or `disappeared` (no impressions at all). The diagnosis is based on Search Console data only; it doesn't see the search results page itself.

**CTR benchmarks.** "Low CTR" compares each query/page pair with the median CTR of *your own site* at the same rounded position. There is no generic industry CTR curve.

**Aggregated positions** (sections, segments) are impression-weighted averages of row positions, close to but not identical with the interface's figure.

**Anonymised queries.** Query-level rows exclude queries Google anonymises. Overviews report what share of clicks comes from visible queries.

**Cannibalisation.** URLs are normalised (host without `www.`, lowercase path, no trailing slash, no parameters). When the competing URLs are really the same page, the query is listed under `url_variant_splits` with the reason (`www vs non-www`, `http vs https`, `trailing slash`, `query parameters`, `letter case`). Those need a redirect or canonical fix, not content changes.

**Dates and time zone.** All dates are Pacific Time, as in Search Console. The URL Inspection daily quota is counted per Pacific-Time day.

---

## Read-only by design

search-console-mcp is read-only at three independent levels:

1. **Google scope.** The server only requests `https://www.googleapis.com/auth/webmasters.readonly`. A token with this scope cannot submit, delete or change anything in Search Console, whatever the code does.
2. **No write tools.** There is no tool to submit or delete sitemaps, or to add or remove properties.
3. **Request guard.** Every Search Console request is checked before it leaves your machine. Only `GET` and the two read-only `POST` endpoints (Search Analytics query, URL Inspection) are allowed; anything else raises `WriteBlocked`. A test verifies that blocked requests never reach Google, and another that no other module can bypass the guard.

Website checks (robots.txt, sitemaps, redirects, HTTPS, images) send `GET` or `HEAD` requests only. The Chrome UX Report API is queried with `POST`, which is how that API reads data.

The only things search-console-mcp writes are local files: its SQLite database, its log file, and CSV or report exports when you ask for them. A few tools that save results this way (for example `inspect_url`, which stores each inspection locally, and `weekly_report`, which writes a Markdown file) are marked as non-read-only in their MCP metadata, which some clients display as "write" tools. They only write to your own computer, never to Search Console.

---

## Requirements

- Claude Desktop (Windows or macOS). Local MCP servers don't work in Claude on the web.
- Python 3.11 or newer, and [pipx](https://pipx.pypa.io/)
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) (`gcloud`)
- A Google Cloud project with the **Google Search Console API** enabled
- A Google account with access to the Search Console properties you want to analyse

Runs on Windows 10 with Python 3.13 and Claude Desktop. The test suite runs on Linux with Python 3.12; macOS should work the same way but hasn't been tried yet.

---

## Installation

### 1. Create an OAuth client

In [Google Cloud Console](https://console.cloud.google.com/):

1. Create or pick a project and enable the [Google Search Console API](https://console.cloud.google.com/apis/library/searchconsole.googleapis.com).
2. Go to **Google Auth Platform** and configure the consent screen: app name, support email, audience **External**.
3. Under **Audience → Test users**, add every Google account you will sign in with. While the app is in *Testing*, Google only lets listed test users sign in; without this, sign-in stops with "Access blocked".
4. Under **Clients → Create client**, choose **Desktop app**, then **Download JSON**. Save it as `client_secret.json`.

On Windows, check the file isn't saved as `client_secret.json.json`; Windows hides file extensions by default. Keep `client_secret.json` private: see [Keep these private](#keep-these-private).

### 2. Sign in with gcloud

```
gcloud auth application-default login --scopes=https://www.googleapis.com/auth/webmasters.readonly,https://www.googleapis.com/auth/cloud-platform --client-id-file=PATH\TO\client_secret.json
```

Sign in, tick every permission checkbox, and note the path printed after `Credentials saved to file:`.

If you also use Google's Ads or Analytics MCP servers, sign in once with all their scopes in the same command; they all share this credentials file.

### 3. Install the server

```
pipx install git+https://github.com/makashibk/search-console-mcp.git
```

Or from a clone:

```
git clone https://github.com/makashibk/search-console-mcp.git
cd search-console-mcp
pipx install .
```

On Windows, if `pipx` isn't recognised, use `py -m pipx` instead. Then find the installed program:

```
where search-console-mcp        # Windows
which search-console-mcp        # macOS
```

If the command isn't found, run `pipx ensurepath` and open a new terminal. The default locations are the ones used in the next step.

### 4. Add it to Claude Desktop

Open **Settings → Developer → Edit Config**. This opens `claude_desktop_config.json`, which lives at `%APPDATA%\Claude\claude_desktop_config.json` on Windows and `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS. Add the server under `mcpServers` and keep any existing content in the file.

**The key is the name Claude shows.** Use exactly `search-console-mcp`: that is the name you will see under **Settings → Developer** and under **+ → Connectors**.

> **Replace every `<you>` with your real Windows or macOS username before saving.** A placeholder left in the path makes Claude Desktop fail to start the server (`spawn ... ENOENT` in its log). On Windows, double every backslash, as in the example.
>
> **Keep the entry inside `mcpServers`.** If other servers are already listed, add a comma after the previous server's closing `}`. An entry placed outside `mcpServers` is silently ignored. Claude Desktop keeps its own preferences in this file too, so leave everything else as it is.

Windows:

```json
{
  "mcpServers": {
    "search-console-mcp": {
      "command": "C:\\Users\\<you>\\.local\\bin\\search-console-mcp.exe",
      "args": [],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "C:\\Users\\<you>\\AppData\\Roaming\\gcloud\\application_default_credentials.json"
      }
    }
  }
}
```

macOS:

```json
{
  "mcpServers": {
    "search-console-mcp": {
      "command": "/Users/<you>/.local/bin/search-console-mcp",
      "args": [],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/Users/<you>/.config/gcloud/application_default_credentials.json"
      }
    }
  }
}
```

Fully quit Claude Desktop (tray or menu bar → Quit, not just closing the window) and reopen it. **Settings → Developer** should show `search-console-mcp` as **Running**.

### 5. Check it works

In a new Claude Desktop chat:

```
Use search-console-mcp: call server_status, then list_properties.
```

`server_status` should report `"mode": "read-only ..."`, and `list_properties` should list your properties.

### Updating

Quit Claude Desktop first (Windows locks files that are in use), then:

```
pipx install --force git+https://github.com/makashibk/search-console-mcp.git
```

If you installed from a clone, run `git pull` and then `pipx install --force .` in the cloned folder. Your database and notes are stored separately and survive updates and reinstalls.

---

## Keep these private

These hold live access to your Google account or your data. Never share them, post them in an issue, or commit them to Git (the repository's `.gitignore` already skips the first three):

| File or folder | Why |
| --- | --- |
| `client_secret.json` | Your OAuth client |
| `application_default_credentials.json` (the file `GOOGLE_APPLICATION_CREDENTIALS` points to) | Your signed-in Google login |
| A service-account JSON key, if you use one | Full access as that service account |
| `claude_desktop_config.json`, as it is | May contain API keys (`CRUX_API_KEY`, `PAGESPEED_API_KEY`) and the settings of your other servers. Share only the `search-console-mcp` block, with keys removed |
| The data folder (`SEARCH_CONSOLE_MCP_DATA_DIR`) | Your inspection history, notes and crawl statistics |

If a credentials file was shared by mistake, run `gcloud auth application-default revoke` (see [Uninstall](#uninstall)) and sign in again. For a leaked service-account key or API key, delete it in Google Cloud Console and create a new one.

---

## Uninstall

Each step is independent: do only what you need.

### 1. Remove it from Claude Desktop

1. Quit Claude Desktop from the tray or menu bar (Quit). On Windows, check in Task Manager that no Claude process is left. Then reopen it and open **Settings → Developer → Edit Config**.
2. Delete the whole `"search-console-mcp": { ... }` block from `mcpServers`. Keep the JSON valid: if the block was the last one, also remove the comma after the entry before it.
3. Save the file, fully quit Claude Desktop (tray or menu bar → Quit) and reopen it.

`search-console-mcp` then disappears from **Settings → Developer** and from **+ → Connectors**. To pause it without removing anything, switch it off under **+ → Connectors** instead.

Do the steps in this order: step 1 removes the server from Claude, and the later steps clean up what it left behind.

### 2. Uninstall the program

Quit Claude Desktop first (Windows locks files that are in use), then:

```
pipx uninstall search-console-mcp
```

If you installed from a clone, you can also delete the cloned folder.

### 3. Delete the local data (optional)

search-console-mcp keeps its data on your computer only. Deleting these folders removes your inspection history, queued URLs, robots.txt version history, property notes and imported crawl statistics. It can't be undone.

| What | Windows | macOS |
| --- | --- | --- |
| Database, log file, cached Google IP ranges | `%LOCALAPPDATA%\search-console-mcp` | `~/.local/share/search-console-mcp` |
| CSV exports and saved reports | `%USERPROFILE%\Documents\search-console-mcp-exports` | `~/Documents/search-console-mcp-exports` |

If you set `SEARCH_CONSOLE_MCP_DATA_DIR` or `SEARCH_CONSOLE_MCP_EXPORT_DIR`, delete those folders instead.

### 4. Revoke Google access (optional)

```
gcloud auth application-default revoke
```

This revokes the credentials created by `gcloud auth application-default login` and deletes the local credentials file. Other tools that share that file, such as Google's Ads or Analytics MCP servers, are signed out too, so sign in again for them afterwards. It doesn't touch a credentials file you pointed `GOOGLE_APPLICATION_CREDENTIALS` at yourself (for example a service-account key): delete that file, and remove the service account's email from your Search Console properties.

You can also remove the app on Google's side: open <https://myaccount.google.com/permissions> and remove the OAuth app you created in step 1. **Don't do this if your other Google tools (Analytics, Ads, other MCP servers) use the same OAuth app**: it cuts them off too. In that case just run the revoke command above and, if you like, remove the extra test users under Google Auth Platform → Audience.

### 5. Remove the Google Cloud setup (optional)

In [Google Cloud Console](https://console.cloud.google.com/): under **APIs & Services → Credentials**, delete the OAuth client; under **APIs & Services → Library → Google Search Console API**, click **Disable API**; or delete the whole project if you created it only for this. Then delete the downloaded `client_secret.json` from your computer (unless other Google tools still use it), and any API keys you created for `CRUX_API_KEY` or `PAGESPEED_API_KEY`. Python, pipx and the Google Cloud CLI can stay: other tools use them too.

### 6. Check it's gone

Reopen Claude Desktop: **Settings → Developer** no longer lists `search-console-mcp`. In a terminal, `where search-console-mcp` (Windows) should say it can't find the file, and `which search-console-mcp` (macOS) should print nothing. The data folder from step 3 should no longer exist.

---

## Configuration

All settings are environment variables in the server's `env` block.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_APPLICATION_CREDENTIALS` | gcloud's default credentials path | OAuth user credentials or a service-account key |
| `SEARCH_CONSOLE_MCP_DATA_DIR` | `%LOCALAPPDATA%\search-console-mcp` (Windows), `~/.local/share/search-console-mcp` (macOS/Linux) | Database and log file |
| `SEARCH_CONSOLE_MCP_EXPORT_DIR` | `~/Documents/search-console-mcp-exports` | CSV exports and saved reports |
| `SEARCH_CONSOLE_MCP_INSPECT_DAILY_LIMIT` | `2000` | Cap on URL inspections per property per day |
| `SEARCH_CONSOLE_MCP_QUOTA_PROJECT` | (none) | Bill API quota to a specific Cloud project |
| `CRUX_API_KEY` | (none) | Enables `core_web_vitals` (Chrome UX Report API) |
| `PAGESPEED_API_KEY` | (none) | API key for `pagespeed_audit`; falls back to `CRUX_API_KEY`. Works without a key, but Google allows very few keyless requests |

`CRUX_API_KEY` and `PAGESPEED_API_KEY` are ordinary Google API keys from a Cloud project that has the Chrome UX Report API (and, for PageSpeed, the PageSpeed Insights API) enabled. Example `env` block:

```json
"env": {
  "GOOGLE_APPLICATION_CREDENTIALS": "C:\\Users\\<you>\\AppData\\Roaming\\gcloud\\application_default_credentials.json",
  "CRUX_API_KEY": "<your API key>"
}
```

**Service accounts.** Point `GOOGLE_APPLICATION_CREDENTIALS` at a service-account JSON key and add the service account's email as a user on each Search Console property. The credential loader supports this; day-to-day use so far has been with OAuth user credentials.

**Login expiry.** While the OAuth app is in *Testing* status, Google expires the login after 7 days. Re-run the `gcloud auth application-default login` command; the server reloads the credentials file automatically, with no restart needed.

---

## Usage

Ask in plain language. Some examples:

```
Overview of example.com for the last 28 days.
Which pages lost traffic this month, and why? Show the queries behind the three biggest drops.
Quick wins for example.com: striking-distance keywords with at least 500 impressions.
Check for keyword cannibalisation over the last 90 days.
Branded vs non-branded clicks for example.com. Brand terms: Example, Exmple.
Daily clicks for /blog/ pages on mobile in the Netherlands, last 6 months, as a chart.
Image search quick wins for example.com.
Which pages have declined three months in a row?
Run check_alerts for all my properties.
Do these old URLs redirect to their new URLs? [paste the migration map]
Is /women?color=red allowed for Googlebot on www.example.com?
HTTPS and DNS check for example.com.
Write a weekly report for example.com.
Verify that the homepage got 1,240 clicks between 2026-09-01 and 2026-09-28.
```

search-console-mcp works alongside other MCP servers in the same chat, for example Google's Analytics and Ads servers:

```
Compare Search Console clicks with GA4 sessions and conversions for our top 20 landing pages.
Which queries do we rank top 3 for organically but still pay for in Google Ads?
```

---

## Page indexing workflow

The Page indexing report has no API, but URL Inspection returns the same coverage reasons ("Crawled - currently not indexed", "Excluded by 'noindex' tag" and so on). search-console-mcp inspects URLs, stores every result, and builds the report from the latest result per URL.

1. **Fill the queue.** `queue_from_sitemaps` for everything in your sitemaps, `queue_from_performance` for pages Google shows, or `queue_urls` for a specific list.
2. **Inspect.** `run_inspections` for a batch now (default 100 URLs, 45-second budget; call again to continue), or `inspection_job action=start` to work through the whole queue in the background.
3. **Analyse.** `indexing_report` for the overview (optionally for one sitemap or URL pattern), `list_inspected_urls` for URL lists and CSV exports, `diagnose_url` for a single page, `indexing_changes` to see what moved.

Re-inspections build a history, so you can see pages drop out of the index over time, something Search Console itself doesn't show per URL.

With Google's quota of 2,000 inspections per property per day, a 50,000-URL store takes about 25 days to cover fully. Start the background job early.

---

## Crawl stats from server logs

1. Copy access logs from the server to your machine (plain or `.gz`; rotated files are fine).
2. Import them:
   ```
   Import C:\logs\access.log* for www.example.com.
   ```
   Imports run in the background, and files already imported are skipped. Ask "show import progress" to check.
3. Analyse:
   ```
   Crawl stats for www.example.com for October: by response, file type and Googlebot type.
   Which 404 URLs does Googlebot request most?
   ```

Supported formats: Apache, Nginx and Varnish "combined" logs, optionally with a leading virtual-host field and a trailing response-time field. Decimal seconds (nginx `$request_time`) are read automatically; Apache `%D` needs `response_time_unit="microseconds"`.

Every hit claiming to be Google is checked against Google's published crawler IP ranges, falling back to reverse and forward DNS. Hits with a Google user agent from a non-Google IP are counted as fake and excluded by default. If most hits fail verification, the log is probably recording a proxy or CDN IP (Varnish, Cloudflare, a load balancer) instead of the client IP; fix the real-IP setting or import with `verify=false`.

"Purpose" is approximate: *Discovery* means the URL appears for the first time in your imported logs, otherwise it's *Refresh*.

---

## Quotas and limits

| Limit | Value |
| --- | --- |
| URL Inspection (Google) | 2,000 per property per day, 600 per minute. The server paces itself at 8 per second and stops cleanly at the quota |
| Search Analytics rows per request (Google) | 25,000; the server pages automatically, up to 100,000 rows per call |
| Search Analytics history (Google) | About 16 months |
| Response cache | Identical Search Analytics requests are served from memory for 10 minutes |
| Rate-limit handling | Search Analytics: up to 4 retries with back-off on HTTP 429. URL Inspection: stops cleanly at the quota and resumes the next Pacific-Time day |
| `run_inspections` | Up to 500 URLs and 240 seconds per call (defaults 100 and 45) |
| `check_redirects` | Up to 500 URLs per call |

Very large sites may hit Google's own row limits in Search Analytics. For complete, unsampled data at that scale, Search Console's BigQuery bulk export is the better source.

---

## Where data is stored

| What | Where |
| --- | --- |
| Database (inspections, URL queue, jobs, robots.txt history, notes, crawl statistics) | `SEARCH_CONSOLE_MCP_DATA_DIR/gsc.sqlite3` |
| Log file (rotated: 1 MB each, 3 backups) | `SEARCH_CONSOLE_MCP_DATA_DIR/server.log` |
| Cached Google crawler IP ranges | `SEARCH_CONSOLE_MCP_DATA_DIR/google-crawler-ranges.json` |
| CSV exports and reports | `SEARCH_CONSOLE_MCP_EXPORT_DIR` |
| Google credentials | Wherever `GOOGLE_APPLICATION_CREDENTIALS` points; never copied |

Server logs you import stay where they are. Only aggregated counts and crawled URLs are stored in the database.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `search-console-mcp` doesn't appear under **Settings → Developer** | The JSON in `claude_desktop_config.json` is invalid (a missing comma, or backslashes in a Windows path that aren't doubled), or Claude Desktop wasn't fully quit. Fix the JSON, then quit from the tray or menu bar and reopen |
| Claude Desktop's log shows `spawn ... ENOENT` | The path in `command` is wrong, usually a `<you>` placeholder left in it. Put your real username in the path, or paste the exact output of `where` / `which search-console-mcp` (backslashes doubled on Windows) |
| The server is in the config but never appears | The entry sits outside `mcpServers` (for example after its closing `}`) and is silently ignored. Move it inside, with a comma after the previous server |
| "Access blocked" in the browser when signing in | That Google account isn't a test user of the OAuth app. Add it under Google Auth Platform → Audience → Test users, then sign in again |
| Claude says the tools aren't available | The chat isn't loading local servers. Use a new chat in the Claude Desktop app (not the browser) and check **+ → Connectors** that `search-console-mcp` is on |
| Server shows **Failed**, log has `notifications/cancelled` | The server took longer than about 60 seconds to start. The first start after installing can be slow on Windows while Defender scans the new files; restart Claude Desktop. If it keeps happening, add a Defender exclusion for the pipx folder |
| `Credentials file not found` | Run the `gcloud auth application-default login` command, or fix the path in `GOOGLE_APPLICATION_CREDENTIALS` |
| `Google login expired or was revoked` / `invalid_grant` | The login expired (7 days in Testing mode) or was removed. Run the login command again; no restart needed |
| `Not all requested scopes were granted` / `insufficient authentication scopes` | The login doesn't include `webmasters.readonly`. Run the login command with that scope and tick every checkbox |
| `Google Search Console API has not been used in project ...` | Enable the API in the Cloud project that owns your OAuth client |
| `User does not have sufficient permission for site` | The signed-in account has no access to that property, or the property string is wrong. Use the exact value from `list_properties` (`sc-domain:example.com` vs `https://www.example.com/`) |
| `Google quota hit (429)` during inspections | Daily inspection quota used up. It resets at midnight Pacific Time; background jobs resume by themselves |
| pipx: `Neither 'setup.py' nor 'pyproject.toml' found` | You pointed pipx at the wrong folder level. Use the folder that contains `pyproject.toml` |
| pipx (Windows): `FileExistsError ... \pipx` | An empty file named `pipx` blocks pipx's folder. Rename it (`ren %USERPROFILE%\pipx pipx-old-file`) and run the command again |
| pipx: `PermissionError: [WinError 32]` | Claude Desktop is still running the server. Quit it fully, then reinstall |
| Crawl stats: most Google hits marked "not google ip" | The log records a proxy/CDN IP. See [Crawl stats from server logs](#crawl-stats-from-server-logs) |

The server writes errors to `server.log` in the data folder. Claude Desktop's **Settings → Developer → View logs** shows startup and protocol messages.

---

## Development

```
git clone https://github.com/makashibk/search-console-mcp.git
cd search-console-mcp
pip install -e . pytest
python -m pytest -q tests
```

The tests use a fake Search Console API, a local web server, synthetic server logs and a self-signed TLS server, so they need no Google account or network access to Google. (The TLS test needs `openssl` on your PATH and is skipped without it.) They cover the robots.txt matching rules (including Google's documented precedence examples), the read-only guard, pagination, the drop decomposition, decay, alerts, sections, segments, the indexing pipeline and quotas, site checks and log parsing.

Two tests keep the documentation honest: one checks that the tools in the README tables are exactly the tools the server registers, the other that [docs/tools.md](docs/tools.md) is up to date. After adding or changing a tool, regenerate the reference:

```
python scripts/gen_tools_doc.py
```

Project layout:

```
search_console_mcp/
  server.py         MCP tool definitions
  api.py            Search Console API client, read-only guard, cache, retries
  auth.py           Credentials loading and per-thread sessions
  performance.py    Search Analytics queries and analyses
  inspector.py      URL Inspection runner, quotas, background jobs
  indexing.py       Page indexing reports from stored inspections
  playbook.py       Coverage reasons: source, meaning, checks, fixes
  robots.py         robots.txt fetching, Google-style matching, history
  livecheck.py      Live page fetch for diagnose_url
  sitechecks.py     Redirects, HTTPS/TLS, DNS, images, PageSpeed
  crux.py           Chrome UX Report (Core Web Vitals)
  crawllogs.py      Access-log import and crawl stats
  sitemap_fetch.py  Sitemap download and parsing
  report.py         Weekly Markdown report
  db.py, util.py    Storage and helpers
docs/tools.md       Generated reference of every tool and parameter
scripts/            gen_tools_doc.py
tests/              Offline test suite
```

Bug reports and feature requests: [GitHub Issues](https://github.com/makashibk/search-console-mcp/issues).

---

## Changelog

### 1.2.0
- Read-only: the server requests `webmasters.readonly` only, the sitemap submit/delete tools are removed, and a request guard blocks every non-read call.

### 1.1.0
- Period comparisons aligned to the last complete day; provenance (`_source`) on results; `verify_metric`.
- New analyses: `traffic_drops` (with exact decomposition), `content_decay`, `check_alerts`, `section_performance`, `query_segments`, `multi_site_dashboard`, `weekly_report`.
- `find_opportunities` adds content gaps and image search; `find_cannibalization` separates URL variants.
- `run_inspections` can inspect top pages and check recrawls since a given time.
- Site checks: `check_redirects` (with migration maps), `check_https`, `check_dns`, `image_audit`, `pagespeed_audit`.
- `property_notes`; Crawl stats from server logs (`import_access_logs`, `crawl_stats`).
- 10-minute response cache and back-off on rate limits.

### 1.0.0
- Performance queries and analyses, sitemaps, URL Inspection with a local database, rebuilt Page indexing report, background inspection jobs, `diagnose_url`, robots.txt monitoring with history, Core Web Vitals via CrUX.

---

## License

MIT. Copyright (c) 2026 MakashibK. See [LICENSE](LICENSE).

Not affiliated with or endorsed by Google or Anthropic. Google Search Console, Chrome UX Report and PageSpeed Insights are trademarks of Google LLC. Claude is a trademark of Anthropic, PBC.
