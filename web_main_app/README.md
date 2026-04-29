# LineageTrace Web Main App

This folder contains a browser-based version of the current main app, built without modifying the desktop app files.

## Features

- Login using the existing app credential store
- Dashboard
- Experiment search
- New experiment creation
- Experiment list
- Inventory list and edit/delete
- Orphanage view
- Protocol Builder draft page

## Run

```bash
cd /path/to/LineageTrace
source venv/bin/activate
python -m uvicorn web_main_app.app:app --reload
```

Then open:

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

## Notes

- This is isolated in `web_main_app/` so the desktop app remains unchanged.
- It reuses the current `pyapp.database` backend functions, so it will follow the same configured backend behavior available in this repo.
