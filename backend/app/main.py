from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.pipeline import router as pipeline_router
from app.api.chatbot import router as chatbot_router
from app.api.auth import router as auth_router
from app.core.config import settings

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
)

# Enable CORS for local and production Vercel frontend deployments
origins = [origin.strip() for origin in settings.frontend_urls.split(",") if origin.strip()]
for default_origin in ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3001", "http://127.0.0.1:3001"]:
    if default_origin not in origins:
        origins.append(default_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow production Vercel deployments and local frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(pipeline_router, prefix="/api")
app.include_router(chatbot_router, prefix="/api")
app.include_router(auth_router, prefix="/api")

# Serve built React frontend if available
dist_path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if dist_path.exists():
    app.mount("/", StaticFiles(directory=str(dist_path), html=True), name="static")
