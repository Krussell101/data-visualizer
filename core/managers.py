from django.db import models


class QueryLogManager(models.Manager):
    """Custom manager for QueryLog model with conversation context retrieval."""

    def get_context_window(self, session, max_entries=10):
        """
        Return last N query/response pairs for conversation context.

        Args:
            session: AnalysisSession instance
            max_entries: Maximum number of entries to return (default 10)

        Returns:
            QuerySet of QueryLog instances ordered by created_at descending
        """
        return self.filter(session=session).order_by('-created_at')[:max_entries]
