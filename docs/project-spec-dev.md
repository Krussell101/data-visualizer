# Project Specification: Insight (PandasAI Django Wrapper) - Development Version

## 1. Executive Summary

Insight is a local development web application allowing users to upload structured data files (Excel/CSV), preview the data, and utilize natural language queries via PandasAI to generate insights and interactive visualizations. This development version is designed for prototyping and learning, running entirely on a local laptop with no connection to corporate networks.

The architecture prioritizes standard Django patterns for maintainability, specifically utilizing "Fat Models" to encapsulate business logic, and uses HTMX for dynamic user interactions. The application runs on a local development server with local file storage and PostgreSQL via Docker.

## 2. Technology Stack & Rationale

**Backend:** Python 3.11+, Django 6.0+.

**Database:** PostgreSQL via Docker.
- Rationale: SQLite and PostgreSQL have enough behavioral differences (JSONField, ALTER TABLE locking, migrations) that developing on SQLite gives false confidence. Use PostgreSQL from day one via Docker Compose for easy local setup.

**AI Engine:** Anthropic API (Direct).
- Model: Claude Sonnet 4.5 (`claude-sonnet-4-5-20250929`) via the Anthropic API.
- Library: PandasAI with the Anthropic Python SDK (`anthropic` package).
- Configuration: Uses API key stored in environment variables (`ANTHROPIC_API_KEY`). No AWS or cloud dependencies.

**Visualization Engine:** Plotly.
- Rationale: Provides interactive charts (zoom, hover, export) which are superior to static Matplotlib images for data exploration. Plotly figures serialize as JSON, making them storable and replayable without re-execution.

**Frontend:** Django Templates + Bootstrap 5 (UI) + HTMX (Interactivity) + Plotly.js (Rendering).

**Infrastructure:** Local development server.
- File Storage: Local filesystem using Django's `FileSystemStorage` with a `media/` directory.
- Database: PostgreSQL running in Docker container.
- Server: Django's development server (`manage.py runserver`) for rapid iteration, or Gunicorn for production-like testing.

**Request Handling:** Synchronous with Django development server or Gunicorn. No Celery or Redis needed for this development version.

## 3. Data Architecture (Models & Business Logic)

We follow the Fat Model philosophy. models.py contains both the schema and the behavioral logic.

### 3.1 Dataset Model

Represents the uploaded file.

**Fields:**
- `id`: UUID (Primary Key)
- `user`: ForeignKey (Links to AUTH_USER_MODEL, on_delete=CASCADE)
- `file`: FileField (Stores the actual Excel/CSV in local `media/uploads/` directory)
- `name`: CharField (Original filename)
- `uploaded_at`: DateTimeField (auto_now_add=True)
- `status`: CharField (choices: `pending`, `processing`, `ready`, `error`; default: `pending`)
- `metadata`: JSONField

**Metadata Schema:**

```json
{
    "row_count": 15000,
    "column_count": 12,
    "columns": [
        {"name": "Region", "dtype": "object", "null_count": 0, "sample_values": ["East", "West"]},
        {"name": "Revenue", "dtype": "float64", "null_count": 3, "sample_values": [1234.56, 7890.12]}
    ],
    "file_size_bytes": 2048000,
    "sheet_names": ["Sheet1", "Budget Q1"],
    "parse_warnings": []
}
```

The metadata schema will be refined during the Phase 0 spike based on what information is actually useful for PandasAI context and user-facing previews.

**Business Logic (Methods):**

- **`ingest_and_validate()`**: Called immediately after upload.
  - Uses pandas to read the file from the local filesystem.
  - Validates file type (content-type verification, not just extension), file size, and structure.
  - Populates self.metadata using the schema above.
  - Sets self.status = "ready" on success, "error" on failure.

- **`get_dataframe()`**: Returns the Pandas DataFrame for this dataset. Uses `utils.get_dataframe_cached()` to avoid repeated file reads and pandas parsing across multiple queries in a session.

### 3.2 AnalysisSession Model

Represents a conversation about a specific dataset.

**Fields:**
- `user`: ForeignKey (Links to AUTH_USER_MODEL, on_delete=CASCADE)
- `dataset`: ForeignKey (Links to Dataset, on_delete=CASCADE)
- `title`: CharField
- `created_at`: DateTimeField (auto_now_add=True)
- `updated_at`: DateTimeField (auto_now=True)

**Business Logic (Methods):**

- **`execute_query(prompt_text)`**: The core logic method.
  - **Client Retrieval:** Retrieves the Anthropic LLM client via a utility function (e.g., utils.get_llm_client()). This uses a singleton/cached pattern to avoid overhead from re-instantiating the Anthropic client on every request.
  - **Context Rehydration:** Retrieves previous QueryLog entries for this session using the QueryLogManager's `get_context_window()` method, which returns the most recent entries within a configurable token budget (default: last 10 exchanges). Formats this history into the context structure required by PandasAI.
  - **Data Loading:** Calls self.dataset.get_dataframe() (which uses the cached DataFrame).
  - **Execution:** Configures SmartDataframe with the rehydrated context and prompt, then runs the query.
  - **Error Handling:** Wraps the Anthropic API call in try/except for `anthropic.APIError` and PandasAI-specific exceptions. Rate limiting (429), context length exceeded, timeouts, and malformed responses are caught and recorded.
  - **Logging:** Creates and returns a new QueryLog instance with the results and status.

### 3.3 QueryLog Model

Stores the chat history.

**Fields:**
- `session`: ForeignKey (Links to AnalysisSession, on_delete=CASCADE)
- `prompt`: TextField (User input)
- `response_text`: TextField (blank=True, default="")
- `response_plot_json`: JSONField (Plotly configuration; null=True, blank=True)
- `status`: CharField (choices: `success`, `error`; default: `success`)
- `error_message`: TextField (blank=True, default=""; stores user-facing error description on failure)
- `created_at`: DateTimeField (auto_now_add=True)

**Manager:**
- `QueryLogManager`: Handles context windowing via `get_context_window(session, max_entries=10)` which returns the most recent entries for conversation context injection. This is required, not optional -- unbounded context will exceed the model's context window after extended sessions.

## 4. Key Functional Workflows

### 4.1 File Upload (The "Ingest" Pipeline)

1. User submits form with Excel/CSV file.
2. **Form Validation (forms.py):** The form's `clean_file()` method validates:
   - File type via content-type verification (using `python-magic` or header byte inspection, not just extension). Only `.xlsx` and `.csv` accepted.
   - File size against a configured maximum (`FILE_UPLOAD_MAX_MEMORY_SIZE`).
   - Rejects `.xlsm` (macro-enabled) and other potentially dangerous formats.
3. **View Logic:** Standard Django CreateView or functional view handles the form save. Sets `dataset.user = request.user`.
4. **Storage:** Save file to local `media/uploads/` directory using Django's default `FileSystemStorage`.
5. **Model Logic:** The view calls `dataset_instance.ingest_and_validate()`. Sets status to `ready` or `error`.

### 4.2 The Chat Interface (The "Insight" Loop)

1. User types a prompt (e.g., "Plot sales by region").
2. **HTMX Request:** POST request to a Django view.
3. **UX:** Uses `hx-indicator` for a "Thinking..." spinner and `hx-disabled-elt` on the submit button to prevent duplicate submissions during the 5-15 second Bedrock response time.

```html
<form hx-post="{% url 'query' session.pk %}"
      hx-target="#chat-history"
      hx-swap="beforeend"
      hx-indicator="#spinner"
      hx-disabled-elt="button[type='submit']">
    <textarea name="prompt"></textarea>
    <button type="submit">Ask</button>
</form>
<div id="spinner" class="htmx-indicator">Thinking...</div>
```

4. **View Logic:**
   - Verify `request.user` owns the session (`get_object_or_404(AnalysisSession, pk=pk, user=request.user)`).
   - Call `session.execute_query(request.POST['prompt'])`.
5. **Model Method Execution (execute_query):**
   - **LLM Config:** Uses cached client from utils.
   - **PandasAI Config:** Configure SmartDataframe to force Plotly JSON output, enforce rich hover data, and inject conversation context from `QueryLogManager.get_context_window()`.
   - **Capture & Save:** The method saves the result into a new QueryLog object (with `status="success"` or `status="error"`) and returns it to the view.
6. **Response:** The View renders the `chat_message.html` partial using the returned QueryLog object. Error states render a user-friendly message in the chat bubble.

## 5. UX/UI Strategy

### 5.1 Conversational Drill-Down

- **Context Awareness:** The SmartDataframe inside the model method receives context via the session history (retrieved via `QueryLogManager.get_context_window()`). Context is bounded to the last 10 exchanges to avoid exceeding the model's context window and to control token costs.
- **Rich Tooltips:** Plotly client-side hover.
- **Note:** The effectiveness of PandasAI's conversation memory must be validated during the Phase 0 spike. Each query may work better as an independent call with just the DataFrame context, with the "conversation" being purely a UI affordance showing previous Q&A pairs.

### 5.2 The "Artifact Gallery" (Sidebar)

- **Implementation:** HTMX Out-of-Band (OOB) Swaps. The partial rendered by the view will include the chat bubble AND the OOB sidebar item.

## 6. Directory Structure (Fat Models Approach)

We removed services.py. All logic is now in models.py, managers.py, or utils.py.

```
insight/
├── manage.py
├── docker-compose.yml     # PostgreSQL container configuration
├── .env                   # Environment variables (ANTHROPIC_API_KEY, etc.)
├── insight/               # Project Configuration
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── core/                  # Main Application
│   ├── migrations/
│   ├── models.py          # Schema + Business Logic (Anthropic API/PandasAI code here)
│   ├── managers.py        # Custom Managers (QueryLogManager for context windowing)
│   ├── views.py           # Lean Views (delegate to Model methods, enforce user ownership)
│   ├── urls.py            # Route definitions
│   ├── forms.py           # Input validation (file type, size, content-type checks)
│   ├── utils.py           # Pure functions & Singletons (LLM Client Factory, DataFrame Cache)
│   └── templates/
│       └── core/
│           ├── upload.html
│           ├── chat.html
│           └── partials/  # HTMX Partials
│               ├── chat_message.html
│               └── sidebar_artifact.html
├── media/                 # Local file uploads (gitignored)
│   └── uploads/
├── requirements/
│   ├── base.txt           # Core dependencies (Django, PandasAI, Anthropic SDK, Plotly, etc.)
│   └── dev.txt            # django-debug-toolbar, django-extensions
└── README.md
```

Note: The `media/` directory stores all uploaded files locally and should be added to `.gitignore`.

## 7. Implementation Phases

### Phase 0: Spike / Proof-of-Concept (Before Any Django Code)

Retire the biggest technical risk: does PandasAI + Anthropic API actually work and produce useful results? This is a standalone Python/Jupyter notebook exercise with no web framework.

**Day 1 -- Plumbing:**
1. Create a Python virtual environment with `pandasai`, `anthropic`, `pandas`, `openpyxl`, `plotly`.
2. Get the Anthropic SDK calling Claude Sonnet 4.5 using an API key. Test basic API connectivity.
3. Get PandasAI configured to use the Anthropic SDK as its LLM backend.

**Day 2 -- Quality:**
4. Load 3 representative datasets: (a) clean CSV with sample data, (b) messy multi-sheet Excel file, (c) a 100MB file (performance test).
5. Run 20 representative queries against each dataset. Document success rate, failure modes, and latency.
6. Test Plotly output: can PandasAI produce Plotly figures? If not, document what it produces and the workaround path.
7. Test conversation memory: does injecting prior Q&A as context improve follow-up queries?

**Day 3 -- Feasibility:**
8. Test sandboxing: run adversarial prompts (`"delete all files"`, `"make an HTTP request"`, `"import subprocess"`). Document what PandasAI does.
9. Measure memory usage with the 100MB file using `tracemalloc`.
10. Write a one-page summary of findings.

**Pass/Fail Criteria:**
- Anthropic SDK can successfully call Claude API with API key: **pass/fail**
- PandasAI produces correct answers for >= 70% of representative queries: **pass/fail**
- End-to-end latency (prompt to result) is under 30 seconds for typical queries: **pass/fail**
- Plotly output is achievable (natively or via documented workaround): **pass/fail**
- Memory usage for a 100MB file is under 4GB: **pass/fail**

If PandasAI doesn't natively support the Anthropic SDK, investigate using the Anthropic SDK to create a custom LLM wrapper for PandasAI.

### Phase 1: MVP

- Django project setup (`insight/` project, `core/` app).
- Docker Compose configuration for PostgreSQL database.
- User authentication using Django's built-in auth (`LoginView`, `LogoutView`, `@login_required`). Accounts created via Django admin / `createsuperuser`.
- Dataset model with user ownership, file upload pipeline to local `media/` directory, `ingest_and_validate()`.
- AnalysisSession and QueryLog models with user ownership.
- Integration with Anthropic API inside AnalysisSession methods (using findings from Phase 0 spike).
- Basic chat interface with HTMX loading states and duplicate-submission prevention.
- Error handling for Anthropic API failures with user-facing error messages in chat.
- Environment variable management for `ANTHROPIC_API_KEY` via `.env` file.

### Phase 2: Interactive Visualization & UX

- Configure PandasAI to generate Plotly figures (or implement workaround identified in spike).
- Implement the "Plotly JSON to Frontend" pipeline.
- Implement the Artifact Sidebar using HTMX OOB swaps.
- DataFrame caching via `functools.lru_cache` in utils.py.

### Phase 3: Stretch Goals

- **Heuristic Fixer:** Robust handling of malformed Excel files inside `ingest_and_validate()` (scope: merged cells, multiple header rows, mixed data types in columns).
- **Auto-Suggestions:** Analyze metadata to suggest initial queries.
- **Export:** Generate PDF reports.

## 8. Security Considerations (Local Development Focus)

### 8.1 Authentication & Authorization

- **Authentication:** Django's built-in auth system. Username/password login for local development/testing.
- **Authorization:** All views enforce user ownership. `Dataset` and `AnalysisSession` models have `user` ForeignKeys. Views filter querysets by `request.user` and use `get_object_or_404` with user scope. All views use `LoginRequiredMixin` (CBVs) or `@login_required` (FBVs).
- **Session Management:** Django's default database-backed sessions (`django.contrib.sessions.backends.db`).

### 8.2 PandasAI Code Execution

PandasAI works by having an LLM generate Python code and then executing that code (`exec()`) against the data. This is the primary security concern.

- **Local Development Risk:** Since this is running on a local development laptop, the risk is contained to the local machine. However, still be cautious about executing untrusted code.
- **PandasAI Safe Mode:** Enable PandasAI's built-in `safe_mode` or equivalent sandbox configuration if available. Validate this during the Phase 0 spike.
- **Risk Assessment:** For development purposes, the risk of intentionally malicious prompts is low. However, prompts misinterpreted by the LLM could potentially cause unintended file operations or system calls. The local development environment limits the blast radius to the local machine.
- **Future Hardening:** If stronger isolation is needed in the future, investigate `RestrictedPython` or Docker containerization (Phase 3 concern).

### 8.3 File Upload Security

- **Allowed Types:** Only `.xlsx` and `.csv`. Enforced via content-type verification (file header bytes), not just extension.
- **Size Limits:** Configured via Django's `FILE_UPLOAD_MAX_MEMORY_SIZE` and `DATA_UPLOAD_MAX_MEMORY_SIZE`.
- **Macro Protection:** `.xlsm` (macro-enabled Excel) and files with embedded objects or external data connections are rejected.
- **Parsing Safety:** `openpyxl` (used by pandas) does not execute macros by default.

### 8.4 API Key Security

- **Anthropic API Key:** Store the `ANTHROPIC_API_KEY` in a `.env` file (never commit to git). Use `python-decouple` or `django-environ` to load environment variables.
- **`.gitignore`:** Ensure `.env` and `media/` directory are in `.gitignore` to prevent accidental commits of secrets or uploaded files.
- **Local Only:** This development version is for local use only. Never deploy with DEBUG=True or expose to the internet.

## 9. Local Development Setup & Deployment

### 9.1 Docker Compose for PostgreSQL

The application uses PostgreSQL running in a Docker container for database consistency with production environments.

**docker-compose.yml:**
```yaml
version: '3.8'

services:
  db:
    image: postgres:15
    environment:
      POSTGRES_DB: insight_dev
      POSTGRES_USER: insight_user
      POSTGRES_PASSWORD: insight_password
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

volumes:
  postgres_data:
```

**Starting the database:**
```bash
docker-compose up -d
```

### 9.2 Development Server

For rapid development, use Django's built-in development server:

```bash
python manage.py runserver
```

For production-like testing with multiple threads, use Gunicorn:

```bash
gunicorn insight.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 120
```

### 9.3 DataFrame Caching

Use in-process `functools.lru_cache` to avoid repeated file reads and pandas parsing:

```python
# core/utils.py
import functools
import pandas as pd

@functools.lru_cache(maxsize=32)
def get_dataframe_cached(dataset_id: str, file_path: str) -> pd.DataFrame:
    """Cache DataFrames in memory to avoid repeated file reads."""
    if file_path.endswith('.csv'):
        return pd.read_csv(file_path)
    else:
        return pd.read_excel(file_path)
```

Per-process cache, suitable for single-developer local development. Clear the cache by restarting the server.

### 9.4 Environment Variables

Create a `.env` file in the project root:

```
# .env (DO NOT COMMIT TO GIT)
SECRET_KEY=your-secret-key-here
DEBUG=True
DATABASE_URL=postgresql://insight_user:insight_password@localhost:5432/insight_dev
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
ALLOWED_HOSTS=localhost,127.0.0.1
```

Use `python-decouple` or `django-environ` to load these variables in `settings.py`.

### 9.5 Database Configuration

Use `dj-database-url` for clean database configuration:

```python
# settings.py
import dj_database_url
from decouple import config

DATABASES = {
    'default': dj_database_url.config(
        default=config('DATABASE_URL')
    )
}

# Connection pooling for better performance
CONN_MAX_AGE = 60
```

### 9.6 Development Django Settings

Key settings for local development:

```python
# settings.py
DEBUG = config('DEBUG', default=False, cast=bool)
SECRET_KEY = config('SECRET_KEY')
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost').split(',')

# File uploads - local storage
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# File upload limits (100MB max)
FILE_UPLOAD_MAX_MEMORY_SIZE = 104857600  # 100MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 104857600  # 100MB

# Static files
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Anthropic API
ANTHROPIC_API_KEY = config('ANTHROPIC_API_KEY')
```

### 9.7 Initial Setup Steps

1. **Clone repository and create virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements/base.txt
   pip install -r requirements/dev.txt
   ```

3. **Start PostgreSQL:**
   ```bash
   docker-compose up -d
   ```

4. **Create `.env` file** with required variables (see 9.4 above).

5. **Run migrations:**
   ```bash
   python manage.py migrate
   ```

6. **Create superuser:**
   ```bash
   python manage.py createsuperuser
   ```

7. **Run development server:**
   ```bash
   python manage.py runserver
   ```

8. **Access application:**
   - Main app: http://localhost:8000
   - Admin: http://localhost:8000/admin
