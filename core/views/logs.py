"""
Log viewer views for the control panel.
"""
import logging
from datetime import datetime
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.services.log_service import LogService
from core.views.decorators import superuser_required
from core.views.params import positive_int

logger = logging.getLogger(__name__)


# The application log is a single shared, non-workspace-scoped file: it mixes
# every workspace's run activity and exception output, so reading it is
# admin-only (matching the destructive ``logs_clear_view`` sibling).
@login_required
@superuser_required
def logs_view(request: HttpRequest) -> HttpResponse:
    """Display logs with filtering options."""
    filters = _read_filters(request)
    page, per_page = filters["page"], 100
    entries, total_count = _filtered_entries(filters, per_page)

    # Calculate pagination
    total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 1

    # Get log file stats
    log_files = LogService.get_log_files()
    total_size = LogService.get_log_file_size()

    return render(
        request,
        "cpanel/logs.html",
        {
            "entries": entries,
            "total_count": total_count,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "level_filter": filters["level"],
            "search_query": filters["search"],
            "date_from": filters["date_from"],
            "date_to": filters["date_to"],
            # Pager links carry the filters URL-encoded (U76: "a&b", "C++", "#").
            "filter_query": _filter_query(filters),
            "log_levels": LogService.LOG_LEVELS,
            "log_files_count": len(log_files),
            "total_size": total_size,
        },
    )


def _read_filters(request) -> dict:
    return {
        "level": request.GET.get("level") or "",
        "search": request.GET.get("search", "").strip(),
        "date_from": request.GET.get("date_from") or "",
        "date_to": request.GET.get("date_to") or "",
        "page": positive_int(request.GET.get("page"), 1),
    }


def _filter_query(filters) -> str:
    keys = ("level", "search", "date_from", "date_to")
    return urlencode({key: filters[key] for key in keys if filters[key]})


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _filtered_entries(filters, per_page):
    level = filters["level"]
    return LogService.read_logs(
        level_filter=level if level in LogService.LOG_LEVELS else None,
        search_query=filters["search"] or None,
        start_date=_parse_date(filters["date_from"]),
        end_date=_parse_date(filters["date_to"]),
        limit=per_page,
        offset=(filters["page"] - 1) * per_page,
    )


def _entry_dict(entry) -> dict:
    return {
        "timestamp": entry.timestamp.isoformat(),
        "level": entry.level,
        "logger": entry.logger,
        "message": entry.message,
        "exception": entry.exception,
    }


@login_required
def logs_api_view(request: HttpRequest) -> JsonResponse:
    """API endpoint for real-time log updates."""
    if not request.user.is_superuser:
        return JsonResponse(
            {
                "success": False,
                "error": "Permission denied. Only administrators can read logs.",
            },
            status=403,
        )

    if request.GET.get("view") == "page":
        # The Logs page's auto-refresh (U76): the same filtered page as the HTML
        # view, so only the table is redrawn and the scroll position is kept.
        filters = _read_filters(request)
        entries, total_count = _filtered_entries(filters, 100)
        return JsonResponse(
            {
                "success": True,
                "total_count": total_count,
                "entries": [_entry_dict(entry) for entry in entries],
            }
        )

    lines = positive_int(request.GET.get("lines"), 50, maximum=200)  # capped for performance

    entries = LogService.tail_logs(lines=lines)

    return JsonResponse(
        {
            "success": True,
            "entries": [_entry_dict(entry) for entry in entries],
        }
    )


@login_required
@require_POST
def logs_clear_view(request: HttpRequest) -> JsonResponse:
    """Clear all log files (superuser only)."""
    if not request.user.is_superuser:
        return JsonResponse(
            {
                "success": False,
                "error": "Permission denied. Only administrators can clear logs.",
            },
            status=403,
        )

    try:
        bytes_freed = LogService.clear_logs()
        logger.info(f"Logs cleared by {request.user.email}, freed {bytes_freed} bytes")
        return JsonResponse(
            {
                "success": True,
                "message": f"Logs cleared. Freed {bytes_freed:,} bytes.",
            }
        )
    except Exception as e:
        logger.exception("Failed to clear logs")
        return JsonResponse(
            {
                "success": False,
                "error": str(e),
            }
        )
