import uuid
import json
import pandas as pd
from pathlib import Path
from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
from .managers import QueryLogManager


class Dataset(models.Model):
    """
    Stores uploaded data files (CSV, Excel).
    Business logic: file ingestion, validation, and metadata extraction.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('ready', 'Ready'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='datasets')
    file = models.FileField(upload_to='datasets/')
    name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.name} ({self.user.username})"

    def ingest_and_validate(self):
        """
        Parse file with pandas, validate, and populate metadata.
        Sets status to 'ready' on success, 'error' on failure.
        """
        try:
            self.status = 'processing'
            self.save()

            # Read file based on extension
            file_path = self.file.path
            if file_path.endswith('.csv'):
                df = pd.read_csv(file_path)
            else:
                df = pd.read_excel(file_path)

            # Validate
            if df.empty:
                raise ValueError("File has no data rows")

            if len(df.columns) == 0:
                raise ValueError("File has no columns")

            # Build metadata
            columns_metadata = []
            for col in df.columns:
                # Get sample values (first 5 unique non-null values)
                sample_values = df[col].dropna().unique()[:5].tolist()

                columns_metadata.append({
                    'name': str(col),
                    'dtype': str(df[col].dtype),
                    'null_count': int(df[col].isnull().sum()),
                    'sample_values': [str(v) for v in sample_values]
                })

            self.metadata = {
                'row_count': len(df),
                'column_count': len(df.columns),
                'columns': columns_metadata,
                'file_size_bytes': Path(file_path).stat().st_size,
                'parse_warnings': []
            }

            self.status = 'ready'
            self.save()

        except Exception as e:
            self.status = 'error'
            self.metadata = {
                'error': str(e),
                'parse_warnings': [str(e)]
            }
            self.save()
            raise

    def get_dataframe(self):
        """
        Return cached DataFrame via utils.
        """
        from . import utils
        return utils.get_dataframe_cached(str(self.id), self.file.path)


class AnalysisSession(models.Model):
    """
    Represents a conversation about a dataset.
    Business logic: executing queries with PandasAI and managing conversation context.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sessions')
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name='sessions')
    title = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.title} - {self.user.username}"

    def execute_query(self, prompt_text):
        """
        Core business logic: Execute a query using PandasAI with Anthropic.

        Args:
            prompt_text: User's natural language query

        Returns:
            QueryLog instance with the results or error
        """
        from . import utils
        from pandasai import SmartDataframe

        try:
            # Get LLM client
            llm = utils.get_llm_client()

            # Get conversation context
            context_logs = QueryLog.objects.get_context_window(self, max_entries=10)

            # Get DataFrame
            df = self.dataset.get_dataframe()

            # Configure PandasAI
            config = {
                "llm": llm,
                "save_charts": False,
                "enable_cache": False,
                "verbose": True,
                "enforce_privacy": True,
            }

            # Create SmartDataframe
            sdf = SmartDataframe(df, config=config)

            # Execute query
            result = sdf.chat(prompt_text)

            # Extract plot JSON if available
            plot_json = None
            if hasattr(result, 'figure'):
                import plotly
                plot_json = json.loads(result.figure.to_json())

            # Create success log
            query_log = QueryLog.objects.create(
                session=self,
                prompt=prompt_text,
                response_text=str(result) if result is not None else "Query executed successfully",
                response_plot_json=plot_json,
                status='success'
            )

        except Exception as e:
            # Create error log
            query_log = QueryLog.objects.create(
                session=self,
                prompt=prompt_text,
                error_message=str(e),
                status='error'
            )

        return query_log


class QueryLog(models.Model):
    """
    Stores chat history (prompt/response pairs).
    """
    STATUS_CHOICES = [
        ('success', 'Success'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(AnalysisSession, on_delete=models.CASCADE, related_name='queries')
    prompt = models.TextField()
    response_text = models.TextField(blank=True)
    response_plot_json = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = QueryLogManager()

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.session.title} - {self.prompt[:50]}"
