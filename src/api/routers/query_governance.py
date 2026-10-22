"""Route registration for Query Governance and retained compatibility handlers."""

from fastapi import APIRouter
from fastapi.routing import APIRoute

from ..application.query_governance import router as query_governance_router
from ..application.query_governance_legacy import router as legacy_router

router = APIRouter()

# Core read paths are owned by the deterministic API.  Keep the legacy router
# only for routes that have not migrated yet (A/B Test and controlled writes),
# instead of registering two handlers for the same method/path.
_deterministic_read_keys = {
    (method, route.path)
    for route in query_governance_router.routes
    if isinstance(route, APIRoute)
    for method in route.methods
}
_remaining_legacy_router = APIRouter()
_remaining_legacy_router.routes.extend(
    route
    for route in legacy_router.routes
    if not (
        isinstance(route, APIRoute)
        and any((method, route.path) in _deterministic_read_keys for method in route.methods)
    )
)

router.include_router(query_governance_router)
router.include_router(_remaining_legacy_router)

__all__ = ["router"]
