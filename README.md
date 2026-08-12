# priceCompare

Comparador de precios automático. Corre diariamente, revisa las URL de tus productos, guarda el historial y te avisa **solo cuando el precio baja**. Soporta productos de cualquier tienda (TVs, cartones de leche, lo que sea).

## Cómo funciona

- Lees tu lista de productos desde `products.json`.
- El script guarda el **precio inicial** (la primera vez que vio el producto) y todo el historial en SQLite.
- Cada día compara el precio actual contra el último conocido.
- Si bajó → genera reporte, manda Telegram y email.
- Si subió o está igual → no hace nada (sin spam).
- Si un producto vuelve a tener stock (estaba agotado) → notifica aunque el precio no haya cambiado.

## Setup (una sola vez)

```powershell
# 1. Crear entorno virtual e instalar dependencias
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 2. Configurar secretos
copy config.example.json config.json
# Editá config.json con los valores reales (ver abajo)

# 3. Crear tu lista de productos
copy products.example.json products.json
# Editá products.json con tus productos reales (ver "Agregar un producto")
```

`config.json` y `products.json` están en `.gitignore`: son tuyos y no se suben al repo. Los archivos `*.example.json` son las plantillas versionadas.

### Telegram (obligatorio si querés notificaciones)

1. Abrí Telegram y buscá **@BotFather**.
2. Mandale `/newbot`, seguí las instrucciones, copiá el `bot_token`.
3. Mandale cualquier mensaje a tu bot.
4. Visitá `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` — ahí ves tu `chat_id` (en `message.chat.id`).
5. Pegalos en `config.json`.

### Email (opcional, respaldo)

1. Activá verificación en 2 pasos en Gmail.
2. Generá un [App Password](https://myaccount.google.com/apppasswords).
3. Pegalo en `config.json` bajo `email.app_password`.

## Uso

### Agregar un producto

Opción A — editar `products.json` a mano.

Opción B — con el helper:
```powershell
python scripts/add_product.py tv-samsung-55 "TV Samsung 55 4K" "https://tienda.com/tv" --target 800 --currency USD
```

Campos de cada producto:

| Campo | Descripción |
|---|---|
| `id` | Identificador único (se usa como clave en la base de datos). |
| `name` | Nombre legible. |
| `url` | URL del producto. |
| `selector` | CSS selector opcional si los extractores automáticos fallan. |
| `target_price` | Precio objetivo. Puede ser absoluto (`899900`) o relativo (`"-20%"` = 20% por debajo del precio inicial). |
| `currency` | Moneda (USD, COP, etc.) solo para formato de display. |
| `active` | `true` para que lo procese, `false` para pausarlo. |
| `grupo` | Agrupa el mismo producto en varios sitios para comparación multi-sitio. |

### Correr manualmente

```powershell
# Corrida normal
python -m src.main

# Solo consultar, sin guardar en BD ni enviar notificaciones
python -m src.main --dry-run

# Procesar un solo producto (por su id)
python -m src.main --product lg-c5-55

# Usar caché HTML (lee el HTML guardado del último scrape en vez de ir a internet)
python -m src.main --cache-html

# Combinaciones
python -m src.main --dry-run --product lg-c5-55
python -m src.main --dry-run --cache-html
```

La **primera corrida** de un producto solo guarda el precio inicial, no notifica nada.

### Dashboard web

Ver historial de precios, estado de stock y comparativas en el navegador:

```powershell
python -m src.dashboard
```

Luego abrí `http://127.0.0.1:8000`. Muestra todos los productos con gráfico de evolución, badges de estado (bajada, objetivo alcanzado, sin stock, mínimo histórico) y filtros. Requiere `fastapi` y `uvicorn` (incluidos en `requirements.txt`).

### Consultar mínimo histórico de un producto

```powershell
python scripts/minimo_producto.py sony-wh1000xm6
```

Salida de ejemplo:
```
───────────────────────────────────────────────────────
Producto         : Sony WH-1000XM6 (sony-wh1000xm6)
Precio inicial   : COP 1,649,934
Precio actual    : COP 1,599,900
MÍNIMO HISTÓRICO : COP 1,549,900  (2026-03-12)
Ahorro vs inicial: COP 100,034 (6.1%)
───────────────────────────────────────────────────────
```

Opciones adicionales:
```powershell
# Varios productos a la vez
python scripts/minimo_producto.py sony-wh1000xm6 samsung-s90f-55

# Todos los productos registrados
python scripts/minimo_producto.py --todos

# Guardar el resultado en reports/
python scripts/minimo_producto.py sony-wh1000xm6 --guardar
```

El mínimo histórico se calcula sobre todo el historial guardado en `data/prices.db`. No requiere configuración adicional.

### Instalar la tarea diaria

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

Por defecto corre todos los días a las 09:00. Para cambiarlo:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -TriggerTime "07:30"
```

Comandos útiles:
```powershell
Get-ScheduledTask -TaskName "priceCompare"      # Ver estado
Start-ScheduledTask -TaskName "priceCompare"    # Forzar corrida ahora
Unregister-ScheduledTask -TaskName "priceCompare" -Confirm:$false  # Eliminar
```

## Estructura de archivos

```
priceCompare/
├── products.example.json      # Plantilla de la lista de productos (versionada)
├── products.json              # Tu lista editable (local, ignorada por git)
├── config.example.json        # Plantilla de configuración (versionada)
├── config.json                # Secretos (local, ignorada por git)
├── data/prices.db             # SQLite con historial + precios iniciales
├── data/html_cache/           # Caché HTML opcional (--cache-html)
├── reports/YYYY-MM-DD.md      # Reporte solo si hubo bajadas/novedades
├── logs/run.log               # Log rotado de cada corrida
├── src/                       # Código
│   └── dashboard.py           # Dashboard web (python -m src.dashboard)
└── scripts/                   # Helpers (add_product, install_task, minimo_producto)
```

`data/`, `reports/` y `logs/` se crean solos en la primera corrida y no se versionan.

## Cómo extrae el precio

Cascada de estrategias, en orden:

1. **CSS selector custom** (si lo definiste en `products.json`).
2. **JSON-LD** — busca `<script type="application/ld+json">` con `Product.offers.price`.
3. **Meta tags** — `og:price:amount`, `product:price:amount`, `itemprop="price"`.
4. **Regex** — busca números tipo precio cerca de palabras como "precio", "price", "total".

Si HTTP simple no funciona (sitio con JavaScript), reintenta con **Playwright headless** y repite las 4 estrategias sobre el HTML renderizado.

## Notas

- El `initial_price` **nunca se sobreescribe**. Es tu baseline histórico para ver cuánto ahorraste respecto al primer día.
- Si un sitio cambia su HTML y deja de funcionar la extracción, vas a ver errores en `logs/run.log`. Solución típica: abrir la página, inspeccionar el precio, y poner un CSS selector en el campo `selector` de ese producto. Podés usar `--cache-html` para iterar los selectores sin re-scrapear.
- Si el precio extraído es menor al 25% o mayor al 400% del precio inicial, se descarta automáticamente como extracción rota (configurable con `sanity_min_ratio` y `sanity_max_ratio` en `config.json`).
- SQLite (`data/prices.db`) se puede abrir con [DB Browser for SQLite](https://sqlitebrowser.org/) para ver el historial completo.

## Licencia

[MIT](LICENSE).
