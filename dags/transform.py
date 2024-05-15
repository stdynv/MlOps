import json
import logging
from datetime import datetime, timedelta
import pandas as pd


from airflow.models.dag import DAG
from airflow.operators.python import PythonOperator
from db_utils import Database

logger = logging.getLogger(__name__)

input_columns = [
    "etiquette_dpe",
    "etiquette_ges",
    "version_dpe",
    "type_energie_principale_chauffage",
    "type_energie_n_1",
]

def transform():
    """
    Function to transform data from the dpe_logement table and store it in the dpe_training table.
    """
    db = Database()
    query = "SELECT * FROM dpe_logement"
    data = pd.read_sql(query, con=db.engine)

    # Displaying the first few rows of the fetched data
    logger.info(data.head())

    new_rows = []
    # Iterate through the rows of the DataFrame
    for _, row in data.iterrows():
        # Ensure that the payload is a dictionary
        payload = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']

        # Creating a dictionary for the new entries
        filtered_payload = {key: payload[key] for key in input_columns if key in payload}

        # Adding the new line to the list
        new_rows.append({'n_dpe': row['n_dpe'], 'payload': json.dumps(filtered_payload)})

    df_new = pd.DataFrame(new_rows)

    # Store the new data in another table
    df_new.to_sql('dpe_training', con=db.engine, if_exists='replace', index=False)
    db.close()


def drop_duplicates():
    query = """
        DELETE FROM dpe_training
        WHERE n_dpe IN (
        SELECT n_dpe
        FROM (
            SELECT n_dpe, ROW_NUMBER() OVER (PARTITION BY n_dpe ORDER BY id DESC) AS rn
            FROM dpe_training
        ) t
        WHERE t.rn > 1
        );
    """

    db = Database()
    db.execute(query)
    db.close()

# DAG definition
with DAG(
    "ademe_transform_data",
    default_args={
        "owner": "airflow",
        "depends_on_past": False,
        "email_on_failure": False,
        "email_on_retry": False,
        "email": ["your-email@example.com"],
        "retries": 0,
        "retry_delay": timedelta(minutes=10),
    },
    description="Transform raw data from dpe_logement, and store into dpe_training",
    schedule_interval="*/3 * * * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["ademe"],
) as dag:
    transform_data_task = PythonOperator(
        task_id="transform_data_task",
        python_callable=transform
    )
    drop_duplicates_task = PythonOperator(
        task_id="drop_duplicates_task", python_callable=drop_duplicates
    )

    transform_data_task >> drop_duplicates_task
