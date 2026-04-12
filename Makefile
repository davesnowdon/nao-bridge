.PHONY: install fmt lint typecheck test check server-init server-up server-bash

# Client library targets (delegate to clients/python/)
install:
	$(MAKE) -C clients/python install

fmt:
	$(MAKE) -C clients/python fmt

lint:
	$(MAKE) -C clients/python lint

typecheck:
	$(MAKE) -C clients/python typecheck

test:
	$(MAKE) -C clients/python test

check:
	$(MAKE) -C clients/python check

# Server targets (delegate to server/)
server-init:
	$(MAKE) -C server init

server-up:
	$(MAKE) -C server up

server-bash:
	$(MAKE) -C server bash
