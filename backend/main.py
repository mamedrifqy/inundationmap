"""
============================================================
  MANGROVE REHABILITATION SUITABILITY — FastAPI Backend
  Connects Google Earth Engine Python API to the web frontend
  
  Author : Muhammad Rifqy
  Year   : 2026
============================================================
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import Optional
import ee
import json
import os
import traceback

# ── INIT APP ──────────────────────────────────────────────
app = FastAPI(
    title="MangroveMap API",
    description="Mangrove Rehabilitation Suitability Analysis via GEE",
    version="1.0.0"
)

# Allow frontend to call the API (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── NGROK BYPASS MIDDLEWARE ───────────────────────────────
# ngrok free tier shows a browser warning page by default.
# Setting this header on every response tells ngrok to skip it.
@app.middleware("http")
async def add_ngrok_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response

# Serve the frontend HTML
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

# ── GEE INITIALIZATION ────────────────────────────────────
GEE_PROJECT = os.environ.get("GEE_PROJECT", "your-project-id")  # set via env var

def init_gee():
    """Initialize GEE. Uses service account if SA_KEY_PATH env var is set,
    otherwise falls back to user credentials (earthengine authenticate)."""
    sa_key = os.environ.get("SA_KEY_PATH")
    if sa_key and os.path.exists(sa_key):
        credentials = ee.ServiceAccountCredentials(
            email=os.environ.get("SA_EMAIL", ""),
            key_file=sa_key
        )
        ee.Initialize(credentials=credentials, project=GEE_PROJECT)
        print(f"[GEE] Authenticated via service account — project: {GEE_PROJECT}")
    else:
        ee.Initialize(project=GEE_PROJECT)
        print(f"[GEE] Authenticated via user credentials — project: {GEE_PROJECT}")

try:
    init_gee()
    GEE_READY = True
except Exception as e:
    print(f"[GEE] WARNING: Could not initialize — {e}")
    print("[GEE] Run 'earthengine authenticate' and restart the server.")
    GEE_READY = False


# ── REQUEST / RESPONSE MODELS ─────────────────────────────
class AnalysisRequest(BaseModel):
    asset_path: str          # e.g. "projects/my-project/assets/prm26"
    base_start: str          # "2024-01-01"
    base_end: str            # "2024-06-30"
    flood_start: str         # "2024-10-01"
    flood_end: str           # "2025-01-31"
    polygon_id: Optional[str] = None   # optional filter by feature ID field


class AnalysisResponse(BaseModel):
    status: str
    polygon_id: Optional[str]
    area_ha: dict            # { total, inundated, flooded, stress, suitable, submerged }
    tile_urls: dict          # { layer_name: xyz_tile_url }
    boundary_geojson: dict   # GeoJSON of the polygon boundary
    water_level_m: float
    error: Optional[str] = None


# ── ANALYSIS ENGINE ───────────────────────────────────────
def run_gee_analysis(req: AnalysisRequest) -> dict:
    """
    Core GEE analysis — Sentinel-1 SAR + Sentinel-2 + FABDEM
    Returns tile URLs and area stats for the frontend.
    """

    # ── 1. Load AOI ─────────────────────────────────────────
    # Strategy: fetch ALL features from the asset in Python, find the ones
    # we want by index position, then pass their raw GeoJSON geometry back
    # to GEE. This bypasses all server-side filter issues entirely.
    fc_full = ee.FeatureCollection(req.asset_path)
    all_feats = fc_full.getInfo().get('features', [])
    print(f"[AOI] asset has {len(all_feats)} features total")

    if req.polygon_id and req.polygon_id.strip():
        # pids may be full paths or short hex — normalise to short suffix
        pids = set(p.strip().split("/")[-1]
                   for p in req.polygon_id.split(",") if p.strip())
        print(f"[AOI] looking for pids: {pids}")

        # Match by: short suffix of feat['id'], OR positional index
        matched = []
        for i, feat in enumerate(all_feats):
            feat_suffix = feat.get('id', '').split('/')[-1]
            if feat_suffix in pids or str(i) in pids:
                matched.append(feat)
                print(f"[AOI]   matched feat {i}: id={feat.get('id')}")

        if not matched:
            # Last resort: use ALL features (show full asset)
            print(f"[AOI] WARNING — no pid match, using all {len(all_feats)} features")
            matched = all_feats
    else:
        matched = all_feats

    print(f"[AOI] using {len(matched)} feature(s) for AOI")

    # Build GeoJSON geometry from matched features — pure Python, no GEE filter
    if len(matched) == 1:
        raw_geom = matched[0]['geometry']
    else:
        # Combine into MultiPolygon manually
        all_coords = []
        for feat in matched:
            g = feat['geometry']
            if g['type'] == 'Polygon':
                all_coords.append(g['coordinates'])
            elif g['type'] == 'MultiPolygon':
                all_coords.extend(g['coordinates'])
        raw_geom = {'type': 'MultiPolygon', 'coordinates': all_coords}

    print(f"[AOI] raw_geom type: {raw_geom['type']}")
    aoi = ee.Geometry(raw_geom)

    # Build fc from matched features for boundary GeoJSON later
    fc = ee.FeatureCollection([ee.Feature(f) for f in matched])

    # Validate
    centroid = aoi.centroid(maxError=100).getInfo()
    print(f"[AOI] centroid: {centroid.get('coordinates')}")

    # ── 2. FABDEM ─────────────────────────────────────────
    fabdem = (ee.ImageCollection("projects/sat-io/open-datasets/FABDEM")
              .filterBounds(aoi).mosaic().clip(aoi))

    # ── 3. Sentinel-2 ─────────────────────────────────────
    def s2_mask(image):
        scl  = image.select('SCL')
        mask = scl.eq(3).Or(scl.gte(7).And(scl.lte(10))).eq(0)
        return image.select(['B.*']).divide(10000).updateMask(mask)

    sentinel2 = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
                 .filterBounds(aoi)
                 .filterDate(req.base_start, req.base_end)
                 .map(s2_mask)
                 .median()
                 .clip(aoi))

    Green = sentinel2.select('B3')
    NIR   = sentinel2.select('B8')
    NDWI  = Green.subtract(NIR).divide(Green.add(NIR)).rename('NDWI').clip(aoi)

    # ── 4. Sentinel-1 ─────────────────────────────────────
    s1_filter = ee.Filter.And(
        ee.Filter.eq('instrumentMode', 'IW'),
        ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'),
        ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'),
        ee.Filter.eq('orbitProperties_pass', 'DESCENDING')
    )

    s1_base  = (ee.ImageCollection('COPERNICUS/S1_GRD')
                .filterBounds(aoi).filterDate(req.base_start,  req.base_end)
                .filter(s1_filter).select(['VV','VH']))
    s1_flood = (ee.ImageCollection('COPERNICUS/S1_GRD')
                .filterBounds(aoi).filterDate(req.flood_start, req.flood_end)
                .filter(s1_filter).select(['VV','VH']))

    # Always clip to aoi — Algorithms.If returns unclipped image on empty collection
    def safe_median(col, bands):
        fallback = ee.Image.constant(ee.List.repeat(-25, len(bands))).rename(bands).clip(aoi)
        filled   = ee.Image(ee.Algorithms.If(col.size().gt(0), col.median(), fallback))
        return filled.clip(aoi)

    base_f  = safe_median(s1_base,  ['VV','VH']).focal_median(50, 'circle', 'meters')
    flood_f = safe_median(s1_flood, ['VV','VH']).focal_median(50, 'circle', 'meters')

    VV_base  = base_f.select('VV');  VH_base  = base_f.select('VH')
    VV_flood = flood_f.select('VV'); VH_flood = flood_f.select('VH')

    delta_VV = VV_flood.subtract(VV_base).rename('Delta_VV')
    delta_VH = VH_flood.subtract(VH_base).rename('Delta_VH')

    # ── 5. Classification masks (all clipped to aoi) ──────
    s2_water      = NDWI.gt(0).clip(aoi).rename('S2_Water')
    s1_open_water = VV_flood.lt(-16).And(VH_flood.lt(-23)).clip(aoi).rename('S1_Open_Water')
    double_bounce = delta_VH.gt(3).And(VH_flood.gt(-15)).clip(aoi).rename('S1_Double_Bounce')
    inundation    = s1_open_water.Or(double_bounce).Or(s2_water).clip(aoi).rename('Inundation')
    flood_mask    = delta_VV.lt(-2).clip(aoi).rename('Flood_BiTemporal')
    stress        = (NDWI.gt(-0.1).And(NDWI.lte(0))
                     .And(inundation.Not())
                     .And(flood_mask.Not())
                     .clip(aoi).rename('Stress_Zone'))

    # ── 6. Inundation depth (FABDEM) ──────────────────────
    open_flood  = s1_open_water.Or(s2_water).clip(aoi)
    edge        = open_flood.focal_max(1).subtract(open_flood)
    wl_dict     = fabdem.updateMask(edge).reduceRegion(
                    reducer=ee.Reducer.median(), geometry=aoi,
                    scale=30, maxPixels=1e9)
    wl_raw      = wl_dict.values().get(0)
    water_level = ee.Number(ee.Algorithms.If(
                    ee.Algorithms.IsEqual(wl_raw, None), 0, wl_raw))
    depth       = (ee.Image(water_level).subtract(fabdem)
                   .updateMask(open_flood).max(ee.Image(0))
                   .clip(aoi).rename('Depth_m'))
    submerged   = depth.gte(1.5).clip(aoi).rename('Permanent_Submergence')

    # ── 7. Suitable mask ──────────────────────────────────
    # Suitable = within AOI and NOT inundated/flooded/stressed/submerged
    unsuitable    = inundation.Or(flood_mask).Or(stress).Or(submerged).clip(aoi)
    suitable_mask = unsuitable.Not().clip(aoi).rename('Suitable')

    # Unsuitability class map (for optional composite layer)
    unsuitable_class = (ee.Image(0)
        .where(stress.eq(1),     ee.Image(2))
        .where(flood_mask.eq(1), ee.Image(1))
        .where(inundation.eq(1), ee.Image(3))
        .clip(aoi).rename('Unsuitability'))

    total_unsuitable = unsuitable.rename('Total_Unsuitable')

    # ── 8. Area statistics ────────────────────────────────
    px = ee.Image.pixelArea()

    def area_ha(mask, band):
        r = mask.multiply(px).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e13)
        return ee.Number(r.get(band)).divide(10000)

    total_r  = ee.Image(1).clip(aoi).multiply(px).reduceRegion(
                 ee.Reducer.sum(), aoi, 10, maxPixels=1e13)
    total_ha = ee.Number(total_r.values().get(0)).divide(10000)

    inund_ha  = area_ha(inundation,        'Inundation')
    flood_ha  = area_ha(flood_mask,        'Flood_BiTemporal')
    stress_ha = area_ha(stress,            'Stress_Zone')
    unsuit_ha = area_ha(total_unsuitable,  'Total_Unsuitable')
    sub_ha    = area_ha(submerged,         'Permanent_Submergence')

    stats_raw = ee.Dictionary({
        'total':      total_ha,
        'inundated':  inund_ha,
        'flooded':    flood_ha,
        'stress':     stress_ha,
        'unsuitable': unsuit_ha,
        'submerged':  sub_ha,
    }).getInfo()

    stats_raw['suitable'] = max(0.0, round(
        stats_raw['total'] - stats_raw['unsuitable'], 2))
    wl_val = water_level.getInfo()

    # ── 9. Tile URLs ──────────────────────────────────────
    def get_tile_url(image, vis_params):
        return image.getMapId(vis_params)['tile_fetcher'].url_format

    tile_urls = {
        'suitable': get_tile_url(
            suitable_mask.selfMask(),
            {'palette': ['00e5a0']}
        ),
        'true_color': get_tile_url(
            sentinel2.select(['B4','B3','B2']),
            {'min': 0, 'max': 0.25, 'gamma': 1.4}
        ),
        'inundation': get_tile_url(
            inundation.selfMask(),
            {'palette': ['1a237e']}
        ),
        'flood': get_tile_url(
            flood_mask.selfMask(),
            {'palette': ['FF6F00']}
        ),
        'stress': get_tile_url(
            stress.selfMask(),
            {'palette': ['FFD600']}
        ),
        'submerged': get_tile_url(
            submerged.selfMask(),
            {'palette': ['000060']}
        ),
        'depth': get_tile_url(
            depth,
            {'min': 0, 'max': 3, 'palette': ['eff3ff','6baed6','2171b5','084594']}
        ),
        'ndwi': get_tile_url(
            NDWI,
            {'min': -1, 'max': 1, 'palette': ['8B4513','white','0000FF']}
        ),
    }

    # ── 10. Boundary GeoJSON ──────────────────────────────
    boundary_geojson = fc.geometry().getInfo()

    return {
        'status':           'success',
        'polygon_id':       req.polygon_id,
        'area_ha':          {k: round(float(v), 2) for k, v in stats_raw.items()},
        'tile_urls':        tile_urls,
        'boundary_geojson': boundary_geojson,
        'water_level_m':    round(float(wl_val), 3) if wl_val else 0.0,
        'error':            None,
    }


# ── API ROUTES ────────────────────────────────────────────

@app.get("/")
def root():
    """Serve the frontend HTML"""
    index = os.path.join(frontend_path, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "MangroveMap API is running. Place index.html in /frontend/"}


@app.get("/health")
def health():
    """Health check — confirms GEE connection status"""
    return {
        "status": "ok",
        "gee_ready": GEE_READY,
        "project": GEE_PROJECT,
    }


@app.get("/polygons")
def list_polygons(asset_path: str):
    """
    List all features in a GEE FeatureCollection asset.
    Returns the real system:index as 'id' so /analyze can filter correctly.
    Usage: GET /polygons?asset_path=projects/my-project/assets/prm26
    """
    if not GEE_READY:
        raise HTTPException(503, "GEE not initialized. Check server logs.")
    try:
        fc   = ee.FeatureCollection(asset_path)
        info = fc.getInfo()
        polygons = []
        for feat in info.get('features', []):
          try:
            props = feat.get('properties') or {}
            geom  = feat.get('geometry')  or {}
            gtype = geom.get('type', '')

            # Extract coordinates safely — handle Polygon, MultiPolygon, empty/null
            all_lons, all_lats = [], []
            try:
                coords = geom.get('coordinates') or []
                if gtype == 'Point' and len(coords) >= 2:
                    all_lons.append(float(coords[0]))
                    all_lats.append(float(coords[1]))
                elif gtype == 'Polygon' and coords:
                    for pt in (coords[0] or []):
                        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                            all_lons.append(float(pt[0]))
                            all_lats.append(float(pt[1]))
                elif gtype == 'MultiPolygon' and coords:
                    for poly in coords:
                        for pt in (poly[0] if poly else []):
                            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                                all_lons.append(float(pt[0]))
                                all_lats.append(float(pt[1]))
            except Exception:
                pass  # silently fall through to default centroid

            if all_lons and all_lats:
                centroid = [sum(all_lats)/len(all_lats), sum(all_lons)/len(all_lons)]
            else:
                centroid = [0.22, 103.22]  # Kuala Selat default fallback

            # system:index is just the hex suffix (last path component)
            # feat['id'] from getInfo() = full path, e.g.
            # 'projects/xxx/assets/yyy/00000000000000000013'
            # We store only the suffix so /analyze filter matches correctly
            sys_index = feat.get('id', '0').split('/')[-1]

            # Derive display name: prefer 'kelompok' field, then common name fields
            name = (props.get('kelompok') or props.get('Kelompok') or
                    props.get('KELOMPOK') or props.get('name')     or
                    props.get('Name')     or props.get('Nama')     or
                    props.get('NAMA')     or props.get('id')       or
                    props.get('ID')       or f"Polygon {sys_index}")

            # Area: prefer pre-computed field, then compute from GEE
            area_ha = (props.get('area_ha') or props.get('Area_Ha') or
                       props.get('AREA_HA') or props.get('Shape_Area') or 0)
            try:
                area_ha = round(float(area_ha), 1)
            except Exception:
                area_ha = 0

            polygons.append({
                'id':         sys_index,
                'name':       str(name),
                'area_ha':    area_ha,
                'centroid':   centroid,
                'properties': props,
            })
          except Exception as feat_err:
            # Skip malformed features instead of crashing the whole request
            print(f"[polygons] Skipping feature {feat.get('id','?')}: {feat_err}")
            continue

        return {"status": "ok", "count": len(polygons), "polygons": polygons}
    except Exception as e:
        raise HTTPException(400, f"Could not load asset: {str(e)}")




@app.get("/debug")
def debug_properties(asset_path: str):
    """
    Returns ALL property keys and sample values from the first 3 features.
    Use this to discover the real field names in your GEE asset.
    """
    if not GEE_READY:
        raise HTTPException(503, "GEE not initialized.")
    try:
        fc   = ee.FeatureCollection(asset_path)
        info = fc.limit(3).getInfo()
        samples = []
        for feat in info.get('features', []):
            props = feat.get('properties') or {}
            samples.append({
                'id': feat.get('id'),
                'property_keys': sorted(props.keys()),
                'properties': props
            })
        return {"status": "ok", "samples": samples}
    except Exception as e:
        raise HTTPException(400, f"Debug error: {str(e)}")

@app.post("/analyze", response_model=AnalysisResponse)
def analyze(req: AnalysisRequest):
    """
    Run full inundation + suitability analysis on a GEE asset polygon.
    Returns tile URLs (XYZ) for Leaflet rendering + area statistics.

    Body:
      asset_path  : "projects/my-project/assets/prm26"
      base_start  : "2024-01-01"
      base_end    : "2024-06-30"
      flood_start : "2024-10-01"
      flood_end   : "2025-01-31"
      polygon_id  : "0"   (optional — omit to use entire collection)
    """
    if not GEE_READY:
        raise HTTPException(503, "GEE not initialized. Run 'earthengine authenticate' and restart.")
    try:
        result = run_gee_analysis(req)
        return result
    except ee.EEException as e:
        raise HTTPException(400, f"GEE error: {str(e)}")
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Analysis failed: {str(e)}")


@app.get("/export-status/{task_id}")
def export_status(task_id: str):
    """Check status of a GEE export task"""
    if not GEE_READY:
        raise HTTPException(503, "GEE not initialized.")
    try:
        tasks = ee.batch.Task.list()
        for t in tasks:
            if t.id == task_id:
                return {
                    "task_id": task_id,
                    "state":   t.state,
                    "description": t.config.get('description', ''),
                }
        raise HTTPException(404, "Task not found")
    except Exception as e:
        raise HTTPException(500, str(e))
