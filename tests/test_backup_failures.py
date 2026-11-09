"""Failed and overlapping backups.

A backup waits for OctoPrint's backup plugin to report it created. When the backup fails, that
report never comes, so the wait must notice the failure on its own, and must not record the
failed backup or apply retention to it (which would delete a good backup). Two backups (say a
scheduled one and one deferred by a print) must not run at the same time: they share the
"backup in progress" state.
"""

import time
from datetime import datetime

import pytest

OLD = "octoprint-daily-20200101-000000.zip"


def schedule(**overrides):
	settings = {"enabled": True, "time": "00:00", "retention": 1, "exclude_uploads": False, "exclude_timelapse": False}
	settings.update(overrides)
	return settings


@pytest.mark.parametrize("mode", [
	# Writing the zip fails: OctoPrint calls the after_backup hook with error=True.
	"write",
	# Not enough free space: raised before the after_backup hook, so only the backup plugin going idle shows it.
	"space",
])
def test_a_failed_backup_is_not_recorded_or_retained_and_is_retried(make_env, mode):
	env = make_env(seed_backups=[OLD], daily=schedule(), daily_backups=[OLD])

	env.trigger("fail", mode=mode)
	env.trigger("backup", backup_type="daily_backups", background=True)

	assert env.wait_for(lambda: "daily_backups octoprint-daily-" in env.log_text()
						and "failed. It will be retried" in env.log_text(), timeout=45), env.diagnostics()
	# Nothing recorded and no retention: the earlier backup is untouched.
	assert env.plugin_setting("daily_backups") == [OLD]
	assert env.backup_file(OLD).exists()
	status = env.scheduler_status()
	assert status["backup_pending"] is True
	assert status["pending"] == ["daily_backups"]

	# The next backup works: nothing is left waiting on the failed one.
	env.trigger("fail", mode="")
	env.trigger("backup", backup_type="daily_backups", background=True)
	assert env.wait_for(lambda: env.plugin_setting("daily_backups") not in ([], [OLD]), timeout=45), env.diagnostics()
	recorded = env.plugin_setting("daily_backups")
	assert len(recorded) == 1 and env.backup_file(recorded[0]).exists()
	assert not env.backup_file(OLD).exists()  # retention 1 now replaces it with a real backup


def test_overlapping_backups_run_one_after_the_other(make_env):
	env = make_env(daily=schedule(), weekly=schedule(day=datetime.now().isoweekday()))
	env.trigger("slow", seconds=3)

	# e.g. a scheduled backup comes due while a deferred one is still running.
	env.trigger("backup", backup_type="daily_backups", background=True)
	env.trigger("backup", backup_type="weekly_backups", background=True)

	# Every backup is recorded only once its zip exists.
	end = time.time() + 45
	recorded = {}
	while time.time() < end and len(recorded) < 2:
		for backup_type in ("daily_backups", "weekly_backups"):
			names = env.plugin_setting(backup_type)
			if names and backup_type not in recorded:
				recorded[backup_type] = (names[0], env.backup_file(names[0]).exists())
		time.sleep(0.1)

	assert len(recorded) == 2, env.diagnostics()
	assert recorded["daily_backups"][1] and recorded["weekly_backups"][1], (recorded, env.diagnostics())
