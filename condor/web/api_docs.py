"""Serve API discovery behind the same authentication as the dashboard."""

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import JSONResponse

from condor.web.auth import get_current_user

_PRIVATE = {"Cache-Control": "private, no-store"}


def register_api_docs(app: FastAPI) -> None:
    dependencies = [Depends(get_current_user)]

    @app.get("/openapi.json", include_in_schema=False, dependencies=dependencies)
    async def schema(request: Request):
        document = app.openapi()
        prefix = request.scope.get("root_path", "").rstrip("/")
        servers = document.get("servers", [])
        if (
            prefix
            and app.root_path_in_servers
            and prefix not in {server.get("url") for server in servers}
        ):
            # Keep proxy-specific URLs out of the process-wide schema cache.
            document = {**document, "servers": [{"url": prefix}, *servers]}
        return JSONResponse(document, headers=_PRIVATE)

    @app.get("/docs", include_in_schema=False, dependencies=dependencies)
    async def swagger(request: Request):
        prefix = request.scope.get("root_path", "").rstrip("/")
        response = get_swagger_ui_html(
            openapi_url=f"{prefix}/openapi.json",
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=f"{prefix}/docs/oauth2-redirect",
        )
        response.headers.update(_PRIVATE)
        return response

    @app.get(
        "/docs/oauth2-redirect", include_in_schema=False, dependencies=dependencies
    )
    async def oauth_redirect():
        response = get_swagger_ui_oauth2_redirect_html()
        response.headers.update(_PRIVATE)
        return response

    @app.get("/redoc", include_in_schema=False, dependencies=dependencies)
    async def redoc(request: Request):
        prefix = request.scope.get("root_path", "").rstrip("/")
        response = get_redoc_html(
            openapi_url=f"{prefix}/openapi.json", title=f"{app.title} - ReDoc"
        )
        response.headers.update(_PRIVATE)
        return response
