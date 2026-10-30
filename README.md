# 🌿 MangroveMap — FastAPI + GEE Backend

Full-stack web app for Mangrove Rehabilitation Suitability Analysis.
Connects Google Earth Engine (Python API) to a Leaflet map frontend.

---

## Project Structure

```
inundationmap/
├── backend/
│   ├── main.py            ← FastAPI server (GEE analysis engine)
│   └── requirements.txt   ← Python dependencies
├── index.html             ← Web app (the only frontend file; served at "/")
├── MangroveMap_Colab.ipynb ← Run server on Google Colab + ngrok
└── README.md
```

> `MangroveMap_Colab.ipynb` embeds a base64 copy of `backend/main.py`.
> After editing `main.py`, regenerate that copy so the notebook stays in sync.

---

## Option A — Run Locally (Your Computer)

### Step 1: Install dependencies
```bash
pip install -r backend/requirements.txt
```

### Step 2: Authenticate GEE
```bash
earthengine authenticate
```

### Step 3: Set your GEE project ID
Edit `backend/main.py` line:
```python
GEE_PROJECT = os.environ.get("GEE_PROJECT", "your-project-id")
```
Or set an environment variable:
```bash
export GEE_PROJECT=your-project-id          # Mac/Linux
set GEE_PROJECT=your-project-id             # Windows
```

### Step 4: Start the server
```bash
# from the repository root
uvicorn backend.main:app --reload --port 8000
```
You should see:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
[GEE] Authenticated via user credentials — project: your-project-id
```

### Step 5: Open the frontend
Open `http://localhost:8000/` (the server serves `index.html`), or open `index.html` directly in your browser.
- Server URL field: `http://localhost:8000`
- Click **Connect**
- Enter your GEE asset path, click ↻
- Select a polygon → Run Analysis

---

## Option B — Run on Google Colab (No local setup)

1. Open `MangroveMap_Colab.ipynb` in Google Colab
2. Follow the cells step by step
3. Get a free ngrok token at https://dashboard.ngrok.com
4. The notebook will print a public URL like `https://abc123.ngrok.io`
5. Paste that URL into the frontend's server URL field

---

## Option C — Run on a Cloud Server (VPS / AWS / GCP)

### Using a Service Account (recommended for servers)
```bash
# Create a GEE service account key JSON file, then:
export SA_KEY_PATH=/path/to/service-account-key.json
export SA_EMAIL=your-sa@your-project.iam.gserviceaccount.com
export GEE_PROJECT=your-project-id

uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### With HTTPS (production)
```bash
# Using gunicorn + nginx recommended for production
pip install gunicorn
gunicorn backend.main:app -w 2 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Serves `index.html` |
| GET | `/health` | Check server + GEE status |
| GET | `/polygons?asset_path=...` | List polygons in a GEE asset |
| GET | `/debug?asset_path=...` | Property keys and sample values of the first 3 features |
| POST | `/analyze` | Run full suitability analysis |
| GET | `/export-status/{task_id}` | State of a GEE export task |

### POST /analyze — Request Body
Give either `asset_path` (optionally with `polygon_id`) **or** `drawn_geometry`
(a GeoJSON `Polygon`/`MultiPolygon`, as produced by the frontend's *Draw Polygon*
mode). When `drawn_geometry` is present it is used as the AOI and `asset_path` /
`polygon_id` are ignored. Invalid input returns HTTP 400.

```json
{
  "asset_path":  "projects/my-project/assets/prm26",
  "base_start":  "2024-01-01",
  "base_end":    "2024-06-30",
  "flood_start": "2024-10-01",
  "flood_end":   "2025-01-31",
  "polygon_id":  "0"
}
```

### POST /analyze — Response
```json
{
  "status": "success",
  "area_ha": {
    "total": 142.3,
    "inundated": 38.2,
    "flooded": 22.7,
    "stress": 31.4,
    "unsuitable": 92.3,
    "suitable": 50.0,
    "submerged": 4.8
  },
  "tile_urls": {
    "inundation": "https://earthengine.googleapis.com/map/.../tiles/{z}/{x}/{y}",
    "flood":      "https://earthengine.googleapis.com/map/.../tiles/{z}/{x}/{y}",
    "stress":     "...",
    "submerged":  "...",
    "true_color": "...",
    "depth":      "...",
    "ndwi":       "..."
  },
  "boundary_geojson": { "type": "Polygon", ... },
  "water_level_m": 1.24
}
```

---

## Analysis Logic

The backend runs the full GEE analysis pipeline:

| Constraint | Detection Method | Source |
|------------|-----------------|--------|
| **Inundation** | S1 specular (VV<−16, VH<−23) + double-bounce (ΔVH>+3) + S2 NDWI>0 | Sentinel-1 + Sentinel-2 |
| **Flooded** | Bi-temporal ΔVV < −2 dB vs dry season baseline | Sentinel-1 |
| **Stress Zone** | NDWI −0.1 to 0 (not inundated/flooded) | Sentinel-2 |
| **Perm. Submerged** | FABDEM depth ≥ 1.5 m | FABDEM DEM |

---

## Troubleshooting

**`EEException: Not found`** — Check your asset path in the frontend  
**`GEE not ready`** — Run `earthengine authenticate` and restart server  
**`CORS error`** — Make sure server is running and URL is correct in frontend  
**`ngrok tunnel expired`** — Free ngrok tunnels expire after 2 hours; restart the Colab notebook  
