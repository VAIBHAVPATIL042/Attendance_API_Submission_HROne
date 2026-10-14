# HROne Attendance API

## Setup on Windows

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `MONGO_URI` and `MONGO_DB` in `.env`. For local MongoDB, start MongoDB on
`localhost:27017`; MongoDB 7.0 is recommended. MongoDB Atlas can be used by
setting its connection string in `MONGO_URI`.

Seed the sample data and start the API:

```powershell
python sample_seed.py
uvicorn app.main:app --port 8000
```

Open the interactive API documentation at <http://127.0.0.1:8000/docs>.

The application creates its required indexes at startup. Do not commit `.env`,
`.venv`, secrets, Dockerfiles, or database dumps.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
