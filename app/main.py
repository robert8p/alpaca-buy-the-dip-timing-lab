from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, time
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

NY = ZoneInfo("America/New_York")

settings.validate_web()
app = FastAPI(title="Alpaca Buy-the-Dip Timing Lab", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax", https_only=settings.session_cookie_secure)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _require_auth(request: Request) -> RedirectResponse | None:
    if not authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return None


def _parse_symbols(value: str) -> list[str]:
    symbols = sorted({x.upper() for x in re.split(r"[\s,;]+", value.strip()) if x})
    invalid = [symbol for symbol in symbols if not re.fullmatch(r"[A-Z0-9.\-]{1,20}", symbol)]
    if invalid:
        raise ValueError("Invalid symbol(s): " + ", ".join(invalid[:10]))
    if len(symbols) > 100:
        raise ValueError("Manual-symbol runs are limited to 100 predeclared symbols")
    return symbols


def _parse_times(value: str) -> list[str]:
    output: list[str] = []
    for raw in re.split(r"[\s,;]+", value.strip()):
        if not raw:
            continue
        parsed = datetime.strptime(raw, "%H:%M").strftime("%H:%M")
        if parsed < "09:31" or parsed > "15:59":
            raise ValueError(f"Entry time outside regular session: {parsed}")
        output.append(parsed)
    if not output:
        raise ValueError("At least one fixed entry time is required")
    return list(dict.fromkeys(output))


def _parse_costs(value: str) -> list[int]:
    costs = sorted({int(x) for x in re.split(r"[\s,;]+", value.strip()) if x})
    if not costs or any(x < 0 or x > 500 for x in costs):
        raise ValueError("Costs must be between 0 and 500 basis points")
    return costs


@app.get("/health")
def health() -> dict[str, object]:
    runtime = fetch_one("select worker_status, heartbeat_at, last_error from public.dip_runtime where id=1")
    return {
        "status": "ok",
        "version": __version__,
        "app": "alpaca-buy-the-dip-timing-lab",
        "trading_enabled": False,
        "worker": runtime,
    }


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
    runtime = fetch_one("select * from public.dip_runtime where id=1")
    runs = fetch_all("select * from public.dip_runs order by created_at desc limit 100")
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "runtime": runtime,
            "runs": runs,
            "default_costs": ",".join(str(x) for x in settings.default_cost_bps),
        },
    )


@app.post("/runs")
def create_run(
    request: Request,
    name: Annotated[str, Form()],
    source_mode: Annotated[str, Form()],
    start_date: Annotated[date, Form()],
    end_date: Annotated[date, Form()],
    symbols_text: Annotated[str, Form()] = "",
    cutoff_et: Annotated[str, Form()] = "12:30",
    fixed_entry_times_et: Annotated[str, Form()] = "12:31,12:35,12:45,13:00",
    confirmation_variants: Annotated[list[str] | None, Form()] = None,
    target_net_pct: Annotated[float, Form()] = 3.0,
    target_gross_pct: Annotated[float, Form()] = 3.5,
    stop_loss_pct: Annotated[float, Form()] = 5.0,
    cost_bps: Annotated[str, Form()] = "15,20,50",
    min_price: Annotated[float, Form()] = 2.0,
    max_price: Annotated[float, Form()] = 50.0,
    min_dollar_volume: Annotated[float, Form()] = 5000000,
    dip_from_high_pct: Annotated[float, Form()] = 3.0,
    dip_from_open_pct: Annotated[float, Form()] = 1.0,
    apply_dip_filter: Annotated[str | None, Form()] = None,
    require_below_vwap: Annotated[str | None, Form()] = None,
):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    try:
        if end_date < start_date:
            raise ValueError("End date must not precede start date")
        if end_date >= datetime.now(NY).date():
            raise ValueError("Use completed historical dates only; end date must be before today in New York")
        cutoff = datetime.strptime(cutoff_et, "%H:%M").time()
        times = _parse_times(fixed_entry_times_et)
        costs = _parse_costs(cost_bps)
        symbols = _parse_symbols(symbols_text)
        if source_mode not in {"scanner_alerts", "manual_symbols"}:
            raise ValueError("Unknown candidate source")
        if not name.strip():
            raise ValueError("Run name is required")
        if cutoff < time(9, 30) or cutoff >= time(15, 59):
            raise ValueError("Cutoff must fall within the regular session")
        if any(datetime.strptime(value, "%H:%M").time() <= cutoff for value in times):
            raise ValueError("Every fixed entry must be later than the signal cutoff")
        if not (0 < target_net_pct <= 50 and 0 < target_gross_pct <= 50 and 0 < stop_loss_pct <= 50):
            raise ValueError("Net target, gross target and stop must be greater than 0% and no more than 50%")
        if target_gross_pct + 1e-9 < target_net_pct + max(costs) / 100.0:
            raise ValueError("Gross target must cover the net target plus the highest configured cost")
        if min_price <= 0 or max_price <= min_price:
            raise ValueError("Maximum price must be greater than minimum price")
        if min_dollar_volume < 0 or dip_from_high_pct < 0 or dip_from_open_pct < 0:
            raise ValueError("Liquidity and dip thresholds cannot be negative")
        if source_mode == "manual_symbols" and not symbols:
            raise ValueError("Manual-symbol mode requires at least one symbol")
        if source_mode == "manual_symbols" and len(symbols) * ((end_date - start_date).days + 1) > 200_000:
            raise ValueError("Manual run is too large; reduce symbols or date range so symbol × calendar-day combinations do not exceed 200,000")
        confirmations = confirmation_variants or []
        allowed = {"higher_low", "vwap_reclaim", "prior_bar_high", "five_bar_break"}
        if any(x not in allowed for x in confirmations):
            raise ValueError("Unknown confirmation variant")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = fetch_one(
        """
        insert into public.dip_runs(
          name, source_mode, start_date, end_date, symbols, cutoff_et,
          fixed_entry_times_et, confirmation_variants, target_net_pct, target_gross_pct, stop_loss_pct,
          cost_bps, apply_dip_filter, min_price, max_price, min_dollar_volume,
          dip_from_high_pct, dip_from_open_pct, require_below_vwap
        ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        returning id
        """,
        (
            name.strip(), source_mode, start_date, end_date, Jsonb(symbols), cutoff,
            Jsonb(times), Jsonb(confirmations), target_net_pct, target_gross_pct, stop_loss_pct,
            Jsonb(costs), apply_dip_filter is not None, min_price, max_price, min_dollar_volume,
            dip_from_high_pct, dip_from_open_pct, require_below_vwap is not None,
        ),
    )
    return RedirectResponse(f"/runs/{row['id']}", status_code=303)


@app.post("/runs/upload")
async def upload_candidates(
    request: Request,
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form()],
    cutoff_et: Annotated[str, Form()] = "12:30",
    fixed_entry_times_et: Annotated[str, Form()] = "12:31,12:35,12:45,13:00",
    target_net_pct: Annotated[float, Form()] = 3.0,
    target_gross_pct: Annotated[float, Form()] = 3.5,
    stop_loss_pct: Annotated[float, Form()] = 5.0,
    cost_bps: Annotated[str, Form()] = "15,20,50",
):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(status_code=400, detail="CSV exceeds 10 MB")
    try:
        decoded = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))
        required = {"trade_date", "symbol"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError("CSV must include trade_date and symbol columns")
        records = list(reader)
        if not records:
            raise ValueError("CSV is empty")
        if len(records) > 100_000:
            raise ValueError("CSV exceeds the 100,000-candidate safety limit")
        cutoff = datetime.strptime(cutoff_et, "%H:%M").time()
        times = _parse_times(fixed_entry_times_et)
        costs = _parse_costs(cost_bps)
        if not name.strip():
            raise ValueError("Run name is required")
        if cutoff < time(9, 30) or cutoff >= time(15, 59):
            raise ValueError("Cutoff must fall within the regular session")
        if any(datetime.strptime(value, "%H:%M").time() <= cutoff for value in times):
            raise ValueError("Every fixed entry must be later than the signal cutoff")
        if not (0 < target_net_pct <= 50 and 0 < target_gross_pct <= 50 and 0 < stop_loss_pct <= 50):
            raise ValueError("Net target, gross target and stop must be greater than 0% and no more than 50%")
        if target_gross_pct + 1e-9 < target_net_pct + max(costs) / 100.0:
            raise ValueError("Gross target must cover the net target plus the highest configured cost")
        parsed: list[tuple[str, date]] = []
        for row in records:
            symbol = row["symbol"].strip().upper()
            if not re.fullmatch(r"[A-Z0-9.\-]{1,20}", symbol):
                raise ValueError(f"Invalid symbol: {symbol!r}")
            parsed.append((symbol, date.fromisoformat(row["trade_date"].strip())))
        parsed = sorted(set(parsed), key=lambda x: (x[1], x[0]))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    start_date = min(x[1] for x in parsed)
    end_date = max(x[1] for x in parsed)
    if end_date >= datetime.now(NY).date():
        raise HTTPException(status_code=400, detail="CSV must contain completed historical dates only")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into public.dip_runs(
                  name, source_mode, start_date, end_date, cutoff_et, fixed_entry_times_et,
                  confirmation_variants, target_net_pct, target_gross_pct, stop_loss_pct,
                  cost_bps, apply_dip_filter, status, stage
                ) values (%s,'candidate_csv',%s,%s,%s,%s,%s,%s,%s,%s,%s,false,'cancelled','uploading_candidates')
                returning id
                """,
                (
                    name.strip(), start_date, end_date, cutoff, Jsonb(times),
                    Jsonb(["higher_low", "vwap_reclaim", "prior_bar_high"]),
                    target_net_pct, target_gross_pct, stop_loss_pct, Jsonb(costs),
                ),
            )
            run_id = str(cur.fetchone()["id"])
            cur.executemany(
                """
                insert into public.dip_candidates(run_id, symbol, trade_date, cutoff_at, source)
                values (%s,%s,%s,%s,'candidate_csv')
                on conflict (run_id, symbol, trade_date) do nothing
                """,
                [
                    (run_id, symbol, trade_date, datetime.combine(trade_date, cutoff, tzinfo=NY))
                    for symbol, trade_date in parsed
                ],
            )
            cur.execute(
                """
                update public.dip_runs
                set status='queued', stage='queued', candidate_count=(select count(*) from public.dip_candidates where run_id=%s)
                where id=%s
                """,
                (run_id, run_id),
            )
        conn.commit()
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    run = fetch_one("select * from public.dip_runs where id=%s", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    candidates = fetch_all(
        """
        select status, count(*) as n from public.dip_candidates
        where run_id=%s group by status order by status
        """,
        (run_id,),
    )
    metrics = fetch_all(
        """
        select * from public.dip_metrics
        where run_id=%s and (%s or split <> 'sealed_test')
        order by split, cost_bps desc, net_target_success_rate_pct desc nulls last, median_net_return_pct desc nulls last
        """,
        (run_id, run["sealed_opened"]),
    )
    issues = fetch_all("select * from public.dip_issues where run_id=%s order by created_at desc limit 100", (run_id,))
    return templates.TemplateResponse(
        "run_detail.html",
        {"request": request, "run": run, "candidate_statuses": candidates, "metrics": metrics, "issues": issues},
    )


@app.post("/runs/{run_id}/cancel")
def cancel_run(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    execute(
        """
        update public.dip_runs
        set cancel_requested=true,
            status=case when status='queued' then 'cancelled' else status end,
            stage=case when status='queued' then 'cancelled' else 'cancellation_requested' end,
            completed_at=case when status='queued' then now() else completed_at end
        where id=%s and status in ('queued','running')
        """,
        (run_id,),
    )
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/retry")
def retry_run(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    execute(
        """
        update public.dip_runs
        set status='queued', stage='queued_for_resume', cancel_requested=false, last_error=null
        where id=%s and status in ('failed','cancelled','completed_with_warnings')
        """,
        (run_id,),
    )
    execute("update public.dip_candidates set status='queued', last_error=null where run_id=%s and status='failed'", (run_id,))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/open-sealed")
def open_sealed(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    run = fetch_one("select * from public.dip_runs where id=%s", (run_id,))
    if not run or run["status"] not in {"completed", "completed_with_warnings"}:
        raise HTTPException(status_code=400, detail="Discovery and validation must be complete")
    if run["sealed_opened"]:
        raise HTTPException(status_code=400, detail="Sealed test has already been opened")
    failed = fetch_one("select count(*) as n from public.dip_candidates where run_id=%s and status='failed'", (run_id,)) or {"n": 0}
    if int(failed["n"]) > 0:
        raise HTTPException(status_code=400, detail="Resolve failed discovery/validation candidates before opening the sealed test")
    if not run["winner_variant"]:
        raise HTTPException(status_code=400, detail="No validation winner exists to test")
    execute(
        """
        update public.dip_candidates
        set status='queued', last_error=null, completed_at=null
        where run_id=%s and split='sealed_test' and status='sealed'
        """,
        (run_id,),
    )
    execute(
        """
        update public.dip_runs
        set sealed_opened=true, status='queued', stage='sealed_test_queued',
            cancel_requested=false, completed_at=null, last_error=null
        where id=%s
        """,
        (run_id,),
    )
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}/export")
def export_run(request: Request, run_id: str):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    filename, payload = build_run_export(run_id)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
