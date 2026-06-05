# Health Dynamics

Starter project for a Flask application using SQLAlchemy, SQLite, and Bulma.

## Structure

```text
health-dynamics/
├── app/
│   ├── __init__.py
│   ├── models.py
│   ├── views.py
│   ├── forms.py
│   ├── templates/
│   └── static/
├── etl/
├── analytics/
├── warehouse/
├── data/
│   ├── raw/
│   └── processed/
├── tests/
├── config.py
├── run.py
├── requirements.txt
└── README.md
```

## Setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Start the app with `python run.py`.

To inspect an Excel file without loading it into the database, run
`python etl/explore_excel.py /path/to/workbook.xlsx`.

The ETL, analytics, and warehouse layers are intentionally left unimplemented for now.
