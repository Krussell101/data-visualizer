# Project Specification: Insight (PandasAI Django Wrapper)

## 1. Executive Summary

Insight is a single-tenant internal web application for a Fortune 500 company, allowing users to upload structured data files (Excel/CSV), preview the data, and utilize natural language queries via PandasAI to generate insights and interactive visualizations. The application serves two primary user groups: mechanical/electrical engineers analyzing power plant operational data, and financial analysts reviewing large budget spreadsheets for savings opportunities and anomalous patterns.

The architecture prioritizes standard Django patterns for maintainability, specifically utilizing "Fat Models" to encapsulate business logic, and uses HTMX for dynamic user interactions. The application is deployed on AWS GovCloud via Fargate and is accessible only on the internal corporate network.

## 2. Technology Stack & Rationale

**Backend:** Python 3.11+, Django 6.0+.

**Database:** PostgreSQL (all environments via RDS in production, Docker locally).
- Rationale: SQLite and PostgreSQL have enough behavioral differences (JSONField, ALTER TABLE locking, migrations) that developing on SQLite gives false confidence. Use PostgreSQL from day one.

**AI Engine:** AWS Bedrock (via GovCloud).
- Model: Anthropic Claude Sonnet 3.7 (`anthropic.claude-3-7-sonnet-20250219-v1:0`), with potential upgrade to Sonnet 4.5 pending resolution of unrelated issues.
- Library: PandasAI with litellm for model routing. litellm claims support for all Bedrock models.
- Configuration: Uses boto3 with GovCloud region endpoints (e.g., us-gov-west-1). IAM role authentication (no access keys).

**Visualization Engine:** Plotly.
- Rationale: Provides interactive charts (zoom, hover, export) which are superior to static Matplotlib images for data exploration. Plotly figures serialize as JSON, making them storable and replayable without re-execution.

**Frontend:** Django Templates + Bootstrap 5 (UI) + HTMX (Interactivity) + Plotly.js (Rendering).

**Infrastructure:** AWS GovCloud (Fargate for compute, S3 for file storage, RDS PostgreSQL for database, ALB for load balancing).

**Request Handling:** Synchronous with Gunicorn gthread workers. No Celery or Redis. Scale is handled by adding Gunicorn threads and Fargate tasks.

## 3. Data Architecture (Models & Business Logic)

We follow the Fat Model philosophy. models.py contains both the schema and the behavioral logic.

### 3.1 Dataset Model

Represents the uploaded file.

**Fields:**
- `id`: UUID (Primary Key)
- `user`: ForeignKey (Links to AUTH_USER_MODEL, on_delete=CASCADE)
- `file`: FileField (Stores the actual Excel/CSV via S3Boto3Storage)
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
  - Uses pandas to read the file from S3.
  - Validates file type (content-type verification, not just extension), file size, and structure.
  - Populates self.metadata using the schema above.
  - Sets self.status = "ready" on success, "error" on failure.

- **`get_dataframe()`**: Returns the Pandas DataFrame for this dataset. Uses `utils.get_dataframe_cached()` to avoid repeated S3 reads and pandas parsing across multiple queries in a session.

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
  - **Client Retrieval:** Retrieves the Bedrock LLM client via a utility function (e.g., utils.get_llm_client()). This uses a singleton/cached pattern to avoid overhead from re-instantiating boto3 clients on every request.
  - **Context Rehydration:** Retrieves previous QueryLog entries for this session using the QueryLogManager's `get_context_window()` method, which returns the most recent entries within a configurable token budget (default: last 10 exchanges). Formats this history into the context structure required by PandasAI.
  - **Data Loading:** Calls self.dataset.get_dataframe() (which uses the cached DataFrame).
  - **Execution:** Configures SmartDataframe with the rehydrated context and prompt, then runs the query.
  - **Error Handling:** Wraps the Bedrock call in try/except for `botocore.exceptions.ClientError` and PandasAI-specific exceptions. Throttling (429), context length exceeded, timeouts, and malformed responses are caught and recorded.
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
4. **Storage:** Save file to `storages.backends.s3boto3.S3Boto3Storage`.
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
├── insight/               # Project Configuration
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── core/                  # Main Application
│   ├── migrations/
│   ├── models.py          # Schema + Business Logic (Bedrock/PandasAI code here)
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
├── requirements/
│   ├── base.txt           # Shared dependencies
│   ├── dev.txt            # django-debug-toolbar, django-extensions
│   └── prod.txt           # gunicorn, django-storages[boto3]
└── gunicorn.conf.py
```

Note: No local `media/` directory in production. `DEFAULT_FILE_STORAGE` points to S3 in all deployed environments. A local `media/` directory may be used for local development only.

## 7. Implementation Phases

### Phase 0: Spike / Proof-of-Concept (Before Any Django Code)

Retire the biggest technical risk: does PandasAI + litellm + GovCloud Bedrock actually work and produce useful results? This is a standalone Python/Jupyter notebook exercise with no web framework.

**Day 1 -- Plumbing:**
1. Create a Python virtual environment with `pandasai`, `litellm`, `boto3`, `pandas`, `openpyxl`, `plotly`.
2. Get litellm calling Sonnet 3.7 via GovCloud Bedrock. Confirm IAM role auth works (not access keys, since Fargate uses task roles).
3. Get PandasAI configured to use litellm as its LLM backend.

**Day 2 -- Quality:**
4. Load 3 representative datasets: (a) clean CSV with sensor/meter readings (engineer use case), (b) messy multi-sheet Excel budget file (analyst use case), (c) a 100MB file (performance test).
5. Run 20 representative queries against each dataset. Document success rate, failure modes, and latency.
6. Test Plotly output: can PandasAI produce Plotly figures? If not, document what it produces and the workaround path.
7. Test conversation memory: does injecting prior Q&A as context improve follow-up queries?

**Day 3 -- Feasibility:**
8. Test sandboxing: run adversarial prompts (`"delete all files"`, `"make an HTTP request"`, `"import subprocess"`). Document what PandasAI does.
9. Measure memory usage with the 100MB file using `tracemalloc`.
10. Write a one-page summary of findings.

**Pass/Fail Criteria:**
- litellm can call Bedrock in GovCloud with IAM role auth: **pass/fail**
- PandasAI produces correct answers for >= 70% of representative queries: **pass/fail**
- End-to-end latency (prompt to result) is under 30 seconds for typical queries: **pass/fail**
- Plotly output is achievable (natively or via documented workaround): **pass/fail**
- Memory usage for a 100MB file is under 4GB: **pass/fail**

If litellm cannot talk to GovCloud Bedrock, investigate bypassing litellm and passing a direct boto3 Bedrock client to PandasAI.

### Phase 1: MVP

- Django project setup (`insight/` project, `core/` app).
- User authentication using Django's built-in auth (`LoginView`, `LogoutView`, `@login_required`). Accounts created via Django admin / `createsuperuser`.
- Dataset model with user ownership, file upload pipeline via S3, `ingest_and_validate()`.
- AnalysisSession and QueryLog models with user ownership.
- Integration with AWS Bedrock inside AnalysisSession methods (using findings from Phase 0 spike).
- Basic chat interface with HTMX loading states and duplicate-submission prevention.
- Error handling for Bedrock failures with user-facing error messages in chat.

### Phase 2: Interactive Visualization & UX

- Configure PandasAI to generate Plotly figures (or implement workaround identified in spike).
- Implement the "Plotly JSON to Frontend" pipeline.
- Implement the Artifact Sidebar using HTMX OOB swaps.
- DataFrame caching via `functools.lru_cache` in utils.py.

### Phase 3: Stretch Goals

- **Heuristic Fixer:** Robust handling of malformed Excel files inside `ingest_and_validate()` (scope: merged cells, multiple header rows, mixed data types in columns).
- **Auto-Suggestions:** Analyze metadata to suggest initial queries.
- **Export:** Generate PDF reports.

## 8. Security Considerations (AWS GovCloud Focus)

### 8.1 Authentication & Authorization

- **Authentication:** Django's built-in auth system. Username/password login. The application is only accessible on the internal corporate network (VPN/private network).
- **Authorization:** All views enforce user ownership. `Dataset` and `AnalysisSession` models have `user` ForeignKeys. Views filter querysets by `request.user` and use `get_object_or_404` with user scope. All views use `LoginRequiredMixin` (CBVs) or `@login_required` (FBVs).
- **Session Management:** Django's default database-backed sessions (`django.contrib.sessions.backends.db`). No Redis required.

### 8.2 PandasAI Code Execution

PandasAI works by having an LLM generate Python code and then executing that code (`exec()`) against the data. This is the primary security concern.

- **Fargate Containment:** The application runs on Fargate, which provides container-level isolation. The Fargate task IAM role has minimal permissions: S3 read/write to one bucket, Bedrock `InvokeModel`, RDS access, nothing else.
- **PandasAI Safe Mode:** Enable PandasAI's built-in `safe_mode` or equivalent sandbox configuration if available. Validate this during the Phase 0 spike.
- **Risk Assessment:** Users are trusted internal employees (engineers, analysts). The risk of intentionally malicious prompts is low, but prompts misinterpreted by the LLM (e.g., "delete all rows where X" causing file operations) are possible. The IAM role and container isolation limit the blast radius.
- **No Docker-in-Docker:** Fargate does not support running Docker inside containers. If stronger isolation is needed in the future, investigate `RestrictedPython` or subprocess execution with `seccomp` restrictions (Phase 3 concern).

### 8.3 File Upload Security

- **Allowed Types:** Only `.xlsx` and `.csv`. Enforced via content-type verification (file header bytes), not just extension.
- **Size Limits:** Configured via Django's `FILE_UPLOAD_MAX_MEMORY_SIZE` and `DATA_UPLOAD_MAX_MEMORY_SIZE`.
- **Macro Protection:** `.xlsm` (macro-enabled Excel) and files with embedded objects or external data connections are rejected.
- **Parsing Safety:** `openpyxl` (used by pandas) does not execute macros by default.

### 8.4 Infrastructure Security

- **IAM Roles:** Use Fargate task IAM roles for `bedrock:InvokeModel` and S3 access. No access keys.
- **Data Residency:** S3, Bedrock, and RDS endpoints must be in GovCloud region.
- **Transport Security:** `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` all enabled in production settings.
- **Network Isolation:** ALB in private subnet (internal network only). Fargate tasks in private subnets. VPC endpoint for S3 (gateway type). VPC endpoint for Bedrock Runtime (interface type).
- **Secrets Management:** Database credentials and any other secrets pulled from AWS Secrets Manager via environment variables, not hardcoded.
- **Logging:** Structured JSON logs for CloudWatch ingestion.

## 9. Infrastructure & Deployment

### 9.1 Fargate Configuration

**Container Image:** Single container with Django + Gunicorn + PandasAI + all dependencies.

```
ALB (internal) --> Fargate Service (2-4 tasks)
                       |
                       +--> Container: Django + Gunicorn (gthread)
                               port 8000
```

**Task Sizing:**
- **CPU:** 2 vCPU (recommended given pandas memory allocation patterns).
- **Memory:** 8GB (a 100MB Excel file can consume 1GB+ as a DataFrame; multiple concurrent users may have active DataFrames).
- **Scale:** Start with 2 Fargate tasks behind an internal ALB. Scale to 4 if needed. At 10 concurrent users, 2 tasks with gthread config gives 32 concurrent request slots.

### 9.2 Gunicorn Configuration

```python
# gunicorn.conf.py
worker_class = "gthread"
workers = 4          # 2-4 per vCPU
threads = 4          # 4 threads per worker = 16 concurrent requests per task
timeout = 120        # Bedrock can be slow; 120s covers worst case
graceful_timeout = 30
```

Rationale: gthread workers release the GIL during I/O (boto3 calls), allowing other threads to serve requests. With 4 workers x 4 threads = 16 concurrent requests per Fargate task.

### 9.3 Timeout Chain

Every layer must have consistent timeouts, with outer layers timing out before inner layers:

| Layer | Timeout |
|-------|---------|
| ALB idle timeout | 120s |
| Gunicorn timeout | 120s |
| boto3 read timeout | 90s |
| HTMX client | No timeout (server-side governs); show spinner via `hx-indicator` |

### 9.4 DataFrame Caching

Use in-process `functools.lru_cache` to avoid repeated S3 reads and pandas parsing:

```python
# core/utils.py
import functools
import pandas as pd

@functools.lru_cache(maxsize=32)
def get_dataframe_cached(dataset_id: str, file_url: str) -> pd.DataFrame:
    """Cache DataFrames in memory to avoid repeated S3 reads."""
    return pd.read_excel(file_url)  # or pd.read_csv based on file type
```

Per-process cache, shared across threads in that process. Some duplication across workers is acceptable at this scale. No Redis required.

### 9.5 Networking

- ALB in private subnet (VPN/internal network access only).
- Fargate tasks in private subnets.
- VPC endpoint for S3 (gateway type, free).
- VPC endpoint for Bedrock Runtime (interface type).
- Security group on Fargate tasks: inbound 8000 from ALB only, outbound to VPC endpoints and RDS only.
- RDS security group: inbound 5432 from Fargate security group only.

### 9.6 Database

RDS PostgreSQL (`db.t3.medium` or GovCloud equivalent). Django `DATABASE_URL` pattern with `dj-database-url` for clean Fargate configuration via environment variables. `CONN_MAX_AGE` set for connection pooling.

### 9.7 Production Django Settings

Required settings beyond Django defaults:
- `django-storages` with `S3Boto3Storage` configured for GovCloud S3 endpoint.
- `SECURE_SSL_REDIRECT = True`
- `SECURE_HSTS_SECONDS` (e.g., 31536000)
- `SESSION_COOKIE_SECURE = True`
- `CSRF_COOKIE_SECURE = True`
- `LOGGING` configured for structured JSON output (CloudWatch).
- `CONN_MAX_AGE` for database connection pooling.
- `DEFAULT_FILE_STORAGE` pointing to S3.
