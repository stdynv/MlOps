"""
Training the model
"""
import json
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from sklearn.metrics import precision_score, recall_score
from sklearn.preprocessing import LabelEncoder  # convert categorical to numerical
from sklearn.model_selection import train_test_split, GridSearchCV, KFold
from sklearn.ensemble import RandomForestClassifier


import mlflow

import logging

from airflow.models.dag import DAG
from airflow.operators.python import PythonOperator

# local
from db_utils import Database

import random

logger = logging.getLogger(__name__)


# --------------------------------------------------
# TrainDPE class
# --------------------------------------------------


class NotEnoughSamples(ValueError):
    pass


train_columns = ["version_dpe", "type_energie_principale_chauffage", "type_energie_n_1"]


class TrainDPE:
    param_grid = {
        "n_estimators": sorted([random.randint(1, 20) * 10 for _ in range(2)]),
        "max_depth": [random.randint(3, 10)],
        "min_samples_leaf": [random.randint(2, 5)],
    }

    n_splits = 3
    test_size = 0.3
    minimum_training_samples = 200

    def __init__(self, data, target="etiquette_ges"):
        # drop samples with no target
        data = data[data[target] >= 0].copy()
        data.reset_index(inplace=True, drop=True)
        if data.shape[0] < TrainDPE.minimum_training_samples:
            raise NotEnoughSamples(
                "data has {data.shape[0]} samples, which is not enough to train a model. min required {TrainDPE.minimum_training_samples}"
            )

        self.data = data
        print(f"training on {data.shape[0]} samples")

        self.model = RandomForestClassifier()
        self.target = target
        self.params = {}
        self.train_score = 0.0

        self.precision_score = 0.0
        self.recall_score = 0.0
        self.probabilities = [0.0, 0.0]

    def main(self):
        # shuffle

        X = self.data[train_columns].copy()  # Features
        y = self.data[self.target].copy()  # Target variable

        # Split the data into training and testing sets
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=TrainDPE.test_size, random_state=808
        )

        # Setup GridSearchCV with k-fold cross-validation
        cv = KFold(n_splits=TrainDPE.n_splits, random_state=42, shuffle=True)

        grid_search = GridSearchCV(
            estimator=self.model, param_grid=TrainDPE.param_grid, cv=cv, scoring="accuracy"
        )

        # Fit the model
        grid_search.fit(X_train, y_train)

        self.model = grid_search.best_estimator_
        self.params = grid_search.best_params_
        self.train_score = grid_search.best_score_

        yhat = grid_search.predict(X_test)
        self.precision_score = precision_score(y_test, yhat, average="weighted")
        self.recall_score = recall_score(y_test, yhat, average="weighted")
        self.probabilities = np.max(grid_search.predict_proba(X_test), axis=1)

    def report(self):
        # Best parameters and best score
        print("--" * 20, "Best model")
        print(f"\tparameters: {self.params}")
        print(f"\tcross-validation score: {self.train_score}")
        print(f"\tmodel: {self.model}")
        print("--" * 20, "performance")
        print(f"\tprecision_score: {np.round(self.precision_score, 2)}")
        print(f"\trecall_score: {np.round(self.recall_score, 2)}")
        print(f"\tmedian(probabilities): {np.round(np.median(self.probabilities), 2)}")
        print(f"\tstd(probabilities): {np.round(np.std(self.probabilities), 2)}")


# --------------------------------------------------
# set up MLflow
# --------------------------------------------------
from mlflow import MlflowClient

experiment_name = "dpe_logement"

# mlflow.set_tracking_uri("http://host.docker.internal:5001")
# mlflow.set_tracking_uri("http://localhost:9090")

mlflow.set_tracking_uri("http://mlflow:5000")


print("--" * 40)
print("mlflow set experiment")
print("--" * 40)
mlflow.set_experiment(experiment_name)

mlflow.sklearn.autolog()

# --------------------------------------------------
# load data
# --------------------------------------------------


def load_data_for_inference(n_samples):
    db = Database()
    query = f"select * from dpe_training limit {n_samples}"
    df = pd.read_sql(query, con=db.engine)
    db.close()
    # dump payload into new dataframe
    df["payload"] = df["payload"].apply(lambda d: json.loads(d))
    data = pd.DataFrame(list(df.payload.values))
    # data = data.astype(int)
    le = LabelEncoder()

    # Apply LabelEncoder to each column in the DataFrame
    for col in data.columns:
        data[col] = le.fit_transform(data[col])

    data.reset_index(inplace=True, drop=True)
    y = data["etiquette_ges"]
    X = data[train_columns]

    return X, y


def load_data_for_training(n_samples):
    # TODO simply load payload not all columns
    db = Database()
    query = f"select * from dpe_training limit {n_samples}"
    df = pd.read_sql(query, con=db.engine)
    db.close()
    # dump payload into new dataframe
    df["payload"] = df["payload"].apply(lambda d: json.loads(d))
    data = pd.DataFrame(list(df.payload.values))
    # data = data.astype(int)
    le = LabelEncoder()

    # Apply LabelEncoder to each column in the DataFrame
    for col in data.columns:
        data[col] = le.fit_transform(data[col])

    data.reset_index(inplace=True, drop=True)
    print(data.head())
    return data


# ---------------------------------------------
#  tasks
# ---------------------------------------------
challenger_model_name = "dpe_challenger"
champion_model_name = "dpe_champion"
client = MlflowClient()


def train_model():
    data = load_data_for_training(n_samples=200)
    with mlflow.start_run() as run:
        train = TrainDPE(data)
        train.main()
        train.report()

        try:
            model = client.get_registered_model(challenger_model_name)
        except:
            print("model does not exist")
            print("registering new model", challenger_model_name)
            client.create_registered_model(
                challenger_model_name, description="sklearn random forest for dpe_logement"
            )

        # set version and stage
        run_id = run.info.run_id
        model_uri = f"runs:/{run_id}/model"
        model_version = client.create_model_version(
            name=challenger_model_name, source=model_uri, run_id=run_id
        )

        client.transition_model_version_stage(
            name=challenger_model_name, version=model_version.version, stage="Staging"
        )


def create_champion():
    """
    if there is not champion yet, creates a champion from current challenger
    """
    results = client.search_registered_models(filter_string=f"name='{champion_model_name}'")
    # if not exists: promote current model
    if len(results) == 0:
        print("champion model not found, promoting challenger to champion")

        champion_model = client.copy_model_version(
            src_model_uri=f"models:/{challenger_model_name}/Staging",
            dst_name=champion_model_name,
        )
        client.transition_model_version_stage(
            name=champion_model_name, version=champion_model.version, stage="Staging"
        )

        # reload champion and print info
        results = client.search_registered_models(filter_string=f"name='{champion_model_name}'")
        print(results[0].latest_versions)


def promote_model():
    X, y = load_data_for_inference(200)
    # inference challenger and champion
    # load model & inference
    chl = mlflow.sklearn.load_model(f"models:/{challenger_model_name}/Staging")
    yhat = chl.best_estimator_.predict(X)
    challenger_precision = precision_score(y, yhat, average="weighted")
    challenger_recall = recall_score(y, yhat, average="weighted")
    print(f"\t challenger_precision: {np.round(challenger_precision, 2)}")
    print(f"\t challenger_recall: {np.round(challenger_recall, 2)}")

    # inference on production model
    champ = mlflow.sklearn.load_model(f"models:/{champion_model_name}/Staging")
    yhat = champ.best_estimator_.predict(X)
    champion_precision = precision_score(y, yhat, average="weighted")
    champion_recall = recall_score(y, yhat, average="weighted")
    print(f"\t champion_precision: {np.round(champion_precision, 2)}")
    print(f"\t champion_recall: {np.round(champion_recall, 2)}")

    # if performance 5% above current champion: promote
    if challenger_precision > champion_precision:
        print(f"{challenger_precision} > {champion_precision}")
        print("Promoting new model to champion ")
        champion_model = client.copy_model_version(
            src_model_uri=f"models:/{challenger_model_name}/Staging",
            dst_name=champion_model_name,
        )

        client.transition_model_version_stage(
            name=champion_model_name, version=champion_model.version, stage="Staging"
        )
    else:
        print(f"{challenger_precision} < {champion_precision}")
        print("champion remains undefeated ")


# ---------------------------------------------
#  DAG
# ---------------------------------------------
with DAG(
    "ademe_models",
    default_args={
        "retries": 0,
        "retry_delay": timedelta(minutes=10),
    },
    description="Model training and promotion",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["ademe"],
) as dag:
    train_model_task = PythonOperator(task_id="train_model_task", python_callable=train_model)

    create_champion_task = PythonOperator(
        task_id="create_champion_task", python_callable=create_champion
    )

    promote_model_task = PythonOperator(task_id="promote_model_task", python_callable=promote_model)

    train_model_task >> create_champion_task >> promote_model_task
