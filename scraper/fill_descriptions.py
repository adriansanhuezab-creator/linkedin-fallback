"""
Completa la propiedad "Job Description" de Job Applications (Notion) con el
texto COMPLETO del aviso (hasta 1.900 caracteres), directo vía la API de Notion.

Por qué existe: la rutina de Claude lee los avisos con una herramienta que no
permite copiar más de ~125 caracteres textuales, así que deja solo un extracto.
Este script corre en GitHub Actions después de la rutina y reemplaza esos
extractos por el texto original.

Qué filas toca (todas deben cumplirse):
  - Source = LinkedIn y Date Found dentro de los últimos DAYS_BACK días
  - "CV Generated" sin marcar
  - Job Description vacía, de menos de MIN_OK_CHARS caracteres, o que empiece
    con "[Resumen" / "[Extracto" (marcas que deja la rutina)
  - Se encontró un texto más largo que el actual

De dónde saca el texto (en este orden):
  1. data/page_*.json de este repo (resultado de JobSpy)
  2. Los últimos datasets del task de Apify
  3. La página pública del aviso en LinkedIn (jobs-guest), con pausas
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "9b8e56cf-df7d-4de2-9a68-469d4f7a787d")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
APIFY_TASK_ID = os.environ.get("APIFY_TASK_ID", "9DrHciQIojkUDRhod")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "3"))
MIN_OK_CHARS = int(os.environ.get("MIN_OK_CHARS", "600"))
MAX_LINKEDIN_FETCHES = int(os.environ.get("MAX_LINKEDIN_FETCHES", "40"))
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
DESC_MAX = 1900

NOTION_VERSION = "2022-06-28"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MARKERS = ("[Resumen", "[Extracto")

session = requests.Session()


# ---------------------------------------------------------------- Notion ----
def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def plain(rich: list) -> str:
    return "".join(part.get("plain_text", "") for part in rich or [])


def notion_candidates() -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).date().isoformat()
    body = {
        "page_size": 100,
        "filter": {
            "and": [
                {"property": "Source", "select": {"equals": "LinkedIn"}},
                {"property": "Date Found", "date": {"on_or_after": since}},
                {"property": "CV Generated", "checkbox": {"equals": False}},
            ]
        },
    }
    rows, cursor = [], None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        r = session.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=notion_headers(), json=body, timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        for page in data["results"]:
            props = page["properties"]
            job_id = plain(props.get("Job ID", {}).get("rich_text"))
            desc = plain(props.get("Job Description", {}).get("rich_text"))
            title = plain(props.get("Job Title", {}).get("title"))
            if not re.fullmatch(r"\d{6,}", job_id or ""):
                continue  # solo IDs numéricos de LinkedIn
            if len(desc) >= MIN_OK_CHARS and not desc.startswith(MARKERS):
                continue
            rows.append({"page_id": page["id"], "job_id": job_id, "title": title, "desc": desc})
        if not data.get("has_more"):
            return rows
        cursor = data.get("next_cursor")


def notion_update(page_id: str, text: str) -> None:
    r = session.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=notion_headers(),
        json={"properties": {"Job Description": {"rich_text": [{"type": "text", "text": {"content": text}}]}}},
        timeout=60,
    )
    r.raise_for_status()


# --------------------------------------------------------------- sources ----
def from_repo() -> dict[str, str]:
    out = {}
    for f in sorted(DATA_DIR.glob("page_*.json")):
        try:
            for item in json.loads(f.read_text(encoding="utf-8")):
                if item.get("id") and item.get("descriptionText"):
                    out[str(item["id"])] = item["descriptionText"]
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {f.name}: {exc}")
    return out


def from_apify(limit_runs: int = 4) -> dict[str, str]:
    if not APIFY_TOKEN:
        return {}
    out = {}
    try:
        r = session.get(
            f"https://api.apify.com/v2/actor-tasks/{APIFY_TASK_ID}/runs",
            params={"token": APIFY_TOKEN, "desc": 1, "limit": limit_runs, "status": "SUCCEEDED"},
            timeout=60,
        )
        r.raise_for_status()
        for run in r.json()["data"]["items"]:
            ds = run.get("defaultDatasetId")
            if not ds:
                continue
            items = session.get(
                f"https://api.apify.com/v2/datasets/{ds}/items",
                params={"token": APIFY_TOKEN, "fields": "id,descriptionText", "clean": 1},
                timeout=120,
            ).json()
            for item in items:
                if item.get("id") and item.get("descriptionText"):
                    out.setdefault(str(item["id"]), item["descriptionText"])
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Apify: {exc}")
    return out


def from_linkedin(job_id: str) -> str:
    from bs4 import BeautifulSoup  # viene con python-jobspy

    r = session.get(
        f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"[warn] LinkedIn {job_id}: HTTP {r.status_code}")
        return ""
    node = BeautifulSoup(r.text, "html.parser").select_one(".show-more-less-html__markup")
    return node.get_text("\n", strip=True) if node else ""


def tidy(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:DESC_MAX]


# ------------------------------------------------------------------ main ----
def main() -> int:
    if not NOTION_TOKEN:
        print("[error] Falta el secreto NOTION_TOKEN.")
        return 1

    rows = notion_candidates()
    print(f"Filas por completar: {len(rows)}")
    if not rows:
        return 0

    repo, apify = from_repo(), None
    updated, missing, errors, li_calls = [], [], [], 0
    for row in rows:
        jid = row["job_id"]
        text, source = repo.get(jid, ""), "repo"
        if not text:
            if apify is None:
                apify = from_apify()
            text, source = apify.get(jid, ""), "apify"
        if not text and li_calls < MAX_LINKEDIN_FETCHES:
            li_calls += 1
            text, source = from_linkedin(jid), "linkedin"
            time.sleep(2)
        text = tidy(text)
        if not text or (len(text) <= len(row["desc"]) and not row["desc"].startswith(MARKERS)):
            missing.append(jid)
            continue
        if not DRY_RUN:
            try:
                notion_update(row["page_id"], text)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{jid}: {exc}")
                continue
        updated.append((jid, source, len(text), row["title"]))

    for jid, source, n, title in updated:
        print(f"OK {jid} [{source}] {n} chars – {title}")
    print(f"Actualizadas: {len(updated)} · sin texto disponible: {len(missing)} {missing}"
          + (" (DRY RUN, nada escrito)" if DRY_RUN else ""))
    if errors:
        print("[error] Fallaron actualizaciones en Notion:\n  " + "\n  ".join(errors))
        return 1  # workflow en rojo -> GitHub avisa por email
    return 0


if __name__ == "__main__":
    sys.exit(main())
