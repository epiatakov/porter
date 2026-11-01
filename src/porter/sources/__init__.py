"""Task sources: where porter gets its work."""

from .base import TaskSource
from .github_issues import GitHubIssues
from .local_file import LocalFile

__all__ = ["GitHubIssues", "LocalFile", "TaskSource"]
