#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# pytype: skip-file

import logging
import re
import time
from typing import Optional
from typing import Tuple

from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.runners.interactive.dataproc.types import ClusterMetadata
from apache_beam.runners.interactive.utils import progress_indicated

try:
  from google.cloud import dataproc_v1
  from apache_beam.io.gcp import gcsfilesystem  #pylint: disable=ungrouped-imports
except ImportError:

  class UnimportedDataproc:
    Cluster = None

  dataproc_v1 = UnimportedDataproc()

_LOGGER = logging.getLogger(__name__)

DATAPROC_STAGING_LOG_NAME = 'dataproc-startup-script_output'


class DataprocClusterManager:
  """Self-contained cluster manager that controls the lifecyle of a Dataproc
  cluster connected by one or more pipelines under Interactive Beam.
  """
  def __init__(self, cluster_metadata: ClusterMetadata) -> None:
    """Initializes the DataprocClusterManager with properties required
    to interface with the Dataproc ClusterControllerClient.
    """
    self.cluster_metadata = cluster_metadata
    # Pipelines whose jobs are executed on the cluster.
    self.pipelines = set()
    self._cluster_client = dataproc_v1.ClusterControllerClient(
        client_options={
            'api_endpoint': \
            f'{self.cluster_metadata.region}-dataproc.googleapis.com:443'
        })
    self._fs = gcsfilesystem.GCSFileSystem(PipelineOptions())
    self._staging_directory = None

  @progress_indicated
  def create_cluster(self, cluster: dict) -> None:
    """Attempts to create a cluster using attributes that were
    initialized with the DataprocClusterManager instance.

    Args:
      cluster: Dictionary representing Dataproc cluster. Read more about the
          schema for clusters here:
          https://cloud.google.com/python/docs/reference/dataproc/latest/google.cloud.dataproc_v1.types.Cluster
    """
    if self.cluster_metadata.master_url:
      return
    try:
      self._cluster_client.create_cluster(
          request={
              'project_id': self.cluster_metadata.project_id,
              'region': self.cluster_metadata.region,
              'cluster': cluster
          })
    except Exception as e:
      if e.code == 409:
        _LOGGER.info(
            'Cluster %s already exists. Continuing...',
            self.cluster_metadata.cluster_name)
      elif e.code == 403:
        _LOGGER.error(
            'Due to insufficient project permissions, '
            'unable to create cluster: %s',
            self.cluster_metadata.cluster_name)
        raise ValueError(
            'You cannot create a cluster in project: {}'.format(
                self.cluster_metadata.project_id))
      elif e.code == 501:
        _LOGGER.error(
            'Invalid region provided: %s', self.cluster_metadata.region)
        raise ValueError(
            'Region {} does not exist!'.format(self.cluster_metadata.region))
      else:
        _LOGGER.error(
            'Unable to create cluster: %s', self.cluster_metadata.cluster_name)
        raise e
    else:
      _LOGGER.info(
          'Cluster created successfully: %s',
          self.cluster_metadata.cluster_name)
      self._staging_directory = self.get_staging_location()
      master_url, dashboard = self.get_master_url_and_dashboard()
      self.cluster_metadata.master_url = master_url
      self.cluster_metadata.dashboard = dashboard

  def create_flink_cluster(self) -> None:
    """Calls _create_cluster with a configuration that enables FlinkRunner."""
    cluster = {
        'project_id': self.cluster_metadata.project_id,
        'cluster_name': self.cluster_metadata.cluster_name,
        'config': {
            'software_config': {
                # TODO(BEAM-14142): Uncomment these lines when a Dataproc
                # image is released with previously missing dependencies.
                # 'image_version': ie.current_env().clusters.
                # DATAPROC_IMAGE_VERSION,
                'optional_components': ['DOCKER', 'FLINK']
            },
            'gce_cluster_config': {
                'metadata': {
                    'flink-start-yarn-session': 'true'
                },
                'service_account_scopes': [
                    'https://www.googleapis.com/auth/cloud-platform'
                ]
            },
            'endpoint_config': {
                'enable_http_port_access': True
            }
        },
        'labels': {
            'domain': 'goog-dataflow-notebook'
        }
    }
    self.create_cluster(cluster)

  def cleanup(self) -> None:
    """Deletes the cluster that uses the attributes initialized
    with the DataprocClusterManager instance."""
    try:
      self._cluster_client.delete_cluster(
          request={
              'project_id': self.cluster_metadata.project_id,
              'region': self.cluster_metadata.region,
              'cluster_name': self.cluster_metadata.cluster_name,
          })
      self.cleanup_staging_files()
    except Exception as e:
      if e.code == 403:
        _LOGGER.error(
            'Due to insufficient project permissions, '
            'unable to clean up the default cluster: %s',
            self.cluster_metadata.cluster_name)
        raise ValueError(
            'You cannot delete a cluster in project: {}'.format(
                self.cluster_metadata.project_id))
      elif e.code == 404:
        _LOGGER.error(
            'Cluster does not exist: %s', self.cluster_metadata.cluster_name)
        raise ValueError(
            'Cluster was not found: {}'.format(
                self.cluster_metadata.cluster_name))
      else:
        _LOGGER.error(
            'Failed to delete cluster: %s', self.cluster_metadata.cluster_name)
        raise e

  def get_cluster_details(self) -> dataproc_v1.Cluster:
    """Gets the Dataproc_v1 Cluster object for the current cluster manager."""
    try:
      return self._cluster_client.get_cluster(
          request={
              'project_id': self.cluster_metadata.project_id,
              'region': self.cluster_metadata.region,
              'cluster_name': self.cluster_metadata.cluster_name
          })
    except Exception as e:
      if e.code == 403:
        _LOGGER.error(
            'Due to insufficient project permissions, '
            'unable to retrieve information for cluster: %s',
            self.cluster_metadata.cluster_name)
        raise ValueError(
            'You cannot view clusters in project: {}'.format(
                self.cluster_metadata.project_id))
      elif e.code == 404:
        _LOGGER.error(
            'Cluster does not exist: %s', self.cluster_metadata.cluster_name)
        raise ValueError(
            'Cluster was not found: {}'.format(
                self.cluster_metadata.cluster_name))
      else:
        _LOGGER.error(
            'Failed to get information for cluster: %s',
            self.cluster_metadata.cluster_name)
        raise e

  def wait_for_cluster_to_provision(self) -> None:
    while self.get_cluster_details().status.state.name == 'CREATING':
      time.sleep(15)

  def get_staging_location(self) -> str:
    """Gets the staging bucket of an existing Dataproc cluster."""
    try:
      self.wait_for_cluster_to_provision()
      cluster_details = self.get_cluster_details()
      bucket_name = cluster_details.config.config_bucket
      gcs_path = 'gs://' + bucket_name + '/google-cloud-dataproc-metainfo/'
      for file in self._fs._list(gcs_path):
        if self.cluster_metadata.cluster_name in file.path:
          # this file path split will look something like:
          # ['gs://.../google-cloud-dataproc-metainfo/{staging_dir}/',
          # '-{node-type}/dataproc-startup-script_output']
          return file.path.split(self.cluster_metadata.cluster_name)[0]
    except Exception as e:
      _LOGGER.error(
          'Failed to get %s cluster staging bucket.',
          self.cluster_metadata.cluster_name)
      raise e

  def parse_master_url_and_dashboard(self, line: str) -> Tuple[str, str]:
    """Parses the master_url and YARN application_id of the Flink process from
    an input line. The line containing both the master_url and application id
    is always formatted as such:
    {text} Found Web Interface {master_url} of application
    '{application_id}'.\\n

    Truncated example where '...' represents additional text between segments:
    ... google-dataproc-startup[000]: ... activate-component-flink[0000]:
    ...org.apache.flink.yarn.YarnClusterDescriptor... [] -
    Found Web Interface example-master-url:50000 of application
    'application_123456789000_0001'.

    Returns the flink_master_url and dashboard link as a tuple."""
    cluster_details = self.get_cluster_details()
    yarn_endpoint = cluster_details.config.endpoint_config.http_ports[
        'YARN ResourceManager']
    segment = line.split('Found Web Interface ')[1].split(' of application ')
    master_url = segment[0]
    application_id = re.sub('\'|.\n', '', segment[1])
    dashboard = re.sub(
        '/yarn/',
        '/gateway/default/yarn/proxy/' + application_id + '/',
        yarn_endpoint)
    return master_url, dashboard

  def get_master_url_and_dashboard(self) -> Tuple[Optional[str], Optional[str]]:
    """Returns the master_url of the current cluster."""
    startup_logs = []
    for file in self._fs._list(self._staging_directory):
      if DATAPROC_STAGING_LOG_NAME in file.path:
        startup_logs.append(file.path)

    for log in startup_logs:
      content = self._fs.open(log)
      for line in content.readlines():
        decoded_line = line.decode()
        if 'Found Web Interface' in decoded_line:
          return self.parse_master_url_and_dashboard(decoded_line)
    return None, None

  def cleanup_staging_files(self) -> None:
    if self._staging_directory:
      staging_files = [
          file.path for file in self._fs._list(self._staging_directory)
      ]
      self._fs.delete(staging_files)
