"""Script de diagnóstico para el producto XIAOMI G30."""
from playwright.sync_api import sync_playwright

URL = "https://www.mi.com/co/product/xiaomi-vacuum-cleaner-g30-max/buy/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--window-position=-32000,-32000", "--window-size=1,1"])
    context = browser.new_context(user_agent=UA)
    page = context.new_page()
    from playwright_stealth import Stealth
    Stealth().apply_stealth_sync(context)

    page.goto(URL, timeout=30000, wait_until="domcontentloaded")
    print("URL final:", page.url)

    try:
        page.wait_for_load_state("networkidle", timeout=20000)
        print("networkidle OK")
    except Exception as e:
        print("networkidle timeout:", e)

    # Selector configurado
    el = page.query_selector(".buy-product__installment-price")
    print("\n[Selector configurado] .buy-product__installment-price encontrado:", el is not None)
    if el:
        print("  Texto:", repr(el.inner_text()))

    # Alternativa
    el2 = page.query_selector("strong.notranslate")
    print("[Alternativa] strong.notranslate encontrado:", el2 is not None)
    if el2:
        print("  Texto:", repr(el2.inner_text()))

    # Todos los elementos con 'price' en la clase
    print("\n[Elementos con class *=price]:")
    for e in page.query_selector_all("[class*=price]")[:20]:
        txt = e.inner_text().strip()
        if txt:
            print(f"  class={e.get_attribute('class')!r} -> {txt!r}")

    # Revisar si hay redirección o bloqueo
    print("\n[Título de la página]:", page.title())

    browser.close()
