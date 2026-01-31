from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import CreateView, DetailView
from django.urls import reverse
from django.http import HttpResponse
from .models import Dataset, AnalysisSession, QueryLog
from .forms import DatasetUploadForm, QueryForm


class DatasetUploadView(LoginRequiredMixin, CreateView):
    """
    View for uploading datasets.
    Thin orchestration: validates form, sets user, delegates to model.
    """
    model = Dataset
    form_class = DatasetUploadForm
    template_name = 'core/upload.html'

    def form_valid(self, form):
        """Set user ownership and trigger ingestion."""
        dataset = form.save(commit=False)
        dataset.user = self.request.user
        dataset.save()

        # Delegate business logic to model
        try:
            dataset.ingest_and_validate()
        except Exception as e:
            # If ingestion fails, show error on upload page
            form.add_error(None, f"Error processing file: {str(e)}")
            dataset.delete()
            return self.form_invalid(form)

        # Create a default analysis session for this dataset
        session = AnalysisSession.objects.create(
            user=self.request.user,
            dataset=dataset,
            title=f"Analysis of {dataset.name}"
        )

        # Redirect to chat interface
        return redirect('chat', pk=session.pk)


class ChatView(LoginRequiredMixin, DetailView):
    """
    View for the chat interface.
    Thin orchestration: authorization and template rendering.
    """
    model = AnalysisSession
    template_name = 'core/chat.html'
    context_object_name = 'session'

    def get_queryset(self):
        """Filter to only user's own sessions (authorization)."""
        return AnalysisSession.objects.filter(user=self.request.user)

    def get_context_data(self, **kwargs):
        """Add query form to context."""
        context = super().get_context_data(**kwargs)
        context['form'] = QueryForm()
        context['queries'] = self.object.queries.all()
        return context


@login_required
def query_view(request, session_pk):
    """
    HTMX endpoint for submitting queries.
    Thin orchestration: validates, authorizes, delegates to model.
    """
    # Authorization: ensure user owns the session
    session = get_object_or_404(
        AnalysisSession,
        pk=session_pk,
        user=request.user
    )

    if request.method == 'POST':
        form = QueryForm(request.POST)

        if form.is_valid():
            prompt_text = form.cleaned_data['prompt']

            # Delegate business logic to model
            query_log = session.execute_query(prompt_text)

            # Return partial template for HTMX swap
            return render(request, 'core/partials/chat_message.html', {
                'query': query_log
            })

        else:
            # Return error message for HTMX
            return HttpResponse(
                '<div class="alert alert-danger">Invalid query. Please try again.</div>',
                status=400
            )

    # GET request not allowed
    return HttpResponse('Method not allowed', status=405)
