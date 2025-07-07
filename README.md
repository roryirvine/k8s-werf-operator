<<<<<<< HEAD
=======
# Werf Kubernetes Operator

Simple kubernetes operator for checking registry and pull bundles to cluster via job.

## Getting started

#### From bundle

- Latest version:
  
  `werf bundle apply --repo=registry.gitlab.com/onegreyonewhite/werf-operator/bundles --tag latest --env production --release werf-operator --namespace werf-operator`

- Tagged version:

  `werf bundle apply --repo=registry.gitlab.com/onegreyonewhite/werf-operator/bundles --tag latest --env production --release werf-operator --namespace werf-operator`

Latest version updates on each tag deploy. So, for update just call command again.

#### From sources

- `git clone https://gitlab.com/onegreyonewhite/werf-operator.git`
- `cd werf-operator`
- `export WERF_REPO=...` with path to your registry repo
- `werf converge`

## Example

#### Run example

- `kubectl create namespace quickstart-application`
- [configure access to the container registry](https://werf.io/documentation/v1.2/advanced/ci_cd/run_in_container/use_kubernetes.html#2-configure-access-to-the-container-registry)
- `kubectl apply -f https://gitlab.com/onegreyonewhite/werf-operator/-/raw/main/test.yaml -n quickstart-application`

#### Explanation

```yaml
apiVersion: operator.werf.dev/v1
kind: Bundle
metadata:
  name: test-bundle-2
spec:
  registry: registry.gitlab.com
  repo: onegreyonewhite/werf-operator-test-bundle
  version: 0.1.*
  auth: public-registry
  values: test-values
```

- **apiVersion** and **kind** - CRDs parameters to be tracked by the operator.
- **spec.registry** - the address of the registry server to which the connection will be made to track version changes and deployment.
- **spec.repo** - the name of the repository on the registry server containing images and bundles.
- **spec.version** - version of the monitored bundle. Supported as a specific name (checked by digest and updated for change)
  or semver with mask on minor and patch revision. Default is `latest`.
- **spec.auth** - the name of the secret containing the username and password to connect to the registry. Default is empty.
- **spec.values** - the name of a configmap containing a list of files with deployment settings.
  It is assumed that the keys contain the *.yaml extension. Default is empty.

Required spec's only **registry** and **repo**. Secret `auth` and ConfigMap `values` should be in same namespace that has Bundle.
Also, namespace must be configured with [werf instructions](https://werf.io/documentation/v1.2/advanced/ci_cd/run_in_container/use_kubernetes.html#2-configure-access-to-the-container-registry).
>>>>>>> 05b11f0 (Update README.md instructions.)
