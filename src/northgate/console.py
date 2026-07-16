from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse


async def console_index(request: Request) -> FileResponse:
    directory: Path = request.app.state.console_directory
    return FileResponse(directory / "index.html")
