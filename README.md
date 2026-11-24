# priceCompare

Comparador de precios automático. Corre diariamente, revisa las URL de tus productos, guarda el historial y te avisa **solo cuando el precio baja**. Soporta productos de cualquier tienda (TVs, cartones de leche, lo que sea).

## Cómo funciona

- Lees tu lista de productos desde `products.json`.
- El script guarda el **precio inicial** (la primera vez que vio el producto) y todo el historial en SQLite.
- Cada día compara el precio actual contra el último conocido.
- Si bajó → genera reporte, manda Telegram y email.
- Si subió o está igual → no hace nada (sin spam).
- Si un producto vuelve a tener stock (estaba agotado) → notifica aunque el precio no haya cambiado.

Por ejemplo: `100 → 90` notifica, repetir `90` no notifica, `90 → 100` no notifica y `100 → 95` vuelve a notificar. El precio inicial se conserva para calcular la diferencia acumulada; la alerta compara contra la última captura válida.

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
| `variant` | Opcional. Talla a seguir (`"240 x 220 cm"`) cuando la URL publica en JSON-LD un `Product` por talla; se compara con su campo `size`. |

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
.venv\Scripts\python.exe scripts\set_dashboard_password.py   # una sola vez: usuario y contraseña
.venv\Scripts\python.exe -m src.dashboard
```

Luego abrí `http://127.0.0.1:8000` e iniciá sesión. Sin contraseña configurada el dashboard no arranca. Para dejarlo siempre activo y accesible desde afuera, ver [Desplegar el dashboard en el mini PC](#desplegar-el-dashboard-en-el-mini-pc). Muestra todos los productos con gráfico de evolución, badges de estado (bajada, objetivo alcanzado, sin stock, mínimo histórico) y filtros. Requiere `fastapi` y `uvicorn` (incluidos en `requirements.txt`).

Cada tarjeta muestra la última captura exitosa, el último intento y su resultado. Un precio sin captura exitosa durante más de 48 horas aparece como **desactualizado**. Los fallos de consulta y los precios rechazados conservan el último precio aceptado y muestran el motivo. La hora del encabezado corresponde a una captura real.

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

Si el equipo estaba apagado a la hora programada, la tarea corre apenas enciende.

### Desplegar el dashboard en el mini PC

El dashboard queda siempre activo: arranca con Windows aunque nadie inicie sesión. Se publica en un subdominio de Cloudflare protegido en dos capas: Cloudflare Access y el login propio del dashboard. Los ejemplos asumen el proyecto en `C:\priceCompare`.

**1. Login del dashboard.** Se configura una vez. Para cambiar la contraseña se repite el comando, y eso cierra las sesiones abiertas.

```powershell
cd C:\priceCompare
.venv\Scripts\python.exe scripts\set_dashboard_password.py
```

En la sección `dashboard` de `config.json` se guardan solo el hash scrypt de la contraseña y una clave de sesión aleatoria. Ahí también se ajustan:

- `host` y `port`: por defecto `127.0.0.1:8000`. Cambiá el puerto si otro proyecto lo usa.
- `session_days`: por defecto `30`.

Tras 5 intentos fallidos seguidos, la IP queda bloqueada 15 minutos.

**2. Tarea que arranca con el equipo.** Primero cerrá el dashboard si lo tenías abierto a mano, para liberar el puerto. Después, en PowerShell **como administrador**:

```powershell
cd C:\priceCompare
powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_task.ps1
```

Esto crea la tarea `priceCompare-dashboard`, que:

- arranca con Windows usando tu usuario, sin necesidad de iniciar sesión;
- se revisa cada 5 minutos y relanza el proceso si se cayó;
- no tiene límite de duración;
- escribe los logs en `logs\dashboard.log`.

```powershell
Get-ScheduledTask -TaskName "priceCompare-dashboard"     # Ver estado
Stop-ScheduledTask -TaskName "priceCompare-dashboard"    # Detener (la revisión la relanza en ≤5 min)
Start-ScheduledTask -TaskName "priceCompare-dashboard"   # Iniciar
Disable-ScheduledTask -TaskName "priceCompare-dashboard" # Desactivar del todo
Unregister-ScheduledTask -TaskName "priceCompare-dashboard" -Confirm:$false  # Eliminar
```

Después de actualizar el código, reiniciá la tarea con `Stop-ScheduledTask` y luego `Start-ScheduledTask`.

**3. Publicarlo en el túnel de Cloudflare**, en un subdominio como `precios.tudominio.com`:

- **Túnel administrado desde el panel** (instalado con `cloudflared service install <token>`): andá a Zero Trust → Networks → Tunnels → tu túnel → *Public Hostname*. En el panel nuevo esa opción se llama *Published application routes*. Agregá una ruta con subdominio `precios`, tu dominio, tipo `HTTP` y URL `localhost:8000`. Cloudflare crea el registro DNS solo.
- **Túnel local con `config.yml`**: en el `config.yml` que usa tu servicio de `cloudflared`, agregá esta regla antes de la regla final `- service: http_status:404`:

  ```yaml
  - hostname: precios.tudominio.com
    service: http://localhost:8000
  ```

  Después corré `cloudflared tunnel route dns <nombre-del-tunel> precios.tudominio.com` y `Restart-Service cloudflared`.

El dashboard solo escucha en `127.0.0.1`, así que desde la red solo se llega a él a través del túnel. Detrás de Cloudflare, la cookie de sesión se marca `Secure` y el límite de intentos usa la IP real del visitante.

**4. Cloudflare Access (primera capa).** Andá a Zero Trust → Access → Applications → *Add an application* → *Self-hosted*:

- Dominio `precios.tudominio.com` y *Session Duration* `1 month`.
- Política con acción `Allow`, e *Include* → *Emails* con tu correo.
- Método de login: *One-time PIN* (código al correo) o Google.

Así nadie más llega siquiera al mini PC, y el login del dashboard queda como segunda capa. Si la sesión de Access vence con el dashboard abierto, las acciones fallan: recargá la página para volver a pasar por Access.

**5. Comprobar desde afuera.** Desde el celular con datos móviles, abrí `https://precios.tudominio.com`. Debe pedir Access y después el login del dashboard. Editá un producto y usá *Cerrar sesión*: la siguiente carga tiene que volver al login.

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
├── logs/dashboard.log         # Log rotado del dashboard
├── src/                       # Código
│   ├── dashboard.py           # Dashboard web (python -m src.dashboard)
│   └── auth.py                # Login del dashboard: hash, cookie firmada, límite de intentos
└── scripts/                   # Helpers (add_product, install_task, install_dashboard_task,
                               #          set_dashboard_password, minimo_producto)
```

`data/`, `reports/` y `logs/` se crean solos en la primera corrida y no se versionan.

## Cómo extrae el precio

Cascada de estrategias, en orden:

1. **CSS selector custom** (si lo definiste en `products.json`).
2. **JSON-LD** — identifica el producto principal y selecciona una oferta con su precio, moneda y disponibilidad; prioriza ofertas disponibles de la moneda configurada.
3. **Meta tags** — `og:price:amount`, `product:price:amount`, `itemprop="price"`.
4. **Regex** — busca un importe inequívoco etiquetado como precio en el contenido principal, descartando cuotas y recomendaciones.

Si HTTP simple no funciona (sitio con JavaScript), reintenta con **Playwright headless** y repite las 4 estrategias sobre el HTML renderizado.

Algunas tiendas Shopify (por ejemplo Tayrona) publican el precio sin descuento y muestran la rebaja con el script `window.TDS` del tema. Si la página trae ese script con una regla vigente, el precio obtenido por JSON-LD, meta tags o regex se reemplaza por el precio rebajado de la variante seleccionada, y el método queda como `tds`. Un precio tomado del CSS selector custom no se modifica.

Los precios ambiguos, no positivos o no finitos se descartan, incluso en la primera captura. Un selector CSS inválido se rechaza al guardar; si ya existe en el catálogo, solo falla ese producto. Las ofertas agotadas se guardan en el historial, pero no participan en la selección de la mejor oferta de un grupo. Los grupos tampoco repiten alertas por precios sin cambios.

## Notas

- El `initial_price` **nunca se sobreescribe**. Es tu baseline histórico para ver cuánto ahorraste respecto al primer día.
- Si un sitio cambia su HTML y deja de funcionar la extracción, vas a ver errores en `logs/run.log`. Solución típica: abrir la página, inspeccionar el precio, y poner un CSS selector en el campo `selector` de ese producto. Podés usar `--cache-html` para iterar los selectores sin re-scrapear.
- Si el precio extraído es menor al 25% o mayor al 400% del precio inicial, se descarta automáticamente como extracción rota (configurable con `sanity_min_ratio` y `sanity_max_ratio` en `config.json`).
- SQLite (`data/prices.db`) se puede abrir con [DB Browser for SQLite](https://sqlitebrowser.org/) para ver el historial completo.

## Verificación

```powershell
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -v
```

Las pruebas usan bases temporales, HTML de ejemplo y respuestas simuladas. No consultan tiendas ni envían notificaciones. Las pruebas del dashboard requieren Chromium instalado mediante `playwright install chromium`. La tabla `scrape_status` se crea automáticamente al abrir una base existente, sin modificar sus precios iniciales ni su historial.

## Licencia

[MIT](LICENSE).
