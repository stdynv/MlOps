from __future__ import annotations
import os
import re
import json
import time
from datetime import datetime, timedelta
import logging
from urllib.parse import urlparse, parse_qs
import typing as t
import requests
import pandas as pd
from airflow.models.dag import DAG
from airflow.operators.python import PythonOperator
from airflow.models import Variable
from db_utils import Database

API_PATH = "../data/api"


URL_FILE = os.path.join(API_PATH, "url.json")
RESULTS_FILE = os.path.join(API_PATH, "results.json")
print(URL_FILE, RESULTS_FILE)
logger = logging.getLogger(__name__)


def rename_columns(columns: t.List[str]) -> t.List[str]:
    columns = [col.lower() for col in columns]

    rgxs = [
        (r"[°|/|']", "_"),
        (r"²", "2"),
        (r"[(|)]", ""),
        (r"é|è", "e"),
        (r"â", "a"),
        (r"^_", "dpe_"),
        (r"_+", "_"),
    ]
    for rgx in rgxs:
        columns = [re.sub(rgx[0], rgx[1], col) for col in columns]

    return columns


def check_environment_setup(*op_args):
    logger.info("--" * 20)
    logger.info(f"[info logger] cwd: {os.getcwd()}")
    logger.info(f"[info logger] URL_FILE: {URL_FILE}")
    assert os.path.isfile(URL_FILE)
    logger.info(f"[info logger] RESULTS_FILE: {RESULTS_FILE}")

    container_client_ = op_args[0]
    logger.info(f"[info logger] assert container_client exists \n{container_client_.exists()}")
    assert container_client_.exists()

    try:
        pg_password = Variable.get("AZURE_PG_PASSWORD")
    except:
        pg_password = os.environ.get("AZURE_PG_PASSWORD")

    assert pg_password is not None

    logger.info("--" * 20)


def ademe_api():
    """
    Interrogates the ADEME API using the specified URL and payload from a JSON file.

    - Reads the URL and payload from a JSON file defined by the constant `URL_FILE`.
    - Performs a GET request to the obtained URL with the given payload.
    - Saves the results to a JSON file defined by the constant `RESULTS_FILE`.

    Raises:
        AssertionError: If the URL file does not exist, or if the retrieved URL or payload is None.
        requests.exceptions.RequestException: If the GET request encounters an error.

    """

    # test url file exists
    assert os.path.isfile(URL_FILE)
    # open url file
    with open(URL_FILE, encoding="utf-8") as file:
        url = json.load(file)
    assert url.get("url") is not None
    assert url.get("payload") is not None

    # make GET requests
    results = requests.get(url.get("url"), params=url.get("payload"), timeout=5)
    assert results.raise_for_status() is None

    data = results.json()

    # save results to file
    with open(RESULTS_FILE, "w") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def save_postgresdb():
    assert os.path.isfile(RESULTS_FILE)

    # read previous API call output
    with open(RESULTS_FILE, encoding="utf-8") as file:
        data = json.load(file)

    data = pd.DataFrame(data["results"])

    # set columns
    new_columns = rename_columns(data.columns)
    data.columns = new_columns
    data = data.astype(str).replace("nan", "")

    # now check that the data does not have columns not already in the table
    db = Database()
    check_cols_query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
w        AND table_name   = 'dpe_tertiaire';
    """
    table_cols = pd.read_sql(check_cols_query, con=db.engine)
    table_cols = [col for col in table_cols["column_name"] if col != "id"]

    # drop data columns not in table_cols
    for col in data.columns:
        if col not in table_cols:
            data.drop(columns=[col], inplace=True)
            logger.info(f"dropped column {col} from dataset")

    # add empty columns in data that are in the table
    for col in table_cols:
        if col not in data.columns:
            if col in ["created_at", "modified_at"]:
                data[col] = datetime.now()
            else:
                data[col] = ""
            logger.info(f"added column {col} in data")

    # data = data[table_cols].copy()
    assert sorted(data.columns) == sorted(table_cols)

    logger.info(f"loaded {data.shape}")

    # to_sql
    data.to_sql(name="dpe_tertiaire", con=db.engine, if_exists="append", index=False)
    db.close()


def drop_duplicates():
    query = """
        DELETE FROM dpe_tertiaire
        WHERE id IN (
        SELECT id
        FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY n_dpe ORDER BY id DESC) AS rn
            FROM dpe_tertiaire
        ) t
        WHERE t.rn > 1
        );
    """

    db = Database()
    db.execute(query)
    db.close()


with DAG(
    "ademe_data",
    default_args={
        "depends_on_past": False,
        "email": ["yassinemed.essamadi@gmail.com"],
        "email_on_failure": True,
        "email_on_retry": False,
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    description="Get data from ADEME API, save to postgres and in azure bucket, then clean up",
    schedule="*/5 * * * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["ademe"],
) as dag:
    """check_environment_setup = PythonOperator(
        task_id="check_environment_setup",
        python_callable=check_environment_setup,
    )"""

    ademe_api = PythonOperator(
        task_id="ademe_api",
        python_callable=ademe_api,
    )

    """process_results = PythonOperator(
        task_id="process_results",
        python_callable=process_results,
    )"""

    """save_postgresdb = PythonOperator(
        task_id="save_postgresdb",
        python_callable=save_postgresdb,
    )"""

    # check_environment_setup.set_downstream(ademe_api)
    # save to postgres
    # check_environment_setup >> ademe_api >> process_results >> save_postgresdb >> drop_duplicates >>
