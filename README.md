# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the Employee Attendance & Analytics API assignment.

## Project Files

Read the assignment files in this order:

1. `PROBLEM_STATEMENT.docx` — requirements and evaluation criteria.
2. `openapi.yaml` — API contract and business rules R1–R10.
3. `DATA_MODEL.md` — MongoDB data model and sample documents.
4. `app/main.py` — application implementation. All application code is contained in this file.
5. `REVIEW.md` and `DECISIONS.md` — starter-code review and implementation decisions.

The `sample_data/` directory contains the sample documents. `sample_seed.py` loads them into MongoDB.

## Local Setup

**Requirements:** Python 3.11+ and MongoDB 6.0+.

Create and activate a virtual environment in the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create a `.env` file using `.env.example` as a reference, and configure:

```text
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Load the sample data:

```powershell
python sample_seed.py
```

Start the application:

```powershell
python -m uvicorn app.main:app --port 8000
```

## API Documentation

Once the application is running:

- Swagger UI: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## Submission

The submission consists of a public Git repository containing:

- `app/main.py`
- `requirements.txt`
- `REVIEW.md`
- `DECISIONS.md`
- `README.md`

Do not commit `.env` files, credentials, or other secrets. A Dockerfile is not required by the assignment.

