#
# Copyright (c) Granulate. All rights reserved.
# Licensed under the AGPL3 License. See LICENSE.md in the project root for license information.
#

import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional

import requests

from granulate_utils.exceptions import DatabricksJobNameDiscoverException

HOST_KEY_NAME = "*.sink.ganglia.host"
DATABRICKS_METRICS_PROP_PATH = "/databricks/spark/conf/metrics.properties"
CLUSTER_USAGE_ALL_TAGS_PROP = "spark.databricks.clusterUsageTags.clusterAllTags"
CLUSTER_USAGE_CLUSTER_NAME_PROP = "spark.databricks.clusterUsageTags.clusterName"
CLUSTER_USAGE_RELEVANT_TAGS_PROPS = [
    "spark.databricks.clusterUsageTags.cloudProvider",
    "spark.databricks.clusterUsageTags.clusterAvailability",
    "spark.databricks.clusterUsageTags.clusterCreator",
    "spark.databricks.clusterUsageTags.clusterFirstOnDemand",
    "spark.databricks.clusterUsageTags.clusterMaxWorkers",
    "spark.databricks.clusterUsageTags.clusterMinWorkers",
    "spark.databricks.clusterUsageTags.clusterNodeType",
    "spark.databricks.clusterUsageTags.clusterScalingType",
    "spark.databricks.clusterUsageTags.clusterSizeType",
    "spark.databricks.clusterUsageTags.clusterSku",
    "spark.databricks.clusterUsageTags.clusterSpotBidMaxPrice",
    "spark.databricks.clusterUsageTags.clusterTargetWorkers",
    "spark.databricks.clusterUsageTags.clusterWorkers",
    "spark.databricks.clusterUsageTags.driverNodeType",
]
DATABRICKS_REDACTED_STR = "redacted"
SPARKUI_APPS_URL = "http://{}/api/v1/applications"
REQUEST_TIMEOUT = 5
JOB_NAME_KEY = "RunName"
CLUSTER_NAME_KEY = "ClusterName"
DEFAULT_WEBUI_PORT = 40001
DATABRICKS_JOBNAME_TIMEOUT_S = 2 * 60
RETRY_INTERVAL_S = 1
RUN_ID_REGEX = "run-\\d+-"


class DBXWebUIEnvWrapper:
    def __init__(self, logger: logging.LoggerAdapter, enable_retries: bool = True) -> None:
        """
        When `enable_retries` is True, the wrapper will retry the request to the webui until it succeeds or until
        """
        self.logger = logger
        self.enable_retries = enable_retries
        self._apps_url: Optional[str] = None
        self.logger.debug("Getting DBX environment properties")
        self.all_props_dict: Optional[Dict[str, str]] = self.extract_relevant_metadata()
        if self.all_props_dict is None:
            self.logger.warning(
                "DBXWebUIEnvWrapper failed to get relevant metadata, service name will not include metadata from DBX"
            )

    def _request_get(self, url: str) -> requests.Response:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp

    @staticmethod
    def get_webui_address() -> Optional[str]:
        with open(DATABRICKS_METRICS_PROP_PATH) as f:
            properties = f.read()
        try:
            host = dict([line.split("=", 1) for line in properties.splitlines()])[HOST_KEY_NAME]
        except KeyError as e:
            if e.args[0] == HOST_KEY_NAME:
                # Might happen while provisioning the cluster, retry.
                return None
            raise DatabricksJobNameDiscoverException(f"Failed to get Databricks webui address {properties=}") from e
        except Exception as e:
            raise DatabricksJobNameDiscoverException(f"Failed to get Databricks webui address {properties=}") from e
        return f"{host}:{DEFAULT_WEBUI_PORT}"

    def extract_relevant_metadata(self) -> Optional[Dict[str, str]]:
        # Retry in case of a connection error, as the metrics server might not be up yet.
        start_time = time.monotonic()
        while time.monotonic() - start_time < DATABRICKS_JOBNAME_TIMEOUT_S:
            try:
                if cluster_all_props := self._cluster_all_tags_metadata():
                    self.logger.info(
                        "Successfully got relevant cluster tags metadata",
                        cluster_all_props=cluster_all_props,
                    )
                    return cluster_all_props
                else:
                    # No environment metadata yet, retry.
                    time.sleep(RETRY_INTERVAL_S)
            except DatabricksJobNameDiscoverException:
                self.logger.exception("Failed to get DBX environment properties")
                return None
            except Exception:
                self.logger.exception("Generic exception was raise during DBX environment properties discovery")
                return None
            if not self.enable_retries:
                break
        self.logger.info("Databricks get DBX environment metadata timeout, continuing...")
        return None

    def _discover_apps_url(self) -> bool:
        """
        Discovers the SparkUI apps url, and setting it to `self._apps_url`.
        Returns `True` if the url was discovered, `False` otherwise.
        """
        if self._apps_url is not None:  # Checks if the url was already discovered.
            return True
        else:
            if (web_ui_address := self.get_webui_address()) is None:
                return False
            self._apps_url = SPARKUI_APPS_URL.format(web_ui_address)
            self.logger.debug("Databricks SparkUI address", apps_url=self._apps_url)
            return True

    def _spark_apps_json(self) -> Any:
        assert self._apps_url, "SparkUI apps url was not discovered"
        try:
            response = self._request_get(self._apps_url)
        except requests.exceptions.RequestException:
            # Request might fail in cases where the cluster is still initializing, retrying.
            return None
        try:
            apps = response.json()
        except Exception as e:
            if "Spark is starting up. Please wait a while until it's ready" in response.text:
                # Spark is still initializing, retrying.
                # https://github.com/apache/spark/blob/38c41c/core/src/main/scala/org/apache/spark/ui/SparkUI.scala#L64
                return None
            else:
                raise DatabricksJobNameDiscoverException(
                    f"Failed to parse apps url response, query {response.text=}"
                ) from e
        return apps

    def _spark_app_env_json(self, app_id: str) -> Any:
        assert self._apps_url is not None, "SparkUI apps url was not discovered"
        env_url = f"{self._apps_url}/{app_id}/environment"
        try:
            response = self._request_get(env_url)
        except Exception as e:
            # No reason for any exception, `environment` uri should be accessible if we have running apps.
            raise DatabricksJobNameDiscoverException(f"Environment request failed {env_url=}") from e
        try:
            env = response.json()
        except Exception as e:
            raise DatabricksJobNameDiscoverException(f"Environment request failed {response.text=}") from e
        return env

    def _cluster_all_tags_metadata(self) -> Optional[Dict[str, str]]:
        """
        Returns `includes spark.databricks.clusterUsageTags.clusterAllTags` tags as `Dict`.
        In any case this function returns `None`, a retry is required.
        """
        if not os.path.isfile(DATABRICKS_METRICS_PROP_PATH):
            # We want to retry in case the cluster is still initializing, and the file is not yet deployed.
            return None
        # Discovering SparkUI apps url.
        if self._discover_apps_url() is False:
            # SparkUI apps url was not discovered, retrying.
            return None

        # Getting spark apps in JSON format.
        if (apps := self._spark_apps_json()) is None:
            return None
        if len(apps) == 0:
            # apps might be empty because of initialization, retrying.
            self.logger.debug("No apps yet, retrying.")
            return None

        # Extracting for the first app the "sparkProperties" table of the application environment.
        full_spark_app_env = self._spark_app_env_json(apps[0]["id"])
        spark_properties = full_spark_app_env.get("sparkProperties")
        if spark_properties is None:
            raise DatabricksJobNameDiscoverException(f"sparkProperties was not found in {full_spark_app_env=}")

        # Convert from [[key, val], [key, val]] to {key: val, key: val}
        try:
            spark_properties = dict(spark_properties)
        except Exception as e:
            raise DatabricksJobNameDiscoverException(f"Failed to parse as dict {full_spark_app_env=}") from e

        # First, trying to extract `CLUSTER_TAGS_KEY` property, in case not redacted.
        result: Dict[str, str] = {}
        if (
            cluster_all_tags_value := spark_properties.get(CLUSTER_USAGE_ALL_TAGS_PROP)
        ) is not None and DATABRICKS_REDACTED_STR not in cluster_all_tags_value:
            try:
                cluster_all_tags_value_json = json.loads(cluster_all_tags_value)
            except Exception as e:
                raise DatabricksJobNameDiscoverException(f"Failed to parse {cluster_all_tags_value}") from e

            result.update(
                {cluster_all_tag["key"]: cluster_all_tag["value"] for cluster_all_tag in cluster_all_tags_value_json}
            )
        # As a fallback, trying to extract `CLUSTER_USAGE_CLUSTER_NAME_PROP` property.
        elif (cluster_name_value := spark_properties.get(CLUSTER_USAGE_CLUSTER_NAME_PROP)) is not None:
            result[CLUSTER_NAME_KEY] = cluster_name_value

        else:
            # We expect at least one of the properties to be present.
            raise DatabricksJobNameDiscoverException(
                f"Failed to extract {CLUSTER_USAGE_ALL_TAGS_PROP} or "
                f"{CLUSTER_USAGE_CLUSTER_NAME_PROP} from {spark_properties=}"
            )

        # Now add additional intereseting data to the metadata
        for key in spark_properties:
            if key in CLUSTER_USAGE_RELEVANT_TAGS_PROPS:
                val = spark_properties[key]
                if DATABRICKS_REDACTED_STR not in val:
                    result[key] = val

        return self._apply_pattern(result)

    @staticmethod
    def _apply_pattern(metadata: Dict[str, str]) -> Dict[str, str]:
        """
        Applies certain patterns on the metadata values.
        We mostly use the metadata values as service names, so we want to make sure the metadata values
        match some service name requirements.

        e.g.: Job Name might include spaces, we want to replace them with dashes.
        """
        if JOB_NAME_KEY in metadata:
            metadata[JOB_NAME_KEY] = metadata[JOB_NAME_KEY].replace(" ", "-").lower()
        if CLUSTER_NAME_KEY in metadata:
            # We've tackled cases where the cluster name includes Run ID, we want to remove it.
            metadata[CLUSTER_NAME_KEY] = re.sub(RUN_ID_REGEX, "", metadata[CLUSTER_NAME_KEY])
            metadata[CLUSTER_NAME_KEY] = metadata[CLUSTER_NAME_KEY].replace(" ", "-").lower()
        return metadata


def get_name_from_metadata(metadata: Dict[str, str]) -> Optional[str]:
    assert metadata is not None, "all_props_dict is None, can't get name from metadata"
    if job_name := metadata.get(JOB_NAME_KEY):
        return f"job-{job_name}"
    elif cluster_name := metadata.get(CLUSTER_NAME_KEY):
        return cluster_name
    return None
