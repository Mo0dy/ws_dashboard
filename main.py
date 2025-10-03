#!/usr/bin/env python3
import os
from pathlib import Path
from typing import Any, Dict, List
import shutil
from collections import OrderedDict

import yaml
from ruamel.yaml import YAML
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from datetime import datetime, timezone, date
import asyncio
import httpx

APP_DIR = Path(__file__).parent.resolve()
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
CONFIG_PATH = APP_DIR / "config.yaml"

app = FastAPI(title="Windsurf Dashboard (Southern Germany)")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --- DWD images and caching ---
DWD_IMAGES = {
    "main": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/bwk_bodendruck_na_ana.png",
    "v36": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_v36.png",
    "036": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_036.png",
    "048": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_048.png",
    "060": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_060.png",
    "084": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_084.png",
    "108": "https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_108.png",
}

CACHE_DIR = APP_DIR / "cache" / "dwd"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_dwd_locks = {name: asyncio.Lock() for name in DWD_IMAGES.keys()}

# --- Pydantic models for API ---
class WindyConfig(BaseModel):
    zoom: int = 10
    overlay: str = "wind"
    units_wind: str = "kmh"
    marker: bool = True
    detail: bool = True

class WindfinderConfig(BaseModel):
    widget_src: str

class SpotConfig(BaseModel):
    provider: str = "windy"
    lat: float
    lon: float
    wind_directions: str = "N,NE,E,SE,S,SW,W,NW"  # Default to all directions
    windy: WindyConfig = WindyConfig()
    windfinder: WindfinderConfig = None

class SpotUpdate(BaseModel):
    name: str = None  # For renaming
    config: SpotConfig = None

class SpotReorderRequest(BaseModel):
    spot_order: List[str]

# Initialize YAML handler for preserving order and comments
yaml_handler = YAML()
yaml_handler.preserve_quotes = True
yaml_handler.default_flow_style = False

def _is_fresh(path: Path, max_age_hours: int = 2) -> bool:
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

# --- config ---
def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml_handler.load(f)
    cfg.setdefault("rotation", {"enabled": False, "interval_seconds": 30})
    cfg.setdefault("spots", {})
    return cfg

def save_config(cfg: Dict[str, Any]) -> None:
    """Save config to YAML file with backup."""
    # Create backup
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    shutil.copy2(CONFIG_PATH, backup_path)
    
    # Save new config
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml_handler.dump(cfg, f)

def generate_views_from_spots(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate views automatically from spots configuration."""
    spots = cfg.get("spots", {})
    views = []
    
    # Main overview view that shows DWD and forecasts for all spots
    if spots:
        views.append({
            "name": "overview",
            "show_dwd": True,
            "spots": list(spots.keys())
        })
    
    # Individual detail views for each spot (show_dwd=false)
    for spot_name in spots.keys():
        views.append({
            "name": spot_name,
            "show_dwd": False,
            "spots": [spot_name]
        })
    
    return views

# --- Windy/Windfinder URL builders ---
def windy_iframe_src(lat: float, lon: float, opts: Dict[str, Any]) -> str:
    # Map embed (with map) - this should show the interactive map for detail pages
    zoom = opts.get("zoom", 6)
    overlay = opts.get("overlay", "wind")
    marker = "true" if opts.get("marker", True) else "false"
    units_wind = opts.get("units_wind", "default")  # default, kmh, ms, kt, mph, bft
    product = opts.get("product", "ecmwf")
    # Updated to include marker at specific location and pressure isolines
    return (
        "https://embed.windy.com/embed.html"
        "?type=map"
        "&location=coordinates"
        f"&metricRain=default"
        f"&metricTemp=default"
        f"&metricWind={units_wind}"
        f"&zoom={zoom}"
        f"&overlay={overlay}"
        f"&product={product}"
        "&level=surface"
        f"&lat={lat:.3f}&lon={lon:.3f}"
        f"&detailLat={lat:.3f}&detailLon={lon:.3f}"
        "&detail=true"
        "&pressure=true"
    )

def windy_forecast_iframe_src(lat: float, lon: float, opts: dict) -> str:
    """
    Windy forecast-only (no map) widget.
    Docs/example come from the Windy embed configurator; this endpoint is supported.
    Modified to show only wind data for more compact display.
    """
    units_wind = opts.get("units_wind", "kmh")  # kmh, ms, kt, mph, bft
    return (
        "https://embed.windy.com/embed.html"
        "?type=forecast"
        "&location=coordinates"
        "&detail=true"
        f"&detailLat={lat:.5f}&detailLon={lon:.5f}"
        f"&metricWind={units_wind}"
        "&overlay=wind"
        "&isolines=0"
        "&airportIdent="
        "&showAirports=false"
    )

def windfinder_iframe_src(widget_src: str) -> str:
    return widget_src

# --- Config API endpoints ---
@app.get("/api/config")
async def get_config():
    """Get the current configuration."""
    try:
        cfg = load_config()
        return JSONResponse(cfg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load config: {str(e)}")

@app.get("/api/config/spots")
async def get_spots():
    """Get all spots."""
    try:
        cfg = load_config()
        return JSONResponse(cfg.get("spots", {}))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load spots: {str(e)}")

@app.put("/api/config/spots/reorder")
async def reorder_spots(request: SpotReorderRequest):
    """Reorder spots according to the provided list."""
    try:
        cfg = load_config()
        current_spots = cfg["spots"]
        spot_order = request.spot_order
        
        # Validate that all spots in the order exist
        if set(spot_order) != set(current_spots.keys()):
            raise HTTPException(status_code=400, detail="Spot order doesn't match existing spots")
        
        # Create new ordered dict
        new_spots = {}
        for spot_name in spot_order:
            new_spots[spot_name] = current_spots[spot_name]
        
        cfg["spots"] = new_spots
        save_config(cfg)
        return JSONResponse({"message": "Spots reordered successfully"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reorder spots: {str(e)}")

@app.post("/api/config/spots/{spot_name}")
async def add_spot(spot_name: str, spot_config: SpotConfig):
    """Add a new spot."""
    try:
        cfg = load_config()
        if spot_name in cfg["spots"]:
            raise HTTPException(status_code=400, detail="Spot already exists")
        
        cfg["spots"][spot_name] = spot_config.dict()
        save_config(cfg)
        return JSONResponse({"message": f"Spot '{spot_name}' added successfully"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add spot: {str(e)}")

@app.put("/api/config/spots/{spot_name}")
async def update_spot(spot_name: str, update: SpotUpdate):
    """Update or rename an existing spot."""
    try:
        cfg = load_config()
        if spot_name not in cfg["spots"]:
            raise HTTPException(status_code=404, detail="Spot not found")
        
        # Handle renaming
        if update.name and update.name != spot_name:
            if update.name in cfg["spots"]:
                raise HTTPException(status_code=400, detail="New spot name already exists")
            # Move the spot to new name
            cfg["spots"][update.name] = cfg["spots"].pop(spot_name)
            spot_name = update.name
        
        # Update configuration
        if update.config:
            cfg["spots"][spot_name] = update.config.dict()
        
        save_config(cfg)
        return JSONResponse({"message": f"Spot '{spot_name}' updated successfully"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update spot: {str(e)}")

@app.delete("/api/config/spots/{spot_name}")
async def delete_spot(spot_name: str):
    """Delete a spot."""
    try:
        cfg = load_config()
        if spot_name not in cfg["spots"]:
            raise HTTPException(status_code=404, detail="Spot not found")
        
        del cfg["spots"][spot_name]
        save_config(cfg)
        return JSONResponse({"message": f"Spot '{spot_name}' deleted successfully"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete spot: {str(e)}")

# --- DWD endpoints ---
@app.get("/dwd/{name}.png")
async def dwd_image(name: str):
    if name not in DWD_IMAGES:
        return HTMLResponse("Unknown DWD image", status_code=404)

    url = DWD_IMAGES[name]
    out = CACHE_DIR / f"{name}.png"

    if not _is_fresh(out, max_age_hours=2):
        lock = _dwd_locks[name]
        async with lock:
            if not _is_fresh(out, max_age_hours=2):
                await _fetch_and_cache(url, out)

    headers = {"Cache-Control": "public, max-age=3600"}
    return FileResponse(out, media_type="image/png", headers=headers)

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
def root():
    cfg = load_config()
    views = generate_views_from_spots(cfg)
    if views:
        return RedirectResponse(url=f"/view/{views[0]['name']}")
    return HTMLResponse("<h1>No spots configured. Please edit config.yaml</h1>")

@app.get("/view/{view_name}", response_class=HTMLResponse)
def view_page(request: Request, view_name: str):
    cfg = load_config()
    views: List[Dict[str, Any]] = generate_views_from_spots(cfg)
    view_names = [v["name"] for v in views]
    if view_name not in view_names:
        return HTMLResponse(f"<h1>Unknown view: {view_name}</h1>", status_code=404)

    view = next(v for v in views if v["name"] == view_name)

    show_dwd = bool(view.get("show_dwd", False))
    spot_cards = []

    # Views with show_dwd=true use forecast widgets; views with show_dwd=false use map widgets
    use_maps = not show_dwd  # If DWD is not shown, use maps instead of forecast widgets
    
    for spot_name in view.get("spots", []):
        spec = cfg["spots"].get(spot_name)
        if not spec:
            spot_cards.append({
                "title": f"{spot_name} (missing in config)", 
                "iframe_src": None, 
                "detail_link": None,
                "view_link": None,
                "wind_directions": "N,NE,E,SE,S,SW,W,NW"
            })
            continue

        provider = spec.get("provider", "windy").lower()
        title = spot_name
        iframe_src = None
        detail_link = f"/spot/{spot_name}"
        # Add link to spot view (only on overview page with multiple spots)
        view_link = f"/view/{spot_name}" if show_dwd and len(view.get("spots", [])) > 1 else None

        if provider == "windy":
            lat = spec.get("lat"); lon = spec.get("lon")
            windy_opts = spec.get("windy", {})
            if lat is None or lon is None:
                spot_cards.append({
                    "title": f"{title} (missing lat/lon)", 
                    "iframe_src": None, 
                    "detail_link": None,
                    "view_link": None
                })
            else:
                # Use map widget if DWD is not shown, forecast widget if DWD is shown
                if use_maps:
                    iframe_src = windy_iframe_src(lat, lon, windy_opts)
                else:
                    iframe_src = windy_forecast_iframe_src(lat, lon, windy_opts)

        elif provider == "windfinder":
            wf = spec.get("windfinder", {})
            src = wf.get("widget_src")
            if src:
                iframe_src = windfinder_iframe_src(src)
            else:
                spot_cards.append({
                    "title": f"{title} (missing windfinder.widget_src)", 
                    "iframe_src": None, 
                    "detail_link": None,
                    "view_link": None
                })

        else:
            spot_cards.append({
                "title": f"{title} (unknown provider: {provider})", 
                "iframe_src": None, 
                "detail_link": None,
                "view_link": None,
                "wind_directions": spec.get("wind_directions", "N,NE,E,SE,S,SW,W,NW")
            })

        if iframe_src:
            spot_cards.append({
                "title": title, 
                "iframe_src": iframe_src, 
                "detail_link": detail_link,
                "view_link": view_link,
                "wind_directions": spec.get("wind_directions", "N,NE,E,SE,S,SW,W,NW")
            })

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
            "compact": show_dwd,  # compact=true (forecast) when DWD shown, compact=false (maps) when not
            "dwd_version": date.today().strftime("%Y%m%d"),
        },
    )

@app.get("/spot/{spot_name}", response_class=HTMLResponse)
def spot_detail(request: Request, spot_name: str):
    """Detail page for a single spot: Windy map (or Windfinder widget)."""
    cfg = load_config()
    spec = cfg["spots"].get(spot_name)
    if not spec:
        raise HTTPException(status_code=404, detail="Unknown spot")

    provider = spec.get("provider", "windy").lower()
    title = spot_name
    iframe_src = None

    if provider == "windy":
        lat = spec.get("lat"); lon = spec.get("lon")
        windy_opts = spec.get("windy", {})
        if lat is not None and lon is not None:
            # DETAIL PAGE -> show the map
            iframe_src = windy_iframe_src(lat, lon, windy_opts)
    elif provider == "windfinder":
        src = spec.get("windfinder", {}).get("widget_src")
        if src:
            iframe_src = src

    spot_cards = []
    if iframe_src:
        spot_cards.append({
            "title": title, 
            "iframe_src": iframe_src, 
            "detail_link": None,
            "view_link": None,
            "wind_directions": spec.get("wind_directions", "N,NE,E,SE,S,SW,W,NW")
        })

    # You can choose to keep DWD on the left on detail pages; set to False if not desired.
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_view": f"spot:{spot_name}",
            "views": [],                 # hide top view tabs on detail (optional)
            "show_dwd": False,
            "spot_cards": spot_cards,
            "rotation_enabled": False,   # usually no rotation on detail
            "rotation_interval": 30,
            "compact": False,            # detail -> maps
            "dwd_version": date.today().strftime("%Y%m%d"),
        },
    )

@app.get("/config", response_class=HTMLResponse)
def config_editor(request: Request):
    """Configuration editor page."""
    cfg = load_config()
    return templates.TemplateResponse(
        "config_editor.html",
        {
            "request": request,
            "config": cfg,
            "spots": cfg.get("spots", {}),
        },
    )

@app.get("/config/edit/{spot_name}", response_class=HTMLResponse)
def edit_spot_page(request: Request, spot_name: str):
    """Edit spot page."""
    cfg = load_config()
    spot_config = cfg["spots"].get(spot_name)
    if not spot_config:
        raise HTTPException(status_code=404, detail="Spot not found")
    
    return templates.TemplateResponse(
        "edit_spot.html",
        {
            "request": request,
            "spot_name": spot_name,
            "spot_config": spot_config,
        },
    )
