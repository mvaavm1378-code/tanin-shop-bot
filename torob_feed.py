"""صفحه‌های ساده‌ی HTML برای ترب (لیست محصولات، صفحه‌ی هر محصول، sitemap).

این فایل کاملاً مستقل است و به کدهای ربات دست نمی‌زند.
اگر خطایی رخ دهد فقط همین آدرس‌ها خطا می‌دهند و ربات سالم می‌ماند.
"""
import os
import time
import asyncio
import logging
from html import escape
from aiohttp import web

logger = logging.getLogger("torob_feed")

SITE_URL = os.getenv("SITE_URL", "").strip().rstrip("/")
CACHE_SECONDS = 600  # محصولات هر ۱۰ دقیقه دوباره از دیتابیس خوانده می‌شوند

_cache = {"t": 0.0, "rows": []}


def _e(v):
    return escape("" if v is None else str(v))


def _price(v):
    try:
        return f"{int(float(v)):,}"
    except Exception:
        return _e(v)


def _page(title, body):
    return (
        '<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title></head><body>{body}</body></html>"
    )


def _load_rows(get_conn):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT id, name, category, size, color, price, photo_url, pack_info "
            "FROM products WHERE active=TRUE ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()


async def _products(get_conn):
    if _cache["rows"] and time.time() - _cache["t"] < CACHE_SECONDS:
        return _cache["rows"]
    try:
        rows = await asyncio.to_thread(_load_rows, get_conn)
    except Exception:
        logger.exception("Torob feed: could not read products")
        if _cache["rows"]:  # اگر دیتابیس جواب نداد، آخرین نسخه‌ی سالم را بده
            return _cache["rows"]
        raise
    _cache["rows"], _cache["t"] = rows, time.time()
    return rows


def _base(request):
    host = request.headers.get("X-Forwarded-Host") or request.host
    return "https://" + host


def register_torob_routes(web_app, get_conn):
    async def products_page(request):
        try:
            rows = await _products(get_conn)
        except Exception:
            return web.Response(status=503, text="temporarily unavailable")
        items = "".join(
            f'<li><a href="/product/{r["id"]}">{_e(r["name"])}</a> — {_price(r["price"])} تومان</li>'
            for r in rows
        )
        body = f"<h1>محصولات تولیدی تنین ایران</h1><ul>{items}</ul>"
        return web.Response(
            text=_page("محصولات | تولیدی تنین ایران", body), content_type="text/html"
        )

    async def product_page(request):
        try:
            pid = int(request.match_info["pid"])
            rows = await _products(get_conn)
        except ValueError:
            return web.Response(status=404, text="not found")
        except Exception:
            return web.Response(status=503, text="temporarily unavailable")
        r = next((x for x in rows if x["id"] == pid), None)
        if not r:
            return web.Response(status=404, text="not found")
        photo = (r.get("photo_url") or "").strip()
        img = (
            f'<img src="{_e(photo)}" alt="{_e(r["name"])}" width="400">'
            if photo.startswith("http")
            else ""
        )
        details = "".join(
            f"<li>{label}: {_e(r.get(key))}</li>"
            for label, key in [
                ("دسته‌بندی", "category"),
                ("سایز", "size"),
                ("رنگ", "color"),
                ("بسته‌بندی", "pack_info"),
            ]
            if r.get(key)
        )
        link = (
            f'<p><a href="{_e(SITE_URL)}/#/product/{r["id"]}">مشاهده و سفارش در فروشگاه</a></p>'
            if SITE_URL
            else ""
        )
        body = (
            f'<h1>{_e(r["name"])}</h1>{img}'
            f'<p>قیمت: {_price(r["price"])} تومان</p><ul>{details}</ul>{link}'
        )
        return web.Response(
            text=_page(f'{r["name"]} | تولیدی تنین ایران', body),
            content_type="text/html",
        )

    async def sitemap(request):
        try:
            rows = await _products(get_conn)
        except Exception:
            return web.Response(status=503, text="temporarily unavailable")
        base = _base(request)
        urls = [f"<url><loc>{base}/products</loc></url>"] + [
            f'<url><loc>{base}/product/{r["id"]}</loc></url>' for r in rows
        ]
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(urls)
            + "</urlset>"
        )
        return web.Response(text=xml, content_type="application/xml")

    web_app.router.add_get("/products", products_page)
    web_app.router.add_get("/product/{pid}", product_page)
    web_app.router.add_get("/sitemap.xml", sitemap)
      
