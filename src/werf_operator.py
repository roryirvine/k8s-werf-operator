import base64
import dataclasses
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

    def __post_init__(self):
        if isinstance(self.version, str):
            if self.version_re.match(self.version):
                self.version = re.compile(r'^' + self.version.replace('.', r'\.').replace('*', r'.+'), re.MULTILINE)
                self.semver = True
            else:
                self.version = re.compile(r'^' + self.version + r'$')

    def login(self, username, password):
        self.client.login(password=password, username=username)

    def get_required_tag(self):
        tags = list(filter(self.version.match, self.client.get_tags(self.repo)['tags']))

        if self.semver:
            tags.sort(key=parse_version)
            tags.reverse()
            return tags[0]

        return tags[0]

    def get_required_digest(self, tag):
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

        exec_container = {
            "image": image,
            "name": f"{action.replace(' ', '-')}-bundle",
            "args": ['sh', '-ec', f'werf {command}'],
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
                "args": ['sh', '-ec', f'werf cr login {self.client.remote.hostname}'],
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
    repo_handler = RepoHandler(
        client=OrasClient(hostname=registry),
        repo=repo,
        version=version,
        namespace=project_namespace,
        secret_name=auth,
        **kwargs,
    )

    if auth:
        v1 = k8s_client.CoreV1Api()
        sec: dict[str, str] = v1.read_namespaced_secret(auth, namespace).data  # type: ignore
        username = base64.b64decode(sec["username"]).decode('utf-8')
        password = base64.b64decode(sec["password"]).decode('utf-8')
        repo_handler.login(password=password, username=username)

    return repo_handler


@kopf.on.create('operator.werf.dev', 'v1', 'Bundle')
@kopf.on.resume('operator.werf.dev', 'v1', 'Bundle')
def ready(spec, namespace, name, **_):
    try:
        NAMESPACED_REPOS[namespace][name] = get_image_repo(namespace, **spec)
    except Exception as err:
        raise kopf.TemporaryError(f"The data is not yet ready. {err}", delay=5)
    return 1


@kopf.on.update('operator.werf.dev', 'v1', 'Bundle')  # type: ignore
def ready(spec, name, namespace, body, **_):  # noqa: F811
    try:
        NAMESPACED_REPOS[namespace][name] = get_image_repo(namespace, **spec)
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
def update(name, namespace, status, body, patch, logger, **_):
    try:
        handler = NAMESPACED_REPOS[namespace][name]
    except KeyError as err:
        raise kopf.TemporaryError(f'Still initialize: {err}', delay=10)

    current_digest = status.get('deploy', {}).get('digest')
    try:
        latest_tag = handler.get_required_tag()
        latest_digest = handler.get_required_digest(latest_tag)
    except ValueError as err:
        kopf.exception(body, exc=err)
        raise kopf.TemporaryError(str(err))

    if current_digest == latest_digest and not status.get('forceUpdate'):
        return

    kopf.info(body, reason='Update', message='Initialize deploy application by digest update.')
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
