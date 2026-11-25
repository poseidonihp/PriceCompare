# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Idioma

El proyecto está íntegramente en español (logs, docstrings, mensajes de usuario, commits del README). Mantener español en todo código, mensajes de log y output al usuario. No mezclar con inglés salvo para términos técnicos sin traducción clara.

## Comandos

```powershell
# Setup (una sola vez)
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy config.example.json config.json       # editar con secretos reales
copy products.example.json products.json   # editar con los productos reales

# Pipeline
python -m src.main                          # corrida normal
python -m src.main --dry-run                # consulta precios, no guarda ni notifica (siempre exit 0)
python -m src.main --product lg-c5-55       # solo un producto (por id de products.json)
python -m src.main --cache-html             # lee/escribe HTML en data/html_cache/ para iterar selectores

# Dashboard web (FastAPI + uvicorn) → http://127.0.0.1:8000 (host/puerto en config.json "dashboard")
python scripts/set_dashboard_password.py    # una vez: usuario + hash scrypt + clave de sesión en config.json
python -m src.dashboard                     # sin login configurado sale con código 1

# Dashboard siempre activo en el mini PC: tarea al iniciar Windows (PowerShell como administrador)
powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_task.ps1
Unregister-ScheduledTask -TaskName "priceCompare-dashboard" -Confirm:$false

# Helpers
python scripts/add_product.py <id> "<nombre>" "<url>" [--selector CSS] [--target 800] [--currency USD] [--grupo ID]
python scripts/minimo_producto.py <id> [<id> ...] | --todos [--guardar]

# Tarea diaria de Windows (default 09:00; corre .venv\Scripts\python.exe -m src.main)
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 [-TriggerTime "07:30"]
Start-ScheduledTask -TaskName "priceCompare"
Unregister-ScheduledTask -TaskName "priceCompare" -Confirm:$false
```

Las regresiones se ejecutan con `.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -v`; usan bases temporales y respuestas simuladas, incluyendo Chromium para el dashboard. No hay linter configurado. `python -m src.main` actualiza la base real y puede enviar notificaciones; para validar el flujo completo sin esos efectos, usar una copia temporal de la base y simular los notificadores.

## Arquitectura

Pipeline orquestado por [src/main.py](src/main.py), en dos fases:

- **Fase 1 (paralela, solo red):** `_scrape_all` lanza un `ThreadPoolExecutor` con `scraper.max_workers` y solo hace `fetch_price` en los workers. Los logs de cada worker se capturan en un buffer por hilo (`_CapturaLogsWorkers` + `_SilenciarHilosCapturados`) y se re-emiten agrupados por producto en el orden de `products.json`, para que el log se lea igual que una corrida secuencial.
- **Fase 2 (secuencial, hilo principal):** SQLite y todas las decisiones (`_process_product`, `_process_grupo`). SQLite nunca se toca desde los workers.

Módulos:

1. **[config.py](src/config.py)** — carga `config.json` (Telegram/email/scraper) y `products.json` en dataclasses; define rutas absolutas (`ROOT`, `DB_PATH`, `REPORTS_DIR`, `LOGS_DIR`, `HTML_CACHE_DIR`). `save_products` escribe de forma **atómica** (`.json.tmp` + `os.replace`) porque el dashboard puede escribir mientras corre la tarea programada. `target_price` string con `%` → `target_price_relative`.
2. **[scraper.py](src/scraper.py)** `fetch_price(url, selector, config, cache_dir)` — cascada de transporte: caché HTML (si `cache_dir`) → HTTP con reintentos y backoff exponencial (`_es_reintentable`: timeouts, 5xx y 429 sí; 4xx no) → **Playwright headless** → **Playwright no-headless** (ventana fuera de pantalla, serializada con un lock global para no abrir dos Chromium visibles a la vez). `playwright-stealth` se aplica al contexto si `stealth_enabled`. Lanza `RedirectError` si la URL termina en otro dominio.
3. **[extractors.py](src/extractors.py)** — `extract_offer` devuelve precio, moneda, disponibilidad y método de una misma oferta. Identifica el producto JSON-LD por URL/nombre o entidad única; no toma precios de accesorios. Si el producto define `variant`, filtra las entidades `Product` por `size` y, sin coincidencia, no recurre a metadatos ni texto. Prioriza selector, JSON-LD, metadatos y texto inequívoco. Descarta cuotas, importes ambiguos, valores no positivos y no finitos. Los botones deshabilitados u ocultos y las recomendaciones no prueban disponibilidad. Si la página trae el script de descuentos `window.TDS` (Shopify, Tayrona), `_apply_tds_discount` replica sus reglas y reemplaza el precio base, salvo que venga del selector; método `tds`. `fetch_offer` conserva esos datos y añade el transporte al método; las interfaces anteriores de precio y disponibilidad siguen disponibles.
4. **[storage.py](src/storage.py)** — SQLite con `products` (metadata + `initial_price`/`initial_date` inmutables + `purchased`) y `price_history` (append-only, con `available`). Las columnas nuevas se agregan en `_migrate` con `ALTER TABLE` condicional: **toda columna nueva debe sumarse ahí además de en `SCHEMA`**, o las BD existentes se rompen.
5. **[reporter.py](src/reporter.py)** — tres salidas desde los mismos dataclasses (`PriceDrop`, `PriceDropGrupo`, `RestockAlert`): Markdown persistente, texto plano para Telegram y HTML para email (con sparkline PNG en base64 generado por matplotlib; si matplotlib falla, se omite silenciosamente).
6. **[notifier.py](src/notifier.py)** — Telegram vía `api.telegram.org` y email SMTP con `starttls` + app password. Ambos respetan `enabled` y nunca propagan excepciones (loguean y retornan `False`).
7. **[dashboard.py](src/dashboard.py)** — FastAPI de un solo archivo (HTML/CSS/JS inline en `_DASHBOARD_HTML` y `_LOGIN_HTML`, Chart.js por CDN). CRUD de productos, toggle activo/comprado, histórico compacto, estadísticas y purga de huérfanos. El middleware `_require_login` protege todo salvo `/login`, `/favicon.svg`, `/api/login` y `/api/logout`: `/api/*` sin sesión → 401 (el JS redirige vía `apiFetch`), el resto → 303 a `/login`. Sin `password_hash`/`session_secret` en config.json **falla cerrado** con 503. Lee la sección `dashboard` en cada request, así que un cambio de contraseña aplica sin reiniciar. Toda llamada nueva del JS a la API debe usar `apiFetch`, no `fetch`.
8. **[auth.py](src/auth.py)** — solo stdlib: hash scrypt, cookie `pc_session` firmada con HMAC (usuario + vencimiento) y `LoginRateLimiter` por IP (5 fallos → 15 min). Detrás de cloudflared, uvicorn toma IP y esquema de `X-Forwarded-*` (confía solo en 127.0.0.1), así que la cookie es `Secure` por https y no por http local.

### Reglas de notificación

Hay tres disparadores independientes, con referencias distintas:

- **Bajada individual** (`_build_drop`): compara contra el **último precio válido**. Solo alerta si `current < previous`, incluso si el precio sigue por encima del inicial. El inicial permanece inmutable para calcular la diferencia acumulada. `hit_target` sigue siendo una bandera del reporte.
- **Grupo multi-sitio** (`_process_grupo`): requiere ≥2 sitios de la misma moneda, excluye agotados y exige una bajada respecto al precio anterior. Con padre, mantiene la comparación contra la tienda de referencia; sin padre, exige que baje el mínimo del grupo. Una primera captura o una oferta sin cambios no dispara una alerta de grupo. Los productos incluidos en un grupo reportado se excluyen de las bajadas individuales.
- **Restock** (`_detect_restock`): `available` pasó de `False` a `True`, sin importar el precio.

### Invariantes que el código asume

- **`initial_price` e `initial_date` nunca se sobreescriben.** `register_new_product` se llama una sola vez por `product.id`; `update_product_metadata` (usado por el dashboard) sincroniza nombre/URL/moneda/target y nada más. Cualquier feature que "resetee" el inicial va contra el diseño.
- **Primera corrida de un producto no notifica.** `_process_product` retorna sin drop cuando `storage.get_product` es `None`, aunque el precio se haya extraído bien; sí devuelve el precio para que el producto pueda participar en la comparación de grupo.
- **Un precio que no pasa el sanity check no se guarda.** `_sanity_check` corre **antes** del `append_price` y compara contra `initial_price` usando `sanity_min_ratio`/`sanity_max_ratio`; si falla, el producto queda fuera del historial y de la comparación de grupo de esa corrida. No aplica en la primera captura (no hay referencia).
- **`purchased` pausa la consulta.** `_scrape_all` excluye los productos comprados antes del scraping: no se consultan, conservan su historial y el dashboard los muestra como comprados sin marcarlos como desactualizados. Si se marca comprado durante una corrida en curso, `_process_product` guarda esa captura sin generar drops ni restocks. Al desmarcarlo se reanuda la consulta. `active: false` en `products.json` también lo excluye del pipeline; la diferencia es que `purchased` vive en la BD.
- **Sin stock ⇒ no se compara precio.** Si `available is False`, el precio se guarda pero no se evalúa bajada (evita alertar por precios fantasma de páginas agotadas).
- **Estado de captura:** `scrape_status` registra último intento, último éxito, resultado, motivo seguro y método de extracción. Fallos y rechazos no sustituyen precios aceptados. Para bases antiguas, el dashboard obtiene la última captura desde `price_history`; marca antigüedad superior a 48 horas.
- **Redirección fuera de dominio = error.** `_same_domain` compara netloc del URL original vs. el final; una redirección a homepage/login es fallo de scraping, no "producto movido".
- **Exit code refleja errores, no bajadas.** `run()` retorna `0` cuando hubo novedades (incluso si algún producto falló) y `0` cuando no hubo novedades ni errores; `1` solo si no hubo novedades y sí hubo al menos un error de scraping (o si `--product` no matchea ningún activo). `--dry-run` siempre retorna `0`. No cambiar esto sin revisar el comportamiento del scheduler.
- **`products.json` es la fuente de verdad, la BD solo enriquece.** El dashboard lista desde `products.json` y adjunta datos de la BD si existen; crear un producto desde la UI **no** toca la BD (su `initial_price` se captura en la próxima corrida del pipeline y aparece como "pendiente de captura"). Borrar un producto lo quita del JSON y por defecto conserva el historial, que queda como "huérfano" purgable desde `/api/huerfanos`.

### Cómo debuggear una extracción rota

Abrir la URL en el navegador, inspeccionar el elemento del precio y agregar un `selector` CSS al producto en `products.json` (el selector custom es la primera estrategia y pisa a JSON-LD/meta/regex). Iterar con `python -m src.main --dry-run --product <id> --cache-html` para no re-scrapear en cada intento. Si el sitio es SPA y ni Playwright lo saca, subir `playwright_timeout_seconds` antes de tocar código. [scripts/debug_g30.py](scripts/debug_g30.py) es el patrón para un diagnóstico ad-hoc de un sitio concreto (imprime URL final, título y todos los elementos con `class*=price`).

## Archivos que no deben commitearse

`config.json` (secretos), `products.json` (lista personal de compras), `data/` (incluye `prices.db` y `html_cache/`), `logs/`, `reports/`, `.claude/settings.local.json` — ver [.gitignore](.gitignore). Las plantillas versionadas son [config.example.json](config.example.json) y [products.example.json](products.example.json): **si se agrega un campo nuevo a cualquiera de los dos archivos reales, hay que reflejarlo en su `.example`**. Si se agregan nuevos tipos de credencial, seguir el mismo patrón de plantilla + exclusión en `.gitignore`.

## Código muerto conocido

`extractors.extract_price` (duplica la cascada de `extract_price_and_availability`) y `Storage.get_last_prices` no tienen llamadores. No borrarlos como efecto colateral de otro cambio.
