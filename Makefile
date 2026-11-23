.PHONY: install fmt lint typecheck test check image test-container

install:
	$(MAKE) -C server install
	$(MAKE) -C clients/python install
	$(MAKE) -C examples/recorder install
	$(MAKE) -C examples/wanderer install

fmt lint typecheck:
	$(MAKE) -C server $@
	$(MAKE) -C clients/python $@
	$(MAKE) -C examples/recorder $@
	$(MAKE) -C examples/wanderer $@

test:
	$(MAKE) -C server test
	$(MAKE) -C clients/python test

check: lint typecheck test

image:
	docker build --platform linux/amd64 -f server/Dockerfile -t nao-bridge-core .

test-container: image
	docker run --rm --network none --entrypoint /opt/modern/bin/python3 \
		-v "$(CURDIR)/server/tests/container_smoke.py:/tests/container_smoke.py:ro" \
		-v "$(CURDIR)/scripts/conformance.py:/tests/conformance.py:ro" \
		-v "$(CURDIR)/contracts/fixtures/presentation-sequence.json:/tests/presentation-sequence.json:ro" \
		nao-bridge-core /tests/container_smoke.py
