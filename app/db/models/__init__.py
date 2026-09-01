"""ORM model registry.

Importing this package registers every model on ``Base.metadata``, which is
what Alembic autogenerate and the test schema builder rely on.
"""

from app.db.base import Base
from app.db.models.device import Device
from app.db.models.ipo import IPO, IPOSnapshot
from app.db.models.notification import NotificationDelivery, NotificationPreference
from app.db.models.scrape import ScrapeRawPayload, ScrapeRun
from app.db.models.user import User

__all__ = [
    "IPO",
    "Base",
    "Device",
    "IPOSnapshot",
    "NotificationDelivery",
    "NotificationPreference",
    "ScrapeRawPayload",
    "ScrapeRun",
    "User",
]
