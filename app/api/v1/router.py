"""Aggregates every v1 route module under a single router.

Introducing ``/api/v2`` later means adding a sibling package and mounting it
alongside this one - v1 clients keep working untouched.
"""

from fastapi import APIRouter

from app.api.v1 import auth, ipos, notifications, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(ipos.router)
api_router.include_router(notifications.router)

__all__ = ["api_router"]
