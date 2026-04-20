"""Event store -- backend-agnostic interface plus two concrete drivers."""

from .backend import StoreBackend
from .db import EventStore, WriteJob
from .libsql_backend import LibsqlBackend
from .sqlite_backend import SqliteBackend

__all__ = [
    "EventStore",
    "LibsqlBackend",
    "SqliteBackend",
    "StoreBackend",
    "WriteJob",
]
