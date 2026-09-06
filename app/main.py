from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.types.json import Jsonb
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .auth import authenticated, valid_credentials
from .config import settings
from .db import connection, execute, fetch_all, fetch_one
from .exports import build_run_export
from .research import (
    CONFIRMATION_INITIAL_SESSIONS, CONFIRMATION_MAX_SESSIONS, FORWARD_INITIAL_SESSIONS, FORWARD_MAX_SESSIONS,
    EVIDENCE_VERSION, TRIGGER_RECIPES, discovery_evidence_is_current, frozen_config_payload, frozen_config_sha256,
)

NY = ZoneInfo("America/New_York")

settings.validate_web()
app = FastAPI(title="Alpaca Dip-Reversal Trigger Discovery & Confirmation Lab", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax", https_only=settings.session_cookie_secure)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_version"] = __version__
templates.env.globals["evidence_version"] = EVIDENCE_VERSION


def _require_auth(request: Request) -> RedirectResponse | None:
    return None if authenticated(request) else RedirectResponse("/login", status_code=303)


def _parse_symbols(value: str) -> list[str]:
    symbols = sorted({x.upper() for x in re.split(r"[\s,;]+", value.strip()) if x})
    invalid = [x for x in symbols if not re.fullmatch(r"[A-Z0-9.\-]{1,20}", x)]
    if invalid:
        raise ValueError("Invalid symbol(s): " + ", ".join(invalid[:10]))
    if len(symbols) > 250:
        raise ValueError("Manual-symbol runs are limited to 250 symbols")
    return symbols


def _parse_costs(value: str) -> list[int]:
    costs = sorted({int(x) for x in re.split(r"[\s,;]+", value.strip()) if x})
    if not costs or any(x < 0 or x > 500 for x in costs):
        raise ValueError("Costs must be between 0 and 500 basis points")
    return costs


def _clock(value: str, label: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"{label} must use HH:MM") from exc


@app.get("/health")
def health() -> dict[str, object]:
    runtime = fetch_one("select worker_status,heartbeat_at,last_error from public.dip_trigger_runtime where id=1")
    return {"status": "ok", "version": __version__, "app": "alpaca-dip-reversal-trigger-lab", "trading_enabled": False, "worker": runtime}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()]):
    if not valid_credentials(username, password):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"}, status_code=401)
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    runtime = fetch_one("select * from public.dip_trigger_runtime where id=1")
    runs = fetch_all("select * from public.dip_trigger_runs order by created_at desc limit 100")
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "runtime": runtime, "runs": runs, "default_costs": ",".join(map(str, settings.default_cost_bps)), "recipes": TRIGGER_RECIPES},
    )


@app.post("/runs")
def create_run(
    request: Request,
    name: Annotated[str, Form()],
    source_mode: Annotated[str, Form()],
    start_date: Annotated[date, Form()],
    end_date: Annotated[date, Form()],
    symbols_text: Annotated[str, Form()] = "",
    scanner_scan_types: Annotated[list[str] | None, Form()] = None,
    search_start_et: Annotated[str, Form()] = "09:45",
    search_end_et: Annotated[str, Form()] = "15:15",
    trigger_recipes: Annotated[list[str] | None, Form()] = None,
    target_net_pct: Annotated[float, Form()] = 3.0,
    target_gross_pct: Annotated[float, Form()] = 3.5,
    stop_loss_pct: Annotated[float, Form()] = 5.0,
    cost_bps: Annotated[str, Form()] = "15,20,50",
    min_price: Annotated[float, Form()] = 2.0,
    max_price: Annotated[float, Form()] = 50.0,
    min_dollar_volume: Annotated[float, Form()] = 5_000_000,
    min_drawdown_high_pct: Annotated[float, Form()] = 5.0,
    min_drawdown_open_pct: Annotated[float, Form()] = 2.0,
    min_below_vwap_pct: Annotated[float, Form()] = 1.0,
    oversold_memory_minutes: Annotated[int, Form()] = 15,
    volume_climax_ratio: Annotated[float, Form()] = 2.5,
    min_history_bars: Annotated[int, Form()] = 30,
):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    try:
        if not name.strip():
            raise ValueError("Run name is required")
        if source_mode not in {"scanner_alerts", "manual_symbols"}:
            raise ValueError("Unknown candidate source")
        if end_date < start_date:
            raise ValueError("End date must not precede start date")
        if end_date >= datetime.now(NY).date():
            raise ValueError("Use completed historical dates only")
        symbols = _parse_symbols(symbols_text)
        if source_mode == "manual_symbols" and not symbols:
            raise ValueError("Manual-symbol mode requires at least one symbol")
        scans = scanner_scan_types or ["pre_open", "midday"]
        if any(x not in {"pre_open", "midday"} for x in scans):
            raise ValueError("Unknown scanner scan type")
        recipes = trigger_recipes or []
        if not recipes:
            raise ValueError("Select at least one trigger recipe")
        if any(x not in TRIGGER_RECIPES for x in recipes):
            raise ValueError("Unknown trigger recipe")
        search_start = _clock(search_start_et, "Search start")
        search_end = _clock(search_end_et, "Search end")
        if search_start < time(9, 30) or search_end > time(15, 58) or search_start >= search_end:
            raise ValueError("Search window must fall within 09:30–15:58 ET")
        costs = _parse_costs(cost_bps)
        if target_gross_pct + 1e-9 < target_net_pct + max(costs) / 100:
            raise ValueError("Gross target must cover net target plus the highest configured cost")
        if not (0 < target_net_pct <= 50 and 0 < target_gross_pct <= 50 and 0 < stop_loss_pct <= 50):
            raise ValueError("Targets and stop must be between 0% and 50%")
        if min_price <= 0 or max_price <= min_price:
            raise ValueError("Maximum price must exceed minimum price")
        if min_dollar_volume < 0:
            raise ValueError("Minimum dollar volume cannot be negative")
        if min_drawdown_high_pct <= 0 or min_drawdown_open_pct < 0 or min_below_vwap_pct < 0:
            raise ValueError("Oversold thresholds must be non-negative and high drawdown must be positive")
        if not (5 <= oversold_memory_minutes <= 60):
            raise ValueError("Oversold memory must be between 5 and 60 minutes")
        if not (1.0 <= volume_climax_ratio <= 20):
            raise ValueError("Volume climax ratio must be between 1 and 20")
        if not (10 <= min_history_bars <= 120):
            raise ValueError("Minimum history bars must be between 10 and 120")
        if source_mode == "manual_symbols" and len(symbols) * ((end_date - start_date).days + 1) > 250_000:
            raise ValueError("Manual run too large; reduce symbols or dates")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = fetch_one(
        """
        insert into public.dip_trigger_runs(
          name,source_mode,start_date,end_date,symbols,scanner_scan_types,search_start_et,search_end_et,
          trigger_recipes,target_net_pct,target_gross_pct,stop_loss_pct,cost_bps,min_price,max_price,
          min_dollar_volume,min_drawdown_high_pct,min_drawdown_open_pct,min_below_vwap_pct,
          oversold_memory_minutes,volume_climax_ratio,min_history_bars
        ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        returning id
        """,
        (
            name.strip(), source_mode, start_date, end_date, Jsonb(symbols), Jsonb(scans), search_start, search_end,
            Jsonb(recipes), target_net_pct, target_gross_pct, stop_loss_pct, Jsonb(costs), min_price, max_price,
            min_dollar_volume, min_drawdown_high_pct, min_drawdown_open_pct, min_below_vwap_pct,
            oversold_memory_minutes, volume_climax_ratio, min_history_bars,
        ),
    )
    return RedirectResponse(f"/runs/{row['id']}", status_code=303)


def _completed_parent(parent_run_id: str) -> dict:
    parent = fetch_one("select * from public.dip_trigger_runs where id=%s", (parent_run_id,))
    if not parent:
        raise HTTPException(status_code=404, detail="Parent run not found")
    if parent.get("run_mode") in {"forward_sealed", "historical_sealed"}:
        raise HTTPException(status_code=400, detail="Create confirmation tests from the discovery run, not from another confirmation child")
    if parent["status"] not in {"completed", "completed_with_warnings"}:
        raise HTTPException(status_code=400, detail="The parent discovery run must be complete")
    if not discovery_evidence_is_current(parent.get("result_json") or {}):
        raise HTTPException(status_code=400, detail="Create a new discovery run with the current evidence checks before starting confirmation; the historical result remains available")
    recipe = str(parent.get("winner_recipe") or "")
    segment = str(parent.get("winner_segment") or "")
    if not recipe or recipe not in TRIGGER_RECIPES or not segment:
        raise HTTPException(status_code=400, detail="Freeze a winner recipe and segment before creating a confirmation test")
    return parent


def _insert_confirmation_child(
    parent: dict,
    run_mode: str,
    anchor_date: date,
    source_mode: str,
    symbols: list[str],
    backtest_scope: str | None,
) -> str:
    recipe = str(parent["winner_recipe"])
    segment = str(parent["winner_segment"])
    child_config = dict(parent)
    child_config.update({
        "run_mode": run_mode,
        "parent_run_id": parent["id"],
        "confirmation_anchor_date": anchor_date,
        "backtest_scope": backtest_scope,
        "source_mode": source_mode,
        "symbols": symbols,
    })
    frozen = frozen_config_payload(child_config, recipe, segment)
    digest = frozen_config_sha256(frozen)
    protocol = "true_forward_30_then_90" if run_mode == "forward_sealed" else "historical_sealed_30_then_90"
    mode_label = "Forward sealed" if run_mode == "forward_sealed" else "Historical sealed backtest"
    verdict = "forward_30_queued" if run_mode == "forward_sealed" else "backtest_30_queued"
    result = {
        "protocol": protocol,
        "parent_run_id": str(parent["id"]),
        "frozen_recipe": recipe,
        "frozen_segment": segment,
        "frozen_config_sha256": digest,
        "initial_sessions": CONFIRMATION_INITIAL_SESSIONS,
        "maximum_sessions": CONFIRMATION_MAX_SESSIONS,
        "confirmation_anchor_date": anchor_date.isoformat(),
        "backtest_scope": backtest_scope,
        "policy": "One frozen rule. No recipe, segment, threshold, cost or population changes after creation.",
    }
    row = fetch_one(
        """
        insert into public.dip_trigger_runs(
          name,run_mode,parent_run_id,confirmation_target_sessions,confirmation_max_sessions,confirmation_stage,
          confirmation_anchor_date,backtest_scope,forward_target_sessions,forward_max_sessions,forward_stage,
          frozen_config,frozen_config_sha256,source_mode,start_date,end_date,symbols,scanner_scan_types,
          search_start_et,search_end_et,trigger_recipes,target_net_pct,target_gross_pct,stop_loss_pct,cost_bps,
          min_price,max_price,min_dollar_volume,min_drawdown_high_pct,min_drawdown_open_pct,min_below_vwap_pct,
          oversold_memory_minutes,volume_climax_ratio,min_history_bars,discovery_ratio,validation_ratio,
          sealed_opened,winner_recipe,winner_segment,verdict,result_json
        ) values (
          %s,%s,%s,%s,%s,'initial_30',%s,%s,%s,%s,'initial_30',%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s,%s,%s
        ) returning id
        """,
        (
            f"{mode_label} 30-session test · {parent['name']}", run_mode, parent["id"],
            CONFIRMATION_INITIAL_SESSIONS, CONFIRMATION_MAX_SESSIONS, anchor_date, backtest_scope,
            CONFIRMATION_INITIAL_SESSIONS, CONFIRMATION_MAX_SESSIONS, Jsonb(frozen), digest,
            source_mode, anchor_date, anchor_date, Jsonb(symbols), Jsonb(parent.get("scanner_scan_types") or []),
            parent["search_start_et"], parent["search_end_et"], Jsonb([recipe]), parent["target_net_pct"],
            parent["target_gross_pct"], parent["stop_loss_pct"], Jsonb(parent.get("cost_bps") or []),
            parent["min_price"], parent["max_price"], parent["min_dollar_volume"],
            parent["min_drawdown_high_pct"], parent["min_drawdown_open_pct"], parent["min_below_vwap_pct"],
            parent["oversold_memory_minutes"], parent["volume_climax_ratio"], parent["min_history_bars"],
            parent["discovery_ratio"], parent["validation_ratio"], recipe, segment, verdict, Jsonb(result),
        ),
    )
    return str(row["id"])


@app.post("/runs/{parent_run_id}/forward")
def create_forward_run(request: Request, parent_run_id: str, start_date: Annotated[date, Form()]):
    """Create a true-forward sealed 30-session test strictly after the discovery period."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    parent = _completed_parent(parent_run_id)
    if parent["source_mode"] not in {"scanner_alerts", "manual_symbols"}:
        raise HTTPException(status_code=400, detail="Forward continuation requires scanner alerts or a frozen manual symbol universe")
    if start_date <= parent["end_date"]:
        raise HTTPException(status_code=400, detail="Forward start must be strictly after the parent run end date")
    if start_date >= datetime.now(NY).date():
        raise HTTPException(status_code=400, detail="The first forward date must already have occurred; the app then waits for 30 completed sessions")
    existing = fetch_one(
        "select id from public.dip_trigger_runs where parent_run_id=%s and run_mode='forward_sealed' order by created_at desc limit 1",
        (parent_run_id,),
    )
    if existing:
        return RedirectResponse(f"/runs/{existing['id']}", status_code=303)
    run_id = _insert_confirmation_child(
        parent, "forward_sealed", start_date, parent["source_mode"], list(parent.get("symbols") or []), None
    )
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{parent_run_id}/backtest")
def create_historical_backtest(
    request: Request,
    parent_run_id: str,
    end_date: Annotated[date, Form()],
    backtest_scope: Annotated[str, Form()] = "end_to_end",
):
    """Create an earlier, non-overlapping sealed historical 30-session test."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    parent = _completed_parent(parent_run_id)
    if end_date >= parent["start_date"]:
        raise HTTPException(status_code=400, detail="Historical backtest must end strictly before the discovery run begins")
    if end_date >= datetime.now(NY).date():
        raise HTTPException(status_code=400, detail="Historical backtest dates must be complete")
    if backtest_scope not in {"end_to_end", "frozen_parent_universe"}:
        raise HTTPException(status_code=400, detail="Unknown historical backtest scope")
    existing = fetch_one(
        "select id from public.dip_trigger_runs where parent_run_id=%s and run_mode='historical_sealed' order by created_at desc limit 1",
        (parent_run_id,),
    )
    if existing:
        return RedirectResponse(f"/runs/{existing['id']}", status_code=303)

    source_mode = parent["source_mode"]
    symbols = list(parent.get("symbols") or [])
    if backtest_scope == "end_to_end":
        if source_mode not in {"scanner_alerts", "manual_symbols"}:
            raise HTTPException(status_code=400, detail="End-to-end historical testing requires scanner alerts or a manual-symbol parent")
    else:
        rows = fetch_all(
            "select distinct upper(symbol) as symbol from public.dip_trigger_candidates where run_id=%s order by symbol",
            (parent_run_id,),
        )
        symbols = [str(row["symbol"]) for row in rows]
        if not symbols:
            raise HTTPException(status_code=400, detail="The parent run has no candidate symbols to freeze")
        if len(symbols) > 1000:
            raise HTTPException(status_code=400, detail="Frozen parent universe exceeds the 1,000-symbol safety limit")
        source_mode = "manual_symbols"

    run_id = _insert_confirmation_child(parent, "historical_sealed", end_date, source_mode, symbols, backtest_scope)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/extend-confirmation")
def extend_confirmation_run(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    run = fetch_one("select * from public.dip_trigger_runs where id=%s", (run_id,))
    if not run or run.get("run_mode") not in {"forward_sealed", "historical_sealed"}:
        raise HTTPException(status_code=404, detail="Sealed confirmation run not found")
    target = int(run.get("confirmation_target_sessions") or run.get("forward_target_sessions") or 0)
    if target != CONFIRMATION_INITIAL_SESSIONS:
        raise HTTPException(status_code=400, detail="This run is already extended")
    expected = "forward_30_pass_extension_available" if run["run_mode"] == "forward_sealed" else "backtest_30_pass_extension_available"
    if run.get("verdict") != expected:
        raise HTTPException(status_code=400, detail="The 30-session gate must pass before extension")
    prefix = "forward" if run["run_mode"] == "forward_sealed" else "backtest"
    execute(
        """
        update public.dip_trigger_runs
        set confirmation_target_sessions=%s,forward_target_sessions=%s,confirmation_stage='extension_to_90',
            forward_stage='extension_to_90',status='queued',stage=%s,verdict=%s,completed_at=null,last_error=null,
            cancel_requested=false,result_json=coalesce(result_json,'{}'::jsonb) ||
              jsonb_build_object('extension_authorised_at',now(),'confirmation_target_sessions',%s::int)
        where id=%s
        """,
        (CONFIRMATION_MAX_SESSIONS, CONFIRMATION_MAX_SESSIONS, f"{prefix}_90_queued", f"{prefix}_90_queued", CONFIRMATION_MAX_SESSIONS, run_id),
    )
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# Backwards-compatible v2.1 endpoint.
@app.post("/runs/{run_id}/extend-forward")
def extend_forward_run(request: Request, run_id: str):
    return extend_confirmation_run(request, run_id)


@app.post("/runs/{run_id}/recheck-forward")
def recheck_forward_run(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    run = fetch_one("select * from public.dip_trigger_runs where id=%s", (run_id,))
    if not run or run.get("run_mode") != "forward_sealed":
        raise HTTPException(status_code=404, detail="Forward run not found")
    if run.get("verdict") != "forward_window_incomplete":
        raise HTTPException(status_code=400, detail="This forward window is not waiting for additional completed sessions")
    execute(
        """
        update public.dip_trigger_runs
        set status='queued',stage='rechecking_forward_window',completed_at=null,last_error=null,cancel_requested=false
        where id=%s
        """,
        (run_id,),
    )
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/upload")
async def upload_candidates(
    request: Request,
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form()],
    search_start_et: Annotated[str, Form()] = "09:45",
    search_end_et: Annotated[str, Form()] = "15:15",
):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(status_code=400, detail="CSV exceeds 10 MB")
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        if not reader.fieldnames or not {"trade_date", "symbol"}.issubset(reader.fieldnames):
            raise ValueError("CSV must include trade_date and symbol")
        start_clock = _clock(search_start_et, "Search start")
        end_clock = _clock(search_end_et, "Search end")
        if start_clock >= end_clock:
            raise ValueError("Search start must precede search end")
        parsed: list[tuple[str, date, datetime]] = []
        for row in reader:
            symbol = row["symbol"].strip().upper()
            if not re.fullmatch(r"[A-Z0-9.\-]{1,20}", symbol):
                raise ValueError(f"Invalid symbol: {symbol}")
            trade_date = date.fromisoformat(row["trade_date"].strip())
            available_clock = _clock(row.get("available_at_et", "") or search_start_et, "available_at_et")
            parsed.append((symbol, trade_date, datetime.combine(trade_date, available_clock, tzinfo=NY)))
        if not parsed:
            raise ValueError("CSV is empty")
        parsed = sorted(set(parsed), key=lambda x: (x[1], x[0]))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    start_date = min(x[1] for x in parsed)
    end_date = max(x[1] for x in parsed)
    if end_date >= datetime.now(NY).date():
        raise HTTPException(status_code=400, detail="CSV must use completed dates only")
    default_recipes = ["deep_higher_low", "deep_prior_high_break", "capitulation_higher_low", "relative_strength_turn", "vwap_reclaim_after_deep"]
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into public.dip_trigger_runs(
                  name,source_mode,start_date,end_date,search_start_et,search_end_et,trigger_recipes,status,stage
                ) values (%s,'candidate_csv',%s,%s,%s,%s,%s,'cancelled','uploading_candidates') returning id
                """,
                (name.strip(), start_date, end_date, start_clock, end_clock, Jsonb(default_recipes)),
            )
            run_id = str(cur.fetchone()["id"])
            cur.executemany(
                """
                insert into public.dip_trigger_candidates(run_id,symbol,trade_date,source,source_scan_types,source_details,available_at)
                values (%s,%s,%s,'candidate_csv','[]'::jsonb,'{}'::jsonb,%s)
                on conflict (run_id,symbol,trade_date) do nothing
                """,
                [(run_id, symbol, trade_date, available_at) for symbol, trade_date, available_at in parsed],
            )
            cur.execute("update public.dip_trigger_runs set status='queued',stage='queued',candidate_count=(select count(*) from public.dip_trigger_candidates where run_id=%s) where id=%s", (run_id, run_id))
        conn.commit()
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    run = fetch_one("select * from public.dip_trigger_runs where id=%s", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    statuses = fetch_all("select status,count(*) as n from public.dip_trigger_candidates where run_id=%s group by status order by status", (run_id,))
    metrics = fetch_all(
        """
        select * from public.dip_trigger_metrics
        where run_id=%s and (%s or split <> 'sealed_test')
        order by split,cost_bps desc,worst_fold_mean_pct desc nulls last,median_net_return_pct desc nulls last
        """,
        (run_id, run["sealed_opened"]),
    )
    issues = fetch_all("select * from public.dip_trigger_issues where run_id=%s order by created_at desc limit 100", (run_id,))
    parent = fetch_one("select id,name,start_date,end_date,winner_recipe,winner_segment from public.dip_trigger_runs where id=%s", (run.get("parent_run_id"),)) if run.get("parent_run_id") else None
    is_confirmation = run.get("run_mode") in {"forward_sealed", "historical_sealed"}
    forward_child = fetch_one(
        "select id,status,verdict,coalesce(confirmation_target_sessions,forward_target_sessions) as target_sessions from public.dip_trigger_runs where parent_run_id=%s and run_mode='forward_sealed' order by created_at desc limit 1",
        (run_id,),
    ) if not is_confirmation else None
    backtest_child = fetch_one(
        "select id,status,verdict,coalesce(confirmation_target_sessions,forward_target_sessions) as target_sessions,backtest_scope from public.dip_trigger_runs where parent_run_id=%s and run_mode='historical_sealed' order by created_at desc limit 1",
        (run_id,),
    ) if not is_confirmation else None
    can_create_confirmation = bool(
        not is_confirmation
        and run.get("status") in {"completed", "completed_with_warnings"}
        and run.get("winner_recipe")
        and run.get("winner_segment")
        and run.get("source_mode") in {"scanner_alerts", "manual_symbols"}
    )
    target_sessions = int(run.get("confirmation_target_sessions") or run.get("forward_target_sessions") or CONFIRMATION_INITIAL_SESSIONS)
    confirmation_active = is_confirmation and run.get("status") in {"queued", "running"}
    return templates.TemplateResponse("run_detail.html", {
        "request": request, "run": run, "candidate_statuses": statuses, "metrics": metrics,
        "issues": issues, "recipes": TRIGGER_RECIPES, "parent": parent, "forward_child": forward_child,
        "backtest_child": backtest_child, "can_create_forward": can_create_confirmation and not forward_child,
        "can_create_backtest": can_create_confirmation and not backtest_child,
        "default_forward_start": run["end_date"] + timedelta(days=1),
        "default_backtest_end": run["start_date"] - timedelta(days=1),
        "confirmation_initial_sessions": CONFIRMATION_INITIAL_SESSIONS,
        "confirmation_max_sessions": CONFIRMATION_MAX_SESSIONS,
        "forward_initial_sessions": FORWARD_INITIAL_SESSIONS, "forward_max_sessions": FORWARD_MAX_SESSIONS,
        "confirmation_target_sessions": target_sessions,
        "confirmation_sealed_active": confirmation_active,
        "forward_sealed_active": confirmation_active,
    })


@app.post("/runs/{run_id}/cancel")
def cancel_run(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    execute("update public.dip_trigger_runs set cancel_requested=true,status=case when status='queued' then 'cancelled' else status end,stage=case when status='queued' then 'cancelled' else 'cancellation_requested' end,completed_at=case when status='queued' then now() else completed_at end where id=%s and status in ('queued','running')", (run_id,))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/retry")
def retry_run(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    execute("update public.dip_trigger_runs set status='queued',stage='queued_for_resume',cancel_requested=false,last_error=null where id=%s and status in ('failed','cancelled','completed_with_warnings')", (run_id,))
    execute("update public.dip_trigger_candidates set status='queued',last_error=null where run_id=%s and status='failed'", (run_id,))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/open-sealed")
def open_sealed(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    run = fetch_one("select * from public.dip_trigger_runs where id=%s", (run_id,))
    if not run or run["status"] not in {"completed", "completed_with_warnings"}:
        raise HTTPException(status_code=400, detail="Discovery and validation must be complete")
    if run["sealed_opened"]:
        raise HTTPException(status_code=400, detail="Sealed test already opened")
    if not run["winner_recipe"]:
        raise HTTPException(status_code=400, detail="No materially consistent validation trigger exists")
    execute("update public.dip_trigger_candidates set status='queued',last_error=null,completed_at=null where run_id=%s and split='sealed_test' and status='sealed'", (run_id,))
    execute("update public.dip_trigger_runs set sealed_opened=true,status='queued',stage='sealed_test_queued',cancel_requested=false,completed_at=null,last_error=null where id=%s", (run_id,))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}/export")
def export_run(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    run = fetch_one("select run_mode,status,stage from public.dip_trigger_runs where id=%s", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("run_mode") == "forward_sealed" and run.get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Forward results remain sealed until the complete target window has finished")
    if run.get("run_mode") == "historical_sealed" and run.get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Historical backtest results remain sealed until the complete target window has finished")
    filename, payload = build_run_export(run_id)
    return Response(content=payload, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
