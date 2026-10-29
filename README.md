# LinkedIn fallback (JobSpy + GitHub Actions)

Respaldo del Daily Job Pipeline: cuando la cuota mensual de Apify se agota, este workflow
busca los avisos de LinkedIn con [JobSpy](https://github.com/speedyapply/JobSpy) y los publica
en `data/` con el mismo formato que el dataset de Apify.

## Cómo funciona
- Corre todos los días a las 11:35 UTC (antes del run de Apify y de la rutina de Claude).
- Consulta la cuota de Apify. **Si queda cuota → no scrapea** (`data/meta.json` queda con `"status": "skipped"`).
- Si quedan menos de USD 0.25 (o no se puede consultar Apify) → scrapea LinkedIn (`"status": "scraped"`)
  y escribe `data/page_0.json`, `data/page_1.json`, … (10 avisos por página, descripción hasta 1.900 caracteres).
- Si JobSpy falla → `"status": "error"` y el workflow queda en rojo (GitHub te avisa por email).
- La rutina de Claude lee `meta.json` y las páginas desde `raw.githubusercontent.com`.

## Instalación (una sola vez, ~5 minutos)
1. En GitHub crea un repositorio **público** llamado `linkedin-fallback` (público para que la rutina pueda leerlo;
   solo contiene avisos públicos de empleo, ningún dato tuyo).
2. Sube estos archivos respetando las carpetas (`.github/workflows/…`, `scraper/…`, `README.md`).
   Si usas la web de GitHub: *Add file → Upload files* y arrastra la carpeta completa.
3. *Settings → Secrets and variables → Actions → New repository secret*:
   nombre `APIFY_TOKEN`, valor = tu token de Apify.
4. *Actions* → habilita los workflows si GitHub lo pide → abre **LinkedIn fallback (JobSpy)** →
   *Run workflow* con `force` marcado para probarlo. Al terminar debe aparecer `data/meta.json` con `"status": "scraped"`.

## Ajustes opcionales (variables de entorno en el workflow)
`SEARCH_TERM`, `LOCATION` (por defecto `Chile`), `HOURS_OLD` (26), `RESULTS_WANTED` (60), `MIN_REMAINING_USD` (0.25).

## Notas
- LinkedIn puede limitar consultas desde servidores de GitHub (HTTP 429/bloqueos). Si pasa seguido, se puede bajar
  `RESULTS_WANTED` o agregar proxies (parámetro `proxies` de JobSpy).
- El scraping de LinkedIn va contra sus términos de uso; no usa tu cuenta, pero úsalo con moderación.
- GitHub desactiva los workflows programados tras 60 días sin actividad; el commit diario de `meta.json` lo mantiene activo.
