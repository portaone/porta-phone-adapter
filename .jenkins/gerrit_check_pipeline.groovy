// Gerrit check for porta-phone/adapter_python : adapter (WT-2002).
//
// Runs the adapter unit tests in Docker on every patchset uploaded to Gerrit that
// touches app/, tests/, pyproject.toml or the check itself, and votes Verified +1 / -1
// on the change through the Gerrit Trigger plugin. See .jenkins/README.md for how the
// checks are set up.
//
// Everything runs inside containers described by .jenkins/gerrit_check/compose.yml; the
// agent only needs git and Docker. The compose project name is unique per build, so
// parallel checks on the shared agent never share containers, networks or volumes.

pipeline {
  agent { label 'centos-lxc-ci-2' }

  options {
    buildDiscarder(logRotator(daysToKeepStr: '14'))
    timeout(time: 20, unit: 'MINUTES')
    // The checkout is done explicitly below, from the patchset ref the trigger supplies.
    skipDefaultCheckout()
  }

  triggers {
    gerrit(serverName: 'git.portaone.com',
           gerritProjects: [[compareType: 'PLAIN',
                             pattern: 'porta-phone/adapter_python',
                             branches: [[compareType: 'REG_EXP', pattern: '.*']],
                             filePaths: [[compareType: 'ANT', pattern: 'app/**'],
                                         [compareType: 'ANT', pattern: 'tests/**'],
                                         [compareType: 'ANT', pattern: 'pyproject.toml'],
                                         [compareType: 'ANT', pattern: 'Dockerfile'],
                                         [compareType: 'ANT', pattern: '.jenkins/gerrit_check/**'],
                                         [compareType: 'ANT', pattern: '.jenkins/gerrit_check_pipeline.groovy']],
                             disableStrictForbiddenFileVerification: false]],
           triggerOnEvents: [patchsetCreated(excludeDrafts: true, excludeNoCodeChange: true)])
  }

  environment {
    DOCKER_CONFIG = "${WORKSPACE}/.docker"
    COMPOSE_FILE  = '.jenkins/gerrit_check/compose.yml'
    // Lower-case letters, digits, '-' and '_' only: what docker compose accepts.
    COMPOSE_PROJECT_NAME = "${env.BUILD_TAG.toLowerCase().replaceAll(/[^a-z0-9_-]/, '-')}"
  }

  stages {
    stage('Checkout') {
      steps {
        // changelog: false - the git plugin computes it with 'git whatchanged', which the
        // agent's git refuses to run (deprecated).
        checkout(changelog: false, poll: false, scm: [$class: 'GitSCM',
                  branches: [[name: env.GERRIT_PATCHSET_REVISION]],
                  userRemoteConfigs: [[url: 'ssh://jenkins@git.portaone.com:29418/porta-phone/adapter_python.git',
                                       refspec: env.GERRIT_REFSPEC]]])
        sh 'git --no-pager log -1 --oneline'
        script {
          currentBuild.description = "${env.GERRIT_CHANGE_NUMBER},${env.GERRIT_PATCHSET_NUMBER}: ${env.GERRIT_CHANGE_SUBJECT}"
          // The job runs this file from the default branch, but the compose file comes from the
          // patchset. A branch that does not have the check yet (an old release branch) has
          // nothing to run: end as NOT_BUILT, which the Gerrit Trigger reports without a vote.
          if (!fileExists(env.COMPOSE_FILE)) {
            env.SKIP_CHECK = 'true'
            currentBuild.result = 'NOT_BUILT'
            echo "${env.COMPOSE_FILE} is not on ${env.GERRIT_BRANCH}; nothing to check."
          }
        }
      }
    }

    stage('Docker Hub login') {
      when { expression { env.SKIP_CHECK != 'true' } }
      steps {
        // Base images come from Docker Hub, whose anonymous pull limit is shared by everything
        // behind this agent's address. A missing credential must not fail the check.
        script {
          try {
            withCredentials([usernamePassword(credentialsId: 'docker_creds',
                                              usernameVariable: 'DH_USR', passwordVariable: 'DH_PSW')]) {
              sh 'echo "$DH_PSW" | docker login --username "$DH_USR" --password-stdin'
            }
          } catch (err) {
            echo "Docker Hub login skipped (${err.message}) - continuing with anonymous pulls"
          }
        }
      }
    }

    stage('Unit tests') {
      when { expression { env.SKIP_CHECK != 'true' } }
      steps {
        sh 'docker compose build'
        sh 'docker compose run --rm test'
      }
    }
  }

  post {
    always {
      sh 'docker compose down --volumes --remove-orphans --rmi local || true'
      sh 'docker logout || true'
      cleanWs()
    }
  }
}
