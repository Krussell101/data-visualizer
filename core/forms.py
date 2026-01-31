import magic
from django import forms
from django.core.exceptions import ValidationError
from .models import Dataset


class DatasetUploadForm(forms.ModelForm):
    """
    Form for uploading datasets with security-critical file validation.
    """

    class Meta:
        model = Dataset
        fields = ['file', 'name']
        widgets = {
            'file': forms.FileInput(attrs={'accept': '.csv,.xlsx,.xls'}),
        }

    def clean_file(self):
        """
        Validate file type using magic bytes (content-type), not just extension.
        Security-critical: prevents malicious file uploads.
        """
        uploaded_file = self.cleaned_data.get('file')

        if not uploaded_file:
            raise ValidationError("No file was uploaded.")

        # Check file size (100MB limit)
        if uploaded_file.size > 104857600:  # 100MB in bytes
            raise ValidationError("File size must be less than 100MB.")

        # Read first 2048 bytes for magic byte detection
        uploaded_file.seek(0)
        file_head = uploaded_file.read(2048)
        uploaded_file.seek(0)

        # Detect content type using python-magic
        mime = magic.Magic(mime=True)
        content_type = mime.from_buffer(file_head)

        # Allowed MIME types
        allowed_types = [
            'text/csv',
            'text/plain',  # CSV files sometimes detected as text/plain
            'application/vnd.ms-excel',  # .xls
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',  # .xlsx
        ]

        if content_type not in allowed_types:
            raise ValidationError(
                f"Invalid file type: {content_type}. "
                f"Only CSV and Excel files (.csv, .xlsx, .xls) are allowed."
            )

        # Block macro-enabled Excel files
        file_name = uploaded_file.name.lower()
        if file_name.endswith('.xlsm'):
            raise ValidationError("Macro-enabled Excel files (.xlsm) are not allowed for security reasons.")

        return uploaded_file

    def clean_name(self):
        """Ensure name is not empty and reasonable length."""
        name = self.cleaned_data.get('name', '').strip()

        if not name:
            raise ValidationError("Dataset name is required.")

        if len(name) > 255:
            raise ValidationError("Dataset name must be less than 255 characters.")

        return name


class QueryForm(forms.Form):
    """
    Simple form for submitting natural language queries.
    """
    prompt = forms.CharField(
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': 'Ask a question about your data...',
            'class': 'form-control'
        }),
        max_length=2000,
        label='Your Question'
    )

    def clean_prompt(self):
        """Ensure prompt is not empty."""
        prompt = self.cleaned_data.get('prompt', '').strip()

        if not prompt:
            raise ValidationError("Please enter a question.")

        return prompt
