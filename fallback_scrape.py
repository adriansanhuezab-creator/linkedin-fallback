"""
Respaldo de LinkedIn para el Daily Job Pipeline de Adrián.

Corre a diario en GitHub Actions ANTES del Apify scheduled run:
  1. Consulta la cuota mensual de Apify (/v2/users/me/limits).
  2. Si queda cuota suficiente  -> no scrapea (status "skipped").
     Si la cuota se agotó        -> scrapea LinkedIn con JobSpy (status "scraped").
     Si no se puede consultar    -> scrapea igual, por seguridad (status "scraped").
     FORCE=true                  -> scrapea siempre.
  3. Escribe data/meta.json + data/page_N.json (10 avisos por página) con los
     MISMOS nombres de campo que el dataset de Apify, para que la rutina de
     Claude los lea igual que hoy (vía raw.githubusercontent.com).
"""

from __future__ import annotations

import json
import math
import os
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- config ----
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
FORCE = os.environ.get("FORCE", "false").strip().lower() == "true"
# Un run del task cuesta ~USD 0.17; bajo este margen se considera "sin cuota".
MIN_REMAINING_USD = float(os.environ.get("MIN_REMAINING_USD", "0.25"))

# Misma búsqueda que el task de Apify (linkedin-jobs-scraper-task).
SEARCH_TERM = os.environ.get(
    "SEARCH_TERM",
    "BI Analyst OR Financial Analyst OR Data Consultant OR Automation Analyst OR Analytics Engineer",
)
LOCATION = os.environ.get("LOCATION", "Chile")
HOURS_OLD = int(os.environ.get("HOURS_OLD", "26"))  # 24 h + margen
RESULTS_WANTED = int(os.environ.get("RESULTS_WANTED", "60"))
DESC_MAX = 1900  # límite de la propiedad "Job Description" en Notion
PAGE_SIZE = 10

OUT_DIR = Path(__file__).resolve().parent.parent / "data"

JOB_TYPE_MAP = {
    "fulltime": "Full-time",
    "parttime": "Part-time",
    "contract": "Contract",
    "internship": "Internship",
    "temporary": "Temporary",
}


# --------------------------------------------------------------- helpers ----
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(value) -> str:
    """Convierte NaN/None/fechas/listas a texto limpio."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    if isinstance(value, (list, tuple)):
        return ", ".join(clean(v) for v in value if clean(v))
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def apify_quota() -> dict | None:
    """Devuelve el uso mensual de Apify o None si no se pudo consultar."""
    if not APIFY_TOKEN:
        return None
    try:
        r = requests.get(
            "https://api.apify.com/v2/users/me/limits",
            params={"token": APIFY_TOKEN},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()["data"]
        used = float(d["current"]["monthlyUsageUsd"])
        cap = float(d["limits"]["maxMonthlyUsageUsd"])
        return {
            "monthly_usage_usd": round(used, 4),
            "max_monthly_usage_usd": cap,
            "remaining_usd": round(cap - used, 4),
            "cycle_start": d.get("monthlyUsageCycle", {}).get("startAt"),
            "cycle_end": d.get("monthlyUsageCycle", {}).get("endAt"),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] No se pudo consultar la cuota de Apify: {exc}")
        return None


def format_salary(row) -> str:
    def num(v) -> str:
        v = clean(v)
        try:
            return f"{float(v):,.0f}"
        except ValueError:
            return v

    lo, hi = num(row.get("min_amount")), num(row.get("max_amount"))
    if not lo and not hi:
        return ""
    cur = clean(row.get("currency"))
    interval = clean(row.get("interval"))
    rng = f"{lo}–{hi}" if lo and hi and lo != hi else (lo or hi)
    return " ".join(p for p in [cur, rng]) + (f"/{interval}" if interval else "")


def format_job_type(value) -> str:
    raw = clean(value).lower()
    if not raw:
        return ""
    parts = [p.strip() for p in raw.replace("|", ",").split(",") if p.strip()]
    return ", ".join(JOB_TYPE_MAP.get(p, p) for p in parts)


def to_item(row) -> dict:
    """Fila de JobSpy -> ítem con los nombres de campo del dataset de Apify."""
    job_id = clean(row.get("id"))
    if job_id.startswith("li-"):
        job_id = job_id[3:]
    work = clean(row.get("work_from_home_type"))
    is_remote = row.get("is_remote")
    return {
        "id": job_id,
        "title": clean(row.get("title")),
        "companyName": clean(row.get("company")),
        "location": clean(row.get("location")),
        "postedAt": clean(row.get("date_posted"))[:10],
        "salary": format_salary(row),
        "employmentType": format_job_type(row.get("job_type")),
        "industries": clean(row.get("company_industry")),
        "applyUrl": clean(row.get("job_url_direct")),
        "linkedinUrl": clean(row.get("job_url")) or (f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else ""),
        "isRemote": bool(is_remote) if clean(is_remote) else None,
        "workFromHomeType": work,
        "jobLevel": clean(row.get("job_level")),
        "descriptionText": clean(row.get("description"))[:DESC_MAX],
    }


def scrape() -> list[dict]:
    from jobspy import scrape_jobs  # import tardío: no se necesita si se omite

    kwargs = dict(
        site_name=["linkedin"],
        search_term=SEARCH_TERM,
        location=LOCATION,
        hours_old=HOURS_OLD,
        results_wanted=RESULTS_WANTED,
        description_format="plain",
        verbose=1,
    )
    try:
        df = scrape_jobs(fetch_description=True, **kwargs)
    except TypeError:  # versiones antiguas de JobSpy
        df = scrape_jobs(linkedin_fetch_description=True, **kwargs)

    items, seen = [], set()
    for row in df.to_dict(orient="records"):
        item = to_item(row)
        if not item["id"] or item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append(item)
    return items


def write_output(meta: dict, items: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("page_*.json"):
        old.unlink()
    pages = []
    for i in range(0, len(items), PAGE_SIZE):
        name = f"page_{i // PAGE_SIZE}.json"
        chunk = [dict(item, offset=i + k) for k, item in enumerate(items[i : i + PAGE_SIZE])]
        (OUT_DIR / name).write_text(json.dumps(chunk, ensure_ascii=False, indent=1), encoding="utf-8")
        pages.append(name)
    meta.update(item_count=len(items), page_size=PAGE_SIZE, pages=pages)
    (OUT_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=1))


# ------------------------------------------------------------------ main ----
def main() -> int:
    quota = apify_quota()
    meta = {
        "source": "jobspy-linkedin",
        "generated_at": now_iso(),
        "search_term": SEARCH_TERM,
        "location": LOCATION,
        "hours_old": HOURS_OLD,
        "apify": quota,
    }

    if FORCE:
        reason = "forced (workflow_dispatch)"
    elif quota is None:
        reason = "apify quota check failed"
    elif quota["remaining_usd"] < MIN_REMAINING_USD:
        reason = f"apify quota exhausted (remaining USD {quota['remaining_usd']})"
    else:
        meta.update(status="skipped", reason=f"apify has quota (remaining USD {quota['remaining_usd']})")
        write_output(meta, [])
        return 0

    meta["reason"] = reason
    try:
        items = scrape()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        meta.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
        write_output(meta, [])
        return 1  # marca el workflow como fallido -> GitHub envía email

    meta["status"] = "scraped"
    write_output(meta, items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
