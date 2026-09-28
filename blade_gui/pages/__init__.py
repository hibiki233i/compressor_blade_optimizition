"""GUI pages."""
from __future__ import annotations

from .analytics import AnalyticsPage
from .base import Page
from .cases_page import CasesPage
from .config_page import ConfigPage
from .dashboard import DashboardPage
from .run_page import RunPage
from .validation_page import ValidationPage

#: sidebar order
PAGE_CLASSES = [DashboardPage, AnalyticsPage, ConfigPage, RunPage, CasesPage, ValidationPage]

__all__ = [
    "AnalyticsPage",
    "CasesPage",
    "ConfigPage",
    "DashboardPage",
    "PAGE_CLASSES",
    "Page",
    "RunPage",
]
