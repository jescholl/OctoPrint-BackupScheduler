"""A backup that comes due during a print runs when the print ends, without blocking OctoPrint's events.

Backup Scheduler defers a scheduled backup while printing and runs it on PrintDone/PrintFailed.
The backup waits for the core backup plugin's plugin_backup_backup_created event. OctoPrint
delivers events on a single thread, so waiting for that event from inside the PrintDone handler
can never finish: the backup completes, but the event never arrives, and OctoPrint stops
delivering events to every plugin until it restarts.
"""

from datetime import datetime

import pytest

GCODE = b"G28\nG90\nG4 P3000\nG1 X10 Y10 F6000\nG1 X20 Y20 F6000\nM84\n"


def schedule(**overrides):
	settings = {"enabled": True, "time": "00:00", "retention": 2, "exclude_uploads": False, "exclude_timelapse": False}
	settings.update(overrides)
	return settings


def print_with_deferred_backups(env, backup_types):
	r = env.api("POST", "/api/connection", json={"command": "connect", "port": "VIRTUAL", "baudrate": 115200})
	assert r.status_code == 204, r.text
	assert env.wait_for(lambda: env.printer_state() == "Operational", timeout=30), env.diagnostics()

	r = env.api("POST", "/api/files/local", files={"file": ("slow.gcode", GCODE)}, data={"print": "true"})
	assert r.status_code == 201, r.text
	assert env.wait_for(lambda: env.printer_state() == "Printing", timeout=15), env.diagnostics()

	# Scheduled backups come due mid-print: Backup Scheduler defers them.
	for backup_type in backup_types:
		r = env.api("POST", "/api/plugin/backup_trigger", json={"command": "backup", "backup_type": backup_type})
		assert r.status_code == 200, r.text
	assert r.json()["pending"] == backup_types

	assert env.wait_for(lambda: "PrintDone" in env.events_seen(), timeout=60), env.diagnostics()


def assert_events_still_flow(env):
	seen = len(env.events_seen())
	r = env.api("POST", "/api/connection", json={"command": "disconnect"})
	assert r.status_code == 204, r.text
	assert env.wait_for(lambda: "Disconnected" in env.events_seen()[seen:], timeout=30), env.diagnostics()


@pytest.mark.parametrize("hold_print_done", [
	# Backup Scheduler sees PrintDone after the printer is Operational, as on a real instance:
	# without the fix this is the deadlock.
	True,
	# Backup Scheduler sees PrintDone while the printer is still "Finishing": without the fix
	# the backup is deferred again, to the end of the next print.
	False,
])
def test_backup_deferred_by_a_print_runs_after_it_and_events_keep_flowing(make_env, hold_print_done):
	env = make_env(hold_print_done=hold_print_done, daily=schedule())

	print_with_deferred_backups(env, ["daily_backups"])

	# The deferred backup completes and is recorded (this needs its backup_created event).
	assert env.wait_for(lambda: len(env.plugin_setting("daily_backups")) == 1, timeout=60), env.diagnostics()
	assert_events_still_flow(env)


def test_every_deferred_backup_type_runs(make_env):
	env = make_env(daily=schedule(), weekly=schedule(day=datetime.now().isoweekday()))

	print_with_deferred_backups(env, ["daily_backups", "weekly_backups"])

	assert env.wait_for(lambda: len(env.plugin_setting("daily_backups")) == 1, timeout=60), env.diagnostics()
	assert env.wait_for(lambda: len(env.plugin_setting("weekly_backups")) == 1, timeout=60), env.diagnostics()
	assert_events_still_flow(env)
