#!/usr/bin/env bash
# The one command set for the Terraform environments (US-30), dev and staging, on a Docker host:
#
#   scripts/env.sh up <env>      build the images and provision the environment from empty
#   scripts/env.sh check <env>   the gateway answers /healthz and /readyz; the dashboard serves
#   scripts/env.sh down <env>    destroy everything the environment created
#   scripts/env.sh gone <env>    prove nothing of it is left: no container, volume or network
#
# Needs Docker (DOCKER_HOST, or the local socket) and Terraform >= 1.6, or OpenTofu (TF=tofu).
# The state holds the generated passwords and keys: infra/terraform/envs/<env>/terraform.tfstate
# stays on this host (it is git-ignored); use a remote, encrypted backend to share it.
set -euo pipefail

cd "$(dirname "$0")/.."
TF="${TF:-terraform}"
usage="usage: scripts/env.sh up|check|down|gone dev|staging"
action="${1:?$usage}"
env="${2:?$usage}"
case "$env" in dev | staging) ;; *) echo "$usage" >&2 && exit 2 ;; esac
dir="infra/terraform/envs/$env"
tag="${IMAGE_TAG:-local}"

build() {
  export DOCKER_BUILDKIT=1
  docker build -q -t "finops-postgres:$tag" infra/docker/postgres
  docker build -q -t "finops-gateway:$tag" -f infra/docker/gateway/Dockerfile .
  docker build -q -t "finops-mock-llm:$tag" -f sidecars/mock_llm/Dockerfile .
  docker build -q -t "finops-docparse:$tag" -f sidecars/docparse/Dockerfile .
  docker build -q -t "finops-web:$tag" web
}

output() { "$TF" -chdir="$dir" output -raw "$1"; }

left_behind() {
  docker ps -a --format '{{.Names}}' | grep -E "^finops-$env-" || true
  docker volume ls --format '{{.Name}}' | grep -E "^finops-$env-" || true
  docker network ls --format '{{.Name}}' | grep -E "^finops-$env$" || true
}

case "$action" in
up)
  build
  "$TF" -chdir="$dir" init -input=false
  "$TF" -chdir="$dir" apply -input=false -auto-approve -var "image_tag=$tag"
  echo "$env: gateway $(output gateway_url), dashboard $(output web_url)"
  ;;
check)
  gateway="$(output gateway_url)"
  web="$(output web_url)"
  curl -fsS --retry 10 --retry-delay 3 --retry-all-errors "$gateway/healthz" >/dev/null
  curl -fsS --retry 10 --retry-delay 3 --retry-all-errors "$gateway/readyz" >/dev/null
  curl -fsS --retry 10 --retry-delay 3 --retry-all-errors "$web/" | grep -q '<div id="root">'
  curl -sS "$web/v1/models" -o /dev/null -w '%{http_code}\n' | grep -q '^401$' ||
    { echo "the dashboard does not reach the gateway" >&2 && exit 1; }
  echo "$env answers: $gateway, $web"
  ;;
down)
  "$TF" -chdir="$dir" destroy -input=false -auto-approve -var "image_tag=$tag"
  ;;
gone)
  left="$(left_behind)"
  if [ -n "$left" ]; then
    printf 'left behind:\n%s\n' "$left" >&2
    exit 1
  fi
  echo "nothing of $env is left"
  ;;
*)
  echo "$usage" >&2
  exit 2
  ;;
esac
