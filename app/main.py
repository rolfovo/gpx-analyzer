from fastapi import FastAPI, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import shutil, os, zipfile, io
from datetime import datetime

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# In-memory databáze (pro demo)
rides = []
horses = []

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "rides": rides})

@app.post("/upload")
async def upload_gpx(
    gpx_file: UploadFile,
    horse: str = Form(None),
    ride_name: str = Form(None),
    ride_date: str = Form(None)
):
    if not os.path.exists("uploads"):
        os.makedirs("uploads")
    file_path = f"uploads/{gpx_file.filename}"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(gpx_file.file, buffer)
    rides.append({
        "date": ride_date or datetime.now().strftime("%Y-%m-%d"),
        "name": ride_name or gpx_file.filename,
        "horse": horse,
        "distance": round(10 + len(rides) * 2.5, 2),
        "avg_speed": round(5 + len(rides), 2),
        "max_speed": round(15 + len(rides) * 1.5, 2),
    })
    return RedirectResponse("/", status_code=303)

@app.get("/backup")
async def backup():
    mem_file = io.BytesIO()
    with zipfile.ZipFile(mem_file, "w") as zf:
        zf.writestr("rides.txt", str(rides))
        zf.writestr("horses.txt", str(horses))
    mem_file.seek(0)
    return FileResponse(mem_file, media_type="application/zip", filename="backup.zip")
