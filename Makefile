.PHONY: install fmt lint typecheck test check image test-container

install:
	$(MAKE) -C server install
	$(MAKE) -C clients/python install

fmt lint typecheck test:
	$(MAKE) -C server $@
	$(MAKE) -C clients/python $@

check: lint typecheck test

image:
	docker build --platform linux/amd64 -f server/Dockerfile -t nao-bridge-core .

test-container: image
	docker run --rm --network none --entrypoint /opt/modern/bin/python3 \
		-v "$(CURDIR)/server/tests/container_smoke.py:/tests/container_smoke.py:ro" \
		-v "$(CURDIR)/scripts/conformance.py:/tests/conformance.py:ro" \
		nao-bridge-core /tests/container_smoke.py
