#!/usr/bin/env python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2019             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# tails. You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.
"""
Special agent for monitoring Kubernetes clusters.
"""

from __future__ import (
    absolute_import,
    division,
    print_function,
)

import argparse
from collections import OrderedDict, MutableSequence, defaultdict
import contextlib
import functools
import itertools
import json
import logging
import operator
import os
import sys
import time
from typing import (  # pylint: disable=unused-import
    Any, Dict, Generic, List, Mapping, Optional, TypeVar, Union,
)
import urllib3  # type: ignore

from dateutil.parser import parse as parse_time
# We currently have no typeshed for kubernetes
from kubernetes import client  # type: ignore
from kubernetes.client.rest import ApiException  # type: ignore

import cmk.utils.profile
import cmk.utils.password_store


@contextlib.contextmanager
def suppress(*exc):
    # This is contextlib.suppress from Python 3.2
    try:
        yield
    except exc:
        pass


class PathPrefixAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if not values:
            return ''
        path_prefix = '/' + values.strip('/')
        setattr(namespace, self.dest, path_prefix)


def parse(args):
    # type: (List[str]) -> argparse.Namespace
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--debug', action='store_true', help='Debug mode: raise Python exceptions')
    p.add_argument('-v',
                   '--verbose',
                   action='count',
                   default=0,
                   help='Verbose mode (for even more output use -vvv)')
    p.add_argument('host', metavar='HOST', help='Kubernetes host to connect to')
    p.add_argument('--port', type=int, default=443, help='Port to connect to')
    p.add_argument('--token', required=True, help='Token for that user')
    p.add_argument(
        '--infos',
        type=lambda x: x.split(','),
        required=True,
        help='Comma separated list of items that should be fetched',
    )
    p.add_argument('--url-prefix', help='Custom URL prefix for Kubernetes API calls')
    p.add_argument('--path-prefix',
                   default='',
                   action=PathPrefixAction,
                   help='Optional URL path prefix to prepend to Kubernetes API calls')
    p.add_argument('--no-cert-check', action='store_true', help='Disable certificate verification')
    p.add_argument('--profile',
                   metavar='FILE',
                   help='Profile the performance of the agent and write the output to a file')

    arguments = p.parse_args(args)
    return arguments


def setup_logging(verbosity):
    # type: (int) -> None
    if verbosity >= 3:
        lvl = logging.DEBUG
    elif verbosity == 2:
        lvl = logging.INFO
    elif verbosity == 1:
        lvl = logging.WARN
    else:
        logging.disable(logging.CRITICAL)
        lvl = logging.CRITICAL
    logging.basicConfig(level=lvl, format='%(asctime)s %(levelname)s %(message)s')


def parse_frac_prefix(value):
    # type: (str) -> float
    if value.endswith('m'):
        return 0.001 * float(value[:-1])
    return float(value)


def parse_memory(value):
    # type: (str) -> float
    if value.endswith('Ki'):
        return 1024**1 * float(value[:-2])
    if value.endswith('Mi'):
        return 1024**2 * float(value[:-2])
    if value.endswith('Gi'):
        return 1024**3 * float(value[:-2])
    if value.endswith('Ti'):
        return 1024**4 * float(value[:-2])
    if value.endswith('Pi'):
        return 1024**5 * float(value[:-2])
    if value.endswith('Ei'):
        return 1024**6 * float(value[:-2])

    if value.endswith('K') or value.endswith('k'):
        return 1e3 * float(value[:-1])
    if value.endswith('M'):
        return 1e6 * float(value[:-1])
    if value.endswith('G'):
        return 1e9 * float(value[:-1])
    if value.endswith('T'):
        return 1e12 * float(value[:-1])
    if value.endswith('P'):
        return 1e15 * float(value[:-1])
    if value.endswith('E'):
        return 1e18 * float(value[:-1])

    # millibytes are a useless, but valid option:
    # https://github.com/kubernetes/kubernetes/issues/28741
    if value.endswith('m'):
        return 1e-3 * float(value[:-1])

    return float(value)


def left_join_dicts(initial, new, operation):
    d = {}
    for key, value in initial.iteritems():
        if isinstance(value, dict):
            d[key] = left_join_dicts(value, new.get(key, {}), operation)
        else:
            if key in new:
                d[key] = operation(value, new[key])
            else:
                d[key] = value
    return d


class Metadata(object):
    def __init__(self, metadata):
        # type: (Optional[client.V1ObjectMeta]) -> None
        if metadata:
            self.name = metadata.name
            self.namespace = metadata.namespace
            self.creation_timestamp = (time.mktime(metadata.creation_timestamp.utctimetuple())
                                       if metadata.creation_timestamp else None)
            self.labels = metadata.labels if metadata.labels else {}
        else:
            self.name = None
            self.namespace = None
            self.creation_timestamp = None
            self.labels = {}

    def matches(self, selectors):
        if not selectors:
            return False

        for name, value in selectors.iteritems():
            if name not in self.labels or self.labels[name] != value:
                return False
        return True


class Node(Metadata):
    def __init__(self, node, stats):
        # type: (client.V1Node, str) -> None
        super(Node, self).__init__(node.metadata)
        self._status = node.status
        # kubelet replies statistics for the last 2 minutes with 10s
        # intervals. We only need the latest state.
        self.stats = eval(stats)['stats'][-1]
        # The timestamps are returned in RFC3339Nano format which cannot be parsed
        # by Pythons time module. Therefore we use dateutils parse function here.
        self.stats['timestamp'] = time.mktime(parse_time(self.stats['timestamp']).utctimetuple())

    @property
    def conditions(self):
        # type: () -> Optional[Dict[str, str]]
        if not self._status:
            return None
        conditions = self._status.conditions
        if not conditions:
            return None
        return {c.type: c.status for c in conditions}

    @staticmethod
    def zero_resources():
        return {
            'capacity': {
                'cpu': 0.0,
                'memory': 0.0,
                'pods': 0,
            },
            'allocatable': {
                'cpu': 0.0,
                'memory': 0.0,
                'pods': 0,
            },
        }

    @property
    def resources(self):
        # type: () -> Dict[str, Dict[str, float]]
        view = self.zero_resources()
        if not self._status:
            return view
        capacity, allocatable = self._status.capacity, self._status.allocatable
        if capacity:
            view['capacity']['cpu'] += parse_frac_prefix(capacity.get('cpu', '0.0'))
            view['capacity']['memory'] += parse_memory(capacity.get('memory', '0.0'))
            view['capacity']['pods'] += int(capacity.get('pods', '0'))
        if allocatable:
            view['allocatable']['cpu'] += parse_frac_prefix(allocatable.get('cpu', '0.0'))
            view['allocatable']['memory'] += parse_memory(allocatable.get('memory', '0.0'))
            view['allocatable']['pods'] += int(allocatable.get('pods', '0'))
        return view


class ComponentStatus(Metadata):
    def __init__(self, status):
        # type: (client.V1ComponentStatus) -> None
        super(ComponentStatus, self).__init__(status.metadata)
        self._conditions = status.conditions

    @property
    def conditions(self):
        # type: () -> List[Dict[str, str]]
        if not self._conditions:
            return []
        return [{'type': c.type, 'status': c.status} for c in self._conditions]


class Service(Metadata):
    def __init__(self, service):
        super(Service, self).__init__(service.metadata)

        spec = service.spec
        if spec:
            # For details refer to:
            # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1ServiceSpec.md

            # type may be: ExternalName, ClusterIP, NodePort, and LoadBalancer
            self._type = spec.type
            self._selector = spec.selector if spec.selector else {}
            # cluster_ip may be: None (headless service), "" (no IP), or a str (valid IP)
            self._cluster_ip = spec.cluster_ip
            # only applies to type LoadBalancer
            self._load_balancer_ip = spec.load_balancer_ip
            self._ports = spec.ports if spec.ports else []
        else:
            self._type_ = None
            self._selector = {}
            self._cluster_ip = ""
            self._load_balancer_ip = ""
            self._ports = []

    @property
    def info(self):
        return {
            'type': self._type,
            'cluster_ip': self._cluster_ip,
            'load_balancer_ip': self._load_balancer_ip,
        }

    @property
    def selector(self):
        return self._selector

    @property
    def ports(self):
        # port is the only field that is not optional
        return {
            port.name if port.name else port.port: {
                'port': port.port,
                'name': port.name,
                'protocol': port.protocol,
                'target_port': port.target_port,
                'node_port': port.node_port,
            } for port in self._ports
        }


class Deployment(Metadata):
    # TODO: include pods of the deployment?
    def __init__(self, deployment):
        # type: (client.V1Deployment) -> None
        super(Deployment, self).__init__(deployment.metadata)
        spec = deployment.spec
        if spec:
            self._paused = spec.paused

            strategy = spec.strategy
            if strategy:
                self._strategy_type = strategy.type
                rolling_update = strategy.rolling_update
                if rolling_update:
                    self._max_surge = rolling_update.max_surge
                    self._max_unavailable = rolling_update.max_unavailable
                else:
                    self._max_surge = None
                    self._max_unavailable = None
            else:
                self._strategy_type = None
                self._max_surge = None
                self._max_unavailable = None
        else:
            self._paused = None
            self._strategy_type = None
            self._max_surge = None
            self._max_unavailable = None

        status = deployment.status
        if status:
            self._ready_replicas = status.ready_replicas
            self._replicas = status.replicas
        else:
            self._ready_replicas = None
            self._replicas = None

    @property
    def replicas(self):
        return {
            'paused': self._paused,
            'ready_replicas': self._ready_replicas,
            'replicas': self._replicas,
            'strategy_type': self._strategy_type,
            'max_surge': self._max_surge,
            'max_unavailable': self._max_unavailable,
        }


class Ingress(Metadata):
    def __init__(self, ingress):
        super(Ingress, self).__init__(ingress.metadata)
        self._backends = []  # list of (path, service_name, service_port)
        self._hosts = defaultdict(list)  # secret -> list of hosts
        self._load_balancers = []

        spec = ingress.spec
        if spec:
            if spec.backend:
                self._backends.append(
                    ("(default)", spec.backend.service_name, spec.backend.service_port))
            for rule in spec.rules if spec.rules else ():
                if rule.http:
                    for path in rule.http.paths:
                        path_ = {
                            (True, True): rule.host + path.path,
                            (True, False): rule.host,
                            (False, True): path.path,
                            (False, False): "/"
                        }[(rule.host is not None, path.path is not None)]
                        self._backends.append(
                            (path_, path.backend.service_name, path.backend.service_port))
            for tls in spec.tls if spec.tls else ():
                self._hosts[tls.secret_name if tls.secret_name else ""].extend(
                    tls.hosts if tls.hosts else ())

        status = ingress.status
        if status:
            with suppress(AttributeError):
                # Anything along the path to status..ingress is optional (aka may be None).
                self._load_balancers.extend([{
                    "hostname": _.hostname if _.hostname else "",
                    "ip": _.ip if _.ip else "",
                } for _ in status.load_balancer.ingress])

    @property
    def info(self):
        return {
            self.name: {
                "backends": self._backends,
                "hosts": self._hosts,
                "load_balancers": self._load_balancers,
            }
        }


class Pod(Metadata):
    def __init__(self, pod):
        # type: (client.V1Pod) -> None
        super(Pod, self).__init__(pod.metadata)
        spec = pod.spec
        if spec:
            self.node = spec.node_name
            self.host_network = (spec.host_network if spec.host_network is not None else False)
            self.dns_policy = spec.dns_policy
            self._containers = spec.containers
        else:
            self.node = None
            self.host_network = False
            self.dns_policy = None
            self._containers = []

        status = pod.status
        if status:
            self.host_ip = status.host_ip
            self.pod_ip = status.pod_ip
            self.qos_class = status.qos_class
            self._container_statuses = (status.container_statuses
                                        if status.container_statuses else [])
            self._conditions = status.conditions if status.conditions else []
        else:
            self.host_ip = None
            self.pod_ip = None
            self.qos_class = None
            self._container_statuses = []
            self._conditions = []

    @staticmethod
    def zero_resources():
        return {
            'limits': {
                'cpu': 0.0,
                'memory': 0.0,
            },
            'requests': {
                'cpu': 0.0,
                'memory': 0.0,
            }
        }

    @property
    def resources(self):
        view = self.zero_resources()
        for container in self._containers:
            resources = container.resources
            if not resources:
                continue
            limits = resources.limits
            if limits:
                view['limits']['cpu'] += parse_frac_prefix(limits.get('cpu', 'inf'))
                view['limits']['memory'] += parse_memory(limits.get('memory', 'inf'))
            else:
                view['limits']['cpu'] += float('inf')
                view['limits']['memory'] += float('inf')
            requests = resources.requests
            if requests:
                view['requests']['cpu'] += parse_frac_prefix(requests.get('cpu', '0.0'))
                view['requests']['memory'] += parse_memory(requests.get('memory', '0.0'))
        return view

    @property
    def containers(self):
        view = {
            container.name: {
                'image': container.image,
                'image_pull_policy': container.image_pull_policy,
                'ready': False,
                'restart_count': 0,
                'state': None,
                'state_reason': "",
                'state_exit_code': 0,
                'container_id': None,
                'image_id': None,
            } for container in self._containers
        }
        for container_status in self._container_statuses:
            data = view[container_status.name]
            state = container_status.state
            if state:
                if state.running:
                    data["state"] = "running"
                elif state.terminated:
                    data["state"] = "terminated"
                    data["state_exit_code"] = state.terminated.exit_code
                    data["state_reason"] = state.terminated.reason
                elif state.waiting:
                    data["state"] = "waiting"
                    data["state_reason"] = state.waiting.reason
            data['ready'] = container_status.ready
            data['restart_count'] = container_status.restart_count
            data['container_id'] = (container_status.container_id.replace('docker://', '')
                                    if container_status.container_id else '')
            data['image_id'] = container_status.image_id
        return view

    @property
    def conditions(self):
        """Return condition type and status.

        See Also:
            - Node.conditions

        """
        return {c.type: c.status for c in self._conditions}

    @property
    def info(self):
        return {
            'node': self.node,
            'host_network': self.host_network,
            'dns_policy': self.dns_policy,
            'host_ip': self.host_ip,
            'pod_ip': self.pod_ip,
            'qos_class': self.qos_class,
        }


class Endpoint(Metadata):
    # See Also:
    #   https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1Endpoints.md

    def __init__(self, endpoint):
        super(Endpoint, self).__init__(endpoint.metadata)
        # There is no spec here.
        self._subsets = [
            self._parse_subset(subset) for subset in (endpoint.subsets if endpoint.subsets else ())
        ]

    @staticmethod
    def _parse_subset(subset):
        # Silent false positive from pylint.
        #  - https://github.com/PyCQA/pylint/issues/574
        #  - https://github.com/PyCQA/pylint/issues/2818
        # pylint: disable=superfluous-parens
        addresses = [{
            "hostname": _.hostname if _.hostname else "",
            "ip": _.ip if _.ip else "",
            "node_name": _.node_name if _.node_name else "",
        } for _ in (subset.addresses if subset.addresses else ())]
        not_ready_addresses = [{
            "hostname": _.hostname if _.hostname else "",
            "ip": _.ip if _.ip else "",
            "node_name": _.node_name if _.node_name else "",
        } for _ in (subset.not_ready_addresses if subset.not_ready_addresses else ())]
        ports = [
            {
                "name": _.name if _.name else "",
                "port": _.port,  # not optional
                "protocol": _.protocol if _.protocol else "TCP",
            } for _ in (subset.ports if subset.ports else ())
        ]
        # pylint: enable=superfluous-parens
        return {"addresses": addresses, "not_ready_addresses": not_ready_addresses, "ports": ports}

    @property
    def infos(self):
        return {"subsets": self._subsets}


class Job(Metadata):
    def __init__(self, job):
        super(Job, self).__init__(job.metadata)
        spec = job.spec
        if spec:
            self._pod = spec.template
            self._pod_spec = self._pod.spec
        else:
            self._pod = None
            self._pod_spec = None

        if self._pod_spec:
            self._pod_containers = self._pod_spec.containers
            self._pod_node = self._pod_spec.node_name
            self._pod_host_network = self._pod_spec.host_network if self._pod_spec.host_network else False
            self._pod_dns_policy = self._pod_spec.dns_policy
        else:
            self._pod_containers = []
            self._pod_node = None
            self._pod_host_network = False
            self._pod_dns_policy = None

        def count(nn):
            return nn if nn is not None else 0

        status = job.status
        if status:
            self._active = count(status.active)
            self._failed = count(status.failed)
            self._succeeded = count(status.succeeded)
        else:
            self._active = 0
            self._failed = 0
            self._succeeded = 0

    @property
    def infos(self):
        return {
            "active": self._active,
            "failed": self._failed,
            "succeeded": self._succeeded,
        }

    @property
    def pod_infos(self):
        # Pod handles `spec.template.spec` of type `V1PodSpec`.
        # According to the official docs: The pod template section "has exactly
        # the same schame as a pod, except it is nested and does not have an
        # apiVersion or kind."
        return {
            "node": self._pod_node,
            "host_network": self._pod_host_network,
            "dns_policy": self._pod_dns_policy,
            "host_ip": None,
            "pod_ip": None,
            "qos_class": None,
        }

    @property
    def containers(self):
        # Pod handles `spec.template.spec` of type `V1PodSpec`.
        # See also: `pod_info(self)`, `Pod.containers()`.
        if not self._pod:
            return {}
        return {
            container.name: {
                'image': container.image,
                'image_pull_policy': container.image_pull_policy,
            } for container in self._pod_containers
        }


class DaemonSet(Metadata):
    def __init__(self, daemon_set):
        super(DaemonSet, self).__init__(daemon_set.metadata)
        status = daemon_set.status
        if status:
            self.collision_count = status.collision_count
            self.conditions = status.conditions
            self.desired_number_scheduled = status.desired_number_scheduled
            self.current_number_scheduled = status.current_number_scheduled
            self.number_misscheduled = status.number_misscheduled
            self.number_ready = status.number_ready
            self.number_available = status.number_available
            self.number_unavailable = status.number_unavailable
            self.observed_generation = status.observed_generation
            self.updated_number_scheduled = status.updated_number_scheduled
        else:
            self.collision_count = None
            self.conditions = None
            self.current_number_scheduled = None
            self.desired_number_scheduled = None
            self.number_available = None
            self.number_misscheduled = None
            self.number_ready = None
            self.number_unavailable = None
            self.observed_generation = None
            self.updated_number_scheduled = None

        try:
            self._containers = daemon_set.spec.template.spec.containers
        except AttributeError:
            self._containers = []

    @property
    def info(self):
        return {
            'collision_count': self.collision_count,
            'conditions': self.conditions,
            'current_number_scheduled': self.current_number_scheduled,
            'desired_number_scheduled': self.desired_number_scheduled,
            'number_available': self.number_available,
            'number_misscheduled': self.number_misscheduled,
            'number_ready': self.number_ready,
            'number_unavailable': self.number_unavailable,
            'observed_generation': self.observed_generation,
            'updated_number_scheduled': self.updated_number_scheduled,
        }

    @property
    def containers(self):
        return {
            container.name: {
                'image': container.image,
                'image_pull_policy': container.image_pull_policy,
            } for container in self._containers
        }


class StatefulSet(Metadata):
    def __init__(self, stateful_set):
        super(StatefulSet, self).__init__(stateful_set.metadata)
        spec = stateful_set.spec
        strategy = spec.update_strategy
        if strategy:
            self._strategy_type = strategy.type
            rolling_update = strategy.rolling_update
            if rolling_update:
                self._partition = rolling_update.partition
            else:
                self._partition = None
        else:
            self._strategy_type = None
            self._partition = None
        status = stateful_set.status
        if status:
            self._ready_replicas = status.ready_replicas
            self._replicas = status.replicas
        else:
            self._ready_replicas = None
            self._replicas = None

    @property
    def replicas(self):
        return {
            'ready_replicas': self._ready_replicas,
            'replicas': self._ready_replicas,
            'strategy_type': self._strategy_type,
            'partition': self._partition,
        }


class Namespace(Metadata):
    # TODO: namespaces may have resource quotas and limits
    # https://kubernetes.io/docs/tasks/administer-cluster/namespaces/
    def __init__(self, namespace):
        # type: (client.V1Namespace) -> None
        super(Namespace, self).__init__(namespace.metadata)
        self._status = namespace.status

    @property
    def phase(self):
        # type: () -> Optional[str]
        if self._status:
            return self._status.phase
        return None


class PersistentVolume(Metadata):
    def __init__(self, pv):
        # type: (client.V1PersistentVolume) -> None
        super(PersistentVolume, self).__init__(pv.metadata)
        self._status = pv.status
        self._spec = pv.spec

    @property
    def access_modes(self):
        # type: () -> Optional[List[str]]
        if self._spec:
            return self._spec.access_modes
        return None

    @property
    def capacity(self):
        # type: () -> Optional[float]
        if not self._spec or not self._spec.capacity:
            return None
        storage = self._spec.capacity.get('storage')
        if storage:
            return parse_memory(storage)
        return None

    @property
    def phase(self):
        # type: () -> Optional[str]
        if self._status:
            return self._status.phase
        return None


class PersistentVolumeClaim(Metadata):
    def __init__(self, pvc):
        # type: (client.V1PersistentVolumeClaim) -> None
        super(PersistentVolumeClaim, self).__init__(pvc.metadata)
        self._status = pvc.status
        self._spec = pvc.spec

    @property
    def conditions(self):
        # type: () -> Optional[client.V1PersistentVolumeClaimCondition]
        # TODO: don't return client specific object
        if self._status:
            return self._status.conditions
        return None

    @property
    def phase(self):
        # type: () -> Optional[str]
        if self._status:
            return self._status.phase
        return None

    @property
    def volume_name(self):
        # type: () -> Optional[str]
        if self._spec:
            return self._spec.volume_name
        return None


class StorageClass(Metadata):
    def __init__(self, storage_class):
        # type: (client.V1StorageClass) -> None
        super(StorageClass, self).__init__(storage_class.metadata)
        self.provisioner = storage_class.provisioner
        self.reclaim_policy = storage_class.reclaim_policy


class Role(Metadata):
    def __init__(self, role):
        # type: (Union[client.V1Role, client.V1ClusterRole]) -> None
        super(Role, self).__init__(role.metadata)


ListElem = TypeVar('ListElem', bound=Metadata)


class K8sList(Generic[ListElem], MutableSequence):
    def __init__(self, elements):
        # type: (List[ListElem]) -> None
        super(K8sList, self).__init__()
        self._elements = elements

    def __getitem__(self, index):
        return self._elements[index]

    def __setitem__(self, index, value):
        self._elements.__setitem__(index, value)

    def __delitem__(self, index):
        self._elements.__delitem__(index)

    def __len__(self):
        # type: () -> int
        return len(self._elements)

    def insert(self, index, value):
        self._elements.insert(index, value)

    def labels(self):
        return {item.name: item.labels for item in self}

    def group_by(self, selectors):
        grouped = {}
        for element in self:
            for name, selector in selectors.iteritems():
                if element.matches(selector):
                    grouped.setdefault(name, self.__class__(elements=[])).append(element)
        return grouped


class NodeList(K8sList[Node]):
    def list_nodes(self):
        # type: () -> Dict[str, List[str]]
        return {'nodes': [node.name for node in self if node.name]}

    def conditions(self):
        # type: () -> Dict[str, Dict[str, str]]
        return {node.name: node.conditions for node in self if node.name and node.conditions}

    def resources(self):
        # type: () -> Dict[str, Dict[str, Dict[str, Optional[float]]]]
        return {node.name: node.resources for node in self if node.name}

    def stats(self):
        return {node.name: node.stats for node in self if node.name}

    def total_resources(self):
        merge = functools.partial(left_join_dicts, operation=operator.add)
        return functools.reduce(merge, self.resources().itervalues())

    def cluster_stats(self):
        stats = self.stats()
        merge = functools.partial(left_join_dicts, operation=operator.add)
        result = functools.reduce(merge, stats.itervalues())
        # During the merging process the sum of all timestamps is calculated.
        # To obtain the average time of all nodes devide by the number of nodes.
        result['timestamp'] = round(result['timestamp'] / len(stats), 1)  # fixed: true-division
        return result


class ComponentStatusList(K8sList[ComponentStatus]):
    def list_statuses(self):
        # type: () -> Dict[str, List[Dict[str, str]]]
        return {status.name: status.conditions for status in self if status.name}


class ServiceList(K8sList[Service]):
    def infos(self):
        return {service.name: service.info for service in self}

    def selector(self):
        return {service.name: service.selector for service in self}

    def ports(self):
        return {service.name: service.ports for service in self}


class DeploymentList(K8sList[Deployment]):
    def replicas(self):
        return {deployment.name: deployment.replicas for deployment in self}


class IngressList(K8sList[Ingress]):
    def infos(self):
        return {ingress.name: ingress.info for ingress in self}


class DaemonSetList(K8sList[DaemonSet]):
    def info(self):
        return {daemon_set.name: daemon_set.info for daemon_set in self}

    def containers(self):
        return {daemon_set.name: daemon_set.containers for daemon_set in self}


class StatefulSetList(K8sList[StatefulSet]):
    def replicas(self):
        return {stateful_set.name: stateful_set.replicas for stateful_set in self}


class PodList(K8sList[Pod]):
    def pods_per_node(self):
        # type: () -> Dict[str, Dict[str, Dict[str, int]]]
        pods_sorted = sorted(self, key=lambda pod: pod.node)
        by_node = itertools.groupby(pods_sorted, lambda pod: pod.node)
        return {
            node: {
                'requests': {
                    'pods': len(list(pods))
                }
            } for node, pods in by_node if node is not None
        }

    def pods_in_cluster(self):
        return {'requests': {'pods': len(self)}}

    def info(self):
        return {pod.name: pod.info for pod in self}

    def resources(self):
        return {pod.name: pod.resources for pod in self}

    def containers(self):
        return {pod.name: pod.containers for pod in self}

    def conditions(self):
        return {pod.name: pod.conditions for pod in self}

    def resources_per_node(self):
        # type: () -> Dict[str, Dict[str, Dict[str, float]]]
        """
        Returns the limits and requests of all containers grouped by node. If at least
        one container does not specify a limit, infinity is returned as the container
        may consume any amount of resources.
        """

        pods_sorted = sorted(self, key=lambda pod: pod.node)
        by_node = itertools.groupby(pods_sorted, lambda pod: pod.node)
        merge = functools.partial(left_join_dicts, operation=operator.add)
        return {
            node: functools.reduce(merge, [p.resources for p in pods], Pod.zero_resources())
            for node, pods in by_node
            if node is not None
        }

    def total_resources(self):
        merge = functools.partial(left_join_dicts, operation=operator.add)
        return functools.reduce(merge, [p.resources for p in self], Pod.zero_resources())


class EndpointList(K8sList[Endpoint]):
    def info(self):
        return {endpoint.name: endpoint.infos for endpoint in self}


class JobList(K8sList[Job]):
    def info(self):
        return {job.name: job.infos for job in self}

    def pod_infos(self):
        return {job.name: job.pod_infos for job in self}

    def containers(self):
        return {job.name: job.containers for job in self}


class NamespaceList(K8sList[Namespace]):
    def list_namespaces(self):
        # type: () -> Dict[str, Dict[str, Dict[str, Optional[str]]]]
        return {
            namespace.name: {
                'status': {
                    'phase': namespace.phase,
                },
            } for namespace in self if namespace.name
        }


class PersistentVolumeList(K8sList[PersistentVolume]):
    def list_volumes(self):
        # type: () -> Dict[str, Dict[str, Union[Optional[List[str]], Optional[float], Dict[str, Optional[str]]]]]
        # TODO: Output details of the different types of volumes
        return {
            pv.name: {
                'access': pv.access_modes,
                'capacity': pv.capacity,
                'status': {
                    'phase': pv.phase,
                },
            } for pv in self if pv.name
        }


class PersistentVolumeClaimList(K8sList[PersistentVolumeClaim]):
    def list_volume_claims(self):
        # type: () -> Dict[str, Dict[str, Any]]
        # TODO: Fix "Any"
        return {
            pvc.name: {
                'namespace': pvc.namespace,
                'condition': pvc.conditions,
                'phase': pvc.phase,
                'volume': pvc.volume_name,
            } for pvc in self if pvc.name
        }


class StorageClassList(K8sList[StorageClass]):
    def list_storage_classes(self):
        # type: () -> Dict[Any, Dict[str, Any]]
        # TODO: should be Dict[str, Dict[str, Optional[str]]]
        return {
            storage_class.name: {
                'provisioner': storage_class.provisioner,
                'reclaim_policy': storage_class.reclaim_policy
            } for storage_class in self if storage_class.name
        }


class RoleList(K8sList[Role]):
    def list_roles(self):
        return [{
            'name': role.name,
            'namespace': role.namespace,
            'creation_timestamp': role.creation_timestamp
        } for role in self if role.name]


class Metric(Metadata):
    def __init__(self, metric):
        # Initialize Metric objects without metadata for now, because
        # the provided metadata only contains a selfLink and no other
        # valuable information.
        super(Metric, self).__init__(metadata=None)
        self.from_object = metric['describedObject']
        self.metrics = {metric['metricName']: metric.get('value')}

    def __add__(self, other):
        assert self.from_object == other.from_object
        self.metrics.update(other.metrics)
        return self

    def __str__(self):
        return str(self.__dict__)

    def __repr__(self):
        return str(self.__dict__)


class MetricList(K8sList[Metric]):
    def __add__(self, other):
        return MetricList([a + b for a, b in zip(self, other)])

    def list_metrics(self):
        return [item.__dict__ for item in self]


class PiggybackGroup(object):
    """
    A group of elements where an element is e.g. a piggyback host.
    """
    def __init__(self):
        # type: () -> None
        super(PiggybackGroup, self).__init__()
        self._elements = OrderedDict()  # type: OrderedDict[str, PiggybackHost]

    def get(self, element_name):
        # type: (str) -> PiggybackHost
        if element_name not in self._elements:
            self._elements[element_name] = PiggybackHost()
        return self._elements[element_name]

    def join(self, section_name, pairs):
        # type: (str, Mapping[str, Dict[str, Any]]) -> PiggybackGroup
        for element_name, data in pairs.iteritems():
            section = self.get(element_name).get(section_name)
            section.insert(data)
        return self

    def output(self, piggyback_prefix=""):
        # type: (str) -> List[str]
        # The names of elements may not be unique. Kubernetes guarantees e.g. that
        # only one object of a given kind can have one one name at a time. I.e.
        # there may only be one deployment with the name "foo", but there may exist
        # a service with name "foo" as well.
        # To obtain unique names for piggyback hosts it is therefore possible to
        # specify a name prefix.
        # see: https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#names
        data = []
        for name, element in self._elements.iteritems():
            data.append('<<<<%s>>>>' % (piggyback_prefix + name))
            data.extend(element.output())
            data.append('<<<<>>>>')
        return data


class PiggybackHost(object):
    """
    An element that bundles a collection of sections.
    """
    def __init__(self):
        # type: () -> None
        super(PiggybackHost, self).__init__()
        self._sections = OrderedDict()  # type: OrderedDict[str, Section]

    def get(self, section_name):
        # type: (str) -> Section
        if section_name not in self._sections:
            self._sections[section_name] = Section()
        return self._sections[section_name]

    def output(self):
        # type: () -> List[str]
        data = []
        for name, section in self._sections.iteritems():
            data.append('<<<%s:sep(0)>>>' % name)
            data.append(section.output())
        return data


class Section(object):
    """
    An agent section.
    """
    def __init__(self):
        # type: () -> None
        super(Section, self).__init__()
        self._content = OrderedDict()  # type: OrderedDict[str, Dict[str, Any]]

    def insert(self, data):
        # type: (Dict[str, Any]) -> None
        for key, value in data.iteritems():
            if key not in self._content:
                self._content[key] = value
            else:
                if isinstance(value, dict):
                    self._content[key].update(value)
                else:
                    raise ValueError('Key %s is already present and cannot be merged' % key)

    def output(self):
        # type: () -> str
        return json.dumps(self._content)


class ApiData(object):
    """
    Contains the collected API data.
    """
    def __init__(self, api_client):
        # type: (client.ApiClient) -> None
        super(ApiData, self).__init__()
        logging.info('Collecting API data')

        logging.debug('Constructing API client wrappers')
        core_api = client.CoreV1Api(api_client)
        storage_api = client.StorageV1Api(api_client)
        rbac_authorization_api = client.RbacAuthorizationV1Api(api_client)
        ext_api = client.ExtensionsV1beta1Api(api_client)
        batch_api = client.BatchV1Api(api_client)
        apps_api = client.AppsV1beta1Api(api_client)

        self.custom_api = client.CustomObjectsApi(api_client)

        logging.debug('Retrieving data')
        storage_classes = storage_api.list_storage_class()
        namespaces = core_api.list_namespace()
        roles = rbac_authorization_api.list_role_for_all_namespaces()
        cluster_roles = rbac_authorization_api.list_cluster_role()
        component_statuses = core_api.list_component_status()
        nodes = core_api.list_node()
        # Try to make it a post, when client api support sending post data
        # include {"num_stats": 1} to get the latest only and use less bandwidth
        nodes_stats = [
            core_api.connect_get_node_proxy_with_path(node.metadata.name, "stats")
            for node in nodes.items
        ]
        pvs = core_api.list_persistent_volume()
        pvcs = core_api.list_persistent_volume_claim_for_all_namespaces()
        pods = core_api.list_pod_for_all_namespaces()
        endpoints = core_api.list_endpoints_for_all_namespaces()
        jobs = batch_api.list_job_for_all_namespaces()
        services = core_api.list_service_for_all_namespaces()
        deployments = ext_api.list_deployment_for_all_namespaces()
        ingresses = ext_api.list_ingress_for_all_namespaces()
        daemon_sets = ext_api.list_daemon_set_for_all_namespaces()
        stateful_sets = apps_api.list_stateful_set_for_all_namespaces()

        logging.debug('Assigning collected data')
        self.storage_classes = StorageClassList(map(StorageClass, storage_classes.items))
        self.namespaces = NamespaceList(map(Namespace, namespaces.items))
        self.roles = RoleList(map(Role, roles.items))
        self.cluster_roles = RoleList(map(Role, cluster_roles.items))
        self.component_statuses = ComponentStatusList(map(ComponentStatus,
                                                          component_statuses.items))
        self.nodes = NodeList(map(Node, nodes.items, nodes_stats))
        self.persistent_volumes = PersistentVolumeList(map(PersistentVolume, pvs.items))
        self.persistent_volume_claims = PersistentVolumeClaimList(
            map(PersistentVolumeClaim, pvcs.items))
        self.pods = PodList(map(Pod, pods.items))
        self.endpoints = EndpointList(map(Endpoint, endpoints.items))
        self.jobs = JobList(map(Job, jobs.items))
        self.services = ServiceList(map(Service, services.items))
        self.deployments = DeploymentList(map(Deployment, deployments.items))
        self.ingresses = IngressList(map(Ingress, ingresses.items))
        self.daemon_sets = DaemonSetList(map(DaemonSet, daemon_sets.items))
        self.stateful_sets = StatefulSetList(map(StatefulSet, stateful_sets.items))

        pods_custom_metrics = {
            "memory": ['memory_rss', 'memory_swap', 'memory_usage_bytes', 'memory_max_usage_bytes'],
            "fs": ['fs_inodes', 'fs_reads', 'fs_writes', 'fs_limit_bytes', 'fs_usage_bytes'],
            "cpu": ['cpu_system', 'cpu_user', 'cpu_usage']
        }

        self.pods_Metrics = dict()  # type: Dict[str, Dict[str, List]]
        for metric_group, metrics in pods_custom_metrics.items():
            self.pods_Metrics[metric_group] = self.get_namespaced_group_metric(metrics)

    def get_namespaced_group_metric(self, metrics):
        # type: (List[str]) -> Dict[str, List]
        queries = [self.get_namespaced_custom_pod_metric(metric) for metric in metrics]

        grouped_metrics = {}  # type: Dict[str, List]
        for response in queries:
            for namespace in response:
                grouped_metrics.setdefault(namespace, []).append(response[namespace])

        for namespace in grouped_metrics:
            grouped_metrics[namespace] = functools.reduce(
                operator.add, grouped_metrics[namespace]).list_metrics()

        return grouped_metrics

    def get_namespaced_custom_pod_metric(self, metric):
        # type: (str) -> Dict

        logging.debug('Query Custom Metrics Endpoint: %s', metric)
        custom_metric = {}
        for namespace in self.namespaces:
            try:
                data = map(
                    Metric,
                    self.custom_api.get_namespaced_custom_object(
                        'custom.metrics.k8s.io',
                        'v1beta1',
                        namespace.name,
                        'pods/*',
                        metric,
                    )['items'])
                custom_metric[namespace.name] = MetricList(data)
            except ApiException as err:
                if err.status == 404:
                    logging.info('Data unavailable. No pods in namespace %s', namespace.name)
                elif err.status == 500:
                    logging.info('Data unavailable. %s', err)
                else:
                    raise err

        return custom_metric

    def cluster_sections(self):
        # type: () -> str
        logging.info('Output cluster sections')
        e = PiggybackHost()
        e.get('k8s_nodes').insert(self.nodes.list_nodes())
        e.get('k8s_namespaces').insert(self.namespaces.list_namespaces())
        e.get('k8s_persistent_volumes').insert(self.persistent_volumes.list_volumes())
        e.get('k8s_component_statuses').insert(self.component_statuses.list_statuses())
        e.get('k8s_persistent_volume_claims').insert(
            self.persistent_volume_claims.list_volume_claims())
        e.get('k8s_storage_classes').insert(self.storage_classes.list_storage_classes())
        e.get('k8s_roles').insert({'roles': self.roles.list_roles()})
        e.get('k8s_roles').insert({'cluster_roles': self.cluster_roles.list_roles()})
        e.get('k8s_resources').insert(self.nodes.total_resources())
        e.get('k8s_resources').insert(self.pods.total_resources())
        e.get('k8s_resources').insert(self.pods.pods_in_cluster())
        e.get('k8s_stats').insert(self.nodes.cluster_stats())
        return '\n'.join(e.output())

    def node_sections(self):
        # type: () -> str
        logging.info('Output node sections')
        g = PiggybackGroup()
        g.join('labels', self.nodes.labels())
        g.join('k8s_resources', self.nodes.resources())
        g.join('k8s_resources', self.pods.resources_per_node())
        g.join('k8s_resources', self.pods.pods_per_node())
        g.join('k8s_stats', self.nodes.stats())
        g.join('k8s_conditions', self.nodes.conditions())
        return '\n'.join(g.output())

    def custom_metrics_section(self):
        # type: () -> str
        logging.info('Output pods custom metrics')
        e = PiggybackHost()
        for c_metric in self.pods_Metrics:
            e.get('k8s_pods_%s' % c_metric).insert(self.pods_Metrics[c_metric])
        return '\n'.join(e.output())

    def pod_sections(self):
        logging.info('Output pod sections')
        g = PiggybackGroup()
        g.join('labels', self.pods.labels())
        g.join('k8s_resources', self.pods.resources())
        g.join('k8s_conditions', self.pods.conditions())
        g.join('k8s_pod_container', self.pods.containers())
        g.join('k8s_pod_info', self.pods.info())
        return '\n'.join(g.output(piggyback_prefix="pod_"))

    def endpoint_sections(self):
        logging.info('Output endpoint sections')
        g = PiggybackGroup()
        g.join('labels', self.endpoints.labels())
        g.join('k8s_endpoint_info', self.endpoints.info())
        return '\n'.join(g.output(piggyback_prefix="endpoint_"))

    def job_sections(self):
        logging.info('Output job sections')
        g = PiggybackGroup()
        g.join('labels', self.jobs.labels())
        g.join('k8s_job_container', self.jobs.containers())
        g.join('k8s_pod_info', self.jobs.pod_infos())
        g.join('k8s_job_info', self.jobs.info())
        return '\n'.join(g.output(piggyback_prefix="job_"))

    def service_sections(self):
        logging.info('Output service sections')
        g = PiggybackGroup()
        g.join('labels', self.services.labels())
        g.join('k8s_selector', self.services.selector())
        g.join('k8s_service_info', self.services.infos())
        g.join('k8s_service_port', self.services.ports())
        pod_names = {
            service_name: {
                'names': [pod.name for pod in pods]
            } for service_name, pods in self.pods.group_by(self.services.selector()).iteritems()
        }
        g.join('k8s_assigned_pods', pod_names)
        return '\n'.join(g.output(piggyback_prefix="service_"))

    def deployment_sections(self):
        logging.info('Output deployment sections')
        g = PiggybackGroup()
        g.join('labels', self.deployments.labels())
        g.join('k8s_replicas', self.deployments.replicas())
        return '\n'.join(g.output(piggyback_prefix="deployment_"))

    def ingress_sections(self):
        logging.info('Output ingress sections')
        g = PiggybackGroup()
        g.join('labels', self.ingresses.labels())
        g.join('k8s_ingress_infos', self.ingresses.infos())
        return '\n'.join(g.output(piggyback_prefix="ingress_"))

    def daemon_set_sections(self):
        logging.info('Daemon set sections')
        g = PiggybackGroup()
        g.join('labels', self.daemon_sets.labels())
        g.join('k8s_daemon_pods', self.daemon_sets.info())
        g.join('k8s_daemon_pod_containers', self.daemon_sets.containers())
        return '\n'.join(g.output(piggyback_prefix="daemon_set_"))

    def stateful_set_sections(self):
        logging.info('Stateful set sections')
        g = PiggybackGroup()
        g.join('labels', self.stateful_sets.labels())
        g.join('k8s_stateful_set_replicas', self.stateful_sets.replicas())
        return '\n'.join(g.output(piggyback_prefix="stateful_set_"))


def get_api_client(arguments):
    # type: (argparse.Namespace) -> client.ApiClient
    logging.info('Constructing API client')

    config = client.Configuration()
    if arguments.url_prefix:
        config.host = '%s:%s%s' % (arguments.url_prefix.rstrip("/"), arguments.port,
                                   arguments.path_prefix)
    else:
        config.host = 'https://%s:%s%s' % (arguments.host, arguments.port, arguments.path_prefix)

    config.api_key_prefix['authorization'] = 'Bearer'
    config.api_key['authorization'] = arguments.token

    if arguments.no_cert_check:
        logging.info('Disabling SSL certificate verification')
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        config.verify_ssl = False
    else:
        config.ssl_ca_cert = os.environ.get('REQUESTS_CA_BUNDLE')

    return client.ApiClient(config)


def main(args=None):
    # type: (Optional[List[str]]) -> int
    if args is None:
        cmk.utils.password_store.replace_passwords()
        args = sys.argv[1:]
    arguments = parse(args)

    try:
        setup_logging(arguments.verbose)
        logging.debug('parsed arguments: %s\n', arguments)

        with cmk.utils.profile.Profile(enabled=bool(arguments.profile),
                                       profile_file=arguments.profile):
            api_client = get_api_client(arguments)
            api_data = ApiData(api_client)
            print(api_data.cluster_sections())
            print(api_data.custom_metrics_section())
            if 'nodes' in arguments.infos:
                print(api_data.node_sections())
            if 'pods' in arguments.infos:
                print(api_data.pod_sections())
            if 'endpoints' in arguments.infos:
                print(api_data.endpoint_sections())
            if 'jobs' in arguments.infos:
                print(api_data.job_sections())
            if 'deployments' in arguments.infos:
                print(api_data.deployment_sections())
            if 'ingresses' in arguments.infos:
                print(api_data.ingress_sections())
            if 'services' in arguments.infos:
                print(api_data.service_sections())
            if 'daemon_sets' in arguments.infos:
                print(api_data.daemon_set_sections())
            if 'stateful_sets' in arguments.infos:
                print(api_data.stateful_set_sections())
    except urllib3.exceptions.MaxRetryError as e:
        if arguments.debug:
            raise
        if isinstance(e.reason, urllib3.exceptions.NewConnectionError):
            sys.stderr.write('Failed to establish a connection to %s:%s at URL %s' %
                             (e.pool.host, e.pool.port, e.url))
        else:
            sys.stderr.write("%s" % e)
        return 1
    except Exception as e:
        if arguments.debug:
            raise
        sys.stderr.write("%s" % e)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())