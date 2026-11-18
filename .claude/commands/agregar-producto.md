Agrega un nuevo producto al comparador de precios ejecutando `scripts/add_product.py`.

Argumentos: `<id> "<nombre>" "<url>" [--selector CSS] [--target PRECIO] [--currency MONEDA] [--grupo ID_GRUPO] [--variant "TALLA"]`

`--variant` elige la talla cuando la URL publica varias en JSON-LD (ej. `--variant "240 x 220 cm"`).

Ejemplos:
- `/agregar-producto tv-lg-55 "LG 55 OLED" "https://tienda.com/tv" --target 4000000 --currency COP`
- `/agregar-producto sony-xm5 "Sony WH-XM5" "https://sony.com/xm5" --currency COP --grupo sony-xm5`

Instrucciones:
1. Parseá los argumentos `$ARGUMENTS` y ejecutá el siguiente comando en PowerShell desde la raíz del proyecto:
   ```
   .venv\Scripts\python.exe scripts/add_product.py $ARGUMENTS
   ```
2. Mostrá la salida del comando al usuario.
3. Si el comando falla, explicá el error en español.
4. Si el producto se agregó correctamente, confirmá con el ID y nombre del producto agregado.
