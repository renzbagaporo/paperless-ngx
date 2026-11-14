#!/usr/bin/env bash
# Check that management commands started as root in a built image run as the
# paperless user, both through a wrapper and as a bare manage.py.
# Usage: .github/scripts/check_image_user.sh IMAGE
set -euo pipefail

image="$1"
expected="$(docker run --rm --entrypoint id "${image}" -u paperless)"

check() {
	local label="$1"
	shift
	local uid
	uid="$(docker run --rm --user root "$@" | tail -n 1)"
	if [[ "${uid}" != "${expected}" ]]; then
		echo "${label} ran as uid ${uid}, expected paperless (${expected})" >&2
		exit 1
	fi
	echo "${label} runs as paperless (uid ${uid})"
}

check "manage.py" --entrypoint python3 "${image}" \
	manage.py shell -c 'import os; print(os.geteuid())'
check "python3 src/manage.py from the image root" \
	--workdir /usr/src/paperless --entrypoint python3 "${image}" \
	src/manage.py shell -c 'import os; print(os.geteuid())'

for wrapper in document_ocr document_page_data; do
	docker run --rm --entrypoint test "${image}" -x "/usr/local/bin/${wrapper}"
done
echo "Wrappers for the fork's commands are installed"
