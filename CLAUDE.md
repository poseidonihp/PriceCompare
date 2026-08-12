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

# Dashboard web (FastAPI + uvicorn) → http://127.0.0.1:8000
python -m src.dashboard

# Helpers
python scripts/add_product.py <id> "<nombre>" "<url>" [--selector CSS] [--target 800] [--currency USD] [--grupo ID]
python scripts/minimo_producto.py <id> [<id> ...] | --todos [--guardar]

# Tarea diaria de Windows (default 09:00; corre .venv\Scripts\python.exe -m src.main)
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 [-TriggerTime "07:30"]
Start-ScheduledTask -TaskName "priceCompare"
Unregister-ScheduledTask -TaskName "priceCompare" -Confirm:$false
```

No hay test suite ni linter configurado. La validación end-to-end es `python -m src.main`: crea/actualiza `data/prices.db`, escribe `logs/run.log` (rotado, 2 MB × 5) y, si hubo novedades, genera `reports/YYYY-MM-DD.md`. Para probar cambios sin ensuciar la BD ni disparar notificaciones: `--dry-run` (opcionalmente con `--product` y `--cache-html`).

## Arquitectura

Pipeline orquestado por [src/main.py](src/main.py), en dos fases:

- **Fase 1 (paralela, solo red):** `_scrape_all` lanza un `ThreadPoolExecutor` con `scraper.max_workers` y solo hace `fetch_price` en los workers. Los logs de cada worker se capturan en un buffer por hilo (`_CapturaLogsWorkers` + `_SilenciarHilosCapturados`) y se re-emiten agrupados por producto en el orden de `products.json`, para que el log se lea igual que una corrida secuencial.
- **Fase 2 (secuencial, hilo principal):** SQLite y todas las decisiones (`_process_product`, `_process_grupo`). SQLite nunca se toca desde los workers.

Módulos:

1. **[config.py](src/config.py)** — carga `config.json` (Telegram/email/scraper) y `products.json` en dataclasses; define rutas absolutas (`ROOT`, `DB_PATH`, `REPORTS_DIR`, `LOGS_DIR`, `HTML_CACHE_DIR`). `save_products` escribe de forma **atómica** (`.json.tmp` + `os.replace`) porque el dashboard puede escribir mientras corre la tarea programada. `target_price` string con `%` → `target_price_relative`.
2. **[scraper.py](src/scraper.py)** `fetch_price(url, selector, config, cache_dir)` — cascada de transporte: caché HTML (si `cache_dir`) → HTTP con reintentos y backoff exponencial (`_es_reintentable`: timeouts, 5xx y 429 sí; 4xx no) → **Playwright headless** → **Playwright no-headless** (ventana fuera de pantalla, serializada con un lock global para no abrir dos Chromium visibles a la vez). `playwright-stealth` se aplica al contexto si `stealth_enabled`. Lanza `RedirectError` si la URL termina en otro dominio.
3. **[extractors.py](src/extractors.py)** — `extract_price_and_availability` corre la cascada de precio sobre un solo parse: (a) selector CSS custom, (b) JSON-LD (`price`/`lowPrice`/`highPrice`, recursivo, toma el mínimo), (c) meta tags, (d) regex de números cerca de "precio"/"price"/"total"/"valor". En paralelo detecta disponibilidad (JSON-LD `availability` → meta → botones/inputs → texto de página), donde una señal de compra activa gana sobre cualquier "agotado" del footer o de productos relacionados. `_normalize_price` sobre texto con varios números normaliza cada token y devuelve **el menor** (evita el bug de concatenar dígitos en bloques "antes/ahora").
4. **[storage.py](src/storage.py)** — SQLite con `products` (metadata + `initial_price`/`initial_date` inmutables + `purchased`) y `price_history` (append-only, con `available`). Las columnas nuevas se agregan en `_migrate` con `ALTER TABLE` condicional: **toda columna nueva debe sumarse ahí además de en `SCHEMA`**, o las BD existentes se rompen.
5. **[reporter.py](src/reporter.py)** — tres salidas desde los mismos dataclasses (`PriceDrop`, `PriceDropGrupo`, `RestockAlert`): Markdown persistente, texto plano para Telegram y HTML para email (con sparkline PNG en base64 generado por matplotlib; si matplotlib falla, se omite silenciosamente).
6. **[notifier.py](src/notifier.py)** — Telegram vía `api.telegram.org` y email SMTP con `starttls` + app password. Ambos respetan `enabled` y nunca propagan excepciones (loguean y retornan `False`).
7. **[dashboard.py](src/dashboard.py)** — FastAPI de un solo archivo (HTML/CSS/JS inline en `_DASHBOARD_HTML`, Chart.js por CDN). CRUD de productos, toggle activo/comprado, histórico compacto, estadísticas y purga de huérfanos.

### Reglas de notificación

Hay tres disparadores independientes, con referencias distintas:

- **Bajada individual** (`_build_drop`): la referencia es **siempre el `initial_price`**. Solo alerta si `current < initial` estrictamente. `hit_target` es solo una bandera de "objetivo alcanzado" en el reporte, nunca un disparador propio.
- **Grupo multi-sitio** (`_process_grupo`): aquí sí se compara contra el **precio anterior**, no contra el inicial. Requiere ≥2 sitios con precio hoy. Si existe un producto cuyo `id` es igual al `grupo` (el "**padre**", la tienda de referencia), alerta cuando algún sitio está más barato que el padre o cuando el padre bajó vs. su precio anterior. Sin padre, alerta cuando aparece un sitio nuevo o cuando el mínimo del grupo baja. Un producto que ya se reportó dentro de un grupo se **excluye** de la sección de bajadas individuales.
- **Restock** (`_detect_restock`): `available` pasó de `False` a `True`, sin importar el precio.

### Invariantes que el código asume

- **`initial_price` e `initial_date` nunca se sobreescriben.** `register_new_product` se llama una sola vez por `product.id`; `update_product_metadata` (usado por el dashboard) sincroniza nombre/URL/moneda/target y nada más. Cualquier feature que "resetee" el inicial va contra el diseño.
- **Primera corrida de un producto no notifica.** `_process_product` retorna sin drop cuando `storage.get_product` es `None`, aunque el precio se haya extraído bien; sí devuelve el precio para que el producto pueda participar en la comparación de grupo.
- **Un precio que no pasa el sanity check no se guarda.** `_sanity_check` corre **antes** del `append_price` y compara contra `initial_price` usando `sanity_min_ratio`/`sanity_max_ratio`; si falla, el producto queda fuera del historial y de la comparación de grupo de esa corrida. No aplica en la primera captura (no hay referencia).
- **`purchased` silencia, no pausa.** Un producto comprado sigue guardando historial, pero no genera drops ni restocks. `active: false` en `products.json` sí lo excluye del pipeline por completo.
- **Sin stock ⇒ no se compara precio.** Si `available is False`, el precio se guarda pero no se evalúa bajada (evita alertar por precios fantasma de páginas agotadas).
- **Redirección fuera de dominio = error.** `_same_domain` compara netloc del URL original vs. el final; una redirección a homepage/login es fallo de scraping, no "producto movido".
- **Exit code refleja errores, no bajadas.** `run()` retorna `0` cuando hubo novedades (incluso si algún producto falló) y `0` cuando no hubo novedades ni errores; `1` solo si no hubo novedades y sí hubo al menos un error de scraping (o si `--product` no matchea ningún activo). `--dry-run` siempre retorna `0`. No cambiar esto sin revisar el comportamiento del scheduler.
- **`products.json` es la fuente de verdad, la BD solo enriquece.** El dashboard lista desde `products.json` y adjunta datos de la BD si existen; crear un producto desde la UI **no** toca la BD (su `initial_price` se captura en la próxima corrida del pipeline y aparece como "pendiente de captura"). Borrar un producto lo quita del JSON y por defecto conserva el historial, que queda como "huérfano" purgable desde `/api/huerfanos`.

### Cómo debuggear una extracción rota

Abrir la URL en el navegador, inspeccionar el elemento del precio y agregar un `selector` CSS al producto en `products.json` (el selector custom es la primera estrategia y pisa a JSON-LD/meta/regex). Iterar con `python -m src.main --dry-run --product <id> --cache-html` para no re-scrapear en cada intento. Si el sitio es SPA y ni Playwright lo saca, subir `playwright_timeout_seconds` antes de tocar código. [scripts/debug_g30.py](scripts/debug_g30.py) es el patrón para un diagnóstico ad-hoc de un sitio concreto (imprime URL final, título y todos los elementos con `class*=price`).

## Archivos que no deben commitearse

`config.json` (secretos), `products.json` (lista personal de compras), `data/` (incluye `prices.db` y `html_cache/`), `logs/`, `reports/`, `.claude/settings.local.json` — ver [.gitignore](.gitignore). Las plantillas versionadas son [config.example.json](config.example.json) y [products.example.json](products.example.json): **si se agrega un campo nuevo a cualquiera de los dos archivos reales, hay que reflejarlo en su `.example`**. Si se agregan nuevos tipos de credencial, seguir el mismo patrón de plantilla + exclusión en `.gitignore`.

## Código muerto conocido

`extractors.extract_price` (duplica la cascada de `extract_price_and_availability`) y `Storage.get_last_prices` no tienen llamadores. No borrarlos como efecto colateral de otro cambio.
