#!/usr/bin/env python3
import os
from pathlib import Path
from typing import Any, Dict, List

import yaml
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from datetime import datetime, timezone, date
import asyncio
import httpx
from fastapi.responses import FileResponse

APP_DIR = Path(__file__).parent.resolve()
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
CONFIG_PATH = APP_DIR / "config.yaml"

app = FastAPI(title="Windsurf Dashboard (Southern Germany)")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DWD_IMAGES = {
    "main": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/bwk_bodendruck_na_ana.png",
    "v036": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_v36.png",
    "v048": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_048.png",
    "v060": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_060.png",
    "v084": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_084.png",
    "v108": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_108.png",
}
CACHE_DIR = APP_DIR / "cache" / "dwd"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_dwd_locks = {name: asyncio.Lock() for name in DWD_IMAGES.keys()}

def _is_fresh(path: Path, max_age_hours: int = 24) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
    return age < max_age_hours * 3600

async def _fetch_and_cache(url: str, dst: Path) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        r.raise_for_status()
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(dst)


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Normalize
    cfg.setdefault("views", [])
    cfg.setdefault("rotation", {"enabled": False, "interval_seconds": 30})
    cfg.setdefault("spots", {})
    return cfg

def windy_iframe_src(lat: float, lon: float, opts: Dict[str, Any]) -> str:
    # Windy embed parameters reference: https://embed.windy.com/
    zoom = opts.get("zoom", 10)
    overlay = opts.get("overlay", "wind")
    marker = "true" if opts.get("marker", True) else "false"
    detail = "true" if opts.get("detail", True) else "false"
    units_wind = opts.get("units_wind", "kmh")  # kmh, ms, kt, mph, bft
    # Use embed2.html (new). location=coordinates shows lat/lon in URL
    return (
        "https://embed.windy.com/embed2.html"
        f"?lat={lat:.5f}&lon={lon:.5f}"
        f"&zoom={zoom}"
        "&level=surface"
        f"&overlay={overlay}"
        f"&marker={marker}"
        f"&location=coordinates"
        f"&detail={detail}"
        f"&detailLat={lat:.5f}&detailLon={lon:.5f}"
        f"&metricWind={units_wind}"
        f"&metricTemp=C"
    )

def windfinder_iframe_src(widget_src: str) -> str:
    # Windfinder provides a full <iframe>. We only store the src URL in config.
    # Rules: up to 3 widgets per page, no auto-refresh. We rotate views without reloading the page.
    # https://www.windfinder.com/apps/homepageweather + https://www.windfinder.com/help/other/widgets.htm
    return widget_src


@app.get("/dwd/{name}.png")
async def dwd_image(name: str):
    if name not in DWD_IMAGES:
        return HTMLResponse("Unknown DWD image", status_code=404)

    url = DWD_IMAGES[name]
    out = CACHE_DIR / f"{name}.png"

    # fetch once per day
    if not _is_fresh(out, max_age_hours=24):
        lock = _dwd_locks[name]
        async with lock:
            if not _is_fresh(out, max_age_hours=24):
                await _fetch_and_cache(url, out)

    # mild browser caching; daily cache-buster in template handles the rest
    headers = {"Cache-Control": "public, max-age=3600"}
    return FileResponse(out, media_type="image/png", headers=headers)


@app.get("/", response_class=HTMLResponse)
def root():
    # Redirect to first view for convenience
    cfg = load_config()
    if cfg["views"]:
        return RedirectResponse(url=f"/view/{cfg['views'][0]['name']}")
    return HTMLResponse("<h1>No views configured. Please edit config.yaml</h1>")

@app.get("/view/{view_name}", response_class=HTMLResponse)
def view_page(request: Request, view_name: str):
    cfg = load_config()
    views: List[Dict[str, Any]] = cfg["views"]
    view_names = [v["name"] for v in views]
    if view_name not in view_names:
        return HTMLResponse(f"<h1>Unknown view: {view_name}</h1>", status_code=404)

    view = next(v for v in views if v["name"] == view_name)

    # Build cards (DWD + spots)
    show_dwd = bool(view.get("show_dwd", False))

    spot_cards = []
    for spot_name in view.get("spots", []):
        spec = cfg["spots"].get(spot_name)
        if not spec:
            spot_cards.append({"title": f"{spot_name} (missing in config)", "iframe_src": None})
            continue

        provider = spec.get("provider", "windy").lower()
        title = spot_name
        iframe_src = None

        if provider == "windy":
            lat = spec.get("lat")
            lon = spec.get("lon")
            windy_opts = spec.get("windy", {})
            if lat is None or lon is None:
                spot_cards.append({"title": f"{title} (missing lat/lon)", "iframe_src": None})
            else:
                iframe_src = windy_iframe_src(lat, lon, windy_opts)

        elif provider == "windfinder":
            wf = spec.get("windfinder", {})
            src = wf.get("widget_src")
            if src:
                iframe_src = windfinder_iframe_src(src)
            else:
                spot_cards.append({"title": f"{title} (missing windfinder.widget_src)", "iframe_src": None})

        else:
            spot_cards.append({"title": f"{title} (unknown provider: {provider})", "iframe_src": None})

        if iframe_src:
            spot_cards.append({"title": title, "iframe_src": iframe_src})

    rotation = cfg.get("rotation", {})
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_view": view_name,
            "views": view_names,
            "show_dwd": show_dwd,
            "spot_cards": spot_cards,
            "rotation_enabled": bool(rotation.get("enabled", False)),
            "rotation_interval": int(rotation.get("interval_seconds", 30)),
            "dwd_version": date.today().strftime("%y%m%d"),
        },
    )
