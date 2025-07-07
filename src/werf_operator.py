<<<<<<< HEAD
=======
"""
Copyright 2023 Sergey Klyuykov

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from __future__ import annotations

>>>>>>> 7f7efc6 (0.2.1)
import base64
import dataclasses
import functools
import os
import random
import re
import typing as _t
import uuid
from collections import defaultdict
from contextlib import suppress
from copy import deepcopy

import kopf
import yaml
from kubernetes import client as k8s_client
from kubernetes.watch import Watch
from oras.client import OrasClient
from pkg_resources import parse_version

DISABLE_ANNOTATION = 'operator.werf.dev/disable-autoupdate'
JOB_LABEL = 'operator.werf.dev/deployment'
JOBS_TTL = int(os.getenv('WERF_OPERATOR_JOBS_TTL', '120'))
T = _t.TypeVar('T')


def reconnect_on_error(func: T) -> T:
    @functools.wraps(func)  # type: ignore[arg-type]
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except ValueError:
            self.login()
            return func(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def valid_annotation(annotation):
    return any([
        annotation.startswith('app.kubernetes.io/'),
        annotation.startswith('app.k8s.io/'),
        annotation.startswith('argocd.argoproj.io/'),
    ])


@dataclasses.dataclass(slots=True)
class RepoHandler:
    version_re = re.compile(r"^[v]?([\d]+)\.([\d\*]+)\.([\d\*]+)", re.MULTILINE)

    client: OrasClient
    repo: str
    values: _t.Optional[str] = None
    env: dict[str, str] = dataclasses.field(default_factory=lambda: {})
    version: _t.Pattern | str = re.compile('latest')
    secret_name: _t.Optional[str] = None
    namespace: _t.Optional[str] = None
    semver: bool = False
    repo_username: _t.Optional[str] = None
    repo_password: _t.Optional[str] = None
    annotations: dict[str, str] = dataclasses.field(default_factory=dict)
    labels: dict[str, str] = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.version, str):
            if self.version_re.match(self.version):
                self.version = re.compile(r'^' + self.version.replace('.', r'\.').replace('*', r'.+'), re.MULTILINE)
                self.semver = True
            else:
                self.version = re.compile(r'^' + self.version + r'$')

    def login(self):
        self.client.login(password=self.repo_password, username=self.repo_username)

    @reconnect_on_error
    def get_required_tag(self) -> str:
        tags = list(filter(self.version.match, self.client.get_tags(self.repo)))  # type: ignore[union-attr]

        if self.semver:
            tags.sort(key=parse_version)
            tags.reverse()
            return tags[0]

        return tags[0]

    @reconnect_on_error
    def get_required_digest(self, tag: str) -> str:
        manifest = self.client.remote.get_manifest(f'{self.repo}:{tag}')
        return manifest['config']['digest']

    def get_latest_digest(self):
        return self.get_required_digest(self.get_required_tag())

    def deploy(self, version, name, namespace):
        return self.make_action(version, name, namespace, action='bundle apply')

    def dismiss(self, version, name, namespace):
        return self.make_action(version, name, namespace, action='dismiss')

    def make_action(self, version, name, namespace, action):
        env_variables = {
            "WERF_REPO": f'{self.client.remote.hostname}/{self.repo}',
            "WERF_TAG": version,
            "WERF_NAMESPACE": self.namespace or namespace,
            "WERF_RELEASE": name,
        }
        if self.env:
            env_variables.update({k: v for k, v in self.env.items() if k not in env_variables})

        if action == 'dismiss':
            command = f'helm uninstall {name} --namespace={self.namespace or namespace}'
        else:
            command = action

        image = f'registry.werf.io/werf/werf:{os.getenv("WERF_OPERATOR_TAG", "latest")}'

        volumes = [
            {'name': 'docker', 'emptyDir': {}},
        ]
        mounts = [
            {
                "mountPath": '/home/build/.docker',
                "name": "docker",
            },
        ]

        if action != dismiss and self.values:
            with suppress(Exception):
                api_client = k8s_client.CoreV1Api()
                data = api_client.read_namespaced_config_map(self.values, namespace=namespace).data
                volumes.append({'name': 'values', 'configMap': {"name": self.values}})
                mounts.append({'mountPath': '/home/build/.values', "name": 'values', "readOnly": True})
                env_variables.update({
                    f"WERF_VALUES_OPERATOR_{i}": f"/home/build/.values/{n}"
                    for i, n in enumerate(data)
                })

        env_variables.update({
            f'WERF_ADD_ANNOTATION_OPERATOR{i}': f'{k}={v}'
            for i, (k, v) in enumerate(self.annotations.items())
            if valid_annotation(k)
        })
        env_variables.update({
            f'WERF_ADD_LABEL_OPERATOR{i}': f'{k}={v}'
            for i, (k, v) in enumerate(self.labels.items())
            if valid_annotation(k)
        })

        exec_container = {
            "image": image,
            "name": f"{action.replace(' ', '-')}-bundle",
            "command": ['sh'],
            "args": ['-ec', f'werf {command}'],
            "env": [
                {"name": key, "value": value}
                for key, value in env_variables.items()
            ],
            'volumeMounts': deepcopy(mounts),
        }

        result = {
            'apiVersion': 'batch/v1',
            'kind': 'Job',
            'metadata': {
                'name': f"werf-{action.replace(' ', '-')}-{uuid.uuid4()}",
                'annotations': {
                    JOB_LABEL: name,
                    **{
                        k: v
                        for k, v in self.annotations.items()
                        if valid_annotation(k)
                    },
                },
            },
            'spec': {
                'ttlSecondsAfterFinished': JOBS_TTL,
                'backoffLimit': 0,
                'template': {
                    'spec': {
                        'serviceAccount': 'werf',
                        'automountServiceAccountToken': True,
                        'volumes': volumes,
                        'containers': [exec_container],
                        'restartPolicy': 'Never',
                    },
                },
            },
        }
        if self.secret_name and action != 'dismiss':
            result['spec']['template']['spec']['initContainers'] = [{
                "image": image,
                "name": 'repo-auth',
                "command": ['sh'],
                "args": ['-ec', f'werf cr login {self.client.remote.hostname}'],
                "env": [
                    {"name": 'WERF_USERNAME',
                     "valueFrom": {"secretKeyRef": {"name": self.secret_name, "key": "username"}}},
                    {"name": 'WERF_PASSWORD',
                     "valueFrom": {"secretKeyRef": {"name": self.secret_name, "key": "password"}}},
                ],
                'volumeMounts': deepcopy(mounts),
            }]
        return result

    @classmethod
    def from_spec(cls, spec: dict):
        fields = dataclasses.fields(cls)
        return cls(**{
            key: value
            for key, value in spec.items()
            if key in fields
        })


NAMESPACED_REPOS: dict[str, dict[str, RepoHandler]] = defaultdict(lambda: {})


def get_image_repo(namespace, registry, repo, auth, version='latest', project_namespace=None, **kwargs) -> RepoHandler:
    if auth:
        v1 = k8s_client.CoreV1Api()
        sec: dict[str, str] = v1.read_namespaced_secret(auth, namespace).data  # type: ignore
        kwargs['repo_username'] = base64.b64decode(sec["username"]).decode('utf-8')
        kwargs['repo_password'] = base64.b64decode(sec["password"]).decode('utf-8')

    repo_handler = RepoHandler(
        client=OrasClient(hostname=registry),
        repo=repo,
        version=version,
        namespace=project_namespace,
        secret_name=auth,
        **kwargs,
    )

    if auth:
        repo_handler.login()

    return repo_handler


@kopf.on.create('operator.werf.dev', 'v1', 'Bundle')
@kopf.on.resume('operator.werf.dev', 'v1', 'Bundle')
def ready(spec, namespace, name, meta, **_):
    try:
        NAMESPACED_REPOS[namespace][name] = get_image_repo(
            namespace,
            **{'annotations': meta.get('annotations', {}), 'labels': meta.get('labels', {}), **spec},
        )
    except Exception as err:
        raise kopf.TemporaryError(f"The data is not yet ready. {err}", delay=5)
    return 1


@kopf.on.update('operator.werf.dev', 'v1', 'Bundle')  # type: ignore
def ready(spec, name, namespace, body, meta, **_):  # noqa: F811
    try:
        NAMESPACED_REPOS[namespace][name] = get_image_repo(
            namespace,
            **{'annotations': meta.get('annotations', {}), 'labels': meta.get('labels', {}), **spec},
        )
    except Exception as err:
        kopf.exception(body, exc=err)
        return 0
    return 1


@kopf.on.delete('operator.werf.dev', 'v1', 'Bundle', when=lambda status, **_: status.get('ready'))
def dismiss(name, namespace, status, logger, **_):
    try:
        current_version = status.get('deploy', {}).get('version')
        if current_version:
            job = NAMESPACED_REPOS[namespace][name].dismiss(current_version, name, namespace)
            logger.debug(f"Dismiss Job:\n{yaml.dump(job)}")

            batch_client = k8s_client.BatchV1Api()
            batch_client.create_namespaced_job(namespace, job)
    except Exception as err:
        logger.error(err)


def check_if_has_bundle(name, namespace):
    with suppress(Exception):
        for handler_name, handler in NAMESPACED_REPOS[namespace].items():
            if handler.values == name:
                yield handler_name


@kopf.on.field(
    'configmap',
    field='data',
    when=(lambda name, namespace, **_: bool(next(check_if_has_bundle(name, namespace), 0))),
)
def update_bundle(name, namespace, **_):
    api_client = k8s_client.CustomObjectsApi()
    patch_body = {'status': {"forceUpdate": 1}}
    for handler_name in check_if_has_bundle(name, namespace):
        api_client.patch_namespaced_custom_object(
            'operator.werf.dev', 'v1', namespace, 'bundles',
            handler_name, patch_body,
        )


@kopf.timer(
    'operator.werf.dev', 'v1', 'Bundle',
    interval=int(os.getenv('WERF_OPERATOR_TIMER_INTERVAL', '600')),
    initial_delay=3,
    idle=int(os.getenv('WERF_OPERATOR_TIMER_IDLE', '10')),
    annotations={DISABLE_ANNOTATION: kopf.ABSENT},
    when=(lambda status, **_: status.get('ready')),
)
def update(name, namespace, status, body, patch, logger, spec, **_):
    try:
        handler = NAMESPACED_REPOS[namespace][name]
    except KeyError as err:
        raise kopf.TemporaryError(f'Still initialize: {err}', delay=10)

    try:
        latest_tag = handler.get_required_tag()
    except KeyError as err:
        if "'tags'" == str(err):
            handler = NAMESPACED_REPOS[namespace][name] = get_image_repo(namespace, **spec)
            latest_tag = handler.get_required_tag()
        else:
            kopf.exception(body, exc=err)
            raise kopf.TemporaryError('Cannot parse latest tag.', delay=300)

    current_digest = status.get('deploy', {}).get('digest')
    try:
        latest_digest = handler.get_required_digest(latest_tag)
    except ValueError as err:
        kopf.exception(body, exc=err)
        raise kopf.TemporaryError(str(err))

    if str(current_digest) == str(latest_digest) and not status.get('forceUpdate'):
        return

    kopf.info(body, reason='Update', message='Initialize deploy application by digest update.')
    logger.info(f"Updating bundle image:\n{current_digest=}\n{latest_digest=}\nforceUpdate{status.get('forceUpdate')}")
    patch.status['target'] = {"digest": latest_digest, "version": latest_tag}
    job = handler.deploy(latest_tag, name, namespace)
    kopf.adopt(job)
    logger.debug(f"Deploy Job:\n{yaml.dump(job)}")

    batch_client = k8s_client.BatchV1Api()
    job_obj = batch_client.create_namespaced_job(namespace, job)
    job_body_dict = job_obj.to_dict()
    if 'apiVersion' not in job_body_dict:
        job_body_dict['apiVersion'] = job_body_dict['api_version']
    kopf.info(job_body_dict, reason='Nesting', message=f'Job created by werf operator bundle {name}')
    w = Watch()

    for event in w.stream(
            batch_client.list_namespaced_job,
            namespace=namespace,
            field_selector=f'metadata.name={job["metadata"]["name"]}',
    ):
        event_object = event["object"]
        if event_object.status.succeeded:
            w.stop()
            patch.status['deploy'] = {"digest": latest_digest, "version": latest_tag}
            kopf.info(body, reason='Update', message='Actual release has been deployed.')
        if not event_object.status.active and event_object.status.failed:
            w.stop()
            kopf.info(body, reason="ERROR", message="Cannot deploy release. Job failed.")

    if status['forceUpdate']:
        patch.status['forceUpdate'] = 0


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.peering.priority = random.randint(0, 32767)  # nosec
