#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'EOF'
Build and publish the accounting reconciliation images to Harbor.

Usage:
  scripts/publish-images.sh VERSION_TAG

Example:
  scripts/publish-images.sh 0.1.1

Optional environment variables:
  BACKEND_IMAGE_REPOSITORY  Backend repository (default: harbor.somenergia.coop/saldos_odoo/som-accounting-reconciliation-backend)
  FRONTEND_IMAGE_REPOSITORY Frontend repository (default: harbor.somenergia.coop/saldos_odoo/som-accounting-reconciliation-frontend)
EOF
}

fail() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
	usage
	exit 0
fi

[[ "$#" -eq 1 ]] || {
	usage >&2
	exit 2
}

version_tag="$1"
case "$version_tag" in
	''|[-.]*|*[!a-zA-Z0-9_.-]*) fail "Invalid Docker image tag: ${version_tag}" ;;
esac
[[ "${#version_tag}" -le 128 ]] || fail "Docker image tags must not exceed 128 characters."
[[ "$version_tag" != "latest" ]] || fail "Use a version tag; the script publishes latest automatically."

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "${script_dir}/.." && pwd)"
registry="harbor.somenergia.coop"
backend_repository="${BACKEND_IMAGE_REPOSITORY:-${registry}/saldos_odoo/som-accounting-reconciliation-backend}"
frontend_repository="${FRONTEND_IMAGE_REPOSITORY:-${registry}/saldos_odoo/som-accounting-reconciliation-frontend}"

for command in docker git; do
	command -v "$command" >/dev/null 2>&1 || fail "Required command not found: ${command}"
done

git -C "$project_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "Project directory is not a Git working tree."
if [[ -n "$(git -C "$project_dir" status --porcelain)" ]]; then
	fail "Working tree is not clean. Commit or remove changes before publishing so the image tag identifies the committed source."
fi

for repository in "$backend_repository" "$frontend_repository"; do
	[[ "$repository" != *:* ]] || fail "Image repository must not contain a tag: ${repository}"
	[[ "$repository" == "${registry}/"* ]] || fail "Image repositories must be in ${registry}."
done

docker info >/dev/null 2>&1 || fail "Cannot reach the Docker daemon."
[[ -f "${project_dir}/backend/Dockerfile" ]] || fail "Backend Dockerfile not found."
[[ -f "${project_dir}/frontend/Dockerfile" ]] || fail "Frontend Dockerfile not found."

backend_versioned_image="${backend_repository}:${version_tag}"
backend_latest_image="${backend_repository}:latest"
frontend_versioned_image="${frontend_repository}:${version_tag}"
frontend_latest_image="${frontend_repository}:latest"

printf 'Source commit: %s\n' "$(git -C "$project_dir" rev-parse --short HEAD)"
printf 'Backend:  %s and %s\n' "$backend_versioned_image" "$backend_latest_image"
printf 'Frontend: %s and %s\n' "$frontend_versioned_image" "$frontend_latest_image"
printf 'Building both images before publishing...\n'

docker build --pull \
	--file "${project_dir}/backend/Dockerfile" \
	--tag "$backend_versioned_image" \
	--tag "$backend_latest_image" \
	"${project_dir}/backend"

docker build --pull \
	--file "${project_dir}/frontend/Dockerfile" \
	--tag "$frontend_versioned_image" \
	--tag "$frontend_latest_image" \
	"${project_dir}/frontend"

printf 'Pushing versioned images...\n'
docker push "$backend_versioned_image"
docker push "$frontend_versioned_image"

printf 'Pushing latest images...\n'
docker push "$backend_latest_image"
docker push "$frontend_latest_image"

printf 'Published all images successfully.\n'
