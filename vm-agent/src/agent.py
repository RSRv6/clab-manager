from fastapi import FastAPI

from src.api.routes import router
from src.logging_config import setup_logging

setup_logging()

app = FastAPI(title="VM Agent", version="0.1.0")
app.include_router(router)