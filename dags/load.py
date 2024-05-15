import os
import json
import re
from datetime import timedelta
import requests
from datetime import datetime, timedelta

import pandas as pd
from airflow.models.dag import DAG
from airflow.operators.python import PythonOperator
from db_utils import Database
import logging

logger = logging.getLogger(__name__)

DATA_PATH = "/opt/airflow/data/"
URL_FILE = os.path.join(DATA_PATH, "api", "url.json")
RESULTS_FILE = os.path.join(DATA_PATH, "api", "results.json")


def rename_columns(columns):
    """
    Rename columns to ensure compatibility with database constraints.
    """
    columns = [col.lower() for col in columns]
    patterns = [
        (r"[°/']", "_"),
        (r"²", "2"),
        (r"[(|)]", ""),
        (r"é|è", "e"),
        (r"â", "a"),
        (r"^_", "dpe_"),
        (r"_+", "_"),
    ]
    for pattern, replace in patterns:
        columns = [re.sub(pattern, replace, col) for col in columns]
    return columns


def check_environment_setup():
    """
    Check that necessary files are in place before proceeding.
    """
    logger.info("--" * 20)
    logger.info("[info logger] cwd: %s", os.getcwd())
    assert os.path.isfile(URL_FILE), f"File not found: {URL_FILE}"
    logger.info("[info logger] URL_FILE: %s", URL_FILE)
    logger.info("--" * 20)


def interrogate_api():
    """
    Fetch data from API and save to results file.
    """
    assert os.path.isfile(URL_FILE), f"URL file not found: {URL_FILE}"
    with open(URL_FILE, encoding="utf-8") as file:
        url = json.load(file)
    assert url.get("url") is not None, "URL is missing in file."
    assert url.get("payload") is not None, "Payload is missing in file."

    response = requests.get(url.get("url"), params=url.get("payload"), timeout=5)
    response.raise_for_status()  # This will raise an HTTPError if the HTTP request returned an unsuccessful status code.
    
    data = response.json()
    with open(RESULTS_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def save_postgresdb():
    """
    Save fetched data into PostgreSQL database.
    """
    assert os.path.isfile(RESULTS_FILE), f"Results file not found: {RESULTS_FILE}"
    with open(RESULTS_FILE, encoding="utf-8") as file:
        data = json.load(file)

    df = pd.DataFrame(data["results"])
    new_columns = rename_columns(df.columns)
    df.columns = new_columns
    df = df.astype(str).replace("nan", "")
    df['payload'] = df.apply(lambda row: json.dumps(row.to_dict()), axis=1)
    df = df[['n_dpe', 'payload']]

    db = Database()
    try:
        df.to_sql(name="dpe_logement", con=db.engine, if_exists="append", index=False)
        logger.info("Data successfully stored to the database.")
    except Exception as e:
        logger.error("An error occurred while storing data: %s", e)
    finally:
        db.close()

def drop_duplicates():
    """
    Drop duplicate rows in the PostgreSQL table `dpe_logement`.
    Duplicates are identified based on `n_dpe` and `payload` columns.
    """
    db = Database()
    try:
        with db.engine.connect() as conn:
            # Identify and delete duplicate rows, keeping the first occurrence
            conn.execute(text("""
                DELETE FROM dpe_logement
                WHERE ctid NOT IN (
                    SELECT min(ctid)
                    FROM dpe_logement
                    GROUP BY n_dpe, payload
                );
            """))
            logger.info("Duplicates successfully dropped from the database.")
    except Exception as e:
        logger.error(f"An error occurred while dropping duplicates: {e}")
    finally:
        db.close()


default_args = {
    "depends_on_past": False,
    "email": ["airflow@example.com"],
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    "get_ademe_data",
    default_args=default_args,
    description="Get & store ademe data to database",
    schedule_interval=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["example"],
) as dag:

    check_environment_setup_task = PythonOperator(
        task_id="check_environment_setup",
        python_callable=check_environment_setup,
    )

    interrogate_api_task = PythonOperator(
        task_id="interrogate_api",
        python_callable=interrogate_api,
    )

    store_results_task = PythonOperator(
        task_id="save_postgresdb",
        python_callable=save_postgresdb,
    )
    drop_duplicates_task = PythonOperator(
        task_id="drop_duplicates",
        python_callable=drop_duplicates,
    )


    check_environment_setup_task >> interrogate_api_task >> store_results_task >> drop_duplicates_task
    
