# GPX Analyzer – Variant B (FastAPI + Render)

## Local run
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload

## Deploy on Render (Native/Python)
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Add a Disk and set env: `DATABASE_URL=sqlite:////data/gpx_analyzer.db`
- Or add Render Postgres and set `DATABASE_URL=postgresql+psycopg2://...`
