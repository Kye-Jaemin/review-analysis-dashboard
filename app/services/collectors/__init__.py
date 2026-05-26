from app.models.source import SourceType
from app.services.collectors.app_store import AppStoreCollector
from app.services.collectors.base import CollectorBase, CollectedItem
from app.services.collectors.google_play import GooglePlayCollector
from app.services.collectors.reddit import RedditCollector
from app.services.collectors.web import WebCollector

COLLECTORS: dict[SourceType, type[CollectorBase]] = {
    SourceType.google_play: GooglePlayCollector,
    SourceType.app_store: AppStoreCollector,
    SourceType.reddit: RedditCollector,
    SourceType.web: WebCollector,
}


def get_collector(source) -> CollectorBase:
    cls = COLLECTORS[source.type]
    return cls(source)


__all__ = [
    "COLLECTORS",
    "get_collector",
    "CollectorBase",
    "CollectedItem",
    "GooglePlayCollector",
    "AppStoreCollector",
    "RedditCollector",
    "WebCollector",
]
