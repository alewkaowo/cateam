.PHONY: help init configure config pull deploy update cert renew user admin backup restore status logs down restart check smoke rtc-check doctor prune compose
ACCOUNT ?=
BACKUP ?=
help:
	@echo 'init configure config deploy update user ACCOUNT=alice admin ACCOUNT=admin renew backup restore BACKUP=path status logs doctor prune smoke rtc-check'
init configure config pull deploy update cert renew backup status doctor prune monitor:
	python3 scripts/manage.py $@
user admin:
	python3 scripts/manage.py $@ '$(ACCOUNT)'
restore:
	python3 scripts/manage.py restore '$(BACKUP)'
logs:
	python3 scripts/manage.py compose logs --tail=100 -f
down:
	python3 scripts/manage.py compose down
restart:
	python3 scripts/manage.py compose restart
compose:
	python3 scripts/manage.py compose $(ARGS)
check:
	python3 -m unittest discover -s tests -v
smoke:
	python3 scripts/probe.py
rtc-check:
	python3 scripts/probe.py --rtc
