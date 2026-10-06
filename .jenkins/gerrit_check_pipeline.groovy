pipeline {
  agent { label 'centos-lxc-ci-2' }
  options {
    buildDiscarder(logRotator(daysToKeepStr:'14'))
  }
  environment {
    DOCKER_IO_CREDS=credentials('docker_creds')
    DOCKER_CONFIG="${WORKSPACE}/.docker"
    TEST_IMAGE="adapter_python-test:${BUILD_TAG}"
  }
  triggers {
    gerrit(serverName: 'git.portaone.com',
         gerritProjects: [[
           compareType: 'PLAIN',
           pattern: 'porta-phone/adapter_python',
           branches: [[ compareType: 'REG_EXP', pattern: '.*' ]],
           disableStrictForbiddenFileVerification: false
         ]],
         triggerOnEvents: [patchsetCreated()]
    )
  }
  stages {
    stage('Login to Docker Hub') {
      steps {
        sh label: "Docker Hub login", script:
          "echo ${DOCKER_IO_CREDS_PSW} | docker login --username ${DOCKER_IO_CREDS_USR} --password-stdin"
      }
    }
    stage('Run unit tests') {
      steps {
        sh 'docker build --target test -t "$TEST_IMAGE" .'
        sh 'docker run --rm "$TEST_IMAGE"'
      }
      post {
        always {
          sh 'docker rmi "$TEST_IMAGE" || true'
          sh "docker logout"
        }
      }
    }
  }
  post {
    always {
      cleanWs()
    }
  }
}
