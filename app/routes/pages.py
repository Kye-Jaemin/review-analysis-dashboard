from fastapi import APIRouter, Request

from app.templating import render

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    return render(request, "dashboard.html")
