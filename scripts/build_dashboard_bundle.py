#!/usr/bin/env python3
"""Build a local dashboard app and a portable single-file export.

The app loads normalized daily data from JSON. The portable export embeds the
same normalized data and remains suitable for archival or offline sharing.
Provider credentials are never read or written by this command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import build_enriched_morning_dashboard as dashboard
from private_artifacts import write_private_bytes


SCHEMA_VERSION = "codex-screener-bundle-v1"


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} JSON must be an object")
    return dict(value)


def _atomic_write(path: Path, content: bytes) -> None:
    write_private_bytes(path, content)


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() == content:
            return
        raise FileExistsError(f"immutable publication already exists with different content: {path}")
    _atomic_write(path, content)


def _assert_immutable_compatible(path: Path, content: bytes) -> None:
    if path.exists() and path.read_bytes() != content:
        raise FileExistsError(f"immutable publication already exists with different content: {path}")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _split_fragment(fragment: str) -> tuple[str, str, str]:
    styles = re.findall(r"<style>(.*?)</style>", fragment, flags=re.DOTALL | re.IGNORECASE)
    scripts = re.findall(r"<script>(.*?)</script>", fragment, flags=re.DOTALL | re.IGNORECASE)
    if not styles or len(scripts) != 1:
        raise ValueError("dashboard fragment must contain styles and exactly one script")
    markup = re.sub(r"<meta\s+charset=[^>]+>", "", fragment, flags=re.IGNORECASE)
    markup = re.sub(r"<style>.*?</style>", "", markup, flags=re.DOTALL | re.IGNORECASE)
    markup = re.sub(r"<script>.*?</script>", "", markup, flags=re.DOTALL | re.IGNORECASE)
    return markup.strip(), "\n".join(styles).strip() + "\n", scripts[0].strip() + "\n"


def _externalize_data(script: str) -> str:
    marker = "const DATA="
    start = script.find(marker)
    if start < 0:
        raise ValueError("inline dashboard data declaration was not found")
    value_start = start + len(marker)
    _, consumed = json.JSONDecoder().raw_decode(script[value_start:])
    value_end = value_start + consumed
    if script[value_end:value_end + 1] != ";":
        raise ValueError("inline dashboard data declaration is malformed")
    replacement = """const response=await fetch('./data/latest.json',{cache:'no-store'});
if(!response.ok)throw new Error(`Dashboard data request failed (${response.status})`);
let DATA=await response.json();
let replayActive=false,replaySelection='',navigationEpoch=0,appliedDigest=null;
let refreshFailures=0,refreshInFlight=false;
try{
  const catalogResponse=await fetch('./data/publications.json',{cache:'no-store'});
  if(catalogResponse.ok)DATA.publications=await catalogResponse.json();
}catch{}
document.getElementById('app-status').hidden=true"""
    external = script[:start] + replacement + script[value_end + 1:]
    external = external.replace(
        "let publicationLoader=null;",
        "const publicationLoader=async url=>{const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error(`Replay load failed (${response.status})`);return response.json()};",
        1,
    )
    external = re.sub(r"function renderReplay\(\)\{[^\n]*", """function renderReplay(){
  const select=byId('me-replay'),entries=DATA.publications?.entries||[];
  if(!publicationLoader)return;
  select.hidden=false;
  select.innerHTML=`<option value="">Live · latest stored publication</option>${entries.map(row=>`<option value="${esc(row.url)}">${esc(row.date)} · ${esc(row.kind?.replaceAll('_',' '))}</option>`).join('')}`;
  select.value=replaySelection;
  select.onchange=async()=>{
    const url=select.value,epoch=++navigationEpoch,previousReplay=replayActive;
    replayActive=Boolean(url);
    try{
      const next=await publicationLoader(url||'./data/latest.json');
      if(epoch!==navigationEpoch)return;
      if(!next||!Array.isArray(next.entries))throw new Error('Invalid publication');
      applyPublication(next);
      replaySelection=url;
      appliedDigest=null;
      refreshFailures=0;
      renderAvailability();
    }catch(error){
      if(epoch!==navigationEpoch)return;
      replayActive=previousReplay;
      select.value=replaySelection;
      showRefreshError(error);
    }
  };
}""", external, count=1)
    if not external.startswith("(()=>{"):
        raise ValueError("dashboard script must use the expected IIFE wrapper")
    external = "(async()=>{" + external[len("(()=>{"):]
    ending = "})();"
    end = external.rfind(ending)
    if end < 0:
        raise ValueError("dashboard script closing wrapper was not found")
    live_refresh = """
function renderAvailability(){
  const status=byId('app-status'),cutoff=Date.parse(DATA.asOf),age=(Date.now()-cutoff)/3600000;
  status.hidden=false;
  if(replayActive){status.textContent=`REPLAY — frozen publication as of ${DATA.asOf}. Automatic refresh is paused until you select Live.`;return}
  if(refreshFailures){status.textContent=`REFRESH FAILED (${refreshFailures}) — showing the last stored publication as of ${DATA.asOf}. No new data is confirmed.`;return}
  if(!Number.isFinite(age)||age>6){status.textContent=`${DATA.mode==='RETROSPECTIVE_REPROCESSING'?'RETROSPECTIVE RECALCULATION · ':''}STALE STORED DATA — as of ${DATA.asOf||'unknown'}${Number.isFinite(age)?` (${age.toFixed(1)} hours old)`:''}. This is not a live market quote.`;return}
  status.textContent=`Latest stored publication as of ${DATA.asOf}. Research only; not a live market quote.`;
}
function showRefreshError(error){refreshFailures+=1;renderAvailability();console.error(error)}
function applyPublication(nextData){
  const selectedTicker=DATA.entries[selected]?.ticker,publications=DATA.publications;
  DATA=nextData;
  DATA.publications=publications;
  selected=Math.max(0,DATA.entries.findIndex(item=>item.ticker===selectedTicker));
  setRunHeader();renderConsensusPulse();renderSystemStatus();renderMacro();renderRanking();renderWatchAlerts();renderWatchControls();renderControls();renderEvaluation();renderPlatformTracking();renderPlatformDetails();renderSelected();
}
async function refreshLatest(){
  if(document.hidden||replayActive||refreshInFlight)return;
  refreshInFlight=true;
  const epoch=navigationEpoch;
  try{
  const manifestResponse=await fetch('./data/live-status.json',{cache:'no-store'});
  if(!manifestResponse.ok)throw new Error(`Publication status failed (${manifestResponse.status})`);
  const manifest=await manifestResponse.json();
  if(!/^[a-f0-9]{64}$/.test(manifest.sha256||''))throw new Error('Invalid publication status');
  if(epoch!==navigationEpoch||replayActive)return;
  if(manifest.sha256===appliedDigest){refreshFailures=0;renderAvailability();return}
  const nextResponse=await fetch('./data/latest.json',{cache:'no-store'});
  if(!nextResponse.ok)throw new Error(`Dashboard refresh failed (${nextResponse.status})`);
  const body=await nextResponse.text();
  const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body))),value=>value.toString(16).padStart(2,'0')).join('');
  if(hash!==manifest.sha256)throw new Error('Publication changed during refresh; waiting for a coherent version');
  const nextData=JSON.parse(body);
  if(!nextData||!Array.isArray(nextData.entries))throw new Error('Dashboard refresh payload is invalid');
  if(epoch!==navigationEpoch||replayActive)return;
  applyPublication(nextData);
  appliedDigest=hash;
  refreshFailures=0;
  renderAvailability();
  }finally{refreshInFlight=false}
}
const pollSeconds=Math.max(15,Math.min(120,Number(DATA.refresh?.pollSeconds)||30));
setInterval(()=>refreshLatest().catch(showRefreshError),pollSeconds*1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshLatest().catch(showRefreshError)});
renderAvailability();
"""
    external = external[:end] + live_refresh + external[end:]
    end = external.rfind(ending)
    failure = """})().catch(error=>{
const status=document.getElementById('app-status');
if(status){status.hidden=false;status.textContent=`Dashboard could not load: ${error.message}. Run the local server; direct file:// loading is not supported.`}
console.error(error);
});"""
    return external[:end] + failure + external[end + len(ending):]


def _document(markup: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="color-scheme" content="dark light">
  <title>Codex Screener</title>
  <link rel="stylesheet" href="./assets/app.css">
</head>
<body>
  <p id="app-status" role="status">Loading stored dashboard data…</p>
  {markup}
  <script src="./assets/app.js" defer></script>
</body>
</html>
"""


def build_shell(*, run: Mapping[str, Any], app_root: Path) -> dict[str, Any]:
    """Update only the reusable app shell; do not modify stored publications."""

    fragment = dashboard.build_fragment(run)
    markup, css, inline_script = _split_fragment(fragment)
    app_script = _externalize_data(inline_script)
    shell_css = """html,body{margin:0;min-height:100%;background:#050706}body{padding:0;color:#e5ebe5}#app-status{margin:0;padding:10px 14px;background:#171309;color:#f6df68;font:12px ui-monospace,SFMono-Regular,Consolas,monospace}@media(max-width:460px){#app-status{padding:9px 12px}}\n"""
    content = {
        "index": _document(markup).encode("utf-8"),
        "css": (shell_css + css).encode("utf-8"),
        "javascript": app_script.encode("utf-8"),
    }
    paths = {
        "index": app_root / "index.html",
        "css": app_root / "assets" / "app.css",
        "javascript": app_root / "assets" / "app.js",
    }
    for key, path in paths.items():
        _atomic_write(path, content[key])
    return {
        key: {"path": str(path), "sha256": _digest(path.read_bytes()), "bytes": path.stat().st_size}
        for key, path in paths.items()
    }


def publish_latest_data(*, run: Mapping[str, Any], app_root: Path) -> dict[str, Any]:
    """Atomically publish one normalized intraday view without changing dated data."""

    normalized = dashboard.normalize_run(run)
    content = (json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    latest = app_root / "data" / "latest.json"
    status = app_root / "data" / "live-status.json"
    _atomic_write(latest, content)
    status_payload = {
        "schema_version": "codex-screener-live-status-v1",
        "data_version": normalized.get("dataVersion"),
        "cutoff_at": normalized.get("asOf"),
        "generated_at": normalized.get("generatedAt"),
        "entry_count": len(normalized.get("entries", [])),
        "sha256": _digest(content),
        "research_only": True,
    }
    _atomic_write(status, (json.dumps(status_payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return status_payload | {"path": str(latest), "bytes": len(content)}


def build_bundle(
    *,
    run: Mapping[str, Any],
    app_root: Path,
    portable_output: Path,
    archive_reference: Path | None = None,
) -> dict[str, Any]:
    normalized = dashboard.normalize_run(run)
    run_id = str(run.get("run_id") or normalized.get("asOf") or "unknown-run")
    run_date = str(normalized.get("asOf") or run.get("cutoff_at") or run_id)[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_date):
        raise ValueError("run_id must begin with an ISO date")

    fragment = dashboard.build_fragment(run)
    markup, css, inline_script = _split_fragment(fragment)
    app_script = _externalize_data(inline_script)
    shell_css = """html,body{margin:0;min-height:100%;background:#050706}body{padding:0;color:#e5ebe5}#app-status{margin:0;padding:10px 14px;background:#171309;color:#f6df68;font:12px ui-monospace,SFMono-Regular,Consolas,monospace}@media(max-width:460px){#app-status{padding:9px 12px}}\n"""

    data_content = (json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    css_content = (shell_css + css).encode("utf-8")
    js_content = app_script.encode("utf-8")
    html_content = _document(markup).encode("utf-8")
    portable_content = fragment.encode("utf-8")

    canonical_directory = app_root / "data" / run_date
    canonical_data = canonical_directory / "run.json"
    # A date is the immutable first publication. Same-day research revisions
    # use a content-addressed directory so the canonical archive is preserved.
    daily_directory = (
        canonical_directory
        if not canonical_data.exists() or canonical_data.read_bytes() == data_content
        else app_root / "data" / "publications" / run_date / _digest(data_content)[:16]
    )
    paths = {
        "index": app_root / "index.html",
        "css": app_root / "assets" / "app.css",
        "javascript": app_root / "assets" / "app.js",
        "daily_data": daily_directory / "run.json",
        "latest_data": app_root / "data" / "latest.json",
        "portable": portable_output,
    }
    _assert_immutable_compatible(paths["portable"], portable_content)
    _write_immutable(paths["daily_data"], data_content)
    _write_immutable(paths["portable"], portable_content)
    _atomic_write(paths["index"], html_content)
    _atomic_write(paths["css"], css_content)
    _atomic_write(paths["javascript"], js_content)
    _atomic_write(paths["latest_data"], data_content)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_date": run_date,
        "publication_kind": "CANONICAL_DATE" if daily_directory == canonical_directory else "CONTENT_ADDRESSED_REVISION",
        "cutoff_at": run.get("cutoff_at"),
        "generated_at": run.get("generated_at"),
        "entry_count": len(normalized.get("entries", [])),
        "research_only": True,
        "credentials_embedded": False,
        "files": {
            key: {"path": str(path), "sha256": _digest(path.read_bytes()), "bytes": path.stat().st_size}
            for key, path in paths.items()
        },
        "archive_reference": (
            {"path": str(archive_reference), "sha256": _digest(archive_reference.read_bytes())}
            if archive_reference is not None
            else None
        ),
    }
    manifest_content = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path = daily_directory / "manifest.json"
    _write_immutable(manifest_path, manifest_content)
    _update_publication_catalog(app_root)
    publish_latest_data(run=run, app_root=app_root)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _update_publication_catalog(app_root: Path) -> None:
    """Publish a deterministic index of immutable dated and revision artifacts."""

    data_root = app_root / "data"
    entries: list[dict[str, Any]] = []
    for manifest_path in sorted(data_root.rglob("manifest.json")):
        try:
            manifest = _read_object(manifest_path, "publication manifest")
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        daily = manifest.get("files", {}).get("daily_data", {}).get("path")
        if not isinstance(daily, str):
            continue
        run_path = Path(daily)
        try:
            relative = run_path.resolve().relative_to(app_root.resolve())
        except ValueError:
            continue
        entries.append({
            "runId": manifest.get("run_id"),
            "date": manifest.get("run_date"),
            "generatedAt": manifest.get("generated_at"),
            "kind": manifest.get("publication_kind"),
            "url": "./" + relative.as_posix(),
        })
    entries.sort(key=lambda item: (str(item.get("date")), str(item.get("generatedAt")), str(item.get("runId"))), reverse=True)
    payload = {"schema_version": "codex-screener-publications-v1", "entries": entries}
    _atomic_write(data_root / "publications.json", (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--enhanced-input", type=Path)
    parser.add_argument("--evaluation-input", type=Path)
    parser.add_argument("--previous-input", type=Path)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--portable-output", type=Path)
    parser.add_argument("--archive-reference", type=Path)
    parser.add_argument("--shell-only", action="store_true")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Rebuild the local app shell and latest data without a portable export",
    )
    args = parser.parse_args(argv)

    if args.shell_only and args.local_only:
        raise SystemExit("--shell-only and --local-only are mutually exclusive")

    run = _read_object(args.input, "input")
    for argument, key, label in (
        (args.enhanced_input, "enhanced_summary", "enhanced input"),
        (args.evaluation_input, "model_evaluation", "evaluation input"),
        (args.previous_input, "previous_run", "previous input"),
    ):
        if argument is not None:
            run[key] = _read_object(argument, label)
    if args.shell_only:
        manifest = {"shell_only": True, "files": build_shell(run=run, app_root=args.app_root)}
    elif args.local_only:
        manifest = {
            "local_only": True,
            "files": build_shell(run=run, app_root=args.app_root),
            "latest": publish_latest_data(run=run, app_root=args.app_root),
        }
    else:
        if args.portable_output is None:
            raise SystemExit("--portable-output is required unless --shell-only or --local-only is used")
        manifest = build_bundle(
            run=run,
            app_root=args.app_root,
            portable_output=args.portable_output,
            archive_reference=args.archive_reference,
        )
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
