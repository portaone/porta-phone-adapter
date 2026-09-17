// Pipeline for Devel/webtrit/adapter_python-build on jenkins.int.portaone.com (REL-5040).
//
// Builds the WebTrit BSS adapter (python) image from this repository and archives it as a
// build artefact.
//
// Job parameters:
//   SRC_REF      - mandatory. Branch, tag or commit of this repository to build.
//                  A vX.Y.Z tag produces the release image :X.Y.Z; any other ref produces
//                  dev-<short sha>, because this component keeps no version in its sources -
//                  the version exists only in the git tag. Default main.
//
// The job also builds when a v* tag is pushed to this repository: the Gerrit ref-updated
// event supplies GERRIT_REFNAME. The trigger accepts only that pattern, so any other ref
// arriving here fails the build with an explanation instead of being ignored.
// A deleted tag arrives as the same ref-updated event with GERRIT_NEWREV all zeros; the run
// then ends as NOT_BUILT without a failure email, because there is nothing to build.
//
// Only the root Dockerfile is in scope. The repository also holds tests/Dockerfile and
// app/bss/adapters/portaswitch/Dockerfile, which the old workflow never built either.
//
// "Publish" pushes the image to registry.portaone.com with the harbor-webtrit-ci-rw
// credential. Nothing guards an existing tag, so rerunning a version replaces what is
// published under it.

pipeline {
  agent { label 'centos-lxc-ci-2' }

  options {
    buildDiscarder(logRotator(daysToKeepStr: '14'))
    disableConcurrentBuilds()
  }

  triggers {
    gerrit(serverName: 'git.portaone.com',
           gerritProjects: [[compareType: 'PLAIN',
                             pattern: 'porta-phone/adapter_python',
                             branches: [[compareType: 'REG_EXP', pattern: '^refs/tags/v.*$']],
                             disableStrictForbiddenFileVerification: false]],
           triggerOnEvents: [refUpdated()])
  }

  environment {
    IMAGE       = 'registry.portaone.com/webtrit/webtrit_bss_adapter_python'
    REGISTRY    = 'registry.portaone.com'
    REPO_URL    = 'ssh://jenkins@git.portaone.com:29418/porta-phone/adapter_python.git'
    DOCKER_CONFIG = "${WORKSPACE}/.docker"
  }

  stages {

    stage('Checkout') {
      steps {
        script {
          // A Gerrit ref-updated event decides what to build; a manual or API run uses SRC_REF.
          // The trigger only accepts refs/tags/v*, so any other ref arriving here means it was
          // widened or misconfigured - fail loudly rather than build something unintended.
          if (env.GERRIT_EVENT_TYPE == 'ref-updated' && env.GERRIT_REFNAME) {
            if (!env.GERRIT_REFNAME.startsWith('refs/tags/v')) {
              error("Triggered by ${env.GERRIT_REFNAME}, which is not a release tag; the Gerrit trigger should only accept refs/tags/v*.")
            }
            // Gerrit emits ref-updated for a tag deletion as well, with GERRIT_NEWREV all zeros,
            // and the trigger cannot filter deletions out. There is nothing to build, so stop
            // here as NOT_BUILT: a build result can only get worse, and NOT_BUILT already ranks
            // above the FAILURE that error() would set, so post { failure } does not fire.
            if (env.GERRIT_NEWREV ==~ /^0+$/) {
              currentBuild.result = 'NOT_BUILT'
              error("Tag ${env.GERRIT_REFNAME} was deleted; nothing to build.")
            }
            env.SRC = env.GERRIT_REFNAME.replaceFirst('^refs/tags/', '')
          } else {
            env.SRC = (params.SRC_REF ?: 'main').trim()
            if (!env.SRC) { error('SRC_REF must not be empty') }
          }
        }
        // changelog: false - the git plugin computes it with 'git whatchanged', which the
        // agent's git refuses to run (deprecated); it only produced a stack trace per build.
        checkout(changelog: false, poll: false, scm: [$class: 'GitSCM',
                  branches: [[name: env.SRC]],
                  userRemoteConfigs: [[url: env.REPO_URL,
                                       refspec: '+refs/heads/*:refs/remotes/origin/* +refs/tags/*:refs/tags/*']]])
        sh 'git --no-pager log -1 --oneline'
      }
    }

    stage('Resolve version') {
      steps {
        script {
          def sha = sh(returnStdout: true, script: 'git rev-parse --short HEAD').trim()
          // A bare 'v' prefix is not enough to recognise a release: a branch such as
          // verify-something also starts with it. Only vX.Y.Z names a release.
          if (env.SRC ==~ /^v\d+\.\d+\.\d+.*$/) {
            // The tag names the release. This component has no version in its sources at all:
            // pyproject.toml holds only tooling config.
            env.VERSION = env.SRC.substring(1)
            env.RELEASE = 'true'
          } else {
            // Not a release tag, and there is no in-source version to derive one from, so mark
            // it plainly as a development build.
            env.VERSION = "dev-${sha}"
            env.RELEASE = 'false'
          }
          currentBuild.description = "${env.SRC} -> ${env.IMAGE}:${env.VERSION}"
          echo "Building ${env.IMAGE}:${env.VERSION} (release=${env.RELEASE})"
        }
      }
    }

    stage('Docker preflight') {
      steps {
        sh 'docker version && docker info --format "server={{.ServerVersion}} driver={{.Driver}} root={{.DockerRootDir}}"'
      }
    }

    stage('Build image') {
      steps {
        // The base image comes from Docker Hub, whose anonymous pull limit is shared by
        // everything behind this agent's address; authenticating lifts it. A missing
        // credential must not fail a build that may well not need to pull at all.
        script {
          try {
            withCredentials([usernamePassword(credentialsId: 'docker_creds',
                                              usernameVariable: 'DH_USR', passwordVariable: 'DH_PSW')]) {
              sh 'echo "$DH_PSW" | docker login --username "$DH_USR" --password-stdin'
            }
            echo 'Docker Hub login: ok'
          } catch (err) {
            echo "Docker Hub login skipped (${err.message}) - continuing with anonymous pulls"
          }
        }
        sh '''set -eu
          # Context is the repository root: the Dockerfile copies app/ from there, and the
          # .dockerignore that applies is the one beside it. No build args - the Dockerfile's
          # ARG defaults are overridden at deploy time by the addon-mart chart, and baking a
          # value here would freeze it into the image.
          docker build -f Dockerfile . \
            -t "$IMAGE:$VERSION" \
            --label GIT_REVISION="$(git rev-parse HEAD)" \
            --label GIT_COMMIT_DATE="$(git show -s --format=%ci HEAD)"
          docker image inspect "$IMAGE:$VERSION" --format 'image={{.Id}} size={{.Size}} created={{.Created}}'
        '''
      }
    }

    stage('Export image') {
      steps {
        // The image is this job's only output while the push is not executed, so it has to
        // leave the agent as a build artefact or the run produces nothing.
        sh '''set -eu
          rm -f webtrit_bss_adapter_python-*.tar.gz
          docker save "$IMAGE:$VERSION" | gzip -1 > "webtrit_bss_adapter_python-$VERSION.tar.gz"
          ls -l "webtrit_bss_adapter_python-$VERSION.tar.gz"
        '''
        archiveArtifacts artifacts: 'webtrit_bss_adapter_python-*.tar.gz', fingerprint: true, onlyIfSuccessful: true
      }
    }

    stage('Publish') {
      steps {
        withCredentials([usernamePassword(credentialsId: 'harbor-webtrit-ci-rw',
                                          usernameVariable: 'REG_USR', passwordVariable: 'REG_PSW')]) {
          sh 'echo "$REG_PSW" | docker login "$REGISTRY" --username "$REG_USR" --password-stdin'
          // Only the exact version tag is published - the old workflow also moved an X.Y tag,
          // which nothing consumes.
          sh 'docker push "$IMAGE:$VERSION"'
          echo "Pushed ${env.IMAGE}:${env.VERSION}"
        }
      }
      post { always { sh 'docker logout "$REGISTRY" || true' } }
    }
  }

  post {
    failure {
      emailext attachLog: true,
               to: 'volodymyr.bohdan@portaone.com',
               subject: "Jenkins Build ${currentBuild.currentResult}: Job ${env.JOB_NAME}",
               body: "${currentBuild.currentResult}: Job ${env.JOB_NAME} build ${env.BUILD_NUMBER}\n${env.BUILD_URL}"
    }
    always {
      // The artefact has been archived by now; drop the local tag so the shared agent does not
      // accumulate one image per build.
      // VERSION is unset when the run stops before 'Resolve version' (a deleted tag, a bad
      // ref); skip the removal then rather than log 'invalid reference format'.
      sh '[ -z "${VERSION:-}" ] || docker rmi "$IMAGE:$VERSION" || true'
      sh 'docker logout || true'
      cleanWs()
    }
  }
}
