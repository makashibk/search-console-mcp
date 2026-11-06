# Tool reference

Every tool `search-console-mcp` exposes, with its parameters. You don't call tools by name: ask Claude in plain
language and it picks the right one. The grouped overview is in the [README](../README.md#what-it-can-do).

_This file is generated from the server's tool definitions by `scripts/gen_tools_doc.py`. Don't edit it by hand._

Conventions used by most tools:

- **`site`** is a Search Console property: `sc-domain:example.com`, a URL-prefix property such as
  `https://www.example.com/`, or just `example.com`. Use `list_properties` to see the exact values.
- **Dates** are `YYYY-MM-DD` in Pacific Time, as in Search Console. `days` counts back from yesterday and is ignored
  when `start_date` is given. Comparisons end on the latest complete day.
- **Access.** Nothing here can change Search Console. Tools marked *saves locally* also write results to your own
  computer (the local database, CSV or Markdown files).

**44 tools:** [`check_alerts`](#check_alerts), [`check_dns`](#check_dns), [`check_https`](#check_https), [`check_redirects`](#check_redirects), [`compare_periods`](#compare_periods), [`content_decay`](#content_decay), [`core_web_vitals`](#core_web_vitals), [`crawl_stats`](#crawl_stats), [`diagnose_url`](#diagnose_url), [`find_cannibalization`](#find_cannibalization), [`find_opportunities`](#find_opportunities), [`get_sitemap`](#get_sitemap), [`image_audit`](#image_audit), [`import_access_logs`](#import_access_logs), [`indexing_changes`](#indexing_changes), [`indexing_fix_guide`](#indexing_fix_guide), [`indexing_report`](#indexing_report), [`inspect_url`](#inspect_url), [`inspection_job`](#inspection_job), [`list_inspected_urls`](#list_inspected_urls), [`list_properties`](#list_properties), [`list_sitemaps`](#list_sitemaps), [`multi_site_dashboard`](#multi_site_dashboard), [`pagespeed_audit`](#pagespeed_audit), [`performance_overview`](#performance_overview), [`performance_query`](#performance_query), [`property_notes`](#property_notes), [`property_overview`](#property_overview), [`query_segments`](#query_segments), [`queue_from_performance`](#queue_from_performance), [`queue_from_sitemaps`](#queue_from_sitemaps), [`queue_urls`](#queue_urls), [`robots_txt_check`](#robots_txt_check), [`robots_txt_check_property`](#robots_txt_check_property), [`robots_txt_diff`](#robots_txt_diff), [`robots_txt_history`](#robots_txt_history), [`robots_txt_test`](#robots_txt_test), [`robots_txt_watch`](#robots_txt_watch), [`run_inspections`](#run_inspections), [`section_performance`](#section_performance), [`server_status`](#server_status), [`traffic_drops`](#traffic_drops), [`verify_metric`](#verify_metric), [`weekly_report`](#weekly_report)

---

### `check_alerts`

Severity-rated alerts: site-wide click drop, query position drops (incl. falling off page one), CTR collapses at the same position, page click losses, pages that vanished.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `days` | integer (1–90) | `7` | Compare the last N complete days with the N before |
| `search_type` | `web` / `image` / `video` / `news` / `discover` / `googleNews` | `web` |  |
| `min_impressions` | integer (≥ 1) | `100` |  |
| `min_clicks` | integer (≥ 1) | `20` |  |
| `position_drop` | number (> 0) | `5.0` |  |
| `ctr_drop_pct` | number (≤ 100) | `50.0` |  |
| `click_drop_pct` | number (≤ 100) | `30.0` |  |
| `site_drop_pct` | number (≤ 100) | `20.0` |  |

### `check_dns`

DNS records (A, AAAA, CNAME, MX, NS, TXT, CAA) plus Google site-verification tokens found in DNS TXT and on the home page.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `domain` | string | **required** | Domain or sc-domain property |
| `include_www` | boolean | `true` |  |

### `check_https`

HTTPS health (Search Console's HTTPS report has no API): certificate validity/expiry/names, http->https redirect, HSTS, mixed content.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `host_or_url` | string | **required** | Host or URL |
| `urls` | list of string | – | Optional URLs to confirm they end on https (max 100) |

### `check_redirects`

Trace redirect chains in bulk: every hop, final status, chains, loops, temporary redirects, https->http; validates migration redirect maps.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `urls` | list of string | – | URLs to trace (max 500) |
| `pairs` | list of object | – | Migration map [{from, to}]: verifies each old URL ends at its new URL |
| `concurrency` | integer (1–16) | `8` |  |

### `compare_periods`

Winners and losers between two periods (default: previous equal-length period), with new and lost queries/pages.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `dimension` | `query` / `page` / `country` / `device` / `searchAppearance` | `query` | What to compare |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |
| `previous_start` | string | – | YYYY-MM-DD (Pacific Time) |
| `previous_end` | string | – | YYYY-MM-DD (Pacific Time) |
| `search_type` | `web` / `image` / `video` / `news` / `discover` / `googleNews` | `web` | Search Console search type; 'discover' = Discover report |
| `filters` | list of object | – | Same format as performance_query filters |
| `top_n` | integer (1–500) | `25` |  |
| `min_impressions` | integer (≥ 0) | `10` |  |

### `content_decay`

Pages (or queries) whose clicks fell in every one of N consecutive periods: slow decay, not a one-off dip.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `dimension` | `page` / `query` | `page` |  |
| `periods` | integer (2–6) | `3` | Consecutive periods that must each decline |
| `period_days` | integer (7–120) | `30` |  |
| `search_type` | `web` / `image` / `video` / `news` / `discover` / `googleNews` | `web` |  |
| `min_clicks` | integer (≥ 1) | `20` | Clicks needed in the oldest period |
| `min_decline_pct` | number (0–100) | `20.0` |  |
| `top_n` | integer (1–500) | `50` |  |

### `core_web_vitals`

Core Web Vitals field data (LCP, INP, CLS, FCP, TTFB at p75) from the Chrome UX Report, the data behind Search Console's CWV report. Needs CRUX_API_KEY.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `target` | string | **required** | Origin (https://www.example.com) or full page URL |
| `form_factor` | `PHONE` / `DESKTOP` / `TABLET` / `ALL` | `PHONE` | Device class |
| `history` | boolean | `false` | Weekly history instead of the latest 28-day window |

### `crawl_stats`

Crawl stats from your logs: requests, download size, response time per day; breakdowns by response, file type, Googlebot type, purpose and host; robots.txt fetches; example URLs.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `host` | string | – | Host, or omit/'all' for every imported host |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `verification` | `exclude_fake` / `verified_only` / `all` / `fake_only` | `exclude_fake` | Which hits count; fake = Google user agent from a non-Google IP |
| `bot_types` | list of string | – | e.g. ['Smartphone','Desktop','Image','AdsBot','StoreBot'] |
| `examples` | integer (0–50) | `5` | Example URLs per response type |
| `top_urls` | integer (0–1000) | `0` | Most-crawled URLs |
| `response` | string | – | Limit top_urls to one response, e.g. 'Not found (404)' |

### `diagnose_url`

Why is this URL (not) indexed? Combines Google's inspection with a live fetch: redirect chain, status, noindex, canonicals, robots.txt rule, thin content.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `url` | string | **required** | Full page URL |
| `use_cached_inspection` | boolean | `false` | Use the last stored inspection instead of a new one |
| `user_agent` | `default` / `googlebot_smartphone` | `default` | UA for the live fetch. A Googlebot UA from your IP may be blocked by a WAF |

### `find_cannibalization`

Queries where 2+ of your URLs split impressions. URL variants (www/non-www, http/https, slash, parameters) are reported separately from genuinely competing pages.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `days` | integer (1–490) | `90` | Days ending yesterday (PT); ignored if start_date is given |
| `min_impressions` | integer (≥ 1) | `50` |  |
| `min_share` | number (0–1, exclusive) | `0.1` | Each competing page's minimum share of the query's impressions |
| `top_n` | integer (1–500) | `50` |  |
| `search_type` | `web` / `image` / `video` / `news` | `web` | Search type |

### `find_opportunities`

Quick wins: striking-distance query/page pairs, top positions with low CTR (vs this site's own CTR by position), and content gaps (demand, but ranking below 20).

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |
| `min_impressions` | integer (≥ 1) | `100` |  |
| `position_from` | number | `4.0` |  |
| `position_to` | number | `20.0` |  |
| `top_n` | integer (1–500) | `50` |  |
| `search_type` | `web` / `image` / `video` / `news` | `web` | 'image' = image-search quick wins |

### `get_sitemap`

Details of one submitted sitemap.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `sitemap_url` | string | **required** | Full sitemap URL |

### `image_audit`

Image SEO audit of pages: alt text, file names, width/height, lazy loading near the top, formats, file size, broken images, og:image and structured-data images.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `urls` | list of string | **required** | Pages to audit (max 10) |
| `check_files` | boolean | `true` | Fetch image headers for size/broken checks (up to 30 per page) |
| `max_images` | integer (1–300) | `60` |  |
| `large_kb` | integer (50–5000) | `300` |  |

### `import_access_logs`

Import Apache/Nginx/Varnish access logs to rebuild Crawl stats. Runs in the background; already-imported files are skipped.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `paths` | list of string | – | Log files or patterns on this PC, e.g. C:\logs\access.log*; .gz is fine. Omit to see import progress |
| `host` | string | – | Host the log belongs to (unless the log has a virtual-host field) |
| `response_time_unit` | `auto` / `seconds` / `milliseconds` / `microseconds` / `none` | `auto` | Unit of a trailing response-time field; 'auto' only reads decimal seconds (nginx $request_time) |
| `verify` | boolean | `true` | Verify Google IPs against Google's published ranges |

### `indexing_changes`

URLs whose coverage reason changed between their last two inspections (dropped out of the index, newly indexed, ...), plus per-day inspection trend.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `days` | integer (1–365) | `30` |  |
| `limit` | integer (1–1000) | `200` |  |

### `indexing_fix_guide`

What each Page indexing reason means, whether it's a Website or Google-systems issue, what to check and how to fix it.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `reason` | string | – | A coverage reason; omit for all |

### `indexing_report`

Page-indexing report from local inspections: indexed vs not, every reason with source, samples, checks and fixes; canonicals, fetch/robots states, crawl age, rich results.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `sitemap` | string | – | Only URLs from this sitemap (by our sitemap download or Google's sitemap list) |
| `url_contains` | string | – | Substring filter on URL |
| `url_regex` | string | – | Python regex on URL |
| `source` | `sitemap` / `performance` / `manual` / `inspected` | – | Queue source |
| `samples` | integer (0–50) | `5` |  |

### `inspect_url`

Live URL Inspection result from Google (index status, coverage reason, canonicals, last crawl, robots, sitemaps, referring URLs, rich results). Stored locally.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `url` | string | **required** | Full page URL |
| `language` | string | `en-US` | Language for issue messages |

### `inspection_job`

Background inspection while Claude Desktop is open: works through the queue within the daily quota and resumes after restarts.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `action` | `start` / `stop` / `status` | `status` | Background job control |
| `daily_limit` | integer (1–2000) | `2000` | Max inspections per Pacific-Time day for this property |
| `stale_days` | integer (1–365) | `14` | Re-inspect after this many days |

### `list_inspected_urls`

URL-level drill-down of stored inspections with filtering, sorting, paging and CSV export.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `coverage_state` | string | – | Exact reason, e.g. 'Crawled - currently not indexed' |
| `indexed` | boolean | – | True = indexed only, False = not indexed only |
| `url_contains` | string | – |  |
| `url_regex` | string | – |  |
| `sitemap` | string | – | Only URLs from this sitemap |
| `rich_type` | string | – | e.g. 'Product snippets', 'Merchant listings', 'Breadcrumbs', 'Review snippets' |
| `has_rich_issues` | boolean | – |  |
| `crawled_before_days` | integer | – | Only URLs last crawled more than N days ago |
| `sort_by` | `last_crawl_time` / `inspected_at` / `url` / `coverage_state` / `rich_issue_count` | `last_crawl_time` |  |
| `descending` | boolean | `false` |  |
| `limit` | integer (1–1000) | `100` |  |
| `offset` | integer (≥ 0) | `0` |  |
| `export_csv` | boolean | `false` |  |
| `with_performance_days` | integer | – | Add clicks/impressions per URL for the last N days |

### `list_properties`

All Search Console properties this Google account can access, with permission level.

_Reads data. Changes nothing in Search Console._

No parameters.

### `list_sitemaps`

Sitemaps submitted for the property: last submitted/downloaded, pending, errors, warnings, submitted URL counts by type.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `sitemap_index` | string | – | Only children of this sitemap index |

### `multi_site_dashboard`

Health check across many properties at once: clicks, impressions, CTR, position vs previous period, flagged dropping/growing.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `properties` | list of string | – | Default: every accessible property |
| `days` | integer (7–90) | `28` |  |
| `drop_pct` | number (≤ 100) | `20.0` | Change that flags a property as dropping/growing |

### `pagespeed_audit`

PageSpeed Insights: Lighthouse scores, lab metrics, top opportunities by savings, failing SEO/other audits, and real-user field data. Uses PAGESPEED_API_KEY or CRUX_API_KEY if set.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | **required** |  |
| `strategy` | `MOBILE` / `DESKTOP` | `MOBILE` |  |
| `categories` | list of `PERFORMANCE` / `SEO` / `ACCESSIBILITY` / `BEST_PRACTICES` | – | Lighthouse categories |

### `performance_overview`

Totals vs the previous equal-length period, daily series, top queries and pages. Use search_type='discover' for Discover.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |
| `search_type` | `web` / `image` / `video` / `news` / `discover` / `googleNews` | `web` | Search Console search type; 'discover' = Discover report |

### `performance_query`

Search performance (clicks, impressions, CTR, position) for any dimension combination, search type, date range and filters.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `dimensions` | list of `date` / `hour` / `query` / `page` / `country` / `device` / `searchAppearance` | – | Group-by dimensions. 'hour' gives hourly data for recent days. Empty = totals only |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |
| `search_type` | `web` / `image` / `video` / `news` / `discover` / `googleNews` | `web` | Search Console search type; 'discover' = Discover report |
| `filters` | list of object | – | AND filters: [{dimension: query\|page\|country\|device\|searchAppearance, operator: equals\|notEquals\|contains\|notContains\|includingRegex\|excludingRegex, expression}] |
| `aggregation` | `auto` / `byPage` / `byProperty` | `auto` | How results are aggregated |
| `data_state` | `all` / `final` | `all` | 'all' matches the UI (includes fresh data); 'final' = finalised only |
| `max_rows` | integer (1–100000) | `1000` | Rows to fetch (pages of 25,000) |
| `return_rows` | integer (0–5000) | `200` | Rows to return in chat; the rest only in the CSV |
| `sort_by` | `clicks` / `impressions` / `ctr` / `position` / `key` | `clicks` | Sort rows |
| `export_csv` | boolean | `false` | Write all fetched rows to a CSV file |

### `property_notes`

Context saved with a property for future chats: brand terms, migration dates, decisions, site changes.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `action` | `list` / `add` / `delete` | `list` |  |
| `kind` | `note` / `brand_terms` / `migration` / `decision` / `change` | `note` | brand_terms = comma-separated brand names used by query_segments |
| `text` | string | – |  |
| `note_id` | integer | – |  |

### `property_overview`

One-call overview: web + Discover performance vs previous period, sitemaps health, local indexing summary, robots.txt status.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |

### `query_segments`

Queries split into branded vs non-branded, questions (EN/NL/SV/PT/FR/DE), query length, results page, and chat-style fragments.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |
| `search_type` | `web` / `image` / `video` / `news` | `web` |  |
| `brand_terms` | list of string | – | Brand names/spellings; default = the property's brand_terms note |
| `top_n` | integer (1–50) | `10` |  |

### `queue_from_performance`

Queue every page that got impressions in Search (useful to inspect the pages Google actually shows).

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `days` | integer (1–490) | `90` | Days ending yesterday (PT); ignored if start_date is given |
| `min_impressions` | integer (≥ 0) | `1` |  |
| `search_type` | `web` / `image` / `video` / `news` / `discover` / `googleNews` | `web` | Search Console search type; 'discover' = Discover report |
| `max_urls` | integer (1–100000) | `25000` |  |

### `queue_from_sitemaps`

Download sitemaps (indexes, .gz) and queue every page URL for inspection, remembering which sitemap each URL came from.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `sitemap_url` | string | – | One sitemap or index; default = every sitemap submitted in Search Console |
| `max_urls` | integer (1–500000) | `50000` |  |

### `queue_urls`

Add URLs to this property's local inspection queue.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `urls` | list of string | **required** | Full URLs inside the property |
| `label` | string | `manual` | Source label for filtering later |

### `robots_txt_check`

Fetch robots.txt like Googlebot: status and Google's interpretation, size vs the 500 KiB limit, groups, Googlebot rules, sitemaps, syntax warnings, and a diff if it changed since the last check. Saved to history.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `host_or_url` | string | **required** | Host, URL or sc-domain property, e.g. www.example.com |

### `robots_txt_check_property`

Check robots.txt on every host that had Search traffic in the property (all subdomains of a Domain property).

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |
| `max_hosts` | integer (1–50) | `20` |  |

### `robots_txt_diff`

Unified diff between two stored robots.txt checks (ids from robots_txt_history).

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `from_check_id` | integer | **required** |  |
| `to_check_id` | integer | **required** |  |

### `robots_txt_history`

Every stored robots.txt check for a host (date, status, size, hash, response time) and each distinct version with first/last seen.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `host_or_url` | string | **required** |  |
| `limit` | integer (1–500) | `50` |  |

### `robots_txt_test`

Is this URL allowed for this crawler? Returns the deciding rule and line, using Google's matching rules.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | **required** | Full URL to test |
| `user_agent` | string | `Googlebot` | Googlebot, Googlebot-Image, Googlebot-News, Googlebot-Video, Storebot-Google, AdsBot-Google, Google-Extended, or any token |
| `check_id` | integer | – | Test against a stored version instead of the live file |
| `show_content` | boolean | `false` |  |

### `robots_txt_watch`

Keep checking robots.txt for a host in the background (while Claude Desktop is open) to build version history.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `action` | `add` / `remove` / `list` | `list` | Watch list control |
| `host_or_url` | string | – |  |
| `interval_hours` | integer (1–168) | `24` |  |

### `run_inspections`

Inspect URLs now: the queue (never-inspected first, then stale), an explicit list, or your top pages. Respects the 2,000/day and 600/minute per-property quotas.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `max_urls` | integer (1–500) | `100` |  |
| `time_budget_seconds` | integer (5–240) | `45` | Stop after this long; call again to continue |
| `stale_days` | integer (0–365) | `7` | Re-inspect URLs whose last inspection is older than this |
| `concurrency` | integer (1–10) | `5` |  |
| `urls` | list of string | – | Inspect exactly these URLs instead of the queue |
| `top_pages` | integer (1–500) | – | Inspect the top N pages by clicks (last 28 days) |
| `recrawled_since` | string | – | YYYY-MM-DD: also report whether Google crawled each URL after this date |

### `section_performance`

Performance per site section (URL folder or custom prefixes) with share of clicks and change vs the previous period. Domain properties are split per host.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `level` | integer (1–5) | `1` | Folder depth: 1 = /blog/, 2 = /blog/seo/ |
| `prefixes` | list of string | – | Custom sections by path prefix, e.g. ['/blog/', '/women/'] |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |
| `search_type` | `web` / `image` / `video` / `news` / `discover` / `googleNews` | `web` | Search Console search type; 'discover' = Discover report |
| `compare_previous` | boolean | `true` |  |
| `top_n` | integer (1–500) | `50` |  |

### `server_status`

Server health: credentials, local database, inspection queues, background jobs, robots.txt watches, today's inspection quota use.

_Reads data. Changes nothing in Search Console._

No parameters.

### `traffic_drops`

Pages/queries that lost clicks vs the previous period, each diagnosed: ranking loss, CTR drop, demand drop or disappeared, with the click loss split exactly into fewer impressions vs lower CTR.

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `dimension` | `page` / `query` / `country` / `device` | `page` | What lost traffic |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |
| `search_type` | `web` / `image` / `video` / `news` / `discover` / `googleNews` | `web` |  |
| `min_clicks_prev` | integer (≥ 1) | `10` |  |
| `position_threshold` | number (> 0) | `2.0` | Position worsening that counts as ranking loss |
| `top_n` | integer (1–500) | `25` |  |
| `explain_top` | integer (0–20) | `5` | For the N biggest drops, list the queries (or pages) that lost most |

### `verify_metric`

Re-query Google to confirm a number before presenting it (e.g. 'this page got 500 clicks').

_Reads data. Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `metric` | `clicks` / `impressions` / `ctr` / `position` | **required** |  |
| `expected` | number | **required** |  |
| `start_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `end_date` | string | – | YYYY-MM-DD (Pacific Time) |
| `days` | integer (1–490) | `28` | Days ending yesterday (PT); ignored if start_date is given |
| `search_type` | `web` / `image` / `video` / `news` / `discover` / `googleNews` | `web` | Search Console search type; 'discover' = Discover report |
| `page` | string | – | Exact page URL |
| `query` | string | – | Exact query |
| `country` | string | – | ISO-3166 alpha-3, e.g. nld |
| `device` | `DESKTOP` / `MOBILE` / `TABLET` | – |  |
| `tolerance_pct` | number (0–50) | `1.0` |  |

### `weekly_report`

Full report in one call (performance, alerts, drops, quick wins, content gaps, decay, cannibalisation, sitemaps, indexing, robots.txt), saved as a Markdown file.

_Reads data; also *saves locally* (database or files). Changes nothing in Search Console._

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `site` | string | **required** | Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com' |
| `days` | integer (7–90) | `28` |  |
| `top_n` | integer (3–50) | `10` |  |
