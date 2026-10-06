# Jenkins pipelines

| File | Job | What it does |
|---|---|---|
| `build.groovy` | `Devel/webtrit/adapter_python-build` | Builds and publishes the adapter image (REL-5040) |
| `gerrit_check_pipeline.groovy` | Gerrit check | Runs the unit tests on every patchset (WT-2002) |

## Gerrit checks (WT-2002)

### What triggers it

Every new patchset uploaded to `porta-phone/adapter_python`, on any branch, when it
changes at least one of:

- `app/**`, `tests/**`, `pyproject.toml`, `Dockerfile`
- the check itself: `.jenkins/gerrit_check/**`, `.jenkins/gerrit_check_pipeline.groovy`

Drafts and patchsets that only change the commit message (no code change) do not trigger it.

### What runs

1. Jenkins checks out the patchset (`GERRIT_REFSPEC`) on the `centos-lxc-ci-2` agent.
2. `docker compose build` builds the `test` stage of the repository's `Dockerfile`: the
   adapter image's base (Python and `app/requirements.txt`) plus `tests/requirements.txt`,
   `app/` and `tests/`.
3. `docker compose run --rm test` runs the unit tests in that image (the `test` stage's `CMD`).
   The run takes about 1 minute, plus the image build when a requirements file changed.
4. The containers and the image are always removed afterwards.

Only the **unit tests** run: `pytest tests --ignore-glob='tests/test_[01]*'`, i.e.
`tests/test_30` and up. They import the adapter from `app/` and stub its API objects. A
new unit test file is picked up automatically unless it is numbered below 20.

The following are **not** run:

- `tests/test_01` to `test_15`: integration tests that need a running adapter (`--server`).
- `app/bss/adapters/portaswitch/tests/`: integration tests that need a running adapter
  connected to a PortaSwitch.

### Result in Gerrit

The Gerrit Trigger plugin votes on the change: **Verified +1** if all tests pass and
**Verified -1** if any test fails or the build breaks. The vote comment links to the
Jenkins build.

### Run it locally

From the repository root, exactly as Jenkins does:

```sh
export COMPOSE_FILE=.jenkins/gerrit_check/compose.yml COMPOSE_PROJECT_NAME=gc-local
docker compose build
docker compose run --rm test
docker compose down --volumes --remove-orphans --rmi local
```

Single file: `docker compose run --rm test python -m pytest -v tests/test_36_portaswitch_call_queues.py`.

### Files

| File | Purpose |
|---|---|
| `.jenkins/gerrit_check_pipeline.groovy` | Jenkins pipeline: Gerrit trigger, checkout, Docker Hub login, build and run, cleanup |
| `.jenkins/gerrit_check/compose.yml` | The `test` service (build context = repository root) |
| `Dockerfile` | Stage `test` is the test image and runs the unit tests; the last stage is still the adapter image |

### Jenkins job setup (once)

1. New **Pipeline** job, e.g. `Devel/webtrit/adapter_python-gerrit-check`.
2. Definition: **Pipeline script from SCM**, Git
   `ssh://jenkins@git.portaone.com:29418/porta-phone/adapter_python.git`, branch `main`,
   script path `.jenkins/gerrit_check_pipeline.groovy`, **Lightweight checkout** on.
   That's only where Jenkins reads the pipeline from. The pipeline then checks out the
   patchset itself (`GERRIT_REFSPEC`).
3. Run the job manually once. The run fails because there is no Gerrit event, but it
   registers the `triggers { gerrit(...) }` block. From then on, patchsets trigger the job.

### Branches without the check

Jenkins reads the pipeline from the default branch, but the compose file and
Dockerfile come from the patchset. On a branch that does not contain them yet (an
older release branch, for example) the run ends as **NOT_BUILT** right after
checkout and Gerrit gets no vote. To get checks there, cherry-pick the
`.jenkins/` check files onto that branch.
