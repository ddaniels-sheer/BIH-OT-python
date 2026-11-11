"""Tableau Cloud: export every view in every sufficiently viewed non-CGBI workbook.

AUTOMATION ANYWHERE:
    Python: Open -> choose this .py file
    Python: Execute Function -> function name: current_function
    Input parameters: NONE
    Output variable: any STRING variable

The function ALWAYS returns readable status text:
    SUCCESS | CSV: <absolute path> | ...
    ERROR | <error name>: <reason> | Traceback: ...

Automation Anywhere compatible: no interactive prompts or extra configuration files.
Install dependency once: pip install requests

DECISIONS / SETTINGS ARE AT THE TOP.

Rules:
  * A workbook qualifies if the sum of its accessible view's all-time
    totalViewCount values is at least MIN_WORKBOOK_ALL_TIME_VIEWS.
  * ALL its views go into the CSV, even individual views with zero visits.
  * Exclude the WHOLE workbook if ANY of its embedded or published upstream
    Tableau data source names contains EXCLUDE_DATASOURCE_TEXT (case-insensitive).
  * Source names are obtained from Tableau Metadata API, with an optional
    workbook-definition download fallback when Metadata API isn't accessible.
  * Nothing is changed on Tableau. If a source check fails, no CSV is written.
"""

# ===================== EDIT THESE SETTINGS =====================
TABLEAU_SERVER = "https://10az.online.tableau.com"
TABLEAU_SITE = "reportingsheerlogistics"
API_VERSION = "3.22"

PAT_NAME = "api"
PAT_SECRET = ""  # Paste your current PERSONAL ACCESS TOKEN SECRET here, or use environment variable.
# Never use the temporary X-Tableau-Auth session token as PAT_SECRET.

MIN_WORKBOOK_ALL_TIME_VIEWS = 5
EXCLUDE_DATASOURCE_TEXT = "cgbi"  # Case-insensitive substring in ANY source's name.

OUTPUT_CSV = "tableau_workbook_views_no_cgbi.csv"
# Relative names save beside this .py file. Or enter a full Windows path, e.g.:
# OUTPUT_CSV = r"C:\Reports\tableau_workbook_views_no_cgbi.csv"

PAGE_SIZE = 1000
METADATA_DATASOURCE_PAGE_SIZE = 100
ALLOW_WORKBOOK_DOWNLOAD_FALLBACK = True  # If Metadata API access is unavailable.
REQUEST_TIMEOUT_SECONDS = 90
# ==============================================================

import csv
import io
import os
import sys
import traceback
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


FIELDS = [
    "workbook_name",
    "workbook_all_time_views",
    "view_name",
    "view_all_time_views",
    "data_source_names",
    "workbook_url",
    "view_url",
    "workbook_id",
    "view_id",
]


class TableauFailure(RuntimeError):
    pass


def as_list(obj):
    if obj is None:
        return []
    return obj if isinstance(obj, list) else [obj]


def collection(payload, collection_name, item_name):
    """Supports both Tableau's JSON collection shapes."""
    value = payload.get(collection_name, [])
    if isinstance(value, dict):
        value = value.get(item_name, [])
    return as_list(value)


def make_session():
    sess = requests.Session()
    sess.headers.update({"Accept": "application/json"})
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
    )
    sess.mount("https://", HTTPAdapter(max_retries=retry))
    return sess


def request(session, method, path, **kwargs):
    url = TABLEAU_SERVER.rstrip("/") + path
    response = session.request(
        method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs
    )
    if not response.ok:
        # Do not print the auth request body or secrets.
        body = response.text[:350] if not kwargs.get("stream") else ""
        response.close()
        raise TableauFailure(f"{method} {path} -> HTTP {response.status_code}: {body}")
    return response


def request_json(session, method, path, **kwargs):
    response = request(session, method, path, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise TableauFailure(f"{method} {path}: response was not JSON") from exc
    finally:
        response.close()


def login(session):
    secret = PAT_SECRET.strip() or os.getenv("TABLEAU_PAT_SECRET", "").strip()
    name = PAT_NAME.strip() or os.getenv("TABLEAU_PAT_NAME", "").strip()
    if not secret:
        raise TableauFailure(
            "Set PAT_SECRET near the top of this script, or set the "
            "TABLEAU_PAT_SECRET environment variable for your Automation Anywhere bot."
        )
    if not name:
        raise TableauFailure("Set PAT_NAME near the top of this script.")
    data = request_json(
        session,
        "POST",
        f"/api/{API_VERSION}/auth/signin",
        json={
            "credentials": {
                "personalAccessTokenName": name,
                "personalAccessTokenSecret": secret,
                "site": {"contentUrl": TABLEAU_SITE},
            }
        },
        headers={"Content-Type": "application/json"},
    )
    try:
        credentials = data["credentials"]
        site_id = credentials["site"]["id"]
        session.headers["X-Tableau-Auth"] = credentials["token"]
    except (KeyError, TypeError) as exc:
        raise TableauFailure("The Tableau sign-in response did not contain a token/site ID") from exc
    return site_id


def get_paged_rest_collection(session, site_id, resource, group, item):
    rows = []
    page = 1
    while True:
        payload = request_json(
            session,
            "GET",
            f"/api/{API_VERSION}/sites/{site_id}/{resource}",
            params={"pageSize": PAGE_SIZE, "pageNumber": page, **(
                {"includeUsageStatistics": "true"} if resource == "views" else {}
            )},
        )
        part = collection(payload, group, item)
        rows.extend(part)
        pagination = payload.get("pagination") or {}
        if "totalAvailable" not in pagination:
            raise TableauFailure(f"Tableau returned no pagination totals for {resource}")
        total = int(pagination["totalAvailable"])
        if page * PAGE_SIZE >= total:
            if len(rows) < total:
                raise TableauFailure(f"Tableau returned only {len(rows)}/{total} {resource}")
            return rows
        if not part:
            raise TableauFailure(f"Tableau returned an empty middle page for {resource}")
        page += 1


def graphql(session, query, variables):
    response = request_json(
        session, "POST", "/api/metadata/graphql",
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json"},
    )
    if response.get("errors"):
        msgs = "; ".join(str(e.get("message", e)) for e in response["errors"])
        raise TableauFailure("Metadata API error: " + msgs[:600])
    if "data" not in response or response["data"] is None:
        raise TableauFailure("Metadata API returned no data")
    return response["data"]


DS_QUERY = """
query GetWorkbookSources($luid: String!, $embeddedOffset: Int!, $upstreamOffset: Int!, $limit: Int!) {
  workbooks(filter: {luid: $luid}) {
    luid
    embedded: embeddedDatasourcesConnection(first: $limit, offset: $embeddedOffset) {
      nodes { name }
      totalCount
    }
    upstream: upstreamDatasourcesConnection(first: $limit, offset: $upstreamOffset) {
      nodes { name }
      totalCount
    }
  }
}
"""


def metadata_datasource_names(session, workbook_id):
    """Read all embedded and published upstream source names, including pagination."""
    names = set()
    emb_offset = 0
    up_offset = 0
    while True:
        data = graphql(session, DS_QUERY, {
            "luid": workbook_id,
            "embeddedOffset": emb_offset,
            "upstreamOffset": up_offset,
            "limit": METADATA_DATASOURCE_PAGE_SIZE,
        })
        records = data.get("workbooks") or []
        if len(records) != 1:
            raise TableauFailure(
                f"Metadata API returned {len(records)} matches for workbook {workbook_id}"
            )
        wb = records[0]
        embedded = wb.get("embedded")
        upstream = wb.get("upstream")
        if not isinstance(embedded, dict) or not isinstance(upstream, dict):
            raise TableauFailure(f"Missing datasource lineage for workbook {workbook_id}")
        emb_nodes = embedded.get("nodes") or []
        up_nodes = upstream.get("nodes") or []
        for obj in emb_nodes + up_nodes:
            if not obj or obj.get("name") is None:
                raise TableauFailure(f"An unnamed source was returned for workbook {workbook_id}")
            if str(obj["name"]).strip():
                names.add(str(obj["name"]).strip())
        emb_count = int(embedded["totalCount"])
        up_count = int(upstream["totalCount"])
        if emb_offset + len(emb_nodes) < emb_count and not emb_nodes:
            raise TableauFailure(f"Incomplete embedded sources in workbook {workbook_id}")
        if up_offset + len(up_nodes) < up_count and not up_nodes:
            raise TableauFailure(f"Incomplete upstream sources in workbook {workbook_id}")
        emb_offset += len(emb_nodes)
        up_offset += len(up_nodes)
        if emb_offset >= emb_count and up_offset >= up_count:
            return sorted(names, key=str.casefold)


def download_datasource_names(session, site_id, workbook_id):
    """Fallback: read source captions/names from Tableau's .twb definition.

    includeExtract=false avoids downloading large embedded extracts.
    This requires Download Workbook permission.
    """
    path = f"/api/{API_VERSION}/sites/{site_id}/workbooks/{workbook_id}/content"
    response = request(session, "GET", path, params={"includeExtract": "false"}, stream=True)
    try:
        data = bytearray()
        for block in response.iter_content(chunk_size=1024 * 1024):
            if block:
                data.extend(block)
        file_bytes = bytes(data)
    finally:
        response.close()

    if zipfile.is_zipfile(io.BytesIO(file_bytes)):
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            twb_names = [entry for entry in archive.namelist() if entry.lower().endswith(".twb")]
            if not twb_names:
                raise TableauFailure(f"Workbook download had no .twb definition: {workbook_id}")
            twb_xml = archive.read(twb_names[0])
    else:
        twb_xml = file_bytes

    try:
        root = ET.fromstring(twb_xml)
    except ET.ParseError as exc:
        raise TableauFailure(f"Could not parse workbook {workbook_id} definition") from exc

    names = set()
    # Tableau stores both embedded sources and references to published sources
    # under <datasources><datasource name="..." caption="...">.
    for ds in root.findall(".//datasources/datasource"):
        for field in ("caption", "name"):
            val = (ds.attrib.get(field) or "").strip()
            if val:
                names.add(val)
        # Published source locations can include human-readable identifiers.
        for ref in ds.findall(".//repository-location"):
            for field in ("path", "name"):
                val = (ref.attrib.get(field) or "").strip()
                if val:
                    names.add(val)

    if not names:
        raise TableauFailure(
            f"Could not verify any data-source names for workbook {workbook_id}; "
            "stopping rather than accidentally including a CGBI workbook."
        )
    return sorted(names, key=str.casefold)


def output_path():
    path = Path(OUTPUT_CSV)
    if not path.is_absolute():
        script_dir = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
        path = script_dir / path
    return path


def write_csv_atomically(rows):
    filename = output_path()
    filename.parent.mkdir(parents=True, exist_ok=True)
    tempfile = filename.with_name(filename.name + ".tmp")
    try:
        with tempfile.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(str(tempfile), str(filename))
    finally:
        if tempfile.exists():
            tempfile.unlink()
    return filename


def main():
    # Automation safety: do not leave a prior run's CSV in place if this run fails.
    previous_csv = output_path()
    if previous_csv.exists():
        previous_csv.unlink()
    session = make_session()
    site_id = None
    try:
        print("Signing in to Tableau Cloud...")
        site_id = login(session)
        workbooks = get_paged_rest_collection(session, site_id, "workbooks", "workbooks", "workbook")
        views = get_paged_rest_collection(session, site_id, "views", "views", "view")
        print(f"Found {len(workbooks)} workbooks and {len(views)} views.")

        by_workbook = defaultdict(list)
        for view in views:
            wb_id = (view.get("workbook") or {}).get("id")
            if not wb_id:
                raise TableauFailure(f"View {view.get('id')} has no workbook ID")
            usage = view.get("usage")
            if not isinstance(usage, dict) or usage.get("totalViewCount") is None:
                raise TableauFailure(
                    "Tableau didn't return all-time usage for every view. "
                    "The API must support includeUsageStatistics=true."
                )
            view["_count"] = int(usage["totalViewCount"])
            by_workbook[wb_id].append(view)

        candidates = []
        for wb in workbooks:
            wb_views = by_workbook.get(wb["id"], [])
            total = sum(v["_count"] for v in wb_views)
            if total >= MIN_WORKBOOK_ALL_TIME_VIEWS:
                candidates.append((wb, wb_views, total))
        candidates.sort(key=lambda item: (-item[2], item[0]["name"].casefold()))
        print(f"{len(candidates)} workbooks have at least {MIN_WORKBOOK_ALL_TIME_VIEWS} all-time views.")

        rows = []
        skipped = []
        fallback_mode = False
        for i, (wb, wb_views, total) in enumerate(candidates, 1):
            name = wb["name"]
            wb_id = wb["id"]
            try:
                if fallback_mode:
                    sources = download_datasource_names(session, site_id, wb_id)
                    method = "workbook XML"
                else:
                    sources = metadata_datasource_names(session, wb_id)
                    method = "Metadata API"
            except TableauFailure as exc:
                if not ALLOW_WORKBOOK_DOWNLOAD_FALLBACK or fallback_mode:
                    raise TableauFailure(
                        f"Cannot verify data sources for '{name}': {exc}. "
                        "No CSV was produced; CGBI exclusion can't be guaranteed."
                    ) from exc
                print(f"Metadata lookup unavailable ({str(exc)[:140]}). Trying workbook downloads instead.")
                fallback_mode = True
                try:
                    sources = download_datasource_names(session, site_id, wb_id)
                    method = "workbook XML"
                except TableauFailure as download_exc:
                    raise TableauFailure(
                        f"Neither Metadata API nor workbook download could verify '{name}': "
                        f"{download_exc}. No CSV was produced. Check Tableau site permissions."
                    ) from download_exc

            matches = [s for s in sources if EXCLUDE_DATASOURCE_TEXT.casefold() in s.casefold()]
            if matches:
                skipped.append((name, ", ".join(matches)))
                print(f"[{i}/{len(candidates)}] SKIP CGBI: {name} ({', '.join(matches)})")
                continue

            # Every view in the qualifying workbook is exported, including 0-view sheets.
            for v in sorted(wb_views, key=lambda v: (-v["_count"], v["name"].casefold())):
                content_url = v.get("contentUrl", "")
                view_url = ""
                if content_url:
                    view_url = f"{TABLEAU_SERVER.rstrip('/')}/#/site/{TABLEAU_SITE}/views/{content_url}"
                rows.append({
                    "workbook_name": name,
                    "workbook_all_time_views": total,
                    "view_name": v["name"],
                    "view_all_time_views": v["_count"],
                    "data_source_names": "; ".join(sources),
                    "workbook_url": wb.get("webpageUrl", ""),
                    "view_url": view_url,
                    "workbook_id": wb_id,
                    "view_id": v["id"],
                })
            print(f"[{i}/{len(candidates)}] INCLUDE: {name} ({total} views, {method})")

        # Never write partial output after a verification failure.
        output = write_csv_atomically(rows)
        included_wbs = len(candidates) - len(skipped)
        print(f"\nSUCCESS: {len(rows)} view rows from {included_wbs} workbooks")
        print(f"Excluded {len(skipped)} workbooks containing '{EXCLUDE_DATASOURCE_TEXT}' data sources")
        print(f"CSV: {output}")
        return (
            f"SUCCESS | CSV: {output.resolve()} | "
            f"workbooks: {included_wbs} | view_rows: {len(rows)} | "
            f"excluded_cgbi_workbooks: {len(skipped)}"
        )
    finally:
        if site_id is not None:
            try:
                request(session, "POST", f"/api/{API_VERSION}/auth/signout").close()
            except Exception:
                pass
        session.close()


def current_function():
    """Automation Anywhere -> Python: Execute Function. No input parameters.

    Always returns text, including on failures. Map the return value to a bot
    STRING variable so the bot can display/email/log the exact result.
    """
    try:
        return main()
    except Exception as exc:
        # Include a limited traceback for debugging the actual failure.
        details = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__, limit=8
        ))
        result = f"ERROR | {type(exc).__name__}: {exc}\n\nTRACEBACK:\n{details}"
        # Defensive secret redaction: never return a configured token to AA logs.
        secret = PAT_SECRET.strip() or os.getenv("TABLEAU_PAT_SECRET", "").strip()
        if secret:
            result = result.replace(secret, "[REDACTED PAT SECRET]")
        return result


def run_tableau_export():
    """Alternative Automation Anywhere function name (also takes no args)."""
    return current_function()


if __name__ == "__main__":
    result = current_function()
    print(result)
    if result.startswith("ERROR |"):
        sys.exit(1)
