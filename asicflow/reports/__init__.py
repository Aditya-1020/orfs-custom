"""Report builders: dual JSON/text/CSV artifacts for humans and scripts."""

from .base import Finding, Report, ReportTable, load_report
from .path_diagnose import PathDiagnosis, diagnose_path_text, diagnose_paths_report
from .path_groups import PATH_GROUPS, path_group_report
from .regression import generate_regression_report
from .summary import generate_summary

__all__ = [
    "Finding",
    "PATH_GROUPS",
    "PathDiagnosis",
    "Report",
    "ReportTable",
    "diagnose_path_text",
    "diagnose_paths_report",
    "generate_regression_report",
    "generate_summary",
    "load_report",
    "path_group_report",
]
