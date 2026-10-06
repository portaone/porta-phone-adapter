# Jenkins pipelines

| File | Job | What it does |
|---|---|---|
| `build.groovy` | `Devel/webtrit/adapter_python-build` | Builds and publishes the adapter image (REL-5040) |
| `gerrit_check_pipeline.groovy` | Gerrit check | Runs the unit tests on every patchset (WT-2002) |

## Gerrit checks (WT-2002)

`gerrit_check_pipeline.groovy` runs the adapter unit tests on every patchset uploaded to
any branch and votes **Verified +1** (passed) or **-1** (failed) through the Gerrit
Trigger plugin. Jenkins reads this file from the patchset, so every branch needs it: a new
branch gets it when cut from `main`, an older one needs it cherry-picked.

It builds the `test` stage of the root `Dockerfile` and runs it:

```sh
docker build --target test -t adapter_python-test .
docker run --rm adapter_python-test
```

That is also how to run the check locally. The `test` stage runs
`pytest tests --ignore-glob='tests/test_[01]*'`: the unit tests, `tests/test_30` and up.
`tests/test_01` to `test_15` are integration tests that need a running adapter and are
left out. The last stage of the `Dockerfile` is still the adapter image, so a plain
`docker build .` is unchanged.

Jenkins job: **Pipeline script from SCM**, Git
`ssh://jenkins@git.portaone.com:29418/porta-phone/adapter_python.git`, refspec
`$GERRIT_REFSPEC`, branch specifier `FETCH_HEAD`, script path
`.jenkins/gerrit_check_pipeline.groovy`. Run it once by hand so the trigger registers.
